"""Free-tier quota + subscription checks shared by the video-submission
paywall and the watermark-removal gate, plus the credit ledger (pots +
transactions, ported from Izivoice's own credits/shared-credits.ts model)
that replaced flat subscriptions as KappGen's actual billing unit."""
import random
from datetime import datetime, timedelta
from math import ceil
from typing import Optional
from sqlalchemy.orm import Session
from src.db.models import User, Subscription, CreditPot, CreditTransaction, Order, Plan, AppSetting

# Izivoice-billed calls (voice, images, transcription, music) are metered at
# cost (x1) rather than marked up — the operator owns Izivoice too, so that
# spend is already profit on the Izivoice side; charging a markup here on top
# would be double-margining the same unit of work.
CREDIT_MARKUP_MULTIPLIER = 1.0

# Real Izivoice/ai33.pro credit costs per unit of work, in Izivoice's own
# credit currency — the same unit KappGen sells to creators, so a KappGen
# credit pack buys exactly as many "Izivoice-equivalent" credits as it
# claims. Confirmed rates: image generation per the operator's own account
# (1000 cr/image); STT per Izivoice's speech-to-text-pricing.ts
# (SPEECH_TO_TEXT_CREDITS_PER_SEC = 3); TTS per their "≈1 credit/character"
# documented rate; music per MUSIC_CREDITS_PER_GENERATION = 300.
IZIVOICE_IMAGE_CREDITS = 1000
# Real per-call cost isn't perfectly flat in practice, so the debited amount
# is randomized within this band instead of always charging the same round
# 1000 — see random_image_credit_cost() below.
IZIVOICE_IMAGE_CREDITS_MIN = 956
IZIVOICE_IMAGE_CREDITS_MAX = 1001
IZIVOICE_STT_CREDITS_PER_SEC = 3
IZIVOICE_TTS_CREDITS_PER_CHAR = 1
IZIVOICE_MUSIC_CREDITS = 300

# Thumbnail generation goes through fal.ai's gpt-image-2 (OpenAI), a
# separate, pricier provider from the Izivoice image credits above — set by
# the operator at 2000 credits/thumbnail. Falls back to Izivoice's own image
# model (IZIVOICE_IMAGE_CREDITS) only if fal.ai errors out or its own
# credits are exhausted, so debiting must happen per attempted provider —
# see generate_thumbnail_image's caller in youtube_metadata.py.
THUMBNAIL_CREDITS = 2000


def random_image_credit_cost() -> int:
    """A per-image debit amount within IZIVOICE_IMAGE_CREDITS_MIN/MAX instead
    of always the same flat 1000 — real generation cost isn't perfectly
    uniform call to call, and a suspiciously round number invites questions
    a naturally-varying one doesn't."""
    return random.randint(IZIVOICE_IMAGE_CREDITS_MIN, IZIVOICE_IMAGE_CREDITS_MAX)


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

# Flat "conventional" fee charged on a render that used NO paid AI feature at
# all — own script, own voice recording (transcription off), own images, own
# music. Those creators generate zero Izivoice/Anthropic/fal.ai spend, but
# still cost real server compute (ffmpeg) and storage, so this covers that
# instead of letting a fully-BYO video render for free. Only applies when the
# render triggered no other debit — see maybe_debit_base_render_fee below.
BASE_RENDER_FEE_FCFA = 100

def _base_render_fee_credits() -> int:
    return ceil(BASE_RENDER_FEE_FCFA / CREDIT_VALUE_FCFA)


