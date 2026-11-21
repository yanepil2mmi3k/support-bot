from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from conftest import FakeBot, FakeCallback, FakeMessage, FakeState
from sqlalchemy import func, select

from support_bot.db.models import MessageLink, Review
from support_bot.db.session import Database
from support_bot.handlers.edit import edit_action, menu_text
from support_bot.handlers.staff import confirm_accept
from support_bot.handlers.user import rate_ticket, review_comment
from support_bot.services.employees import EmployeeService
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import TicketService
from support_bot.services.topics import TopicService, esc


async def test_long_card_and_partial_topic_content_recovery(config, db, employee) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    description = "а<" * 2000
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        await settings.set_topic("requests", 10)
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.create(
            user_id=2001, username="client", display_name="Клиент", category="Другое", description=description
        )
        await topics.publish_request(tickets, settings, ticket, None)
        preview = bot.called("send_message")[0][0][1]
        assert len(preview) <= 4096 and "Текст обрезан" in preview
        await tickets.assign(ticket, employee)
        bot.fail_next("send_message")
        with pytest.raises(TelegramAPIError):
            await topics.create_ticket_topic(tickets, settings, ticket, employee)
        assert ticket.thread_id is not None
        assert json.loads(ticket.initial_message_ids or "[]") == []
        await topics.create_ticket_topic(tickets, settings, ticket, employee)
        initial_ids = json.loads(ticket.initial_message_ids)
        assert len(initial_ids) >= 2
        assert len(bot.called("create_forum_topic")) == 1
        cards = [
            args[1]
            for args, kwargs in bot.called("send_message")
            if kwargs.get("message_thread_id") == ticket.thread_id
            and ("Описание проблемы" in args[1] or "Продолжение описания" in args[1])
        ]
        assert all(len(card) <= 4096 for card in cards)
        assert esc(description[:100]) in cards[0]
        assert esc(description[-100:]) in cards[-1]

        second = await tickets.create(
            user_id=2002,
            username=None,
            display_name="Клиент 2",
            category="Другое",
            description="С вложением",
            media_file_id="photo-id",
            media_type="photo",
        )
        await tickets.assign(second, employee)
        bot.fail_next("copy_message")
        with pytest.raises(TelegramAPIError):
            await topics.create_ticket_topic(tickets, settings, second, employee, 88)
        assert second.topic_media_message_id is None
        initial_ids = second.initial_message_ids
        await topics.create_ticket_topic(tickets, settings, second, employee, 88)
        assert second.topic_media_message_id is not None
        assert second.initial_message_ids == initial_ids
        assert len(bot.called("create_forum_topic")) == 2


