"""Live "is this provider working right now" checks for the admin
"Ressources" page. Checks both environment variables (.env / Coolify) and
keys configured in the database via the admin panel (HuggingFaceAccount table).
"""
import httpx
from src.config import (
    ANTHROPIC_API_KEY, FAL_API_KEY, OPENAI_API_KEY,
    IZIVOICE_API_KEY, IZIVOICE_BASE_URL,
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, GROQ_API_KEY,
    KIE_API_KEY, KIE_BASE_URL, KIE_CLAUDE_MODEL,
    AI33PRO_API_KEY, AI33PRO_BASE_URL,
    XAI_API_KEY, XAI_BASE_URL,
    GEMINI_API_KEY,
)

PROBE_TIMEOUT = 15.0


def _get_effective_key(provider: str, env_key: str = "") -> str:
    """Returns the environment key if non-empty, otherwise searches for an
    enabled key in the database pool (HuggingFaceAccount table)."""
    if env_key:
        return env_key
    try:
        from src.db.session import SessionLocal
        from src.db.models import HuggingFaceAccount
        db = SessionLocal()
        try:
            account = (
                db.query(HuggingFaceAccount)
                .filter(HuggingFaceAccount.provider == provider, HuggingFaceAccount.is_enabled == True)  # noqa: E712
                .order_by(HuggingFaceAccount.last_used_at.asc().nullsfirst())
                .first()
            )
            if account and account.token:
                return account.token
        finally:
            db.close()
    except Exception:
        pass
    return ""


def _check_anthropic():
    key = _get_effective_key("anthropic", ANTHROPIC_API_KEY)
    if not key:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        client.messages.count_tokens(model="claude-sonnet-5", messages=[{"role": "user", "content": "ping"}])
        return {"configured": True, "status": "ok", "detail": "Clé valide. Aucun solde consultable via l'API Anthropic."}
    except Exception as exc:
        import anthropic
        if isinstance(exc, anthropic.RateLimitError) or "credit balance" in str(exc).lower() or "insufficient" in str(exc).lower():
            return {"configured": True, "status": "quota_exhausted", "detail": f"Clé valide mais solde/quota insuffisant : {exc}"}
        return {"configured": True, "status": "error", "detail": f"Clé invalide, révoquée, ou service injoignable : {exc}"}


def _check_openai():
    key = _get_effective_key("openai", OPENAI_API_KEY)
    if not key:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    try:
        # 1-token minimal test to detect real quota exhaustion (402/429 / insufficient_quota)
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
            timeout=PROBE_TIMEOUT,
        )
        if resp.status_code in (402, 429) or "insufficient_quota" in resp.text.lower() or "quota" in resp.text.lower():
            return {"configured": True, "status": "quota_exhausted", "detail": "Clé valide mais solde/crédits insuffisants sur le compte OpenAI."}
        if resp.status_code == 401:
            return {"configured": True, "status": "error", "detail": "Clé invalide, révoquée, ou service injoignable."}
        resp.raise_for_status()
        return {"configured": True, "status": "ok", "detail": "Clé valide et solde disponible."}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (402, 429) or "insufficient_quota" in exc.response.text.lower() or "quota" in exc.response.text.lower():
            return {"configured": True, "status": "quota_exhausted", "detail": "Clé valide mais solde/quota épuisé."}
        return {"configured": True, "status": "error", "detail": f"Erreur OpenAI ({exc.response.status_code}) — service injoignable ou en panne."}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"OpenAI injoignable : {exc}"}


def _check_deepseek():
    key = _get_effective_key("deepseek", DEEPSEEK_API_KEY)
    if not key:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    try:
        resp = httpx.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "deepseek-v4-flash", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
            timeout=PROBE_TIMEOUT,
        )
        if resp.status_code in (402, 429) or "insufficient" in resp.text.lower():
            return {"configured": True, "status": "quota_exhausted", "detail": "Clé valide mais solde insuffisant sur le compte DeepSeek — à recharger sur platform.deepseek.com."}
        if resp.status_code == 401:
            return {"configured": True, "status": "error", "detail": "Clé invalide, révoquée, ou service injoignable."}
        resp.raise_for_status()
        return {"configured": True, "status": "ok", "detail": "Clé valide et compte crédité."}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (402, 429) or "insufficient" in exc.response.text.lower():
            return {"configured": True, "status": "quota_exhausted", "detail": "Solde insuffisant sur le compte DeepSeek."}
        return {"configured": True, "status": "error", "detail": f"Erreur DeepSeek ({exc.response.status_code}) — service injoignable ou en panne."}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"DeepSeek injoignable : {exc}"}


