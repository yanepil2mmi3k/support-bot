from __future__ import annotations

import asyncio
from datetime import timedelta

from conftest import FakeBot, FakeCallback, FakeMessage, FakeState
from sqlalchemy import func, select

from support_bot.db.models import MessageLink, Review, Ticket, utcnow
from support_bot.handlers.user import create_ticket
from support_bot.jobs import BackgroundJobs
from support_bot.services.employees import EmployeeService
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import TicketService
from support_bot.services.topics import TopicService


async def test_thirty_complete_ticket_lifecycles(config, db) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    jobs = BackgroundJobs(config, db, topics)
    config.defaults["statuses"].append(
        {"key": "custom_done", "title": "Закрыто вручную", "code": "ЗК", "closed": True}
    )
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        for key, thread_id in {"requests": 10, "general": 1, "admin": 4}.items():
            await settings.set_topic(key, thread_id)
        employees = EmployeeService(session, config.admin_id)
        staff = [
            await employees.create(1100 + index, f"Оператор {index}", "f" if index % 2 else "m", f"Оп{index}")
            for index in range(5)
        ]
        tickets = TicketService(session, config.admin_id)
        for index in range(30):
            user_id = 3000 + index
            description = f"Описание {index} " * (100 if index % 8 == 0 else 1)
            ticket = await tickets.create(
                user_id=user_id,
                username=f"user{index}",
                display_name=f"Клиент {index}",
                category="Другое",
                description=description,
                media_file_id=f"photo-{index}" if index % 4 == 0 else None,
                media_type="photo" if index % 4 == 0 else None,
            )
            assert ticket.number == index + 1
            await topics.publish_request(tickets, settings, ticket, index + 100)
            if index % 5 == 0:
                await topics.relay_user(FakeMessage(user_id, "Дополнение", message_id=1000 + index), tickets, settings, ticket)
            employee = staff[index % len(staff)]
            await tickets.assign(ticket, employee)
            await topics.create_ticket_topic(tickets, settings, ticket, employee, index + 100)
            await topics.relay_user(FakeMessage(user_id, "Сообщение", message_id=2000 + index), tickets, settings, ticket)
            staff_message = FakeMessage(
                employee.telegram_id,
                "Ответ",
                chat_id=config.support_group_id,
                thread_id=ticket.thread_id,
                message_id=3000 + index,
            )
            await topics.relay_staff(staff_message, tickets, settings, ticket)
            if index % 3 == 0:
                target = staff[(index + 1) % len(staff)]
                await topics.transfer(tickets, settings, ticket, target)
                assert ticket.previous_employee_id == employee.id
            status_key = ["resolved", "archive", "custom_done"][index % 3]
            await topics.close(tickets, settings, ticket, await settings.status(status_key))
            resolved_at = ticket.resolved_at
            assert await tickets.active_for_user(user_id, await settings.closed_keys()) is None
            assert not await topics.close(tickets, settings, ticket, await settings.status(status_key))
            assert ticket.resolved_at == resolved_at
            await tickets.add_review(ticket, index % 5 + 1, f"Отзыв {index}")
            await topics.reopen(tickets, settings, ticket)
            assert ticket.status == "work" and ticket.resolved_at is None and ticket.archived_at is None
            assert (await tickets.active_for_user(user_id, await settings.closed_keys())).number == ticket.number
            await topics.close(tickets, settings, ticket)
            if index % 3 == 0:
                ticket.archived_at = utcnow() - timedelta(days=31)
                await tickets.session.commit()
        await settings.set("archive_auto_delete", True)
        await settings.set("archive_days", 30)

    await jobs.cleanup_archive()
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        all_tickets = list((await session.execute(select(Ticket))).scalars())
        assert len(all_tickets) == 30
        assert await db.counter_value() == 30
        assert sum(ticket.deleted_at is not None for ticket in all_tickets) == 10
        assert sum(ticket.status != "archive" for ticket in all_tickets) == 0
        assert await session.scalar(select(func.count(Review.id))) == 30
        for ticket in all_tickets:
            if ticket.deleted_at:
                assert ticket.user_id is None and ticket.description == "" and ticket.thread_id is None
                assert await session.scalar(
                    select(func.count(MessageLink.id)).where(MessageLink.ticket_id == ticket.id)
                ) == 0


async def test_twenty_schedule_flows_with_repeated_telegram_failures(config, db) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        await settings.set("assignment_mode", "schedule")
        employees = EmployeeService(session, config.admin_id)
        for index in range(3):
            employee = await employees.create(1200 + index, f"Смена {index}", "m", f"Смена{index}")
            await employees.set_schedule(employee, [(day, 0, 0) for day in range(7)])

    for index in range(20):
        user_id = 4000 + index
        callback = FakeCallback(user_id, "ticket:confirm", bot)
        state = FakeState(
            {"category": "Другое", "description": f"Обращение {index}"},
            "TicketForm:confirm",
        )
        if (index + 1) % 5 == 0:
            bot.fail_next("create_forum_topic")
            await create_ticket(callback, state, config, db, topics)
            assert state.clear_count == 0
            async with db.sessions() as session:
                assert await TicketService(session, config.admin_id).active_for_user(user_id) is None
        await create_ticket(callback, state, config, db, topics)
        assert state.clear_count == 1
        async with db.sessions() as session:
            ticket = await TicketService(session, config.admin_id).active_for_user(user_id)
            assert ticket is not None and ticket.thread_id is not None and ticket.employee_id is not None

    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        assert len(await tickets.list()) == 20
        assert await db.counter_value() == 24
        maximum = await session.scalar(select(func.max(Ticket.number)))
        assert maximum == 24
        assert len(bot.called("create_forum_topic")) == 24


async def test_concurrent_confirmation_creates_only_one_active_ticket(config, db) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        await SettingsService(session, config.defaults).set_topic("requests", 10)

    async def confirm():
        state = FakeState(
            {"category": "Другое", "description": "Одновременная отправка"},
            "TicketForm:confirm",
        )
        await create_ticket(FakeCallback(5000, "ticket:confirm", bot), state, config, db, topics)

    await asyncio.gather(*(confirm() for _ in range(10)))
    async with db.sessions() as session:
        rows = list(
            (
                await session.execute(
                    select(Ticket).where(Ticket.user_id == 5000, Ticket.deleted_at.is_(None))
                )
            ).scalars()
        )
        assert len(rows) == 1
        assert await db.counter_value() == 1
