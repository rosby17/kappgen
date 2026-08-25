from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from src.db.session import get_db
from src.db.models import User, Channel, Video, Plan, Subscription, Order, ApiUsageLog, Folder, PasswordReset, CommunityLibraryFolder
from src.utils.auth import get_current_admin
from src.utils.billing import user_has_active_subscription, get_credit_balance, credit_user, debit_credits

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/users")
def list_users(q: Optional[str] = None, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    query = db.query(User)
    if q:
        query = query.filter(User.email.ilike(f"%{q}%"))
    users = query.order_by(User.created_at.desc()).limit(500).all()
    result = []
    for u in users:
        data = u.to_dict()
        data["channel_count"] = db.query(Channel).filter(Channel.user_id == u.id).count()
        data["video_count"] = (
            db.query(Video).join(Channel, Video.channel_id == Channel.id).filter(Channel.user_id == u.id).count()
        )
        data["has_active_subscription"] = user_has_active_subscription(db, u)
        data["credit_balance"] = get_credit_balance(db, u)
        result.append(data)
    return result


@router.get("/users/{user_id}")
def get_user_detail(user_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    data = user.to_dict()
    data["channels"] = [c.to_dict() for c in db.query(Channel).filter(Channel.user_id == user_id).all()]
    data["subscriptions"] = [
        s.to_dict() for s in db.query(Subscription).filter(Subscription.user_id == user_id).order_by(Subscription.started_at.desc()).all()
    ]
    data["orders"] = [
        o.to_dict() for o in db.query(Order).filter(Order.user_id == user_id).order_by(Order.created_at.desc()).all()
    ]
    data["credit_balance"] = get_credit_balance(db, user)
    return data


@router.delete("/users/{user_id}")
def delete_user(user_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Permanently deletes an account — for removing throwaway/test/audit
    accounts. Channels, their videos, API keys and subscriptions cascade via
    the User model's own relationships; PasswordReset/Order/ApiUsageLog rows
    reference the user without a cascading relationship (Order keeps
    financial history intentionally elsewhere), so they're deleted explicitly
    here to avoid a foreign-key violation."""
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Impossible de supprimer ton propre compte admin.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    db.query(PasswordReset).filter(PasswordReset.user_id == user_id).delete()
    db.query(Order).filter(Order.user_id == user_id).delete()
    db.query(ApiUsageLog).filter(ApiUsageLog.user_id == user_id).delete()
    db.query(Folder).filter(Folder.user_id == user_id).delete()
    db.delete(user)
    db.commit()
    return {"deleted": True}


class GrantSubscriptionPayload(BaseModel):
    plan_id: Optional[str] = None
    duration_days: Optional[int] = None  # required if plan_id is not given (a custom/free grant)
    note: Optional[str] = None


@router.post("/users/{user_id}/grant-subscription")
def grant_subscription(user_id: str, payload: GrantSubscriptionPayload, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    duration_days = payload.duration_days
    plan = None
    if payload.plan_id:
        plan = db.query(Plan).filter(Plan.id == payload.plan_id).first()
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        duration_days = duration_days or plan.duration_days
    if not duration_days:
        raise HTTPException(status_code=400, detail="duration_days est requis quand aucun plan n'est fourni.")

    sub = Subscription(
        user_id=user.id,
        plan_id=plan.id if plan else None,
        status="active",
        started_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=duration_days),
        granted_by_admin_id=admin.id,
        note=payload.note,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub.to_dict()


@router.post("/users/{user_id}/revoke-subscription")
def revoke_subscription(user_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    subs = db.query(Subscription).filter(Subscription.user_id == user_id, Subscription.status == "active").all()
    for s in subs:
        s.status = "cancelled"
    db.commit()
    return {"revoked": len(subs)}


class CreditAdjustPayload(BaseModel):
    amount: int
    note: Optional[str] = None


@router.post("/users/{user_id}/credits/grant")
def grant_credits(user_id: str, payload: CreditAdjustPayload, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Adds credits to a user's balance — a permanent grant (never expires,
    same convention as the welcome bonus), not tied to any Order/Plan, for
    manual top-ups, goodwill compensation, promo grants, etc."""
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Le montant doit être positif.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    note = f"Crédit admin ({admin.email}){': ' + payload.note if payload.note else ''}"
    credit_user(db, user, payload.amount, 36500, note, transaction_type="admin_grant")
    return {"credit_balance": get_credit_balance(db, user)}


@router.post("/users/{user_id}/credits/revoke")
def revoke_credits(user_id: str, payload: CreditAdjustPayload, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Debits credits from a user's balance. Unlike a normal usage debit,
    this is allowed to bring the balance to exactly 0 even if `amount`
    slightly overshoots what's actually left — an admin correcting a
    mistaken grant shouldn't fail because the user already spent a few
    credits since."""
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Le montant doit être positif.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    note = f"Retrait admin ({admin.email}){': ' + payload.note if payload.note else ''}"
    balance = get_credit_balance(db, user)
    ok = debit_credits(db, user, min(payload.amount, balance), note)
    if not ok:
        raise HTTPException(status_code=400, detail="Échec du retrait de crédits.")
    return {"credit_balance": get_credit_balance(db, user)}


@router.get("/plans")
def admin_list_plans(admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    return [p.to_dict() for p in db.query(Plan).order_by(Plan.price_fcfa.asc()).all()]


class PlanPayload(BaseModel):
    name: str
    price_fcfa: int
    original_price_fcfa: Optional[int] = None
    duration_days: int = 30
    is_active: bool = True
    # Credit-pack plans (Izivoice-style) set `credits`; hybrid subscription
    # tiers leave it null and use video_quota_per_cycle/monthly_credit_grant
    # instead — see Plan.video_quota_per_cycle's docstring in src/db/models.py.
    # The feature/cap fields below apply to both kinds of plan (a credit pack
    # can gate features exactly like a subscription tier does).
    credits: Optional[int] = None
    video_quota_per_cycle: Optional[int] = None
    ai_transcription_enabled: bool = True
    ai_images_enabled: bool = True
    ai_script_enabled: bool = True
    autopublish_enabled: bool = True
    monthly_credit_grant: Optional[int] = None
    max_channels: Optional[int] = None
    max_video_duration_seconds: Optional[int] = None


@router.post("/plans")
def create_plan(payload: PlanPayload, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    plan = Plan(**payload.model_dump())
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan.to_dict()


@router.put("/plans/{plan_id}")
def update_plan(plan_id: str, payload: PlanPayload, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    plan = db.query(Plan).filter(Plan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    for field, value in payload.model_dump().items():
        setattr(plan, field, value)
    db.commit()
    db.refresh(plan)
    return plan.to_dict()


@router.delete("/plans/{plan_id}")
def delete_plan(plan_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    plan = db.query(Plan).filter(Plan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    plan.is_active = False  # soft-delete: existing Subscriptions/Orders still reference it
    db.commit()
    return {"message": "Plan deactivated"}


@router.get("/stats")
def admin_stats(admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    total_users = db.query(User).count()
    active_subscriptions = db.query(Subscription).filter(
        Subscription.status == "active", Subscription.expires_at > datetime.utcnow()
    ).count()
    revenue_fcfa = db.query(Order).filter(Order.status == "success").with_entities(Order.amount_fcfa).all()
    total_revenue = sum(r[0] for r in revenue_fcfa)
    total_videos = db.query(Video).count()
    today = datetime.utcnow().date()
    videos_today = db.query(Video).filter(Video.created_at >= datetime(today.year, today.month, today.day)).count()
    return {
        "total_users": total_users,
        "active_subscriptions": active_subscriptions,
        "total_revenue_fcfa": total_revenue,
        "total_videos": total_videos,
        "videos_today": videos_today,
    }


@router.get("/providers/status")
def admin_provider_status(admin: User = Depends(get_current_admin)):
    """Live 'is this provider working right now' check for each external API
    the pipeline depends on — see src/utils/provider_status.py for exactly
    what each check does and, importantly, doesn't do (most providers expose
    no real balance API; this is not a substitute for checking their own
    dashboards for exact billing)."""
    from src.utils.provider_status import check_all_providers
    return {"providers": check_all_providers()}


@router.get("/costs")
def admin_costs(days: int = 30, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Estimated spend across every external API the pipeline calls (see
    src/utils/cost_tracking.py — these are computed from token/character/
    image counts against a published pricing table, not a live account
    balance, since most providers don't expose one). Powers the admin
    "Coûts" page: a total, a breakdown by provider and by operation, and the
    most expensive recent videos."""
    days = max(1, min(days, 365))
    since = datetime.utcnow() - timedelta(days=days)
    rows = db.query(ApiUsageLog).filter(ApiUsageLog.created_at >= since).all()

    total_cost = sum(r.cost_usd for r in rows)
    by_provider: dict = {}
    by_operation: dict = {}
    by_video: dict = {}
    for r in rows:
        by_provider.setdefault(r.provider, {"cost_usd": 0.0, "calls": 0})
        by_provider[r.provider]["cost_usd"] += r.cost_usd
        by_provider[r.provider]["calls"] += 1

        by_operation.setdefault(r.operation, {"cost_usd": 0.0, "calls": 0})
        by_operation[r.operation]["cost_usd"] += r.cost_usd
        by_operation[r.operation]["calls"] += 1

        if r.video_id:
            by_video.setdefault(r.video_id, 0.0)
            by_video[r.video_id] += r.cost_usd

    top_video_ids = sorted(by_video, key=by_video.get, reverse=True)[:10]
    top_videos = []
    if top_video_ids:
        videos = {v.id: v for v in db.query(Video).filter(Video.id.in_(top_video_ids)).all()}
        for vid in top_video_ids:
            v = videos.get(vid)
            top_videos.append({"video_id": vid, "title": v.title if v else None, "cost_usd": round(by_video[vid], 4)})

    return {
        "days": days,
        "total_cost_usd": round(total_cost, 4),
        "total_calls": len(rows),
        "by_provider": {k: {"cost_usd": round(v["cost_usd"], 4), "calls": v["calls"]} for k, v in by_provider.items()},
        "by_operation": {k: {"cost_usd": round(v["cost_usd"], 4), "calls": v["calls"]} for k, v in by_operation.items()},
        "top_videos": top_videos,
    }


@router.get("/activity")
def admin_activity(days: int = 28, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Daily new-users / new-videos series for the dashboard chart, plus a
    couple of "right now" numbers — mirrors the shape of iziVoice's admin
    overview (daily line + a live sidebar) without needing a time-series DB."""
    days = max(1, min(days, 90))
    today = datetime.utcnow().date()
    start = today - timedelta(days=days - 1)

    users = db.query(User).filter(User.created_at >= datetime(start.year, start.month, start.day)).all()
    videos = db.query(Video).filter(Video.created_at >= datetime(start.year, start.month, start.day)).all()

    by_day = {}
    for i in range(days):
        d = start + timedelta(days=i)
        by_day[d.isoformat()] = {"date": d.isoformat(), "new_users": 0, "new_videos": 0}
    for u in users:
        key = u.created_at.date().isoformat()
        if key in by_day:
            by_day[key]["new_users"] += 1
    for v in videos:
        key = v.created_at.date().isoformat()
        if key in by_day:
            by_day[key]["new_videos"] += 1

    now = datetime.utcnow()
    videos_48h = db.query(Video).filter(Video.created_at >= now - timedelta(hours=48)).count()

    return {
        "series": list(by_day.values()),
        "users_total": db.query(User).count(),
        "videos_last_48h": videos_48h,
    }


@router.get("/videos")
def admin_list_videos(q: Optional[str] = None, status_filter: Optional[str] = None, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    query = db.query(Video).join(Channel, Video.channel_id == Channel.id).join(User, Channel.user_id == User.id)
    if q:
        query = query.filter((Video.title.ilike(f"%{q}%")) | (User.email.ilike(f"%{q}%")) | (Channel.name.ilike(f"%{q}%")))
    if status_filter:
        query = query.filter(Video.status == status_filter)
    videos = query.order_by(Video.created_at.desc()).limit(300).all()
    from src.api.routes.videos import _video_cost_transactions
    result = []
    for v in videos:
        data = v.to_dict()
        data["channel_name"] = v.channel.name if v.channel else None
        owner = v.channel.user if v.channel else None
        data["owner_email"] = owner.email if owner else None
        data["total_credits"] = (
            -sum(t.amount for t in _video_cost_transactions(db, v, owner.id)) if owner else 0
        )
        result.append(data)
    return result


@router.get("/videos/{video_id}")
def admin_video_detail(video_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Full technical recap for one video — preview, voice/script/subtitles/
    music actually used, and an itemized credit cost breakdown — for the
    admin dashboard's video detail popup."""
    from src.api.routes.videos import _video_cost_transactions
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    channel = video.channel
    owner = channel.user if channel else None

    transactions = _video_cost_transactions(db, video, owner.id) if owner else []
    cost_items = [{"description": t.description, "credits": -t.amount, "created_at": t.created_at.isoformat() if t.created_at else None} for t in transactions]

    data = video.to_dict()
    data["channel_name"] = channel.name if channel else None
    data["owner_email"] = owner.email if owner else None
    data["voice_name"] = channel.voice_name if channel and channel.voice_id == video.voice_id else None
    data["subtitle_style"] = channel.subtitle_style if channel else None
    data["music_preference"] = channel.music_preference if channel else None
    data["image_style"] = channel.image_style if channel else None
    data["total_credits"] = sum(item["credits"] for item in cost_items)
    data["cost_items"] = cost_items
    return data


@router.delete("/videos/{video_id}")
def admin_delete_video(video_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    db.delete(video)
    db.commit()
    return {"message": "Video deleted"}


@router.get("/orders")
def admin_list_orders(admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    orders = db.query(Order).order_by(Order.created_at.desc()).limit(300).all()
    result = []
    for o in orders:
        data = o.to_dict()
        data["user_email"] = o.user.email if o.user else None
        data["plan_name"] = o.plan.name if o.plan else None
        result.append(data)
    return result


# ── Bibliothèque collaborative ──────────────────────────────────────────
# Curation of channel image libraries their owners opted to share with the
# community (see Channel.image_style.share_with_community and
# CommunityLibraryFolder in models.py). A niche's "master" library is just
# the union of every folder here with status="approved" for that niche —
# approving a second folder into a niche IS the merge, no files are copied.

@router.get("/community-library")
def admin_list_community_library(
    niche: Optional[str] = None,
    status_filter: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    query = db.query(CommunityLibraryFolder)
    if niche:
        query = query.filter(CommunityLibraryFolder.niche.ilike(f"%{niche}%"))
    if status_filter:
        query = query.filter(CommunityLibraryFolder.status == status_filter)
    folders = query.order_by(CommunityLibraryFolder.niche.asc(), CommunityLibraryFolder.created_at.desc()).limit(500).all()
    return [f.to_dict() for f in folders]


@router.get("/community-library/{folder_id}/images")
def admin_community_library_images(folder_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Lists up to 24 filenames from this folder's library dir — enough for
    the admin curation grid without loading a potentially huge folder."""
    from src.config import STORAGE_PATH
    from src.api.routes.channels import ALLOWED_LIBRARY_EXTENSIONS
    folder = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Dossier introuvable.")
    library_dir = STORAGE_PATH / "channels" / folder.channel_id / "library"
    if not library_dir.is_dir():
        return {"filenames": []}
    filenames = sorted(
        item.name for item in library_dir.iterdir()
        if item.is_file() and item.suffix.lower() in ALLOWED_LIBRARY_EXTENSIONS
    )[:24]
    return {"filenames": filenames}


@router.get("/community-library/{folder_id}/images/{filename}")
def admin_community_library_image_file(folder_id: str, filename: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse
    from src.config import STORAGE_PATH
    folder = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Dossier introuvable.")
    library_dir = (STORAGE_PATH / "channels" / folder.channel_id / "library").resolve()
    # Reject any filename that could escape the folder (path traversal) —
    # same defensive posture as validate_channel_visual_source in videos.py.
    candidate = (library_dir / filename).resolve()
    if candidate.parent != library_dir or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Image introuvable.")
    return FileResponse(candidate)


class CommunityLibraryStatusPayload(BaseModel):
    status: str  # "pending" | "approved" | "flagged"


@router.put("/community-library/{folder_id}/status")
def admin_set_community_library_status(
    folder_id: str,
    payload: CommunityLibraryStatusPayload,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    if payload.status not in ("pending", "approved", "flagged"):
        raise HTTPException(status_code=400, detail="Statut invalide.")
    folder = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Dossier introuvable.")
    folder.status = payload.status
    db.commit()
    return folder.to_dict()
