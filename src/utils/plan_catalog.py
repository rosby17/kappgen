"""Operator-owned, permanent KappGen sales catalog.

Plans are product configuration, not mutable admin content.  Keeping the
catalog here guarantees that a stale/deactivated/empty plans table can never
make checkout disappear for creators.
"""
from sqlalchemy.orm import Session

from src.db.models import Plan


PLAN_CATALOG = (
    {"name": "Starter", "price_fcfa": 3_500, "credits": 100_000},
    {"name": "Creator", "price_fcfa": 7_000, "credits": 200_000},
    {"name": "Standard", "price_fcfa": 12_500, "credits": 400_000},
    {"name": "Créateur", "price_fcfa": 18_000, "credits": 600_000},
    {"name": "Automatique", "price_fcfa": 42_000, "credits": 1_500_000},
    {"name": "Pro", "price_fcfa": 55_000, "credits": 2_000_000},
    {"name": "Scale", "price_fcfa": 90_000, "credits": 3_500_000},
)


def ensure_sales_catalog(db: Session) -> list[Plan]:
    """Upsert and reactivate the immutable public catalog."""
    plans = []
    for spec in PLAN_CATALOG:
        plan = db.query(Plan).filter(Plan.name == spec["name"]).first()
        if plan is None:
            plan = Plan(name=spec["name"], price_fcfa=spec["price_fcfa"])
            db.add(plan)
        plan.price_fcfa = spec["price_fcfa"]
        plan.duration_days = 30
        plan.credits = spec["credits"]
        plan.is_active = True
        plans.append(plan)
    db.commit()
    for plan in plans:
        db.refresh(plan)
    return sorted(plans, key=lambda plan: plan.price_fcfa)