def maybe_debit_base_render_fee(db: Session, user: User, video) -> None:
    """Charges BASE_RENDER_FEE_FCFA for a just-finished render, but only if
    nothing else was already debited for it — i.e. the creator used no paid
    AI feature (transcription, AI images/thumbnail, Izivoice voice/music) at
    all. Since most debits along the pipeline don't have a video_id to tag
    themselves with (see debit_credits), "nothing else was debited" is
    approximated by no debit existing for this user between the video's
    render start and now — good enough in practice since a single creator
    rarely has two videos rendering in the exact same window. Best-effort:
    never raises, never blocks/undoes the render itself if the balance is
    short (same fail-open posture as debit_script_generation_cost)."""
    if not video.started_at:
        return
    already_debited = db.query(CreditTransaction).filter(
        CreditTransaction.user_id == user.id,
        CreditTransaction.transaction_type == "debit",
        CreditTransaction.created_at >= video.started_at,
    ).first() is not None
    if already_debited:
        return
    charge = _base_render_fee_credits()
    ok = debit_credits(
        db, user, charge,
        f"Frais forfaitaire vidéo ({BASE_RENDER_FEE_FCFA} FCFA — aucune fonctionnalité IA payante utilisée)",
        video_id=video.id,
    )
    if not ok:
        from src.utils.logger import logger
        logger.warning(f"Base render fee debit failed for user {user.id}, video {video.id}: needed {charge} credits, balance insufficient.")


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


def get_active_subscription(db: Session, user: User) -> Optional[Subscription]:
    return (
        db.query(Subscription)
        .filter(Subscription.user_id == user.id, Subscription.status == "active", Subscription.expires_at > datetime.utcnow())
        .order_by(Subscription.expires_at.desc())
        .first()
    )


def user_has_active_subscription(db: Session, user: User) -> bool:
    if get_active_subscription(db, user) is not None:
        return True
    # Backward compatibility for credit packs/admin grants issued before
    # credit-backed subscriptions were recorded explicitly. Their remaining
    # paid balance is already valid access and must be presented as active.
    return user_has_purchased_credits(db, user) and get_credit_balance(db, user) > 0


