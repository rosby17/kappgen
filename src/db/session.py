from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from src.config import DATABASE_URL
from src.utils.logger import logger

db_url = DATABASE_URL
# Handle Supabase Postgres dialect string formatting if needed
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
elif db_url.startswith("postgresql://") and not db_url.startswith("postgresql+psycopg2://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

logger.info(f"Connecting database engine: {'PostgreSQL / Supabase' if 'postgresql' in db_url else 'SQLite (' + db_url + ')'}")

# Engine configuration for SQLite or PostgreSQL (Supabase)
engine_kwargs = {}
if db_url.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    engine_kwargs["pool_pre_ping"] = True
    engine_kwargs["pool_size"] = 10
    engine_kwargs["max_overflow"] = 20

engine = create_engine(db_url, **engine_kwargs)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    from src.db.models import User, Channel, Video, Folder, ApiKey, PasswordReset  # ensure models are imported
    Base.metadata.create_all(bind=engine)

    # Lightweight migration: create_all only adds missing tables, not missing
    # columns on tables that already exist in production.
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    if "videos" in inspector.get_table_names():
        existing_columns = {col["name"] for col in inspector.get_columns("videos")}
        video_migrations = {
            "folder_id": "ALTER TABLE videos ADD COLUMN folder_id VARCHAR(36)",
            "duration_seconds": "ALTER TABLE videos ADD COLUMN duration_seconds FLOAT",
            "estimated_duration_seconds": "ALTER TABLE videos ADD COLUMN estimated_duration_seconds FLOAT",
            "purged_at": "ALTER TABLE videos ADD COLUMN purged_at TIMESTAMP",
            "restart_count": "ALTER TABLE videos ADD COLUMN restart_count INTEGER DEFAULT 0 NOT NULL",
            "progress_stage": "ALTER TABLE videos ADD COLUMN progress_stage VARCHAR(255)",
            "progress_percent": "ALTER TABLE videos ADD COLUMN progress_percent INTEGER DEFAULT 0 NOT NULL",
            "is_reassembly": "ALTER TABLE videos ADD COLUMN is_reassembly BOOLEAN DEFAULT FALSE NOT NULL",
            "edit_assets_purged_at": "ALTER TABLE videos ADD COLUMN edit_assets_purged_at TIMESTAMP",
            "transcribe_audio": "ALTER TABLE videos ADD COLUMN transcribe_audio BOOLEAN DEFAULT TRUE NOT NULL",
            "voice_id": "ALTER TABLE videos ADD COLUMN voice_id VARCHAR(255)",
            "pending_edit": "ALTER TABLE videos ADD COLUMN pending_edit JSON",
            "title": "ALTER TABLE videos ADD COLUMN title VARCHAR(255)",
            "youtube_video_id": "ALTER TABLE videos ADD COLUMN youtube_video_id VARCHAR(32)",
            "youtube_published_at": "ALTER TABLE videos ADD COLUMN youtube_published_at TIMESTAMP",
            "youtube_publish_error": "ALTER TABLE videos ADD COLUMN youtube_publish_error TEXT",
            "youtube_description": "ALTER TABLE videos ADD COLUMN youtube_description TEXT",
            "scheduled_publish_at": "ALTER TABLE videos ADD COLUMN scheduled_publish_at TIMESTAMP",
            "approved_for_publish": "ALTER TABLE videos ADD COLUMN approved_for_publish BOOLEAN DEFAULT FALSE NOT NULL",
        }
        with engine.begin() as conn:
            for col_name, ddl in video_migrations.items():
                if col_name not in existing_columns:
                    logger.info(f"Migrating videos table: adding {col_name} column.")
                    conn.execute(text(ddl))

    if "users" in inspector.get_table_names():
        existing_user_columns = {col["name"] for col in inspector.get_columns("users")}
        migrations = {
            "picture_url": "ALTER TABLE users ADD COLUMN picture_url VARCHAR(1024)",
            "phone": "ALTER TABLE users ADD COLUMN phone VARCHAR(50)",
            "auth_provider": "ALTER TABLE users ADD COLUMN auth_provider VARCHAR(50) DEFAULT 'password'",
            "izivoice_api_key_encrypted": "ALTER TABLE users ADD COLUMN izivoice_api_key_encrypted TEXT",
            "izivoice_key_prefix": "ALTER TABLE users ADD COLUMN izivoice_key_prefix VARCHAR(20)",
            "izivoice_connected_at": "ALTER TABLE users ADD COLUMN izivoice_connected_at TIMESTAMP",
        }
        with engine.begin() as conn:
            for col_name, ddl in migrations.items():
                if col_name not in existing_user_columns:
                    logger.info(f"Migrating users table: adding {col_name} column.")
                    conn.execute(text(ddl))

    if "channels" in inspector.get_table_names():
        existing_channel_columns = {col["name"] for col in inspector.get_columns("channels")}
        channel_migrations = {
            "automation_mode": "ALTER TABLE channels ADD COLUMN automation_mode VARCHAR(20) DEFAULT 'manual' NOT NULL",
            "automation_style_prompt": "ALTER TABLE channels ADD COLUMN automation_style_prompt TEXT",
            "last_auto_run_date": "ALTER TABLE channels ADD COLUMN last_auto_run_date VARCHAR(10)",
            "script_structure": "ALTER TABLE channels ADD COLUMN script_structure JSON",
            "voice_id": "ALTER TABLE channels ADD COLUMN voice_id VARCHAR(255)",
            "voice_name": "ALTER TABLE channels ADD COLUMN voice_name VARCHAR(255)",
            "voice_settings": "ALTER TABLE channels ADD COLUMN voice_settings JSON",
            "youtube_channel_id": "ALTER TABLE channels ADD COLUMN youtube_channel_id VARCHAR(64)",
            "youtube_channel_title": "ALTER TABLE channels ADD COLUMN youtube_channel_title VARCHAR(255)",
            "youtube_channel_handle": "ALTER TABLE channels ADD COLUMN youtube_channel_handle VARCHAR(255)",
            "youtube_channel_thumbnail_url": "ALTER TABLE channels ADD COLUMN youtube_channel_thumbnail_url VARCHAR(1024)",
            "youtube_access_token": "ALTER TABLE channels ADD COLUMN youtube_access_token TEXT",
            "youtube_refresh_token": "ALTER TABLE channels ADD COLUMN youtube_refresh_token TEXT",
            "youtube_token_expiry": "ALTER TABLE channels ADD COLUMN youtube_token_expiry TIMESTAMP",
            "youtube_connected_at": "ALTER TABLE channels ADD COLUMN youtube_connected_at TIMESTAMP",
            "publish_mode": "ALTER TABLE channels ADD COLUMN publish_mode VARCHAR(20) DEFAULT 'manual' NOT NULL",
            "publish_schedule_hour": "ALTER TABLE channels ADD COLUMN publish_schedule_hour INTEGER DEFAULT 8 NOT NULL",
            "publish_schedule_day_offset": "ALTER TABLE channels ADD COLUMN publish_schedule_day_offset INTEGER DEFAULT 1 NOT NULL",
            "timezone": "ALTER TABLE channels ADD COLUMN timezone VARCHAR(64) DEFAULT 'Africa/Douala' NOT NULL",
            "videos_per_day": "ALTER TABLE channels ADD COLUMN videos_per_day INTEGER DEFAULT 1 NOT NULL",
            "automation_window_start_hour": "ALTER TABLE channels ADD COLUMN automation_window_start_hour INTEGER DEFAULT 7 NOT NULL",
            "automation_window_end_hour": "ALTER TABLE channels ADD COLUMN automation_window_end_hour INTEGER DEFAULT 11 NOT NULL",
            "active_days": "ALTER TABLE channels ADD COLUMN active_days JSON",
            "auto_videos_generated_today": "ALTER TABLE channels ADD COLUMN auto_videos_generated_today INTEGER DEFAULT 0 NOT NULL",
        }
        with engine.begin() as conn:
            for col_name, ddl in channel_migrations.items():
                if col_name not in existing_channel_columns:
                    logger.info(f"Migrating channels table: adding {col_name} column.")
                    conn.execute(text(ddl))
