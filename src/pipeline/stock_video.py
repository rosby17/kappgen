"""Free stock video clips (Pexels) as an automatic visual source.

Until now a scene was either an AI-generated still animated with a Ken Burns
zoom, or a clip the creator uploaded themselves (B-roll). This adds a third
option: real stock footage fetched automatically from Pexels' free API, so a
channel with no footage of its own still gets actual motion instead of pans
over static images — the single biggest visual gap against hand-made
documentary channels.

Free by design, like the Hugging Face image tier: no KappGen credits are ever
debited here and nothing is charged to the creator. Pexels' API is free with a
key (200 requests/hour, 20 000/month), which is why every clip is cached on
disk by search query and reused across renders and channels — a niche's
recurring queries ("ocean waves", "city night traffic") hit the cache instead
of the API after the first render.

Attribution: the Pexels licence allows commercial use and modification without
per-file attribution, but the API terms do require crediting Pexels and the
creators. Every cached clip stores its author next to it (see `_meta_path`),
and `collect_attributions` gathers them so the publisher can append proper
credits to the video description.
"""
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from src.config import ASSETS_PATH, PEXELS_API_KEY
from src.utils.logger import logger

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/videos/search"
CACHE_DIR = ASSETS_PATH / "stock_video_cache"

# A scene lasts 20-45s; a clip shorter than this is looped by build_video_clip
# (visible repetition), so prefer longer source footage when it exists.
PREFERRED_MIN_DURATION_SECONDS = 8
MAX_CLIP_BYTES = 80 * 1024 * 1024  # a single 1080p stock clip; guards against pulling a 4K monster
REQUEST_TIMEOUT_SECONDS = 30.0


def _cache_key(query: str) -> str:
    return hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()[:16]


def _clip_path(query: str) -> Path:
    return CACHE_DIR / f"{_cache_key(query)}.mp4"


def _meta_path(query: str) -> Path:
    return CACHE_DIR / f"{_cache_key(query)}.json"


