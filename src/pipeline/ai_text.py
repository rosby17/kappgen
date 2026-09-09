"""Shared text-generation helper with a provider fallback chain, text-only
(see src/pipeline/vision.py for the separate 3-provider vision chain):
Anthropic direct -> fal.ai (Claude via OpenRouter, billed against fal.ai
credits) -> OpenAI -> OpenRouter direct (free-tier model, last resort when
all three paid providers are out of credits at once). Any Claude-driven text
step (topic selection, script writing, niche detection, ...) should go
through this instead of calling `anthropic.Anthropic` directly, so an
exhausted Anthropic account doesn't silently break the whole feature."""
import re
import time
import httpx
from typing import Optional
from src.config import (
    ANTHROPIC_API_KEY, FAL_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY,
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, GROQ_API_KEY, GEMINI_API_KEY, GEMINI_API_KEYS,
    KIE_API_KEY, KIE_BASE_URL, KIE_CLAUDE_MODEL, XAI_API_KEY, XAI_BASE_URL,
    OLLAMA_BASE_URL, OLLAMA_API_KEY, OLLAMA_MODEL,
)
from src.utils.logger import logger
from src.utils.cost_tracking import log_usage, estimate_anthropic_cost, estimate_openai_cost, estimate_deepseek_cost, estimate_kie_claude_cost, PRICING

# OpenRouter's own ":free" model catalog changes over time; this one has
# stayed reliably available and free as of writing. Swap it if OpenRouter
# retires/rate-limits it — nothing else here needs to change.
OPENROUTER_FREE_MODEL = "nvidia/nemotron-3-nano-30b-a3b:free"
# This free reasoning model sometimes leaks its internal chain-of-thought
# straight into the answer instead of keeping it in the separate `reasoning`
# field — content starting with one of these reads as thinking-out-loud, not
# a usable answer (e.g. it would inject "Okay, the user wants..." into a
# script). Treated as a failure so the caller sees a clean error instead of
# garbage text, rather than trying to salvage/strip it.
_OPENROUTER_LEAKED_REASONING_PREFIXES = ("okay,", "let me", "i need to", "the user", "first,")

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"


def _classify_key_failure(exc: Exception) -> str:
    """"invalid" (bad/revoked key — auth failure) vs "quota_exhausted" (out
    of credits/rate-limited) for the admin pool's live status column. Checked
    by status_code first (both the Anthropic SDK's APIStatusError and a
    plain httpx.HTTPStatusError expose one); falls back to sniffing the
    message for providers/exceptions that don't."""
    status_code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status_code in (401, 403):
        return "invalid"
    if status_code in (402, 429):
        return "quota_exhausted"
    text = str(exc).lower()
    if any(m in text for m in ("insufficient_quota", "credit_balance", "429", "rate limit", "quota")):
        return "quota_exhausted"
    return "invalid"


