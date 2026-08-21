from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from src.db.session import get_db
from src.db.models import User, Channel, Video, Plan, Subscription, Order, ApiUsageLog
from src.utils.auth import get_current_admin
from src.utils.billing import user_has_active_subscription

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
    return data


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


@router.get("/plans")
def admin_list_plans(admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    return [p.to_dict() for p in db.query(Plan).order_by(Plan.price_fcfa.asc()).all()]


class PlanPayload(BaseModel):
    name: str
    price_fcfa: int
    duration_days: int = 30
    is_active: bool = True


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
    result = []
    for v in videos:
        data = v.to_dict()
        data["channel_name"] = v.channel.name if v.channel else None
        data["owner_email"] = v.channel.user.email if v.channel and v.channel.user else None
        result.append(data)
    return result


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
