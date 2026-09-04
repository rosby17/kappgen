"""One-off generator for the looping particle overlay clips used by the
"Particules & météo" video effects (stars/dust/snow/rain/sparks).

Run once (`python scripts/generate_particle_assets.py`) whenever an effect's
look needs to change — the output .mp4 files are committed to
assets/particles/ and read directly by assembler.py at render time, no
generation happens during an actual video render.

Each clip is rendered on a black background and composited in assembler.py
with ffmpeg's `blend=all_mode=screen`, which is a no-op on black — only the
bright particle pixels actually show through onto the real video underneath.
This mirrors the CSS `mix-blend-screen` trick the wizard's effect preview
already uses, so the final render finally matches what the preview promises.

Every particle's motion is driven by `(frame / TOTAL_FRAMES) * cycles`, an
integer number of full cycles per clip — that guarantees frame TOTAL_FRAMES
lands exactly back on frame 0, so looping the clip with `-stream_loop -1` in
ffmpeg has no visible seam.
"""
import math
import random
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

WIDTH, HEIGHT = 960, 540
FPS = 30
DURATION_SECONDS = 4
TOTAL_FRAMES = FPS * DURATION_SECONDS

OUT_DIR = Path(__file__).resolve().parent.parent / "assets" / "particles"


def _canvas():
    return Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))


def _save_and_encode(frames_dir: Path, name: str):
    out_path = OUT_DIR / f"{name}.mp4"
    cmd = [
        "ffmpeg", "-y", "-framerate", str(FPS),
        "-i", str(frames_dir / "%04d.png"),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"Wrote {out_path}")


def gen_stars(frames_dir: Path):
    rng = random.Random(1)
    stars = []
    for _ in range(240):
        x = rng.uniform(0, WIDTH)
        y = rng.uniform(0, HEIGHT)
        r = rng.uniform(0.35, 1.25)
        cycles = rng.choice([1, 2, 3])
        phase = rng.uniform(0, math.tau)
        base = rng.uniform(0.22, 0.75)
        tint = rng.choice([(255, 255, 255), (223, 246, 255), (255, 246, 204)])
        stars.append((x, y, r, cycles, phase, base, tint))
    for f in range(TOTAL_FRAMES):
        img = _canvas()
        draw = ImageDraw.Draw(img)
        for x, y, r, cycles, phase, base, tint in stars:
            twinkle = base + (1 - base) * (0.5 + 0.5 * math.sin((f / TOTAL_FRAMES) * cycles * math.tau + phase))
            level = max(0, min(255, round(255 * twinkle)))
            color = tuple(round(c * twinkle) for c in tint)
            draw.ellipse([x - r, y - r, x + r, y + r], fill=color)
        img.save(frames_dir / f"{f:04d}.png")


def gen_dust(frames_dir: Path):
    rng = random.Random(2)
    motes = []
    for _ in range(120):
        x = rng.uniform(0, WIDTH)
        y = rng.uniform(0, HEIGHT)
        r = rng.uniform(1.2, 3.2)
        cycles = rng.choice([1, 2, 3])
        drift = rng.uniform(35, 115)
        brightness = rng.uniform(0.28, 0.82)
        motes.append((x, y, r, cycles, drift, brightness))
    for f in range(TOTAL_FRAMES):
        img = _canvas()
        draw = ImageDraw.Draw(img)
        t = f / TOTAL_FRAMES
        for x, y, r, cycles, drift, brightness in motes:
            # Warm airborne dust travels across frame left-to-right, with a
            # gentle turbulent lift rather than orbiting in a neat pattern.
            px = (x + t * cycles * WIDTH * 0.42) % WIDTH
            py = (y + math.sin(t * cycles * math.tau + x) * drift * 0.22) % HEIGHT
            level = round(255 * brightness)
            draw.ellipse([px - r, py - r, px + r, py + r], fill=(level, round(level * 0.9), round(level * 0.7)))
        img = img.filter(ImageFilter.GaussianBlur(0.8))
        img.save(frames_dir / f"{f:04d}.png")


