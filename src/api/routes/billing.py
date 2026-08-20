import secrets
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from src.db.session import get_db
from src.db.models import Plan, Subscription, Order, User
from src.utils.auth import get_current_user
from src.config import TARA_WEBHOOK_SECRET
from src.pipeline.payments import (
    create_maketou_checkout, poll_maketou_order, create_tarapay_checkout,
)

router = APIRouter(prefix="/api/billing", tags=["billing"])


@router.get("/plans")
def list_plans(db: Session = Depends(get_db)):
    plans = db.query(Plan).filter(Plan.is_active == True).order_by(Plan.price_fcfa.asc()).all()  # noqa: E712
    return [p.to_dict() for p in plans]


class CheckoutPayload(BaseModel):
    plan_id: str
    provider: str  # "maketou" | "tarapay"


@router.post("/checkout")
def create_checkout(payload: CheckoutPayload, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    plan = db.query(Plan).filter(Plan.id == payload.plan_id, Plan.is_active == True).first()  # noqa: E712
    if not plan:
        raise HTTPException(status_code=404, detail="Offre introuvable.")
    if payload.provider not in ("maketou", "tarapay"):
        raise HTTPException(status_code=400, detail="Fournisseur de paiement inconnu.")

    order = Order(
        user_id=current_user.id,
        plan_id=plan.id,
        provider=payload.provider,
        amount_fcfa=plan.price_fcfa,
        status="pending",
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    try:
        if payload.provider == "maketou":
            result = create_maketou_checkout(order.id, plan.price_fcfa, current_user.email, current_user.name)
            order.provider_ref = result.get("provider_ref")
            redirect_url = result.get("redirect_url")
        else:
            result = create_tarapay_checkout(order.id, plan.price_fcfa, plan.name)
            redirect_url = result.get("redirect_url")
    except Exception as exc:
        order.status = "failed"
        db.commit()
        raise HTTPException(status_code=502, detail=f"Impossible de créer le paiement : {exc}")

    if not redirect_url:
        order.status = "failed"
        db.commit()
        raise HTTPException(status_code=502, detail="Le fournisseur de paiement n'a pas renvoyé de lien de paiement.")

    db.commit()
    return {"order_id": order.id, "redirect_url": redirect_url}


def _activate_subscription(db: Session, order: Order) -> Subscription:
    """Creates the subscription tied to a just-paid order. Extends from
    'now' rather than stacking onto an existing expiry — simple, matches a
    single-active-subscription-per-user model."""
    plan = order.plan
    sub = Subscription(
        user_id=order.user_id,
        plan_id=plan.id,
        status="active",
        started_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=plan.duration_days),
        note=f"Payé via {order.provider} (commande {order.id})",
    )
    db.add(sub)
    db.commit()
    return sub


def _claim_order_success(db: Session, order: Order) -> bool:
    """Atomic pending->success claim so a webhook, a manual verify call, and
    the reverify sweep can never double-activate the same order if they race."""
    updated = (
        db.query(Order)
        .filter(Order.id == order.id, Order.status != "success")
        .update({"status": "success"}, synchronize_session=False)
    )
    db.commit()
    return updated > 0


@router.get("/verify")
def verify_order(order_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Called by the frontend when the user lands back on /billing/success —
    the only confirmation path for Maketou, which has no webhook."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Commande introuvable.")
    if order.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Accès refusé.")

    if order.status == "success":
        return {"status": "success"}

    if order.provider == "maketou" and order.provider_ref:
        try:
            provider_status = poll_maketou_order(order.provider_ref)
        except Exception:
            return {"status": "pending"}
        if provider_status == "completed":
            if _claim_order_success(db, order):
                _activate_subscription(db, order)
            return {"status": "success"}

    return {"status": "pending"}


@router.post("/webhook/tarapay")
async def tarapay_webhook(request: Request, key: str = "", db: Session = Depends(get_db)):
    # Tara Money doesn't sign webhooks — the shared secret embedded in the
    # webHookUrl query param at checkout time IS the auth mechanism (matches
    # izivoice's integration exactly).
    if not TARA_WEBHOOK_SECRET or not secrets.compare_digest(key, TARA_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")

    payload = await request.json()
    order_status = str(payload.get("status") or "").upper()
    product_id = payload.get("productId")
    payment_id = payload.get("paymentId")
    amount = payload.get("amount")

    order = db.query(Order).filter(Order.id == product_id).first()
    if not order:
        return {"success": False, "message": "Order not found"}

    if order_status == "SUCCESS":
        try:
            paid_amount = float(amount)
            tolerance = max(order.amount_fcfa * 0.02, 5)
            if paid_amount < order.amount_fcfa - tolerance:
                order.status = "flagged_underpaid"
                db.commit()
                return {"success": False, "message": "Underpaid"}
        except (TypeError, ValueError):
            pass
        order.provider_ref = payment_id or product_id
        db.commit()
        if _claim_order_success(db, order):
            _activate_subscription(db, order)
        return {"success": True}

    if order_status in ("FAILED", "FAIL", "CANCELLED", "CANCELED", "EXPIRED", "ERROR", "DECLINED"):
        order.status = "failed"
        db.commit()

    return {"success": True}