async def test_reopen_guard_and_transfer_closed_ticket(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        other = await EmployeeService(session, config.admin_id).create(1002, "Другой", "m", "Другой")
        first = await tickets.get(ticket_number)
        await topics.close(tickets, settings, first)
        second = await tickets.create(
            user_id=2002, username=None, display_name="Клиент", category="Другое", description="Новый тикет"
        )
        await tickets.assign(second, employee)
        second.thread_id = 888
        await session.commit()
        with pytest.raises(ValueError, match="активное обращение"):
            await topics.reopen(tickets, settings, first)
        assert first.status == "archive" and not bot.called("reopen_forum_topic")

        await topics.close(tickets, settings, second)
        archived_at = second.archived_at
        resolved_at = second.resolved_at
        await topics.transfer(tickets, settings, second, other)
        assert second.status == "archive"
        assert second.archived_at == archived_at and second.resolved_at == resolved_at
        assert not bot.called("reopen_forum_topic")
        await topics.transfer(tickets, settings, second, employee, "work")
        assert second.status == "work" and second.archived_at is None and second.resolved_at is None
        assert len(bot.called("reopen_forum_topic")) == 1


async def test_close_retry_after_pin_failure_restores_rating(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.get(ticket_number)
        bot.fail_next("edit_forum_topic")
        with pytest.raises(TelegramAPIError):
            await topics.close(tickets, settings, ticket)
        assert ticket.status == "archive"
        assert not [call for call in bot.called("send_message") if call[0][0] == 2002]
        await topics.close(tickets, settings, ticket)
        assert len([call for call in bot.called("send_message") if call[0][0] == 2002]) == 2
        await topics.close(tickets, settings, ticket)
        assert len([call for call in bot.called("send_message") if call[0][0] == 2002]) == 2


async def test_delete_missing_target_and_edit_dismiss(config, db, employee, ticket_number) -> None:
    class MissingTargetBot(FakeBot):
        async def delete_message(self, *args, **kwargs):
            self.calls.append(("delete_message", args, kwargs))
            raise TelegramBadRequest(SimpleNamespace(), "Bad Request: message to delete not found")

    bot = MissingTargetBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(ticket_number)
        message = FakeMessage(
            employee.telegram_id,
            "Ответ",
            chat_id=config.support_group_id,
            thread_id=777,
            message_id=400,
        )
        await topics.relay_staff(message, tickets, SettingsService(session, config.defaults), ticket)
        link = await tickets.linked_message(ticket, config.support_group_id, 400)
        await topics.delete_relayed(link, delete_source=True)
        await session.delete(link)
        await session.commit()
        assert await session.scalar(select(func.count(MessageLink.id))) == 0

    state = FakeState(
        {"ticket_number": ticket_number, "action": "note", "menu_key": "edit:test"},
        "EditForm:value",
    )
    callback = FakeCallback(employee.telegram_id, "edit:dismiss", bot)
    callback.message = FakeMessage(
        employee.telegram_id, chat_id=config.support_group_id, thread_id=777
    )
    callback.message.bot = bot
    await edit_action(callback, state, config, db, topics)
    assert state.state is None and state.data == {}


async def test_long_note_preview_and_additive_migration(config, db, employee, ticket_number, tmp_path: Path) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.get(ticket_number)
        ticket.note = "<" * 3000
        await session.commit()
        await topics.update_pinned(tickets, settings, ticket)
        text = await menu_text(settings, ticket)
        assert len(text) < 4096
        assert ticket.note == "<" * 3000

    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE tickets (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}", path)
    await database.init()
    async with database.engine.begin() as connection:
        rows = await connection.exec_driver_sql("PRAGMA table_info(tickets)")
        columns = {row[1] for row in rows}
    assert {"initial_message_ids", "topic_media_message_id"}.issubset(columns)
    await database.close()


async def test_manual_accept_recovers_partial_topic(config, db, employee) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        await settings.set_topic("requests", 10)
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.create(
            user_id=2099, username=None, display_name="Клиент", category="Другое", description="Принять"
        )
        await topics.publish_request(tickets, settings, ticket, None)
        callback = FakeCallback(employee.telegram_id, f"accept_confirm:{ticket.number}", bot)
        bot.fail_next("send_message")
        with pytest.raises(TelegramAPIError):
            await confirm_accept(callback, config, db, topics)
        await session.refresh(ticket)
        assert ticket.thread_id is not None and ticket.employee_id == employee.id
        await confirm_accept(callback, config, db, topics)
        await session.refresh(ticket)
        assert json.loads(ticket.initial_message_ids)
        assert ticket.pinned_message_id is not None
        assert len(bot.called("create_forum_topic")) == 1
        assert any(call[0][0] == 2099 for call in bot.called("send_message"))


async def test_operator_reopen_routes_next_text_to_operator(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.get(ticket_number)
        await topics.close(tickets, settings, ticket, await settings.status("resolved"))
    state = FakeState()
    await rate_ticket(FakeCallback(2002, f"rate:{ticket_number}:5", bot), state, config, db)
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.get(ticket_number)
        await topics.reopen(tickets, settings, ticket)
    message = FakeMessage(2002, "Это оператору", message_id=900)
    await review_comment(message, state, config, db, topics)
    assert state.state is None
    assert bot.called("copy_message")
    async with db.sessions() as session:
        review = await session.scalar(select(Review).where(Review.ticket_number == ticket_number))
        assert review.rating == 5 and review.comment is None
