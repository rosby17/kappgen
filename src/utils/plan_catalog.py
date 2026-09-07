"""Operator-owned, permanent KappGen sales catalog.

Plans are product configuration, not mutable admin content.  Keeping the
catalog here guarantees that a stale/deactivated/empty plans table can never
make checkout disappear for creators.
"""
from sqlalchemy.orm import Session

from src.db.models import Plan


# max_channels/max_cloned_voices: None means unlimited. A progressive-paywall
# pass (2026-09-07) — the real scarcity levers across tiers are channel count
# and cloned-voice count, both crescendo with price. Deliberately NOT
# touching max_video_duration_seconds or the ai_*_enabled flags here: video
# length tiering was already configured correctly (admin-set, left alone),
# and script/image/transcription generation stay available on every tier —
# they're already billed per task via credits, so gating them by plan on top
# would just double-charge the same usage instead of differentiating tiers.
PLAN_CATALOG = (
    {"name": "Starter", "price_fcfa": 3_500, "credits": 100_000, "max_channels": 2, "max_cloned_voices": 1},
    {"name": "Creator", "price_fcfa": 7_000, "credits": 200_000, "max_channels": 5, "max_cloned_voices": 2},
    {"name": "Standard", "price_fcfa": 12_500, "credits": 400_000, "max_channels": 10, "max_cloned_voices": 5},
    {"name": "Pro", "price_fcfa": 55_000, "credits": 2_000_000, "max_channels": None, "max_cloned_voices": None},
)


def ensure_sales_catalog(db: Session) -> list[Plan]:
    """Upsert and reactivate the immutable public catalog.

    Any existing plan NOT in PLAN_CATALOG (e.g. a retired duplicate tier) is
    deactivated rather than deleted — old Order/Subscription rows still
    reference it by id, so removing the row outright would break that
    history. Deactivating is enough to drop it from checkout/admin display."""
    catalog_names = {spec["name"] for spec in PLAN_CATALOG}
    plans = []
    for spec in PLAN_CATALOG:
        plan = db.query(Plan).filter(Plan.name == spec["name"]).first()
        if plan is None:
            plan = Plan(name=spec["name"], price_fcfa=spec["price_fcfa"])
            db.add(plan)
        plan.price_fcfa = spec["price_fcfa"]
        plan.duration_days = 30
        plan.credits = spec["credits"]
        plan.max_channels = spec["max_channels"]
        plan.max_cloned_voices = spec["max_cloned_voices"]
        plan.is_active = True
        plans.append(plan)
    for stale in db.query(Plan).filter(Plan.name.notin_(catalog_names), Plan.is_active.is_(True)).all():
        stale.is_active = False
    db.commit()
    for plan in plans:
        db.refresh(plan)
    return sorted(plans, key=lambda plan: plan.price_fcfa)
