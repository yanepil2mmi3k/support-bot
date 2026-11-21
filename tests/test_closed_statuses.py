from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import FakeBot, FakeCallback, FakeMessage, FakeState
from sqlalchemy import select

from support_bot.db.models import Reminder, Review, utcnow
from support_bot.handlers.staff import change_status, relay_staff_message
from support_bot.handlers.user import rate_ticket, reopen_ticket, review_comment
from support_bot.jobs import BackgroundJobs
from support_bot.services.reports import build_report
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import TicketService
from support_bot.services.topics import TopicService


@pytest.mark.parametrize("status_key", ["resolved", "archive", "custom_done"])
async def test_closed_status_full_lifecycle(status_key, config, db, employee, ticket_number) -> None:
    if status_key == "custom_done":
        config.defaults["statuses"].append(
            {"key": "custom_done", "title": "Закрыт вручную", "code": "ЗК", "closed": True}
        )
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(ticket_number)
        await tickets.set_reminder(ticket, employee, 1)
        ticket.last_user_message_at = utcnow() - timedelta(hours=10)
        ticket.last_support_message_at = None
        await session.commit()
        await SettingsService(session, config.defaults).set_topic("general", 1)

    bot = FakeBot()
    topics = TopicService(bot, config)
    message = FakeMessage(employee.telegram_id, chat_id=config.support_group_id, thread_id=ticket.thread_id)
    await change_status(message, SimpleNamespace(args=status_key), config, db, topics)

    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(ticket_number)
        closed_statuses = await settings.closed_keys()
        assert ticket.status == status_key
        assert ticket.resolved_at is not None
        if status_key == "archive":
            assert ticket.archived_at is not None
        else:
            assert ticket.archived_at is None
        assert await tickets.active_for_user(ticket.user_id, closed_statuses) is None
        reminder = await session.get(Reminder, ticket.id)
        assert reminder is not None and reminder.active is False
        first_resolved_at = ticket.resolved_at
        first_archived_at = ticket.archived_at
        report = await build_report(session, closed_statuses)
        assert "Открыто: 0; закрыто: 1" in report
    assert len(bot.called("close_forum_topic")) == 1
    assert len([call for call in bot.called("send_message") if call[0][0] == 2002]) == 2

    jobs = BackgroundJobs(config, db, topics)
    before_stale = len(bot.called("send_message"))
    await jobs.send_stale_summary(force=True)
    assert len(bot.called("send_message")) == before_stale

    staff_message = FakeMessage(
        employee.telegram_id,
        "Не должно уйти",
        chat_id=config.support_group_id,
        thread_id=ticket.thread_id,
        message_id=200,
    )
    await relay_staff_message(staff_message, config, db, topics)
    assert not bot.called("copy_message")

    await change_status(message, SimpleNamespace(args=status_key), config, db, topics)
    assert len(bot.called("close_forum_topic")) == 1
    assert len([call for call in bot.called("send_message") if call[0][0] == 2002]) == 2
    async with db.sessions() as session:
        ticket = await TicketService(session, config.admin_id).get(ticket_number)
        assert ticket.resolved_at == first_resolved_at
        assert ticket.archived_at == first_archived_at

    callback = FakeCallback(2002, f"rate:{ticket_number}:5", bot)
    review_state = FakeState()
    await rate_ticket(callback, review_state, config, db)
    assert review_state.state == "ReviewForm:comment"
    comment = FakeMessage(2002, "Полезный комментарий")
    await review_comment(comment, review_state, config, db, topics)
    async with db.sessions() as session:
        review = await session.scalar(select(Review).where(Review.ticket_number == ticket_number))
        assert review.rating == 5 and review.comment == "Полезный комментарий"

    reopen_callback = FakeCallback(2002, f"reopen:{ticket_number}", bot)
    await reopen_ticket(reopen_callback, review_state, config, db, topics)
    assert review_state.clear_count >= 1
    assert len(bot.called("reopen_forum_topic")) == 1
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(ticket_number)
        assert ticket.status == "work"
        assert ticket.resolved_at is None and ticket.archived_at is None
        assert (await tickets.active_for_user(2002, await settings.closed_keys())).number == ticket_number

    await relay_staff_message(staff_message, config, db, topics)
    assert len(bot.called("copy_message")) == 1
    assert bot.called("copy_message")[0][0][0] == 2002
