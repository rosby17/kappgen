import httpx
from pathlib import Path
from typing import List, Optional
from src.config import AI_IMAGE_PROVIDER_API_KEY, AI_IMAGE_PROVIDER_ENDPOINT
from src.utils.logger import logger
from src.pipeline.image_pool import generate_fallback_image, get_image_pool

def fetch_or_generate_images(
    prompts: List[str],
    output_dir: Path,
    image_style: Optional[dict] = None
) -> List[Path]:
    """
    Fetches AI-generated images or retrieves library fallback images.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_paths = []
    
    source_type = image_style.get("source", "library") if image_style else "library"
    style_prompt = image_style.get("style_prompt", "") if image_style else ""
    
    if source_type == "ai_generated" and AI_IMAGE_PROVIDER_API_KEY and AI_IMAGE_PROVIDER_ENDPOINT:
        logger.info(f"Generating {len(prompts)} images via AI provider...")
        for i, p in enumerate(prompts):
            img_file = output_dir / f"ai_img_{i+1}.png"
            full_prompt = f"{p}, {style_prompt}" if style_prompt else p
            try:
                response = httpx.post(
                    AI_IMAGE_PROVIDER_ENDPOINT,
                    headers={"Authorization": f"Bearer {AI_IMAGE_PROVIDER_API_KEY}"},
                    json={"prompt": full_prompt, "size": "1920x1080"},
                    timeout=30.0
                )
                response.raise_for_status()
                data = response.json()
                if "url" in data:
                    img_resp = httpx.get(data["url"])
                    img_file.write_bytes(img_resp.content)
                elif "b64" in data:
                    import base64
                    img_file.write_bytes(base64.b64decode(data["b64"]))
                generated_paths.append(img_file)
            except Exception as e:
                logger.warning(f"AI image generation failed for prompt '{p}': {e}. Using synthetic fallback.")
                generate_fallback_image(img_file, i, label=p[:20])
                generated_paths.append(img_file)
        return generated_paths
        
    # Default library / fallback mode
    logger.info(f"Using image library / synthetic image pool for {len(prompts)} segments.")
    return get_image_pool(output_dir, len(prompts))
