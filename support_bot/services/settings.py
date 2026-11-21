from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from support_bot.db.models import Setting, SystemTopic


class SettingsService:
    def __init__(self, session: AsyncSession, defaults: dict[str, Any]) -> None:
        self.session = session
        self.defaults = defaults

    async def get(self, key: str, default: Any = None) -> Any:
        setting = await self.session.get(Setting, key)
        if setting is not None:
            try:
                return json.loads(setting.value)
            except json.JSONDecodeError:
                pass
        if key in self.defaults:
            return deepcopy(self.defaults[key])
        return deepcopy(default)

    async def set(self, key: str, value: Any) -> None:
        setting = await self.session.get(Setting, key)
        encoded = json.dumps(value, ensure_ascii=False)
        if setting is None:
            self.session.add(Setting(key=key, value=encoded))
        else:
            setting.value = encoded
        await self.session.commit()

    async def statuses(self) -> list[dict[str, Any]]:
        return list(await self.get("statuses", []))

    async def closed_keys(self) -> set[str]:
        return {
            str(status["key"])
            for status in await self.statuses()
            if status.get("closed") or status.get("archive")
        }

    async def archive_keys(self) -> set[str]:
        return {str(status["key"]) for status in await self.statuses() if status.get("archive")}

    async def is_closed(self, key: str) -> bool:
        return key in await self.closed_keys()

    async def status(self, key: str) -> dict[str, Any]:
        for status in await self.statuses():
            if status["key"] == key:
                return status
        raise ValueError(f"Неизвестный статус: {key}")

    async def find_status(self, value: str) -> dict[str, Any] | None:
        normalized = value.strip().casefold()
        for status in await self.statuses():
            if normalized in {
                str(status["key"]).casefold(),
                str(status["title"]).casefold(),
                str(status["code"]).casefold(),
            }:
                return status
        return None

    async def categories(self) -> list[str]:
        return [str(item) for item in await self.get("categories", [])]

    async def set_topic(self, key: str, thread_id: int) -> None:
        topic = await self.session.get(SystemTopic, key)
        if topic is None:
            self.session.add(SystemTopic(key=key, thread_id=thread_id))
        else:
            topic.thread_id = thread_id
        await self.session.commit()

    async def topic(self, key: str) -> int | None:
        topic = await self.session.get(SystemTopic, key)
        return int(topic.thread_id) if topic else None

    async def topics(self) -> dict[str, int]:
        result = await self.session.execute(select(SystemTopic))
        return {topic.key: int(topic.thread_id) for topic in result.scalars()}
