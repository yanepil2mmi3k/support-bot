from __future__ import annotations

from conftest import FakeBot, FakeCallback, FakeState

from support_bot.handlers.user import create_ticket
from support_bot.services.employees import EmployeeService
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import TicketService
from support_bot.services.topics import TopicService


def form_state() -> FakeState:
    return FakeState(
        {"category": "Другое", "description": "Проверка сбоя"},
        "TicketForm:confirm",
    )


async def enable_schedule(db, config, employee) -> None:
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        await settings.set("assignment_mode", "schedule")
        employees = EmployeeService(session, config.admin_id)
        current = await employees.get(employee.telegram_id)
        await employees.set_schedule(current, [(day, 0, 0) for day in range(7)])


async def test_manual_publish_failure_compensates_and_retries(config, db) -> None:
    async with db.sessions() as session:
        await SettingsService(session, config.defaults).set_topic("requests", 10)
    bot = FakeBot()
    bot.fail_next("send_message")
    callback = FakeCallback(2001, "ticket:confirm", bot)
    state = form_state()
    topics = TopicService(bot, config)

    await create_ticket(callback, state, config, db, topics)

    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        cancelled = await tickets.get(1)
        assert cancelled is not None and cancelled.deleted_at is not None
        assert cancelled.employee_id is None
        assert await tickets.active_for_user(2001) is None
        assert await db.counter_value() == 1
    assert state.state == "TicketForm:confirm"
    assert state.data["description"] == "Проверка сбоя"
    assert not bot.called("create_forum_topic")

    await create_ticket(callback, state, config, db, topics)

    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        published = await tickets.get(2)
        assert published is not None and published.request_message_id is not None
        assert published.deleted_at is None
        assert (await tickets.active_for_user(2001)).number == 2
        assert len(await tickets.list()) == 1
        assert await db.counter_value() == 2
    assert state.clear_count == 1
    published_calls = len(bot.called("send_message"))

    recovery_state = form_state()
    await create_ticket(callback, recovery_state, config, db, topics)
    assert len(bot.called("send_message")) == published_calls
    assert recovery_state.clear_count == 1
    async with db.sessions() as session:
        assert await db.counter_value() == 2
        assert (await TicketService(session, config.admin_id).active_for_user(2001)).number == 2


async def test_schedule_topic_failure_compensates_and_does_not_reuse_number(config, db, employee) -> None:
    await enable_schedule(db, config, employee)
    bot = FakeBot()
    bot.fail_next("create_forum_topic")
    callback = FakeCallback(2001, "ticket:confirm", bot)
    state = form_state()
    topics = TopicService(bot, config)

    await create_ticket(callback, state, config, db, topics)

    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        cancelled = await tickets.get(1)
        assert cancelled is not None and cancelled.deleted_at is not None
        assert cancelled.employee_id is None
        assert await tickets.active_for_user(2001) is None
    assert state.clear_count == 0

    await create_ticket(callback, state, config, db, topics)
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        recovered = await tickets.get(2)
        assert recovered is not None and recovered.thread_id is not None
        assert recovered.employee_id == employee.id
        assert (await tickets.active_for_user(2001)).number == 2
        assert await db.counter_value() == 2
    created_topic_calls = len(bot.called("create_forum_topic"))

    recovery_state = form_state()
    await create_ticket(callback, recovery_state, config, db, topics)
    assert len(bot.called("create_forum_topic")) == created_topic_calls
    async with db.sessions() as session:
        assert await db.counter_value() == 2


async def test_existing_topic_is_preserved_after_later_telegram_failure(config, db, employee) -> None:
    await enable_schedule(db, config, employee)
    bot = FakeBot()
    bot.fail_next("send_message")
    callback = FakeCallback(2001, "ticket:confirm", bot)
    state = form_state()
    topics = TopicService(bot, config)

    await create_ticket(callback, state, config, db, topics)

    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(1)
        assert ticket is not None and ticket.thread_id == 501
        assert ticket.deleted_at is None and ticket.employee_id == employee.id
        assert "сохранено" in callback.message.answers[-1]["text"]
    assert state.clear_count == 0

    await create_ticket(callback, state, config, db, topics)
    assert len(bot.called("create_forum_topic")) == 1
    assert len(bot.called("send_message")) == 3
    assert state.clear_count == 1
    async with db.sessions() as session:
        ticket = await TicketService(session, config.admin_id).get(1)
        assert ticket.thread_id == 501 and ticket.pinned_message_id is not None
        assert await db.counter_value() == 1
