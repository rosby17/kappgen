"""Extracts a clean list of video titles from whatever a creator pastes into
Channel.topic_examples — often not a tidy list at all, but a raw copy-paste
straight off a competitor's channel page (view counts, relative dates,
timestamps, "voir plus" buttons and other UI chrome all mixed in with the
actual titles). Left unfiltered, that noise overwhelmed the topic-selection
prompt badly enough to break it outright in production (a 40KB+ paste on one
real channel). script_writer._pick_topic already has a cheap regex safety
net for obvious junk lines; this is the smarter version — an AI pass that
understands *what a title actually is* well enough to separate real
signal (titles worth studying for angle/hook style) from everything else,
including titles that happen to wrap across two lines or carry inline
metadata a regex would never safely untangle.
"""
import json
import re

from src.pipeline.ai_text import generate_text
from src.utils.logger import logger


def clean_topic_examples(raw_text: str) -> str:
    """Returns a clean, one-title-per-line string extracted from `raw_text`.
    Raises ValueError if nothing usable could be extracted — caller should
    surface that instead of silently emptying the creator's field."""
    if not raw_text.strip():
        raise ValueError("Le texte collé est vide.")

    prompt = f"""Un créateur YouTube a collé le texte suivant dans un champ "exemples de titres/sujets qui marchent" pour sa chaîne — c'est souvent un copier-coller brut d'une page de chaîne concurrente ou d'une liste de vidéos, pas une liste propre : ça peut mélanger de vrais titres avec du nombre de vues ("4,5 M de vues"), des dates relatives ("il y a 11 mois"), des horodatages, des boutons d'interface ("Voir plus"), etc.

Texte collé :
\"\"\"
{raw_text[:12000]}
\"\"\"

Extrais UNIQUEMENT les vrais titres de vidéos qui s'y trouvent, un par ligne, sans numérotation ni tiret, sans rien ajouter ni reformuler — copie le titre exact tel qu'il apparaît dans le texte. Ignore tout le reste (vues, dates, horodatages, éléments d'interface, doublons — ne garde qu'une occurrence de chaque titre).

Réponds uniquement avec ce JSON, rien d'autre :
{{"titles": ["titre 1", "titre 2", ...]}}"""

    text = generate_text(prompt, max_tokens=3000, operation="topic_examples_cleanup")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"[topic_examples_cleaner] non-JSON response: {text[:300]}")
        raise ValueError("L'IA n'a pas renvoyé un résultat exploitable, réessaie.") from e

    titles = [str(t).strip() for t in (data.get("titles") or []) if str(t).strip()]
    if not titles:
        raise ValueError("Aucun titre exploitable n'a été trouvé dans ce texte.")
    return "\n".join(titles)
