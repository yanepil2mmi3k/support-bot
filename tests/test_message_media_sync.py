from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.enums import ContentType
from aiogram.types import (
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)
from conftest import FakeBot, FakeMessage

from support_bot.db.models import MessageLink
from support_bot.handlers.staff import edit_staff_message, relay_staff_message
from support_bot.handlers.user import edit_user_message, relay_user_message
from support_bot.services.employees import EmployeeService
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import TicketService, extract_message_media
from support_bot.services.topics import TopicService


def link() -> MessageLink:
    return MessageLink(
        id=1,
        ticket_id=1,
        source_chat_id=2002,
        source_message_id=300,
        target_chat_id=-100500,
        target_message_id=900,
    )


@pytest.mark.parametrize(
    ("attribute", "content_type", "media_class"),
    [
        ("photo", ContentType.PHOTO, InputMediaPhoto),
        ("video", ContentType.VIDEO, InputMediaVideo),
        ("document", ContentType.DOCUMENT, InputMediaDocument),
        ("audio", ContentType.AUDIO, InputMediaAudio),
        ("animation", ContentType.ANIMATION, InputMediaAnimation),
    ],
)
async def test_edit_replaces_supported_media(config, attribute, content_type, media_class) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    message = FakeMessage(2002, "", message_id=300)
    message.text = None
    message.content_type = content_type
    media = SimpleNamespace(file_id=f"new-{attribute}")
    setattr(message, attribute, [media] if attribute == "photo" else media)
    message.caption = "Новая подпись"

    assert await topics.relay_edit(message, link()) is True

    call = bot.called("edit_message_media")[0][1]
    assert isinstance(call["media"], media_class)
    assert call["media"].media == f"new-{attribute}"
    assert call["media"].caption == "Новая подпись"
    assert call["chat_id"] == -100500
    assert call["message_id"] == 900
    assert not bot.called("edit_message_caption")


async def test_edit_media_clears_removed_caption(config) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    message = FakeMessage(2002, "", message_id=300)
    message.text = None
    message.content_type = ContentType.PHOTO
    message.photo = [SimpleNamespace(file_id="same-photo")]
    message.caption = None

    assert await topics.relay_edit(message, link()) is True

    media = bot.called("edit_message_media")[0][1]["media"]
    assert media.media == "same-photo"
    assert media.caption is None


async def test_edit_caption_without_media_replacement(config) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    message = FakeMessage(2002, "", message_id=300)
    message.text = None
    message.caption = "Только подпись"

    assert await topics.relay_edit(message, link()) is True

    call = bot.called("edit_message_caption")[0][1]
    assert call["caption"] == "Только подпись"
    assert not bot.called("edit_message_media")


async def test_closed_ticket_edit_is_not_relayed(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    message = FakeMessage(2002, "Исходный текст", message_id=300)
    await relay_user_message(message, config, db, topics)

    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(ticket_number)
        await topics.close(tickets, SettingsService(session, config.defaults), ticket)
    bot.calls.clear()

    message.text = "Текст после закрытия"
    await edit_user_message(message, config, db, topics)

    assert not bot.called("edit_message_text")
    assert not bot.called("edit_message_media")


async def test_other_employee_edit_is_not_relayed(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    message = FakeMessage(
        employee.telegram_id,
        "Ответ оператора",
        chat_id=config.support_group_id,
        thread_id=777,
        message_id=400,
    )
    await relay_staff_message(message, config, db, topics)
    async with db.sessions() as session:
        other = await EmployeeService(session, config.admin_id).create(1002, "Другой", "m", "Другой")
    bot.calls.clear()

    message.from_user.id = other.telegram_id
    message.text = "Попытка изменить чужой ответ"
    await edit_staff_message(message, config, db, topics)

    assert not bot.called("edit_message_text")
    assert not bot.called("edit_message_media")


@pytest.mark.parametrize(
    ("attribute", "content_type"),
    [
        ("photo", ContentType.PHOTO),
        ("video", ContentType.VIDEO),
        ("document", ContentType.DOCUMENT),
        ("animation", ContentType.ANIMATION),
        ("audio", ContentType.AUDIO),
        ("voice", ContentType.VOICE),
        ("video_note", ContentType.VIDEO_NOTE),
        ("sticker", ContentType.STICKER),
    ],
)
async def test_media_extractor_and_user_relay_without_caption(
    config, db, employee, ticket_number, attribute, content_type
) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    message = FakeMessage(2002, "", message_id=300)
    message.text = None
    message.content_type = content_type
    media = SimpleNamespace(
        file_id=f"{attribute}-large",
        file_name=f"{attribute}.bin",
        mime_type="application/octet-stream",
        file_size=123,
        thumbnail=SimpleNamespace(file_id=f"{attribute}-thumbnail"),
    )
    setattr(message, attribute, [SimpleNamespace(file_id="photo-small"), media] if attribute == "photo" else media)

    assert extract_message_media(message) == {
        "media_file_id": f"{attribute}-large",
        "thumbnail_file_id": f"{attribute}-thumbnail",
        "file_name": f"{attribute}.bin",
        "mime_type": "application/octet-stream",
        "file_size": 123,
    }
    await relay_user_message(message, config, db, topics)

    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(ticket_number)
        saved = await tickets.linked_message(ticket, 2002, 300)
        assert saved.body == "" and saved.content_type == attribute
        assert saved.media_file_id == f"{attribute}-large"
        assert saved.thumbnail_file_id == f"{attribute}-thumbnail"
        assert saved.file_name == f"{attribute}.bin"
        assert saved.mime_type == "application/octet-stream"
        assert saved.file_size == 123


async def test_staff_media_relay_saves_empty_body_and_metadata(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    message = FakeMessage(
        employee.telegram_id,
        "",
        chat_id=config.support_group_id,
        thread_id=777,
        message_id=400,
    )
    message.text = None
    message.content_type = ContentType.VOICE
    message.voice = SimpleNamespace(file_id="voice-id", file_size=456, mime_type="audio/ogg")

    await relay_staff_message(message, config, db, topics)

    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(ticket_number)
        saved = await tickets.linked_message(ticket, config.support_group_id, 400)
        assert saved.from_staff is True and saved.body == ""
        assert saved.media_file_id == "voice-id" and saved.file_size == 456
        assert saved.mime_type == "audio/ogg" and saved.thumbnail_file_id is None


async def test_edit_updates_stored_media_metadata(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    message = FakeMessage(2002, "", message_id=300)
    message.text = None
    message.content_type = ContentType.PHOTO
    message.photo = [SimpleNamespace(file_id="old-photo", file_size=100)]
    await relay_user_message(message, config, db, topics)

    message.photo = [SimpleNamespace(
        file_id="new-photo",
        file_size=200,
        thumbnail=SimpleNamespace(file_id="new-thumbnail"),
    )]
    message.caption = "Обновлённая подпись"
    await edit_user_message(message, config, db, topics)

    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(ticket_number)
        saved = await tickets.linked_message(ticket, 2002, 300)
        assert saved.body == "Обновлённая подпись"
        assert saved.media_file_id == "new-photo"
        assert saved.thumbnail_file_id == "new-thumbnail"
        assert saved.file_size == 200
