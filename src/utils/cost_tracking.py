"""Estimated cost tracking for every external API call the pipeline makes.

Nothing here reads a real account balance — Anthropic in particular exposes
no such API. Instead, each instrumented call site reports how much of a
provider it consumed (tokens, characters, seconds, images), and this module
converts that into an estimated USD cost using the PRICING table below, then
writes one row to api_usage_logs via log_usage(). The admin "Coûts" page
aggregates those rows.

PRICING is a best-effort snapshot of each provider's published rates at the
time this was written — update the numbers below if a provider changes its
pricing; nothing else needs to change. Izivoice's real per-character/per-
second rate isn't publicly documented anywhere accessible from here, so its
entry is a placeholder — adjust IZIVOICE_TTS_PER_CHAR / IZIVOICE_STT_PER_SECOND
once the actual contract rate is known.
"""
from datetime import datetime
from typing import Optional

# All prices in USD. Text-model prices are per token (i.e. already divided
# by 1,000,000 from the usual "$X per million tokens" published rate).
PRICING = {
    "anthropic": {
        # Claude Sonnet 5 — Anthropic's published per-million-token rate as of
        # writing: $3 input / $15 output.
        "input_per_token": 3.0 / 1_000_000,
        "output_per_token": 15.0 / 1_000_000,
    },
    "openai": {
        # gpt-4o — OpenAI's published per-million-token rate as of writing:
        # $2.50 input / $10 output.
        "input_per_token": 2.5 / 1_000_000,
        "output_per_token": 10.0 / 1_000_000,
    },
    "deepseek": {
        # deepseek-v4-flash, off-peak cache-miss rate (DeepSeek's own
        # published pricing) — the cheap/fast model, not deepseek-v4-pro.
        "input_per_token": 0.22 / 1_000_000,
        "output_per_token": 0.66 / 1_000_000,
    },
    "groq": {
        # Free tier — no per-token cost to us.
        "input_per_token": 0.0,
        "output_per_token": 0.0,
    },
    "fal_text": {
        # fal.ai's OpenRouter passthrough doesn't report token usage back to
        # us, so this is a flat per-request estimate rather than per-token.
        "flat_per_request": 0.01,
    },
    "izivoice_tts": {
        # PLACEHOLDER — replace with Izivoice's real contract rate once known.
        "per_character": 0.00003,
    },
    "izivoice_stt": {
        # PLACEHOLDER — replace with Izivoice's real contract rate once known.
        "per_second": 0.0001,
    },
    "fal_image": {
        # PLACEHOLDER flat estimate — fal.ai's image models are priced per
        # model (some per-megapixel, some flat); refine per-model if the
        # mix of models used ever becomes cost-significant.
        "per_image": 0.02,
    },
}


def estimate_anthropic_cost(input_tokens: int, output_tokens: int) -> float:
    p = PRICING["anthropic"]
    return input_tokens * p["input_per_token"] + output_tokens * p["output_per_token"]


def estimate_openai_cost(input_tokens: int, output_tokens: int) -> float:
    p = PRICING["openai"]
    return input_tokens * p["input_per_token"] + output_tokens * p["output_per_token"]


def estimate_deepseek_cost(input_tokens: int, output_tokens: int) -> float:
    p = PRICING["deepseek"]
    return input_tokens * p["input_per_token"] + output_tokens * p["output_per_token"]


def estimate_izivoice_tts_cost(character_count: int) -> float:
    return character_count * PRICING["izivoice_tts"]["per_character"]


def estimate_izivoice_stt_cost(seconds: float) -> float:
    return seconds * PRICING["izivoice_stt"]["per_second"]


def estimate_image_cost(image_count: int) -> float:
    return image_count * PRICING["fal_image"]["per_image"]


def log_usage(
    provider: str,
    operation: str,
    quantity: float,
    unit: str,
    cost_usd: float,
    user_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    video_id: Optional[str] = None,
    meta: Optional[dict] = None,
) -> None:
    """Writes one api_usage_logs row in its own short-lived session, so a
    logging failure (or the caller not having a db session handy) can never
    fail the actual generation it's reporting on. Swallows its own errors."""
    try:
        from src.db.session import SessionLocal
        from src.db.models import ApiUsageLog

        db = SessionLocal()
        try:
            db.add(ApiUsageLog(
                provider=provider,
                operation=operation,
                quantity=quantity,
                unit=unit,
                cost_usd=cost_usd,
                user_id=user_id,
                channel_id=channel_id,
                video_id=video_id,
                meta=meta,
                created_at=datetime.utcnow(),
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        from src.utils.logger import logger
        logger.exception("[cost_tracking] failed to log API usage — continuing without it.")
