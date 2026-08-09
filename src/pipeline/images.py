import json
import time
import httpx
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


def _poll_izivoice_task(task_id: str, client: httpx.Client) -> Dict[str, Any]:
    """Polls GET /api/tasks/{task_id} until status is 'done' or 'error'/'failed' (or timeout).
    Transient 5xx responses are retried rather than treated as a hard failure, since the
    underlying task may still be processing."""
    elapsed = 0.0
    while elapsed < TASK_POLL_TIMEOUT_SECONDS:
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
    raise TimeoutError(f"Izivoice image task {task_id} did not complete within {TASK_POLL_TIMEOUT_SECONDS}s")


def generate_ai_image(prompt: str, output_path: Path, client: httpx.Client) -> Path:
    """Generates a single 16:9 image via Izivoice's private image API (task-based) and saves it to output_path."""
    model_parameters = json.dumps({"aspect_ratio": "16:9", "resolution": "2K"})
    resp = client.post(
        f"{IZIVOICE_BASE_URL}/images/generate",
        headers=_izivoice_headers(),
        files={
            "prompt": (None, prompt[:4000]),
            "model_id": (None, IZIVOICE_IMAGE_MODEL_ID),
            "generations_count": (None, "1"),
            "model_parameters": (None, model_parameters),
        },
        timeout=30.0
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success") or not data.get("task_id"):
        raise ValueError(f"Unexpected Izivoice generate-image response: {data}")

    task = _poll_izivoice_task(data["task_id"], client)
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
    image_style: Optional[dict] = None
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

    def generate_images(ai_prompts: List[str], prefix: str = "ai_img") -> List[Path]:
        if not IZIVOICE_API_KEY:
            raise RuntimeError("La génération d’images IA n’est pas configurée sur le serveur.")
        logger.info(f"Generating {len(ai_prompts)} images via Izivoice (model={IZIVOICE_IMAGE_MODEL_ID})...")
        generated_paths = []
        failures = 0
        with httpx.Client() as client:
            for i, p in enumerate(ai_prompts):
                img_file = output_dir / f"{prefix}_{i+1}.png"
                full_prompt = f"{p}, {style_prompt}" if style_prompt else p
                try:
                    generate_ai_image(full_prompt, img_file, client)
                    generated_paths.append(img_file)
                except Exception as e:
                    # ai33.pro is a third-party service that has proven unreliable
                    # (slow/erroring under load) — one failed image shouldn't sink
                    # an otherwise-ready render. Use a fallback asset instead.
                    failures += 1
                    logger.warning(f"AI image generation failed for prompt '{p[:60]}': {e}. Using fallback image instead.")
                    fallback = get_image_pool(output_dir, 1, custom_library_path=library_path)
                    if fallback:
                        generated_paths.append(fallback[0])
        if failures:
            logger.warning(f"{failures}/{len(ai_prompts)} AI images fell back to library/synthetic assets due to provider errors.")
        return generated_paths

    if source_type == "ai_generated":
        return generate_images(prompts)

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