def _anthropic_complete(prompt: str, max_tokens: int, model: str, usage_ctx: dict, enable_web_search: bool = False) -> tuple:
    import anthropic
    from src.pipeline.images import _provider_accounts_from_db, _mark_provider_account

    accounts = _provider_accounts_from_db("anthropic", env_fallback_keys=[ANTHROPIC_API_KEY] if ANTHROPIC_API_KEY else [])
    if not accounts:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured on the server.")
    # Anthropic's server-side web_search tool runs the search(es) itself and
    # feeds the results back into the same response — no client-side tool
    # loop needed, just pass the tool and read the final text block(s). Only
    # used for topic ideation on news/trend-driven channels (see
    # script_writer._pick_topic); every other text call stays search-free.
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if enable_web_search:
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    last_exc = None
    for account in accounts:
        client = anthropic.Anthropic(api_key=account["token"])
        try:
            # Retried once, in-process, before falling to the next key: an
            # empty response (no text block at all — no exception, no error
            # status, just nothing to read) has been observed in production
            # for a specific recurring prompt shape — a one-off hiccup that a
            # plain retry of the exact same request has cleared every time.
            response = client.messages.create(**kwargs)
            if not any(block.type == "text" for block in response.content):
                logger.warning(f"Anthropic returned no text content (stop_reason={response.stop_reason!r}, operation={usage_ctx.get('operation')}) — retrying once.")
                response = client.messages.create(**kwargs)
            in_tok, out_tok = response.usage.input_tokens, response.usage.output_tokens
            # Web searches are billed separately by Anthropic per-use, on top
            # of tokens — the SDK reports the real count on
            # usage.server_tool_use when the tool actually ran; falling back
            # to counting the server_tool_use content blocks themselves
            # covers older SDK versions that don't expose that usage field.
            web_search_uses = getattr(getattr(response.usage, "server_tool_use", None), "web_search_requests", None)
            if web_search_uses is None:
                web_search_uses = sum(1 for block in response.content if getattr(block, "type", None) == "server_tool_use" and getattr(block, "name", None) == "web_search")
            cost_usd = estimate_anthropic_cost(in_tok, out_tok, web_search_uses=web_search_uses)
            log_usage(
                "anthropic", usage_ctx.get("operation", "text"), in_tok + out_tok, "tokens",
                cost_usd,
                user_id=usage_ctx.get("user_id"), channel_id=usage_ctx.get("channel_id"), video_id=usage_ctx.get("video_id"),
                meta={"model": model, "input_tokens": in_tok, "output_tokens": out_tok, "web_search": enable_web_search, "web_search_uses": web_search_uses},
            )
            # With tools enabled, content interleaves server_tool_use/
            # web_search_tool_result blocks with the final text — collect
            # every text block instead of returning on the first one, and
            # use the last of them (Claude's actual answer, after any search
            # commentary).
            text_blocks = [block.text for block in response.content if block.type == "text"]
            if not text_blocks:
                raise RuntimeError("Anthropic text generation returned no text content.")
            _mark_provider_account(account["id"], "active")
            return text_blocks[-1].strip(), cost_usd
        except Exception as exc:  # noqa: BLE001 - trying the next pooled key is the point
            _mark_provider_account(account["id"], _classify_key_failure(exc), str(exc)[:300])
            last_exc = exc
            continue
    raise RuntimeError(f"All Anthropic keys failed: {last_exc}")


def _kie_response_text(data: dict) -> str:
    if data.get("choices"):
        return "\n".join(((choice.get("message") or {}).get("content") or "") for choice in data["choices"]).strip()
    if data.get("content"):
        return "\n".join(block.get("text", "") for block in data["content"] if block.get("type") == "text").strip()
    if data.get("output"):
        return "\n".join(
            part.get("text", "")
            for item in data["output"] if item.get("type") == "message"
            for part in item.get("content", []) if part.get("type") == "output_text"
        ).strip()
    candidates = data.get("candidates") or []
    return "\n".join(
        part.get("text", "") for candidate in candidates
        for part in ((candidate.get("content") or {}).get("parts") or []) if part.get("text")
    ).strip()


