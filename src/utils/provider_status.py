"""Live "is this provider working right now" checks for the admin
"Ressources" page — deliberately NOT a balance/credit reader, because most of
these providers don't expose one via API:

- Anthropic has no account-balance endpoint at all, so its check only
  confirms the key is valid (via the free /count_tokens call — no completion
  is generated, so this costs nothing).
- OpenAI's real usage/billing API needs an org-level Admin key, which is a
  different credential than the completions key configured here — its check
  is also just a key-validity probe (GET /v1/models).
- fal.ai has no free "ping" endpoint we know of — every real fal.ai call
  costs money, and burning fal.ai credits just to check whether fal.ai
  credits are available would be absurd. Its status is therefore limited to
  "is a key configured", nothing more.
- Izivoice: same situation as Anthropic — /voices with page_size=1 confirms
  the key works, but Izivoice doesn't return a balance either.
- DeepSeek and Groq are both probed with a minimal 1-token completion (the
  cheapest real signal for "is this key actually usable right now" — a
  balance-less key still returns 401/402 on this, which is exactly the
  distinction the admin panel needs).

OpenRouter is intentionally not checked here: it's not part of the AI-text
provider chain (quality was rejected for this product), so it isn't
surfaced on the Ressources page at all.
"""
import httpx
from src.config import (
    ANTHROPIC_API_KEY, FAL_API_KEY, OPENAI_API_KEY,
    IZIVOICE_API_KEY, IZIVOICE_BASE_URL,
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, GROQ_API_KEY,
)

PROBE_TIMEOUT = 15.0


def _check_anthropic():
    if not ANTHROPIC_API_KEY:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        client.messages.count_tokens(model="claude-sonnet-5", messages=[{"role": "user", "content": "ping"}])
        return {"configured": True, "status": "ok", "detail": "Clé valide. Aucun solde consultable via l'API Anthropic."}
    except Exception as exc:
        import anthropic
        # Anthropic reports "credit balance too low" as a plain 400
        # (BadRequestError), not a dedicated status code — text match is the
        # only reliable signal, alongside the real RateLimitError (429).
        if isinstance(exc, anthropic.RateLimitError) or "credit balance" in str(exc).lower() or "insufficient" in str(exc).lower():
            return {"configured": True, "status": "quota_exhausted", "detail": f"Clé valide mais solde/quota insuffisant : {exc}"}
        return {"configured": True, "status": "error", "detail": f"Clé invalide, révoquée, ou service injoignable : {exc}"}


def _check_openai():
    if not OPENAI_API_KEY:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    try:
        resp = httpx.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=PROBE_TIMEOUT)
        if resp.status_code in (402, 429):
            return {"configured": True, "status": "quota_exhausted", "detail": "Clé valide mais solde/quota insuffisant sur le compte OpenAI."}
        if resp.status_code == 401:
            return {"configured": True, "status": "error", "detail": "Clé invalide, révoquée, ou service injoignable."}
        resp.raise_for_status()
        return {"configured": True, "status": "ok", "detail": "Clé valide. Le solde/quota nécessite une clé Admin d'organisation, différente de celle-ci."}
    except httpx.HTTPStatusError as exc:
        return {"configured": True, "status": "error", "detail": f"Erreur OpenAI ({exc.response.status_code}) — service injoignable ou en panne."}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"OpenAI injoignable : {exc}"}


def _check_deepseek():
    if not DEEPSEEK_API_KEY:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    try:
        resp = httpx.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={"model": "deepseek-v4-flash", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
            timeout=PROBE_TIMEOUT,
        )
        if resp.status_code in (402, 429):
            return {"configured": True, "status": "quota_exhausted", "detail": "Clé valide mais solde insuffisant sur le compte DeepSeek — à recharger sur platform.deepseek.com."}
        if resp.status_code == 401:
            return {"configured": True, "status": "error", "detail": "Clé invalide, révoquée, ou service injoignable."}
        resp.raise_for_status()
        return {"configured": True, "status": "ok", "detail": "Clé valide et compte crédité."}
    except httpx.HTTPStatusError as exc:
        return {"configured": True, "status": "error", "detail": f"Erreur DeepSeek ({exc.response.status_code}) — service injoignable ou en panne."}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"DeepSeek injoignable : {exc}"}


def _check_groq():
    if not GROQ_API_KEY:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    try:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "openai/gpt-oss-120b", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
            timeout=PROBE_TIMEOUT,
        )
        if resp.status_code in (402, 429):
            return {"configured": True, "status": "quota_exhausted", "detail": "Clé valide mais quota/solde insuffisant sur le compte Groq."}
        if resp.status_code == 401:
            return {"configured": True, "status": "error", "detail": "Clé invalide, révoquée, ou service injoignable."}
        resp.raise_for_status()
        return {"configured": True, "status": "ok", "detail": "Clé valide. Gratuit."}
    except httpx.HTTPStatusError as exc:
        return {"configured": True, "status": "error", "detail": f"Erreur Groq ({exc.response.status_code}) — service injoignable ou en panne."}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"Groq injoignable : {exc}"}


def _check_fal():
    if not FAL_API_KEY:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    return {"configured": True, "status": "unknown", "detail": "Clé présente. Pas de vérification en direct : tout appel fal.ai réel est payant, y compris un simple test."}


def _check_izivoice():
    if not IZIVOICE_API_KEY:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé partagée configurée (les utilisateurs peuvent connecter la leur individuellement)."}
    try:
        resp = httpx.get(f"{IZIVOICE_BASE_URL}/voices", headers={"Authorization": f"Bearer {IZIVOICE_API_KEY}"}, params={"page": 0, "page_size": 1}, timeout=PROBE_TIMEOUT)
        if resp.status_code in (402, 429):
            return {"configured": True, "status": "quota_exhausted", "detail": "Clé valide mais solde/quota insuffisant sur le compte Izivoice."}
        if resp.status_code in (401, 403):
            return {"configured": True, "status": "error", "detail": "Clé invalide, révoquée, ou service injoignable."}
        resp.raise_for_status()
        return {"configured": True, "status": "ok", "detail": "Clé valide. Izivoice n'expose aucun solde consultable via l'API."}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"Izivoice injoignable : {exc}"}


def check_all_providers() -> list:
    checks = [
        ("anthropic", "Anthropic (Claude)", _check_anthropic),
        ("openai", "OpenAI", _check_openai),
        ("deepseek", "DeepSeek", _check_deepseek),
        ("groq", "Groq (gratuit)", _check_groq),
        ("fal", "fal.ai", _check_fal),
        ("izivoice", "Izivoice", _check_izivoice),
    ]
    results = []
    for provider_id, label, fn in checks:
        result = fn()
        result["id"] = provider_id
        result["label"] = label
        results.append(result)
    return results
