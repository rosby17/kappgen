import random
import math
from pathlib import Path
from typing import List
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from src.utils.logger import logger
from src.config import ASSETS_PATH

def generate_fallback_image(path: Path, index: int, label: str = "Nichecut Scene"):
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

def get_image_pool(image_dir: Path, required_count: int) -> List[Path]:
    """
    Retrieves available images from `image_dir` or assets library. If insufficient, generates artistic fallback images.
    Returns a shuffled list of images matching required_count.
    """
    image_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    existing_images = []
    
    # Check custom channel image dir
    if image_dir.exists():
        existing_images = [f for f in image_dir.iterdir() if f.suffix.lower() in image_extensions and not f.name.startswith("subtitled_")]
        
    # Check general assets library directory
    library_dir = ASSETS_PATH / "images" / "library"
    if library_dir.exists():
        lib_images = [f for f in library_dir.iterdir() if f.suffix.lower() in image_extensions]
        existing_images.extend(lib_images)

    # Generate default fallback images if empty
    if not existing_images:
        logger.info(f"No existing images found in {image_dir}, generating {max(5, required_count)} artistic fallback assets.")
        fallback_dir = ASSETS_PATH / "images" / "fallbacks"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        gen_count = max(5, required_count)
        for i in range(gen_count):
            img_path = fallback_dir / f"artwork_{i+1:02d}.png"
            if not img_path.exists():
                generate_fallback_image(img_path, i)
            existing_images.append(img_path)
            
    # Pool & cycle images to fulfill required count
    pool = []
    while len(pool) < required_count:
        shuffled = list(existing_images)
        random.shuffle(shuffled)
        pool.extend(shuffled)
        
    return pool[:required_count]
