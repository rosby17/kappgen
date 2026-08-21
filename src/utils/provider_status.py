"""Live "is this provider working right now" checks for the admin
"Ressources" page — deliberately NOT a balance/credit reader, because most of
these providers don't expose one via API:

- Anthropic has no account-balance endpoint at all, so its check only
  confirms the key is valid (via the free /count_tokens call — no completion
  is generated, so this costs nothing).
- OpenAI's real usage/billing API needs an org-level Admin key, which is a
  different credential than the completions key configured here — its check
  is also just a key-validity probe (GET /v1/models).
- OpenRouter is the one exception: /api/v1/auth/key genuinely returns the
  key's remaining credit ("limit" minus "usage"), so its status includes a
  real balance.
- fal.ai has no free "ping" endpoint we know of — every real fal.ai call
  costs money, and burning fal.ai credits just to check whether fal.ai
  credits are available would be absurd. Its status is therefore limited to
  "is a key configured", nothing more.
- Izivoice: same situation as Anthropic — /voices with page_size=1 confirms
  the key works, but Izivoice doesn't return a balance either.
"""
import httpx
from src.config import (
    ANTHROPIC_API_KEY, FAL_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY,
    IZIVOICE_API_KEY, IZIVOICE_BASE_URL,
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
        return {"configured": True, "status": "error", "detail": f"Clé invalide ou compte à problème : {exc}"}


def _check_openai():
    if not OPENAI_API_KEY:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    try:
        resp = httpx.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=PROBE_TIMEOUT)
        if resp.status_code == 401:
            return {"configured": True, "status": "error", "detail": "Clé invalide ou révoquée."}
        resp.raise_for_status()
        return {"configured": True, "status": "ok", "detail": "Clé valide. Le solde/quota nécessite une clé Admin d'organisation, différente de celle-ci."}
    except httpx.HTTPStatusError as exc:
        return {"configured": True, "status": "error", "detail": f"Erreur OpenAI ({exc.response.status_code})."}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"OpenAI injoignable : {exc}"}


def _check_openrouter():
    if not OPENROUTER_API_KEY:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    try:
        resp = httpx.get("https://openrouter.ai/api/v1/auth/key", headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"}, timeout=PROBE_TIMEOUT)
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
        limit = data.get("limit")
        usage = data.get("usage")
        if limit is None:
            detail = f"Clé valide. Usage : ${usage:.2f} (illimité)." if usage is not None else "Clé valide."
        else:
            remaining = max(0, limit - (usage or 0))
            detail = f"Crédit restant estimé : ${remaining:.2f} (limite ${limit:.2f})."
        return {"configured": True, "status": "ok", "detail": detail, "balance_usd": (None if limit is None else round(max(0, limit - (usage or 0)), 2))}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"OpenRouter injoignable ou clé invalide : {exc}"}


def _check_fal():
    if not FAL_API_KEY:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    return {"configured": True, "status": "unknown", "detail": "Clé présente. Pas de vérification en direct : tout appel fal.ai réel est payant, y compris un simple test."}


def _check_izivoice():
    if not IZIVOICE_API_KEY:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé partagée configurée (les utilisateurs peuvent connecter la leur individuellement)."}
    try:
        resp = httpx.get(f"{IZIVOICE_BASE_URL}/voices", headers={"Authorization": f"Bearer {IZIVOICE_API_KEY}"}, params={"page": 0, "page_size": 1}, timeout=PROBE_TIMEOUT)
        if resp.status_code in (401, 403):
            return {"configured": True, "status": "error", "detail": "Clé invalide ou révoquée."}
        resp.raise_for_status()
        return {"configured": True, "status": "ok", "detail": "Clé valide. Izivoice n'expose aucun solde consultable via l'API."}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"Izivoice injoignable : {exc}"}


def check_all_providers() -> list:
    checks = [
        ("anthropic", "Anthropic (Claude)", _check_anthropic),
        ("openai", "OpenAI", _check_openai),
        ("openrouter", "OpenRouter", _check_openrouter),
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
