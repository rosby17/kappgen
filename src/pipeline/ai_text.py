"""Shared text-generation helper with the same provider fallback chain used
for vision (see src/pipeline/vision.py): Anthropic direct -> fal.ai (Claude
via OpenRouter, billed against fal.ai credits) -> OpenAI. Any Claude-driven
text step (topic selection, script writing, niche detection, ...) should go
through this instead of calling `anthropic.Anthropic` directly, so an
exhausted Anthropic account doesn't silently break the whole feature."""
import httpx
from typing import Optional
from src.config import ANTHROPIC_API_KEY, FAL_API_KEY, OPENAI_API_KEY
from src.utils.logger import logger
from src.utils.cost_tracking import log_usage, estimate_anthropic_cost, estimate_openai_cost, PRICING

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"


def _anthropic_complete(prompt: str, max_tokens: int, model: str, usage_ctx: dict) -> str:
    import anthropic

    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured on the server.")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}])
    in_tok, out_tok = response.usage.input_tokens, response.usage.output_tokens
    log_usage(
        "anthropic", usage_ctx.get("operation", "text"), in_tok + out_tok, "tokens",
        estimate_anthropic_cost(in_tok, out_tok),
        user_id=usage_ctx.get("user_id"), channel_id=usage_ctx.get("channel_id"), video_id=usage_ctx.get("video_id"),
        meta={"model": model, "input_tokens": in_tok, "output_tokens": out_tok},
    )
    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    raise RuntimeError("Anthropic text generation returned no text content.")


def _fal_complete(prompt: str, max_tokens: int, usage_ctx: dict) -> str:
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
    log_usage(
        "fal_text", usage_ctx.get("operation", "text"), 1, "request", PRICING["fal_text"]["flat_per_request"],
        user_id=usage_ctx.get("user_id"), channel_id=usage_ctx.get("channel_id"), video_id=usage_ctx.get("video_id"),
        meta={"model": "anthropic/claude-sonnet-4.5 (via fal.ai fallback)"},
    )
    return output.strip()


def _openai_complete(prompt: str, max_tokens: int, usage_ctx: dict) -> str:
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
    log_usage(
        "openai", usage_ctx.get("operation", "text"), in_tok + out_tok, "tokens",
        estimate_openai_cost(in_tok, out_tok),
        user_id=usage_ctx.get("user_id"), channel_id=usage_ctx.get("channel_id"), video_id=usage_ctx.get("video_id"),
        meta={"model": "gpt-4o (fallback)", "input_tokens": in_tok, "output_tokens": out_tok},
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
) -> str:
    """Tries Anthropic first, falls back to fal.ai (Claude via OpenRouter), then OpenAI.
    Raises if every configured provider fails (or none are configured).

    `operation`/`user_id`/`channel_id`/`video_id` are purely for cost
    attribution (see src/utils/cost_tracking.py) — all optional, and a
    missing one just means that dimension shows up blank on the admin
    "Coûts" page rather than breaking anything."""
    usage_ctx = {"operation": operation, "user_id": user_id, "channel_id": channel_id, "video_id": video_id}
    last_exc = None
    for name, fn in [
        ("anthropic", lambda: _anthropic_complete(prompt, max_tokens, model, usage_ctx)),
        ("fal.ai", lambda: _fal_complete(prompt, max_tokens, usage_ctx)),
        ("openai", lambda: _openai_complete(prompt, max_tokens, usage_ctx)),
    ]:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - deliberately broad, this is a fallback chain
            logger.warning(f"[ai_text] provider '{name}' failed, trying next: {exc}")
            last_exc = exc
    raise RuntimeError(f"All AI providers failed for this request. Last error: {last_exc}")
