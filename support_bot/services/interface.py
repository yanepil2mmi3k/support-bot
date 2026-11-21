from __future__ import annotations

import json
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession

from support_bot.db.models import Setting

logger = logging.getLogger(__name__)


class InterfaceService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _key(key: str) -> str:
        return f"interface:{key}"

    async def ids(self, key: str) -> list[int]:
        data = await self._data(key)
        values = data.get("messages", [])
        return [int(value) for value in values] if isinstance(values, list) else []

    async def track(self, key: str, chat_id: int, message_id: int) -> None:
        ids = await self.ids(key)
        ids = [value for value in ids if value != message_id]
        ids.append(message_id)
        await self._save(key, {"chat_id": chat_id, "messages": ids[-50:]})

    async def trim(self, key: str, bot: Bot, keep: int = 2) -> None:
        data = await self._data(key)
        ids = data.get("messages", [])
        if len(ids) <= keep:
            return
        chat_id = data.get("chat_id")
        remaining = []
        for message_id in ids[:-keep]:
            if not await self._delete(bot, chat_id, message_id):
                remaining.append(message_id)
        remaining.extend(ids[-keep:])
        await self._save(key, {"chat_id": chat_id, "messages": remaining})

    async def clear(self, key: str, bot: Bot) -> None:
        data = await self._data(key)
        chat_id = data.get("chat_id")
        remaining = []
        for message_id in data.get("messages", []):
            if not await self._delete(bot, chat_id, message_id):
                remaining.append(message_id)
        if remaining:
            await self._save(key, {"chat_id": chat_id, "messages": remaining})
            return
        setting = await self.session.get(Setting, self._key(key))
        if setting:
            await self.session.delete(setting)
        await self.session.commit()

    async def _data(self, key: str) -> dict:
        setting = await self.session.get(Setting, self._key(key))
        if not setting:
            return {}
        try:
            data = json.loads(setting.value)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    async def _save(self, key: str, data: dict) -> None:
        setting = await self.session.get(Setting, self._key(key))
        encoded = json.dumps(data)
        if setting is None:
            self.session.add(Setting(key=self._key(key), value=encoded))
        else:
            setting.value = encoded
        await self.session.commit()

    @staticmethod
    async def _delete(bot: Bot, chat_id: int | None, message_id: int) -> bool:
        if chat_id is None:
            return False
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramBadRequest as exc:
            if "message to delete not found" in str(exc).casefold() or "message not found" in str(exc).casefold():
                logger.debug("Служебное сообщение %s уже удалено", message_id)
                return True
            logger.debug("Не удалось удалить служебное сообщение %s: %s", message_id, exc)
            return False
        except TelegramAPIError as exc:
            logger.debug("Временно не удалось удалить служебное сообщение %s: %s", message_id, exc)
            return False
        return True
