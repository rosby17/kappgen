"""Suggests a niche for a channel from its real YouTube name/description —
used right after a YouTube connection, since the creator never has to type
the niche themselves if we can infer it accurately from what they already
published on YouTube."""
import json
import re

from src.pipeline.ai_text import generate_text, any_text_provider_configured
from src.utils.logger import logger


def suggest_niche(title: str, description: str, existing_niches: list) -> str | None:
    """Returns a niche label — either one of `existing_niches` (preferred, so
    the shared niche list stays tidy) or a short new one if nothing fits.
    Returns None if it can't produce a confident guess."""
    if not any_text_provider_configured() or not title:
        return None
    try:
        niches_list = "\n".join(f"- {n}" for n in existing_niches[:200]) or "(aucune pour l'instant)"
        prompt = f"""Tu choisis la niche de contenu d'une chaîne YouTube à partir de son nom et sa description.

Nom de la chaîne : {title}
Description : {description[:1000] or "(vide)"}

Niches déjà utilisées par d'autres chaînes (réutilise-en une si elle correspond vraiment) :
{niches_list}

Réponds uniquement avec ce JSON, rien d'autre :
{{"niche": "Nom de la niche en 1 à 3 mots, en français"}}"""
        text = generate_text(prompt, max_tokens=200, operation='niche_detection')
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        data = json.loads(text)
        niche = str(data.get("niche") or "").strip()
        return niche or None
    except Exception as e:
        logger.warning(f"Niche detection failed, leaving niche unchanged: {e}")
        return None
