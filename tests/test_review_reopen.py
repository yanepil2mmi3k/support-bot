from __future__ import annotations

from conftest import FakeBot, FakeCallback, FakeMessage, FakeState
from sqlalchemy import select

from support_bot.db.models import Review
from support_bot.handlers.user import rate_ticket, relay_user_message, reopen_ticket
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import TicketService
from support_bot.services.topics import TopicService


async def test_reopen_clears_pending_comment_and_restores_relay(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.get(ticket_number)
        await topics.close(tickets, settings, ticket, await settings.status("resolved"))

    state = FakeState()
    await rate_ticket(FakeCallback(2002, f"rate:{ticket_number}:5", bot), state, config, db)
    assert state.state == "ReviewForm:comment"
    assert state.data["rating"] == 5

    await reopen_ticket(FakeCallback(2002, f"reopen:{ticket_number}", bot), state, config, db, topics)
    assert state.state is None and state.data == {}

    message = FakeMessage(2002, "Это сообщение оператору, а не комментарий", message_id=300)
    await relay_user_message(message, config, db, topics)
    assert len(bot.called("copy_message")) == 1
    args, kwargs = bot.called("copy_message")[0]
    assert args[1] == 2002 and kwargs["message_thread_id"] == 777

    async with db.sessions() as session:
        review = await session.scalar(select(Review).where(Review.ticket_number == ticket_number))
        assert review.rating == 5 and review.comment is None
        ticket = await TicketService(session, config.admin_id).get(ticket_number)
        assert ticket.status == "work"

    rejected_state = FakeState()
    await rate_ticket(FakeCallback(2002, f"rate:{ticket_number}:1", bot), rejected_state, config, db)
    assert rejected_state.state is None
    async with db.sessions() as session:
        review = await session.scalar(select(Review).where(Review.ticket_number == ticket_number))
        assert review.rating == 5 and review.comment is None


async def test_reopen_without_rating_keeps_ticket_and_relay(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(ticket_number)
        await topics.close(tickets, SettingsService(session, config.defaults), ticket)

    state = FakeState()
    await reopen_ticket(FakeCallback(2002, f"reopen:{ticket_number}", bot), state, config, db, topics)
    assert state.clear_count == 1
    async with db.sessions() as session:
        ticket = await TicketService(session, config.admin_id).get(ticket_number)
        assert ticket.status == "work" and ticket.number == ticket_number
        assert await session.scalar(select(Review).where(Review.ticket_number == ticket_number)) is None

    await relay_user_message(FakeMessage(2002, "Продолжаем"), config, db, topics)
    assert len(bot.called("copy_message")) == 1


async def test_closed_ticket_message_shows_start_menu_without_reopen(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(ticket_number)
        await topics.close(tickets, SettingsService(session, config.defaults), ticket)

    private_messages = bot.called("send_message")
    assert len(private_messages) == 2
    assert private_messages[0][0][0] == 2002
    assert private_messages[1][0][1] == config.message("start")
    rating_buttons = private_messages[0][1]["reply_markup"].inline_keyboard
    assert any(button.callback_data == f"reopen:{ticket_number}" for row in rating_buttons for button in row)

    message = FakeMessage(2002, "Новый вопрос после закрытия")
    await relay_user_message(message, config, db, topics)
    assert message.answers[-1]["text"] == config.message("start")
    menu_buttons = message.answers[-1]["reply_markup"].inline_keyboard
    assert all(not (button.callback_data or "").startswith("reopen:") for row in menu_buttons for button in row)
    assert not bot.called("copy_message")
