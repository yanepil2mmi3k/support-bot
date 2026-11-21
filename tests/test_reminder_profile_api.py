from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import FakeBot, FakeCallback, FakeMessage, FakeState
from sqlalchemy import select

from support_bot.db.models import Reminder, utcnow
from support_bot.handlers.staff import (
    reminder_disable,
    reminder_transfer,
    reminder_transfer_confirm,
    reminder_transfer_target,
    save_greeting,
)
from support_bot.jobs import BackgroundJobs
from support_bot.miniapp import create_miniapp
from support_bot.services.employees import EmployeeService
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import TicketService
from support_bot.services.topics import TopicService


def buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


async def test_ticket_reminder_pin_and_owner_transfer_api(config, db, employee) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        await employees.ensure_admin()
        target = await employees.create(1002, "Смена", "m", "Смена")
        stranger = await employees.create(1003, "Чужой", "m", "Чужой")
        inactive = await employees.create(1004, "Неактивный", "m", "Неактивный")
        await employees.update(inactive, active=False)
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.create(
            user_id=2001,
            username="client",
            display_name="Клиент",
            category="Другое",
            description="Проверка API",
        )
        await tickets.assign(ticket, employee)
        ticket.thread_id = 777
        ticket.pinned_message_id = 555
        await session.commit()
        target_id, stranger_id, inactive_id = target.id, stranger.id, inactive.id

    client = TestClient(TestServer(create_miniapp(config, db, bot, topics)))
    await client.start_server()
    owner = {"X-Dev-User-ID": str(employee.telegram_id)}
    other = {"X-Dev-User-ID": str(stranger.telegram_id)}
    try:
        response = await client.get("/api/bootstrap", headers=owner)
        assert response.status == 200
        assert (await response.json())["tickets"][0]["reminder"] == {
            "active": False,
            "next_run_at": None,
        }

        response = await client.put("/api/tickets/1/reminder", headers=owner, json={"minutes": 15})
        assert response.status == 200
        reminder = (await response.json())["ticket"]["reminder"]
        assert reminder["active"] is True
        assert datetime.fromisoformat(reminder["next_run_at"]) > datetime.now(UTC)

        for minutes in (0, 10081, True, "bad"):
            response = await client.put("/api/tickets/1/reminder", headers=owner, json={"minutes": minutes})
            assert response.status == 400

        response = await client.put("/api/tickets/1/reminder", headers=other, json={"minutes": 15})
        assert response.status == 400
        response = await client.post("/api/tickets/1/pin", headers=owner)
        assert response.status == 200
        assert bot.called("edit_message_text")

        response = await client.patch("/api/tickets/1", headers=owner, json={"employee_id": target_id})
        assert response.status == 200
        assert (await response.json())["ticket"]["employee_id"] == target_id

        response = await client.post("/api/tickets/1/pin", headers=owner)
        assert response.status == 400
        response = await client.patch("/api/tickets/1", json={"employee_id": target_id})
        assert response.status == 400
        response = await client.patch("/api/tickets/1", json={"employee_id": inactive_id})
        assert response.status == 400
        response = await client.patch("/api/tickets/1", json={"employee_id": employee.id})
        assert response.status == 200
        assert (await response.json())["ticket"]["employee_id"] == employee.id

        response = await client.delete("/api/tickets/1/reminder", headers=owner)
        assert response.status == 200
        reminder = (await response.json())["ticket"]["reminder"]
        assert reminder["active"] is False and reminder["next_run_at"] is not None
    finally:
        await client.close()


