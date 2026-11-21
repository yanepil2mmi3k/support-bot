from __future__ import annotations

from pathlib import Path

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import Base, Counter


class Database:
    def __init__(self, url: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(url, echo=False, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
            dbapi_connection.execute("PRAGMA journal_mode=WAL")
            dbapi_connection.execute("PRAGMA busy_timeout=5000")

    async def init(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            rows = await connection.execute(text("PRAGMA table_info(tickets)"))
            columns = {row[1] for row in rows}
            additions = {
                "initial_message_ids": "TEXT NOT NULL DEFAULT ''",
                "topic_media_message_id": "BIGINT",
            }
            for name, definition in additions.items():
                if name not in columns:
                    await connection.execute(text(f"ALTER TABLE tickets ADD COLUMN {name} {definition}"))
            rows = await connection.execute(text("PRAGMA table_info(message_links)"))
            columns = {row[1] for row in rows}
            message_additions = {
                "body": "TEXT",
                "content_type": "VARCHAR(30) NOT NULL DEFAULT 'text'",
                "media_file_id": "VARCHAR(300)",
                "thumbnail_file_id": "VARCHAR(300)",
                "file_name": "VARCHAR(255)",
                "mime_type": "VARCHAR(100)",
                "file_size": "BIGINT",
                "from_staff": "BOOLEAN NOT NULL DEFAULT FALSE",
                "created_at": "DATETIME",
            }
            for name, definition in message_additions.items():
                if name not in columns:
                    await connection.execute(text(f"ALTER TABLE message_links ADD COLUMN {name} {definition}"))
            await connection.execute(text("UPDATE message_links SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
        async with self.sessions() as session:
            counter = await session.get(Counter, "ticket")
            if counter is None:
                session.add(Counter(name="ticket", value=0))
                await session.commit()

    async def counter_value(self) -> int:
        async with self.sessions() as session:
            result = await session.scalar(select(Counter.value).where(Counter.name == "ticket"))
            return result or 0

    async def close(self) -> None:
        await self.engine.dispose()