def _kie_complete(prompt: str, max_tokens: int, usage_ctx: dict, selected_model: Optional[str] = None) -> tuple:
    """Claude through kie.ai's reseller proxy — a cheaper alternative to
    calling Anthropic directly (see src/pipeline/ai_providers.py). This is
    NOT the Anthropic API: kie.ai exposes its own wrapper
    (POST /claude/v1/messages) with a much smaller surface — no `system`
    prompt, no extended-thinking effort controls, no prompt caching,
    max_tokens capped low by default. The exact model comes from
    KIE_CLAUDE_MODEL so deployments can pin a supported Kie model.
    """
    from src.pipeline.images import _provider_accounts_from_db, _mark_provider_account

    accounts = _provider_accounts_from_db("kie", env_fallback_keys=[KIE_API_KEY] if KIE_API_KEY else [])
    if not accounts:
        raise RuntimeError("KIE_API_KEY is not configured on the server.")
    model = selected_model or KIE_CLAUDE_MODEL
    last_exc = None
    for account in accounts:
        try:
            if model.startswith("claude-"):
                path = "/claude/v1/messages"
                payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False, "max_tokens": max_tokens}
            elif model.startswith("gemini-"):
                path = f"/gemini/v1/models/{model}:streamGenerateContent"
                payload = {"stream": False, "contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": max_tokens}}
            elif model.startswith("grok-"):
                path = "/grok/v1/responses"
                payload = {"model": model, "stream": False, "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}]}
            elif model.startswith("deepseek-"):
                path = "/deepseek/v1/chat/completions"
                payload = {"model": model, "stream": False, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
            else:
                path = "/codex/v1/responses"
                payload = {"model": model, "stream": False, "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}], "reasoning": {"effort": "low"}}
            resp = httpx.post(
                f"{KIE_BASE_URL}{path}",
                headers={"Authorization": f"Bearer {account['token']}", "Content-Type": "application/json"},
                json=payload,
                timeout=120.0,
            )
            resp.raise_for_status()
            data = resp.json()
            # Kie may return HTTP 200 while reporting an API failure inside
            # its JSON envelope (observed with code 401). Surface that real
            # cause instead of misreporting a generic empty-text response.
            internal_code = data.get("code")
            if internal_code not in (None, 0, 200):
                detail = data.get("msg") or data.get("message") or "unknown error"
                raise RuntimeError(f"Kie.ai error {internal_code}: {detail}")
            text = _kie_response_text(data)
            if not text:
                raise RuntimeError("Kie.ai text generation returned no text content.")
            usage = data.get("usage") or data.get("usageMetadata") or {}
            in_tok = usage.get("input_tokens", usage.get("promptTokenCount", 0))
            out_tok = usage.get("output_tokens", usage.get("candidatesTokenCount", 0))
            # kie.ai reports its own credits_consumed too, but the per-token
            # estimate keeps this provider comparable to every other row on
            # the admin "Coûts" page (all of which are token-based).
            from src.pipeline.model_catalog import MODEL_PRICING
            price = MODEL_PRICING.get(f"kie:{model}", {})
            cost_usd = in_tok / 1_000_000 * price.get("input", 0) + out_tok / 1_000_000 * price.get("output", 0)
            log_usage(
                "kie_claude", usage_ctx.get("operation", "text"), in_tok + out_tok, "tokens",
                cost_usd,
                user_id=usage_ctx.get("user_id"), channel_id=usage_ctx.get("channel_id"), video_id=usage_ctx.get("video_id"),
                meta={"model": model, "input_tokens": in_tok, "output_tokens": out_tok, "credits_consumed": data.get("credits_consumed")},
            )
            _mark_provider_account(account["id"], "active")
            return text, cost_usd
        except Exception as exc:  # noqa: BLE001 - trying the next pooled key is the point
            _mark_provider_account(account["id"], _classify_key_failure(exc), str(exc)[:300])
            last_exc = exc
            continue
    raise RuntimeError(f"All Kie.ai keys failed: {last_exc}")


def _fal_complete(prompt: str, max_tokens: int, usage_ctx: dict) -> tuple:
    if not FAL_API_KEY:
        raise RuntimeError("FAL_API_KEY is not configured on the server.")
    resp = httpx.post(
        "https://fal.run/openrouter/router",
        headers={"Authorization": f"Key {FAL_API_KEY}", "Content-Type": "application/json"},
        json={"prompt": prompt, "model": "anthropic/claude-sonnet-4.5", "max_tokens": max_tokens},
        timeout=120.0,
    )
    resp.raise_for_status()
    output = (resp.json() or {}).get("output")
    if not output:
        raise RuntimeError("fal.ai text generation returned no output.")
    cost_usd = PRICING["fal_text"]["flat_per_request"]
    log_usage(
        "fal_text", usage_ctx.get("operation", "text"), 1, "request", cost_usd,
        user_id=usage_ctx.get("user_id"), channel_id=usage_ctx.get("channel_id"), video_id=usage_ctx.get("video_id"),
        meta={"model": "anthropic/claude-sonnet-4.5 (via fal.ai fallback)"},
    )
    return output.strip(), cost_usd


def _openai_complete(prompt: str, max_tokens: int, usage_ctx: dict) -> tuple:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured on the server.")
    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={"model": "gpt-4o", "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]},
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    text = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content")
    if not text:
        raise RuntimeError("OpenAI text generation returned no text content.")
    usage = data.get("usage") or {}
    in_tok, out_tok = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    cost_usd = estimate_openai_cost(in_tok, out_tok)
    log_usage(
        "openai", usage_ctx.get("operation", "text"), in_tok + out_tok, "tokens",
        cost_usd,
        user_id=usage_ctx.get("user_id"), channel_id=usage_ctx.get("channel_id"), video_id=usage_ctx.get("video_id"),
        meta={"model": "gpt-4o (fallback)", "input_tokens": in_tok, "output_tokens": out_tok},
    )
    return text.strip(), cost_usd


DEEPSEEK_MODEL = "deepseek-v4-flash"

XAI_MODEL = "grok-4-6"


def _xai_complete(prompt: str, max_tokens: int, usage_ctx: dict) -> tuple:
    from src.pipeline.images import _provider_accounts_from_db, _mark_provider_account
    accounts = _provider_accounts_from_db("xai", [XAI_API_KEY] if XAI_API_KEY else [])
    last_exc = None
    for account in accounts:
        try:
            resp = httpx.post(
                f"{XAI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {account['token']}", "Content-Type": "application/json"},
                json={"model": XAI_MODEL, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]},
                timeout=120.0,
            )
            resp.raise_for_status()
            data = resp.json()
            text = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content")
            if not text:
                raise RuntimeError("xAI text generation returned no text content.")
            usage = data.get("usage") or {}
            in_tok, out_tok = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
            cost_usd = in_tok / 1_000_000 * 0.80 + out_tok / 1_000_000 * 2.40
            _mark_provider_account(account["id"], "active")
            log_usage("xai", usage_ctx.get("operation", "text"), in_tok + out_tok, "tokens", cost_usd,
                      user_id=usage_ctx.get("user_id"), channel_id=usage_ctx.get("channel_id"), video_id=usage_ctx.get("video_id"),
                      meta={"model": XAI_MODEL, "input_tokens": in_tok, "output_tokens": out_tok})
            return text.strip(), cost_usd
        except Exception as exc:
            last_exc = exc
            _mark_provider_account(account["id"], _classify_key_failure(exc), str(exc)[:300])
    raise RuntimeError(f"All xAI keys failed: {last_exc}")


def _deepseek_complete(prompt: str, max_tokens: int, usage_ctx: dict) -> tuple:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured on the server.")
    resp = httpx.post(
        f"{DEEPSEEK_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
        json={"model": DEEPSEEK_MODEL, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]},
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    text = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content")
    if not text:
        raise RuntimeError("DeepSeek text generation returned no text content.")
    usage = data.get("usage") or {}
    in_tok, out_tok = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    cost_usd = estimate_deepseek_cost(in_tok, out_tok)
    log_usage(
        "deepseek", usage_ctx.get("operation", "text"), in_tok + out_tok, "tokens",
        cost_usd,
        user_id=usage_ctx.get("user_id"), channel_id=usage_ctx.get("channel_id"), video_id=usage_ctx.get("video_id"),
        meta={"model": DEEPSEEK_MODEL, "input_tokens": in_tok, "output_tokens": out_tok},
    )
    return text.strip(), cost_usd


GROQ_MODEL = "openai/gpt-oss-120b"


def _groq_complete(prompt: str, max_tokens: int, usage_ctx: dict) -> tuple:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured on the server.")
    resp = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": GROQ_MODEL,
            # gpt-oss is a reasoning model: it spends part of max_tokens on a hidden
            # "reasoning" field before writing the actual answer, so give it headroom
            # and keep the reasoning budget low to avoid burning tokens/latency on it.
            "max_tokens": max(max_tokens, 300),
            # ...but only gpt-oss accepts that parameter. Sending it to
            # groq/compound is a hard 400, which made that model unusable
            # here even though it answers fine without it.
            **({"reasoning_effort": "low"} if GROQ_MODEL.startswith("openai/gpt-oss") else {}),
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    message = ((data.get("choices") or [{}])[0]).get("message") or {}
    text = message.get("content")
    # Qwen-style models put their chain of thought in the answer itself,
    # wrapped in <think>…</think>; left in, it gets spoken by the voiceover.
    # Some also return the visible answer empty and everything in a separate
    # "reasoning" field — falling back to it keeps the model usable instead of
    # failing the whole part.
    if text:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if not text:
        reasoning = (message.get("reasoning") or "").strip()
        text = re.sub(r"<think>.*?</think>", "", reasoning, flags=re.DOTALL).strip()
    if not text:
        raise RuntimeError("Groq text generation returned no text content.")
    usage = data.get("usage") or {}
    in_tok, out_tok = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    log_usage(
        "groq", usage_ctx.get("operation", "text"), in_tok + out_tok, "tokens",
        0.0,
        user_id=usage_ctx.get("user_id"), channel_id=usage_ctx.get("channel_id"), video_id=usage_ctx.get("video_id"),
        meta={"model": GROQ_MODEL, "input_tokens": in_tok, "output_tokens": out_tok, "free_tier": True},
    )
    return text.strip(), 0.0


GEMINI_MODEL = "gemini-3.6-flash"


def _gemini_complete(prompt: str, max_tokens: int, usage_ctx: dict) -> tuple:
    keys = [{"id": None, "token": key} for key in GEMINI_API_KEYS]
    try:
        from src.db.session import SessionLocal
        from src.db.models import HuggingFaceAccount
        db = SessionLocal()
        try:
            rows = (db.query(HuggingFaceAccount.id, HuggingFaceAccount.token)
                    .filter(HuggingFaceAccount.provider == "gemini", HuggingFaceAccount.is_enabled == True)
                    .order_by(HuggingFaceAccount.last_used_at.asc().nullsfirst()).all())
            if rows:
                keys = [{"id": row[0], "token": row[1]} for row in rows]
        finally:
            db.close()
    except Exception:
        pass
    if not keys:
        raise RuntimeError("GEMINI_API_KEY is not configured on the server.")
    last_error = None
    for account in keys:
        key = account["token"]
        try:
            resp = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            params={"key": key},
            json={
            "contents": [{"parts": [{"text": prompt}]}],
            # This model burns the vast majority of maxOutputTokens on hidden
            # "thinking" before writing any visible text — observed ~90-95%
            # of the budget gone to thoughtsTokenCount regardless of
            # thinkingConfig (thinkingBudget: 0 is rejected outright by this
            # model, and lower positive budgets are silently ignored). Padding
            # generously here is the only way to reliably get the requested
            # content length instead of an early MAX_TOKENS cutoff — still
            # free-tier, just token-budget-hungry.
            "generationConfig": {"maxOutputTokens": max(max_tokens * 15, 4000)},
            },
            timeout=120.0,
            )
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates") or []
            parts = (((candidates or [{}])[0]).get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts).strip()
            if not text:
                reason = (candidates[0] if candidates else {}).get("finishReason", "unknown")
                raise RuntimeError(f"Gemini text generation returned no text content (finishReason={reason}).")
            usage = data.get("usageMetadata") or {}
            in_tok, out_tok = usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)
            log_usage("gemini", usage_ctx.get("operation", "text"), in_tok + out_tok, "tokens", 0.0, user_id=usage_ctx.get("user_id"), channel_id=usage_ctx.get("channel_id"), video_id=usage_ctx.get("video_id"), meta={"model": GEMINI_MODEL, "input_tokens": in_tok, "output_tokens": out_tok, "free_tier": True})
            if account["id"]:
                from src.pipeline.images import _mark_provider_account
                _mark_provider_account(account["id"], "active")
            return text, 0.0
        except Exception as exc:
            last_error = exc
            continue
