#!/usr/bin/env python3
"""
Import script for Easy Voice (Izivoice) synthetic AI voices catalog.
Bulk imports 12,000+ voices, uploads audio preview files to Cloudflare R2 or local storage,
and updates database records in batch transactions.

Usage:
  # Standard test run
  python backend/scripts/import_voices.py --source-dir ~/Downloads/mes_voix --limit 100 --dry-run

  # Full import with Cloudflare R2 audio previews storage
  python backend/scripts/import_voices.py --source-dir ~/Downloads/mes_voix --use-r2 --batch-size 500
"""

import os
import sys
import json
import csv
import glob
import shutil
import argparse
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.session import engine, SessionLocal
from src.db.models import Base, Voice
from src.config import (
    STORAGE_PATH, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_URL_BASE,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("import_voices")

CHECKPOINT_FILE = Path(__file__).resolve().parent / "import_voices_checkpoint.json"
PREVIEWS_STORAGE_DIR = STORAGE_PATH / "voices" / "previews"

_r2_client = None


def is_r2_configured() -> bool:
    return bool(R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME and R2_PUBLIC_URL_BASE)


def get_r2_client():
    global _r2_client
    if _r2_client is not None:
        return _r2_client
    import boto3
    _r2_client = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )
    return _r2_client


def upload_preview_to_r2(local_path: Path, object_key: str) -> Optional[str]:
    """Uploads preview audio file to Cloudflare R2 bucket and returns public CDN URL."""
    try:
        client = get_r2_client()
        content_type = "audio/mpeg" if local_path.suffix.lower() == ".mp3" else "audio/wav"
        client.upload_file(
            str(local_path),
            R2_BUCKET_NAME,
            object_key,
            ExtraArgs={"ContentType": content_type},
        )
        url_base = R2_PUBLIC_URL_BASE.rstrip("/")
        return f"{url_base}/{object_key}"
    except Exception as exc:
        logger.warning(f"R2 upload failed for {local_path} ({object_key}): {exc}")
        return None


def parse_metadata_file(source_dir: Path) -> List[Dict[str, Any]]:
    """Tries to read metadata from JSON, CSV, or auto-indexes audio files in directory."""
    voices_data = []

    # 1. Try voices.json / metadata.json
    json_candidates = list(source_dir.glob("*.json"))
    for json_file in json_candidates:
        if json_file.name == "import_voices_checkpoint.json":
            continue
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                content = json.load(f)
                if isinstance(content, list):
                    logger.info(f"Loaded {len(content)} voices from JSON file: {json_file.name}")
                    return content
                elif isinstance(content, dict) and "voices" in content:
                    logger.info(f"Loaded {len(content['voices'])} voices from JSON key 'voices': {json_file.name}")
                    return content["voices"]
        except Exception as e:
            logger.warning(f"Could not parse JSON file {json_file.name}: {e}")

    # 2. Try CSV metadata file
    csv_candidates = list(source_dir.glob("*.csv"))
    for csv_file in csv_candidates:
        try:
            with open(csv_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    voices_data.append(row)
            if voices_data:
                logger.info(f"Loaded {len(voices_data)} voices from CSV file: {csv_file.name}")
                return voices_data
        except Exception as e:
            logger.warning(f"Could not parse CSV file {csv_file.name}: {e}")

    # 3. Fallback: Auto-index all audio files in source directory
    audio_extensions = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}
    audio_files = [f for f in source_dir.rglob("*") if f.suffix.lower() in audio_extensions]

    if audio_files:
        logger.info(f"No JSON/CSV found. Auto-indexing {len(audio_files)} audio files directly...")
        for idx, audio in enumerate(audio_files):
            stem = audio.stem
            gender = "female" if any(g in stem.lower() for g in ["female", "femme", "woman", "girl"]) else ("male" if any(g in stem.lower() for g in ["male", "homme", "man", "boy"]) else "neutral")
            language = "fr" if any(l in stem.lower() for l in ["fr", "french", "francais"]) else "en"
            
            voices_data.append({
                "voice_id": f"voice_{idx+1:05d}",
                "name": stem.replace("_", " ").replace("-", " ").title(),
                "language": language,
                "gender": gender,
                "preview_filename": str(audio.relative_to(source_dir)),
                "category": "Synthetic AI",
                "tags": ["ai", "synthetic", gender, language],
            })
        return voices_data

    return []


def load_checkpoint() -> set:
    if CHECKPOINT_FILE.exists():
        try:
            with open(CHECKPOINT_FILE, "r") as f:
                return set(json.load(f).get("imported_ids", []))
        except Exception:
            return set()
    return set()


def save_checkpoint(imported_ids: set):
    try:
        with open(CHECKPOINT_FILE, "w") as f:
            json.dump({"imported_ids": list(imported_ids)}, f)
    except Exception as e:
        logger.warning(f"Failed to save checkpoint: {e}")


