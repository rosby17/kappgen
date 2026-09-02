"""Shared text-generation helper with a provider fallback chain, text-only
(see src/pipeline/vision.py for the separate 3-provider vision chain):
Anthropic direct -> fal.ai (Claude via OpenRouter, billed against fal.ai
credits) -> OpenAI -> OpenRouter direct (free-tier model, last resort when
all three paid providers are out of credits at once). Any Claude-driven text
step (topic selection, script writing, niche detection, ...) should go
through this instead of calling `anthropic.Anthropic` directly, so an
exhausted Anthropic account doesn't silently break the whole feature."""
import httpx
from typing import Optional
from src.config import (
    ANTHROPIC_API_KEY, FAL_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY,
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, GROQ_API_KEY,
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
    cost_usd = estimate_anthropic_cost(in_tok, out_tok)
    # Web searches themselves are billed separately by Anthropic per-use;
    # not tracked here (token cost is), same tradeoff as other providers'
    # flat-rate estimates in this file already accept.
    log_usage(
        "anthropic", usage_ctx.get("operation", "text"), in_tok + out_tok, "tokens",
        cost_usd,
        user_id=usage_ctx.get("user_id"), channel_id=usage_ctx.get("channel_id"), video_id=usage_ctx.get("video_id"),
        meta={"model": model, "input_tokens": in_tok, "output_tokens": out_tok, "web_search": enable_web_search},
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
            "reasoning_effort": "low",
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    text = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content")
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
    }
    from src.pipeline.ai_providers import ordered_ids
    order = [pid for pid in ordered_ids("text") if pid in providers]
    last_exc = None
    for name in order:
        try:
            text, cost_usd = providers[name]()
            if cost_sink is not None:
                cost_sink.append(cost_usd)
            return text
        except Exception as exc:  # noqa: BLE001 - deliberately broad, this is a fallback chain
            logger.warning(f"[ai_text] provider '{name}' failed, trying next: {exc}")
            last_exc = exc
    raise RuntimeError(f"All AI providers failed for this request. Last error: {last_exc}")
