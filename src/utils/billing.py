"""Free-tier quota + subscription checks shared by the video-submission
paywall and the watermark-removal gate, plus the credit ledger (pots +
transactions, ported from Izivoice's own credits/shared-credits.ts model)
that replaced flat subscriptions as KappGen's actual billing unit."""
from datetime import datetime, timedelta
from math import ceil
from sqlalchemy.orm import Session
from src.db.models import User, Subscription, CreditPot, CreditTransaction

# Every KappGen credit debited for an Izivoice-billed call (voice, images,
# transcription, music) costs this many times the real Izivoice credits it
# consumed — the operator's target margin. Applied at debit time, not at
# purchase time: a credit pack costs the buyer exactly what Izivoice charges
# per credit (see the Starter/Creator/Standard/Pro packs), but each API call
# is metered against more of the buyer's balance than it actually cost us.
CREDIT_MARKUP_MULTIPLIER = 3.5

# Real Izivoice/ai33.pro credit costs per unit of work, in Izivoice's own
# credit currency — the same unit KappGen sells to creators, so a KappGen
# credit pack buys exactly as many "Izivoice-equivalent" credits as it
# claims. Confirmed rates: image generation per the operator's own account
# (1000 cr/image); STT per Izivoice's speech-to-text-pricing.ts
# (SPEECH_TO_TEXT_CREDITS_PER_SEC = 3); TTS per their "≈1 credit/character"
# documented rate; music per MUSIC_CREDITS_PER_GENERATION = 300.
IZIVOICE_IMAGE_CREDITS = 1000
IZIVOICE_STT_CREDITS_PER_SEC = 3
IZIVOICE_TTS_CREDITS_PER_CHAR = 1
IZIVOICE_MUSIC_CREDITS = 300

# Converts a real Anthropic/OpenAI/fal.ai script-generation cost (in USD) into
# KappGen credits, the same way IZIVOICE_* above converts Izivoice's own
# credit currency. There's no direct USD credit-pack price to read (packs are
# priced in FCFA), so the conversion goes through an approximate FCFA/USD
# rate and the Starter pack's rate (the least favorable to the buyer, i.e.
# highest FCFA per credit) — same reasoning as CREDIT_MARKUP_MULTIPLIER:
# protect the operator's margin rather than pick a buyer-favorable rate.
FCFA_PER_USD = 600.0
STARTER_PACK_PRICE_FCFA = 3500
STARTER_PACK_CREDITS = 100_000
CREDIT_VALUE_FCFA = STARTER_PACK_PRICE_FCFA / STARTER_PACK_CREDITS  # 0.035 FCFA/credit

# Automatic ("KappGen AI choisit le sujet et écrit le script") mode bills the
# creator this many times the script generation's real provider cost — same
# margin-protection idea as CREDIT_MARKUP_MULTIPLIER above, applied to
# Anthropic/OpenAI/fal.ai spend instead of Izivoice spend.
SCRIPT_GENERATION_COST_MARKUP_MULTIPLIER = 4.0


def usd_to_credits(cost_usd: float) -> int:
    """Converts a real USD provider cost into the equivalent number of
    KappGen credits, at the Starter pack's FCFA/credit rate."""
    if cost_usd <= 0:
        return 0
    return ceil((cost_usd * FCFA_PER_USD) / CREDIT_VALUE_FCFA)

# Credit-pack validity tiers — ported as-is from Izivoice's own promo.ts
# (MARKUP_BY_CYCLE / getValidityDays): each pack's listed price is the
# "monthly" (30-day) rate; a longer commitment multiplies both price and
# validity instead of selling a separate plan row per duration.
CREDIT_CYCLE_MARKUPS = {
    "monthly": 1.0,
    "quarterly": 1.1,      # 3 mois
    "semiannual": 1.2,     # 6 mois
    "yearly": 1.25,        # 1 an
    "lifetime": 1.3,       # à vie
}
CREDIT_CYCLE_DAYS = {
    "monthly": 30,
    "quarterly": 90,
    "semiannual": 180,
    "yearly": 365,
    "lifetime": 36500,  # ~100 years — expires_at is NOT NULL, so "never" needs a real date
}
CREDIT_CYCLE_LABELS_FR = {
    "monthly": "Mensuel",
    "quarterly": "3 mois",
    "semiannual": "6 mois",
    "yearly": "1 an",
    "lifetime": "À vie",
}


def marketing_round_fcfa(price: float) -> int:
    """Rounds up to the nearest 500 FCFA — same cosmetic/margin-protecting
    rule as Izivoice's marketingRound() for FCFA amounts."""
    import math
    return int(math.ceil(price / 500.0) * 500)


def price_for_cycle(base_monthly_price_fcfa: int, cycle: str) -> int:
    """The price a credit pack's cycle actually charges — server-computed
    from the plan's base (monthly) price so a client can never just submit
    a cheaper price for a longer cycle."""
    markup = CREDIT_CYCLE_MARKUPS.get(cycle, 1.0)
    return marketing_round_fcfa(base_monthly_price_fcfa * markup)


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
    if get_credit_balance(db, user) > 0:
        return True, ""
    return False, "Quota gratuit épuisé — recharge des crédits pour générer d'autres vidéos."