def import_voices(
    source_dir: Path,
    batch_size: int = 500,
    limit: Optional[int] = None,
    dry_run: bool = False,
    use_r2: bool = False,
    reset_checkpoint: bool = False,
):
    source_dir = source_dir.expanduser().resolve()
    if not source_dir.exists():
        logger.error(f"Source directory '{source_dir}' does not exist.")
        sys.exit(1)

    logger.info(f"Scanning source directory: {source_dir}")
    raw_voices = parse_metadata_file(source_dir)

    if not raw_voices:
        logger.error(f"No voices metadata or audio files found in '{source_dir}'.")
        sys.exit(1)

    if limit:
        raw_voices = raw_voices[:limit]
        logger.info(f"Limited processing to {limit} voices.")

    upload_to_r2 = use_r2 or is_r2_configured()
    if upload_to_r2:
        if not is_r2_configured():
            logger.error("Cloudflare R2 requested (--use-r2) but R2 environment variables are not fully set in .env")
            sys.exit(1)
        logger.info(f"Cloudflare R2 Enabled! Uploading audio previews to R2 Bucket: '{R2_BUCKET_NAME}'")
    else:
        logger.info("Using local storage for audio previews.")
        PREVIEWS_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    imported_ids = set() if reset_checkpoint else load_checkpoint()
    logger.info(f"Total voices to process: {len(raw_voices)} (Already imported: {len(imported_ids)})")

    if not dry_run:
        Base.metadata.create_all(bind=engine)

    logger.info("Indexing source audio files for fast lookup...")
    file_map = {}
    samples_dir = source_dir / "samples"
    search_paths = [samples_dir] if samples_dir.exists() else [source_dir]
    for p in search_paths:
        for f in p.glob("*"):
            if f.is_file():
                file_map[f.name] = f
    if len(file_map) < 100:
        for f in source_dir.rglob("*"):
            if f.is_file():
                file_map[f.name] = f

    db = SessionLocal()
    existing_db_ids = set()
    if not dry_run:
        try:
            existing_db_ids = set(r[0] for r in db.query(Voice.id).all())
            logger.info(f"Found {len(existing_db_ids)} existing voices in database.")
        except Exception as e:
            logger.warning(f"Could not query existing voice IDs: {e}")

    batch = []
    processed_count = 0
    success_count = 0

    try:
        for idx, item in enumerate(raw_voices, start=1):
            voice_id = str(item.get("voice_id") or item.get("id") or f"synth_v_{idx:06d}")
            if voice_id in imported_ids or voice_id in existing_db_ids:
                continue

            name = item.get("name") or f"Voix Synthétique #{idx}"
            language = item.get("language") or "fr"
            gender = item.get("gender") or "neutral"
            category = item.get("category") or "Générale"
            tags = item.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]

            # Handle preview audio file
            preview_filename = item.get("preview_filename") or item.get("preview") or item.get("audio_file") or item.get("filename")
            preview_url = item.get("preview_url")

            if preview_filename:
                audio_src = source_dir / preview_filename
                if not audio_src.exists():
                    audio_src = file_map.get(Path(preview_filename).name)

                if audio_src and audio_src.exists():
                    dest_filename = f"{voice_id}{audio_src.suffix}"
                    object_key = f"voices/previews/{dest_filename}"

                    if upload_to_r2:
                        if not dry_run:
                            r2_url = upload_preview_to_r2(audio_src, object_key)
                            preview_url = r2_url or preview_url
                        else:
                            preview_url = f"{R2_PUBLIC_URL_BASE}/{object_key}"
                    else:
                        dest_path = PREVIEWS_STORAGE_DIR / dest_filename
                        if not dry_run and not dest_path.exists():
                            shutil.copy2(audio_src, dest_path)
                        preview_url = f"/api/channels/storage/voices/previews/{dest_filename}"

            voice_obj = Voice(
                id=voice_id,
                name=name,
                language=language.lower(),
                gender=gender.lower(),
                category=category,
                preview_url=preview_url,
                tags=tags,
                provider="izivoice",
                is_active=True,
            )

            batch.append(voice_obj)
            imported_ids.add(voice_id)
            processed_count += 1
            success_count += 1

            if len(batch) >= batch_size or idx == len(raw_voices):
                if not dry_run and batch:
                    db.add_all(batch)
                    db.commit()
                    save_checkpoint(imported_ids)
                logger.info(f"Processed batch ({len(batch)} voices). Total imported: {success_count}/{len(raw_voices)}")
                batch.clear()

        logger.info(f"=== Import Completed Successfully! ===")
        logger.info(f"Total voices added/updated: {success_count}")
        if dry_run:
            logger.info("[DRY RUN MODE] No changes were written to the database or R2 storage.")

    except Exception as e:
        db.rollback()
        logger.error(f"Error during import execution: {e}", exc_info=True)
        sys.exit(1)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Import 12,000+ synthetic AI voices into Easy Voice library.")
    parser.add_argument("--source-dir", required=True, type=str, help="Directory containing voice files / metadata")
    parser.add_argument("--batch-size", type=int, default=500, help="Batch size for database transactions (default: 500)")
    parser.add_argument("--limit", type=int, default=None, help="Limit maximum number of voices to import")
    parser.add_argument("--dry-run", action="store_true", help="Simulate import without modifying database or uploading files")
    parser.add_argument("--use-r2", action="store_true", help="Force upload audio previews to Cloudflare R2 bucket")
    parser.add_argument("--reset-checkpoint", action="store_true", help="Reset checkpoint and re-import all voices")

    args = parser.parse_args()

    import_voices(
        source_dir=Path(args.source_dir),
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
        use_r2=args.use_r2,
        reset_checkpoint=args.reset_checkpoint,
    )


if __name__ == "__main__":
    main()
