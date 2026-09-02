from datetime import datetime, timedelta
from pathlib import Path
import re
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session
from src.db.session import get_db
from src.db.models import User, Channel, Video, Plan, Subscription, Order, ApiUsageLog, Folder, PasswordReset, CommunityLibraryFolder, CommunityLibraryImagePlacement, HuggingFaceAccount, IzivoiceAccount
from src.utils.auth import get_current_admin
from src.utils.billing import user_has_active_subscription, get_credit_balance, credit_user, debit_credits

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _admin_video_title(video: Video) -> str:
    """Best available title, including legacy audio uploads.

    Audio submissions stored the cleaned original filename in script_text but
    left Video.title empty until later metadata generation.  Surface that
    value immediately so queued/failed audio jobs are identifiable too.
    """
    if (video.title or "").strip():
        return video.title.strip()
    if video.input_type == "audio":
        if (video.script_text or "").strip():
            return video.script_text.strip()
        if video.audio_input_path:
            stem = Path(video.audio_input_path).stem
            stem = re.sub(r"^upload_[0-9a-f-]+$", "", stem, flags=re.IGNORECASE)
            cleaned = re.sub(r"[-_]+", " ", stem).strip()
            if cleaned:
                return cleaned
    return "(sans titre)"


def _queued_video_positions(db: Session) -> tuple[dict[str, int], int]:
    active_plan_price = (
        db.query(func.coalesce(func.max(Plan.price_fcfa), 0))
        .select_from(Subscription)
        .join(Plan, Subscription.plan_id == Plan.id)
        .filter(
            Subscription.user_id == Channel.user_id,
            Subscription.status == "active",
            Subscription.expires_at > datetime.utcnow(),
        )
        .correlate(Channel)
        .scalar_subquery()
    )
    queued_ids = [row[0] for row in (
        db.query(Video.id)
        .join(Channel, Video.channel_id == Channel.id)
        .filter(Video.status == "queued")
        .order_by(active_plan_price.desc(), Video.created_at.asc(), Video.id.asc())
        .all()
    )]
    return {video_id: index + 1 for index, video_id in enumerate(queued_ids)}, len(queued_ids)


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
    """Adds a finite credit balance without granting unlimited access.

    Subscription access is intentionally managed by the separate
    grant-subscription endpoint. Combining the two made an admin credit
    adjustment silently create a century-long unrestricted subscription.
    """
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Le montant doit être positif.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    note = f"Crédit admin ({admin.email}){': ' + payload.note if payload.note else ''}"
    credit_user(db, user, payload.amount, 36500, note, transaction_type="admin_grant")
    return {
        "credit_balance": get_credit_balance(db, user),
        "has_active_subscription": user_has_active_subscription(db, user),
    }


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
    from src.utils.plan_catalog import ensure_sales_catalog
    return [p.to_dict() for p in ensure_sales_catalog(db)]


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
    raise HTTPException(status_code=405, detail="Le catalogue des offres est géré par l'application.")


