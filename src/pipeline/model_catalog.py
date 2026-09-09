"""Provider/model catalog used by the admin routing UI.

Model ids are intentionally configuration data: providers can add or retire
models without changing the routing code. The runtime still validates the
selected provider and falls back when a model is unavailable.
"""

MODEL_CATALOG = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "text": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-5"],
    },
    "kie": {
        "label": "Kie.ai",
        "text": [
            "gpt-6-astra",
            "gpt-5-6-terra",
            "gpt-5-6-sol",
            "gpt-5-6-luna",
            "gpt-5-5",
            "gpt-5-codex",
            "gpt-5-2",
            "gemini-3-8-flash",
            "gemini-3-7-flash",
            "gemini-3-6-flash",
            "gemini-3-pro",
            "gemini-3-flash",
            "grok-4-6",
            "grok-4-5",
            "grok-4-3",
        ],
        "image": ["gpt-image-2", "flux-pro", "flux-dev", "flux-schnell"],
        "thumbnail": ["gpt-image-2", "flux-pro", "flux-dev", "flux-schnell"],
        "music": ["suno-v4", "suno-v4.5", "suno-v5"],
        "voice": ["standard", "neural", "elevenlabs"],
    },
    "fal": {
        "label": "fal.ai",
    },
    "openai": {
        "label": "OpenAI",
        "text": ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-4o", "gpt-4o-mini", "o3-mini"],
        "image": ["gpt-image-2", "gpt-image-1.5"],
        "thumbnail": ["gpt-image-2", "gpt-image-1.5"],
        "voice": ["gpt-4o-mini-tts", "gpt-4o-realtime"],
    },
    "deepseek": {"label": "DeepSeek", "text": ["deepseek-v4-flash", "deepseek-v4", "deepseek-r1", "deepseek-v3"]},
    "groq": {"label": "Groq", "text": ["openai/gpt-oss-120b", "llama-4-scout", "qwen3-32b", "llama-3.3-70b-versatile"]},
    "xai": {"label": "xAI", "text": ["grok-4-6", "grok-4", "grok-4-fast"]},
    "gemini": {
        "label": "Google Gemini",
        "text": ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-pro", "gemini-2.0-flash", "gemini-1.5-pro"],
        "image": ["imagen-4", "gemini-2.5-flash-image"],
        "thumbnail": ["imagen-4", "gemini-2.5-flash-image"],
        "voice": ["gemini-2.5-flash-tts"],
    },
    "izivoice": {
        "label": "Izivoice",
        "image": ["gpt-image-2"],
        "thumbnail": ["gpt-image-2"],
        "voice": ["default"],
        "music": ["default"],
    },
    "ai33pro": {
        "label": "KappGen",
        "image": ["gpt-image-2"],
        "thumbnail": ["gpt-image-2"],
        "voice": ["default"],
        "music": ["default"],
    },
    "huggingface": {
        "label": "Hugging Face",
        "image": ["black-forest-labs/FLUX.1-schnell"],
        "thumbnail": ["black-forest-labs/FLUX.1-schnell"],
    },
    "ollama": {
        "label": "Ollama (Mac)",
        "text": ["qwen3.5:latest", "qwen3-coder:30b", "qwen3-coder:latest"],
        "vision": ["qwen3.5:latest"],
    },
}

TASKS = ("text", "image", "thumbnail", "music", "voice")

