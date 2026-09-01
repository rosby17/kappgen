from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.session import Base
from src.db.models import Channel, CreditPot, CreditTransaction, Plan, Subscription, User, Video
from src.api.routes.admin import _queued_video_positions
from src.utils.billing import get_credit_balance, migrate_legacy_accounts_to_welcome_credits, user_can_render
from src.utils.plan_catalog import PLAN_CATALOG, ensure_sales_catalog


def _db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _user(db, email: str) -> User:
    user = User(
        email=email,
        name="Test",
        hashed_password="unused",
        free_video_quota_granted=0,
        free_videos_used=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _credits(db, user: User, amount: int) -> None:
    db.add(CreditPot(
        user_id=user.id,
        amount=amount,
        original_amount=amount,
        expires_at=datetime.utcnow() + timedelta(days=30),
    ))
    db.commit()


def test_welcome_balance_only_authorizes_affordable_render():
    db = _db()
    user = _user(db, "welcome@example.com")
    _credits(db, user, 10_000)

    assert user_can_render(db, user, 10_000)[0] is True
    assert user_can_render(db, user, 10_001)[0] is False


def test_legacy_video_quota_is_replaced_by_one_welcome_credit_pot():
    db = _db()
    user = _user(db, "legacy@example.com")
    user.free_video_quota_granted = 10
    db.commit()

    assert user_can_render(db, user, 1)[0] is False
    assert migrate_legacy_accounts_to_welcome_credits(db) == 1
    db.refresh(user)
    assert user.free_video_quota_granted == 0
    assert get_credit_balance(db, user) == 10_000
    assert migrate_legacy_accounts_to_welcome_credits(db) == 0
    assert db.query(CreditTransaction).filter_by(
        user_id=user.id, transaction_type="welcome_bonus"
    ).count() == 1


def test_credit_pack_subscription_does_not_become_unlimited_after_balance_is_empty():
    db = _db()
    user = _user(db, "pack@example.com")
    plan = Plan(name="Pack", price_fcfa=3500, duration_days=36500, credits=100_000)
    db.add(plan)
    db.commit()
    db.add(Subscription(
        user_id=user.id,
        plan_id=plan.id,
        status="active",
        started_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=36500),
    ))
    db.commit()

    assert get_credit_balance(db, user) == 0
    allowed, reason = user_can_render(db, user, 1)
    assert allowed is False
    assert "insuffisant" in reason.lower()


def test_admin_subscription_without_credit_pack_can_grant_access():
    db = _db()
    user = _user(db, "admin-sub@example.com")
    db.add(Subscription(
        user_id=user.id,
        plan_id=None,
        status="active",
        started_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=30),
    ))
    db.commit()

    assert user_can_render(db, user, 999_999)[0] is True


def test_sales_catalog_is_created_and_reactivated_automatically():
    db = _db()
    plans = ensure_sales_catalog(db)
    assert [(p.name, p.price_fcfa, p.duration_days, p.credits) for p in plans] == [
        (spec["name"], spec["price_fcfa"], 30, spec["credits"])
        for spec in PLAN_CATALOG
    ]

    plans[0].is_active = False
    plans[0].price_fcfa = 1
    db.commit()
    refreshed = ensure_sales_catalog(db)
    assert refreshed[0].is_active is True
    assert refreshed[0].price_fcfa == 3_500


def test_queue_prioritizes_more_expensive_active_plan_then_fifo():
    db = _db()
    free_user = _user(db, "free@example.com")
    starter_user = _user(db, "starter@example.com")
    scale_user = _user(db, "scale@example.com")
    starter = Plan(name="Starter test", price_fcfa=3_500, duration_days=30, credits=100_000)
    scale = Plan(name="Scale test", price_fcfa=90_000, duration_days=30, credits=3_500_000)
    db.add_all([starter, scale])
    db.commit()
    now = datetime.utcnow()
    db.add_all([
        Subscription(user_id=starter_user.id, plan_id=starter.id, status="active", started_at=now, expires_at=now + timedelta(days=30)),
        Subscription(user_id=scale_user.id, plan_id=scale.id, status="active", started_at=now, expires_at=now + timedelta(days=30)),
    ])
    channels = [
        Channel(user_id=free_user.id, name="Free", niche="test"),
        Channel(user_id=starter_user.id, name="Starter", niche="test"),
        Channel(user_id=scale_user.id, name="Scale", niche="test"),
    ]
    db.add_all(channels)
    db.commit()
    videos = [
        Video(channel_id=channels[0].id, status="queued", created_at=now - timedelta(minutes=30)),
        Video(channel_id=channels[1].id, status="queued", created_at=now - timedelta(minutes=20)),
        Video(channel_id=channels[2].id, status="queued", created_at=now - timedelta(minutes=10)),
    ]
    db.add_all(videos)
    db.commit()

    positions, total = _queued_video_positions(db)
    assert total == 3
    assert positions[videos[2].id] == 1
    assert positions[videos[1].id] == 2
    assert positions[videos[0].id] == 3
