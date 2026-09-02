import base64
import json
import time
import random
import httpx
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Dict, Any
from src.config import IZIVOICE_API_KEY, IZIVOICE_BASE_URL, FAL_API_KEY, HUGGINGFACE_API_KEYS, IMAGE_UPLOAD_EXTENSIONS
from src.utils.logger import logger

# Image models often invent misspelled copy when a scene mentions a concept,
# a book, a sign, a storefront, or a brand. Keep this constraint centralized
# so it is applied even when the scene-director step fails and raw narration is
# sent straight to a provider.
TEXT_FREE_IMAGE_RULE = (
    "pure visual scene only, absolutely no text, no words, no letters, no numbers, "
    "no captions, no titles, no slogans, no labels, no signs, no posters, no banners, "
    "no book or document writing, no interface text, no typography, no logos, "
    "no signatures, no watermarks, no writing of any kind anywhere in the image"
)


def text_free_image_prompt(prompt: str) -> str:
    """Append the non-negotiable no-writing rule without exceeding API limits."""
    if "[[ALLOW_TEXT]]" in (prompt or ""):
        return (prompt or "").replace("[[ALLOW_TEXT]]", "").strip()[:4000]
    suffix = f", {TEXT_FREE_IMAGE_RULE}"
    return f"{(prompt or '').strip()[:4000 - len(suffix)]}{suffix}"
from src.utils.cost_tracking import log_usage, estimate_image_cost
from src.pipeline.image_pool import get_image_pool

TASK_POLL_INTERVAL_SECONDS = 3.0
TASK_POLL_TIMEOUT_SECONDS = 90  # fail fast to a fallback image rather than stalling the whole render
# Izivoice exposes OpenAI's GPT Image 2 directly. Use it for the Izivoice
# path so thumbnail generations do not silently run through Seedream.
IZIVOICE_IMAGE_MODEL_ID = "gpt-image-2"


def _izivoice_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {IZIVOICE_API_KEY}"}


def _approved_community_library_files(niche: Optional[str]) -> List[Path]:
    """Every other channel's own image library that's been opted into the
    community-sharing program (Channel.image_style.share_with_community) and
    approved by an admin for this niche — read live off disk, never copied.
    Used both by the explicit "community" visual source (a creator's own
    deliberate choice) and, as of the free-fallback chain below, as the
    imagery-quality safety net when free AI generation itself fails: real,
    niche-relevant photos from other creators beat generic synthetic
    gradient artwork with zero niche relevance."""
    if not niche:
        return []
    from src.db.session import SessionLocal
    from src.db.models import CommunityLibraryFolder, CommunityLibraryImagePlacement
    from src.config import STORAGE_PATH
    db = SessionLocal()
    try:
        folders = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.status == "approved").all()
        channel_ids = [folder.channel_id for folder in folders]
        placements = {
            (row.channel_id, row.filename): row.niche
            for row in db.query(CommunityLibraryImagePlacement).filter(
                CommunityLibraryImagePlacement.channel_id.in_(channel_ids)
            ).all()
        } if channel_ids else {}
        files: List[Path] = []
        extensions = IMAGE_UPLOAD_EXTENSIONS
        for folder in folders:
            library_dir = STORAGE_PATH / "channels" / folder.channel_id / "library"
            if not library_dir.is_dir():
                continue
            files.extend(
                path for path in library_dir.iterdir()
                if path.is_file()
                and path.suffix.lower() in extensions
                and placements.get((folder.channel_id, path.name), folder.niche).casefold() == niche.casefold()
            )
        return files
    finally:
        db.close()


IMAGE_SOURCE_PRIORITY = ["ai_generated", "library", "community"]


