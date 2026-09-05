import random
import math
from pathlib import Path
from typing import List, Optional
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from src.utils.logger import logger
from src.config import ASSETS_PATH, STORAGE_PATH, IMAGE_UPLOAD_EXTENSIONS

def generate_fallback_image(path: Path, index: int, label: str = "KappGen Scene"):
    """
    Generates a stunning, high-definition 1920x1080 artistic illustration using Pillow.
    Features atmospheric landscape silhouettes, glowing celestial lighting, and rich color gradients.
    """
    width, height = 1920, 1080
    img = Image.new("RGB", (width, height), color=(10, 15, 25))
    draw = ImageDraw.Draw(img)
    
    # 5 Rich thematic atmosphere palettes (Stoic Blue, Mystic Sunset, Deep Emerald, Velvet Purple, Golden Hour)
    themes = [
        {"sky_top": (15, 23, 42), "sky_bottom": (88, 28, 135), "accent": (236, 72, 153), "mode": "mountains"},
        {"sky_top": (12, 10, 30), "sky_bottom": (30, 58, 138), "accent": (56, 189, 248), "mode": "temple"},
        {"sky_top": (20, 30, 25), "sky_bottom": (6, 78, 59), "accent": (52, 211, 153), "mode": "forest"},
        {"sky_top": (28, 15, 40), "sky_bottom": (120, 53, 15), "accent": (251, 191, 36), "mode": "sun"},
        {"sky_top": (15, 15, 20), "sky_bottom": (55, 65, 85), "accent": (148, 163, 184), "mode": "geometry"},
    ]
    theme = themes[index % len(themes)]
    c1, c2 = theme["sky_top"], theme["sky_bottom"]
    
    # Render rich gradient sky background
    for y in range(height):
        ratio = y / height
        r = int(c1[0] * (1 - ratio) + c2[0] * ratio)
        g = int(c1[1] * (1 - ratio) + c2[1] * ratio)
        b = int(c1[2] * (1 - ratio) + c2[2] * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
        
    # Draw starry sky / particles
    random.seed(index * 100)
    for _ in range(120):
        sx = random.randint(0, width)
        sy = random.randint(0, int(height * 0.65))
        size = random.randint(1, 3)
        brightness = random.randint(150, 255)
        draw.ellipse([sx, sy, sx + size, sy + size], fill=(brightness, brightness, brightness, 200))

    # Draw glowing sun/moon celestial orb
    ox, oy = width // 2 + (index % 3 - 1) * 300, 320
    for r in range(160, 0, -4):
        alpha = int(40 * (1 - r / 160))
        glow_c = theme["accent"]
        draw.ellipse([ox - r, oy - r, ox + r, oy + r], outline=(glow_c[0], glow_c[1], glow_c[2], alpha), width=3)
    draw.ellipse([ox - 70, oy - 70, ox + 70, oy + 70], fill=(255, 255, 255))

    # Draw thematic silhouette structures (Mountains / Pillars)
    if theme["mode"] in ["mountains", "forest"]:
        # Layer 1 distant mountains
        points = [(0, height)]
        for x in range(0, width + 50, 100):
            my = int(height * 0.55 + math.sin(x * 0.005 + index) * 120 + math.cos(x * 0.01) * 60)
            points.append((x, my))
        points.append((width, height))
        draw.polygon(points, fill=(15, 20, 35))

        # Layer 2 foreground mountain ridge
        fg_points = [(0, height)]
        for x in range(0, width + 50, 80):
            my = int(height * 0.70 + math.sin(x * 0.008 + index * 2) * 80)
            fg_points.append((x, my))
        fg_points.append((width, height))
        draw.polygon(fg_points, fill=(8, 12, 22))

    else: # temple / geometry / sun
        # Draw stoic architectural pillars / horizon
        draw.rectangle([0, int(height * 0.75), width, height], fill=(10, 12, 18))
        # Draw columns
        for px in [300, 500, width - 500, width - 300]:
            draw.rectangle([px - 35, int(height * 0.35), px + 35, int(height * 0.75)], fill=(18, 24, 38))
            draw.rectangle([px - 50, int(height * 0.33), px + 50, int(height * 0.36)], fill=(30, 40, 60))

    # Save final image
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, quality=95)
    logger.info(f"Generated artistic fallback image: {path}")

MIN_LANDSCAPE_RATIO = 1.35  # tolerant lower bound around 16:9 (~1.78) — excludes portrait/square

def _is_landscape_enough(path: Path) -> bool:
    """
    Rejects portrait/square images: forcing them through the 16:9
    scale+crop pipeline zooms in hard and throws away most of the frame,
    producing an ugly, over-cropped result in the final video.
    """
    try:
        with Image.open(path) as img:
            w, h = img.size
        return h > 0 and (w / h) >= MIN_LANDSCAPE_RATIO
    except Exception as e:
        logger.warning(f"Could not read image dimensions for '{path}', excluding it from the pool: {e}")
        return False


def _filter_landscape(paths: List[Path]) -> List[Path]:
    kept = [p for p in paths if _is_landscape_enough(p)]
    dropped = len(paths) - len(kept)
    if dropped:
        logger.info(f"Excluded {dropped} non-16:9 (portrait/square) image(s) from the pool.")
    return kept


