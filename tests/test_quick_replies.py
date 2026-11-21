from __future__ import annotations

from types import SimpleNamespace

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from conftest import FakeBot, FakeMessage
from sqlalchemy import select

from support_bot.db.models import MessageLink, QuickReply, QuickReplyCopy, QuickReplyItem, SystemTopic
from support_bot.handlers.staff import setup_group
from support_bot.miniapp import create_miniapp
from support_bot.services.employees import EmployeeService
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import TicketService
from support_bot.services.topics import TopicService


async def _client(config, db, bot):
    client = TestClient(TestServer(create_miniapp(config, db, bot, TopicService(bot, config))))
    await client.start_server()
    return client


async def _prepare(config, db, employee):
    async with db.sessions() as session:
        await EmployeeService(session, config.admin_id).ensure_admin()
        if await session.get(SystemTopic, "quick_replies") is None:
            session.add(SystemTopic(key="quick_replies", thread_id=901))
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.create(
            user_id=2001,
            username="client",
            display_name="Клиент",
            category="Другое",
            description="Проверка быстрых ответов",
        )
        await tickets.assign(ticket, employee)
        ticket.thread_id = 777
        ticket.pinned_message_id = 555
        await session.commit()
        return ticket.number


def _form(title: str | None = None, text: str | None = None, files: list[tuple[str, bytes]] | None = None) -> FormData:
    form = FormData()
    if title is not None:
        form.add_field("title", title)
    if text is not None:
        form.add_field("text", text)
    for index, (media_type, content) in enumerate(files or [], start=1):
        form.add_field(f"media_type_{index}", media_type)
        form.add_field(
            f"file_{index}", content, filename=f"file-{index}.bin", content_type="application/octet-stream"
        )
    return form


async def test_setup_creates_quick_replies_topic_idempotently(config, db) -> None:
    message = FakeMessage(config.admin_id, "/setup", chat_id=config.support_group_id, thread_id=1)
    bot = message.bot

    await setup_group(message, config, db)
    await setup_group(message, config, db)

    topics = [call for call in bot.called("create_forum_topic")]
    assert len(topics) == 4
    assert [call[0][1] for call in topics] == ["Команды", "Заявки", "Админ-инфа", "Быстрые ответы"]
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        assert await settings.topic("quick_replies") == 504


async def test_text_reply_shared_crud_send_retry_and_source_sync(config, db, employee) -> None:
    bot = FakeBot()
    number = await _prepare(config, db, employee)
    client = await _client(config, db, bot)
    headers = {"X-Dev-User-ID": str(employee.telegram_id)}
    try:
        response = await client.get("/api/quick-replies", headers=headers)
        assert response.status == 200 and (await response.json())["quick_replies"] == []

        response = await client.post(
            "/api/quick-replies", data=_form("Приветствие", "Здравствуйте!"), headers=headers
        )
        assert response.status == 201
        reply = (await response.json())["quick_reply"]
        assert reply["title"] == "Приветствие" and reply["text"] == "Здравствуйте!"
        assert reply["position"] == 0 and len(reply["items"]) == 1
        item = reply["items"][0]
        assert item["content_type"] == "text" and item["media_url"] is None
        source_call = bot.called("send_message")[0]
        assert source_call[0] == (config.support_group_id, "Здравствуйте!")
        assert source_call[1]["message_thread_id"] == 901 and source_call[1]["parse_mode"] is None

        response = await client.post(f"/api/tickets/{number}/quick-replies/{reply['id']}", headers=headers)
        assert response.status == 201
        messages = (await response.json())["messages"]
        assert len(messages) == 1 and messages[0]["body"] == "Здравствуйте!"
        copies = bot.called("copy_message")
        assert len(copies) == 2
        assert copies[0][0] == (config.support_group_id, config.support_group_id, item["source_message_id"])
        assert copies[0][1]["message_thread_id"] == 777
        assert copies[1][0] == (2001, config.support_group_id, item["source_message_id"])

        response = await client.post(f"/api/tickets/{number}/quick-replies/{reply['id']}", headers=headers)
        assert response.status == 201
        assert (await response.json())["messages"][0]["id"] == messages[0]["id"]
        assert len(bot.called("copy_message")) == 2

        edit_count = len(bot.called("edit_message_text"))
        response = await client.patch(f"/api/quick-replies/{reply['id']}", json={"title": "Новое название"})
        assert response.status == 200
        assert len(bot.called("edit_message_text")) == edit_count and not bot.called("edit_message_caption")

        response = await client.patch(f"/api/quick-replies/{reply['id']}", json={"text": "Обновлённый текст"})
        assert response.status == 200
        assert (await response.json())["quick_reply"]["text"] == "Обновлённый текст"
        edits = bot.called("edit_message_text")[edit_count:]
        assert len(edits) == 3
        assert (edits[0][1]["chat_id"], edits[0][1]["message_id"]) == (
            config.support_group_id, item["source_message_id"]
        )
        assert [call[1]["chat_id"] for call in edits[1:]] == [config.support_group_id, 2001]
        response = await client.get(f"/api/tickets/{number}/messages", headers=headers)
        assert (await response.json())["messages"][0]["body"] == "Обновлённый текст"
    finally:
        await client.close()