async def test_profile_reviews_greeting_and_telegram_validation(config, db, employee) -> None:
    bot = FakeBot()
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        admin = await employees.ensure_admin()
        current_employee = await employees.get_by_id(employee.id)
        await employees.set_schedule(current_employee, [(0, 540, 1080)])
        tickets = TicketService(session, config.admin_id)
        for number in range(1, 22):
            ticket = await tickets.create(
                user_id=3000 + number,
                username=None,
                display_name="Клиент",
                category="Другое",
                description="Отзыв",
            )
            await tickets.assign(ticket, employee)
            await tickets.add_review(ticket, 5, f"Отзыв {number}")
        admin_ticket = await tickets.create(
            user_id=4000,
            username=None,
            display_name="Клиент",
            category="Другое",
            description="Отзыв администратора",
        )
        await tickets.assign(admin_ticket, admin)
        await tickets.add_review(admin_ticket, 4, "Администратору")

    client = TestClient(TestServer(create_miniapp(config, db, bot, TopicService(bot, config))))
    await client.start_server()
    owner = {"X-Dev-User-ID": str(employee.telegram_id)}
    try:
        response = await client.get("/api/profile", headers=owner)
        assert response.status == 200
        profile = await response.json()
        assert profile == {
            "name": "Оператор",
            "nickname": "Оператор",
            "gender": "f",
            "schedule": "Пн 09:00-18:00",
            "greeting": "Здравствуйте! Меня зовут Оператор. Сейчас разберёмся с вашим вопросом.",
            "ratings_visible": False,
            "reviews": [],
        }

        async with db.sessions() as session:
            await SettingsService(session, config.defaults).set("employee_ratings_visible", True)
        response = await client.get("/api/profile", headers=owner)
        profile = await response.json()
        assert profile["ratings_visible"] is True
        assert len(profile["reviews"]) == 20
        assert profile["reviews"][0]["ticket_number"] == 21
        assert profile["reviews"][-1]["ticket_number"] == 2
        assert set(profile["reviews"][0]) == {"ticket_number", "rating", "comment", "created_at"}

        response = await client.patch("/api/profile", headers=owner, json={"greeting": "Привет, {name}!"})
        assert response.status == 200
        assert (await response.json())["greeting"] == "Привет, {name}!"
        for greeting in ("{other}", "x" * 2001, None):
            response = await client.patch("/api/profile", headers=owner, json={"greeting": greeting})
            assert response.status == 400

        response = await client.get("/api/profile")
        profile = await response.json()
        assert profile["ratings_visible"] is True
        assert [review["ticket_number"] for review in profile["reviews"]] == [22]
    finally:
        await client.close()

    invalid_message = FakeMessage(employee.telegram_id, "{unknown}")
    invalid_state = FakeState(state="GreetingForm:text")
    await save_greeting(invalid_message, invalid_state, config, db)
    assert invalid_state.clear_count == 0
    assert "только параметр {name}" in invalid_message.answers[-1]["text"]

    message = FakeMessage(employee.telegram_id, "Здравствуйте, {name}!")
    state = FakeState(state="GreetingForm:text")
    await save_greeting(message, state, config, db)
    assert state.clear_count == 1
    async with db.sessions() as session:
        saved = await EmployeeService(session, config.admin_id).get(employee.telegram_id)
        assert saved.greeting == "Здравствуйте, {name}!"


@pytest.mark.parametrize("miniapp_url", [None, "https://support.example/app?mode=compact&ticket=old#chat"])
async def test_private_reminder_html_preview_and_open_url(config, db, employee, miniapp_url) -> None:
    config = replace(config, miniapp_url=miniapp_url)
    bot = FakeBot()
    jobs = BackgroundJobs(config, db, TopicService(bot, config))
    waiting_from = datetime(2026, 11, 10, 12, 4)
    due = utcnow() - timedelta(minutes=1)
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        first = await tickets.create(
            user_id=2001,
            username="client",
            display_name="Клиент",
            category="Оплата & <доступ>",
            description="Первое обращение",
        )
        await tickets.assign(first, employee)
        first.last_user_message_at = waiting_from
        await tickets.save_message_link(first, 2001, 10, config.support_group_id, 20, body="<жду> & " + "x" * 310)
        await tickets.set_reminder(first, employee, 1)
        second = await tickets.create(
            user_id=2002,
            username="client2",
            display_name="Клиент 2",
            category="Другое",
            description="Второе обращение",
        )
        await tickets.assign(second, employee)
        second.thread_id = 888
        second.last_user_message_at = waiting_from
        await tickets.set_reminder(second, employee, 1)
        await session.commit()
        for reminder in (await session.execute(select(Reminder))).scalars():
            reminder.next_run_at = due
        await session.commit()

    await jobs.process_reminders()

    calls = bot.called("send_message")
    assert len(calls) == 2
    assert [args[0] for args, _ in calls] == [employee.telegram_id, employee.telegram_id]
    assert all("message_thread_id" not in kwargs for _, kwargs in calls)
    text = calls[0][0][1]
    assert "Напоминание по обращению №1" in text
    assert "Клиент ждёт с 15:04" in text
    assert "Оплата &amp; &lt;доступ&gt;" in text
    assert "&lt;жду&gt; &amp;" in text and "<жду>" not in text
    assert text.endswith("…")
    first_buttons = buttons(calls[0][1]["reply_markup"])
    assert {button.callback_data for button in first_buttons if button.callback_data} == {"reminder_transfer:1", "reminder_disable:1"}
    second_buttons = buttons(calls[1][1]["reply_markup"])
    if miniapp_url:
        assert first_buttons[0].web_app.url == "https://support.example/app?mode=compact&ticket=1#chat"
        assert second_buttons[0].web_app.url == "https://support.example/app?mode=compact&ticket=2#chat"
    else:
        assert second_buttons[0].url == "https://t.me/c/500/888"
    async with db.sessions() as session:
        reminders = list((await session.execute(select(Reminder).order_by(Reminder.ticket_id))).scalars())
        assert all(reminder.next_run_at > due for reminder in reminders)


