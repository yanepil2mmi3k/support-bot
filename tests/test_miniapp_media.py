from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from aiogram.types import FSInputFile
from conftest import FakeBot

from support_bot.db.models import MessageLink
from support_bot.db.session import Database
from support_bot.miniapp import create_miniapp
from support_bot.services.employees import EmployeeService
from support_bot.services.tickets import TicketService
from support_bot.services.topics import TopicService


async def _client(config, db, bot):
    client = TestClient(TestServer(create_miniapp(config, db, bot, TopicService(bot, config))))
    await client.start_server()
    return client


async def _ticket(config, db, employee, *, with_media: bool = False):
    async with db.sessions() as session:
        await EmployeeService(session, config.admin_id).ensure_admin()
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.create(
            user_id=2001,
            username="client",
            display_name="Клиент",
            category="Другое",
            description="Проверка медиа",
            media_file_id="ticket-photo" if with_media else None,
            media_type="ContentType.PHOTO" if with_media else None,
        )
        await tickets.assign(ticket, employee)
        ticket.thread_id = 777
        ticket.pinned_message_id = 555
        await session.commit()
        return ticket.number


async def _document_link(config, db, number: int) -> int:
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(number)
        link = await tickets.save_message_link(
            ticket,
            config.support_group_id,
            800,
            ticket.user_id,
            900,
            body="",
            content_type="document",
            from_staff=True,
            media_file_id="document-id",
            thumbnail_file_id="thumbnail-id",
            file_name='report".pdf',
            mime_type="application/pdf",
            file_size=11,
        )
        return link.id


async def test_message_and_ticket_media_contract_and_streaming(config, db, employee) -> None:
    bot = FakeBot()
    number = await _ticket(config, db, employee, with_media=True)
    link_id = await _document_link(config, db, number)
    bot.media_contents["document-id"] = b"hello media"
    bot.media_contents["thumbnail-id"] = b"thumb"
    bot.media_contents["ticket-photo"] = b"ticket image"
    client = await _client(config, db, bot)
    try:
        response = await client.get(f"/api/tickets/{number}/messages")
        assert response.status == 200
        payload = await response.json()
        assert payload["ticket"]["media_url"] == f"/api/tickets/{number}/media"
        assert payload["ticket"]["media_type"] == "photo"
        message = payload["messages"][0]
        assert message["media_url"] == f"/api/messages/{link_id}/media"
        assert message["thumbnail_url"] == f"/api/messages/{link_id}/media?thumbnail=1"
        assert message["file_name"] == 'report".pdf'
        assert message["mime_type"] == "application/pdf"
        assert message["file_size"] == 11
        assert message["body"] == "" and message["legacy"] is False

        response = await client.get(f"/api/messages/{link_id}/media")
        assert response.status == 200
        assert await response.read() == b"hello media"
        assert response.headers["Content-Type"] == "application/pdf"
        assert response.headers["Content-Length"] == "11"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "private, no-store"
        assert response.headers["Content-Disposition"] == (
            "attachment; filename=\"report_.pdf\"; filename*=UTF-8''report%22.pdf"
        )
        assert "Location" not in response.headers

        response = await client.get(f"/api/messages/{link_id}/media?thumbnail=1")
        assert response.status == 200
        assert await response.read() == b"thumb"
        assert response.headers["Content-Type"] == "image/jpeg"
        assert "Content-Disposition" not in response.headers

        response = await client.get(f"/api/tickets/{number}/media")
        assert response.status == 200
        assert await response.read() == b"ticket image"
        assert response.headers["Content-Type"] == "image/jpeg"
        assert bot.closed_streams == 3
        assert all("test-token" not in str(value) for value in response.headers.values())
    finally:
        await client.close()