def get_credit_balance(db: Session, user: User) -> int:
    """Sum of every still-valid pot — never a single mutable counter on the
    user row, so an expired promo pack stops counting on its own without a
    separate cron having to zero anything out (Izivoice's own model)."""
    pots = db.query(CreditPot).filter(
        CreditPot.user_id == user.id,
        CreditPot.amount > 0,
        CreditPot.expires_at > datetime.utcnow(),
    ).all()
    return sum(p.amount for p in pots)


def credit_user(db: Session, user: User, amount: int, valid_days: int, description: str, transaction_type: str = "purchase") -> CreditPot:
    """Grants `amount` credits, expiring in `valid_days` — called after a
    paid order settles, or by an admin manual grant."""
    pot = CreditPot(
        user_id=user.id,
        amount=amount,
        original_amount=amount,
        expires_at=datetime.utcnow() + timedelta(days=valid_days),
    )
    db.add(pot)
    db.add(CreditTransaction(user_id=user.id, amount=amount, transaction_type=transaction_type, description=description))
    db.commit()
    return pot


def debit_credits(db: Session, user: User, amount: int, description: str) -> bool:
    """FIFO-deducts `amount` credits from the soonest-expiring pots first (so
    a creator's promo credits get used before they'd expire unused). Returns
    False — deducting nothing — if the balance can't cover the full amount;
    never partially debits a call that then fails anyway."""
    if amount <= 0:
        return True
    pots = db.query(CreditPot).filter(
        CreditPot.user_id == user.id,
        CreditPot.amount > 0,
        CreditPot.expires_at > datetime.utcnow(),
    ).order_by(CreditPot.expires_at.asc()).all()
    if sum(p.amount for p in pots) < amount:
        return False
    remaining = amount
    for pot in pots:
        if remaining <= 0:
            break
        take = min(pot.amount, remaining)
        pot.amount -= take
        remaining -= take
    db.add(CreditTransaction(user_id=user.id, amount=-amount, transaction_type="debit", description=description))
    db.commit()
    return True


def debit_izivoice_usage_by_user_id(user_id: str, izivoice_credits: float, operation: str) -> bool:
    """Self-contained version of debit_izivoice_usage, opening its own
    short-lived session — for deep pipeline call sites (image generation,
    per-scene TTS/STT) that only have a user_id, not a live db session/User
    object, threaded down to them. Same shape as cost_tracking.log_usage()
    for the same reason: a metering failure must never be able to fail the
    actual generation it's metering. Swallows its own errors and treats them
    as "allow" (fails open) — a metering bug shouldn't block a paying
    creator's render; the balance check at submission time is the real gate."""
    if not user_id:
        return True
    try:
        from src.db.session import SessionLocal
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return True
            has_own_key = bool(user.izivoice_api_key_encrypted)
            return debit_izivoice_usage(db, user, izivoice_credits, operation, has_own_key)
        finally:
            db.close()
    except Exception:
        return True


def debit_script_generation_cost(db: Session, user: User, cost_usd: float) -> bool:
    """Meters an "Automatique" script generation's real provider cost against
    the creator's KappGen balance, at SCRIPT_GENERATION_COST_MARKUP_MULTIPLIER
    times the real cost. Called after generation succeeds (the real cost is
    only known once the call is done) — mirrors debit_izivoice_usage_by_user_id's
    fail-open behavior: a metering shortfall is logged, never used to undo or
    block content that was already generated."""
    if cost_usd <= 0:
        return True
    charge = ceil(cost_usd * SCRIPT_GENERATION_COST_MARKUP_MULTIPLIER * FCFA_PER_USD / CREDIT_VALUE_FCFA)
    ok = debit_credits(
        db, user, charge,
        f"Génération auto de script ({cost_usd:.4f} $ réel x{SCRIPT_GENERATION_COST_MARKUP_MULTIPLIER:.0f})",
    )
    if not ok:
        from src.utils.logger import logger
        logger.warning(
            f"Script-generation cost debit failed for user {user.id}: needed {charge} credits "
            f"(real cost {cost_usd:.4f} USD), balance insufficient."
        )
    return ok


def debit_izivoice_usage(db: Session, user: User, izivoice_credits: float, operation: str, user_has_own_key: bool = False) -> bool:
    """Central metering point for every Izivoice-billed pipeline call (TTS,
    STT, AI image generation, music). Free when the creator connected their
    own Izivoice key (see izivoice_key_for_user) — they're already paying
    Izivoice directly for that call, so charging KappGen credits too would be
    double-billing. Otherwise debits ceil(izivoice_credits * MARKUP) from
    their KappGen balance. Returns False if the balance can't cover it —
    callers should treat that as "insufficient credits", same as any other
    paywall check, and fail before spending real Izivoice money."""
    if user_has_own_key:
        return True
    charge = ceil(izivoice_credits * CREDIT_MARKUP_MULTIPLIER)
    return debit_credits(db, user, charge, f"{operation} ({izivoice_credits:.0f} cr. Izivoice x{CREDIT_MARKUP_MULTIPLIER})")