def resolve_enabled_image_sources(image_style: Optional[dict]) -> List[str]:
    """Returns the visual sources a channel wants, in the fixed priority
    order they're actually tried at render time: AI generation first (its
    own identity, generated fresh), then the channel's own local library if
    it has one, then the niche's community library last. A creator can now
    enable any combination — no longer one exclusive mode — so a channel set
    up for "AI + community" genuinely tries AI for every scene and only
    drops to the shared niche pool on a real failure, never touching a local
    library it doesn't have.

    `image_style.sources` (a list) is the current shape; `image_style.source`
    (a single string) is the old one, translated here for every channel
    saved before this existed — "hybrid" becomes AI-with-library-fallback
    (replacing the old fixed 50/50 split, which fought the same "AI is
    unreliable sometimes, fall back gracefully" goal this whole priority
    chain exists for), everything else maps to itself."""
    if not image_style:
        return ["library"]
    sources = image_style.get("sources")
    if isinstance(sources, list) and sources:
        enabled = [s for s in IMAGE_SOURCE_PRIORITY if s in sources]
        if enabled:
            return enabled
    legacy = image_style.get("source", "library")
    if legacy == "hybrid":
        return ["ai_generated", "library"]
    if legacy in IMAGE_SOURCE_PRIORITY:
        return [legacy]
    return ["library"]


def _persist_generated_images_to_channel_library(
    channel_id: Optional[str], user_id: Optional[str], niche: Optional[str], image_paths: List[Path],
) -> None:
    """Copies every freshly AI-generated scene image into the channel's own
    persistent library folder (channels/{id}/library — the same directory a
    manual library upload writes to) and keeps its CommunityLibraryFolder row
    in sync, growing that niche's shared pool automatically as a side effect
    of ordinary rendering — by default, for every channel, no opt-in
    checkbox. The intent: the more videos get made, the bigger and more
    varied each niche's free image pool gets, so new videos increasingly
    find what they need already sitting there instead of calling any
    generator at all.

    Auto-approved on first creation — unlike a creator's manually curated
    upload (which starts "pending" for admin review), these are already
    AI-generated stock-style scene art, not personal content, so gating
    every single one behind a review queue would defeat the actual point
    (an ever-growing pool that needs zero manual upkeep to work). Never
    touches the status of a folder that already exists, whatever it is
    (approved/pending/flagged) — this only ever adds images and bumps the
    count, exactly like the manual-upload sync path.

    Only ever copies — the source video's own working copy (used for that
    video's own post-render editing) is untouched and follows its own
    retention/purge schedule independently."""
    if not channel_id or not image_paths:
        return
    import shutil
    from src.config import STORAGE_PATH
    from src.db.session import SessionLocal
    from src.db.models import CommunityLibraryFolder

    library_dir = STORAGE_PATH / "channels" / channel_id / "library"
    try:
        library_dir.mkdir(parents=True, exist_ok=True)
        existing_count = len([f for f in library_dir.iterdir() if f.is_file()])
        copied = 0
        for src in image_paths:
            try:
                dest = library_dir / f"generated_{existing_count + copied + 1}{src.suffix or '.png'}"
                shutil.copy2(src, dest)
                copied += 1
            except OSError as e:
                logger.warning(f"Could not persist generated image '{src}' into channel library: {e}")
        if copied == 0:
            return
        total = existing_count + copied
    except OSError as e:
        logger.warning(f"Could not access channel library dir for {channel_id}, skipping auto-share: {e}")
        return

    db = SessionLocal()
    try:
        folder = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.channel_id == channel_id).first()
        if folder:
            folder.image_count = total
            folder.niche = niche or folder.niche
        else:
            db.add(CommunityLibraryFolder(
                channel_id=channel_id,
                user_id=user_id,
                niche=niche or "General",
                image_count=total,
                status="approved",
            ))
        db.commit()
    except Exception as e:
        # Confirmed live: this has silently failed for at least one channel
        # (53 real generated_*.png files on disk, zero DB record of any of
        # them) — most likely two renders' background threads racing to
        # INSERT this channel's very first folder row at once, the loser's
        # commit failing here and just... never being retried. The image
        # files themselves are already safely copied above regardless (that
        # part isn't in this try), so on any failure here — a race or
        # anything else — fall back to a fresh query-and-update instead of
        # only logging and moving on: if the row now exists (the other
        # thread's insert landed first), update its count; still failing
        # after that is genuinely unexpected and worth an ERROR log, not a
        # warning that reads as routine.
        db.rollback()
        try:
            folder = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.channel_id == channel_id).first()
            if folder:
                folder.image_count = total
                folder.niche = niche or folder.niche
                db.commit()
            else:
                logger.error(f"Could not sync CommunityLibraryFolder for channel {channel_id} (no existing row to fall back to): {e}")
        except Exception as retry_exc:
            logger.error(f"CommunityLibraryFolder sync retry also failed for channel {channel_id}: {retry_exc}")
    finally:
        db.close()