async def test_media_reply_order_media_stream_and_future_send_order(config, db, employee) -> None:
    bot = FakeBot()
    number = await _prepare(config, db, employee)
    client = await _client(config, db, bot)
    try:
        response = await client.post(
            "/api/quick-replies",
            data=_form("Инструкция", "Смотрите вложения", [("photo", b"image"), ("document", b"document")]),
        )
        assert response.status == 201
        reply = (await response.json())["quick_reply"]
        items = reply["items"]
        assert [item["position"] for item in items] == [0, 1]
        assert [item["text"] for item in items] == ["Смотрите вложения", ""]
        assert items[0]["media_url"] == f"/api/quick-reply-items/{items[0]['id']}/media"
        assert items[1]["file_name"] == "file-2.bin"
        assert bot.called("send_photo")[0][1]["caption"] == "Смотрите вложения"
        assert bot.called("send_document")[0][1]["caption"] is None

        response = await client.get(items[0]["media_url"])
        assert response.status == 200 and await response.read() == b"image"

        response = await client.post(
            f"/api/quick-replies/{reply['id']}/items", data=_form(files=[("video", b"video")])
        )
        assert response.status == 201
        reply = (await response.json())["quick_reply"]
        assert len(reply["items"]) == 3 and reply["items"][2]["content_type"] == "video"

        response = await client.post(f"/api/tickets/{number}/quick-replies/{reply['id']}")
        assert response.status == 201
        first_messages = (await response.json())["messages"]
        assert [message["content_type"] for message in first_messages] == ["photo", "document", "video"]
        assert len(bot.called("copy_message")) == 6

        ordered = [reply["items"][2]["id"], reply["items"][0]["id"], reply["items"][1]["id"]]
        response = await client.patch(f"/api/quick-replies/{reply['id']}", json={"item_order": ordered})
        assert response.status == 200
        reordered = (await response.json())["quick_reply"]
        assert [item["id"] for item in reordered["items"]] == ordered
        assert not bot.called("send_message") and len(bot.called("copy_message")) == 6

        response = await client.post(f"/api/tickets/{number}/quick-replies/{reply['id']}")
        assert response.status == 201
        retried = (await response.json())["messages"]
        assert [message["id"] for message in retried] == [
            first_messages[2]["id"], first_messages[0]["id"], first_messages[1]["id"]
        ]
        assert len(bot.called("copy_message")) == 6
    finally:
        await client.close()


async def test_source_caption_edit_and_individual_copy_lifecycle(config, db, employee) -> None:
    bot = FakeBot()
    number = await _prepare(config, db, employee)
    client = await _client(config, db, bot)
    try:
        response = await client.post(
            "/api/quick-replies", data=_form("Фото", "Исходная подпись", [("photo", b"image")])
        )
        reply = (await response.json())["quick_reply"]
        response = await client.post(f"/api/tickets/{number}/quick-replies/{reply['id']}")
        message = (await response.json())["messages"][0]

        response = await client.patch(f"/api/quick-replies/{reply['id']}", json={"text": "Новая подпись"})
        assert response.status == 200
        edits = bot.called("edit_message_caption")
        assert len(edits) == 3 and all(call[1]["caption"] == "Новая подпись" for call in edits)

        response = await client.patch(f"/api/messages/{message['id']}", json={"text": "Только эта копия"})
        assert response.status == 200 and (await response.json())["message"]["body"] == "Только эта копия"
        assert len(bot.called("edit_message_caption")) == 5
        async with db.sessions() as session:
            item = await session.get(QuickReplyItem, reply["items"][0]["id"])
            assert item.text == "Новая подпись"

        response = await client.patch(f"/api/messages/{message['id']}", json={"text": ""})
        assert response.status == 200 and (await response.json())["message"]["body"] == ""
        assert bot.called("edit_message_caption")[-1][1]["caption"] is None

        response = await client.delete(f"/api/messages/{message['id']}")
        assert response.status == 200
        async with db.sessions() as session:
            assert await session.get(QuickReplyItem, reply["items"][0]["id"]) is not None
            assert await session.get(MessageLink, message["id"]) is None
            assert await session.scalar(select(QuickReplyCopy)) is None
        deleted_points = {(call[1]["chat_id"], call[1]["message_id"]) for call in bot.called("delete_message")}
        assert (config.support_group_id, reply["items"][0]["source_message_id"]) not in deleted_points
    finally:
        await client.close()


