from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.fsm.storage.base import StorageKey
from conftest import FakeBot
from sqlalchemy import select

from support_bot.db.models import Reminder, Review, utcnow
from support_bot.db.storage import SqliteStorage
from support_bot.jobs import BackgroundJobs
from support_bot.services.employees import EmployeeService
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import PermissionDenied, TicketError, TicketService
from support_bot.services.topics import TopicService


async def test_acl_counter_archive_cleanup_and_fsm_restart(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        other = await employees.create(1002, "Другой", "m", "Другой")
        admin = await employees.ensure_admin()
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(ticket_number)
        with pytest.raises(PermissionDenied):
            tickets.check_access(ticket, other)
        tickets.check_access(ticket, admin)
        with pytest.raises(TicketError):
            await tickets.reset_counter(0)

    storage = SqliteStorage(db)
    key = StorageKey(bot_id=1, chat_id=2002, user_id=2002)
    await storage.set_state(key, "ReviewForm:comment")
    await storage.set_data(key, {"ticket_number": ticket_number, "rating": 5})
    restored = SqliteStorage(db)
    assert await restored.get_state(key) == "ReviewForm:comment"
    assert (await restored.get_data(key))["rating"] == 5

    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(ticket_number)
        await topics.close(tickets, SettingsService(session, config.defaults), ticket)
        await tickets.add_review(ticket, 5, "Комментарий")
        await topics.delete(tickets, ticket)
        assert ticket.number == ticket_number and ticket.deleted_at is not None
        assert ticket.user_id is None and ticket.description == ""
        assert ticket.thread_id is None
        review = await session.scalar(select(Review).where(Review.ticket_number == ticket_number))
        assert review.rating == 5 and review.comment is None and review.user_id is None
        assert await db.counter_value() == ticket_number


async def test_pause_and_no_ping_rules_survive(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    jobs = BackgroundJobs(config, db, topics)
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.get(ticket_number)
        await tickets.set_reminder(ticket, employee, 1)
        await tickets.set_status(ticket, "pause", await settings.status("pause"))
        reminder = await session.get(Reminder, ticket.id)
        reminder.next_run_at = utcnow() - timedelta(minutes=1)
        await session.commit()

    await jobs.process_reminders()
    assert not bot.called("send_message")

    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.get(ticket_number)
        await tickets.set_status(ticket, "work", await settings.status("work"))
        await tickets.mark_support_message(ticket)
        reminder = await session.get(Reminder, ticket.id)
        reminder.active = True
        reminder.next_run_at = utcnow() - timedelta(minutes=1)
        await session.commit()

    await jobs.process_reminders()
    assert not bot.called("send_message")

    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(ticket_number)
        await tickets.mark_user_message(ticket)
        reminder = await session.get(Reminder, ticket.id)
        reminder.next_run_at = utcnow() - timedelta(minutes=1)
        await session.commit()

    await jobs.process_reminders()
    assert len(bot.called("send_message")) == 1
