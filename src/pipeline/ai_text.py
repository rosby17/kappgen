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
)
from src.utils.logger import logger
from src.utils.cost_tracking import log_usage, estimate_anthropic_cost, estimate_openai_cost, estimate_deepseek_cost, PRICING

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


def _anthropic_complete(prompt: str, max_tokens: int, model: str, usage_ctx: dict, enable_web_search: bool = False) -> tuple:
    import anthropic

    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured on the server.")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    # Anthropic's server-side web_search tool runs the search(es) itself and
    # feeds the results back into the same response — no client-side tool
    # loop needed, just pass the tool and read the final text block(s). Only
    # used for topic ideation on news/trend-driven channels (see
    # script_writer._pick_topic); every other text call stays search-free.
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if enable_web_search:
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    # Retried once, in-process, before ever falling through to fal.ai/OpenAI:
    # an empty response (no text block at all — no exception, no error
    # status, just nothing to read) has been observed in production for a
    # specific recurring prompt shape (a long "don't repeat these past
    # titles" list on a health-niche script) — a one-off hiccup on Anthropic's
    # end that a plain retry of the exact same request has cleared every
    # time it's been seen, rather than a real, repeatable rejection of the
    # prompt's content. Logs stop_reason on the empty attempt for whichever
    # case isn't a one-off shows up later.
    response = client.messages.create(**kwargs)
    if not any(block.type == "text" for block in response.content):
        logger.warning(f"Anthropic returned no text content (stop_reason={response.stop_reason!r}, operation={usage_ctx.get('operation')}) — retrying once.")
        response = client.messages.create(**kwargs)
    in_tok, out_tok = response.usage.input_tokens, response.usage.output_tokens
    # Web searches are billed separately by Anthropic per-use, on top of
    # tokens — the SDK reports the real count on usage.server_tool_use when
    # the tool actually ran; falling back to counting the server_tool_use
    # content blocks themselves covers older SDK versions that don't expose
    # that usage field yet. Either way, a real charge on this call must
    # always be reflected in what the creator is billed — never left out.
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
    # With tools enabled, content interleaves server_tool_use/web_search_tool_result
    # blocks with the final text — collect every text block instead of
    # returning on the first one, and use the last of them (Claude's actual
    # answer, after any search commentary).
    text_blocks = [block.text for block in response.content if block.type == "text"]
    if text_blocks:
        return text_blocks[-1].strip(), cost_usd
    raise RuntimeError("Anthropic text generation returned no text content.")


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
    keys = list(GEMINI_API_KEYS)
    try:
        from src.db.session import SessionLocal
        from src.db.models import HuggingFaceAccount
        db = SessionLocal()
        try:
            rows = (db.query(HuggingFaceAccount.token)
                    .filter(HuggingFaceAccount.provider == "gemini", HuggingFaceAccount.is_enabled == True)
                    .order_by(HuggingFaceAccount.last_used_at.asc().nullsfirst()).all())
            if rows:
                keys = [row[0] for row in rows]
        finally:
            db.close()
    except Exception:
        pass
    if not keys:
        raise RuntimeError("GEMINI_API_KEY is not configured on the server.")
    last_error = None
    for key in keys:
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
            return text, 0.0
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"All Gemini keys failed: {last_error}")


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

    `preferred_provider`, when set, moves one configured provider to the front
    for this call while preserving the normal fallback chain. This is useful
    for low-risk tasks such as stock-search keywords that should use Gemini
    before any paid provider.

    `enable_web_search` only applies to the Anthropic path (its server-side
    web_search tool) — every other provider ignores it; if Anthropic isn't
    first (or isn't available) the call still succeeds, just without live
    search results."""
    usage_ctx = {"operation": operation, "user_id": user_id, "channel_id": channel_id, "video_id": video_id}
    providers = {
        "anthropic": lambda: _anthropic_complete(prompt, max_tokens, model, usage_ctx, enable_web_search=enable_web_search),
        "deepseek": lambda: _deepseek_complete(prompt, max_tokens, usage_ctx),
        "fal": lambda: _fal_complete(prompt, max_tokens, usage_ctx),
        "openai": lambda: _openai_complete(prompt, max_tokens, usage_ctx),
        "groq": lambda: _groq_complete(prompt, max_tokens, usage_ctx),
        "gemini": lambda: _gemini_complete(prompt, max_tokens, usage_ctx),
    }
    from src.pipeline.ai_providers import ordered_ids
    order = [pid for pid in ordered_ids("text") if pid in providers]
    if preferred_provider in order:
        order = [preferred_provider] + [pid for pid in order if pid != preferred_provider]
    if enable_web_search and "anthropic" in order:
        # Web search only works through Anthropic's server-side tool (see the
        # docstring) — every other provider silently ignores it. The admin's
        # general priority order (e.g. Gemini/Groq first, both free) has no
        # idea this call needs it, so left alone it would run the "search the
        # web for trends" instruction on a provider that can't search the
        # web, quietly no-opping the whole feature. Force Anthropic first
        # for this one call only, regardless of where it normally ranks.
        order = ["anthropic"] + [pid for pid in order if pid != "anthropic"]
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