def _check_groq():
    key = _get_effective_key("groq", GROQ_API_KEY)
    if not key:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    try:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
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


def _check_kie():
    key = _get_effective_key("kie", KIE_API_KEY)
    if not key:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    try:
        resp = httpx.post(
            f"{KIE_BASE_URL}/claude/v1/messages",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": KIE_CLAUDE_MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": False, "max_tokens": 1},
            timeout=PROBE_TIMEOUT,
        )
        if resp.status_code in (402, 429) or "insufficient" in resp.text.lower() or "credit" in resp.text.lower():
            return {"configured": True, "status": "quota_exhausted", "detail": "Clé valide mais solde/crédits kie.ai insuffisants."}
        if resp.status_code == 401:
            return {"configured": True, "status": "error", "detail": "Clé invalide, révoquée, ou service injoignable."}
        resp.raise_for_status()
        return {"configured": True, "status": "ok", "detail": f"Clé valide. Modèle configuré : {KIE_CLAUDE_MODEL}."}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (402, 429) or "insufficient" in exc.response.text.lower() or "credit" in exc.response.text.lower():
            return {"configured": True, "status": "quota_exhausted", "detail": "Clé valide mais solde/crédits kie.ai insuffisants."}
        return {"configured": True, "status": "error", "detail": f"Erreur kie.ai ({exc.response.status_code})"}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"kie.ai injoignable : {exc}"}


def _check_fal():
    key = _get_effective_key("fal", FAL_API_KEY)
    if not key:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    return {"configured": True, "status": "unknown", "detail": "Clé présente. Pas de vérification en direct : tout appel fal.ai réel est payant, y compris un simple test."}


def _check_izivoice():
    key = _get_effective_key("izivoice", IZIVOICE_API_KEY)
    if not key:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé partagée configurée."}
    try:
        resp = httpx.get(f"{IZIVOICE_BASE_URL}/voices", headers={"Authorization": f"Bearer {key}"}, params={"page": 0, "page_size": 1}, timeout=PROBE_TIMEOUT)
        if resp.status_code in (402, 429):
            return {"configured": True, "status": "quota_exhausted", "detail": "Clé valide mais solde/quota insuffisant sur le compte Izivoice."}
        if resp.status_code in (401, 403):
            return {"configured": True, "status": "error", "detail": "Clé invalide, révoquée, ou service injoignable."}
        resp.raise_for_status()
        return {"configured": True, "status": "ok", "detail": "Clé valide. Izivoice n'expose aucun solde consultable via l'API."}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"Izivoice injoignable : {exc}"}


def _check_ai33pro():
    key = _get_effective_key("ai33pro", AI33PRO_API_KEY)
    if not key:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    return {"configured": True, "status": "unknown", "detail": "Clé présente."}


def _check_xai():
    key = _get_effective_key("xai", XAI_API_KEY)
    if not key:
        return {"configured": False, "status": "not_configured", "detail": "Aucune clé configurée."}
    try:
        resp = httpx.post(
            f"{XAI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "grok-2", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
            timeout=PROBE_TIMEOUT,
        )
        if resp.status_code in (402, 429):
            return {"configured": True, "status": "quota_exhausted", "detail": "Clé valide mais solde insuffisant sur le compte xAI."}
        if resp.status_code == 401:
            return {"configured": True, "status": "error", "detail": "Clé invalide, révoquée, ou service injoignable."}
        resp.raise_for_status()
        return {"configured": True, "status": "ok", "detail": "Clé valide."}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"xAI injoignable : {exc}"}


def check_all_providers() -> list:
    checks = [
        ("anthropic", "Anthropic (Claude)", _check_anthropic),
        ("kie", "Kie.ai", _check_kie),
        ("openai", "OpenAI", _check_openai),
        ("deepseek", "DeepSeek", _check_deepseek),
        ("groq", "Groq", _check_groq),
        ("fal", "fal.ai", _check_fal),
        ("izivoice", "Izivoice", _check_izivoice),
        ("ai33pro", "ai33.pro", _check_ai33pro),
        ("xai", "xAI (Grok)", _check_xai),
    ]
    results = []
    for provider_id, label, fn in checks:
        result = fn()
        result["id"] = provider_id
        result["label"] = label
        results.append(result)
    return results