def _ollama_complete(prompt: str, max_tokens: int, usage_ctx: dict, *, images=None) -> tuple:
    """Ollama local or tunneled native chat endpoint.

    The native endpoint is used because it reliably honors ``think: false``
    with the installed Qwen model, unlike its OpenAI-compatibility endpoint.
    """
    from src.pipeline.images import _provider_accounts_from_db, _mark_provider_account
    accounts = _provider_accounts_from_db("ollama", env_fallback_keys=[OLLAMA_BASE_URL] if OLLAMA_BASE_URL else [])
    if not accounts:
        raise RuntimeError("OLLAMA_BASE_URL is not configured on the server.")
    from src.utils.ollama import request_headers
    from src.config import OLLAMA_VISION_MODEL
    selected_model = OLLAMA_VISION_MODEL if images else OLLAMA_MODEL
    message = {"role": "user", "content": prompt}
    if images:
        import base64
        message["images"] = [base64.b64encode(data).decode() for data, _mime in images]
    last_exc = None
    for account in accounts:
        base_url = account["token"].rstrip("/")
        if not base_url.startswith("http://") and not base_url.startswith("https://"):
            base_url = f"https://{base_url}"
        headers = request_headers()
        try:
            resp = httpx.post(
                f"{base_url}/api/chat",
                headers=headers,
                json={
                    "model": selected_model,
                    "messages": [message],
                    "stream": False,
                    # Qwen otherwise spends a small response budget in its
                    # private reasoning field, which is never usable output.
                    "think": False,
                    "options": {"num_predict": max_tokens},
                },
                timeout=180.0,
            )
            resp.raise_for_status()
            data = resp.json()
            response_message = data.get("message") or {}
            text = response_message.get("content") or ""
            if text:
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL).strip()
            if not text:
                raise RuntimeError(f"Ollama ({selected_model}) returned no text content.")
            usage = data.get("usage") or {}
            # Native Ollama returns these counters at the response root;
            # retain the nested fallback for proxies that normalize them.
            in_tok = data.get("prompt_eval_count", usage.get("prompt_eval_count", 0))
            out_tok = data.get("eval_count", usage.get("eval_count", 0))
            log_usage(
                "ollama", usage_ctx.get("operation", "text"), in_tok + out_tok, "tokens",
                0.0,
                user_id=usage_ctx.get("user_id"), channel_id=usage_ctx.get("channel_id"), video_id=usage_ctx.get("video_id"),
                meta={"model": selected_model, "input_tokens": in_tok, "output_tokens": out_tok, "free_tier": True},
            )
            _mark_provider_account(account["id"], "active")
            return text, 0.0
        except Exception as exc:
            _mark_provider_account(account["id"], "error", str(exc)[:300])
            last_exc = exc
            continue
    raise RuntimeError(f"All Ollama endpoints failed: {last_exc}")


