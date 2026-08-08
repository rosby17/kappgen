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
    from src.db.models import User, Channel, Video, Folder  # ensure models are imported
    Base.metadata.create_all(bind=engine)

    # Lightweight migration: create_all only adds missing tables, not missing
    # columns on tables that already exist in production. Add folder_id to an
    # already-deployed `videos` table if it predates the Folder feature.
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    if "videos" in inspector.get_table_names():
        existing_columns = {col["name"] for col in inspector.get_columns("videos")}
        if "folder_id" not in existing_columns:
            logger.info("Migrating videos table: adding folder_id column.")
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE videos ADD COLUMN folder_id VARCHAR(36)"))