def gen_snow(frames_dir: Path):
    rng = random.Random(3)
    flakes = []
    for _ in range(320):
        x = rng.uniform(0, WIDTH)
        y0 = rng.uniform(0, HEIGHT)
        r = rng.uniform(0.55, 3.4)
        cycles = rng.choice([1, 2, 3, 4])
        sway_amp = rng.uniform(5, 34)
        sway_phase = rng.uniform(0, math.tau)
        brightness = rng.uniform(0.6, 1.0)
        flakes.append((x, y0, r, cycles, sway_amp, sway_phase, brightness))
    for f in range(TOTAL_FRAMES):
        img = _canvas()
        draw = ImageDraw.Draw(img)
        t = f / TOTAL_FRAMES
        for x, y0, r, cycles, sway_amp, sway_phase, brightness in flakes:
            py = (y0 + t * cycles * HEIGHT) % HEIGHT
            px = (x + math.sin(t * cycles * math.tau + sway_phase) * sway_amp) % WIDTH
            level = round(255 * brightness)
            draw.ellipse([px - r, py - r, px + r, py + r], fill=(level, level, level))
        img.save(frames_dir / f"{f:04d}.png")


def gen_rain(frames_dir: Path):
    rng = random.Random(4)
    drops = []
    for _ in range(110):
        x = rng.uniform(-100, WIDTH)
        y0 = rng.uniform(0, HEIGHT)
        length = rng.uniform(14, 30)
        cycles = rng.choice([4, 5, 6])
        brightness = rng.uniform(0.45, 0.85)
        drops.append((x, y0, length, cycles, brightness))
    slant = 0.35  # horizontal drift per unit vertical fall, for a realistic diagonal streak
    for f in range(TOTAL_FRAMES):
        img = _canvas()
        draw = ImageDraw.Draw(img)
        t = f / TOTAL_FRAMES
        for x, y0, length, cycles, brightness in drops:
            py = (y0 + t * cycles * HEIGHT) % HEIGHT
            px = (x + py * slant) % (WIDTH + 100) - 50
            level = round(255 * brightness)
            color = (round(level * 0.75), round(level * 0.88), level)
            draw.line([px, py, px - length * slant, py - length], fill=color, width=2)
        img = img.filter(ImageFilter.GaussianBlur(0.4))
        img.save(frames_dir / f"{f:04d}.png")


def gen_sparks(frames_dir: Path):
    rng = random.Random(5)
    embers = []
    for _ in range(60):
        x = rng.uniform(0, WIDTH)
        y0 = rng.uniform(0, HEIGHT)
        r = rng.uniform(1.4, 3.0)
        cycles = rng.choice([1, 2])
        sway_amp = rng.uniform(10, 34)
        sway_phase = rng.uniform(0, math.tau)
        flicker_phase = rng.uniform(0, math.tau)
        embers.append((x, y0, r, cycles, sway_amp, sway_phase, flicker_phase))
    for f in range(TOTAL_FRAMES):
        img = _canvas()
        draw = ImageDraw.Draw(img)
        t = f / TOTAL_FRAMES
        for x, y0, r, cycles, sway_amp, sway_phase, flicker_phase in embers:
            py = (y0 - t * cycles * HEIGHT) % HEIGHT
            px = (x + math.sin(t * cycles * math.tau + sway_phase) * sway_amp) % WIDTH
            flicker = 0.5 + 0.5 * math.sin(t * cycles * 3 * math.tau + flicker_phase)
            level = round(255 * (0.55 + 0.45 * flicker))
            draw.ellipse([px - r, py - r, px + r, py + r], fill=(level, round(level * 0.55), 0))
        img = img.filter(ImageFilter.GaussianBlur(0.6))
        img.save(frames_dir / f"{f:04d}.png")


GENERATORS = {
    "stars": gen_stars,
    "dust": gen_dust,
    "snow": gen_snow,
    "rain": gen_rain,
    "sparks": gen_sparks,
}


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_root = OUT_DIR / "_frames_tmp"
    for name, fn in GENERATORS.items():
        frames_dir = tmp_root / name
        if frames_dir.exists():
            shutil.rmtree(frames_dir)
        frames_dir.mkdir(parents=True)
        print(f"Rendering {name}...")
        fn(frames_dir)
        _save_and_encode(frames_dir, name)
    shutil.rmtree(tmp_root)


if __name__ == "__main__":
    main()