async def test_retry_resumes_after_completed_item_without_duplicates(config, db, employee) -> None:
    bot = FakeBot()
    number = await _prepare(config, db, employee)
    client = await _client(config, db, bot)
    try:
        response = await client.post(
            "/api/quick-replies", data=_form("Несколько", "Текст", [("photo", b"one"), ("document", b"two")])
        )
        reply = (await response.json())["quick_reply"]
        original = bot.copy_message
        count = 0

        async def fail_third_copy(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 3:
                raise TelegramAPIError(SimpleNamespace(), "Сбой второго элемента")
            return await original(*args, **kwargs)

        bot.copy_message = fail_third_copy
        response = await client.post(f"/api/tickets/{number}/quick-replies/{reply['id']}")
        assert response.status == 500
        bot.copy_message = original
        async with db.sessions() as session:
            links = list((await session.execute(select(MessageLink))).scalars())
            copies = list((await session.execute(select(QuickReplyCopy))).scalars())
            assert len(links) == 1 and len(copies) == 1

        response = await client.post(f"/api/tickets/{number}/quick-replies/{reply['id']}")
        assert response.status == 201
        messages = (await response.json())["messages"]
        assert len(messages) == 2
        assert len({message["id"] for message in messages}) == 2
        assert count == 3 and len(bot.called("copy_message")) == 4
        async with db.sessions() as session:
            assert len(list((await session.execute(select(MessageLink))).scalars())) == 2
            assert len(list((await session.execute(select(QuickReplyCopy))).scalars())) == 2
    finally:
        await client.close()


async def test_delete_item_and_reply_remove_all_available_messages(config, db, employee) -> None:
    bot = FakeBot()
    number = await _prepare(config, db, employee)
    client = await _client(config, db, bot)
    try:
        response = await client.post(
            "/api/quick-replies", data=_form("Удаление", "Подпись", [("photo", b"one"), ("document", b"two")])
        )
        reply = (await response.json())["quick_reply"]
        response = await client.post(f"/api/tickets/{number}/quick-replies/{reply['id']}")
        assert response.status == 201
        first_item, second_item = reply["items"]
        original = bot.delete_message
        attempted = []

        async def delete_with_missing(*args, **kwargs):
            point = (kwargs["chat_id"], kwargs["message_id"])
            attempted.append(point)
            if point == (config.support_group_id, first_item["source_message_id"]):
                raise TelegramBadRequest(SimpleNamespace(), "message not found")
            return await original(*args, **kwargs)

        bot.delete_message = delete_with_missing
        response = await client.delete(f"/api/quick-reply-items/{first_item['id']}")
        assert response.status == 200
        assert len(attempted) == 3
        async with db.sessions() as session:
            assert await session.get(QuickReplyItem, first_item["id"]) is None
            assert len(list((await session.execute(select(MessageLink))).scalars())) == 1
            remaining = await session.get(QuickReplyItem, second_item["id"])
            assert remaining.position == 0

        response = await client.delete(f"/api/quick-replies/{reply['id']}")
        assert response.status == 200
        async with db.sessions() as session:
            assert await session.get(QuickReply, reply["id"]) is None
            assert list((await session.execute(select(QuickReplyItem))).scalars()) == []
            assert list((await session.execute(select(QuickReplyCopy))).scalars()) == []
            assert list((await session.execute(select(MessageLink))).scalars()) == []
    finally:
        await client.close()


async def test_send_acl_closed_ticket_and_multipart_validation(config, db, employee) -> None:
    bot = FakeBot()
    number = await _prepare(config, db, employee)
    async with db.sessions() as session:
        other = await EmployeeService(session, config.admin_id).create(1002, "Другой", "m", "Другой")
    client = await _client(config, db, bot)
    try:
        response = await client.post("/api/quick-replies", data=_form("Ответ", "Текст"))
        reply = (await response.json())["quick_reply"]
        response = await client.post(
            f"/api/tickets/{number}/quick-replies/{reply['id']}",
            headers={"X-Dev-User-ID": str(other.telegram_id)},
        )
        assert response.status == 400
        assert not bot.called("copy_message")

        async with db.sessions() as session:
            tickets = TicketService(session, config.admin_id)
            ticket = await tickets.get(number)
            await tickets.set_status(ticket, "resolved", {"key": "resolved", "closed": True})
        response = await client.post(f"/api/tickets/{number}/quick-replies/{reply['id']}")
        assert response.status == 400 and not bot.called("copy_message")

        form = FormData()
        form.add_field("title", "Битый")
        form.add_field("file_1", b"content", filename="file.bin")
        response = await client.post("/api/quick-replies", data=form)
        assert response.status == 400
        assert list((config.database_path.parent / ".uploads").iterdir()) == []

        response = await client.patch(f"/api/quick-replies/{reply['id']}", json={"item_order": [999]})
        assert response.status == 400
        response = await client.post("/api/quick-replies", data=_form("Пустой", ""))
        assert response.status == 400
    finally:
        await client.close()
