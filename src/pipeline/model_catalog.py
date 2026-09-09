"""Provider/model catalog used by the admin routing UI.

Model ids are intentionally configuration data: providers can add or retire
models without changing the routing code. The runtime still validates the
selected provider and falls back when a model is unavailable.
"""

MODEL_CATALOG = {
    "anthropic": {
        "label": "Claude (Anthropic)",
        "text": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-5"],
    },
    "kie": {
        "label": "Claude via Kie.ai",
        "text": ["claude-opus-5", "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-5"],
        "music": ["suno-v4", "suno-v4.5", "suno-v5"],
        "voice": ["standard", "neural", "elevenlabs"],
    },
    "fal": {
        "label": "fal.ai",
        "text": ["anthropic/claude-opus-5", "anthropic/claude-sonnet-5"],
        "image": ["fal-ai/flux-pro", "fal-ai/flux/dev", "fal-ai/flux/schnell", "fal-ai/gpt-image-2"],
        "music": ["cassetteai/music-generator"],
        "voice": ["fal-ai/dia-tts"],
    },
    "openai": {
        "label": "OpenAI",
        "text": ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
        "image": ["gpt-image-2", "gpt-image-1.5"],
        "voice": ["gpt-4o-mini-tts", "gpt-4o-realtime"],
    },
    "deepseek": {"label": "DeepSeek", "text": ["deepseek-v4-flash", "deepseek-v4" ]},
    "groq": {"label": "Groq (gratuit)", "text": ["openai/gpt-oss-120b", "llama-4-scout", "qwen3-32b"]},
    "gemini": {
        "label": "Google Gemini (gratuit)",
        "text": ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-pro"],
        "image": ["imagen-4", "gemini-2.5-flash-image"],
        "voice": ["gemini-2.5-flash-tts"],
    },
    "izivoice": {"label": "Izivoice", "voice": ["default"], "music": ["default"]},
    "ai33pro": {"label": "ai33.pro", "voice": ["default"], "music": ["default"]},
}

TASKS = ("text", "image", "music", "voice")


def catalog_for(task: str | None = None) -> dict:
    if task is None:
        return MODEL_CATALOG
    return {pid: data for pid, data in MODEL_CATALOG.items() if task in data}
