"""Single registry of every AI provider the platform can call.

Before this module the provider list lived in four places at once — the
runtime chain in ai_text.py, the vision chain in vision.py, the admin
picker's AI_TEXT_PROVIDERS, and an availability guard hardcoding three key
names. They drifted: DeepSeek and Groq existed in some copies and not
others, so a deployment holding only a Groq key was told no provider was
available, and turning a provider off in the admin console changed text
generation but not image analysis.

Adding a provider is now one entry here plus its handler function in
ai_text.py (text) and/or vision.py (vision). The admin picker, the
availability guards and every fallback chain pick it up automatically —
however many providers there end up being.
"""
from typing import List

from src import config

# capabilities: which kinds of call this provider can serve.
#   "text"   — prompt in, text out (scripts, titles, descriptions, prompts...)
#   "vision" — images + prompt in, text out (style references, moodboards)
PROVIDERS = [
    {"id": "anthropic", "label": "Anthropic (Claude)", "key": "ANTHROPIC_API_KEY", "capabilities": {"text", "vision"}},
    {"id": "deepseek",  "label": "DeepSeek",           "key": "DEEPSEEK_API_KEY",  "capabilities": {"text"}},
    {"id": "fal",       "label": "fal.ai",             "key": "FAL_API_KEY",       "capabilities": {"text", "vision"}},
    {"id": "openai",    "label": "OpenAI",             "key": "OPENAI_API_KEY",    "capabilities": {"text", "vision"}},
    {"id": "groq",      "label": "Groq",               "key": "GROQ_API_KEY",      "capabilities": {"text", "vision"}},
]

ALL_IDS = [p["id"] for p in PROVIDERS]
_BY_ID = {p["id"]: p for p in PROVIDERS}


def ids_for(capability: str) -> List[str]:
    """Every registered provider able to serve this kind of call, in default
    order. A provider missing the capability is skipped rather than occupying
    a slot in the chain (DeepSeek has no image input, for instance)."""
    return [p["id"] for p in PROVIDERS if capability in p["capabilities"]]


def is_configured(provider_id: str) -> bool:
    entry = _BY_ID.get(provider_id)
    return bool(entry and getattr(config, entry["key"], ""))


def configured_map() -> dict:
    """{provider_id: has a key} — what the admin console shows as available."""
    return {p["id"]: is_configured(p["id"]) for p in PROVIDERS}


def any_configured(capability: str = "text") -> bool:
    """Whether ANY provider can serve this capability. Feature guards call
    this instead of naming key constants, so a newly added provider makes the
    feature available without touching every guard."""
    return any(is_configured(pid) for pid in ids_for(capability))


def ordered_ids(capability: str = "text") -> List[str]:
    """The admin's priority order (Ressources tab) filtered to providers that
    can serve this capability, with everything unranked appended behind.

    The chain is reordered, never emptied: a provider the admin didn't rank
    still trails as a fallback, so an exhausted balance degrades to the next
    provider instead of failing the request.
    """
    capable = ids_for(capability)
    try:
        from src.utils.app_settings import ai_text_provider_order
        ranked = [pid for pid in ai_text_provider_order() if pid in capable]
    except Exception as exc:  # noqa: BLE001 - ordering is a preference, not a dependency
        # The order lives in the database. If that read fails (pool exhausted,
        # database briefly unreachable), fall back to the default order rather
        # than taking every AI feature down with it — a preference must never
        # be a hard dependency of generating anything.
        from src.utils.logger import logger
        logger.warning(f"[ai_providers] could not read the admin provider order ({exc}); using the default order.")
        ranked = []
    return ranked + [pid for pid in capable if pid not in ranked]
