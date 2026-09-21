"""Matches library/community images to the scene they actually illustrate.

get_image_pool, the path every library and community image took until now,
never sees the narration at all: it gathers files and shuffles them. That is
fine for a niche where any on-theme visual works (religion, tourism,
motivation), and wrong for one where the viewer is being told to do
something specific and has to see it — the "images that don't illustrate
anything" complaint that started this.

This module is the other half of that choice (see SCENE_ACCURACY_STRICT in
images.py): it scores each candidate image against each scene's own text
using the keywords a vision pass already stored for it
(CommunityLibraryImageTag.tags_json — concrete subject/object/setting words,
populated by queue_runner's tagging loop), and leaves a scene unmatched
rather than handing it something irrelevant. An unmatched scene is exactly
what the generator is then paid to fill.

No AI call and no network here: the tags were computed once, in the
background, and matching is a set intersection over them.
"""
import json
import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Set

from src.utils.logger import logger

# A single shared keyword ("man", "light") is a coincidence, not a match:
# asking for two is what separates "this image is about what's being said"
# from "both contain a person". Raising it further mostly produces unmatched
# scenes, which cost a generated image each.
MIN_TAG_OVERLAP = 2

# Words that would otherwise match everything in a narration-heavy prompt.
# Deliberately short: the tags themselves are concrete nouns, so most noise
# never reaches the intersection.
_STOPWORDS = {
    "avec", "dans", "pour", "plus", "cette", "tous", "tout", "sont", "etre",
    "nous", "vous", "leur", "comme", "mais", "donc", "alors", "sans", "vers",
    "une", "des", "les", "aux", "sur", "par", "qui", "que", "est", "son", "ses",
    "the", "and", "for", "this", "that", "with", "from", "into", "about", "are",
    "image", "photo", "scene", "video", "style", "shot", "view", "background",
}


def _normalize(text: str) -> str:
    """Accent-insensitive lowercase, so "santé" and "sante" are one word."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _words(text: str) -> Set[str]:
    return {w for w in re.findall(r"\w+", _normalize(text)) if len(w) > 3 and w not in _STOPWORDS}


def load_image_tags(paths: List[Path]) -> Dict[Path, Set[str]]:
    """Vision keywords for each candidate image, keyed by its path.

    Library files live at channels/{channel_id}/library/{filename}, which is
    how a path on disk is turned back into the (channel_id, filename) pair
    the tag rows are keyed by. An image the tagging pass hasn't reached yet
    simply has no entry and can never match — it stays available to the
    random pool, it just won't be presented as "this illustrates the scene".
    """
    if not paths:
        return {}
    from src.db.session import SessionLocal
    from src.db.models import CommunityLibraryImageTag

    wanted = {}
    for path in paths:
        try:
            channel_id = path.parent.parent.name
        except (AttributeError, IndexError):
            continue
        wanted[(channel_id, path.name)] = path

    db = SessionLocal()
    try:
        rows = (
            db.query(CommunityLibraryImageTag)
            .filter(CommunityLibraryImageTag.channel_id.in_({cid for cid, _ in wanted}))
            .all()
        )
    except Exception as exc:
        logger.warning(f"Could not load library image tags, scene matching disabled for this render: {exc}")
        return {}
    finally:
        db.close()

    tags: Dict[Path, Set[str]] = {}
    for row in rows:
        path = wanted.get((row.channel_id, row.filename))
        if path is None:
            continue
        try:
            keywords = json.loads(row.tags_json) or []
        except (ValueError, TypeError):
            continue
        normalized = {_normalize(str(k)) for k in keywords if str(k).strip()}
        if normalized:
            tags[path] = normalized
    return tags


def match_images_to_scenes(
    scene_texts: List[str],
    candidates: List[Path],
    min_overlap: int = MIN_TAG_OVERLAP,
) -> List[Optional[Path]]:
    """Best tagged image per scene, or None when nothing genuinely fits.

    Each image is used at most once while unused ones remain: repeating one
    picture across a video reads as padding, and the point here is that each
    scene shows its own subject. Once every matching image is spent, later
    scenes are left None rather than recycling — an honest gap the caller
    can fill with a generated image or the random pool.

    Returns a list positionally aligned with scene_texts.
    """
    results: List[Optional[Path]] = [None] * len(scene_texts)
    if not scene_texts or not candidates:
        return results

    tags = load_image_tags(candidates)
    if not tags:
        logger.info("Scene matching: no tagged library images available; every scene stays unmatched.")
        return results

    used: Set[Path] = set()
    matched = 0
    for i, text in enumerate(scene_texts):
        scene_words = _words(text)
        if not scene_words:
            continue
        best_path, best_score = None, 0
        for path, keywords in tags.items():
            if path in used:
                continue
            # A multi-word tag ("chess board") counts when any of its own
            # words appears, so tagging style doesn't decide the match.
            score = sum(1 for tag in keywords if _words(tag) & scene_words)
            if score > best_score:
                best_path, best_score = path, score
        if best_path is not None and best_score >= min_overlap:
            results[i] = best_path
            used.add(best_path)
            matched += 1

    logger.info(
        f"Scene matching: {matched}/{len(scene_texts)} scene(s) matched a tagged library image "
        f"({len(tags)} tagged candidate(s)); the rest are left for generation or the pool."
    )
    return results
