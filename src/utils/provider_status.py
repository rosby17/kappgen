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
    OLLAMA_BASE_URL, OLLAMA_API_KEY, OLLAMA_MODEL,
)

PROBE_TIMEOUT = 15.0


def _get_effective_key(provider: str, env_key: str = "") -> str:
    from src.utils.provider_keys import key
    return key(provider)


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
            return {"configured": True, "status": "invalid", "detail": "Clé invalide, révoquée, ou service injoignable."}
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
            return {"configured": True, "status": "invalid", "detail": "Clé invalide, révoquée, ou service injoignable."}
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
            return {"configured": True, "status": "invalid", "detail": "Clé invalide, révoquée, ou service injoignable."}
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
        from src.utils.app_settings import selected_task_model
        model = selected_task_model("text", "kie") or KIE_CLAUDE_MODEL
        # The health probe follows the same endpoint family as production.
        if model.startswith("claude-"):
            path = "/claude/v1/messages"
            payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": False, "max_tokens": 8}
        elif model.startswith("gemini-"):
            path = f"/gemini/v1/models/{model}:streamGenerateContent"
            payload = {"stream": False, "contents": [{"role": "user", "parts": [{"text": "hi"}]}], "generationConfig": {"maxOutputTokens": 8}}
        elif model.startswith("grok-"):
            path = "/grok/v1/responses"
            payload = {"model": model, "stream": False, "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]}
        elif model.startswith("deepseek-"):
            path = "/deepseek/v1/chat/completions"
            payload = {"model": model, "stream": False, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
        else:
            path = "/codex/v1/responses"
            payload = {"model": model, "stream": False, "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]}
        resp = httpx.post(
            f"{KIE_BASE_URL}{path}",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=PROBE_TIMEOUT,
        )
        if resp.status_code in (402, 429) or "insufficient" in resp.text.lower() or "credit" in resp.text.lower():
            return {"configured": True, "status": "quota_exhausted", "detail": "Clé valide mais solde/crédits kie.ai insuffisants."}
        if resp.status_code == 401:
            return {"configured": True, "status": "invalid", "detail": "Clé invalide, révoquée, ou service injoignable."}
        resp.raise_for_status()
        data = resp.json()
        internal_code = data.get("code")
        if internal_code not in (None, 0, 200):
            detail = data.get("msg") or data.get("message") or "Erreur Kie.ai inconnue."
            if internal_code == 401:
                return {"configured": True, "status": "error", "detail": f"Authentification Kie.ai refusée : {detail}"}
            return {"configured": True, "status": "error", "detail": f"Kie.ai erreur {internal_code} : {detail}"}
        from src.pipeline.ai_text import _kie_response_text
        if not _kie_response_text(data):
            return {"configured": True, "status": "error", "detail": "Kie.ai a accepté la requête mais n'a renvoyé aucun texte."}
        return {"configured": True, "status": "ok", "detail": f"Clé valide. Modèle configuré : {model}."}
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
    try:
        # Use the same router as the text pipeline.  The old queue ping can
        # succeed even when fal locks the account for a top-up, yielding a
        # misleading green badge.
        resp = httpx.post(
            "https://fal.run/openrouter/router",
            headers={"Authorization": f"Key {key}", "Content-Type": "application/json"},
            json={"prompt": "OK", "model": "openai/gpt-4o-mini", "max_tokens": 1},
            timeout=PROBE_TIMEOUT,
        )
        if resp.status_code == 401:
            return {"configured": True, "status": "invalid", "detail": "Clé invalide, révoquée, ou non reconnue par fal.ai."}
        if resp.status_code in (402, 403, 429) or "credit" in resp.text.lower() or "payment" in resp.text.lower() or "top_up" in resp.text.lower():
            return {"configured": True, "status": "quota_exhausted", "detail": "Clé valide mais solde/crédits insuffisants sur le compte fal.ai."}
        resp.raise_for_status()
        if not (resp.json() or {}).get("output"):
            return {"configured": True, "status": "error", "detail": "fal.ai a accepté le contrôle sans renvoyer de texte."}
        return {"configured": True, "status": "ok", "detail": "Clé valide et opérationnelle."}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return {"configured": True, "status": "error", "detail": "Clé invalide ou révoquée."}
        if exc.response.status_code in (402, 429):
            return {"configured": True, "status": "quota_exhausted", "detail": "Solde/crédits insuffisants sur fal.ai."}
        return {"configured": True, "status": "error", "detail": f"Erreur fal.ai ({exc.response.status_code})"}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"fal.ai injoignable : {exc}"}


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
    try:
        resp = httpx.get(
            f"{AI33PRO_BASE_URL}/v1/shared-voices",
            headers={"xi-api-key": key},
            timeout=PROBE_TIMEOUT,
        )
        if resp.status_code in (402, 429) or (resp.is_error and "credits" in resp.text.lower()):
            return {"configured": True, "status": "quota_exhausted", "detail": "Clé valide mais solde/crédits insuffisants sur le compte ai33.pro."}
        if resp.status_code == 401:
            return {"configured": True, "status": "invalid", "detail": "Clé invalide, révoquée, ou service injoignable."}
        resp.raise_for_status()
        return {"configured": True, "status": "ok", "detail": "Clé valide et opérationnelle."}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (402, 429) or "credits" in exc.response.text.lower():
            return {"configured": True, "status": "quota_exhausted", "detail": "Solde/crédits épuisés sur ai33.pro."}
        return {"configured": True, "status": "error", "detail": f"Erreur ai33.pro ({exc.response.status_code})."}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"ai33.pro injoignable : {exc}"}


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
            return {"configured": True, "status": "invalid", "detail": "Clé invalide, révoquée, ou service injoignable."}
        resp.raise_for_status()
        return {"configured": True, "status": "ok", "detail": "Clé valide."}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"xAI injoignable : {exc}"}


