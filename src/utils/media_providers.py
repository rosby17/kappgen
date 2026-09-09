"""Credential lookup for selectable media providers (IDs remain stable)."""
from src import config


def provider_key(provider: str, personal_izivoice_key: str = None) -> str:
    from src.utils.provider_keys import key
    return key(provider)
