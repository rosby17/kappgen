"""Source-video ingestion for the Recap pipeline — a direct upload, or a
YouTube URL downloaded server-side via yt-dlp.

yt-dlp downloading from YouTube is against YouTube's own Terms of Service
(independent of whatever rights the creator has to the underlying content) —
a real operational risk to whichever outbound IP this runs from, not just a
copyright question. Kept in its own module so it can be pointed at a
dedicated egress (proxy/IP) later without touching the rest of the pipeline.
"""
from __future__ import annotations

from pathlib import Path

MAX_RECAP_SOURCE_DURATION_SECONDS = 3 * 60 * 60  # 3h, generous ceiling — real cost control is billing on narration length, not the source


def download_youtube_source(url: str, dest_path: Path) -> Path:
    import yt_dlp

    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": str(dest_path.with_suffix("")) + ".%(ext)s",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": 4 * 1024 * 1024 * 1024,  # 4 GB
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        duration = info.get("duration") or 0
        if duration and duration > MAX_RECAP_SOURCE_DURATION_SECONDS:
            downloaded = Path(ydl.prepare_filename(info))
            downloaded.unlink(missing_ok=True)
            raise ValueError(f"Vidéo source trop longue ({duration/60:.0f} min, max {MAX_RECAP_SOURCE_DURATION_SECONDS//60} min).")
        downloaded = Path(ydl.prepare_filename(info))
        # merge_output_format can rename the extension after prepare_filename
        # was computed pre-merge — fall back to scanning for whatever landed
        # next to it if the exact predicted name isn't there.
        if not downloaded.exists():
            candidates = list(downloaded.parent.glob(downloaded.stem + ".*"))
            if not candidates:
                raise RuntimeError("Le téléchargement YouTube a échoué (fichier introuvable après extraction).")
            downloaded = candidates[0]

    if downloaded != dest_path:
        downloaded.replace(dest_path)
    return dest_path