def activate_credit_subscription(
    db: Session,
    user: User,
    valid_days: int,
    *,
    plan: Optional[Plan] = None,
    granted_by_admin_id: Optional[str] = None,
    note: Optional[str] = None,
) -> Subscription:
    """Give credit-backed access the same active-subscription state as a plan.

    Paid credit packs and manual admin credit grants are real access grants,
    unlike welcome credits.  Keeping this explicit (rather than doing it in
    ``credit_user``) prevents signup bonuses and refunds from accidentally
    activating a subscription.
    """
    now = datetime.utcnow()
    sub = Subscription(
        user_id=user.id,
        plan_id=plan.id if plan else None,
        status="active",
        started_at=now,
        expires_at=now + timedelta(days=max(1, valid_days)),
        granted_by_admin_id=granted_by_admin_id,
        note=note,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _user_relevant_plans(db: Session, user: User) -> list:
    """Every plan this user currently "has access through": their active
    subscription's plan (if any) plus every credit pack they've ever
    successfully bought. Used as the shared basis for every per-feature/
    per-limit gate below — a user gets the best of whatever they've paid
    for, and an empty list (never purchased anything, welcome credits/free
    quota only) is the caller's cue to fall back to "unrestricted"."""
    plans = []
    sub = get_active_subscription(db, user)
    if sub and sub.plan:
        plans.append(sub.plan)
    purchased_plan_ids = [
        row[0] for row in
        db.query(Order.plan_id).join(Plan, Order.plan_id == Plan.id)
        .filter(Order.user_id == user.id, Order.status == "success", Plan.credits.isnot(None))
        .distinct().all()
    ]
    if purchased_plan_ids:
        plans.extend(db.query(Plan).filter(Plan.id.in_(purchased_plan_ids)).all())
    return plans


def _feature_enabled(db: Session, user: User, attr: str) -> bool:
    """True if ANY plan the user has access through grants this specific
    feature (attr is one of Plan's ai_*_enabled column names) — independent
    of whether they have spare credits sitting in their balance; a plain
    balance isn't proof they paid for that feature specifically. A
    subscription with no plan attached (admin-granted, plan_id nullable) is
    treated as permissive, same as never having purchased anything."""
    if user_has_purchased_credits(db, user) and get_credit_balance(db, user) > 0:
        return True
    sub = get_active_subscription(db, user)
    if sub and not sub.plan:
        return True
    plans = _user_relevant_plans(db, user)
    if not plans:
        return True
    return any(getattr(p, attr) for p in plans)


def user_ai_transcription_enabled(db: Session, user: User) -> bool:
    return _feature_enabled(db, user, "ai_transcription_enabled")


def user_ai_images_enabled(db: Session, user: User) -> bool:
    return _feature_enabled(db, user, "ai_images_enabled")


def user_ai_script_enabled(db: Session, user: User) -> bool:
    return _feature_enabled(db, user, "ai_script_enabled")


def user_autopublish_enabled(db: Session, user: User) -> bool:
    return _feature_enabled(db, user, "autopublish_enabled")


def user_max_channels(db: Session, user: User) -> Optional[int]:
    """None means unlimited. Best-of across every plan the user has access
    through (the highest cap they've ever paid for wins) — a user who
    upgraded shouldn't be capped by a smaller pack bought earlier."""
    if user_has_purchased_credits(db, user) and get_credit_balance(db, user) > 0:
        return None
    sub = get_active_subscription(db, user)
    if sub and not sub.plan:
        return None
    plans = _user_relevant_plans(db, user)
    if not plans:
        return None
    caps = [p.max_channels for p in plans]
    if any(c is None for c in caps):
        return None
    return max(caps)


def user_max_video_duration_seconds(db: Session, user: User) -> Optional[int]:
    """Same best-of-across-plans shape as user_max_channels, for the
    per-tier video-length cap shown on the pricing cards."""
    if user_has_purchased_credits(db, user) and get_credit_balance(db, user) > 0:
        return None
    sub = get_active_subscription(db, user)
    if sub and not sub.plan:
        return None
    plans = _user_relevant_plans(db, user)
    if not plans:
        return None
    caps = [p.max_video_duration_seconds for p in plans]
    if any(c is None for c in caps):
        return None
    return max(caps)


def user_video_quota_status(db: Session, user: User) -> tuple[Optional[int], int]:
    """(quota, used_this_cycle) for the user's active subscription tier —
    quota is None when unlimited (or no subscription/plan, i.e. gated by
    credits alone instead). used_this_cycle counts videos created since the
    subscription's started_at, across all of the user's channels."""
    sub = get_active_subscription(db, user)
    if not sub or not sub.plan or sub.plan.video_quota_per_cycle is None:
        return None, 0
    from src.db.models import Video, Channel
    used = (
        db.query(Video)
        .join(Channel, Video.channel_id == Channel.id)
        .filter(Channel.user_id == user.id, Video.created_at >= sub.started_at)
        .count()
    )
    return sub.plan.video_quota_per_cycle, used


def grant_subscription_cycle_credits(db: Session, subscription: Subscription) -> None:
    """Called once when a subscription (re)activates — grants the plan's
    monthly_credit_grant, if any, valid only for that subscription's own
    cycle (expires with it, so it can't be hoarded across renewals the way a
    purchased credit pack can). No-op for plans with no bonus configured."""
    plan = subscription.plan
    if not plan or not plan.monthly_credit_grant:
        return
    valid_days = max(1, (subscription.expires_at - subscription.started_at).days)
    credit_user(
        db, subscription.user,
        amount=plan.monthly_credit_grant,
        valid_days=valid_days,
        description=f"Crédits IA inclus — abonnement {plan.name}",
        transaction_type="subscription_grant",
    )


# New accounts get a spendable credit pot instead of a flat "N free videos"
# counter — a short/cheap video and a 1h AI-generated one used to cost the
# exact same "1 free video" regardless of real spend. Existing users already
# on the old quota keep it untouched; this only applies going forward.
WELCOME_CREDIT_AMOUNT = 10_000
# Never expires — same "effectively forever" convention as the paid
# "lifetime" pack cycle elsewhere in this file (expires_at is NOT NULL, so
# "never" still needs a real date). A welcome bonus that quietly evaporated
# after a month would defeat the point of it being a permanent credit grant.
WELCOME_CREDIT_VALID_DAYS = 36500


def grant_welcome_credits(db: Session, user: User) -> None:
    credit_user(db, user, WELCOME_CREDIT_AMOUNT, WELCOME_CREDIT_VALID_DAYS, "Crédits de bienvenue à l'inscription", transaction_type="welcome_bonus")


def migrate_legacy_accounts_to_welcome_credits(db: Session) -> int:
    """Retire legacy free-video quotas and grants missing welcome pots once."""
    migration_key = "legacy_free_videos_to_10000_credits_v1"
    # API and worker can start simultaneously in production. Serialize this
    # migration on PostgreSQL so two processes cannot grant the same bonus.
    if db.bind and db.bind.dialect.name == "postgresql":
        from sqlalchemy import text
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": migration_key})
    if db.query(AppSetting).filter(AppSetting.key == migration_key).first():
        return 0

    db.query(User).filter(User.free_video_quota_granted != 0).update(
        {User.free_video_quota_granted: 0}, synchronize_session=False
    )
    already_granted = db.query(CreditTransaction.user_id).filter(
        CreditTransaction.transaction_type == "welcome_bonus"
    )
    users = db.query(User).filter(~User.id.in_(already_granted)).all()
    expires_at = datetime.utcnow() + timedelta(days=WELCOME_CREDIT_VALID_DAYS)
    for user in users:
        db.add(CreditPot(
            user_id=user.id, amount=WELCOME_CREDIT_AMOUNT,
            original_amount=WELCOME_CREDIT_AMOUNT, expires_at=expires_at,
        ))
        db.add(CreditTransaction(
            user_id=user.id, amount=WELCOME_CREDIT_AMOUNT,
            transaction_type="welcome_bonus",
            description="Migration vers les 10 000 crédits de bienvenue",
        ))
    db.add(AppSetting(key=migration_key, value="complete"))
    db.commit()
    return len(users)


def estimate_video_cost_credits(
    script_char_count: int = 0,
    estimated_duration_seconds: float = 0.0,
    transcribe_audio: bool = False,
    image_style: Optional[dict] = None,
    music_preference: Optional[dict] = None,
    scene_count: Optional[int] = None,
) -> int:
    """Rough upfront credit cost of one video, from the same per-unit rates
    debit_izivoice_usage/_by_user_id charge as the pipeline actually runs —
    used to reject a render before it starts instead of letting it burn
    partial credit and fail mid-way once the balance runs out. Deliberately
    conservative (rounds generously) since under-estimating just means a
    render that was truly affordable gets blocked, which is a much smaller
    problem than one that starts and can't finish."""
    image_style = image_style or {}
    music_preference = music_preference or {}
    total = 0.0

    # Voiceover (TTS) always runs for a text-input video; for an audio upload
    # it's skipped (the creator supplied their own recording) but STT may run
    # instead, priced per second of that recording.
    if script_char_count:
        total += script_char_count * IZIVOICE_TTS_CREDITS_PER_CHAR
    if transcribe_audio and estimated_duration_seconds:
        total += estimated_duration_seconds * IZIVOICE_STT_CREDITS_PER_SEC

    source = image_style.get("source", "library")
    if source in ("ai_generated", "hybrid"):
        # Mirrors fetch_or_generate_images' own budget: only the opening
        # window's images are ever actually generated, the rest of a long
        # video reuses that pool — so cost caps out instead of scaling
        # linearly with total scene count.
        generation_count = scene_count if scene_count is not None else max(1, round(estimated_duration_seconds / 6))
        if source == "hybrid":
            generation_count = (generation_count + 1) // 2
        total += min(generation_count, 100) * IZIVOICE_IMAGE_CREDITS_MAX

    if music_preference.get("enabled") and music_preference.get("mode") == "ai_generate":
        total += IZIVOICE_MUSIC_CREDITS

    return ceil(max(total, 0) * CREDIT_MARKUP_MULTIPLIER)


def user_has_purchased_credits(db: Session, user: User) -> bool:
    """Whether this creator has ever received real credits — through a paid
    order (transaction_type "purchase", settled in _activate_subscription)
    or a manual admin top-up ("admin_grant", src/api/routes/admin.py) —
    as opposed to only ever having the automatic free-trial "welcome_bonus"
    grant every new signup gets. A lifetime unlock, not a point-in-time
    balance check: it stays true even if the credits granted have since
    been fully spent down to zero. Used to gate the KappGen watermark."""
    return (
        db.query(CreditTransaction)
        .filter(
            CreditTransaction.user_id == user.id,
            CreditTransaction.transaction_type.in_(("purchase", "admin_grant")),
        )
        .first() is not None
    )


def user_can_render(db: Session, user: User, estimated_cost_credits: int = 0) -> tuple[bool, str]:
    """estimated_cost_credits (from estimate_video_cost_credits) checks the
    balance can actually cover THIS video before it starts, instead of just
    "has any credit at all" — a render that's guaranteed to run out of
    balance partway through wastes the portion that did complete for
    nothing, so it's better rejected upfront with a clear reason."""
    # The legacy flat "N free videos" quota (free_video_quota_granted) used to
    # bypass the credit check entirely regardless of actual balance — a
    # real free-render loophole, not the intended policy. The only free
    # allowance is now the one-time WELCOME_CREDIT_AMOUNT grant at signup,
    # which flows through the ordinary credit-balance check below like any
    # other credits. New signups already get free_video_quota_granted=0
    # (see auth.py); this field is otherwise unused/legacy now.
    sub = get_active_subscription(db, user)
    if sub and (sub.plan is None or sub.plan.credits is None):
        quota, used = user_video_quota_status(db, user)
        if quota is None or used < quota:
            return True, ""
        # Over the plan's included quota — fall through to credits for the
        # overage instead of a hard block, same as running out of the free
        # quota above does.
    balance = get_credit_balance(db, user)
    if balance >= max(estimated_cost_credits, 1):
        return True, ""
    if estimated_cost_credits > 0:
        return False, f"Solde de crédits insuffisant pour cette vidéo (environ {estimated_cost_credits} crédits nécessaires, {balance} disponibles) — recharge des crédits."
    return False, "Solde de crédits insuffisant — recharge des crédits pour générer cette vidéo."


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


def debit_credits(db: Session, user: User, amount: int, description: str, video_id: Optional[str] = None) -> bool:
    """FIFO-deducts `amount` credits from the soonest-expiring pots first (so
    a creator's promo credits get used before they'd expire unused). Returns
    False — deducting nothing — if the balance can't cover the full amount;
    never partially debits a call that then fails anyway. `video_id` is
    optional and only meaningful to the per-video cost recap — most call
    sites deep in the pipeline don't have a Video object in hand."""
    if amount <= 0:
        return True
    pots = db.query(CreditPot).filter(
        CreditPot.user_id == user.id,
        CreditPot.amount > 0,
        CreditPot.expires_at > datetime.utcnow(),
    ).order_by(CreditPot.expires_at.asc()).with_for_update().all()
    if sum(p.amount for p in pots) < amount:
        return False
    remaining = amount
    for pot in pots:
        if remaining <= 0:
            break
        take = min(pot.amount, remaining)
        pot.amount -= take
        remaining -= take
    db.add(CreditTransaction(user_id=user.id, video_id=video_id, amount=-amount, transaction_type="debit", description=description))
    db.commit()
    return True


def refund_video_credits(db: Session, video_id: str, reason: str) -> int:
    """Refunds every credit actually debited for one video — base render
    fee, script generation, TTS, STT, AI thumbnail, AI music, all of it,
    summed from CreditTransaction rows tagged with this video_id (see
    debit_credits' video_id param and every debit call site that threads it
    through) — not a flat guessed amount, the real total spent on this
    specific video. Called when a video ultimately fails and is never
    delivered, so a creator doesn't pay for a video they never got.

    Idempotent: a video already refunded (a prior 'refund' CreditTransaction
    exists for it) is skipped — the worker's retry/orphan-requeue logic can
    reach the failure path more than once for the same video, and refunding
    twice would just be free money. Returns the amount refunded (0 if
    nothing to refund or already refunded)."""
    already_refunded = db.query(CreditTransaction).filter(
        CreditTransaction.video_id == video_id,
        CreditTransaction.transaction_type == "refund",
    ).first()
    if already_refunded:
        return 0

    spent = db.query(CreditTransaction).filter(
        CreditTransaction.video_id == video_id,
        CreditTransaction.transaction_type == "debit",
    ).all()
    total = -sum(t.amount for t in spent)  # debit amounts are stored negative
    if total <= 0:
        return 0

    user_id = spent[0].user_id
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return 0

    # A fresh, non-expiring-soon pot rather than trying to credit back into
    # whichever pot(s) the original debit drew from (already partially spent
    # elsewhere by now, possibly expired) — same mechanism as any other
    # credit grant (credit_user), just tagged as a refund.
    pot = CreditPot(user_id=user.id, amount=total, original_amount=total, expires_at=datetime.utcnow() + timedelta(days=365))
    db.add(pot)
    db.add(CreditTransaction(user_id=user.id, video_id=video_id, amount=total, transaction_type="refund", description=reason))
    db.commit()
    return total


def debit_izivoice_usage_by_user_id(user_id: str, izivoice_credits: float, operation: str, video_id: Optional[str] = None) -> bool:
    """Self-contained version of debit_izivoice_usage, opening its own
    short-lived session — for deep pipeline call sites (image generation,
    per-scene TTS/STT) that only have a user_id, not a live db session/User
    object, threaded down to them. Billing failures fail closed: returning
    True without a verified user/debit allowed paid provider work to proceed
    without payment. `video_id`, when the caller has one, tags the resulting
    CreditTransaction so a failed video's total real spend can be refunded
    later (see refund_video_credits) — every credit actually spent on it,
    not just the flat base render fee."""
    if not user_id:
        return False
    try:
        from src.db.session import SessionLocal
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return False
            has_own_key = bool(user.izivoice_api_key_encrypted)
            return debit_izivoice_usage(db, user, izivoice_credits, operation, has_own_key, video_id=video_id)
        finally:
            db.close()
    except Exception as exc:
        from src.utils.logger import logger
        logger.error(f"Credit debit failed closed for user {user_id}, operation {operation}: {exc}")
        return False


def estimate_script_generation_cost(total_words: int, num_parts: int) -> dict:
    """Predicts what an "Automatique" script generation of this shape will
    actually cost, in the same terms debit_script_generation_cost charges
    afterwards — so the structure editor can show the creator a real number
    before they commit to a length, instead of them finding out after the
    fact. Mirrors generate_daily_script's real call shape (script_writer.py):
    one topic-pick call plus one call per part, each with the shared
    style/rules/continuity-tail overhead baked into its prompt.

    Token counts are approximate (real usage is only known after the actual
    Claude call), tuned from that prompt template's typical size — good
    enough for a "what will this roughly cost" preview, not exact billing."""
    from src.utils.cost_tracking import estimate_anthropic_cost

    num_parts = max(1, num_parts)
    TOPIC_INPUT_TOKENS = 400
    TOPIC_OUTPUT_TOKENS = 60
    PART_PROMPT_OVERHEAD_TOKENS = 550  # rules, style/CTA lines, continuity tail
    WORDS_TO_TOKENS = 1.4  # rough English-ish words-to-tokens ratio for narration prose

    input_tokens = TOPIC_INPUT_TOKENS + num_parts * PART_PROMPT_OVERHEAD_TOKENS
    output_tokens = TOPIC_OUTPUT_TOKENS + int(total_words * WORDS_TO_TOKENS)

    cost_usd = estimate_anthropic_cost(input_tokens, output_tokens)
    charge_credits = ceil(cost_usd * SCRIPT_GENERATION_COST_MARKUP_MULTIPLIER * FCFA_PER_USD / CREDIT_VALUE_FCFA)
    charge_fcfa = charge_credits * CREDIT_VALUE_FCFA
    return {
        "cost_usd": round(cost_usd, 4),
        "credits": charge_credits,
        "fcfa": round(charge_fcfa, 2),
    }


def debit_script_generation_cost(db: Session, user: User, cost_usd: float, video_id: Optional[str] = None) -> bool:
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
        video_id=video_id,
    )
    if not ok:
        from src.utils.logger import logger
        logger.warning(
            f"Script-generation cost debit failed for user {user.id}: needed {charge} credits "
            f"(real cost {cost_usd:.4f} USD), balance insufficient."
        )
    return ok


def debit_izivoice_usage(db: Session, user: User, izivoice_credits: float, operation: str, user_has_own_key: bool = False, video_id: Optional[str] = None) -> bool:
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
    return debit_credits(db, user, charge, f"{operation} ({izivoice_credits:.0f} cr. Izivoice x{CREDIT_MARKUP_MULTIPLIER})", video_id=video_id)
