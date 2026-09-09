"""Credential lookup for selectable media providers (IDs remain stable)."""
from src import config


def provider_key(provider: str, personal_izivoice_key: str = None) -> str:
    if provider == "izivoice" and personal_izivoice_key:
        return personal_izivoice_key
    from src.utils.provider_status import _get_effective_key
    name = {"izivoice": "IZIVOICE_API_KEY", "ai33pro": "AI33PRO_API_KEY", "kie": "KIE_API_KEY"}.get(provider)
    return _get_effective_key(provider, getattr(config, name, "")) if name else ""
