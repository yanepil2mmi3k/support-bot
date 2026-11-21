from __future__ import annotations

from types import SimpleNamespace

from conftest import FakeBot, FakeMessage
from sqlalchemy import func, select

from support_bot.db.models import MessageLink
from support_bot.handlers.staff import (
    delete_staff_message,
    edit_staff_message,
    relay_staff_message,
)
from support_bot.handlers.user import (
    delete_user_message,
    edit_user_message,
    relay_user_message,
)
from support_bot.services.topics import TopicService


async def test_bidirectional_edit_and_delete(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)

    user_message = FakeMessage(2002, "Исходный текст", message_id=300)
    await relay_user_message(user_message, config, db, topics)
    user_message.text = "Исправленный текст"
    await edit_user_message(user_message, config, db, topics)
    edit = bot.called("edit_message_text")[-1][1]
    assert edit["text"] == "Исправленный текст"
    assert edit["chat_id"] == config.support_group_id

    delete = FakeMessage(2002, "/delete", message_id=301)
    delete.reply_to_message = SimpleNamespace(message_id=300)
    await delete_user_message(delete, config, db, topics)
    deleted = bot.called("delete_message")
    assert len(deleted) == 1
    assert deleted[0][1]["chat_id"] == config.support_group_id

    staff_message = FakeMessage(
        employee.telegram_id,
        "Ответ оператора",
        chat_id=config.support_group_id,
        thread_id=777,
        message_id=400,
    )
    await relay_staff_message(staff_message, config, db, topics)
    staff_message.text = "Исправленный ответ"
    await edit_staff_message(staff_message, config, db, topics)
    edit = bot.called("edit_message_text")[-1][1]
    assert edit["text"] == "Исправленный ответ" and edit["chat_id"] == 2002

    staff_delete = FakeMessage(
        employee.telegram_id,
        "/delete",
        chat_id=config.support_group_id,
        thread_id=777,
        message_id=401,
    )
    staff_delete.reply_to_message = SimpleNamespace(message_id=400)
    await delete_staff_message(staff_delete, config, db, topics)
    deleted_chats = [call[1]["chat_id"] for call in bot.called("delete_message")]
    assert deleted_chats[-2:] == [2002, config.support_group_id]
    async with db.sessions() as session:
        assert await session.scalar(select(func.count(MessageLink.id))) == 0