def _openrouter_complete(prompt: str, max_tokens: int, usage_ctx: dict) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not configured on the server.")
    # OPENROUTER_FREE_MODEL is a reasoning model — it spends completion
    # tokens on a hidden "reasoning" pass before the actual answer, invisibly
    # eating into max_tokens. Without headroom, a tight budget (sized for a
    # non-reasoning model upstream) burns entirely on reasoning and leaves
    # the real content empty. Padding here only, so the other providers'
    # actual token cost/limits stay exactly as configured by the caller.
    resp = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
        json={"model": OPENROUTER_FREE_MODEL, "max_tokens": max_tokens + 500, "messages": [{"role": "user", "content": prompt}]},
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    text = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content")
    if not text:
        raise RuntimeError("OpenRouter text generation returned no text content.")
    if text.strip().lower().startswith(_OPENROUTER_LEAKED_REASONING_PREFIXES):
        raise RuntimeError("OpenRouter returned leaked reasoning instead of a real answer.")
    log_usage(
        "openrouter", usage_ctx.get("operation", "text"), 1, "request", 0.0,
        user_id=usage_ctx.get("user_id"), channel_id=usage_ctx.get("channel_id"), video_id=usage_ctx.get("video_id"),
        meta={"model": f"{OPENROUTER_FREE_MODEL} (free-tier fallback)"},
    )
    return text.strip()


