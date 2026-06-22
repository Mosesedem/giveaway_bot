"""
Database engine + session factory.

Reads DATABASE_URL from the environment. Render (and most hosts) inject
this automatically when you attach a Postgres instance. If it's not set,
falls back to a local SQLite file so you can run the whole thing on your
laptop with zero setup before touching Postgres at all.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

load_dotenv()

# Treat blank DATABASE_URL in .env as unset so local dev falls back to SQLite.
DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///./giveaway_bot_dev.db"

# Render/Heroku-style URLs sometimes come as "postgres://" but SQLAlchemy
# with psycopg3 wants "postgresql+psycopg://". Normalize it.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Session:
    """FastAPI dependency: yields a session, always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create tables if they don't exist. Call once at startup."""
    from app import models  # noqa: F401  (ensures models are registered on Base)
    models.Base.metadata.create_all(bind=engine)
