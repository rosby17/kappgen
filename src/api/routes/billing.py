import secrets
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from src.db.session import get_db
from src.db.models import Plan, Subscription, Order, User
from src.utils.auth import get_current_user
from src.utils.logger import logger
from src.utils.email import SUPPORTED_LOCALES, send_brevo_email, email_shell, EMAIL_ACCENT
from src.config import TARA_WEBHOOK_SECRET, FRONTEND_BASE_URL
from src.pipeline.payments import (
    create_maketou_checkout, poll_maketou_order, create_tarapay_checkout,
)
from src.utils.rate_limit import rate_limit

router = APIRouter(prefix="/api/billing", tags=["billing"])
_limit_checkout = rate_limit("checkout", max_attempts=20, window_seconds=3600)


@router.get("/plans")
def list_plans(db: Session = Depends(get_db)):
    from src.utils.plan_catalog import ensure_sales_catalog
    return [p.to_dict() for p in ensure_sales_catalog(db)]


@router.get("/subscription")
def my_subscription(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    sub = (
        db.query(Subscription)
        .filter(Subscription.user_id == current_user.id, Subscription.status == "active", Subscription.expires_at > datetime.utcnow())
        .order_by(Subscription.expires_at.desc())
        .first()
    )
    return {"active": sub is not None, "subscription": sub.to_dict() if sub else None}


@router.get("/credits")
def my_credit_balance(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from src.utils.billing import get_credit_balance, user_has_purchased_credits
    return {
        "balance": get_credit_balance(db, current_user),
        # Lifetime unlock for the KappGen watermark: true once this creator
        # has ever paid for a credit pack, even if their balance is now zero.
        "has_purchased_credits": user_has_purchased_credits(db, current_user),
    }


@router.get("/estimate-script-cost")
def estimate_script_cost(total_words: int, num_parts: int = 1, current_user: User = Depends(get_current_user)):
    """Preview of what an "Automatique" script of this length will cost the
    creator — shown live in the script-structure editor as they adjust
    length/parts, so they know the price before generating instead of after."""
    from src.utils.billing import estimate_script_generation_cost
    if total_words <= 0:
        return {"cost_usd": 0, "credits": 0, "fcfa": 0}
    return estimate_script_generation_cost(total_words, num_parts)


class CheckoutPayload(BaseModel):
    plan_id: str
    provider: str  # Payment method identifier returned by KappGen.
    billing_cycle: str = "monthly"


@router.post("/checkout")
def create_checkout(payload: CheckoutPayload, current_user: User = Depends(get_current_user), db: Session = Depends(get_db), _rl=Depends(_limit_checkout)):
    if not current_user.email_verified:
        raise HTTPException(status_code=403, detail="Confirme ton adresse email avant de souscrire à une offre.")
    from src.utils.plan_catalog import ensure_sales_catalog
    ensure_sales_catalog(db)
    plan = db.query(Plan).filter(Plan.id == payload.plan_id, Plan.is_active == True).first()  # noqa: E712
    if not plan:
        raise HTTPException(status_code=404, detail="Offre introuvable.")
    if payload.provider not in ("maketou", "tarapay"):
        raise HTTPException(status_code=400, detail="Fournisseur de paiement inconnu.")

    # Every catalog offer is a 30-day pack at its fixed displayed price.
    billing_cycle = "monthly"
    amount_fcfa = plan.price_fcfa

    order = Order(
        user_id=current_user.id,
        plan_id=plan.id,
        provider=payload.provider,
        amount_fcfa=amount_fcfa,
        billing_cycle=billing_cycle,
        status="pending",
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    try:
        if payload.provider == "maketou":
            result = create_maketou_checkout(order.id, order.amount_fcfa, current_user.email, current_user.name)
            order.provider_ref = result.get("provider_ref")
            redirect_url = result.get("redirect_url")
        else:
            result = create_tarapay_checkout(order.id, order.amount_fcfa, plan.name)
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


def _activate_subscription(db: Session, order: Order):
    """Settles a just-paid order: credit-pack plans (the current model —
    every active plan has `credits` set, mirroring Izivoice's own packs)
    grant a CreditPot; a null-credits plan falls back to the legacy
    unlimited-access Subscription for any pre-migration plan still around."""
    plan = order.plan
    from src.utils.billing import activate_credit_subscription, credit_user, grant_subscription_cycle_credits, CREDIT_CYCLE_DAYS, CREDIT_CYCLE_LABELS_FR
    if plan.credits:
        valid_days = plan.duration_days
        cycle_label = CREDIT_CYCLE_LABELS_FR.get(order.billing_cycle, "Mensuel")
        credit_user(
            db, order.user,
            amount=plan.credits,
            valid_days=valid_days,
            description=f"Achat {plan.name} ({cycle_label}) — {order.amount_fcfa:,} FCFA via {order.provider} (commande {order.id})".replace(",", " "),
        )
        result = activate_credit_subscription(
            db,
            order.user,
            valid_days=valid_days,
            plan=plan,
            note=f"Payé via {order.provider} (commande {order.id})",
        )
    else:
        result = Subscription(
            user_id=order.user_id,
            plan_id=plan.id,
            status="active",
            started_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(days=plan.duration_days),
            note=f"Payé via {order.provider} (commande {order.id})",
        )
        db.add(result)
        db.commit()
        db.refresh(result)
        grant_subscription_cycle_credits(db, result)

    try:
        send_invoice_email(order.user, order, plan)
    except Exception as exc:
        logger.error(f"Invoice email failed for order {order.id}: {exc}")

    return result


def send_invoice_email(user: User, order: Order, plan: Plan) -> None:
    locale = user.locale if user.locale in SUPPORTED_LOCALES else "fr"
    amount_display = f"{order.amount_fcfa:,}".replace(",", " ")
    paid_at = order.created_at.strftime("%d/%m/%Y") if locale == "fr" else order.created_at.strftime("%Y-%m-%d")

    copy = {
        "fr": {
            "eyebrow": "Facture",
            "title": "Merci pour ton paiement",
            "intro": f"Ton abonnement KappGen « {plan.name} » est actif.",
            "subject": f"Facture KappGen — {plan.name}",
            "preheader": f"Reçu de paiement KappGen pour {plan.name}",
            "labels": {"order": "N° de commande", "plan": "Offre", "amount": "Montant", "date": "Date", "method": "Moyen de paiement"},
            "cta": "Voir mes vidéos",
            "text": f"Merci pour ton paiement. Offre : {plan.name}. Montant : {amount_display} FCFA. Commande : {order.id}.",
        },
        "en": {
            "eyebrow": "Invoice",
            "title": "Thanks for your payment",
            "intro": f"Your KappGen \"{plan.name}\" subscription is now active.",
            "subject": f"KappGen invoice — {plan.name}",
            "preheader": f"KappGen payment receipt for {plan.name}",
            "labels": {"order": "Order #", "plan": "Plan", "amount": "Amount", "date": "Date", "method": "Payment method"},
            "cta": "View my videos",
            "text": f"Thanks for your payment. Plan: {plan.name}. Amount: {amount_display} FCFA. Order: {order.id}.",
        },
    }[locale]

    rows = "".join(
        f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #26364a;color:#5b6779;font-size:13px">{label}</td>
          <td style="padding:10px 0;border-bottom:1px solid #26364a;color:#eaf6ff;font-size:13px;text-align:right">{value}</td>
        </tr>
        """
        for label, value in [
            (copy["labels"]["plan"], plan.name),
            (copy["labels"]["amount"], f"{amount_display} FCFA"),
            (copy["labels"]["date"], paid_at),
            (copy["labels"]["method"], order.provider.capitalize()),
            (copy["labels"]["order"], order.id),
        ]
    )

    body = f"""
      <p style="color:{EMAIL_ACCENT};font-size:12px;font-weight:700;letter-spacing:1.4px;margin:0 0 16px;text-transform:uppercase">{copy['eyebrow']}</p>
      <h1 style="color:#eaf6ff;font-size:24px;font-weight:700;margin:0 0 12px;line-height:1.3">{copy['title']}</h1>
      <p style="color:#9badc0;font-size:15px;line-height:1.7;margin:0 0 24px">{copy['intro']}</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0d141f;border:1px solid #26364a;border-radius:12px;padding:4px 20px">
        {rows}
      </table>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:28px">
        <tr>
          <td align="center">
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td style="background:{EMAIL_ACCENT};border-radius:10px">
                  <a href="{FRONTEND_BASE_URL}/dashboard" style="display:inline-block;padding:12px 24px;color:#07101a;font-size:15px;font-weight:700;text-decoration:none">{copy['cta']}</a>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    """
    send_brevo_email(
        user.email,
        copy["subject"],
        email_shell(copy["preheader"], body),
        copy["text"],
    )


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


@router.post("/webhook/tarapay", include_in_schema=False)
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
