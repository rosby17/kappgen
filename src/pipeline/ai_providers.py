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
    {"id": "kie",       "label": "Kie.ai",             "key": "KIE_API_KEY",       "capabilities": {"text"}},
    {"id": "deepseek",  "label": "DeepSeek",           "key": "DEEPSEEK_API_KEY",  "capabilities": {"text"}},
    {"id": "fal",       "label": "fal.ai",             "key": "FAL_API_KEY",       "capabilities": {"text", "vision"}},
    {"id": "openai",    "label": "OpenAI",             "key": "OPENAI_API_KEY",    "capabilities": {"text", "vision"}},
    {"id": "groq",      "label": "Groq",               "key": "GROQ_API_KEY",      "capabilities": {"text", "vision"}},
    {"id": "xai",       "label": "xAI (Grok)",          "key": "XAI_API_KEY",       "capabilities": {"text"}},
    {"id": "gemini",    "label": "Google Gemini",      "key": "GEMINI_API_KEY",    "capabilities": {"text", "vision"}},
    {"id": "ollama",    "label": "Ollama (Local / Mac)", "key": "OLLAMA_BASE_URL", "capabilities": {"text", "vision"}},
]

ALL_IDS = [p["id"] for p in PROVIDERS]
_BY_ID = {p["id"]: p for p in PROVIDERS}


def ids_for(capability: str) -> List[str]:
    """Every registered provider able to serve this kind of call, in default
    order. A provider missing the capability is skipped rather than occupying
    a slot in the chain (DeepSeek has no image input, for instance).

    When the admin's emergency kill switch is on (app_settings.py's
    paid_apis_disabled — "stop everything that spends money right now"),
    every provider not in AI_TEXT_FREE_PROVIDERS is dropped here rather than
    in ordered_ids()'s own ranking logic, since that function always appends
    every capable-but-unranked provider behind the ranked ones — an order
    list alone can reorder the chain but never actually remove a provider
    from it. This is the one place that can."""
    capable = [p["id"] for p in PROVIDERS if capability in p["capabilities"]]
    from src.utils.app_settings import paid_apis_disabled, AI_TEXT_FREE_PROVIDERS
    if paid_apis_disabled():
        return [pid for pid in capable if pid in AI_TEXT_FREE_PROVIDERS]
    return capable


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
    """Return the admin's active routing order for a capability.

    For text generation, a non-empty numbered list is the complete chain.
    Other capabilities retain the legacy appended fallbacks until they have
    their own independent persisted routing settings.
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
    # For text, the chips selected by the admin are the complete routing
    # chain, not merely a preferred prefix.  Silently appending every other
    # provider made disabled providers (notably Gemini) write parts of a
    # script despite not appearing in the active numbered list.
    if capability == "text" and ranked:
        return ranked
    return ranked + [pid for pid in capable if pid not in ranked]
