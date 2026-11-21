from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.storage.base import StorageKey
from conftest import FakeBot, FakeCallback
from sqlalchemy import select

from support_bot.db.models import Employee, Reminder, Ticket, utcnow
from support_bot.db.storage import SqliteStorage
from support_bot.handlers.admin import report
from support_bot.jobs import BackgroundJobs
from support_bot.services.interface import InterfaceService
from support_bot.services.reports import TELEGRAM_MESSAGE_LIMIT, split_html
from support_bot.services.settings import SettingsService
from support_bot.services.topics import TopicService


class TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def visible_text(text: str) -> str:
    parser = TextCollector()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


async def test_split_html_preserves_text_and_balanced_tags() -> None:
    text = '<b>Заголовок &amp; <a href="tg://user?id=1">' + "длинный текст &lt; &amp; " * 500 + "</a></b>"

    parts = split_html(text)

    assert len(parts) > 1
    assert all(len(part) <= TELEGRAM_MESSAGE_LIMIT for part in parts)
    assert all(part.startswith("<b>") for part in parts)
    assert all(part.endswith("</b>") for part in parts)
    assert "".join(visible_text(part) for part in parts) == visible_text(text)


async def test_manual_report_splits_long_message(config, db) -> None:
    bot = FakeBot()
    async with db.sessions() as session:
        session.add_all(
            Employee(
                telegram_id=1000 + index,
                name=f"Сотрудник {index}",
                gender="m",
                nickname=f"employee-{index}",
            )
            for index in range(60)
        )
        await session.commit()
        await SettingsService(session, config.defaults).set_topic("admin", 321)

    await report(FakeCallback(config.admin_id, "admin:report", bot), config, db)

    calls = bot.called("send_message")
    assert len(calls) > 1
    assert all(len(args[1]) <= TELEGRAM_MESSAGE_LIMIT for args, _kwargs in calls)


async def test_scheduled_report_runs_late_once_and_splits(config, db, monkeypatch) -> None:
    class LateDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 15, 12, 30, tzinfo=tz)

    async def long_report(*_args):
        return "<b>Отчёт</b>\n" + "строка отчёта\n" * 600

    monkeypatch.setattr("support_bot.jobs.datetime", LateDateTime)
    monkeypatch.setattr("support_bot.jobs.build_report", long_report)
    bot = FakeBot()
    jobs = BackgroundJobs(config, db, TopicService(bot, config))
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        await settings.set("report_time", "09:00")
        await settings.set_topic("admin", 321)

    await jobs.send_scheduled_report()
    calls = bot.called("send_message")
    assert len(calls) > 1
    assert all(len(args[1]) <= TELEGRAM_MESSAGE_LIMIT for args, _kwargs in calls)

    await jobs.send_scheduled_report()
    assert bot.called("send_message") == calls
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        assert await settings.get("last_report_date") == "2026-01-15"


async def test_stale_summary_splits_long_message(config, db) -> None:
    bot = FakeBot()
    jobs = BackgroundJobs(config, db, TopicService(bot, config))
    old = utcnow() - timedelta(hours=24)
    async with db.sessions() as session:
        session.add_all(
            Ticket(
                number=1000 + index,
                user_id=2000 + index,
                user_display_name=f"Клиент {index}",
                category="Другое",
                description=f"Описание {index} & <данные> " + "x" * 240,
                status="work",
                last_user_message_at=old,
            )
            for index in range(35)
        )
        await session.commit()
        await SettingsService(session, config.defaults).set_topic("general", 123)

    await jobs.send_stale_summary(force=True)

    calls = bot.called("send_message")
    assert len(calls) > 1
    assert all(len(args[1]) <= TELEGRAM_MESSAGE_LIMIT for args, _kwargs in calls)
    combined = "".join(visible_text(args[1]) for args, _kwargs in calls)
    assert "#1000" in combined
    assert "#1034" in combined


async def test_reminder_failure_does_not_rollback_or_stop_others(config, db, employee) -> None:
    class FailSecondBot(FakeBot):
        async def send_message(self, *args, **kwargs):
            if len(self.called("send_message")) == 1:
                self.calls.append(("send_message", args, kwargs))
                raise TelegramAPIError(SimpleNamespace(), "Временный сбой")
            return await self._call("send_message", *args, **kwargs)

    bot = FailSecondBot()
    jobs = BackgroundJobs(config, db, TopicService(bot, config))
    due = utcnow() - timedelta(minutes=5)
    async with db.sessions() as session:
        for index in range(3):
            ticket = Ticket(
                number=2000 + index,
                user_id=3000 + index,
                user_display_name=f"Клиент {index}",
                category="Другое",
                status="work",
                employee_id=employee.id,
                thread_id=700 + index,
                last_user_message_at=utcnow(),
            )
            session.add(ticket)
            await session.flush()
            session.add(Reminder(ticket_id=ticket.id, employee_id=employee.id, next_run_at=due))
        await session.commit()

    await jobs.process_reminders()

    assert len(bot.called("send_message")) == 3
    async with db.sessions() as session:
        reminders = list((await session.execute(select(Reminder).order_by(Reminder.ticket_id))).scalars())
    assert reminders[0].next_run_at > due
    assert reminders[1].next_run_at == due
    assert reminders[2].next_run_at > due


async def test_interface_retains_transient_failures_and_drops_not_found(config, db) -> None:
    class NotFoundBot(FakeBot):
        async def delete_message(self, *args, **kwargs):
            raise TelegramBadRequest(SimpleNamespace(), "Bad Request: message to delete not found")

    bot = FakeBot()
    async with db.sessions() as session:
        interface = InterfaceService(session)
        for message_id in [10, 11, 12]:
            await interface.track("edge:test", config.support_group_id, message_id)

        bot.fail_next("delete_message")
        await interface.trim("edge:test", bot, keep=1)
        assert await interface.ids("edge:test") == [10, 12]

        bot.fail_next("delete_message")
        await interface.clear("edge:test", bot)
        assert await interface.ids("edge:test") == [10]

        await interface.clear("edge:test", NotFoundBot())
        assert await interface.ids("edge:test") == []


async def test_update_data_merges_concurrent_changes_atomically(db) -> None:
    storage = SqliteStorage(db)
    key = StorageKey(bot_id=1, chat_id=2002, user_id=2002)
    await storage.set_data(key, {"initial": True})

    await asyncio.gather(*(storage.update_data(key, {f"key_{index}": index}) for index in range(20)))

    result = await storage.get_data(key)
    assert result == {"initial": True, **{f"key_{index}": index for index in range(20)}}
