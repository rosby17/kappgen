from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from src.db.session import get_db
from src.db.models import User, Channel, Video, Plan, Subscription, Order
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
