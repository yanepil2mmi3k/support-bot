from __future__ import annotations

import html
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest
from conftest import FakeBot, FakeMessage

from support_bot.services.settings import SettingsService
from support_bot.services.tickets import TicketService
from support_bot.services.topics import TopicService


async def test_media_request_long_caption_and_separate_accept(config, db, employee) -> None:
    description = "Длинное описание <проблемы>. " * 100
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        await settings.set_topic("requests", 10)
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.create(
            user_id=2001,
            username="client",
            display_name="Клиент",
            category="Другое",
            description=description,
            media_file_id="photo-file",
            media_type="ContentType.PHOTO",
        )
        await topics.publish_request(tickets, settings, ticket, 77)
        assert ticket.request_media_message_id != ticket.request_message_id

    photo = bot.called("send_photo")
    assert len(photo) == 1
    caption = photo[0][1]["caption"]
    assert len(caption) <= 1024
    assert "Текст обрезан" in caption and "в топике" in caption
    controls = bot.called("send_message")[-1]
    button = controls[1]["reply_markup"].inline_keyboard[0][0]
    assert button.text == f"Принять заявку №{ticket.number}"

    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.get(1)
        await tickets.assign(ticket, employee)
        await topics.create_ticket_topic(tickets, settings, ticket, employee)
    topic_cards = [
        call for call in bot.called("send_message")
        if call[1].get("message_thread_id") == ticket.thread_id and "Описание проблемы" in call[0][1]
    ]
    assert len(topic_cards) == 1 and html.escape(description) in topic_cards[0][0][1]
    topic_media = [call for call in bot.called("copy_message") if call[1].get("message_thread_id") == ticket.thread_id]
    assert len(topic_media) == 1
    assert topic_media[0][1]["caption"] == ""


async def test_unchanged_topic_name_does_not_block_note_update(config, db, employee) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)

    async def topic_not_modified(*args, **kwargs):
        raise TelegramBadRequest(method=SimpleNamespace(), message="Bad Request: TOPIC_NOT_MODIFIED")

    bot.edit_forum_topic = topic_not_modified
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.create(
            user_id=2001, username=None, display_name="Клиент", category="Другое", description="Текст"
        )
        await tickets.assign(ticket, employee)
        ticket.thread_id = 501
        ticket.note = "Новое рабочее описание"
        await session.commit()

        await topics.update_pinned(tickets, settings, ticket)
        assert ticket.pinned_message_id is not None
        await topics.update_pinned(tickets, settings, ticket)

    edits = bot.called("edit_message_text")
    assert len(edits) == 1
    assert "Новое рабочее описание" in edits[0][1]["text"]


async def test_pending_addition_and_status_in_topic_title(config, db, employee) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        await settings.set_topic("requests", 10)
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.create(
            user_id=2001, username=None, display_name="Клиент", category="Другое", description="Текст"
        )
        addition = FakeMessage(2001, "Новые сведения", message_id=300)
        await topics.relay_user(addition, tickets, settings, ticket)
        sent = bot.called("send_message")[-1]
        assert "Дополнение к обращению №1" in sent[0][1]
        assert "Новые сведения" in sent[0][1]

        await tickets.assign(ticket, employee)
        await topics.create_ticket_topic(tickets, settings, ticket, employee)
        created_name = bot.called("create_forum_topic")[0][0][1]
        assert created_name == "Оператор; #1; В работе"
        await topics.close(tickets, settings, ticket, await settings.status("resolved"))
        renamed = bot.called("edit_forum_topic")[-1][1]["name"]
        assert renamed == "Оператор; #1; Решён"