# Free tiers meter per minute, so a 429 is worth waiting out once or twice
# before giving up on an otherwise working provider.
_RATE_LIMIT_RETRIES = 2
_RATE_LIMIT_BACKOFF_SECONDS = 20


def _is_rate_limited(exc: Exception) -> bool:
    text = str(exc).lower()
    # A per-minute rate limit is worth a short retry (waiting genuinely
    # helps); a fully exhausted allowance is not — no amount of waiting
    # inside one request recovers a DAILY quota, so retrying just burns
    # ~60s (2 backoff attempts) before falling through to the next provider
    # anyway. "insufficient_quota"/"credit_balance_exhausted" cover OpenAI/
    # Anthropic's phrasing; "resource_exhausted"/"quota exceeded" cover
    # Gemini's (its free tier's actual failure mode here — 20 requests/day,
    # trivially burned through by a handful of channels' daily automation).
    if any(marker in text for marker in (
        "insufficient_quota", "credit_balance_exhausted", "no credits remaining",
        "resource_exhausted", "quota exceeded", "quota_exceeded",
    )):
        return False
    return "429" in text or "too many requests" in text or "rate limit" in text


# Single source of truth for "can this deployment do AI text at all?".
# Feature guards used to each hardcode `ANTHROPIC_API_KEY or FAL_API_KEY or
# OPENAI_API_KEY`, a list frozen before DeepSeek and Groq existed here: a
# deployment holding only a (free) Groq key was told no provider was
# available and silently dropped to its non-AI fallback, even though
# generate_text() below would have answered fine. Anything added to the
# fallback chain must be added here too — that's the point of it being one
# function instead of five copies of a tuple.
def any_text_provider_configured() -> bool:
    from src.pipeline.ai_providers import any_configured
    return any_configured("text")


