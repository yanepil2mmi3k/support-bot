from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Chat, ForumTopicEdited, Message, MessageEntity, Update, User

from support_bot.db.storage import SqliteStorage
from support_bot.handlers import admin_router, staff_router, user_router
from support_bot.handlers.admin import AdminForm
from support_bot.services.employees import EmployeeService
from support_bot.services.settings import SettingsService
from support_bot.services.topics import TopicService


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls = []
        self.message_id = 1000

    async def close(self) -> None:
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if method.__api_method__ == "getMe":
            return User(id=123456789, is_bot=True, first_name="Support")
        if method.__api_method__ == "sendMessage":
            self.message_id += 1
            return SimpleNamespace(message_id=self.message_id)
        return True

    async def stream_content(self, *args, **kwargs):
        raise NotImplementedError


def command(update_id: int, user: User, chat: Chat, text: str, thread_id: int | None = None) -> Update:
    message = Message(
        message_id=update_id,
        date=datetime.now(),
        chat=chat,
        from_user=user,
        text=text,
        entities=[MessageEntity(type="bot_command", offset=0, length=len(text))],
        message_thread_id=thread_id,
    )
    return Update(update_id=update_id, message=message)


async def test_admin_commands_help_and_primary_button_reach_handlers(config, db) -> None:
    async with db.sessions() as session:
        await EmployeeService(session, config.admin_id).ensure_admin()
        await SettingsService(session, config.defaults).set_topic("admin", 4)
    api = FakeSession()
    bot = Bot(
        "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        session=api,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    config = replace(config, bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    dispatcher = Dispatcher(storage=SqliteStorage(db))
    dispatcher.workflow_data.update(config=config, db=db, topics=TopicService(bot, config))
    dispatcher.include_routers(admin_router, staff_router, user_router)
    admin = User(id=config.admin_id, is_bot=False, first_name="Admin")
    group = Chat(id=config.support_group_id, type="supergroup")

    for update_id, text in enumerate(["/admin", "/report", "/reviews", "/archive", "/help"], start=1):
        await dispatcher.feed_update(bot, command(update_id, admin, group, text, 4))

    sent = [method for method in api.calls if method.__api_method__ == "sendMessage"]
    texts = [method.text for method in sent]
    assert any("Панель администратора" in text for text in texts)
    assert any("Отчёт службы поддержки" in text for text in texts)
    assert any("Отзывов пока нет" in text for text in texts)
    assert any("Архивные тикеты" in text for text in texts)
    assert any("Справка администратора" in text for text in texts)

    staff = User(id=1001, is_bot=False, first_name="Staff")
    await dispatcher.feed_update(bot, command(7, staff, group, "/help", 4))
    sent = [method for method in api.calls if method.__api_method__ == "sendMessage"]
    assert "Справка сотрудника" in sent[-1].text

    form = FSMContext(
        dispatcher.storage,
        StorageKey(bot_id=123456789, chat_id=group.id, user_id=admin.id),
    )
    await form.set_state(AdminForm.category)
    await dispatcher.feed_update(bot, command(8, admin, group, "/report", 4))
    sent = [method for method in api.calls if method.__api_method__ == "sendMessage"]
    assert "Отчёт службы поддержки" in sent[-1].text
    await form.set_state(AdminForm.category)
    await dispatcher.feed_update(bot, command(9, admin, group, "/admin", 4))
    assert await form.get_state() is None

    admin_private = Chat(id=admin.id, type="private")
    await dispatcher.feed_update(bot, command(6, admin, admin_private, "/help"))
    sent = [method for method in api.calls if method.__api_method__ == "sendMessage"]
    assert "Справка администратора" in sent[-1].text

    client = User(id=2002, is_bot=False, first_name="Client")
    private = Chat(id=2002, type="private")
    await dispatcher.feed_update(bot, command(10, client, private, "/start"))
    sent = [method for method in api.calls if method.__api_method__ == "sendMessage"]
    start = sent[-1]
    buttons = [button.text for row in start.reply_markup.inline_keyboard for button in row]
    assert "Написать в поддержку" in buttons
    assert "Как работает поддержка" in buttons

    await dispatcher.feed_update(bot, command(11, client, private, "/help"))
    sent = [method for method in api.calls if method.__api_method__ == "sendMessage"]
    assert "Центр поддержки" in sent[-1].text
    await bot.session.close()


async def test_bot_topic_service_messages_are_deleted(config) -> None:
    from support_bot.handlers.staff import delete_bot_service_message

    api = FakeSession()
    bot = Bot(
        "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        session=api,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    sender = User(id=123456789, is_bot=True, first_name="Support")
    group = Chat(id=config.support_group_id, type="supergroup")
    common = dict(date=datetime.now(), chat=group, from_user=sender, message_thread_id=501)
    pinned = Message(message_id=40, **common)
    messages = [
        Message(message_id=41, forum_topic_edited=ForumTopicEdited(name="Тикет"), **common),
        Message(message_id=42, pinned_message=pinned, **common),
    ]

    for message in messages:
        await delete_bot_service_message(message.as_(bot), config)

    deleted = [method.message_id for method in api.calls if method.__api_method__ == "deleteMessage"]
    assert deleted == [41, 42]
    await bot.session.close()
