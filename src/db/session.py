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
    from src.db.models import User, Channel, Video, Folder, ApiKey  # ensure models are imported
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
        }
        with engine.begin() as conn:
            for col_name, ddl in migrations.items():
                if col_name not in existing_user_columns:
                    logger.info(f"Migrating users table: adding {col_name} column.")
                    conn.execute(text(ddl))
