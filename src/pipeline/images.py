import json
import time
import httpx
from pathlib import Path
from typing import List, Optional, Dict, Any
from src.config import AI_IMAGE_PROVIDER_API_KEY, AI_IMAGE_PROVIDER_ENDPOINT, AI_IMAGE_MODEL_ID
from src.utils.logger import logger
from src.pipeline.image_pool import generate_fallback_image, get_image_pool

TASK_POLL_INTERVAL_SECONDS = 3.0
TASK_POLL_TIMEOUT_SECONDS = 180


def _ai33_headers() -> Dict[str, str]:
    return {"xi-api-key": AI_IMAGE_PROVIDER_API_KEY}


def _poll_ai33_task(task_id: str, client: httpx.Client) -> Dict[str, Any]:
    """Polls GET /v1/task/{task_id} until status is 'done' or 'error' (or timeout)."""
    elapsed = 0.0
    while elapsed < TASK_POLL_TIMEOUT_SECONDS:
        resp = client.get(f"{AI_IMAGE_PROVIDER_ENDPOINT}/v1/task/{task_id}", headers=_ai33_headers(), timeout=30.0)
        resp.raise_for_status()
        task = resp.json()
        status = task.get("status")
        if status == "done":
            return task
        if status == "error":
            raise RuntimeError(f"ai33.pro image task {task_id} failed: {task.get('error_message') or task}")
        time.sleep(TASK_POLL_INTERVAL_SECONDS)
        elapsed += TASK_POLL_INTERVAL_SECONDS
    raise TimeoutError(f"ai33.pro image task {task_id} did not complete within {TASK_POLL_TIMEOUT_SECONDS}s")


def generate_ai_image(prompt: str, output_path: Path, client: httpx.Client) -> Path:
    """Generates a single 16:9 image via the ai33.pro API (task-based) and saves it to output_path."""
    model_parameters = json.dumps({"aspect_ratio": "16:9", "resolution": "2K"})
    resp = client.post(
        f"{AI_IMAGE_PROVIDER_ENDPOINT}/v1i/task/generate-image",
        headers=_ai33_headers(),
        files={
            "prompt": (None, prompt[:4000]),
            "model_id": (None, AI_IMAGE_MODEL_ID),
            "generations_count": (None, "1"),
            "model_parameters": (None, model_parameters),
        },
        timeout=30.0
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success") or not data.get("task_id"):
        raise ValueError(f"Unexpected ai33.pro generate-image response: {data}")

    task = _poll_ai33_task(data["task_id"], client)
    result_images = (task.get("metadata") or {}).get("result_images") or []
    if not result_images:
        raise ValueError(f"ai33.pro task {data['task_id']} completed with no result_images: {task}")

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

    if source_type == "ai_generated" and AI_IMAGE_PROVIDER_API_KEY:
        logger.info(f"Generating {len(prompts)} images via ai33.pro (model={AI_IMAGE_MODEL_ID})...")
        generated_paths = []
        with httpx.Client() as client:
            for i, p in enumerate(prompts):
                img_file = output_dir / f"ai_img_{i+1}.png"
                full_prompt = f"{p}, {style_prompt}" if style_prompt else p
                try:
                    generate_ai_image(full_prompt, img_file, client)
                    generated_paths.append(img_file)
                except Exception as e:
                    logger.warning(f"AI image generation failed for prompt '{p}': {e}. Using synthetic fallback.")
                    generate_fallback_image(img_file, i, label=p[:20])
                    generated_paths.append(img_file)
        return generated_paths

    if source_type == "ai_generated" and not AI_IMAGE_PROVIDER_API_KEY:
        logger.info("image_style.source is 'ai_generated' but AI_IMAGE_PROVIDER_API_KEY is not set. Falling back to local library images.")

    # Library mode: use the client-provided local image folder if configured, else the
    # shared assets library / synthetic fallback artwork.
    logger.info(f"Using local image library for {len(prompts)} segments (library_path={library_path or 'none'}).")
    return get_image_pool(output_dir, len(prompts), custom_library_path=library_path)
