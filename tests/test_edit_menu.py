from __future__ import annotations

from conftest import FakeBot, FakeCallback, FakeMessage, FakeState

from support_bot.handlers.edit import (
    edit_action,
    edit_status,
    open_edit_menu,
    save_edit_value,
)
from support_bot.services.tickets import TicketService
from support_bot.services.topics import TopicService


def edit_callback(user_id: int, data: str, bot: FakeBot, message_id: int) -> FakeCallback:
    callback = FakeCallback(user_id, data, bot)
    callback.message = FakeMessage(
        user_id,
        chat_id=-100500,
        thread_id=777,
        message_id=message_id,
    )
    callback.message.bot = bot
    return callback


async def test_edit_menu_status_note_and_cleanup(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    command = FakeMessage(
        employee.telegram_id,
        "/edit",
        chat_id=config.support_group_id,
        thread_id=777,
        message_id=500,
    )
    command.bot = bot
    await open_edit_menu(command, FakeState(), config, db)
    assert command.deleted is True
    menu_id = bot.next_message_id

    state = FakeState()
    callback = edit_callback(employee.telegram_id, "edit:status", bot, menu_id)
    await edit_action(callback, state, config, db, topics)
    assert "Выберите новый статус" in callback.message.text

    status_callback = edit_callback(employee.telegram_id, "edit_set:resolved", bot, menu_id)
    await edit_status(status_callback, state, config, db, topics)
    async with db.sessions() as session:
        ticket = await TicketService(session, config.admin_id).get(ticket_number)
        assert ticket.status == "resolved"

    open_callback = edit_callback(employee.telegram_id, "edit:open", bot, menu_id)
    await edit_action(open_callback, state, config, db, topics)
    async with db.sessions() as session:
        ticket = await TicketService(session, config.admin_id).get(ticket_number)
        assert ticket.status == "work"

    note_callback = edit_callback(employee.telegram_id, "edit:note", bot, menu_id)
    await edit_action(note_callback, state, config, db, topics)
    assert state.state == "EditForm:value"
    note_message = FakeMessage(
        employee.telegram_id,
        "Новое описание из меню",
        chat_id=config.support_group_id,
        thread_id=777,
        message_id=501,
    )
    note_message.bot = bot
    await save_edit_value(note_message, state, config, db, topics)
    assert note_message.deleted is True and state.state is None
    async with db.sessions() as session:
        ticket = await TicketService(session, config.admin_id).get(ticket_number)
        assert ticket.note == "Новое описание из меню"
    assert bot.called("delete_message")