@router.put("/plans/{plan_id}")
def update_plan(plan_id: str, payload: PlanPayload, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    raise HTTPException(status_code=405, detail="Le catalogue des offres est géré par l'application.")


@router.delete("/plans/{plan_id}")
def delete_plan(plan_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    raise HTTPException(status_code=405, detail="Les offres permanentes ne peuvent pas être désactivées.")


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
        query = query.filter(
            (Video.title.ilike(f"%{q}%")) |
            ((Video.input_type == "audio") & (Video.script_text.ilike(f"%{q}%"))) |
            (User.email.ilike(f"%{q}%")) |
            (Channel.name.ilike(f"%{q}%"))
        )
    if status_filter:
        query = query.filter(Video.status == status_filter)
    videos = query.order_by(Video.created_at.desc()).limit(300).all()
    from src.api.routes.videos import _video_cost_transactions
    queue_positions, queue_total = _queued_video_positions(db)
    result = []
    for v in videos:
        data = v.to_dict()
        data["display_title"] = _admin_video_title(v)
        data["queue_position"] = queue_positions.get(v.id)
        data["queue_total"] = queue_total
        data["channel_name"] = v.channel.name if v.channel else None
        data["youtube_connected"] = bool(v.channel and v.channel.youtube_refresh_token)
        data["youtube_channel_handle"] = v.channel.youtube_channel_handle if v.channel else None
        data["youtube_channel_id"] = v.channel.youtube_channel_id if v.channel else None
        owner = v.channel.user if v.channel else None
        data["owner_email"] = owner.email if owner else None
        data["total_credits"] = (
            -sum(t.amount for t in _video_cost_transactions(db, v, owner.id)) if owner else 0
        )
        # A video costing 0 KappGen credits usually isn't a billing gap — see
        # debit_izivoice_usage: a creator who connected their own Izivoice key
        # is charged nothing here because they're already paying Izivoice
        # directly for that call (avoids double-billing them). Surfaced
        # explicitly so "0 credits" in this list reads as "billed via their
        # own key" instead of looking like every render silently slipped
        # through free.
        data["owner_has_own_izivoice_key"] = bool(owner and owner.izivoice_api_key_encrypted)
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
    data["display_title"] = _admin_video_title(video)
    queue_positions, queue_total = _queued_video_positions(db)
    data["queue_position"] = queue_positions.get(video.id)
    data["queue_total"] = queue_total
    data["channel_name"] = channel.name if channel else None
    data["owner_email"] = owner.email if owner else None
    # Whether/where this video's channel is actually published — an admin
    # investigating a video (e.g. "is this really auto-generated or did the
    # creator write it themselves?") also wants to know if it even has a
    # real destination, not just how it was produced.
    data["youtube_connected"] = bool(channel and channel.youtube_refresh_token)
    data["youtube_channel_handle"] = channel.youtube_channel_handle if channel else None
    data["youtube_channel_title"] = channel.youtube_channel_title if channel else None
    data["youtube_channel_id"] = channel.youtube_channel_id if channel else None
    data["voice_name"] = channel.voice_name if channel and channel.voice_id == video.voice_id else None
    data["subtitle_style"] = channel.subtitle_style if channel else None
    data["music_preference"] = channel.music_preference if channel else None
    data["image_style"] = channel.image_style if channel else None
    data["total_credits"] = sum(item["credits"] for item in cost_items)
    data["cost_items"] = cost_items
    return data


@router.post("/videos/{video_id}/retry")
def admin_retry_video(video_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Retry a creator's failed video while enforcing that creator's billing."""
    from src.utils.billing import user_can_render, estimate_video_cost_credits
    from src.models.project import VideoStatus

    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.status != VideoStatus.FAILED.value:
        raise HTTPException(status_code=409, detail="Seules les vidéos en échec peuvent être relancées.")
    channel = video.channel
    owner = channel.user if channel else None
    if not channel or not owner:
        raise HTTPException(status_code=409, detail="La vidéo n'a plus de chaîne ou de propriétaire valide.")

    estimated_cost = estimate_video_cost_credits(
        script_char_count=len((video.script_text or "").strip()) if video.input_type == "text" else 0,
        estimated_duration_seconds=video.estimated_duration_seconds or video.duration_seconds or 0,
        transcribe_audio=bool(video.transcribe_audio),
        image_style=channel.image_style,
        music_preference=channel.music_preference,
    )
    can_render, reason = user_can_render(db, owner, estimated_cost)
    if not can_render:
        raise HTTPException(status_code=402, detail=f"Impossible de relancer pour {owner.email} : {reason}")

    video.error_message = None
    video.finished_at = None
    video.progress_percent = 0
    if not (video.script_text or "").strip() and channel.automation_mode == "auto" and channel.content_type != "music":
        from threading import Thread
        from src.worker.queue_runner import retry_auto_video_script_background
        video.status = VideoStatus.RENDERING.value
        video.progress_stage = "Régénération du script"
        db.commit()
        Thread(target=retry_auto_video_script_background, args=(video.id,), daemon=True).start()
    else:
        video.status = VideoStatus.QUEUED.value
        video.progress_stage = "En attente du moteur de rendu"
        db.commit()
    db.refresh(video)
    data = video.to_dict()
    data["display_title"] = _admin_video_title(video)
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

# Mirrors the frontend's NICHE_OPTIONS (App.jsx) — kept as a separate literal
# rather than a shared import since the two projects don't share a package;
# used only to make sure every niche shows up as a browsable folder in the
# admin overview below even before anyone has uploaded anything into it.
KNOWN_NICHES = [
    "Philosophie", "Philosophie Stoïcienne", "Philosophie de Machiavel", "Philosophie de Napoleon Hill",
    "Stoïcisme", "Spiritualité", "Prière", "Méditation", "Bouddhisme", "Islam",
    "Mythologie", "Histoires Antiques", "Histoire Africaine", "Histoire Européenne", "Histoire",
    "Développement Personnel", "Motivation", "Récits Captivants", "Psychologie", "Finance", "Business",
    "Santé & Bien-être", "Football", "Sport", "Science", "Faits Divers", "True Crime", "Voyage", "Cuisine",
    "Astuces Maison",
]


@router.get("/community-library/overview")
def admin_community_library_overview(admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Full admin oversight of every user's channel image library, organized
    niche -> user -> channel — independent of whether a creator opted into
    community sharing (CommunityLibraryFolder only covers those). Every known
    niche always appears, even empty, so the admin can browse/organize before
    a single image has landed in it. Existing CommunityLibraryFolder rows
    (if any) are joined in to surface each folder's sharing status.

    A folder always lives under its channel's own niche here, regardless of
    how individual images inside it have been reassigned to other niches'
    pools via "Déplacer vers…"/"Fusionner avec…" — those only change which
    niche's community pool an image feeds during generation
    (CommunityLibraryImagePlacement), not where the folder itself is found
    when browsing. Moving every image out of a folder must never make that
    folder disappear from its own niche."""
    shared_by_channel = {f.channel_id: f for f in db.query(CommunityLibraryFolder).all()}

    niches: dict = {n: {"niche": n, "total_images": 0, "users": {}} for n in KNOWN_NICHES}
    grand_total = 0

    channels_with_library = (
        db.query(Channel)
        .filter(Channel.image_style.isnot(None))
        .all()
    )
    for channel in channels_with_library:
        shared = shared_by_channel.get(channel.id)
        # image_style.library_image_count only reflects a manual upload
        # (POST .../library-images) — it's never touched by images
        # auto-accumulating into the channel's library from AI generation
        # (see _persist_generated_images_to_channel_library in images.py),
        # which instead keeps CommunityLibraryFolder.image_count current.
        # A manually-uploaded-but-never-shared channel has no
        # CommunityLibraryFolder row at all (deleted/never created by
        # _sync_community_library_folder when share_with_community is
        # false), so neither field alone is reliable — take whichever is
        # higher. Without this, a channel that only ever grew its library
        # automatically (never manually uploaded) was invisible here
        # entirely, niche included, however many images it actually had.
        count = max(
            int((channel.image_style or {}).get("library_image_count") or 0),
            shared.image_count if shared else 0,
        )
        if count <= 0:
            continue
        grand_total += count
        home_niche = channel.niche or "Sans niche"

        owner = channel.user
        bucket = niches.setdefault(home_niche, {"niche": home_niche, "total_images": 0, "users": {}})
        bucket["total_images"] += count
        user_key = channel.user_id or "unknown"
        user_bucket = bucket["users"].setdefault(user_key, {
            "user_id": channel.user_id,
            "user_email": owner.email if owner else "Utilisateur supprimé",
            "total_images": 0,
            "folders": [],
        })
        user_bucket["total_images"] += count
        user_bucket["folders"].append({
            "channel_id": channel.id,
            "channel_name": channel.name,
            "image_count": count,
            "community_folder_id": shared.id if shared else None,
            "share_status": shared.status if shared else "not_shared",
        })
        # Newest contributing channel's created_at, as a stand-in for "most
        # recently active niche" — lets the admin sort by recency instead of
        # only by image count or alphabetically.
        channel_created = channel.created_at.isoformat() if channel.created_at else None
        if channel_created and (bucket.get("latest_activity") is None or channel_created > bucket["latest_activity"]):
            bucket["latest_activity"] = channel_created

    niche_list = []
    for bucket in niches.values():
        bucket["users"] = sorted(bucket["users"].values(), key=lambda u: u["total_images"], reverse=True)
        bucket.setdefault("latest_activity", None)
        niche_list.append(bucket)
    niche_list.sort(key=lambda n: (-n["total_images"], n["niche"]))

    return {"total_images": grand_total, "niches": niche_list}


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


@router.post("/community-library/resync/{channel_id}")
def admin_resync_community_library_folder(channel_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Recomputes a channel's CommunityLibraryFolder row from what's actually
    on disk and upserts it — a repair tool for the drift this niche folders
    can silently fall into: _persist_generated_images_to_channel_library
    copies each freshly-generated image to disk first, then syncs the DB row
    in a separate try/except that only logs on failure (never surfaced to a
    creator or retried) — a single failed commit there (e.g. two renders'
    background threads racing to INSERT the very first row for a channel at
    once) leaves every image after that point uncounted, and the whole niche
    silently invisible in the overview (admin_community_library_overview
    skips any channel whose count comes back <=0). Confirmed live: a channel
    with 53 real generated_*.png files on disk had zero DB record of any of
    them. Safe to call anytime — always resets to the true file count,
    never guesses or accumulates."""
    from src.config import STORAGE_PATH
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    library_dir = STORAGE_PATH / "channels" / channel_id / "library"
    real_count = len([f for f in library_dir.iterdir() if f.is_file()]) if library_dir.is_dir() else 0
    folder = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.channel_id == channel_id).first()
    if real_count <= 0:
        return {"channel_id": channel_id, "image_count": 0, "folder_id": folder.id if folder else None, "changed": False}
    if folder:
        changed = folder.image_count != real_count
        folder.image_count = real_count
        folder.niche = channel.niche or folder.niche
    else:
        changed = True
        folder = CommunityLibraryFolder(
            channel_id=channel_id,
            user_id=channel.user_id,
            niche=channel.niche or "General",
            image_count=real_count,
            status="approved",
        )
        db.add(folder)
    db.commit()
    db.refresh(folder)
    return {"channel_id": channel_id, "image_count": real_count, "folder_id": folder.id, "changed": changed}


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


@router.delete("/community-library/{folder_id}/images/{filename}")
def admin_delete_community_library_image(folder_id: str, filename: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Removes one specific image an admin judges unfit for its niche/the
    community, without flagging (or otherwise touching) the whole channel's
    folder — most images from an auto-shared channel are perfectly fine, so
    the fix for one bad one is deleting that one, not blocking everything
    else the channel has already contributed or will contribute."""
    from src.config import STORAGE_PATH
    folder = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.id == folder_id).first()
    if not folder:
        raise HTTPException(status_code=404, detail="Dossier introuvable.")
    library_dir = (STORAGE_PATH / "channels" / folder.channel_id / "library").resolve()
    # Same path-traversal guard as the image-serving route above.
    candidate = (library_dir / filename).resolve()
    if candidate.parent != library_dir or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Image introuvable.")
    candidate.unlink()
    db.query(CommunityLibraryImagePlacement).filter(
        CommunityLibraryImagePlacement.channel_id == folder.channel_id,
        CommunityLibraryImagePlacement.filename == filename,
    ).delete(synchronize_session=False)
    folder.image_count = max(0, folder.image_count - 1)
    db.commit()
    return {"deleted": True, "image_count": folder.image_count}


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


# --- Admin oversight of a channel's library regardless of its owner's own
# sharing choice — the community-library/* routes above only ever work for
# channels that already have a CommunityLibraryFolder row (the owner opted
# in). The admin needs to browse ANY channel's images and force sharing on,
# independent of what the creator picked — final say stays with the admin.

@router.get("/channel-library/{channel_id}/images")
def admin_channel_library_images(
    channel_id: str,
    niche: Optional[str] = None,
    offset: int = 0,
    limit: int = 60,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    from src.config import STORAGE_PATH
    from src.api.routes.channels import ALLOWED_LIBRARY_EXTENSIONS
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    library_dir = STORAGE_PATH / "channels" / channel_id / "library"
    if not library_dir.is_dir():
        return {"filenames": [], "total": 0, "offset": max(0, offset), "has_more": False}
    raw_filenames = sorted(
        item.name for item in library_dir.iterdir()
        if item.is_file() and item.suffix.lower() in ALLOWED_LIBRARY_EXTENSIONS
    )
    placements = {
        row.filename: row.niche
        for row in db.query(CommunityLibraryImagePlacement).filter(
            CommunityLibraryImagePlacement.channel_id == channel_id
        ).all()
    }
    default_niche = channel.niche or "Sans niche"
    all_filenames = [
        filename for filename in raw_filenames
        if not niche or placements.get(filename, default_niche) == niche
    ]
    offset = max(0, offset)
    limit = max(1, min(limit, 120))
    filenames = all_filenames[offset:offset + limit]
    return {
        "filenames": filenames,
        "total": len(all_filenames),
        "offset": offset,
        "has_more": offset + len(filenames) < len(all_filenames),
    }


@router.get("/community-library/niche/{niche}/images")
def admin_niche_library_images(
    niche: str,
    offset: int = 0,
    limit: int = 60,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Every image effectively belonging to this niche, across every
    contributing channel — for browsing a whole niche's pool in one flat
    grid instead of drilling into each user then each channel individually,
    which gets unwieldy as more creators contribute. Same "effective niche"
    rule as the render-time pool (_approved_community_library_files in
    images.py) — a channel's own niche, unless a specific image was
    reassigned elsewhere via CommunityLibraryImagePlacement — except this
    includes every channel with images, not just admin-approved ones,
    matching what the per-channel drill-down already shows."""
    from src.config import STORAGE_PATH
    from src.api.routes.channels import ALLOWED_LIBRARY_EXTENSIONS

    channels = db.query(Channel).filter(Channel.image_style.isnot(None)).all()
    placements_by_channel = {}
    for row in db.query(CommunityLibraryImagePlacement).filter(CommunityLibraryImagePlacement.niche == niche).all():
        placements_by_channel.setdefault(row.channel_id, set()).add(row.filename)
    reassigned_out = {}  # channel_id -> {filenames reassigned to a DIFFERENT niche}
    for row in db.query(CommunityLibraryImagePlacement).filter(CommunityLibraryImagePlacement.niche != niche).all():
        reassigned_out.setdefault(row.channel_id, set()).add(row.filename)

    entries = []  # [{channel_id, channel_name, filename}]
    for channel in channels:
        library_dir = STORAGE_PATH / "channels" / channel.id / "library"
        if not library_dir.is_dir():
            continue
        placed_in = placements_by_channel.get(channel.id, set())
        placed_out = reassigned_out.get(channel.id, set())
        home_matches = (channel.niche or "Sans niche") == niche
        for item in sorted(library_dir.iterdir(), key=lambda p: p.name):
            if not (item.is_file() and item.suffix.lower() in ALLOWED_LIBRARY_EXTENSIONS):
                continue
            effectively_here = item.name in placed_in or (home_matches and item.name not in placed_out)
            if effectively_here:
                entries.append({"channel_id": channel.id, "channel_name": channel.name, "filename": item.name})

    offset = max(0, offset)
    limit = max(1, min(limit, 120))
    page = entries[offset:offset + limit]
    return {
        "images": page,
        "total": len(entries),
        "offset": offset,
        "has_more": offset + len(page) < len(entries),
    }


@router.get("/channel-library/{channel_id}/images/{filename}")
def admin_channel_library_image_file(channel_id: str, filename: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    from fastapi.responses import FileResponse
    from src.config import STORAGE_PATH
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    library_dir = (STORAGE_PATH / "channels" / channel_id / "library").resolve()
    candidate = (library_dir / filename).resolve()
    if candidate.parent != library_dir or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Image introuvable.")
    return FileResponse(candidate)


@router.delete("/channel-library/{channel_id}/images/{filename}")
def admin_delete_channel_library_image(channel_id: str, filename: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    from src.config import STORAGE_PATH
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    library_dir = (STORAGE_PATH / "channels" / channel_id / "library").resolve()
    candidate = (library_dir / filename).resolve()
    if candidate.parent != library_dir or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Image introuvable.")
    candidate.unlink()
    db.query(CommunityLibraryImagePlacement).filter(
        CommunityLibraryImagePlacement.channel_id == channel_id,
        CommunityLibraryImagePlacement.filename == filename,
    ).delete(synchronize_session=False)
    image_style = dict(channel.image_style or {})
    new_count = max(0, int(image_style.get("library_image_count") or 0) - 1)
    image_style["library_image_count"] = new_count
    channel.image_style = image_style
    folder = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.channel_id == channel_id).first()
    if folder:
        folder.image_count = new_count
    db.commit()
    return {"deleted": True, "image_count": new_count}


class AdminForceSharePayload(BaseModel):
    status: str = "approved"  # what to set the folder's curation status to


class AdminMoveChannelLibraryPayload(BaseModel):
    niche: str
    filenames: list[str]


class AdminLibraryLabelPayload(BaseModel):
    label: str


@router.put("/channel-library/{channel_id}/label")
def admin_rename_channel_library(
    channel_id: str,
    payload: AdminLibraryLabelPayload,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Renames the channel itself from the library browser — the folder
    label IS the channel name (single source of truth), so this is a plain
    channel rename, visible to the creator everywhere else too."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    name = payload.label.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Le nom ne peut pas être vide.")
    channel.name = name
    db.commit()
    return {"channel_id": channel.id, "channel_name": channel.name}


@router.put("/channel-library/{channel_id}/niche")
def admin_move_channel_library(
    channel_id: str,
    payload: AdminMoveChannelLibraryPayload,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Reclassifies selected collaborative images without renaming the
    channel, changing its production niche, or moving physical files."""
    target_niche = payload.niche.strip()
    if target_niche not in KNOWN_NICHES:
        raise HTTPException(status_code=400, detail="Niche de destination invalide.")
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")

    filenames = list(dict.fromkeys(name.strip() for name in payload.filenames if name.strip()))
    if not filenames or len(filenames) > 5000:
        raise HTTPException(status_code=400, detail="Sélectionnez entre 1 et 5 000 images.")
    from src.config import STORAGE_PATH
    library_dir = (STORAGE_PATH / "channels" / channel_id / "library").resolve()
    for filename in filenames:
        candidate = (library_dir / filename).resolve()
        if candidate.parent != library_dir or not candidate.is_file():
            raise HTTPException(status_code=404, detail=f"Image introuvable : {filename}")
        placement = db.query(CommunityLibraryImagePlacement).filter(
            CommunityLibraryImagePlacement.channel_id == channel_id,
            CommunityLibraryImagePlacement.filename == filename,
        ).first()
        if placement:
            placement.niche = target_niche
        else:
            db.add(CommunityLibraryImagePlacement(channel_id=channel_id, filename=filename, niche=target_niche))
    db.commit()
    return {
        "moved": True,
        "channel_id": channel.id,
        "channel_name": channel.name,
        "niche": target_niche,
        "image_count": len(filenames),
    }


class AdminMergeFolderPayload(BaseModel):
    niche: str


@router.put("/channel-library/{channel_id}/merge")
def admin_merge_channel_library(
    channel_id: str,
    payload: AdminMergeFolderPayload,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """One-click "fusionner ce dossier avec..." — moves this channel's ENTIRE
    library into the target niche's pool, same mechanism as the per-image
    move above (a CommunityLibraryImagePlacement per file) but without the
    client having to enumerate filenames first (that endpoint's own preview
    list caps at 24, nowhere near enough for a folder of hundreds). No files
    are copied or relocated — this channel's images still live in its own
    folder, just now counted under the target niche alongside whatever
    other channel(s) already feed it, which is exactly what "merging into
    one niche pool" means in this design (see the module docstring above)."""
    target_niche = payload.niche.strip()
    if target_niche not in KNOWN_NICHES:
        raise HTTPException(status_code=400, detail="Niche de destination invalide.")
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")

    from src.config import STORAGE_PATH
    from src.api.routes.channels import ALLOWED_LIBRARY_EXTENSIONS
    library_dir = STORAGE_PATH / "channels" / channel_id / "library"
    if not library_dir.is_dir():
        return {"merged": True, "channel_id": channel.id, "niche": target_niche, "image_count": 0}
    filenames = [f.name for f in library_dir.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_LIBRARY_EXTENSIONS]

    existing_placements = {
        p.filename: p for p in db.query(CommunityLibraryImagePlacement).filter(
            CommunityLibraryImagePlacement.channel_id == channel_id
        ).all()
    }
    for filename in filenames:
        placement = existing_placements.get(filename)
        if placement:
            placement.niche = target_niche
        else:
            db.add(CommunityLibraryImagePlacement(channel_id=channel_id, filename=filename, niche=target_niche))
    db.commit()
    return {"merged": True, "channel_id": channel.id, "channel_name": channel.name, "niche": target_niche, "image_count": len(filenames)}


@router.post("/channel-library/{channel_id}/force-share")
def admin_force_share_channel_library(
    channel_id: str,
    payload: AdminForceSharePayload,
    admin: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Admin override: puts this channel's library into the community
    curation table regardless of the owner's own share_with_community
    setting — the admin has final say on what feeds a niche's shared pool,
    the creator's toggle is just the default/starting point, not a hard
    block. Does NOT flip the channel's own flag (so the creator's Studio UI
    keeps showing their real preference); this only affects what shows up
    in the niche's merged library."""
    if payload.status not in ("pending", "approved", "flagged"):
        raise HTTPException(status_code=400, detail="Statut invalide.")
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    count = int((channel.image_style or {}).get("library_image_count") or 0)
    existing = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.channel_id == channel_id).first()
    if existing:
        existing.status = payload.status
        existing.image_count = count
        existing.niche = channel.niche
        folder = existing
    else:
        folder = CommunityLibraryFolder(
            channel_id=channel.id, user_id=channel.user_id, niche=channel.niche,
            image_count=count, status=payload.status,
        )
        db.add(folder)
    db.commit()
    db.refresh(folder)
    return folder.to_dict()


@router.post("/channel-library/{channel_id}/unshare")
def admin_unshare_channel_library(channel_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Reverses force-share — removes this channel from the community
    curation table entirely (back to "non partagé" in the admin view)."""
    folder = db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.channel_id == channel_id).first()
    if folder:
        db.delete(folder)
        db.commit()
    return {"unshared": True}


@router.delete("/channels/{channel_id}")
def admin_delete_channel(channel_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Admin-side channel delete — for cleaning up a creator's own mistake
    (e.g. a duplicate channel made by accident) directly from the library
    curation view, without needing the creator's own session. Same cascade
    as the creator-facing DELETE /channels/{id} (channels.py): Video cascades
    via the ORM relationship, but ApiUsageLog/VoiceCloneJob/CommunityLibraryFolder
    have a channel_id foreign key with no cascade defined, so they're cleared
    explicitly first or this 500s on the FK violation instead of deleting."""
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Chaîne introuvable.")
    from src.db.models import VoiceCloneJob
    db.query(ApiUsageLog).filter(ApiUsageLog.channel_id == channel_id).delete()
    db.query(VoiceCloneJob).filter(VoiceCloneJob.channel_id == channel_id).delete()
    db.query(CommunityLibraryFolder).filter(CommunityLibraryFolder.channel_id == channel_id).delete()
    db.delete(channel)
    db.commit()
    return {"deleted": True}


# --- Hugging Face free-tier image generation accounts ------------------------
# Admin-managed pool of accounts for the free FLUX.1-schnell path (see
# src/pipeline/images.py) — lets new accounts keep being added over time
# without a redeploy, and shows which ones are currently working vs
# quota-exhausted/invalid.

@router.get("/hf-accounts")
def list_hf_accounts(admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    accounts = db.query(HuggingFaceAccount).order_by(HuggingFaceAccount.created_at.asc()).all()
    return [a.to_dict() for a in accounts]


class HfAccountPayload(BaseModel):
    token: str
    label: Optional[str] = None


def _test_hf_token(token: str) -> tuple[str, Optional[str]]:
    """Fires one real (cheap) generation request to classify the token as
    active/quota_exhausted/invalid. Returns (status, error_message)."""
    import httpx
    try:
        resp = httpx.post(
            "https://router.huggingface.co/nscale/v1/images/generations",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"prompt": "a simple test image", "model": "black-forest-labs/FLUX.1-schnell"},
            timeout=60.0,
        )
        if resp.status_code == 200:
            return "active", None
        if resp.status_code in (401, 403):
            return "invalid", resp.text[:300]
        if resp.status_code in (402, 429):
            return "quota_exhausted", resp.text[:300]
        return "invalid", f"HTTP {resp.status_code}: {resp.text[:300]}"
    except Exception as exc:
        return "invalid", str(exc)[:300]


@router.post("/hf-accounts")
def add_hf_account(payload: HfAccountPayload, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    token = payload.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Le token ne peut pas être vide.")
    if db.query(HuggingFaceAccount).filter(HuggingFaceAccount.token == token).first():
        raise HTTPException(status_code=400, detail="Ce token est déjà enregistré.")

    status, error = _test_hf_token(token)
    account = HuggingFaceAccount(
        token=token,
        label=(payload.label or "").strip() or None,
        status=status,
        last_checked_at=datetime.utcnow(),
        last_error=error,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account.to_dict()


@router.post("/hf-accounts/{account_id}/check")
def check_hf_account(account_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    account = db.query(HuggingFaceAccount).filter(HuggingFaceAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Compte introuvable.")
    status, error = _test_hf_token(account.token)
    account.status = status
    account.last_error = error
    account.last_checked_at = datetime.utcnow()
    db.commit()
    db.refresh(account)
    return account.to_dict()


@router.patch("/hf-accounts/{account_id}")
def update_hf_account(account_id: str, is_enabled: Optional[bool] = None, label: Optional[str] = None, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    account = db.query(HuggingFaceAccount).filter(HuggingFaceAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Compte introuvable.")
    if is_enabled is not None:
        account.is_enabled = is_enabled
    if label is not None:
        account.label = label.strip() or None
    db.commit()
    db.refresh(account)
    return account.to_dict()


@router.delete("/hf-accounts/{account_id}")
def delete_hf_account(account_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    account = db.query(HuggingFaceAccount).filter(HuggingFaceAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Compte introuvable.")
    db.delete(account)
    db.commit()
    return {"deleted": True}


# --- Izivoice shared accounts (voiceover / music / images) -------------------
# Admin-managed pool of shared Izivoice API keys, same pattern as the Hugging
# Face pool above — see src/utils/izivoice_pool.py. Only affects the default
# key used when a creator hasn't connected their own personal Izivoice key.

@router.get("/izivoice-accounts")
def list_izivoice_accounts(admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    accounts = db.query(IzivoiceAccount).order_by(IzivoiceAccount.created_at.asc()).all()
    return [a.to_dict() for a in accounts]


class IzivoiceAccountPayload(BaseModel):
    token: str
    label: Optional[str] = None


def _test_izivoice_token(token: str) -> tuple[str, Optional[str]]:
    """A cheap read-only call (list voices) to classify the token as
    active/quota_exhausted/invalid — no generation cost incurred."""
    import httpx
    from src.config import IZIVOICE_BASE_URL
    try:
        resp = httpx.get(
            f"{IZIVOICE_BASE_URL}/voices",
            headers={"Authorization": f"Bearer {token}"},
            params={"page": 0, "page_size": 1},
            timeout=15.0,
        )
        if resp.status_code == 200:
            return "active", None
        if resp.status_code in (401, 403):
            return "invalid", resp.text[:300]
        if resp.status_code in (402, 429):
            return "quota_exhausted", resp.text[:300]
        return "invalid", f"HTTP {resp.status_code}: {resp.text[:300]}"
    except Exception as exc:
        return "invalid", str(exc)[:300]


@router.post("/izivoice-accounts")
def add_izivoice_account(payload: IzivoiceAccountPayload, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    token = payload.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Le token ne peut pas être vide.")
    if db.query(IzivoiceAccount).filter(IzivoiceAccount.token == token).first():
        raise HTTPException(status_code=400, detail="Ce token est déjà enregistré.")

    status, error = _test_izivoice_token(token)
    account = IzivoiceAccount(
        token=token,
        label=(payload.label or "").strip() or None,
        status=status,
        last_checked_at=datetime.utcnow(),
        last_error=error,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account.to_dict()


@router.post("/izivoice-accounts/{account_id}/check")
def check_izivoice_account(account_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    account = db.query(IzivoiceAccount).filter(IzivoiceAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Compte introuvable.")
    status, error = _test_izivoice_token(account.token)
    account.status = status
    account.last_error = error
    account.last_checked_at = datetime.utcnow()
    db.commit()
    db.refresh(account)
    return account.to_dict()


@router.patch("/izivoice-accounts/{account_id}")
def update_izivoice_account(account_id: str, is_enabled: Optional[bool] = None, label: Optional[str] = None, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    account = db.query(IzivoiceAccount).filter(IzivoiceAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Compte introuvable.")
    if is_enabled is not None:
        account.is_enabled = is_enabled
    if label is not None:
        account.label = label.strip() or None
    db.commit()
    db.refresh(account)
    return account.to_dict()


@router.delete("/izivoice-accounts/{account_id}")
def delete_izivoice_account(account_id: str, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    account = db.query(IzivoiceAccount).filter(IzivoiceAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Compte introuvable.")
    db.delete(account)
    db.commit()
    return {"deleted": True}


# --- Global image-generation provider switches ------------------------------
# Admin-controlled, no redeploy needed — lets the operator pick which
# thumbnail image providers are used and in what priority order: Hugging
# Face (free), fal.ai (paid, best fidelity to a reference image), Izivoice
# (paid). A provider left out of the order is never called — including only
# "huggingface" keeps thumbnails 100% free; adding fal/izivoice is an
# explicit opt-in to spend money on them, in whichever order is chosen.

THUMBNAIL_IMAGE_PROVIDERS = ["huggingface", "fal", "izivoice"]


@router.get("/settings/thumbnail-provider-mode")
def get_thumbnail_provider_mode(admin: User = Depends(get_current_admin)):
    from src.utils.app_settings import thumbnail_provider_order
    from src.config import FAL_API_KEY, IZIVOICE_API_KEY
    configured = {"huggingface": True, "fal": bool(FAL_API_KEY), "izivoice": bool(IZIVOICE_API_KEY)}
    order = thumbnail_provider_order()
    return {"order": order, "available": THUMBNAIL_IMAGE_PROVIDERS, "configured": configured}


class ThumbnailProviderOrderPayload(BaseModel):
    order: List[str]


@router.patch("/settings/thumbnail-provider-mode")
def set_thumbnail_provider_mode(payload: ThumbnailProviderOrderPayload, admin: User = Depends(get_current_admin)):
    cleaned = []
    for p in payload.order:
        if p not in THUMBNAIL_IMAGE_PROVIDERS:
            raise HTTPException(status_code=400, detail=f"Fournisseur invalide : {p}")
        if p not in cleaned:
            cleaned.append(p)
    from src.utils.app_settings import set_thumbnail_provider_order
    set_thumbnail_provider_order(cleaned)
    return {"order": cleaned}


# --- AI text-generation provider switch --------------------------------
# Which of Anthropic/DeepSeek/fal.ai/OpenAI/Groq (see src/pipeline/ai_text.py)
# is tried FIRST for every text-generation call (script writing, topic
# selection, titles, thumbnail concepts, music style suggestions...). The
# rest of the chain still runs as automatic fallback behind it. Built for
# exactly this situation: an exhausted Anthropic balance with no time to
# redeploy — flip to a configured provider from the "Ressources" tab and
# every call picks it up immediately, no restart needed.
AI_TEXT_PROVIDERS = ["anthropic", "deepseek", "fal", "openai", "groq"]


@router.get("/settings/ai-text-provider")
def get_ai_text_provider(admin: User = Depends(get_current_admin)):
    from src.utils.app_settings import ai_text_provider_order
    from src.config import ANTHROPIC_API_KEY, DEEPSEEK_API_KEY, FAL_API_KEY, OPENAI_API_KEY, GROQ_API_KEY
    configured = {
        "anthropic": bool(ANTHROPIC_API_KEY),
        "deepseek": bool(DEEPSEEK_API_KEY),
        "fal": bool(FAL_API_KEY),
        "openai": bool(OPENAI_API_KEY),
        "groq": bool(GROQ_API_KEY),
    }
    custom_order = [p for p in ai_text_provider_order() if p in AI_TEXT_PROVIDERS]
    # Providers not explicitly ranked by the admin still trail behind, in the
    # module's default order, so the "available" list always covers all five.
    full_order = custom_order + [p for p in AI_TEXT_PROVIDERS if p not in custom_order]
    return {"order": custom_order, "available": AI_TEXT_PROVIDERS, "effective_order": full_order, "configured": configured}


class AiTextProviderPayload(BaseModel):
    order: List[str]


@router.patch("/settings/ai-text-provider")
def set_ai_text_provider(payload: AiTextProviderPayload, admin: User = Depends(get_current_admin)):
    cleaned = []
    for p in payload.order:
        if p not in AI_TEXT_PROVIDERS:
            raise HTTPException(status_code=400, detail=f"Fournisseur invalide : {p}")
        if p not in cleaned:
            cleaned.append(p)
    from src.utils.app_settings import set_ai_text_provider_order
    set_ai_text_provider_order(cleaned)
    return {"order": cleaned}
