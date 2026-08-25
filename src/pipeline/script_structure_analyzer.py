"""Splits a full block of pasted instructions/script text across the parts of
a channel's auto-script structure — so a creator can paste one complete
document instead of copying each section into its own field by hand."""
import json
import re

from src.pipeline.ai_text import generate_text
from src.utils.logger import logger


def analyze_script_structure_text(full_text: str, parts: list[dict]) -> list[dict]:
    """Returns `parts` with each item's `guidance` replaced by the portion of
    `full_text` that belongs to it, as judged by the AI. Part `name` and
    `word_count` are left untouched. Raises on failure — the caller decides
    how to surface that to the user."""
    if not full_text.strip():
        raise ValueError("Le texte collé est vide.")
    if not parts:
        raise ValueError("Aucune partie à remplir.")

    parts_list = "\n".join(
        f'{i + 1}. name="{p.get("name", "")}" (~{p.get("word_count", 0)} mots)'
        for i, p in enumerate(parts)
    )
    prompt = f"""Tu reçois un texte complet (script, notes, ou instructions pour un vidéaste) et la liste des parties d'une structure de script à remplir.

Parties à remplir, dans l'ordre :
{parts_list}

Texte collé par l'utilisateur :
\"\"\"
{full_text[:12000]}
\"\"\"

Pour chaque partie, écris une instruction/guidance concise (2-5 phrases) qui résume ce que cette partie du texte collé doit couvrir, dans la même langue que le texte collé. Si le texte ne couvre pas clairement une partie donnée, déduis une instruction raisonnable à partir du contexte général plutôt que de la laisser vide.

Réponds uniquement avec ce JSON, rien d'autre — un objet dont les clés sont exactement les noms de parties ci-dessus :
{{"nom_de_la_partie": "instruction pour cette partie", ...}}"""

    text = generate_text(prompt, max_tokens=2000, operation="script_structure_analysis")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        mapping = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"[script_structure_analyzer] non-JSON response: {text[:300]}")
        raise ValueError("L'IA n'a pas renvoyé un résultat exploitable, réessaie.") from e

    return [
        {**p, "guidance": str(mapping.get(p.get("name", ""), p.get("guidance", "")) or p.get("guidance", ""))}
        for p in parts
    ]