def generate_text(
    prompt: str,
    max_tokens: int = 1000,
    model: str = DEFAULT_ANTHROPIC_MODEL,
    operation: str = "text",
    user_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    video_id: Optional[str] = None,
    cost_sink: Optional[list] = None,
    enable_web_search: bool = False,
    preferred_provider: Optional[str] = None,
) -> str:
    """Tries providers in order — by default Anthropic, DeepSeek, fal.ai
    (Claude via OpenRouter), OpenAI, then Groq's free tier — falling through
    to the next configured one on any failure. Raises only if every provider
    in the chain fails (or none are configured).

    The full priority order is admin-controlled at runtime (see the
    "Ressources" tab / src/utils/app_settings.ai_text_provider_order) — pick
    and reorder any subset of providers there for exactly this situation: an
    exhausted Anthropic balance with no time to redeploy. Any configured
    provider left out of that custom order is still appended after it (in
    the default order above), so nothing is ever unreachable.

    OpenRouter's free-tier model is deliberately NOT in this chain — it's
    unreliable enough (leaks its own chain-of-thought in English into the
    answer, ignores the requested language) that a clean failure + retry on
    the next scheduled run beats silently publishing garbage text, and Groq's
    free tier is both better quality and already in the chain as a genuinely
    free fallback.

    `operation`/`user_id`/`channel_id`/`video_id` are purely for cost
    attribution (see src/utils/cost_tracking.py) — all optional, and a
    missing one just means that dimension shows up blank on the admin
    "Coûts" page rather than breaking anything.

    `cost_sink`, if given a list, gets this call's real provider cost (USD)
    appended to it — used by callers (e.g. auto script generation) that need
    to know the actual cost incurred to bill the creator for it.

    `preferred_provider` is retained for compatibility with older call sites.
    The admin's numbered routing order remains authoritative for every call.

    `enable_web_search` only applies to the Anthropic path (its server-side
    web_search tool) — every other provider ignores it; if Anthropic isn't
    first (or isn't available) the call still succeeds, just without live
    search results."""
    usage_ctx = {"operation": operation, "user_id": user_id, "channel_id": channel_id, "video_id": video_id}
    from src.utils.app_settings import selected_task_model

    def _selected_model(provider_id: str) -> Optional[str]:
        return selected_task_model("text", provider_id)
    providers = {
        "anthropic": lambda: _anthropic_complete(prompt, max_tokens, model, usage_ctx, enable_web_search=enable_web_search),
        "kie": lambda: _kie_complete(prompt, max_tokens, usage_ctx, selected_model=_selected_model("kie")),
        "deepseek": lambda: _deepseek_complete(prompt, max_tokens, usage_ctx),
        "fal": lambda: _fal_complete(prompt, max_tokens, usage_ctx),
        "openai": lambda: _openai_complete(prompt, max_tokens, usage_ctx),
        "groq": lambda: _groq_complete(prompt, max_tokens, usage_ctx),
        "xai": lambda: _xai_complete(prompt, max_tokens, usage_ctx),
        "gemini": lambda: _gemini_complete(prompt, max_tokens, usage_ctx),
        "ollama": lambda: _ollama_complete(prompt, max_tokens, usage_ctx),
    }
    from src.pipeline.ai_providers import ordered_ids
    order = [pid for pid in ordered_ids("text") if pid in providers]
    # The admin's numbered routing order is authoritative.  Call sites used
    # to move Gemini to the front for small/cheap operations, which made a
    # title or short script section bypass the order shown in Resources.
    if enable_web_search and order and order[0] != "anthropic" and "anthropic" in order:
        # Web search only works through Anthropic's server-side tool (see the
        # docstring) — every other provider silently ignores it. Anthropic
        # used to be force-jumped to the front of the chain whenever this
        # flag was set, regardless of the admin's own ranking — which meant
        # a real Anthropic call happened on every web-search-driven topic
        # pick even after the admin deliberately ranked Kie/fal above it to
        # get off Claude entirely. Now the admin's order is the single
        # source of truth: web search only actually runs when Anthropic is
        # already first; otherwise this call just proceeds down the normal
        # chain without live trends data, same as any other provider
        # silently ignoring the flag.
        logger.info(f"[ai_text] web search requested but Anthropic isn't first in the admin's order (operation={operation!r}) — proceeding without live trends data.")
    last_exc = None
    for name in order:
        # A 429 means "you're going too fast", not "this provider is dead" —
        # free tiers meter per minute. Falling straight through to the next
        # provider wasted the working one: a 6-part script generated its first
        # five parts on Groq, hit the per-minute cap on the last, and the whole
        # run was abandoned even though a short wait would have finished it.
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            try:
                text, cost_usd = providers[name]()
                if cost_sink is not None:
                    cost_sink.append(cost_usd)
                return text
            except Exception as exc:  # noqa: BLE001 - deliberately broad, this is a fallback chain
                last_exc = exc
                if _is_rate_limited(exc) and attempt < _RATE_LIMIT_RETRIES:
                    delay = _RATE_LIMIT_BACKOFF_SECONDS * (2 ** attempt)
                    logger.warning(f"[ai_text] provider '{name}' rate-limited, retrying in {delay}s (attempt {attempt + 1}/{_RATE_LIMIT_RETRIES})...")
                    time.sleep(delay)
                    continue
                logger.warning(f"[ai_text] provider '{name}' failed, trying next: {exc}")
                break
    raise RuntimeError(f"All AI providers failed for this request. Last error: {last_exc}")