def _pick_best_file(video: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Picks the largest landscape file that still fits our 1920x1080 canvas
    without pulling an unnecessarily huge 4K master."""
    candidates = []
    for file in video.get("video_files") or []:
        width, height = file.get("width") or 0, file.get("height") or 0
        # Pexels' ``medium`` result is commonly 640x360.  The old 1280px
        # lower bound rejected every one of those files, so a perfectly good
        # API response quietly became the generic image fallback.  Accept
        # standard landscape HD/SD files; prefer HD below when both exist.
        if not file.get("link") or width < 640 or (height and width < height):
            continue
        if width <= 2560:
            candidates.append(file)
    if not candidates:
        return None
    # Closest to 1920 wide, preferring >= 1920 so we downscale rather than upscale.
    candidates.sort(key=lambda f: (f.get("width", 0) < 1920, abs(f.get("width", 0) - 1920)))
    return candidates[0]


def fetch_stock_clip(query: str) -> Optional[Path]:
    """Returns a local path to a landscape stock clip matching `query`, or
    None when stock video isn't configured, nothing matched, or the download
    failed. Never raises: a missing clip just means that scene falls back to
    its image, exactly like any other optional visual source."""
    if not PEXELS_API_KEY:
        return None
    query = re.sub(r"\s+", " ", (query or "")).strip()
    if not query:
        return None

    cached = _clip_path(query)
    if cached.exists() and cached.stat().st_size > 0:
        return cached

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = client.get(
                PEXELS_SEARCH_URL,
                headers={"Authorization": PEXELS_API_KEY},
                # Ask for large first so 1080p is available, while the
                # picker above still accepts 640px clips when that is all the
                # API has for a result.
                params={"query": query, "orientation": "landscape", "per_page": 10, "size": "large"},
            )
            if resp.status_code == 429:
                logger.warning("Pexels rate limit reached; falling back to images for this scene.")
                return None
            resp.raise_for_status()
            videos = resp.json().get("videos") or []
            if not videos:
                logger.info(f"No Pexels stock footage for query '{query}'.")
                return None

            # Prefer footage long enough to fill a scene without looping.
            videos.sort(key=lambda v: (v.get("duration") or 0) < PREFERRED_MIN_DURATION_SECONDS)
            for video in videos:
                best = _pick_best_file(video)
                if not best:
                    continue
                with client.stream("GET", best["link"]) as download:
                    download.raise_for_status()
                    written = 0
                    tmp_path = cached.with_suffix(".part.mp4")
                    with open(tmp_path, "wb") as handle:
                        for chunk in download.iter_bytes():
                            written += len(chunk)
                            if written > MAX_CLIP_BYTES:
                                raise ValueError(f"stock clip exceeds {MAX_CLIP_BYTES} bytes")
                            handle.write(chunk)
                tmp_path.replace(cached)
                _meta_path(query).write_text(json.dumps({
                    "query": query,
                    "pexels_id": video.get("id"),
                    "author": video.get("user", {}).get("name"),
                    "author_url": video.get("user", {}).get("url"),
                    "source_url": video.get("url"),
                    "duration": video.get("duration"),
                }, ensure_ascii=False), encoding="utf-8")
                return cached
    except Exception as exc:
        logger.warning(f"Pexels stock lookup failed for '{query}' ({exc}); this scene falls back to an image.")
        try:
            tmp = cached.with_suffix(".part.mp4")
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return None


PEXELS_PHOTO_SEARCH_URL = "https://api.pexels.com/v1/search"
PHOTO_CACHE_DIR = ASSETS_PATH / "stock_photo_cache"
MAX_PHOTO_BYTES = 15 * 1024 * 1024


def fetch_stock_photo(query: str) -> Optional[Path]:
    """Landscape stock photo for a scene, same free source and same cache
    discipline as the clips above. Used as a visual fallback for scenes that
    stock footage didn't cover — a real photograph still beats a generated
    placeholder gradient when the AI image tier is off or out of quota."""
    if not PEXELS_API_KEY:
        return None
    query = re.sub(r"\s+", " ", (query or "")).strip()
    if not query:
        return None

    cached = PHOTO_CACHE_DIR / f"{_cache_key(query)}.jpg"
    if cached.exists() and cached.stat().st_size > 0:
        return cached

    try:
        PHOTO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = client.get(
                PEXELS_PHOTO_SEARCH_URL,
                headers={"Authorization": PEXELS_API_KEY},
                params={"query": query, "orientation": "landscape", "per_page": 5},
            )
            if resp.status_code == 429:
                logger.warning("Pexels rate limit reached; no stock photo for this scene.")
                return None
            resp.raise_for_status()
            photos = resp.json().get("photos") or []
            for photo in photos:
                link = (photo.get("src") or {}).get("large2x") or (photo.get("src") or {}).get("large")
                if not link:
                    continue
                image = client.get(link)
                image.raise_for_status()
                if len(image.content) > MAX_PHOTO_BYTES:
                    continue
                cached.write_bytes(image.content)
                (PHOTO_CACHE_DIR / f"{_cache_key(query)}.json").write_text(json.dumps({
                    "query": query,
                    "pexels_id": photo.get("id"),
                    "author": photo.get("photographer"),
                    "author_url": photo.get("photographer_url"),
                    "source_url": photo.get("url"),
                }, ensure_ascii=False), encoding="utf-8")
                return cached
    except Exception as exc:
        logger.warning(f"Pexels photo lookup failed for '{query}' ({exc}).")
    return None


def fetch_stock_photos(query: str, count: int = 5) -> List[Path]:
    """Same free Pexels search as fetch_stock_photo, but keeps every distinct
    result it downloads (up to `count`) instead of just the first — that
    single-result behavior meant repeated calls for the same query always
    returned the exact same file forever, and the shared last-resort cache
    (image_pool.py's get_image_pool, when nothing else is configured/
    available) never grew past a literal handful of photos no matter how
    many renders hit it. Each distinct photo is cached under its own
    `{query_hash}_{pexels_id}.jpg`, so a niche's cache genuinely accumulates
    real, on-topic variety over time instead of every render reusing
    whatever happened to be cached first."""
    if not PEXELS_API_KEY:
        return []
    query = re.sub(r"\s+", " ", (query or "")).strip()
    if not query:
        return []

    key = _cache_key(query)
    already_cached = sorted(PHOTO_CACHE_DIR.glob(f"{key}_*.jpg")) if PHOTO_CACHE_DIR.is_dir() else []
    if len(already_cached) >= count:
        return already_cached

    try:
        PHOTO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = client.get(
                PEXELS_PHOTO_SEARCH_URL,
                headers={"Authorization": PEXELS_API_KEY},
                params={"query": query, "orientation": "landscape", "per_page": max(count, 5)},
            )
            if resp.status_code == 429:
                logger.warning("Pexels rate limit reached; using whatever stock photos are already cached.")
                return already_cached
            resp.raise_for_status()
            photos = resp.json().get("photos") or []
            fetched = list(already_cached)
            for photo in photos:
                if len(fetched) >= count:
                    break
                pexels_id = photo.get("id")
                cached = PHOTO_CACHE_DIR / f"{key}_{pexels_id}.jpg"
                if cached.exists() and cached.stat().st_size > 0:
                    if cached not in fetched:
                        fetched.append(cached)
                    continue
                link = (photo.get("src") or {}).get("large2x") or (photo.get("src") or {}).get("large")
                if not link:
                    continue
                image = client.get(link)
                image.raise_for_status()
                if len(image.content) > MAX_PHOTO_BYTES:
                    continue
                cached.write_bytes(image.content)
                (PHOTO_CACHE_DIR / f"{key}_{pexels_id}.json").write_text(json.dumps({
                    "query": query,
                    "pexels_id": pexels_id,
                    "author": photo.get("photographer"),
                    "author_url": photo.get("photographer_url"),
                    "source_url": photo.get("url"),
                }, ensure_ascii=False), encoding="utf-8")
                fetched.append(cached)
            return fetched
    except Exception as exc:
        logger.warning(f"Pexels photo search failed for '{query}' ({exc}); using whatever is already cached.")
        return already_cached


def fetch_stock_clips(queries: List[str]) -> Dict[int, Path]:
    """Resolves a clip per scene index, skipping scenes whose query returns
    nothing. Consecutive scenes never reuse the same file — repeating the same
    footage back-to-back is the most obvious "this was automated" tell — but a
    clip may reappear later in a long video rather than leaving a scene with
    no footage at all."""
    resolved: Dict[int, Path] = {}
    last_path: Optional[Path] = None
    for index, query in enumerate(queries):
        if not query:
            continue
        path = fetch_stock_clip(query)
        if not path or path == last_path:
            continue
        resolved[index] = path
        last_path = path
    return resolved


def collect_attributions(paths: List[Path]) -> List[Dict[str, Any]]:
    """Attribution records for every stock clip actually used in a render —
    what a compliant video description credits (Pexels requires crediting the
    platform and recommends crediting creators)."""
    seen = set()
    credits = []
    for path in paths:
        meta_file = path.with_suffix(".json")
        if not meta_file.exists() or meta_file in seen:
            continue
        seen.add(meta_file)
        try:
            credits.append(json.loads(meta_file.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
    return credits
