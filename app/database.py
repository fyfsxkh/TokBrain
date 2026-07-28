"""Async SQLite session management."""

import asyncio
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import ensure_directories, settings
from app.models import Base
from app.services.migrations import finalize_database, prepare_database


engine = create_async_engine(settings.database_url, echo=settings.debug, future=True)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    ensure_directories()
    await asyncio.to_thread(prepare_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await asyncio.to_thread(finalize_database)


async def get_db():
    async with async_session_factory() as session:
        yield session


@asynccontextmanager
async def get_db_context():
    async with async_session_factory() as session:
        yield session