def _check_ollama():
    raw_url = _get_effective_key("ollama", OLLAMA_BASE_URL)
    if not raw_url:
        return {"configured": False, "status": "not_configured", "detail": "URL du serveur Ollama non configurée."}
    base_url = raw_url.rstrip("/")
    if not base_url.startswith("http://") and not base_url.startswith("https://"):
        base_url = f"https://{base_url}"
    from src.utils.ollama import request_headers
    try:
        headers = request_headers()
        resp = httpx.get(f"{base_url}/api/tags", headers=headers, timeout=PROBE_TIMEOUT)
        if resp.status_code == 401:
            return {"configured": True, "status": "invalid", "detail": "Authentification refusée par le serveur Ollama."}
        resp.raise_for_status()
        data = resp.json()
        models = data.get("models") or []
        names = [m.get("name", "") for m in models if m.get("name")]
        if OLLAMA_MODEL not in names:
            return {"configured": True, "status": "error", "detail": f"Modèle absent : {OLLAMA_MODEL}."}
        probe = httpx.post(
            f"{base_url}/api/chat", headers=headers,
            json={"model": OLLAMA_MODEL, "messages": [{"role": "user", "content": "Hi"}],
                  "options": {"num_predict": 1}, "stream": False, "think": False}, timeout=180.0,
        )
        probe.raise_for_status()
        content = (probe.json().get("message") or {}).get("content")
        if not str(content or "").strip():
            raise ValueError("Réponse de génération Ollama invalide.")
        detail = f"Génération vérifiée. Modèles : {', '.join(names[:3])}" if names else "En ligne (aucun modèle téléchargé)."
        return {"configured": True, "status": "ok", "detail": detail}
    except Exception as exc:
        return {"configured": True, "status": "error", "detail": f"Ollama injoignable : {exc}"}


def check_all_providers() -> list:
    checks = [
        ("anthropic", "Anthropic (Claude)", _check_anthropic),
        ("kie", "Kie.ai", _check_kie),
        ("openai", "OpenAI", _check_openai),
        ("deepseek", "DeepSeek", _check_deepseek),
        ("groq", "Groq", _check_groq),
        ("fal", "fal.ai", _check_fal),
        ("izivoice", "Izivoice", _check_izivoice),
        ("ai33pro", "KappGen", _check_ai33pro),
        ("xai", "xAI (Grok)", _check_xai),
        ("ollama", "Ollama (Mac)", _check_ollama),
    ]
    results = []
    for provider_id, label, fn in checks:
        result = fn()
        result["id"] = provider_id
        result["label"] = label
        results.append(result)
    return results
