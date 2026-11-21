from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from support_bot.config import Config
from support_bot.db.models import MessageLink, Reminder, Ticket, utcnow
from support_bot.db.session import Database
from support_bot.services.reports import build_report, split_html
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import TicketService
from support_bot.services.topics import TopicService, esc, topic_title

logger = logging.getLogger(__name__)


def _miniapp_ticket_url(base_url: str, number: int) -> str:
    parts = urlsplit(base_url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "ticket"]
    query.append(("ticket", str(number)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class BackgroundJobs:
    def __init__(self, config: Config, db: Database, topics: TopicService) -> None:
        self.config = config
        self.db = db
        self.topics = topics
        self.scheduler = AsyncIOScheduler(timezone=config.timezone)

    def start(self) -> None:
        self.scheduler.add_job(
            self.run_maintenance,
            IntervalTrigger(minutes=1, timezone=self.config.timezone),
            id="support-maintenance",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
            replace_existing=True,
        )
        self.scheduler.start()

    async def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def run_maintenance(self) -> None:
        for job in (self.process_reminders, self.send_stale_summary, self.cleanup_archive, self.send_scheduled_report):
            try:
                await job()
            except Exception:
                logger.exception("Ошибка фоновой задачи %s", job.__name__)

    async def process_reminders(self) -> None:
        now = utcnow()
        async with self.db.sessions() as session:
            settings = SettingsService(session, self.config.defaults)
            interval = int(await settings.get("reminder_interval_minutes", 60))
            closed_statuses = await settings.closed_keys()
            rows = (
                await session.execute(
                    select(Reminder, Ticket)
                    .join(Ticket, Ticket.id == Reminder.ticket_id)
                    .options(selectinload(Ticket.employee))
                    .where(
                        Reminder.active.is_(True),
                        Reminder.next_run_at <= now,
                        Ticket.deleted_at.is_(None),
                        Ticket.status.not_in(closed_statuses),
                    )
                )
            ).all()
            for reminder, ticket in rows:
                status = await settings.status(ticket.status)
                should_skip = status.get("pauses_reminders") or (
                    ticket.last_support_message_at is not None
                    and ticket.last_support_message_at >= ticket.last_user_message_at
                )
                try:
                    if not should_skip and ticket.employee:
                        last_link = await session.scalar(
                            select(MessageLink)
                            .where(MessageLink.ticket_id == ticket.id)
                            .order_by(MessageLink.created_at.desc(), MessageLink.id.desc())
                            .limit(1)
                        )
                        body = last_link.body if last_link and last_link.body else "Нет сохранённых сообщений"
                        preview = esc(body[:300]) + ("…" if len(body) > 300 else "")
                        waiting_since = (
                            ticket.last_user_message_at.replace(tzinfo=UTC)
                            .astimezone(self.config.timezone)
                            .strftime("%H:%M")
                            if ticket.last_user_message_at
                            else "Нет данных"
                        )
                        text = (
                            f"<b>Напоминание по обращению №{ticket.number}</b>\n"
                            f"Клиент ждёт с {waiting_since}\n"
                            f"Категория: {esc(ticket.category)}\n"
                            f"Последнее сохранённое сообщение: {preview}"
                        )
                        keyboard = []
                        if self.config.miniapp_url:
                            keyboard.append([InlineKeyboardButton(
                                text="Открыть обращение",
                                web_app=WebAppInfo(url=_miniapp_ticket_url(self.config.miniapp_url, ticket.number)),
                            )])
                        else:
                            group_id = str(self.config.support_group_id)
                            if group_id.startswith("-100") and group_id[4:].isdigit() and ticket.thread_id:
                                keyboard.append([InlineKeyboardButton(
                                    text="Открыть обращение",
                                    url=f"https://t.me/c/{group_id[4:]}/{ticket.thread_id}",
                                )])
                        keyboard.extend([
                            [InlineKeyboardButton(
                                text="Передать сотруднику", callback_data=f"reminder_transfer:{ticket.number}"
                            )],
                            [InlineKeyboardButton(
                                text="Отключить напоминание", callback_data=f"reminder_disable:{ticket.number}"
                            )],
                        ])
                        await self.topics.bot.send_message(
                            ticket.employee.telegram_id,
                            text,
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
                        )
                except TelegramAPIError:
                    logger.exception("Не удалось отправить напоминание по тикету %s", ticket.number)
                    continue
                reminder.next_run_at = now + timedelta(minutes=interval)
                await session.commit()

    async def send_stale_summary(self, *, force: bool = False) -> None:
        now = utcnow()
        async with self.db.sessions() as session:
            settings = SettingsService(session, self.config.defaults)
            if not force:
                last_raw = await settings.get("last_stale_summary")
                if last_raw:
                    last = datetime.fromisoformat(last_raw)
                else:
                    last = now - timedelta(days=3650)
                period = int(await settings.get("stale_summary_hours", 8))
                if now - last < timedelta(hours=period):
                    return
            threshold = int(await settings.get("stale_threshold_hours", 6))
            closed_statuses = await settings.closed_keys()
            tickets = list(
                (
                    await session.execute(
                        select(Ticket)
                        .options(selectinload(Ticket.employee))
                        .where(
                            Ticket.deleted_at.is_(None),
                            Ticket.status.not_in(closed_statuses),
                            Ticket.last_user_message_at <= now - timedelta(hours=threshold),
                        )
                    )
                ).scalars()
            )
            stale = []
            for ticket in tickets:
                status = await settings.status(ticket.status)
                if status.get("pauses_reminders"):
                    continue
                if ticket.last_support_message_at and ticket.last_support_message_at >= ticket.last_user_message_at:
                    continue
                stale.append(ticket)
            general_thread = await settings.topic("general")
            if stale and general_thread is not None:
                lines = [f"<b>Зависшие тикеты, более {threshold} ч без ответа</b>"]
                for ticket in stale:
                    description = ticket.note or ticket.description or "Без описания"
                    status = await settings.status(ticket.status)
                    lines.append(
                        f"#{ticket.number} {esc(topic_title(ticket, status_title=status['title']))}\n"
                        f"{esc(description[:250])}\n"
                        f"Последнее сообщение клиента: {self.topics.format_datetime(ticket.last_user_message_at)}"
                    )
                for part in split_html("\n\n".join(lines)):
                    await self.topics.bot.send_message(
                        self.config.support_group_id, part, message_thread_id=general_thread
                    )
            await settings.set("last_stale_summary", now.isoformat())

    async def cleanup_archive(self) -> None:
        async with self.db.sessions() as session:
            settings = SettingsService(session, self.config.defaults)
            if not await settings.get("archive_auto_delete", False):
                return
            days = int(await settings.get("archive_days", 30))
            cutoff = utcnow() - timedelta(days=days)
            archive_statuses = await settings.archive_keys()
            tickets = list(
                (
                    await session.execute(
                        select(Ticket).where(
                            Ticket.status.in_(archive_statuses),
                            Ticket.deleted_at.is_(None),
                            Ticket.archived_at.is_not(None),
                            Ticket.archived_at <= cutoff,
                        )
                    )
                ).scalars()
            )
            tickets_service = TicketService(session, self.config.admin_id)
            for ticket in tickets:
                try:
                    await self.topics.delete(tickets_service, ticket)
                except Exception:
                    logger.exception("Не удалось автоудалить тикет %s", ticket.number)

    async def send_scheduled_report(self) -> None:
        local_now = datetime.now(self.config.timezone)
        async with self.db.sessions() as session:
            settings = SettingsService(session, self.config.defaults)
            report_time = str(await settings.get("report_time", "09:00"))
            try:
                hour, minute = (int(part) for part in report_time.split(":"))
                scheduled_at = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            except (ValueError, TypeError):
                return
            today = local_now.date().isoformat()
            if local_now < scheduled_at or await settings.get("last_report_date") == today:
                return
            admin_thread = await settings.topic("admin")
            if admin_thread is None:
                return
            text = await build_report(session, await settings.closed_keys())
            for part in split_html(text):
                await self.topics.bot.send_message(
                    self.config.support_group_id, part, message_thread_id=admin_thread
                )
            await settings.set("last_report_date", today)