def get_image_pool(
    image_dir: Path,
    required_count: int,
    custom_library_path: str = None,
    require_custom_library: bool = False,
    additional_library_dirs: Optional[List[Path]] = None,
    additional_library_files: Optional[List[Path]] = None,
    niche: Optional[str] = None,
) -> List[Path]:
    """
    Retrieves available images from (in priority order): the client-provided local
    image folder (`custom_library_path`, set per-channel in image_style.library_path),
    any `additional_library_dirs` (other channels' own libraries, used for the
    "community" visual source — see fetch_or_generate_images in images.py; read
    live off disk, never copied), the current render's own image dir, or the
    shared assets library. If none have images, generates artistic fallback
    images. Returns a shuffled list of images matching required_count.
    """
    # Was a narrower, separately hand-maintained set than the upload
    # validators (channels.py/videos.py) — missing .gif/.avif/.jfif/etc.
    # meant an image could be accepted at upload time yet silently never
    # picked up here at render time. Now the same shared allowlist everywhere.
    image_extensions = IMAGE_UPLOAD_EXTENSIONS
    existing_images = []

    # Client-uploaded image library (highest priority — this is "their own" library).
    # library_path is stored as a path relative to STORAGE_PATH (e.g.
    # "channels/{id}/library"), set by POST /api/channels/{id}/library-images —
    # it is never a path on the end user's own machine/browser.
    if custom_library_path:
        storage_root = STORAGE_PATH.resolve()
        custom_dir = (storage_root / custom_library_path).resolve()
        is_safe_path = custom_dir == storage_root or storage_root in custom_dir.parents
        if not is_safe_path:
            logger.warning(f"Rejected library_path outside STORAGE_PATH: '{custom_dir}'.")
        elif custom_dir.is_dir():
            custom_images = _filter_landscape([f for f in custom_dir.iterdir() if f.suffix.lower() in image_extensions])
            if custom_images:
                existing_images.extend(custom_images)
            else:
                logger.warning(f"Configured library_path '{custom_dir}' contains no supported 16:9 images ({image_extensions}).")
        elif is_safe_path:
            logger.warning(f"Configured library_path '{custom_dir}' does not exist or is not a directory.")

    for extra_dir in (additional_library_dirs or []):
        if extra_dir.is_dir():
            existing_images.extend(_filter_landscape([f for f in extra_dir.iterdir() if f.suffix.lower() in image_extensions]))
    existing_images.extend(_filter_landscape([
        path for path in (additional_library_files or [])
        if path.is_file() and path.suffix.lower() in image_extensions
    ]))

    if require_custom_library and not existing_images:
        raise ValueError(
            "La bibliothèque d’images de cette chaîne est absente, vide, ou ne contient que des "
            "images qui ne sont pas au format 16:9. Modifiez la chaîne et importez des images "
            "au format paysage (16:9)."
        )

    # Check custom channel image dir
    if image_dir.exists():
        existing_images.extend(_filter_landscape([f for f in image_dir.iterdir() if f.suffix.lower() in image_extensions and not f.name.startswith("subtitled_")]))

    # Check general assets library directory
    library_dir = ASSETS_PATH / "images" / "library"
    if library_dir.exists():
        lib_images = _filter_landscape([f for f in library_dir.iterdir() if f.suffix.lower() in image_extensions])
        existing_images.extend(lib_images)

    # Never silently manufacture the generic KappGen artwork for a production
    # montage. Prefer a previously downloaded real Pexels photo before
    # failing clearly; the caller can then use another real video fallback.
    if not existing_images:
        cached_photo_dir = ASSETS_PATH / "stock_photo_cache"
        if cached_photo_dir.is_dir():
            existing_images.extend(_filter_landscape(list(cached_photo_dir.glob("*.jpg"))))
        # This used to just read whatever this shared, cross-channel cache
        # already happened to contain — often a single leftover photo from
        # an unrelated niche's earlier render, reused verbatim by every
        # completely-unrelated video that ever hit this last resort (the
        # reported "same night-street photo on 3 different channels" bug).
        # Actively search Pexels by the actual niche now, growing a real
        # per-niche cache over time (fetch_stock_photos keeps every distinct
        # result, not just the first) instead of only ever reading whatever
        # was already there.
        if niche and len(existing_images) < required_count:
            try:
                from src.pipeline.stock_video import fetch_stock_photos
                existing_images = list(dict.fromkeys(existing_images + fetch_stock_photos(niche, count=max(required_count, 5))))
            except Exception as exc:
                logger.warning(f"Pexels niche-photo top-up failed for '{niche}': {exc}")
        if not existing_images:
            raise ValueError(
                "Aucun visuel réel disponible : ajoutez des images, activez une source IA gratuite "
                "ou vérifiez l'accès Pexels avant de lancer le rendu."
            )
            
    # Pool & cycle images to fulfill required count. Each cycle is freshly
    # shuffled (never a fixed seed), so the sequence — and therefore the whole
    # montage — is different every render. When the count exceeds the library
    # size and images must repeat, avoid placing the same image back-to-back
    # across a cycle boundary so no two consecutive scenes ever match.
    pool = []
    while len(pool) < required_count:
        shuffled = list(existing_images)
        random.shuffle(shuffled)
        if pool and shuffled and pool[-1] == shuffled[0] and len(shuffled) > 1:
            swap_with = random.randint(1, len(shuffled) - 1)
            shuffled[0], shuffled[swap_with] = shuffled[swap_with], shuffled[0]
        pool.extend(shuffled)

    return pool[:required_count]
