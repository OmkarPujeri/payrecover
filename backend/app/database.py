"""Async database engine, session factory, and declarative base.

Reads ``DATABASE_URL`` from settings. Defaults to PostgreSQL (asyncpg) but
transparently supports ``sqlite+aiosqlite`` for fast, dependency-free tests.
Models use portable column types so the same schema runs on both.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# SQLite needs a couple of tweaks; Postgres benefits from pre-ping.
_engine_kwargs: dict = {"echo": False, "future": True}
if settings.is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    _engine_kwargs["pool_pre_ping"] = True

engine = create_async_engine(settings.database_url, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def init_db() -> None:
    """Create all tables. Imports models so they register on Base.metadata."""
    from app import models  # noqa: F401  (ensure models are imported)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session."""
    async with AsyncSessionLocal() as session:
        yield session