def _poll_izivoice_task(task_id: str, client: httpx.Client, timeout_seconds: float = TASK_POLL_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Polls GET /api/tasks/{task_id} until status is 'done' or 'error'/'failed' (or timeout).
    Transient 5xx responses are retried rather than treated as a hard failure, since the
    underlying task may still be processing."""
    elapsed = 0.0
    while elapsed < timeout_seconds:
        try:
            resp = client.get(f"{IZIVOICE_BASE_URL}/tasks/{task_id}", headers=_izivoice_headers(), timeout=30.0)
            if resp.status_code >= 500:
                logger.warning(f"Izivoice image poll for task {task_id} returned {resp.status_code}, retrying...")
                time.sleep(TASK_POLL_INTERVAL_SECONDS)
                elapsed += TASK_POLL_INTERVAL_SECONDS
                continue
            resp.raise_for_status()
            task = resp.json()
        except httpx.TransportError as e:
            logger.warning(f"Izivoice image poll for task {task_id} transport error ({e}), retrying...")
            time.sleep(TASK_POLL_INTERVAL_SECONDS)
            elapsed += TASK_POLL_INTERVAL_SECONDS
            continue
        status = task.get("status")
        if status == "done":
            return task
        if status in ("error", "failed"):
            raise RuntimeError(f"Izivoice image task {task_id} failed: {task.get('error_message') or task}")
        time.sleep(TASK_POLL_INTERVAL_SECONDS)
        elapsed += TASK_POLL_INTERVAL_SECONDS
    raise TimeoutError(f"Izivoice image task {task_id} did not complete within {timeout_seconds}s")


def _submit_izivoice_image_task(prompt: str, client: httpx.Client, reference_image_paths: Optional[List[Path]] = None) -> dict:
    model_parameters = json.dumps({"aspect_ratio": "16:9", "resolution": "2K"})
    fields = {
        "prompt": (None, text_free_image_prompt(prompt)),
        "model_id": (None, IZIVOICE_IMAGE_MODEL_ID),
        "generations_count": (None, "1"),
        "model_parameters": (None, model_parameters),
    }
    # bytedance-seedream-4.5 (the model Izivoice proxies here) natively supports
    # up to 14 reference images for style/character-conditioned generation —
    # Izivoice's own request schema for this isn't publicly documented, so this
    # is a best-effort attempt at the conventional "reference_images" multipart
    # field name. Callers must be ready to retry without it on a 4xx.
    open_files = []
    parts = list(fields.items())
    if reference_image_paths:
        for path in reference_image_paths[:14]:
            if not path.exists():
                continue
            fh = open(path, "rb")
            open_files.append(fh)
            parts.append(("reference_images", (path.name, fh, "image/jpeg")))
    try:
        resp = client.post(
            f"{IZIVOICE_BASE_URL}/images/generate",
            headers=_izivoice_headers(),
            files=parts,
            timeout=30.0
        )
    finally:
        for fh in open_files:
            fh.close()
    resp.raise_for_status()
    return resp.json()


def generate_ai_image(prompt: str, output_path: Path, client: httpx.Client, poll_timeout_seconds: float = TASK_POLL_TIMEOUT_SECONDS, reference_image_paths: Optional[List[Path]] = None) -> Path:
    """Generates a single 16:9 image via Izivoice's private image API (task-based) and saves it to output_path.

    When reference_image_paths is given, attempts style/character-conditioned
    generation first (see _submit_izivoice_image_task); if Izivoice rejects
    that request shape (4xx), retries once as a plain text-only prompt rather
    than failing the whole image — the caller's own broader failure fallback
    (e.g. a video frame grab for thumbnails) is reserved for when both fail."""
    try:
        data = _submit_izivoice_image_task(prompt, client, reference_image_paths)
    except httpx.HTTPStatusError as exc:
        if reference_image_paths and exc.response is not None and exc.response.status_code < 500:
            logger.warning(f"Izivoice rejected reference-image-conditioned request ({exc.response.status_code}), retrying with text-only prompt: {exc}")
            data = _submit_izivoice_image_task(prompt, client, reference_image_paths=None)
        else:
            raise
    if not data.get("success") or not data.get("task_id"):
        raise ValueError(f"Unexpected Izivoice generate-image response: {data}")

    task = _poll_izivoice_task(data["task_id"], client, timeout_seconds=poll_timeout_seconds)
    result_images = (task.get("metadata") or {}).get("result_images") or []
    if not result_images:
        raise ValueError(f"Izivoice image task {data['task_id']} completed with no result_images: {task}")

    image_url = result_images[0]["imageUrl"]
    img_resp = client.get(image_url, timeout=60.0)
    img_resp.raise_for_status()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(img_resp.content)
    # Keep the provider provenance beside the downloaded asset so a later
    # regeneration never loses the Easy Voice/Izivoice task and source URL.
    try:
        from datetime import datetime, timezone
        output_path.with_suffix('.meta.json').write_text(json.dumps({
            "provider": "izivoice", "model": IZIVOICE_IMAGE_MODEL_ID,
            "task_id": data.get("task_id"), "source_url": image_url,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not persist Izivoice thumbnail provenance: %s", exc)
    return output_path


def _generate_with_fal_gpt_image_2(prompt: str, output_path: Path, client: httpx.Client, reference_image_paths: Optional[List[Path]] = None) -> Path:
    """Generates a thumbnail via fal.ai's hosted OpenAI gpt-image-2 — reference
    images are passed as real conditioning inputs (image_urls, data URIs),
    not just a text description, for actual visual resemblance to the
    creator's uploaded style references."""
    if not FAL_API_KEY:
        raise RuntimeError("FAL_API_KEY is not configured on the server.")

    if reference_image_paths:
        endpoint = "openai/gpt-image-2/edit"
        image_urls = []
        for path in reference_image_paths[:16]:
            if not path.exists():
                continue
            media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            image_urls.append(f"data:{media_type};base64,{base64.standard_b64encode(path.read_bytes()).decode('utf-8')}")
        payload = {"prompt": text_free_image_prompt(prompt), "image_urls": image_urls, "image_size": "landscape_16_9"}
    else:
        endpoint = "openai/gpt-image-2"
        payload = {"prompt": text_free_image_prompt(prompt), "image_size": "landscape_16_9"}

    resp = client.post(
        f"https://fal.run/{endpoint}",
        headers={"Authorization": f"Key {FAL_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=120.0,
    )
    resp.raise_for_status()
    images = (resp.json() or {}).get("images") or []
    if not images or not images[0].get("url"):
        raise ValueError(f"fal.ai gpt-image-2 returned no image: {resp.json()}")

    img_resp = client.get(images[0]["url"], timeout=60.0)
    img_resp.raise_for_status()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(img_resp.content)
    log_usage("fal_image", "thumbnail", 1, "images", estimate_image_cost(1), meta={"model": endpoint})
    return output_path


def _hf_accounts_from_db() -> List[Any]:
    """Admin-managed pool (src/api/routes/admin.py's hf-accounts routes) —
    ordered by last_used_at ascending (nulls first) so load spreads evenly
    across accounts instead of hammering whichever sorts first. Falls back to
    the static HUGGINGFACE_API_KEYS env list (wrapped as plain dicts, no id)
    only if the table is empty, so an existing single-env-var deployment
    keeps working before anyone's added an account via the admin UI."""
    from src.db.session import SessionLocal
    from src.db.models import HuggingFaceAccount
    db = SessionLocal()
    try:
        rows = (
            db.query(HuggingFaceAccount)
            .filter(HuggingFaceAccount.is_enabled == True)  # noqa: E712
            .order_by(HuggingFaceAccount.last_used_at.asc().nullsfirst())
            .all()
        )
        if rows:
            return [{"id": r.id, "token": r.token} for r in rows]
    finally:
        db.close()
    return [{"id": None, "token": key} for key in HUGGINGFACE_API_KEYS]


def _mark_hf_account(account_id: Optional[str], status: str, error: Optional[str] = None) -> None:
    """Best-effort status update after a real attempt — never allowed to
    fail the actual generation it's tracking (same fail-open convention as
    cost_tracking.log_usage)."""
    if not account_id:
        return
    try:
        from src.db.session import SessionLocal
        from src.db.models import HuggingFaceAccount
        from datetime import datetime
        db = SessionLocal()
        try:
            account = db.query(HuggingFaceAccount).filter(HuggingFaceAccount.id == account_id).first()
            if account:
                account.status = status
                account.last_used_at = datetime.utcnow()
                # Clear the stale error the moment an account works again —
                # leaving a months-old "402 Payment Required" visible under a
                # green "Actif" badge (which is what an admin actually sees)
                # reads as "this is broken right now" when it isn't.
                account.last_error = error if status != "active" else None
                db.commit()
        finally:
            db.close()
    except Exception:
        pass


def _generate_with_huggingface_flux(prompt: str, output_path: Path, client: httpx.Client, size: str = "1280x720", operation: str = "image") -> Path:
    """Free-tier image generation: FLUX.1-schnell (open-source, Apache 2.0)
    routed through Hugging Face's Inference Providers to nscale — costs
    nothing up to each account's small monthly free credit, so this is tried
    before any paid provider. No image-conditioning support (text-to-image
    only), so callers with reference images must skip this and go straight
    to a provider that supports it (fal.ai gpt-image-2 or Izivoice).

    Tries every enabled account in the admin-managed pool (least-recently-used
    first) so one account being quota-capped (429) doesn't block the others
    still having free credit left."""
    accounts = _hf_accounts_from_db()
    if not accounts:
        raise RuntimeError("No Hugging Face account configured (add one in the admin panel, or set HUGGINGFACE_API_KEYS).")

    last_exc = None
    for account in accounts:
        try:
            resp = client.post(
                "https://router.huggingface.co/nscale/v1/images/generations",
                headers={"Authorization": f"Bearer {account['token']}", "Content-Type": "application/json"},
                json={"prompt": text_free_image_prompt(prompt), "model": "black-forest-labs/FLUX.1-schnell", "size": size},
                timeout=90.0,
            )
            resp.raise_for_status()
            data = (resp.json() or {}).get("data") or []
            b64 = data[0].get("b64_json") if data else None
            if not b64:
                raise ValueError(f"Hugging Face (nscale/FLUX.1-schnell) returned no image: {resp.json()}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(base64.b64decode(b64))
            log_usage("huggingface_image", operation, 1, "images", 0.0, meta={"model": "black-forest-labs/FLUX.1-schnell (nscale, free tier)", "hf_account_id": account["id"]})
            _mark_hf_account(account["id"], "active")
            return output_path
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            if exc.response is not None and exc.response.status_code in (401, 402, 429):
                logger.warning(f"Hugging Face account {account['id'] or '(env)'} exhausted/rejected ({exc.response.status_code}), trying next account...")
                status = "invalid" if exc.response.status_code == 401 else "quota_exhausted"
                _mark_hf_account(account["id"], status, str(exc)[:300])
                continue
            _mark_hf_account(account["id"], "invalid", str(exc)[:300])
            raise
        except Exception as exc:
            last_exc = exc
            _mark_hf_account(account["id"], "invalid", str(exc)[:300])
            continue
    raise RuntimeError(f"All {len(accounts)} Hugging Face account(s) failed. Last error: {last_exc}")


def generate_thumbnail_image(
    prompt: str,
    output_path: Path,
    client: httpx.Client,
    reference_image_paths: Optional[List[Path]] = None,
    provider_order: Optional[List[str]] = None,
) -> Path:
    """Thumbnail-specific image generation, trying providers in
    `provider_order` (admin-controlled — see src/utils/app_settings.py's
    thumbnail_provider_order, set from the "Ressources" tab) and falling
    through on any error — a missing/invalid key, an exhausted free quota, a
    402/429, or anything else. Defaults to Hugging Face alone (free, no
    provider left out generates any cost) if no order is given.

    Hugging Face's FLUX.1-schnell endpoint is text-to-image only, so it's
    skipped whenever reference images are given to condition on — those
    need fal.ai's gpt-image-2 (best fidelity to a reference) or Izivoice.
    If every provider in the order is skipped or fails, raises — the caller
    (youtube_metadata.py) then falls through to its own video-frame-grab
    fallback, same as the per-scene body images already do, so a thumbnail
    never silently costs money beyond what the admin explicitly opted into."""
    order = [p for p in (provider_order or ["huggingface"]) if not (p == "huggingface" and reference_image_paths)]
    funcs = {
        "huggingface": lambda: _generate_with_huggingface_flux(prompt, output_path, client, operation="thumbnail"),
        "fal": lambda: _generate_with_fal_gpt_image_2(prompt, output_path, client, reference_image_paths),
        "izivoice": lambda: generate_ai_image(prompt, output_path, client, reference_image_paths=reference_image_paths),
    }
    labels = {"huggingface": "Hugging Face (FLUX.1-schnell)", "fal": "fal.ai (gpt-image-2)", "izivoice": "Izivoice"}
    last_exc: Optional[Exception] = None
    for name in order:
        try:
            return funcs[name]()
        except Exception as exc:
            logger.warning(f"{labels.get(name, name)} thumbnail generation failed, trying next provider: {exc}")
            last_exc = exc
    raise RuntimeError(f"All thumbnail providers in the configured order failed or were skipped. Last error: {last_exc}")


def fetch_or_generate_images(
    prompts: List[str],
    output_dir: Path,
    image_style: Optional[dict] = None,
    unique_generation_count: Optional[int] = None,
    user_id: Optional[str] = None,
    niche: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> List[Path]:
    """
    Fetches images for each scene. With exactly one visual source enabled,
    that source is used directly, falling through to the next in priority
    order (see resolve_enabled_image_sources) only on an actual
    failure/shortage. With two or more enabled, each scene is randomly
    assigned one of them (roughly evenly split, reshuffled) instead of
    treating the others as pure emergency fallback — a real mix of AI,
    local, and community imagery throughout the video, not "AI unless it
    breaks."
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    enabled = resolve_enabled_image_sources(image_style)
    style_prompt = image_style.get("style_prompt", "") if image_style else ""
    library_path = image_style.get("library_path") if image_style else None

    def expand_randomly(unique_images: List[Path], required_count: int) -> List[Path]:
        """Fill a long timeline from a per-video original pool without obvious
        adjacent repeats. Every cycle is reshuffled, so two videos with the
        same duration still receive a different illustration sequence."""
        if not unique_images:
            return []
        result = list(unique_images[:required_count])
        while len(result) < required_count:
            cycle = list(unique_images)
            random.shuffle(cycle)
            if result and len(cycle) > 1 and cycle[0] == result[-1]:
                swap_index = random.randrange(1, len(cycle))
                cycle[0], cycle[swap_index] = cycle[swap_index], cycle[0]
            result.extend(cycle)
        return result[:required_count]

    def generate_images(ai_prompts: List[str], prefix: str = "ai_img") -> List[Path]:
        logger.info(f"Generating {len(ai_prompts)} images (free tier: Hugging Face FLUX.1-schnell)...")
        # These are independent network calls (each waits on Izivoice's API, not
        # local CPU), so running them one after another was pure dead time —
        # fanning them out is the single biggest lever for a long video's total
        # render time, since a 1h video can mean 150+ sequential image requests.
        results: List[Optional[Path]] = [None] * len(ai_prompts)
        # Only a genuinely fresh HF success gets auto-shared into the channel's
        # library below — never a disk-cache hit from an earlier attempt on
        # this same video (already handled once) nor a library/community/
        # synthetic fallback (that would just copy other creators' — or
        # nobody's — images right back into this channel's own folder).
        fresh_paths: List[Path] = []
        failures = 0

        def fetch_one(i: int, p: str, client: httpx.Client) -> Optional[Path]:
            img_file = output_dir / f"{prefix}_{i+1}.png"
            # output_dir is deterministic per video (channels/{id}/videos/{id}/source/images),
            # so retrying a video that already generated some images before
            # failing at a later step (subtitles, mixing, ...) would otherwise
            # regenerate every single one from scratch — free, but still real
            # time and a real HTTP call each. Reuse whatever's already on disk.
            if img_file.exists():
                return img_file, False
            # Niche is appended directly here as a safety net: build_scene_prompts
            # (the Claude step that's supposed to bake niche-relevant subject
            # matter into each prompt) can fail or be skipped, in which case
            # raw narration text reaches this function instead — without this,
            # that raw text goes straight to the image model with no visual
            # grounding in the channel's actual topic (e.g. a health-niche
            # script producing generic "person talking" imagery instead of
            # doctors/hospitals/medicine).
            full_prompt = f"{p}, {style_prompt}" if style_prompt else p
            if niche:
                full_prompt = f"{full_prompt}, in the visual context of {niche}"
            # Free tier tried first, before any credit is touched — costs
            # nothing up to Hugging Face's small monthly free allowance, so a
            # video's whole visual pool can render for free as long as it lasts.
            try:
                return _generate_with_huggingface_flux(full_prompt, img_file, client, operation="scene_image"), True
            except Exception as e:
                # Paid fallback (Izivoice) intentionally disabled — free tier
                # only, permanently: never spend a credit just because the
                # free generator had a bad moment. Falls back through the
                # channel's own library first (only if the creator actually
                # enabled it — see `enabled` above), then the niche's
                # community library (same condition), and only then
                # get_image_pool's own last-resort synthetic gradient
                # artwork if truly nothing else is enabled/available.
                logger.warning(f"Hugging Face (FLUX.1-schnell) image generation failed, falling back to library images: {e}")
                fallback = get_image_pool(
                    output_dir, 1,
                    custom_library_path=library_path if "library" in enabled else None,
                    additional_library_files=_approved_community_library_files(niche) if "community" in enabled else [],
                )
                return (fallback[0] if fallback else None), False

        with httpx.Client(limits=httpx.Limits(max_connections=8, max_keepalive_connections=8)) as client:
            with ThreadPoolExecutor(max_workers=6) as pool:
                future_to_index = {
                    pool.submit(fetch_one, i, p, client): i for i, p in enumerate(ai_prompts)
                }
                for future in future_to_index:
                    i = future_to_index[future]
                    result, is_fresh = future.result()
                    results[i] = result
                    if result is None:
                        failures += 1
                    elif is_fresh:
                        fresh_paths.append(result)

        generated_paths = [r for r in results if r is not None]
        if failures:
            logger.warning(f"{failures}/{len(ai_prompts)} AI images fell back to library/synthetic assets due to provider errors.")
        if fresh_paths:
            _persist_generated_images_to_channel_library(channel_id, user_id, niche, fresh_paths)
        return generated_paths

    if len(enabled) > 1:
        # Two or more sources enabled — genuinely mix them instead of
        # treating everything past the first as pure emergency fallback.
        # Each scene is randomly assigned one enabled source, reshuffled
        # every "round" so the split stays roughly even across the whole
        # video (e.g. 3 sources, 30 scenes -> ~10 scenes each, in random
        # order, never the same source three times running by design of
        # the shuffle-per-round below... though a run of the same source
        # can still happen by chance, same as shuffling any deck).
        assignment: List[str] = []
        while len(assignment) < len(prompts):
            round_sources = list(enabled)
            random.shuffle(round_sources)
            assignment.extend(round_sources)
        assignment = assignment[:len(prompts)]
        logger.info(f"Mixing visual sources ({', '.join(enabled)}) across {len(prompts)} scene(s): " + ", ".join(f"{assignment.count(s)}× {s}" for s in enabled))

        results: List[Optional[Path]] = [None] * len(prompts)

        if "ai_generated" in enabled:
            ai_indices = [i for i, s in enumerate(assignment) if s == "ai_generated"]
            if ai_indices:
                ai_prompts = [prompts[i] for i in ai_indices]
                generation_count = min(len(ai_prompts), unique_generation_count or len(ai_prompts))
                ai_originals = generate_images(ai_prompts[:generation_count])
                ai_sequence = expand_randomly(ai_originals, len(ai_indices))
                for pos, i in enumerate(ai_indices):
                    if pos < len(ai_sequence):
                        results[i] = ai_sequence[pos]

        # Everything not filled by AI (library/community-assigned scenes,
        # plus any scene where AI generation itself failed) is drawn from
        # one combined pool of whichever of library/community are enabled —
        # get_image_pool already merges and shuffles both together, with
        # synthetic art as the final safety net if truly nothing is available.
        still_needed = sum(1 for r in results if r is None)
        if still_needed:
            pool = iter(get_image_pool(
                output_dir, still_needed,
                custom_library_path=library_path if "library" in enabled else None,
                additional_library_files=_approved_community_library_files(niche) if "community" in enabled else [],
            ))
            for i, r in enumerate(results):
                if r is None:
                    results[i] = next(pool, None)

        return [r for r in results if r is not None]

    if "ai_generated" in enabled:
        # Generate an original visual pool only for the opening window (10 min
        # by default, calculated by the orchestrator), then reuse that video's
        # own pool in a fresh random order. This caps image credits for a 1-hour
        # video at the same cost as a 10-minute one while keeping each video's
        # visual identity original. Any image this fails to generate falls
        # through per-image to library/community/synthetic inside fetch_one
        # above, according to the same `enabled` priority.
        generation_count = min(len(prompts), unique_generation_count or len(prompts))
        originals = generate_images(prompts[:generation_count])
        sequence = expand_randomly(originals, len(prompts))
        reused = max(0, len(sequence) - len(originals))
        logger.info(
            f"AI visual budget: generated {len(originals)} original image(s), "
            f"reused them across {reused} later scene(s)."
        )
        return sequence

    # No AI: pull straight from whichever of library/community are enabled,
    # in that priority order — get_image_pool already tries custom_library_path
    # before additional_library_files, then finally its own synthetic
    # fallback art if genuinely nothing is available from either (never a
    # hard failure; validate_channel_visual_source in videos.py is the real
    # pre-flight gate that stops a channel with nothing at all configured
    # from ever reaching this point).
    community_files = _approved_community_library_files(niche) if "community" in enabled else []
    logger.info(
        f"No AI generation enabled — using {'library' if 'library' in enabled else ''}"
        f"{' + ' if 'library' in enabled and 'community' in enabled else ''}"
        f"{f'community ({len(community_files)} image(s))' if 'community' in enabled else ''} for {len(prompts)} segment(s)."
    )
    return get_image_pool(
        output_dir,
        len(prompts),
        custom_library_path=library_path if "library" in enabled else None,
        additional_library_files=community_files,
    )
