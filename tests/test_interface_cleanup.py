from __future__ import annotations

from conftest import FakeBot, FakeCallback, FakeMessage, FakeState

from support_bot.handlers.user import rate_ticket, review_comment
from support_bot.services.interface import InterfaceService
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import TicketService
from support_bot.services.topics import TopicService


async def test_interface_trim_and_feedback_cleanup(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        interface = InterfaceService(session)
        for message_id in [10, 11, 12]:
            await interface.track("edit:test", -100500, message_id)
        await interface.trim("edit:test", bot, keep=2)
        assert await interface.ids("edit:test") == [11, 12]
        assert bot.called("delete_message")[0][1]["message_id"] == 10
        await interface.clear("edit:test", bot)
        assert await interface.ids("edit:test") == []

        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.get(ticket_number)
        await topics.close(tickets, settings, ticket, await settings.status("resolved"))

    state = FakeState()
    await rate_ticket(FakeCallback(2002, f"rate:{ticket_number}:5", bot), state, config, db)
    comment = FakeMessage(2002, "Всё хорошо")
    comment.bot = bot
    await review_comment(comment, state, config, db, topics)
    final = comment.answers[-1]
    assert "Спасибо за обратную связь" in final["text"]
    assert "Если понадобится помощь," in final["text"]
    assert "Здравствуйте" not in final["text"]
    buttons = [button.text for row in final["reply_markup"].inline_keyboard for button in row]
    assert config.message("contact") in buttons
    assert len(bot.called("delete_message")) >= 4
    async with db.sessions() as session:
        assert await InterfaceService(session).ids("user:2002") == [9001]
