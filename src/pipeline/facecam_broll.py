"""B-roll trigger detection and sourcing for the facecam pipeline (Scout).

Rule-based trigger detection (no topic model) ported from the `editing-os`
reference tool, sourcing footage from three providers in order: the
creator-approved community library, Pexels, then Google Image Search — same
provider set already used elsewhere in KappGen (search_public_library,
stock_video.py), plus Google as a broader fallback for the specific
proper-noun/tool/screenshot triggers below. Every asset used is billed to the
creator via STOCK_MEDIA_CREDITS regardless of provider cost, per the existing
"no call is ever invisible or free" billing rule (see billing.py).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from src.config import GOOGLE_CSE_API_KEY, GOOGLE_CSE_CX, STORAGE_PATH
from src.pipeline.stock_video import fetch_stock_clip, fetch_stock_photo

logger = logging.getLogger(__name__)

DEDUPE_WINDOW_SECONDS = 4.0
MIN_GAP_FOR_PACING_FILL = 30.0
GOOGLE_IMAGE_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"
GOOGLE_CACHE_DIR = STORAGE_PATH / "cache" / "google_images"

TOOL_NAMES = {
    "chatgpt", "notion", "slack", "github", "figma", "youtube", "twitter", "x",
    "instagram", "tiktok", "linkedin", "google", "gmail", "zoom", "discord",
    "spotify", "netflix", "canva", "photoshop", "excel", "airtable",
}
STOP_CAPS = {
    "I", "The", "A", "An", "This", "That", "So", "But", "And", "Because",
    "When", "If", "You", "We", "It", "There", "Here",
}
VISUAL_CUE_PHRASES = [
    "imagine", "picture this", "imaginez", "imagine que", "par exemple",
    "for example", "prenons l'exemple",
]
URL_RE = re.compile(r"\b(?:https?://\S+|\S+\.(?:com|net|org|io|fr)\b)", re.IGNORECASE)


def detect_broll_triggers(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Returns [{"time", "kind", "query"}], deduplicated within a short window
    so a burst of matching words doesn't spam multiple cutaways back to back."""
    triggers: List[Dict[str, Any]] = []
    last_trigger_time = -DEDUPE_WINDOW_SECONDS

    full_text_words = [w["text"] for w in words]
    for i, word in enumerate(words):
        text = word["text"]
        t = word["start"]
        if t - last_trigger_time < DEDUPE_WINDOW_SECONDS:
            continue

        kind = None
        query = None

        if URL_RE.match(text):
            kind, query = "screenshot", text.rstrip(".,!?")
        elif text.strip(".,!?").lower() in TOOL_NAMES:
            kind, query = "logo", text.strip(".,!?")
        elif text[:1].isupper() and text not in STOP_CAPS and i > 0 and len(text) > 2:
            kind, query = "cutaway", text.strip(".,!?")
        else:
            window = " ".join(full_text_words[max(0, i - 2):i + 3]).lower()
            for phrase in VISUAL_CUE_PHRASES:
                if phrase in window:
                    kind, query = "cutaway", " ".join(full_text_words[i:i + 6])
                    break

        if kind and query:
            triggers.append({"time": t, "kind": kind, "query": query})
            last_trigger_time = t

    # Pacing-fill: long stretches with nothing visual yet.
    if words:
        last_covered = 0.0
        for trig in sorted(triggers, key=lambda x: x["time"]):
            if trig["time"] - last_covered > MIN_GAP_FOR_PACING_FILL:
                triggers.append({
                    "time": last_covered + MIN_GAP_FOR_PACING_FILL / 2,
                    "kind": "cutaway",
                    "query": None,  # filled in from surrounding words by the caller if needed
                })
            last_covered = trig["time"]

    return sorted(triggers, key=lambda x: x["time"])


def fetch_google_image(query: str) -> Optional[Path]:
    if not GOOGLE_CSE_API_KEY or not GOOGLE_CSE_CX:
        return None
    query = re.sub(r"\s+", " ", query or "").strip()
    if not query:
        return None

    GOOGLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = GOOGLE_CACHE_DIR / f"{quote(query, safe='')[:120]}.jpg"
    if cached.exists() and cached.stat().st_size > 0:
        return cached

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(GOOGLE_IMAGE_SEARCH_URL, params={
                "key": GOOGLE_CSE_API_KEY,
                "cx": GOOGLE_CSE_CX,
                "q": query,
                "searchType": "image",
                "num": 1,
                "safe": "active",
            })
            if resp.status_code == 429:
                logger.warning("Google CSE quota reached; skipping Google image b-roll for this trigger.")
                return None
            resp.raise_for_status()
            items = resp.json().get("items") or []
            if not items:
                return None
            image_url = items[0].get("link")
            if not image_url:
                return None
            image = client.get(image_url)
            image.raise_for_status()
            cached.write_bytes(image.content)
            return cached
    except Exception:
        logger.exception("Google image b-roll fetch failed for query=%r", query)
        return None


def _find_community_asset(query: str, db) -> Optional[Path]:
    """Mirrors the niche-matching logic in
    channels.py::search_public_library (folder.niche + per-image placement
    override, substring-both-ways matching) — kept in sync with it rather
    than calling the route function directly, since that one is wired to
    FastAPI's Depends(get_db)/Depends(get_current_user) and returns an
    HTTP-shaped payload, not a single best asset path."""
    from src.api.routes.channels import CommunityLibraryFolder, CommunityLibraryImagePlacement

    normalized_query = query.casefold()
    query_words = {w for w in re.findall(r"\w+", normalized_query) if len(w) > 2}
    if not query_words:
        return None

    folders = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.status == "approved").all()
    placements = {
        (p.channel_id, p.filename): p.niche
        for p in db.query(CommunityLibraryImagePlacement)
        .join(CommunityLibraryFolder, CommunityLibraryFolder.channel_id == CommunityLibraryImagePlacement.channel_id)
        .filter(CommunityLibraryFolder.status == "approved")
        .all()
    }
    for folder in folders:
        library_dir = STORAGE_PATH / "channels" / folder.channel_id / "library"
        if not library_dir.is_dir():
            continue
        for asset in library_dir.iterdir():
            if not asset.is_file():
                continue
            asset_niche = placements.get((folder.channel_id, asset.name), folder.niche)
            niche_words = {w for w in re.findall(r"\w+", asset_niche.casefold()) if len(w) > 2}
            if query_words & niche_words:
                return asset
    return None


def source_broll_asset(query: str, kind: str, db) -> Optional[Path]:
    """Tries the community library, then Pexels, then Google Image Search —
    first hit wins. Caller is responsible for billing STOCK_MEDIA_CREDITS
    once per asset actually used (see facecam_editor.py)."""
    community_asset = _find_community_asset(query, db)
    if community_asset:
        return community_asset

    if kind == "cutaway":
        clip = fetch_stock_clip(query)
        if clip:
            return clip
    photo = fetch_stock_photo(query)
    if photo:
        return photo

    return fetch_google_image(query)
