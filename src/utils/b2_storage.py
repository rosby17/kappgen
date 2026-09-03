"""Rendered-video and B-roll storage on Backblaze B2 (S3-compatible).

Replaces Cloudflare R2 as of Sept 2026 — same idea (hybrid local/remote,
never lose a render because the remote store had a bad moment) but on B2
because its storage is ~1/5 the price of R2 (0.006 $/Go vs 0.015 $/Go),
with free egress up to 3x the stored volume/day via Backblaze's Bandwidth
Alliance with Cloudflare. Interface is a straight copy of r2_storage.py —
B2 speaks plain S3 — so nothing downstream needs to know which backend a
video landed on beyond the `storage_backend` column already storing "r2"
history; new writes just always use "b2" going forward.
"""
import re
from pathlib import Path
from typing import Optional
from src.config import (
    B2_ENDPOINT, B2_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME,
    B2_PUBLIC_URL_BASE, B2_FREE_TIER_CAP_BYTES, B2_REGION,
)
from src.utils.logger import logger

_client = None


def slugify(name: Optional[str], fallback: str = "chaine") -> str:
    """Turns a channel name into a short, readable, filesystem/URL-safe
    fragment for object keys — purely cosmetic (so a human browsing the B2
    console can recognize a channel at a glance), never the actual
    identifier: the channel/video UUID stays the real key, this is only
    ever a suffix next to it, so a rename, an empty name, or two channels
    sharing a name can never cause a collision or a lost file."""
    if not name:
        return fallback
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:40] or fallback


def short_id(uuid_str: Optional[str], length: int = 8) -> str:
    """First `length` chars of a UUID — purely for shorter, easier-to-scan B2
    object keys (a creator/admin browsing the B2 console was getting lost in
    full 36-char UUIDs). Not the source of truth: the video's own folder
    still nests under this, and the DB row is always addressed by the full
    UUID — a theoretical 8-char prefix collision between two channels would
    just mean their videos share a top folder in the B2 console, never a
    lost or overwritten file."""
    if not uuid_str:
        return "chaine"
    return uuid_str.replace("-", "")[:length]


def is_b2_configured() -> bool:
    return bool(B2_ENDPOINT and B2_KEY_ID and B2_APPLICATION_KEY and B2_BUCKET_NAME and B2_PUBLIC_URL_BASE)


def _get_client():
    global _client
    if _client is not None:
        return _client
    import boto3
    _client = boto3.client(
        "s3",
        endpoint_url=f"https://{B2_ENDPOINT}",
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APPLICATION_KEY,
        region_name=B2_REGION,
    )
    return _client


def current_b2_usage_bytes(db) -> int:
    """Sum of output_size_bytes for every video currently stored on B2 —
    read from our own DB, never from B2 itself, so this stays cheap
    regardless of how large the bucket gets."""
    from sqlalchemy import func
    from src.db.models import Video
    total = db.query(func.coalesce(func.sum(Video.output_size_bytes), 0)).filter(
        Video.storage_backend == "b2"
    ).scalar()
    return int(total or 0)


def should_upload_to_b2(db, estimated_bytes: int) -> bool:
    if not is_b2_configured():
        return False
    if not B2_FREE_TIER_CAP_BYTES:
        return True  # cap of 0/unset means "no cap, always allowed" once B2 is the primary store
    return (current_b2_usage_bytes(db) + max(0, estimated_bytes)) <= B2_FREE_TIER_CAP_BYTES


def upload_video(local_path: Path, object_key: str) -> Optional[str]:
    """Uploads local_path to B2 under object_key, returns the public URL on
    success or None on failure (caller should keep the file on local disk
    if this fails — never lose a render because B2 had a bad moment)."""
    try:
        client = _get_client()
        client.upload_file(
            str(local_path), B2_BUCKET_NAME, object_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
        return f"{B2_PUBLIC_URL_BASE}/{object_key}"
    except Exception as exc:
        logger.warning(f"B2 upload failed for {local_path} ({object_key}): {exc}")
        return None


def delete_video(object_key: str) -> None:
    """Best-effort delete — a failed cleanup here just leaves an orphaned
    object in the bucket, never something worth failing a purge pass over."""
    if not is_b2_configured():
        return
    try:
        client = _get_client()
        client.delete_object(Bucket=B2_BUCKET_NAME, Key=object_key)
    except Exception as exc:
        logger.warning(f"B2 delete failed for {object_key}: {exc}")


def object_key_from_url(url: str) -> Optional[str]:
    if not url or not url.startswith(B2_PUBLIC_URL_BASE):
        return None
    return url[len(B2_PUBLIC_URL_BASE):].lstrip("/")


def presigned_put_url(object_key: str, content_type: str = "video/mp4", expires_in: int = 1800) -> Optional[str]:
    """A short-lived URL the BROWSER uploads straight to, bypassing our own
    API entirely for the actual file bytes — used for B-roll clips too large
    for api.kappgen.com's own Cloudflare-proxied request-body cap (~100MB)."""
    if not is_b2_configured():
        return None
    try:
        client = _get_client()
        return client.generate_presigned_url(
            "put_object",
            Params={"Bucket": B2_BUCKET_NAME, "Key": object_key, "ContentType": content_type},
            ExpiresIn=expires_in,
        )
    except Exception as exc:
        logger.warning(f"B2 presigned URL generation failed for {object_key}: {exc}")
        return None
