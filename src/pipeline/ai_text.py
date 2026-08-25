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
from src.config import ANTHROPIC_API_KEY, FAL_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY
from src.utils.logger import logger
from src.utils.cost_tracking import log_usage, estimate_anthropic_cost, estimate_openai_cost, PRICING

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


def _anthropic_complete(prompt: str, max_tokens: int, model: str, usage_ctx: dict) -> tuple:
    import anthropic

    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured on the server.")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}])
    in_tok, out_tok = response.usage.input_tokens, response.usage.output_tokens
    cost_usd = estimate_anthropic_cost(in_tok, out_tok)
    log_usage(
        "anthropic", usage_ctx.get("operation", "text"), in_tok + out_tok, "tokens",
        cost_usd,
        user_id=usage_ctx.get("user_id"), channel_id=usage_ctx.get("channel_id"), video_id=usage_ctx.get("video_id"),
        meta={"model": model, "input_tokens": in_tok, "output_tokens": out_tok},
    )
    for block in response.content:
        if block.type == "text":
            return block.text.strip(), cost_usd
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


def generate_text(
    prompt: str,
    max_tokens: int = 1000,
    model: str = DEFAULT_ANTHROPIC_MODEL,
    operation: str = "text",
    user_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    video_id: Optional[str] = None,
    cost_sink: Optional[list] = None,
) -> str:
    """Tries Anthropic first, falls back to fal.ai (Claude via OpenRouter), then OpenAI.
    Raises if every configured provider fails (or none are configured).

    The OpenRouter free-tier model is deliberately NOT in this chain — it's
    unreliable enough (leaks its own chain-of-thought in English into the
    answer, ignores the requested language) that a clean failure + retry on
    the next scheduled run beats silently publishing garbage text.

    `operation`/`user_id`/`channel_id`/`video_id` are purely for cost
    attribution (see src/utils/cost_tracking.py) — all optional, and a
    missing one just means that dimension shows up blank on the admin
    "Coûts" page rather than breaking anything.

    `cost_sink`, if given a list, gets this call's real provider cost (USD)
    appended to it — used by callers (e.g. auto script generation) that need
    to know the actual cost incurred to bill the creator for it."""
    usage_ctx = {"operation": operation, "user_id": user_id, "channel_id": channel_id, "video_id": video_id}
    last_exc = None
    for name, fn in [
        ("anthropic", lambda: _anthropic_complete(prompt, max_tokens, model, usage_ctx)),
        ("fal.ai", lambda: _fal_complete(prompt, max_tokens, usage_ctx)),
        ("openai", lambda: _openai_complete(prompt, max_tokens, usage_ctx)),
    ]:
        try:
            text, cost_usd = fn()
            if cost_sink is not None:
                cost_sink.append(cost_usd)
            return text
        except Exception as exc:  # noqa: BLE001 - deliberately broad, this is a fallback chain
            logger.warning(f"[ai_text] provider '{name}' failed, trying next: {exc}")
            last_exc = exc
    raise RuntimeError(f"All AI providers failed for this request. Last error: {last_exc}")
