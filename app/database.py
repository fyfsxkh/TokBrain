"""Own the SQLite engine lifecycle and provide scoped async sessions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import ensure_directories, settings
from app.models import Base
from app.services.migrations import finalize_database, prepare_database


class DatabaseRuntime:
    """A single local engine plus short-lived sessions for each unit of work."""

    def __init__(self, url: str, *, echo: bool) -> None:
        self.engine: AsyncEngine = create_async_engine(url, echo=echo)
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        event.listen(self.engine.sync_engine, "connect", self._prepare_connection)

    @staticmethod
    def _prepare_connection(dbapi_connection, _record) -> None:
        statements = (
            "PRAGMA foreign_keys = ON",
            "PRAGMA journal_mode = WAL",
            "PRAGMA busy_timeout = 5000",
        )
        cursor = dbapi_connection.cursor()
        try:
            for statement in statements:
                cursor.execute(statement)
        finally:
            cursor.close()

    async def initialize(self) -> None:
        ensure_directories()
        await asyncio.to_thread(prepare_database)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await asyncio.to_thread(finalize_database)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        session = self.session_factory()
        try:
            yield session
        finally:
            await session.close()


database = DatabaseRuntime(settings.database_url, echo=settings.debug)

# Stable compatibility names used by worker coordinators and test fixtures.
engine = database.engine
async_session_factory = database.session_factory


async def init_db() -> None:
    await database.initialize()


async def get_db() -> AsyncIterator[AsyncSession]:
    async with database.session() as session:
        yield session


def get_db_context():
    return database.session()
