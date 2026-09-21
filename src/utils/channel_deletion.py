"""Clears everything that points at a channel before it can be deleted.

Only Video cascades on its own (Channel.videos, cascade="all, delete-orphan").
Every other table carrying a channel_id was added with a plain foreign key
and no cascade, ORM or DB — so each one blocks the delete with an integrity
error the moment a channel has ever been used. The failure surfaces as a
500 with no CORS headers, which the browser reports as "Failed to fetch" and
the UI as "Les serveurs KappGen sont momentanément indisponibles" — a
network outage message for what is really a constraint violation.

Both delete paths (the creator's own DELETE /channels/{id} and the admin's)
call this, so a table added later can only ever be forgotten once, here,
instead of separately in each route.
"""
from sqlalchemy import or_
from sqlalchemy.orm import Session

from src.db.models import (
    ApiUsageLog,
    ChannelSoundEffect,
    CommunityLibraryFolder,
    CommunityLibraryImagePlacement,
    CommunityLibraryImageTag,
    VoiceCloneJob,
)
from src.utils.logger import logger


def purge_channel_references(db: Session, channel_id: str) -> None:
    """Delete every row referencing this channel. Does not commit — the
    caller deletes the channel itself and commits once, so a failure rolls
    the whole thing back rather than leaving a channel stripped of its
    history but still present."""
    removed = {
        "api_usage_logs": db.query(ApiUsageLog).filter(ApiUsageLog.channel_id == channel_id).delete(synchronize_session=False),
        "voice_clone_jobs": db.query(VoiceCloneJob).filter(VoiceCloneJob.channel_id == channel_id).delete(synchronize_session=False),
        "community_folder": db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.channel_id == channel_id).delete(synchronize_session=False),
        # Both columns: a placement can point at this channel as the image's
        # owner OR as the channel an admin merged it into.
        "community_placements": db.query(CommunityLibraryImagePlacement).filter(
            or_(
                CommunityLibraryImagePlacement.channel_id == channel_id,
                CommunityLibraryImagePlacement.target_channel_id == channel_id,
            )
        ).delete(synchronize_session=False),
        "community_image_tags": db.query(CommunityLibraryImageTag).filter(CommunityLibraryImageTag.channel_id == channel_id).delete(synchronize_session=False),
        "sound_effects": db.query(ChannelSoundEffect).filter(ChannelSoundEffect.channel_id == channel_id).delete(synchronize_session=False),
    }
    summary = ", ".join(f"{count} {table}" for table, count in removed.items() if count)
    if summary:
        logger.info(f"Channel {channel_id}: cleared dependent rows before delete ({summary}).")
