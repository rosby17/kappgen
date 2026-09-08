"""Fully deletes a user account and every row that references it — shared
by the admin "delete user" endpoint and the creator's own self-service
account deletion.

Why this exists as its own careful function instead of a plain db.delete(user):
Postgres has NO ACTION (RESTRICT) foreign keys from a dozen+ tables back to
users.id and channels.id (credit_pots, credit_transactions, api_usage_logs,
voice_clone_jobs, community_library_folders/placements/tags,
channel_sound_effects, channel_pipeline_shares, api_credit_pots/
transactions...). None of them cascade at the database level, and only
channels/api_keys/subscriptions cascade at the SQLAlchemy relationship
level — so a plain db.delete(user) silently rolled back with a foreign key
violation for almost any real account (anyone with a credit pot, i.e.
everyone since signup grants one). This was true of the admin endpoint too;
it just went unnoticed because admin-initiated deletion is rare. Deleting
(or, for rows that belong to a DIFFERENT user, nulling out) every dependent
row explicitly, in dependency order, before deleting the user row itself,
is the only way this actually succeeds.
"""
from sqlalchemy import or_
from sqlalchemy.orm import Session

from src.db.models import (
    User, Channel, Video, ApiKey, Subscription, Order, ApiUsageLog, Folder,
    PasswordReset, CreditPot, CreditTransaction, ApiCreditPot, ApiCreditTransaction,
    VoiceCloneJob, CommunityLibraryFolder, CommunityLibraryImagePlacement,
    CommunityLibraryImageTag, ChannelSoundEffect, ChannelPipelineShare,
)


def delete_user_and_all_data(db: Session, user: User) -> None:
    channel_ids = [c.id for c in db.query(Channel.id).filter(Channel.user_id == user.id).all()]
    video_ids = (
        [v.id for v in db.query(Video.id).filter(Video.channel_id.in_(channel_ids)).all()]
        if channel_ids else []
    )

    # Usage/billing history tied to this user or their content.
    usage_log_conditions = [ApiUsageLog.user_id == user.id]
    if video_ids:
        usage_log_conditions.append(ApiUsageLog.video_id.in_(video_ids))
    if channel_ids:
        usage_log_conditions.append(ApiUsageLog.channel_id.in_(channel_ids))
    db.query(ApiUsageLog).filter(or_(*usage_log_conditions)).delete(synchronize_session=False)
    db.query(CreditPot).filter(CreditPot.user_id == user.id).delete(synchronize_session=False)
    db.query(CreditTransaction).filter(CreditTransaction.user_id == user.id).delete(synchronize_session=False)
    db.query(ApiCreditPot).filter(ApiCreditPot.user_id == user.id).delete(synchronize_session=False)
    db.query(ApiCreditTransaction).filter(ApiCreditTransaction.user_id == user.id).delete(synchronize_session=False)
    db.query(Order).filter(Order.user_id == user.id).delete(synchronize_session=False)
    db.query(PasswordReset).filter(PasswordReset.user_id == user.id).delete(synchronize_session=False)
    db.query(Folder).filter(Folder.user_id == user.id).delete(synchronize_session=False)

    if channel_ids:
        # A community library placement can point at one of this user's
        # channels as its *target* merge destination even when the shared
        # image itself belongs to someone else's channel — null that
        # reference out rather than deleting a row that isn't this user's.
        db.query(CommunityLibraryImagePlacement).filter(
            CommunityLibraryImagePlacement.target_channel_id.in_(channel_ids)
        ).update({"target_channel_id": None}, synchronize_session=False)
        db.query(CommunityLibraryImagePlacement).filter(
            CommunityLibraryImagePlacement.channel_id.in_(channel_ids)
        ).delete(synchronize_session=False)
        db.query(CommunityLibraryImageTag).filter(
            CommunityLibraryImageTag.channel_id.in_(channel_ids)
        ).delete(synchronize_session=False)
        db.query(CommunityLibraryFolder).filter(
            CommunityLibraryFolder.channel_id.in_(channel_ids)
        ).delete(synchronize_session=False)
        db.query(VoiceCloneJob).filter(VoiceCloneJob.channel_id.in_(channel_ids)).delete(synchronize_session=False)
        db.query(ChannelSoundEffect).filter(ChannelSoundEffect.channel_id.in_(channel_ids)).delete(synchronize_session=False)
        db.query(ChannelPipelineShare).filter(
            (ChannelPipelineShare.channel_id.in_(channel_ids)) | (ChannelPipelineShare.owner_user_id == user.id)
        ).delete(synchronize_session=False)
        db.query(Video).filter(Video.channel_id.in_(channel_ids)).delete(synchronize_session=False)
        db.query(Channel).filter(Channel.id.in_(channel_ids)).delete(synchronize_session=False)

    # A subscription this user granted as an admin to someone ELSE is that
    # other user's record, not this user's — keep it, just drop the
    # dangling reference to the admin who's being deleted.
    db.query(Subscription).filter(Subscription.granted_by_admin_id == user.id).update(
        {"granted_by_admin_id": None}, synchronize_session=False
    )
    db.query(Subscription).filter(Subscription.user_id == user.id).delete(synchronize_session=False)
    db.query(ApiKey).filter(ApiKey.user_id == user.id).delete(synchronize_session=False)

    db.delete(user)
    db.commit()
