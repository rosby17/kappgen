import json
import time
import random
import httpx
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Dict, Any
from src.config import IZIVOICE_API_KEY, IZIVOICE_BASE_URL
from src.utils.logger import logger
from src.pipeline.image_pool import get_image_pool

TASK_POLL_INTERVAL_SECONDS = 3.0
TASK_POLL_TIMEOUT_SECONDS = 90  # fail fast to a fallback image rather than stalling the whole render
IZIVOICE_IMAGE_MODEL_ID = "bytedance-seedream-4.5"


def _izivoice_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {IZIVOICE_API_KEY}"}


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
        "prompt": (None, prompt[:4000]),
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
    return output_path


def fetch_or_generate_images(
    prompts: List[str],
    output_dir: Path,
    image_style: Optional[dict] = None,
    unique_generation_count: Optional[int] = None,
) -> List[Path]:
    """
    Fetches images for each scene: either generated via the ai33.pro AI image API
    ("ai_generated") or picked from a local image folder / library ("library" —
    default, uses image_style.library_path if the client provided their own asset folder).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    source_type = image_style.get("source", "library") if image_style else "library"
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
        if not IZIVOICE_API_KEY:
            raise RuntimeError("La génération d’images IA n’est pas configurée sur le serveur.")
        logger.info(f"Generating {len(ai_prompts)} images via Izivoice (model={IZIVOICE_IMAGE_MODEL_ID})...")
        # These are independent network calls (each waits on Izivoice's API, not
        # local CPU), so running them one after another was pure dead time —
        # fanning them out is the single biggest lever for a long video's total
        # render time, since a 1h video can mean 150+ sequential image requests.
        results: List[Optional[Path]] = [None] * len(ai_prompts)
        failures = 0

        def fetch_one(i: int, p: str, client: httpx.Client) -> Optional[Path]:
            img_file = output_dir / f"{prefix}_{i+1}.png"
            full_prompt = f"{p}, {style_prompt}" if style_prompt else p
            try:
                generate_ai_image(full_prompt, img_file, client)
                return img_file
            except Exception as e:
                # ai33.pro is a third-party service that has proven unreliable
                # (slow/erroring under load) — one failed image shouldn't sink
                # an otherwise-ready render. Use a fallback asset instead.
                logger.warning(f"AI image generation failed for prompt '{p[:60]}': {e}. Using fallback image instead.")
                fallback = get_image_pool(output_dir, 1, custom_library_path=library_path)
                return fallback[0] if fallback else None

        with httpx.Client(limits=httpx.Limits(max_connections=8, max_keepalive_connections=8)) as client:
            with ThreadPoolExecutor(max_workers=6) as pool:
                future_to_index = {
                    pool.submit(fetch_one, i, p, client): i for i, p in enumerate(ai_prompts)
                }
                for future in future_to_index:
                    i = future_to_index[future]
                    result = future.result()
                    results[i] = result
                    if result is None:
                        failures += 1

        generated_paths = [r for r in results if r is not None]
        if failures:
            logger.warning(f"{failures}/{len(ai_prompts)} AI images fell back to library/synthetic assets due to provider errors.")
        return generated_paths

    if source_type == "ai_generated":
        # Generate an original visual pool only for the opening window (10 min
        # by default, calculated by the orchestrator), then reuse that video's
        # own pool in a fresh random order. This caps image credits for a 1-hour
        # video at the same cost as a 10-minute one while keeping each video's
        # visual identity original.
        generation_count = min(len(prompts), unique_generation_count or len(prompts))
        originals = generate_images(prompts[:generation_count])
        sequence = expand_randomly(originals, len(prompts))
        reused = max(0, len(sequence) - len(originals))
        logger.info(
            f"AI visual budget: generated {len(originals)} original image(s), "
            f"reused them across {reused} later scene(s)."
        )
        return sequence

    if source_type == "hybrid":
        local_count = (len(prompts) + 1) // 2
        local_images = get_image_pool(
            output_dir,
            local_count,
            custom_library_path=library_path,
            require_custom_library=True,
        )
        ai_images = generate_images(prompts[local_count:], prefix="hybrid_ai")
        combined = []
        for index in range(max(len(local_images), len(ai_images))):
            if index < len(local_images):
                combined.append(local_images[index])
            if index < len(ai_images):
                combined.append(ai_images[index])
        logger.info(f"Hybrid image mode prepared {len(local_images)} library and {len(ai_images)} AI images.")
        return combined[:len(prompts)]

    if source_type != "library":
        raise ValueError(f"Mode d’images inconnu: {source_type}")

    # Library mode: use the client-provided local image folder if configured, else the
    # shared assets library / synthetic fallback artwork.
    logger.info(f"Using local image library for {len(prompts)} segments (library_path={library_path or 'none'}).")
    return get_image_pool(
        output_dir,
        len(prompts),
        custom_library_path=library_path,
        require_custom_library=True,
    )
