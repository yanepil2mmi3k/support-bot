from __future__ import annotations

import json
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy import text

from .models import FsmRecord, utcnow
from .session import Database


class SqliteStorage(BaseStorage):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def close(self) -> None:
        pass

    @staticmethod
    def _key(key: StorageKey) -> str:
        values = (
            key.bot_id,
            key.chat_id,
            key.user_id,
            key.thread_id,
            key.business_connection_id,
            key.destiny,
        )
        return ":".join(str(value) for value in values)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        await self._save(key, state=state.state if isinstance(state, State) else state)

    async def get_state(self, key: StorageKey) -> str | None:
        async with self.database.sessions() as session:
            record = await session.get(FsmRecord, self._key(key))
            return record.state if record else None

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        await self._save(key, data=json.dumps(data, ensure_ascii=False))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self.database.sessions() as session:
            record = await session.get(FsmRecord, self._key(key))
            if not record:
                return {}
            try:
                value = json.loads(record.data)
            except json.JSONDecodeError:
                return {}
            return value if isinstance(value, dict) else {}

    async def update_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        storage_key = self._key(key)
        async with self.database.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            record = await session.get(FsmRecord, storage_key)
            try:
                current = json.loads(record.data if record else "{}")
            except json.JSONDecodeError:
                current = {}
            if not isinstance(current, dict):
                current = {}
            current.update(data)
            if record is None:
                record = FsmRecord(key=storage_key)
                session.add(record)
            record.data = json.dumps(current, ensure_ascii=False)
            record.updated_at = utcnow()
            await session.commit()

    async def _save(self, key: StorageKey, **changes: Any) -> None:
        storage_key = self._key(key)
        async with self.database.sessions() as session:
            record = await session.get(FsmRecord, storage_key)
            if record is None:
                record = FsmRecord(key=storage_key)
                session.add(record)
            for field, value in changes.items():
                setattr(record, field, value)
            record.updated_at = utcnow()
            await session.commit()
