"""Hybrid rendered-video storage on Cloudflare R2 (S3-compatible).

The idea: R2's free tier is 10GB. As long as our own tracked usage (summed
from Video.output_size_bytes for every video already on R2 — never a live
"list the whole bucket" call, which would get slower and costlier as the
bucket grows) stays under R2_FREE_TIER_CAP_BYTES, a finished render uploads
there and gets removed from local disk. Once that would tip over the cap,
new renders just stay on local disk exactly like before R2 existed — no
error, no blocked render, just a silent fallback. Raising (or removing)
R2_FREE_TIER_CAP_BYTES after upgrading to a paid R2 plan is the only change
needed to store more there; nothing else in the pipeline has to know.
"""
from pathlib import Path
from typing import Optional
from src.config import (
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME,
    R2_PUBLIC_URL_BASE, R2_FREE_TIER_CAP_BYTES,
)
from src.utils.logger import logger

_client = None


def is_r2_configured() -> bool:
    return bool(R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME and R2_PUBLIC_URL_BASE)


def _get_client():
    global _client
    if _client is not None:
        return _client
    import boto3
    _client = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )
    return _client


def current_r2_usage_bytes(db) -> int:
    """Sum of output_size_bytes for every video currently stored on R2 —
    read from our own DB, never from R2 itself, so this stays cheap
    regardless of how large the bucket gets."""
    from sqlalchemy import func
    from src.db.models import Video
    total = db.query(func.coalesce(func.sum(Video.output_size_bytes), 0)).filter(
        Video.storage_backend == "r2"
    ).scalar()
    return int(total or 0)


def should_upload_to_r2(db, estimated_bytes: int) -> bool:
    if not is_r2_configured():
        return False
    return (current_r2_usage_bytes(db) + max(0, estimated_bytes)) <= R2_FREE_TIER_CAP_BYTES


def upload_video(local_path: Path, object_key: str) -> Optional[str]:
    """Uploads local_path to R2 under object_key, returns the public URL on
    success or None on failure (caller should keep the file on local disk
    if this fails — never lose a render because R2 had a bad moment)."""
    try:
        client = _get_client()
        client.upload_file(
            str(local_path), R2_BUCKET_NAME, object_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
        return f"{R2_PUBLIC_URL_BASE}/{object_key}"
    except Exception as exc:
        logger.warning(f"R2 upload failed for {local_path} ({object_key}): {exc}")
        return None


def delete_video(object_key: str) -> None:
    """Best-effort delete — a failed cleanup here just leaves an orphaned
    object in the bucket, never something worth failing a purge pass over."""
    if not is_r2_configured():
        return
    try:
        client = _get_client()
        client.delete_object(Bucket=R2_BUCKET_NAME, Key=object_key)
    except Exception as exc:
        logger.warning(f"R2 delete failed for {object_key}: {exc}")


def object_key_from_url(url: str) -> Optional[str]:
    if not url or not url.startswith(R2_PUBLIC_URL_BASE):
        return None
    return url[len(R2_PUBLIC_URL_BASE):].lstrip("/")
