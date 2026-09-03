"""Splits a full block of pasted instructions/script text across the parts of
a channel's auto-script structure — so a creator can paste one complete
document instead of copying each section into its own field by hand."""
import json
import re

from src.pipeline.ai_text import generate_text
from src.utils.logger import logger


def analyze_script_structure_text(full_text: str, parts: list[dict]) -> list[dict]:
    """Returns a full parts list (name, word_count, guidance) built from
    `full_text`. `parts` is only a fallback reference structure, not a mold
    the result is forced into: when the pasted text itself implies a
    structure (its own sections, its own number of parts, an explicit
    instruction like "fais-en 8 parties"), the AI is free to invent a
    different part count/order/weighting entirely — a creator pasting a
    9-part outline used to get it silently squeezed into the channel's
    existing 5 fixed parts, mismatched guidance and all. `parts` is used
    as-is only when the text gives no structural signal of its own.
    Raises on failure — the caller decides how to surface that to the user."""
    if not full_text.strip():
        raise ValueError("Le texte collé est vide.")

    reference_parts_list = "\n".join(
        f'{i + 1}. name="{p.get("name", "")}" (~{p.get("word_count", 0)} mots)'
        for i, p in enumerate(parts)
    ) or "(aucune — invente une structure adaptée)"
    prompt = f"""Tu reçois un texte complet (script, notes, ou instructions pour un vidéaste). Détermine la structure de script (liste ordonnée de parties) qui correspond le mieux à ce texte, puis rédige l'instruction de chaque partie.

Règles :
- Si le texte décrit, contient ou implique clairement sa propre structure (ses propres sections, un nombre de parties donné, une instruction explicite comme "fais-en 8 parties"), UTILISE CETTE STRUCTURE plutôt que le modèle de référence ci-dessous — le nombre de parties peut être supérieur, inférieur ou simplement différent de celui du modèle.
- Si le texte ne donne AUCUNE indication de structure, reprends le modèle de référence tel quel comme base.
- Chaque partie a : un nom court en anglais_minuscule_avec_underscores (usage interne, jamais montré au spectateur), un nombre de mots approximatif cohérent avec ce qu'on lui demande de couvrir, et une instruction/guidance concise (2-5 phrases) dans la même langue que le texte collé.

Modèle de référence (structure par défaut, seulement si le texte ne dit rien d'autre) :
{reference_parts_list}

Texte collé par l'utilisateur :
\"\"\"
{full_text[:12000]}
\"\"\"

Réponds uniquement avec ce JSON, rien d'autre :
{{"parts": [{{"name": "...", "word_count": 000, "guidance": "..."}}, ...]}}"""

    text = generate_text(prompt, max_tokens=3000, operation="script_structure_analysis")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"[script_structure_analyzer] non-JSON response: {text[:300]}")
        raise ValueError("L'IA n'a pas renvoyé un résultat exploitable, réessaie.") from e

    new_parts = data.get("parts") if isinstance(data, dict) else None
    if not isinstance(new_parts, list) or not new_parts:
        raise ValueError("L'IA n'a renvoyé aucune partie, réessaie.")

    return [
        {
            "name": str(p.get("name") or f"part_{i + 1}").strip(),
            "word_count": max(20, int(p.get("word_count") or 0) or 300),
            "guidance": str(p.get("guidance") or "").strip(),
        }
        for i, p in enumerate(new_parts)
    ]
