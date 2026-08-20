"""Maketou + Tara Money (dklo.co) payment provider clients — ported faithfully
from the sibling izivoice project's working integration (same merchant
credentials, reused deliberately per the user)."""
import httpx
from typing import Any, Dict
from src.config import (
    MAKETOU_API_KEY, MAKETOU_PRODUCT_ID,
    TARA_API_KEY, TARA_BUSINESS_ID, TARA_WEBHOOK_SECRET,
    FRONTEND_BASE_URL, BACKEND_BASE_URL,
)

MAKETOU_BASE_URL = "https://api.maketou.net/api/v1/stores"
TARAPAY_URL = "https://www.dklo.co/api/tara/paymentlinks"


def create_maketou_checkout(order_id: str, amount_fcfa: int, user_email: str, user_name: str) -> Dict[str, Any]:
    """Creates a Maketou hosted checkout. Returns {"redirect_url", "provider_ref"}."""
    if not MAKETOU_API_KEY or not MAKETOU_PRODUCT_ID:
        raise RuntimeError("Maketou n'est pas configuré sur le serveur.")

    first_name, _, last_name = user_name.partition(" ")
    payload = {
        "productDocumentId": MAKETOU_PRODUCT_ID,
        "email": user_email,
        "firstName": first_name or user_name,
        "lastName": last_name or "",
        "redirectURL": f"{FRONTEND_BASE_URL}/billing/success?order_id={order_id}&provider=maketou",
        "customerPrice": amount_fcfa,
        "meta": {"source": "nichecut_app", "order_id": order_id},
    }
    resp = httpx.post(
        f"{MAKETOU_BASE_URL}/cart/checkout",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {MAKETOU_API_KEY}"},
        json=payload,
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    cart = data.get("cart") or {}
    return {"redirect_url": data.get("redirectUrl"), "provider_ref": cart.get("id")}


def poll_maketou_order(cart_id: str) -> str:
    """Returns Maketou's cart status string (e.g. "completed", "pending").
    Maketou has no webhook — this polling call is the only way to confirm payment."""
    resp = httpx.get(
        f"{MAKETOU_BASE_URL}/cart/{cart_id}",
        headers={"Authorization": f"Bearer {MAKETOU_API_KEY}"},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return ((data.get("cart") or {}).get("status") or "").lower()


def create_tarapay_checkout(order_id: str, amount_fcfa: int, plan_name: str) -> Dict[str, Any]:
    """Creates a Tara Money hosted payment link. Returns {"redirect_url"}."""
    if not TARA_API_KEY or not TARA_BUSINESS_ID:
        raise RuntimeError("Tara Money n'est pas configuré sur le serveur.")
    if not TARA_WEBHOOK_SECRET:
        raise RuntimeError("TARA_WEBHOOK_SECRET n'est pas configuré — le webhook de confirmation ne pourrait pas être vérifié.")

    payload = {
        "apiKey": TARA_API_KEY,
        "businessId": TARA_BUSINESS_ID,
        "productId": order_id,
        "productName": f"Abonnement NicheCut — {plan_name}",
        "productPrice": amount_fcfa,
        "productDescription": f"Abonnement {plan_name}, {amount_fcfa} FCFA. Aucun montant supérieur ne sera débité.",
        "returnUrl": f"{FRONTEND_BASE_URL}/billing/success?order_id={order_id}&provider=tarapay",
        "webHookUrl": f"{BACKEND_BASE_URL}/api/billing/webhook/tarapay?key={TARA_WEBHOOK_SECRET}",
    }
    resp = httpx.post(TARAPAY_URL, headers={"Content-Type": "application/json"}, json=payload, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    return {"redirect_url": data.get("generalLink") or data.get("cardLink")}
