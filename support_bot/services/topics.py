from __future__ import annotations

import html
import json
import logging
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from sqlalchemy import select

from support_bot.config import Config
from support_bot.db.models import Employee, MessageLink, Review, Ticket
from support_bot.services.interface import InterfaceService
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import TicketService, extract_message_media, message_body

logger = logging.getLogger(__name__)


def esc(value: object) -> str:
    return html.escape(str(value or ""))


def topic_title(
    ticket: Ticket, employee: Employee | None = None, status_title: str | None = None
) -> str:
    assignee = employee or ticket.employee
    base = ticket.custom_title or (assignee.nickname if assignee else "Ожидает")
    if status_title is None:
        return f"{base} #{ticket.number}"[:128]
    suffix = f"; #{ticket.number}; {status_title}"
    return f"{base[:max(1, 128 - len(suffix))]}{suffix}"


def rating_keyboard(number: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=str(value), callback_data=f"rate:{number}:{value}") for value in range(1, 6)],
            [InlineKeyboardButton(text="Переоткрыть обращение", callback_data=f"reopen:{number}")],
        ]
    )


class TopicService:
    def __init__(self, bot: Bot, config: Config) -> None:
        self.bot = bot
        self.config = config

    async def publish_request(
        self, tickets: TicketService, settings: SettingsService, ticket: Ticket, media_message_id: int | None
    ) -> None:
        if ticket.request_message_id is not None:
            return
        thread_id = await settings.topic("requests")
        if thread_id is None:
            raise RuntimeError("Сначала выполните /setup в общем топике группы поддержки")
        if ticket.request_media_message_id is None:
            media_senders = {
                "photo": "send_photo",
                "video": "send_video",
                "document": "send_document",
                "animation": "send_animation",
                "audio": "send_audio",
                "voice": "send_voice",
            }
            media_type = (ticket.media_type or "").split(".")[-1].lower()
            if ticket.media_file_id and media_type in media_senders:
                sender = getattr(self.bot, media_senders[media_type])
                sent = await sender(
                    self.config.support_group_id,
                    **{media_type: ticket.media_file_id},
                    caption=self.request_preview(ticket, 1024),
                    message_thread_id=thread_id,
                )
            elif ticket.media_file_id and media_message_id and ticket.user_id:
                await self.bot.send_message(
                    self.config.support_group_id,
                    self.request_preview(ticket, 4096),
                    message_thread_id=thread_id,
                )
                sent = await self.bot.copy_message(
                    self.config.support_group_id,
                    ticket.user_id,
                    media_message_id,
                    message_thread_id=thread_id,
                )
            else:
                sent = await self.bot.send_message(
                    self.config.support_group_id,
                    self.request_preview(ticket, 4096),
                    message_thread_id=thread_id,
                )
            ticket.request_media_message_id = sent.message_id
            await tickets.session.commit()
        controls = await self.bot.send_message(
            self.config.support_group_id,
            f"<b>Управление обращением №{ticket.number}</b>",
            message_thread_id=thread_id,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text=f"Принять заявку №{ticket.number}", callback_data=f"accept:{ticket.number}"
                    )
                ]]
            ),
        )
        ticket.request_message_id = controls.message_id
        await tickets.session.commit()

    async def _rename_topic(self, ticket: Ticket, name: str) -> None:
        try:
            await self.bot.edit_forum_topic(
                self.config.support_group_id,
                ticket.thread_id,
                name=name,
            )
        except TelegramBadRequest as exc:
            if "topic_not_modified" not in exc.message.lower().replace(" ", "_"):
                raise

    async def create_ticket_topic(
        self,
        tickets: TicketService,
        settings: SettingsService,
        ticket: Ticket,
        employee: Employee | None,
        media_message_id: int | None = None,
    ) -> None:
        created_topic = False
        if ticket.thread_id is None:
            status = await settings.status(ticket.status)
            forum_topic = await self.bot.create_forum_topic(
                self.config.support_group_id, topic_title(ticket, employee, status["title"])
            )
            ticket.thread_id = forum_topic.message_thread_id
            await tickets.session.commit()
            created_topic = True
        try:
            initial_ids = json.loads(ticket.initial_message_ids or "[]")
        except json.JSONDecodeError:
            initial_ids = []
        parts = self.request_card_parts(ticket)
        for card in parts[len(initial_ids):]:
            sent = await self.bot.send_message(
                self.config.support_group_id,
                card,
                message_thread_id=ticket.thread_id,
            )
            initial_ids.append(sent.message_id)
            ticket.initial_message_ids = json.dumps(initial_ids)
            await tickets.session.commit()
        if ticket.media_file_id and ticket.topic_media_message_id is None:
            if ticket.request_media_message_id:
                source_chat = self.config.support_group_id
                source_message = ticket.request_media_message_id
            elif media_message_id and ticket.user_id:
                source_chat = ticket.user_id
                source_message = media_message_id
            else:
                source_chat = None
                source_message = None
            if source_chat and source_message:
                copied = await self.bot.copy_message(
                    self.config.support_group_id,
                    source_chat,
                    source_message,
                    message_thread_id=ticket.thread_id,
                    caption="",
                )
                ticket.topic_media_message_id = copied.message_id
                await tickets.session.commit()
        await self.update_pinned(tickets, settings, ticket)
        if ticket.request_message_id:
            requests_thread = await settings.topic("requests")
            try:
                await self.bot.edit_message_reply_markup(
                    chat_id=self.config.support_group_id,
                    message_id=ticket.request_message_id,
                    reply_markup=None,
                )
            except TelegramBadRequest:
                pass
            if created_topic and requests_thread:
                await self.bot.send_message(
                    self.config.support_group_id,
                    f"Обращение #{ticket.number} принято: {esc(employee.nickname if employee else '')}",
                    message_thread_id=requests_thread,
                    reply_to_message_id=ticket.request_message_id,
                    allow_sending_without_reply=True,
                )

    async def update_pinned(self, tickets: TicketService, settings: SettingsService, ticket: Ticket) -> None:
        if not ticket.thread_id:
            return
        status = await settings.status(ticket.status)
        await self._rename_topic(ticket, topic_title(ticket, status_title=status["title"]))
        assignee = ticket.employee.nickname if ticket.employee else "Не назначен"
        dates = [ticket.last_user_message_at]
        if ticket.last_support_message_at:
            dates.append(ticket.last_support_message_at)
        last_response = max(dates)
        note = ticket.note or "Не задано"
        note_preview = esc(note[:500]) + ("…" if len(note) > 500 else "")
        text = (
            f"<b>Обращение №{ticket.number}</b>\n\n"
            f"<b>Статус:</b> {esc(status['code'])} ({esc(status['title'])})\n"
            f"<b>Ответственный:</b> {esc(assignee)}\n"
            f"<b>Создано:</b> {self.format_datetime(ticket.created_at)}\n"
            f"<b>Последний ответ:</b> {self.format_datetime(last_response)}\n\n"
            f"<b>Рабочее описание</b>\n{note_preview}"
        )
        if ticket.pinned_message_id:
            try:
                await self.bot.edit_message_text(
                    text=text,
                    chat_id=self.config.support_group_id,
                    message_id=ticket.pinned_message_id,
                    reply_markup=None,
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in exc.message.lower():
                    return
                logger.warning("Не удалось изменить закреп тикета %s: %s", ticket.number, exc.message)
        sent = await self.bot.send_message(self.config.support_group_id, text, message_thread_id=ticket.thread_id)
        ticket.pinned_message_id = sent.message_id
        await tickets.session.commit()
        try:
            await self.bot.pin_chat_message(
                self.config.support_group_id, sent.message_id, disable_notification=True
            )
        except TelegramBadRequest as exc:
            logger.warning("Не удалось закрепить сообщение тикета %s: %s", ticket.number, exc.message)

    async def transfer(
        self,
        tickets: TicketService,
        settings: SettingsService,
        ticket: Ticket,
        target: Employee,
        new_status: str | None = None,
    ) -> None:
        previous = ticket.employee
        was_closed = await settings.is_closed(ticket.status)
        status_data = await settings.status(new_status) if new_status else None
        await tickets.assign(ticket, target, transfer=True)
        if status_data and not (status_data.get("closed") or status_data.get("archive")):
            if was_closed:
                await self.reopen(tickets, settings, ticket)
            await tickets.set_status(ticket, new_status, status_data)
        if ticket.thread_id:
            current_status = await settings.status(ticket.status)
            await self._rename_topic(ticket, topic_title(ticket, target, current_status["title"]))
            notice = f"Тикет передан сотруднику {esc(target.name)}."
            if previous and previous.gender != target.gender:
                gender = "женский" if previous.gender == "f" else "мужской"
                notice += f" Ранее с клиентом общался сотрудник, использующий {gender} род. Учитывайте это в формулировках."
            await self.bot.send_message(
                self.config.support_group_id, notice, message_thread_id=ticket.thread_id
            )
        if status_data and (status_data.get("closed") or status_data.get("archive")):
            await self.close(tickets, settings, ticket, status_data)
        elif ticket.thread_id:
            await self.update_pinned(tickets, settings, ticket)

    async def greet(self, ticket: Ticket, employee: Employee) -> None:
        if not ticket.user_id:
            return
        if employee.greeting:
            text = employee.greeting.format(name=employee.name)
        else:
            text = self.config.message("default_greeting", name=employee.name)
            if employee.gender == "f":
                text = text.replace("ознакомился", "ознакомилась")
        await self.bot.send_message(ticket.user_id, text)

    async def close(
        self,
        tickets: TicketService,
        settings: SettingsService,
        ticket: Ticket,
        status_data: dict | None = None,
    ) -> bool:
        target = status_data or await settings.status("archive")
        was_closed = await settings.is_closed(ticket.status)
        if ticket.thread_id and not was_closed:
            await self.bot.close_forum_topic(self.config.support_group_id, ticket.thread_id)
        changed = await tickets.close(ticket, target)
        await self.update_pinned(tickets, settings, ticket)
        if ticket.user_id:
            interface = InterfaceService(tickets.session)
            interface_key = f"user:{ticket.user_id}"
            rating_key = f"rating:{ticket.number}"
            pending = await interface.ids(rating_key)
            review = await tickets.session.scalar(select(Review).where(Review.ticket_id == ticket.id))
            if not was_closed:
                await interface.clear(interface_key, self.bot)
            if not was_closed or (not pending and review is None):
                sent = await self.bot.send_message(
                    ticket.user_id,
                    self.config.message("ticket_closed", number=ticket.number),
                    reply_markup=rating_keyboard(ticket.number),
                )
                await interface.track(interface_key, ticket.user_id, sent.message_id)
                await interface.track(rating_key, ticket.user_id, sent.message_id)
                from support_bot.handlers.user import faq_keyboard

                menu = await self.bot.send_message(
                    ticket.user_id,
                    self.config.message("start"),
                    reply_markup=faq_keyboard(self.config),
                )
                await interface.track(interface_key, ticket.user_id, menu.message_id)
        return changed

    async def reopen(self, tickets: TicketService, settings: SettingsService, ticket: Ticket) -> bool:
        if not await settings.is_closed(ticket.status):
            return False
        if ticket.user_id:
            active = await tickets.active_for_user(ticket.user_id, await settings.closed_keys())
            if active and active.id != ticket.id:
                raise ValueError(f"Сначала закройте активное обращение №{active.number}")
        if ticket.thread_id:
            await self.bot.reopen_forum_topic(self.config.support_group_id, ticket.thread_id)
        changed = await tickets.reopen(ticket, await settings.closed_keys())
        if not changed:
            return False
        await InterfaceService(tickets.session).clear(f"rating:{ticket.number}", self.bot)
        await self.update_pinned(tickets, settings, ticket)
        if ticket.thread_id:
            await self.bot.send_message(
                self.config.support_group_id,
                f"Обращение #{ticket.number} переоткрыто.",
                message_thread_id=ticket.thread_id,
            )
        return True

    async def rename(
        self, tickets: TicketService, settings: SettingsService, ticket: Ticket, title: str
    ) -> None:
        title = title.strip()
        if not title or len(title) > 128:
            raise ValueError("Название должно содержать от 1 до 128 символов")
        ticket.custom_title = title
        await tickets.session.commit()
        if ticket.thread_id:
            status = await settings.status(ticket.status)
            await self._rename_topic(ticket, topic_title(ticket, status_title=status["title"]))

    async def delete(self, tickets: TicketService, ticket: Ticket) -> None:
        if ticket.thread_id:
            try:
                await self.bot.delete_forum_topic(self.config.support_group_id, ticket.thread_id)
            except TelegramBadRequest as exc:
                if "not found" not in exc.message.lower():
                    raise
        await tickets.delete(ticket)

    async def relay_user(self, message: Message, tickets: TicketService, settings: SettingsService, ticket: Ticket) -> None:
        pending = ticket.thread_id is None
        thread_id = ticket.thread_id or await settings.topic("requests")
        if thread_id is None:
            raise RuntimeError("Топик для обращения ещё не создан")
        reply_message_id = await self._reply_target(message, tickets, ticket, from_user=True)
        if pending and message.text is not None:
            remaining = message.text
            while remaining:
                prefix = f"<b>Дополнение к обращению №{ticket.number}:</b>\n"
                take = min(len(remaining), 4096 - len(prefix))
                while take > 1 and len(prefix + esc(remaining[:take])) > 4096:
                    take -= 1
                copied = await self.bot.send_message(
                    self.config.support_group_id,
                    prefix + esc(remaining[:take]),
                    message_thread_id=thread_id,
                    reply_parameters={"message_id": reply_message_id} if reply_message_id else None,
                    allow_sending_without_reply=True,
                )
                remaining = remaining[take:]
        else:
            caption = None
            if pending and message.content_type in {"photo", "video", "document", "animation", "audio", "voice"}:
                prefix = f"<b>Дополнение к обращению №{ticket.number}:</b>\n"
                body = message.caption or ""
                keep = len(body)
                caption = prefix + esc(body)
                if len(caption) > 1024:
                    notice = "\n\n<i>Текст обрезан.</i>"
                    keep = max(0, 1024 - len(prefix) - len(notice) - 1)
                    caption = prefix + esc(body[:keep] + "…") + notice
                    while len(caption) > 1024 and keep:
                        keep -= 1
                        caption = prefix + esc(body[:keep] + "…") + notice
            elif pending:
                await self.bot.send_message(
                    self.config.support_group_id,
                    f"<b>Дополнение к обращению №{ticket.number}</b>",
                    message_thread_id=thread_id,
                )
            copied = await self.bot.copy_message(
                self.config.support_group_id,
                message.chat.id,
                message.message_id,
                message_thread_id=thread_id,
                caption=caption,
                reply_parameters={"message_id": reply_message_id} if reply_message_id else None,
                allow_sending_without_reply=True,
            )
        media = extract_message_media(message)
        await tickets.save_message_link(
            ticket,
            message.chat.id,
            message.message_id,
            self.config.support_group_id,
            copied.message_id,
            body=message_body(message, media),
            content_type=str(message.content_type).split(".")[-1].lower(),
            **media,
        )
        await tickets.mark_user_message(ticket)
        await self.update_pinned(tickets, settings, ticket)

    async def relay_staff(self, message: Message, tickets: TicketService, settings: SettingsService, ticket: Ticket) -> None:
        if not ticket.user_id:
            return
        reply_message_id = await self._reply_target(message, tickets, ticket, from_user=False)
        copied = await self.bot.copy_message(
            ticket.user_id,
            message.chat.id,
            message.message_id,
            reply_parameters={"message_id": reply_message_id} if reply_message_id else None,
            allow_sending_without_reply=True,
        )
        media = extract_message_media(message)
        await tickets.save_message_link(
            ticket,
            message.chat.id,
            message.message_id,
            ticket.user_id,
            copied.message_id,
            body=message_body(message, media),
            content_type=str(message.content_type).split(".")[-1].lower(),
            from_staff=True,
            **media,
        )
        await tickets.mark_support_message(ticket)
        await self.update_pinned(tickets, settings, ticket)

    @staticmethod
    def _edited_media(message: Message):
        values = {
            "caption": message.caption,
            "caption_entities": message.caption_entities,
            "parse_mode": None,
        }
        if message.photo:
            return InputMediaPhoto(media=message.photo[-1].file_id, **values)
        if message.video:
            return InputMediaVideo(media=message.video.file_id, **values)
        if message.document:
            return InputMediaDocument(media=message.document.file_id, **values)
        if message.audio:
            return InputMediaAudio(media=message.audio.file_id, **values)
        if message.animation:
            return InputMediaAnimation(media=message.animation.file_id, **values)
        return None

    async def relay_edit(self, message: Message, link: MessageLink) -> bool:
        try:
            media = self._edited_media(message)
            if media is not None:
                await self.bot.edit_message_media(
                    media=media,
                    chat_id=link.target_chat_id,
                    message_id=link.target_message_id,
                )
                return True
            if message.text is not None:
                await self.bot.edit_message_text(
                    text=message.text,
                    chat_id=link.target_chat_id,
                    message_id=link.target_message_id,
                    entities=message.entities,
                    parse_mode=None,
                )
                return True
            if message.caption is not None:
                await self.bot.edit_message_caption(
                    caption=message.caption,
                    chat_id=link.target_chat_id,
                    message_id=link.target_message_id,
                    caption_entities=message.caption_entities,
                    parse_mode=None,
                )
                return True
        except TelegramBadRequest as exc:
            if "message is not modified" in exc.message.lower():
                return True
            raise
        return False

    async def delete_relayed(self, link: MessageLink, *, delete_source: bool) -> None:
        try:
            await self.bot.delete_message(chat_id=link.target_chat_id, message_id=link.target_message_id)
        except TelegramBadRequest as exc:
            if "not found" not in exc.message.casefold():
                raise
        if delete_source:
            try:
                await self.bot.delete_message(chat_id=link.source_chat_id, message_id=link.source_message_id)
            except TelegramBadRequest as exc:
                logger.warning("Не удалось удалить исходное сообщение %s: %s", link.source_message_id, exc.message)

    async def _reply_target(
        self, message: Message, tickets: TicketService, ticket: Ticket, *, from_user: bool
    ) -> int | None:
        reply = message.reply_to_message
        if not reply:
            return None
        link = await tickets.linked_message(ticket, message.chat.id, reply.message_id)
        if not link:
            return None
        if message.chat.id == link.source_chat_id:
            return int(link.target_message_id)
        return int(link.source_message_id)

    def request_preview(self, ticket: Ticket, limit: int) -> str:
        card = self.request_card(ticket)
        notice = "\n\n<i>Текст обрезан. Полная версия будет доступна в топике после принятия.</i>"
        if len(card) <= limit:
            return card
        keep = max(0, limit - len(self.request_card(ticket, "")) - len(notice) - 1)
        preview = self.request_card(ticket, ticket.description[:keep] + "…") + notice
        while len(preview) > limit and keep:
            keep -= 1
            preview = self.request_card(ticket, ticket.description[:keep] + "…") + notice
        return preview

    def request_card_parts(self, ticket: Ticket) -> list[str]:
        if len(self.request_card(ticket)) <= 4096:
            return [self.request_card(ticket)]
        remaining = ticket.description
        parts = []
        while remaining:
            prefix = self.request_card(ticket, "") if not parts else "<b>Продолжение описания</b>\n"
            take = min(len(remaining), max(1, 4096 - len(prefix)))
            while take > 1 and len(prefix + esc(remaining[:take])) > 4096:
                take -= 1
            parts.append(prefix + esc(remaining[:take]))
            remaining = remaining[take:]
        return parts

    def request_card(self, ticket: Ticket, description: str | None = None) -> str:
        username = f"@{ticket.username}" if ticket.username else "не указан"
        user_link = f"<a href=\"tg://user?id={ticket.user_id}\">{esc(ticket.user_display_name)}</a>" if ticket.user_id else "Удалено"
        body = ticket.description if description is None else description
        return (
            f"<b>Обращение №{ticket.number}</b>\n\n"
            f"<b>Клиент:</b> {user_link}\n"
            f"<b>Username:</b> {esc(username)}\n"
            f"<b>Telegram ID:</b> <code>{ticket.user_id or ''}</code>\n"
            f"<b>Категория:</b> {esc(ticket.category)}\n\n"
            f"<b>Описание проблемы</b>\n{esc(body)}"
        )

    def format_datetime(self, value: datetime | None) -> str:
        if not value:
            return "Нет данных"
        return value.replace(tzinfo=UTC).astimezone(self.config.timezone).strftime("%d.%m.%Y %H:%M")