# USD per million tokens. Kept separate from the model ids because the same
# model may cost differently depending on the reseller/source.
MODEL_PRICING = {
    # Anthropic direct ($ / 1M tokens)
    "anthropic:claude-opus-5": {"input": 2.0, "output": 10.0},
    "anthropic:claude-sonnet-5": {"input": 3.0, "output": 15.0},
    "anthropic:claude-haiku-5": {"input": 0.80, "output": 4.0},

    # Kie.ai ($ / 1M tokens — official Kie.ai pricing)
    "kie:gpt-6-astra": {"input": 2.80, "output": 14.0},
    "kie:gpt-5-6-terra": {"input": 0.56, "output": 3.36},
    "kie:gpt-5-6-sol": {"input": 1.40, "output": 8.40},
    "kie:gpt-5-6-luna": {"input": 0.056, "output": 0.336},
    "kie:gpt-5-5": {"input": 1.40, "output": 8.40},
    "kie:gpt-5-4": {"input": 0.70, "output": 5.60},
    "kie:gpt-5-4-codex": {"input": 0.70, "output": 5.60},
    "kie:gpt-5-3-codex": {"input": 0.70, "output": 5.60},
    "kie:gpt-5-2-codex": {"input": 0.70, "output": 5.60},
    "kie:gpt-5-1-codex": {"input": 0.50, "output": 4.00},
    "kie:gpt-5-codex": {"input": 0.50, "output": 4.00},
    "kie:gpt-5-2": {"input": 0.44, "output": 3.50},
    "kie:gpt-4o": {"input": 1.25, "output": 5.00},
    "kie:gpt-4o-mini": {"input": 0.075, "output": 0.30},
    "kie:o3-mini": {"input": 0.55, "output": 2.20},
    "kie:claude-opus-5": {"input": 2.0, "output": 10.0},
    "kie:claude-sonnet-5": {"input": 0.85, "output": 4.275},
    "kie:claude-fable-5": {"input": 4.0, "output": 20.0},
    "kie:claude-opus-4-8": {"input": 2.0, "output": 10.0},
    "kie:claude-opus-4-7": {"input": 1.425, "output": 7.15},
    "kie:claude-sonnet-4-6": {"input": 0.85, "output": 4.275},
    "kie:claude-haiku-4-5": {"input": 0.275, "output": 1.425},
    "kie:claude-haiku-5": {"input": 0.25, "output": 1.25},
    "kie:claude-3-7-sonnet": {"input": 1.50, "output": 7.50},
    "kie:claude-3-5-sonnet": {"input": 1.50, "output": 7.50},
    "kie:gemini-3-8-flash": {"input": 0.225, "output": 1.125},
    "kie:gemini-3-7-flash": {"input": 0.225, "output": 1.125},
    "kie:gemini-3-6-flash": {"input": 0.225, "output": 1.125},
    "kie:gemini-3-5-flash": {"input": 0.45, "output": 2.70},
    "kie:gemini-3-1-pro": {"input": 0.50, "output": 3.50},
    "kie:gemini-3-pro": {"input": 0.50, "output": 3.50},
    "kie:gemini-3-flash": {"input": 0.15, "output": 0.90},
    "kie:gemini-2-5-pro": {"input": 0.38, "output": 3.00},
    "kie:gemini-2-5-flash": {"input": 0.09, "output": 0.75},
    "kie:gemini-2.0-flash": {"input": 0.05, "output": 0.20},
    "kie:gemini-1.5-pro": {"input": 1.25, "output": 5.00},
    "kie:grok-4-6": {"input": 0.80, "output": 2.40},
    "kie:grok-4-5": {"input": 0.80, "output": 2.40},
    "kie:grok-4-3": {"input": 0.50, "output": 1.00},
    "kie:deepseek-r1": {"input": 0.28, "output": 1.10},
    "kie:deepseek-v3": {"input": 0.14, "output": 0.28},
    "kie:gpt-image-2": {"output": 0.02},
    "kie:flux-pro": {"output": 0.04},
    "kie:flux-dev": {"output": 0.025},
    "kie:flux-schnell": {"output": 0.015},
    "kie:suno-v4": {"output": 0.06},
    "kie:suno-v4.5": {"output": 0.06},
    "kie:suno-v5": {"output": 0.06},

    # fal.ai
    "fal:anthropic/claude-opus-5": {"input": 2.0, "output": 10.0},
    "fal:anthropic/claude-sonnet-5": {"input": 1.50, "output": 7.50},
    "fal:anthropic/claude-3.7-sonnet": {"input": 3.0, "output": 15.0},
    "fal:anthropic/claude-3.5-sonnet": {"input": 3.0, "output": 15.0},
    "fal:openai/gpt-4o": {"input": 2.50, "output": 10.0},
    "fal:openai/gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "fal:deepseek/deepseek-r1": {"input": 0.55, "output": 2.19},
    "fal:deepseek/deepseek-v3": {"input": 0.27, "output": 1.10},
    "fal:meta-llama/llama-3.3-70b-instruct": {"input": 0.40, "output": 0.80},
    "fal:fal-ai/flux-pro": {"output": 0.04},
    "fal:fal-ai/flux/dev": {"output": 0.025},
    "fal:fal-ai/flux/schnell": {"output": 0.003},
    "fal:fal-ai/gpt-image-2": {"output": 0.02},
    "fal:fal-ai/recraft-v3": {"output": 0.04},
    "fal:cassetteai/music-generator": {"output": 0.05},
    "fal:fal-ai/dia-tts": {"output": 0.01},

    # OpenAI direct
    "openai:gpt-6-astra": {"input": 2.80, "output": 14.0},
    "openai:gpt-5.6-sol": {"input": 1.40, "output": 8.40},
    "openai:gpt-5.6-terra": {"input": 0.56, "output": 3.36},
    "openai:gpt-5.6-luna": {"output": 0.336},
    "openai:gpt-4o": {"input": 2.50, "output": 10.0},
    "openai:gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "openai:o3-mini": {"input": 1.10, "output": 4.40},

    # DeepSeek direct
    "deepseek:deepseek-r1": {"input": 0.55, "output": 2.19},
    "deepseek:deepseek-v3": {"input": 0.27, "output": 1.10},
    "deepseek:deepseek-v4": {"input": 0.27, "output": 1.10},
    "deepseek:deepseek-v4-flash": {"input": 0.14, "output": 0.55},

    # Google Gemini
    "gemini:gemini-3.8-flash": {"input": 0.225, "output": 1.125},
    "gemini:gemini-3.7-flash": {"input": 0.225, "output": 1.125},
    "gemini:gemini-3.6-flash": {"input": 0.225, "output": 1.125},
    "gemini:gemini-3.5-pro": {"input": 1.25, "output": 5.0},
    "gemini:gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini:gemini-1.5-pro": {"input": 1.25, "output": 5.0},

    # Groq (Free Tier)
    "groq:openai/gpt-oss-120b": {"free_tier": True},
    "groq:llama-4-scout": {"free_tier": True},
    "groq:qwen3-32b": {"free_tier": True},
    "groq:llama-3.3-70b-versatile": {"free_tier": True},

    # xAI
    "xai:grok-4-6": {"input": 0.80, "output": 2.40},
    "xai:grok-4": {"input": 1.00, "output": 3.00},
    "xai:grok-4-fast": {"input": 0.20, "output": 0.50},

    # Izivoice ($5 / 1M tokens)
    "izivoice:default": {"input": 5.0, "output": 5.0},
    "izivoice:gpt-image-2": {"output": 0.02},

    # ai33.pro ($5 / 1M tokens)
    "ai33pro:default": {"input": 5.0, "output": 5.0},
    "ai33pro:gpt-image-2": {"output": 0.02},

    # Hugging Face
    "huggingface:black-forest-labs/FLUX.1-schnell": {"free_tier": True},

    # Ollama (Local / Mac — Free)
    "ollama:qwen3.5:latest": {"free_tier": True},
    "ollama:qwen3-coder:30b": {"free_tier": True},
    "ollama:qwen3-coder:latest": {"free_tier": True},
}


def catalog_for(task: str | None = None) -> dict:
    if task is None:
        return MODEL_CATALOG
    return {pid: data for pid, data in MODEL_CATALOG.items() if task in data}
