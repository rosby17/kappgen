from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Base, Channel, Video
from src.models.project import VideoStatus
from src.worker.queue_runner import (
    AUTOMATION_CREDIT_FAILURE_STAGE,
    _record_automation_failure,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_three_consecutive_credit_failures_disable_channel():
    db = _session()
    channel = Channel(name="Inactive owner", niche="Test", automation_mode="auto", is_active=True)
    db.add(channel)
    db.commit()

    for day in range(3):
        _record_automation_failure(db, channel, "Crédits insuffisants.", credit_insufficient=True)
        # The production guard creates at most one card per local day. Move
        # the saved row back a day so this test represents three daily runs.
        row = db.query(Video).order_by(Video.created_at.desc()).first()
        row.created_at = row.created_at.replace(day=max(1, row.created_at.day - (2 - day)))
        db.commit()

    db.refresh(channel)
    assert channel.is_active is False
    assert db.query(Video).filter(Video.progress_stage == AUTOMATION_CREDIT_FAILURE_STAGE).count() == 3


def test_success_breaks_credit_failure_streak():
    db = _session()
    channel = Channel(name="Returning owner", niche="Test", automation_mode="auto", is_active=True)
    db.add(channel)
    db.commit()

    for _ in range(2):
        db.add(Video(
            channel_id=channel.id,
            creation_source="automatic",
            status=VideoStatus.FAILED.value,
            progress_stage=AUTOMATION_CREDIT_FAILURE_STAGE,
        ))
    db.add(Video(
        channel_id=channel.id,
        creation_source="automatic",
        status=VideoStatus.DONE.value,
        progress_stage="Terminé",
    ))
    db.commit()

    _record_automation_failure(db, channel, "Crédits insuffisants.", credit_insufficient=True)

    db.refresh(channel)
    assert channel.is_active is True
