from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from support_bot.config import Config
from support_bot.db.models import MessageLink, QuickReply, QuickReplyCopy, QuickReplyItem, SystemTopic, Ticket, utcnow
from support_bot.services.tickets import extract_message_media

logger = logging.getLogger(__name__)
UNSET = object()


@dataclass
class QuickReplyUpload:
    media_type: str
    path: Path
    file_name: str
    mime_type: str | None
    file_size: int


def _telegram_error_text(exc: TelegramBadRequest) -> str:
    return exc.message.lower().replace(" ", "_")


def _missing_message(exc: TelegramBadRequest) -> bool:
    return "not_found" in _telegram_error_text(exc)


def _not_modified(exc: TelegramBadRequest) -> bool:
    return "message_is_not_modified" in _telegram_error_text(exc)


class QuickReplyService:
    def __init__(self, session: AsyncSession, bot: Bot, config: Config) -> None:
        self.session = session
        self.bot = bot
        self.config = config

    async def _thread_id(self) -> int:
        topic = await self.session.get(SystemTopic, "quick_replies")
        if topic is None:
            raise ValueError("Сначала выполните /setup в общем топике группы поддержки")
        return int(topic.thread_id)

    async def get(self, reply_id: int) -> QuickReply | None:
        return await self.session.scalar(
            select(QuickReply)
            .options(selectinload(QuickReply.items))
            .where(QuickReply.id == reply_id)
        )

    async def list(self) -> list[QuickReply]:
        return list((await self.session.execute(
            select(QuickReply).options(selectinload(QuickReply.items)).order_by(QuickReply.position, QuickReply.id)
        )).scalars())

    async def _send_upload(self, upload: QuickReplyUpload, *, thread_id: int, caption: str = "") -> QuickReplyItem:
        sender = getattr(self.bot, f"send_{upload.media_type}")
        from aiogram.types import FSInputFile

        message = await sender(
            self.config.support_group_id,
            **{upload.media_type: FSInputFile(upload.path, filename=upload.file_name)},
            caption=caption or None,
            parse_mode=None,
            message_thread_id=thread_id,
        )
        media: dict[str, Any] = extract_message_media(message)
        if not media.get("media_file_id"):
            raise ValueError("Telegram не вернул идентификатор файла")
        media["file_name"] = media["file_name"] or upload.file_name
        media["mime_type"] = media["mime_type"] or upload.mime_type
        media["file_size"] = media["file_size"] or upload.file_size
        return QuickReplyItem(
            source_chat_id=self.config.support_group_id,
            source_message_id=message.message_id,
            text=caption,
            content_type=upload.media_type,
            **media,
        )

    async def _cleanup_sources(self, items: list[QuickReplyItem]) -> None:
        for item in items:
            try:
                await self.bot.delete_message(chat_id=item.source_chat_id, message_id=item.source_message_id)
            except Exception:
                logger.warning("Не удалось удалить незавершённый источник быстрого ответа", exc_info=True)

    async def create(self, title: str, text: str, uploads: list[QuickReplyUpload]) -> QuickReply:
        title = title.strip()
        if not title or len(title) > 128:
            raise ValueError("Название должно содержать от 1 до 128 символов")
        if not text and not uploads:
            raise ValueError("Добавьте текст или хотя бы один файл")
        if len(text) > (1024 if uploads else 4096):
            raise ValueError("Подпись должна быть не длиннее 1024 символов" if uploads else "Текст должен быть не длиннее 4096 символов")
        thread_id = await self._thread_id()
        items: list[QuickReplyItem] = []
        try:
            if uploads:
                for position, upload in enumerate(uploads):
                    items.append(await self._send_upload(upload, thread_id=thread_id, caption=text if position == 0 else ""))
            else:
                message = await self.bot.send_message(
                    self.config.support_group_id,
                    text,
                    parse_mode=None,
                    message_thread_id=thread_id,
                )
                items.append(QuickReplyItem(
                    source_chat_id=self.config.support_group_id,
                    source_message_id=message.message_id,
                    text=text,
                    content_type="text",
                ))
            for position, item in enumerate(items):
                item.position = position
            position = await self.session.scalar(select(func.max(QuickReply.position)))
            reply = QuickReply(title=title, position=int(position if position is not None else -1) + 1, items=items)
            self.session.add(reply)
            await self.session.commit()
            return reply
        except Exception:
            await self.session.rollback()
            await self._cleanup_sources(items)
            raise

    async def add_items(self, reply: QuickReply, uploads: list[QuickReplyUpload]) -> QuickReply:
        if not uploads:
            raise ValueError("Передайте хотя бы один файл")
        thread_id = await self._thread_id()
        items: list[QuickReplyItem] = []
        try:
            for upload in uploads:
                items.append(await self._send_upload(upload, thread_id=thread_id))
            position = max((item.position for item in reply.items), default=-1) + 1
            for item in items:
                item.quick_reply_id = reply.id
                item.position = position
                position += 1
                self.session.add(item)
            await self.session.commit()
            await self.session.refresh(reply, ["items"])
            return reply
        except Exception:
            await self.session.rollback()
            await self._cleanup_sources(items)
            raise

    @staticmethod
    def _ordered_ids(reply: QuickReply, item_order: list[int]) -> list[int]:
        if (
            any(isinstance(item_id, bool) or not isinstance(item_id, int) for item_id in item_order)
            or len(item_order) != len(reply.items)
            or len(set(item_order)) != len(item_order)
            or set(item_order) != {item.id for item in reply.items}
        ):
            raise ValueError("item_order должен содержать каждый элемент быстрого ответа ровно один раз")
        return item_order

    async def _edit_source_text(self, item: QuickReplyItem, text: str) -> None:
        if item.content_type == "text":
            await self.bot.edit_message_text(
                text=text,
                chat_id=item.source_chat_id,
                message_id=item.source_message_id,
                parse_mode=None,
            )
        else:
            await self.bot.edit_message_caption(
                caption=text or None,
                chat_id=item.source_chat_id,
                message_id=item.source_message_id,
                parse_mode=None,
            )

    async def _edit_copy_text(self, link: MessageLink, item: QuickReplyItem, text: str) -> None:
        method = self.bot.edit_message_text if item.content_type == "text" else self.bot.edit_message_caption
        for chat_id, message_id in (
            (link.source_chat_id, link.source_message_id),
            (link.target_chat_id, link.target_message_id),
        ):
            values = {"chat_id": chat_id, "message_id": message_id, "parse_mode": None}
            if item.content_type == "text":
                values["text"] = text
            else:
                values["caption"] = text or None
            try:
                await method(**values)
            except TelegramBadRequest as exc:
                if not _missing_message(exc) and not _not_modified(exc):
                    raise

    async def update(
        self,
        reply: QuickReply,
        *,
        title: str | object = UNSET,
        text: str | object = UNSET,
        item_order: list[int] | object = UNSET,
    ) -> QuickReply:
        if title is UNSET and text is UNSET and item_order is UNSET:
            raise ValueError("Передайте title, text или item_order")
        if title is not UNSET:
            if not isinstance(title, str) or not title.strip() or len(title.strip()) > 128:
                raise ValueError("Название должно содержать от 1 до 128 символов")
        if text is not UNSET:
            if not isinstance(text, str):
                raise ValueError("Текст должен быть строкой")
            if not reply.items:
                raise ValueError("В быстром ответе нет исходного сообщения")
            first = reply.items[0]
            limit = 4096 if first.content_type == "text" else 1024
            if len(text) > limit or (first.content_type == "text" and not text):
                raise ValueError(f"Текст должен содержать от 1 до {limit} символов" if first.content_type == "text" else f"Подпись должна быть не длиннее {limit} символов")
        if item_order is not UNSET:
            if not isinstance(item_order, list):
                raise ValueError("item_order должен быть списком идентификаторов")
            self._ordered_ids(reply, item_order)

        if title is not UNSET:
            reply.title = title.strip()  # type: ignore[union-attr]
        if text is not UNSET:
            first = reply.items[0]
            copies = list((await self.session.execute(
                select(QuickReplyCopy)
                .options(selectinload(QuickReplyCopy.message_link))
                .where(QuickReplyCopy.item_id == first.id)
                .order_by(QuickReplyCopy.id)
            )).scalars())
            await self._edit_source_text(first, text)  # type: ignore[arg-type]
            for copy in copies:
                await self._edit_copy_text(copy.message_link, first, text)  # type: ignore[arg-type]
            first.text = text  # type: ignore[assignment]
            for copy in copies:
                copy.message_link.body = text  # type: ignore[assignment]
        if item_order is not UNSET:
            by_id = {item.id: item for item in reply.items}
            for position, item_id in enumerate(item_order):  # type: ignore[union-attr]
                by_id[item_id].position = position
        await self.session.commit()
        return await self.get(reply.id)  # type: ignore[return-value]

    @staticmethod
    def _copy_points(item: QuickReplyItem, copies: list[QuickReplyCopy]) -> list[tuple[int, int]]:
        points = [(item.source_chat_id, item.source_message_id)]
        for copy in copies:
            link = copy.message_link
            points.extend([
                (link.source_chat_id, link.source_message_id),
                (link.target_chat_id, link.target_message_id),
            ])
        return points

    async def _delete_points(self, points: list[tuple[int, int]]) -> None:
        first_error: Exception | None = None
        for chat_id, message_id in points:
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except TelegramBadRequest as exc:
                if not _missing_message(exc) and first_error is None:
                    first_error = exc
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def _copies(self, item_id: int) -> list[QuickReplyCopy]:
        return list((await self.session.execute(
            select(QuickReplyCopy)
            .options(selectinload(QuickReplyCopy.message_link))
            .where(QuickReplyCopy.item_id == item_id)
            .order_by(QuickReplyCopy.id)
        )).scalars())

    async def delete_item(self, item: QuickReplyItem) -> None:
        copies = await self._copies(item.id)
        await self._delete_points(self._copy_points(item, copies))
        for copy in copies:
            await self.session.delete(copy.message_link)
        reply_id = item.quick_reply_id
        await self.session.delete(item)
        await self.session.flush()
        remaining = list((await self.session.execute(
            select(QuickReplyItem).where(QuickReplyItem.quick_reply_id == reply_id).order_by(QuickReplyItem.position, QuickReplyItem.id)
        )).scalars())
        for position, remaining_item in enumerate(remaining):
            remaining_item.position = position
        await self.session.commit()

    async def delete_reply(self, reply: QuickReply) -> None:
        copies_by_item = {item.id: await self._copies(item.id) for item in reply.items}
        points = []
        for item in reply.items:
            points.extend(self._copy_points(item, copies_by_item[item.id]))
        await self._delete_points(points)
        for copies in copies_by_item.values():
            for copy in copies:
                await self.session.delete(copy.message_link)
        await self.session.delete(reply)
        await self.session.commit()

    async def send(self, reply: QuickReply, ticket: Ticket) -> list[MessageLink]:
        links = []
        for item in reply.items:
            existing = await self.session.scalar(
                select(QuickReplyCopy)
                .join(QuickReplyCopy.message_link)
                .options(selectinload(QuickReplyCopy.message_link))
                .where(QuickReplyCopy.item_id == item.id, MessageLink.ticket_id == ticket.id)
                .order_by(QuickReplyCopy.id)
                .limit(1)
            )
            if existing is not None:
                links.append(existing.message_link)
                continue
            staff_message = await self.bot.copy_message(
                self.config.support_group_id,
                item.source_chat_id,
                item.source_message_id,
                message_thread_id=ticket.thread_id,
            )
            try:
                user_message = await self.bot.copy_message(
                    ticket.user_id,
                    item.source_chat_id,
                    item.source_message_id,
                )
            except Exception:
                try:
                    await self.bot.delete_message(
                        chat_id=self.config.support_group_id, message_id=staff_message.message_id
                    )
                except Exception:
                    logger.warning("Не удалось откатить копию быстрого ответа в топике", exc_info=True)
                raise
            link = MessageLink(
                ticket_id=ticket.id,
                source_chat_id=self.config.support_group_id,
                source_message_id=staff_message.message_id,
                target_chat_id=ticket.user_id,
                target_message_id=user_message.message_id,
                body=item.text,
                content_type=item.content_type,
                media_file_id=item.media_file_id,
                thumbnail_file_id=item.thumbnail_file_id,
                file_name=item.file_name,
                mime_type=item.mime_type,
                file_size=item.file_size,
                from_staff=True,
            )
            try:
                self.session.add(link)
                await self.session.flush()
                self.session.add(QuickReplyCopy(item_id=item.id, message_link_id=link.id))
                now = utcnow()
                ticket.last_support_message_at = now
                ticket.updated_at = now
                await self.session.commit()
            except Exception:
                await self.session.rollback()
                await self._delete_points([
                    (self.config.support_group_id, staff_message.message_id),
                    (ticket.user_id, user_message.message_id),
                ])
                raise
            links.append(link)
        return links
