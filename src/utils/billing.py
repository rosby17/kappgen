"""Free-tier quota + subscription checks shared by the video-submission
paywall and the watermark-removal gate."""
from datetime import datetime
from sqlalchemy.orm import Session
from src.db.models import User, Subscription


def user_has_active_subscription(db: Session, user: User) -> bool:
    return db.query(Subscription).filter(
        Subscription.user_id == user.id,
        Subscription.status == "active",
        Subscription.expires_at > datetime.utcnow(),
    ).first() is not None


def user_can_render(db: Session, user: User) -> tuple[bool, str]:
    if user.free_videos_used < user.free_video_quota_granted:
        return True, ""
    if user_has_active_subscription(db, user):
        return True, ""
    return False, "Quota gratuit épuisé — un abonnement est requis pour générer d'autres vidéos."