async def test_media_acl_legacy_fallback_and_upstream_error(config, db, employee) -> None:
    bot = FakeBot()
    number = await _ticket(config, db, employee)
    link_id = await _document_link(config, db, number)
    async with db.sessions() as session:
        await EmployeeService(session, config.admin_id).create(1002, "Другой", "m", "Другой")
        ticket = await TicketService(session, config.admin_id).get(number)
        session.add(MessageLink(ticket_id=ticket.id, source_chat_id=1, source_message_id=1, target_chat_id=2, target_message_id=2, body=None, content_type="photo"))
        await session.commit()
    client = await _client(config, db, bot)
    try:
        response = await client.get(f"/api/messages/{link_id}/media", headers={"X-Dev-User-ID": "1002"})
        assert response.status == 400
        assert await response.json() == {"detail": "Можно управлять только своими обращениями"}
        assert not bot.called("get_file")

        response = await client.get(f"/api/messages/{link_id + 1}/media")
        assert response.status == 404
        assert response.content_type == "application/json"
        response = await client.get(f"/api/tickets/{number}/messages")
        legacy = (await response.json())["messages"][1]
        assert legacy["legacy"] is True and legacy["media_url"] is None
        assert legacy["thumbnail_url"] is None and legacy["file_size"] is None

        bot.fail_next("stream_content")
        response = await client.get(f"/api/messages/{link_id}/media")
        assert response.status == 500
        assert response.content_type == "application/json"
        assert await response.json() == {"detail": "Действие не выполнено"}
        assert bot.closed_streams == 1
    finally:
        await client.close()


@pytest.mark.parametrize("media_type", ["photo", "video", "document", "animation", "audio", "voice"])
async def test_multipart_media_upload(config, db, employee, media_type) -> None:
    bot = FakeBot()
    number = await _ticket(config, db, employee)
    client = await _client(config, db, bot)
    content = f"{media_type} content".encode()
    try:
        form = FormData()
        form.add_field("media_type", media_type)
        form.add_field("caption", "")
        form.add_field("file", content, filename=f"{media_type}.bin", content_type="application/octet-stream")
        response = await client.post(f"/api/tickets/{number}/messages/media", data=form)
        assert response.status == 201
        message = (await response.json())["message"]
        assert message["content_type"] == media_type
        assert message["body"] == "" and message["legacy"] is False
        assert message["file_name"] == f"{media_type}.bin"
        assert message["mime_type"] == "application/octet-stream"
        assert message["file_size"] == len(content)
        assert message["media_url"] == f"/api/messages/{message['id']}/media"

        calls = bot.called(f"send_{media_type}")
        assert len(calls) == 2
        assert calls[0][0][0] == 2001 and isinstance(calls[0][1][media_type], FSInputFile)
        assert calls[1][0][0] == config.support_group_id
        assert isinstance(calls[1][1][media_type], str)
        assert calls[1][1][media_type] in bot.media_contents
        assert calls[1][1]["message_thread_id"] == 777
        assert all(call[1]["parse_mode"] is None for call in calls)

        response = await client.get(message["media_url"])
        assert response.status == 200 and await response.read() == content
        assert list((config.database_path.parent / ".uploads").iterdir()) == []
    finally:
        await client.close()


async def test_multipart_media_validation_and_cleanup(config, db, employee, monkeypatch) -> None:
    monkeypatch.setattr("support_bot.miniapp.PHOTO_MAX_SIZE", 4)
    monkeypatch.setattr("support_bot.miniapp.MEDIA_MAX_SIZE", 8)
    bot = FakeBot()
    number = await _ticket(config, db, employee)
    client = await _client(config, db, bot)
    try:
        cases = [
            ("photo", b"", "", "Файл не должен быть пустым"),
            ("sticker", b"data", "", "Неподдерживаемый тип медиа"),
            ("photo", b"12345", "", "Размер фотографии превышает 10 MiB"),
            ("document", b"123456789", "", "Размер файла превышает 50 MiB"),
            ("photo", b"data", "x" * 1025, "Подпись должна быть не длиннее 1024 символов"),
        ]
        for media_type, content, caption, detail in cases:
            form = FormData()
            form.add_field("media_type", media_type)
            form.add_field("caption", caption)
            form.add_field("file", content, filename="file.bin", content_type="application/octet-stream")
            response = await client.post(f"/api/tickets/{number}/messages/media", data=form)
            assert response.status == 400
            assert await response.json() == {"detail": detail}
        assert not bot.called("send_photo") and not bot.called("send_document")
        assert list((config.database_path.parent / ".uploads").iterdir()) == []
    finally:
        await client.close()


async def test_message_link_media_columns_are_additive(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE message_links (id INTEGER PRIMARY KEY, body TEXT)")
    connection.commit()
    connection.close()

    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}", path)
    await database.init()
    try:
        async with database.engine.begin() as connection:
            rows = await connection.exec_driver_sql("PRAGMA table_info(message_links)")
            columns = {row[1]: row for row in rows}
        expected = {"media_file_id", "thumbnail_file_id", "file_name", "mime_type", "file_size"}
        assert expected <= columns.keys()
        assert all(columns[name][4] is None for name in expected)
    finally:
        await database.close()