async def test_reminder_callbacks_acl_transfer_disable_and_stale(config, db, employee, ticket_number) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        await employees.ensure_admin()
        target = await employees.create(1002, "Смена", "m", "Смена")
        stranger = await employees.create(1003, "Чужой", "m", "Чужой")
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.get(ticket_number)
        await tickets.set_reminder(ticket, employee, 30)
        target_id = target.id

    denied = FakeCallback(stranger.telegram_id, f"reminder_disable:{ticket_number}", bot)
    await reminder_disable(denied, config, db)
    assert denied.answers[-1][1] is True
    async with db.sessions() as session:
        ticket = await TicketService(session, config.admin_id).get(ticket_number)
        assert ticket.reminder.active is True

    disabled = FakeCallback(employee.telegram_id, f"reminder_disable:{ticket_number}", bot)
    await reminder_disable(disabled, config, db)
    assert "отключено" in disabled.message.text
    async with db.sessions() as session:
        ticket = await TicketService(session, config.admin_id).get(ticket_number)
        assert ticket.reminder.active is False
        await TicketService(session, config.admin_id).set_reminder(ticket, employee, 30)

    select_target = FakeCallback(employee.telegram_id, f"reminder_transfer:{ticket_number}", bot)
    await reminder_transfer(select_target, config, db)
    choices = buttons(select_target.message.edited_kwargs["reply_markup"])
    assert f"reminder_target:{ticket_number}:{target_id}" in {button.callback_data for button in choices}
    assert f"reminder_target:{ticket_number}:{employee.id}" not in {button.callback_data for button in choices}

    confirm_target = FakeCallback(employee.telegram_id, f"reminder_target:{ticket_number}:{target_id}", bot)
    await reminder_transfer_target(confirm_target, config, db)
    confirmation = buttons(confirm_target.message.edited_kwargs["reply_markup"])
    assert confirmation[0].callback_data == f"reminder_confirm:{ticket_number}:{target_id}"

    confirmed = FakeCallback(employee.telegram_id, f"reminder_confirm:{ticket_number}:{target_id}", bot)
    await reminder_transfer_confirm(confirmed, config, db, topics)
    assert "передано" in confirmed.message.text
    async with db.sessions() as session:
        ticket = await TicketService(session, config.admin_id).get(ticket_number)
        assert ticket.employee_id == target_id

    stale = FakeCallback(employee.telegram_id, f"reminder_disable:{ticket_number}", bot)
    await reminder_disable(stale, config, db)
    assert stale.answers[-1][1] is True
    same_target = FakeCallback(target.telegram_id, f"reminder_confirm:{ticket_number}:{target_id}", bot)
    await reminder_transfer_confirm(same_target, config, db, topics)
    assert same_target.answers[-1][1] is True
