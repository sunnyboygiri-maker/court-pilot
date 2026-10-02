"""
Async SQLAlchemy engine/session setup.

DATABASE_URL in settings uses the plain `postgresql://` scheme (shared with
Alembic, which runs synchronously on psycopg2). The async engine swaps in
the asyncpg driver.
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config.settings import settings


def async_database_url(url: str) -> str:
    for prefix in ("postgresql+psycopg2://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


def sync_database_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url[len("postgresql+asyncpg://"):]
    return url


def make_engine(url: str | None = None, **kwargs) -> AsyncEngine:
    return create_async_engine(async_database_url(url or settings.DATABASE_URL), **kwargs)


engine: AsyncEngine = make_engine(pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def worker_session() -> AsyncIterator[AsyncSession]:
    """
    Session for Celery tasks. Each task runs its own event loop via
    asyncio.run(), and pooled asyncpg connections can't cross loops,
    so use a throwaway engine with no pooling.
    """
    eng = make_engine(poolclass=NullPool)
    try:
        async with async_sessionmaker(eng, expire_on_commit=False)() as session:
            yield session
    finally:
        await eng.dispose()
