"""Shared text-generation helper with the same provider fallback chain used
for vision (see src/pipeline/vision.py): Anthropic direct -> fal.ai (Claude
via OpenRouter, billed against fal.ai credits) -> OpenAI. Any Claude-driven
text step (topic selection, script writing, niche detection, ...) should go
through this instead of calling `anthropic.Anthropic` directly, so an
exhausted Anthropic account doesn't silently break the whole feature."""
import httpx
from src.config import ANTHROPIC_API_KEY, FAL_API_KEY, OPENAI_API_KEY
from src.utils.logger import logger

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"


def _anthropic_complete(prompt: str, max_tokens: int, model: str) -> str:
    import anthropic

    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured on the server.")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}])
    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    raise RuntimeError("Anthropic text generation returned no text content.")


def _fal_complete(prompt: str, max_tokens: int) -> str:
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
    return output.strip()


def _openai_complete(prompt: str, max_tokens: int) -> str:
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
    return text.strip()


def generate_text(prompt: str, max_tokens: int = 1000, model: str = DEFAULT_ANTHROPIC_MODEL) -> str:
    """Tries Anthropic first, falls back to fal.ai (Claude via OpenRouter), then OpenAI.
    Raises if every configured provider fails (or none are configured)."""
    last_exc = None
    for name, fn in [
        ("anthropic", lambda: _anthropic_complete(prompt, max_tokens, model)),
        ("fal.ai", lambda: _fal_complete(prompt, max_tokens)),
        ("openai", lambda: _openai_complete(prompt, max_tokens)),
    ]:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - deliberately broad, this is a fallback chain
            logger.warning(f"[ai_text] provider '{name}' failed, trying next: {exc}")
            last_exc = exc
    raise RuntimeError(f"All AI providers failed for this request. Last error: {last_exc}")
