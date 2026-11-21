from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer
from conftest import FakeBot

from support_bot.miniapp import create_miniapp
from support_bot.services.employees import EmployeeService
from support_bot.services.tickets import TicketService
from support_bot.services.topics import TopicService


async def test_miniapp_ticket_message_lifecycle(config, db, employee) -> None:
    bot = FakeBot()
    topics = TopicService(bot, config)
    async with db.sessions() as session:
        await EmployeeService(session, config.admin_id).ensure_admin()
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.create(
            user_id=2001,
            username="client",
            display_name="Клиент",
            category="Другое",
            description="Проверка Mini App",
        )
        await tickets.assign(ticket, employee)
        ticket.thread_id = 777
        await session.commit()
    client = TestClient(TestServer(create_miniapp(config, db, bot, topics)))
    await client.start_server()
    try:
        response = await client.get("/api/bootstrap")
        assert response.status == 200
        payload = await response.json()
        assert payload["employee"]["admin"] is True
        assert payload["tickets"][0]["number"] == 1

        response = await client.post("/api/tickets/1/messages", json={"text": "Ответ из Mini App"})
        assert response.status == 201
        created = (await response.json())["message"]
        assert created["from_staff"] is True and created["body"] == "Ответ из Mini App"
        sent = bot.called("send_message")
        assert sent[0][0] == (2001, "Ответ из Mini App")
        assert sent[1][0] == (-100500, "Ответ из Mini App")

        response = await client.get("/api/tickets/1/messages")
        history = (await response.json())["messages"]
        assert len(history) == 1 and history[0]["id"] == created["id"]

        response = await client.patch(f"/api/messages/{created['id']}", json={"text": "Исправленный ответ"})
        assert response.status == 200
        assert len(bot.called("edit_message_text")) == 2

        response = await client.delete(f"/api/messages/{created['id']}")
        assert response.status == 200
        assert len(bot.called("delete_message")) == 2
        response = await client.get("/api/tickets/1/messages")
        assert (await response.json())["messages"] == []
    finally:
        await client.close()


async def test_miniapp_rejects_inactive_or_unknown_user(config, db) -> None:
    bot = FakeBot()
    client = TestClient(TestServer(create_miniapp(config, db, bot, TopicService(bot, config))))
    await client.start_server()
    try:
        response = await client.get("/api/bootstrap", headers={"X-Dev-User-ID": "404"})
        assert response.status == 403
    finally:
        await client.close()


async def test_miniapp_claim_greets_only_after_new_claim(config, db, employee) -> None:
    bot = FakeBot()
    async with db.sessions() as session:
        await EmployeeService(session, config.admin_id).ensure_admin()
        ticket = await TicketService(session, config.admin_id).create(
            user_id=2001,
            username="client",
            display_name="Клиент",
            category="Другое",
            description="Проверка приветствия",
        )
        number = ticket.number
    client = TestClient(TestServer(create_miniapp(config, db, bot, TopicService(bot, config))))
    await client.start_server()
    try:
        response = await client.post(f"/api/tickets/{number}/claim", headers={"X-Dev-User-ID": "1001"})
        assert response.status == 200
        private_messages = [call for call in bot.called("send_message") if call[0][0] == 2001]
        assert len(private_messages) == 2
        assert private_messages[0][0][1] == config.message("ticket_accepted", number=number)
        assert private_messages[1][0][1] == config.message("default_greeting", name=employee.name).replace("ознакомился", "ознакомилась")

        response = await client.post(f"/api/tickets/{number}/claim", headers={"X-Dev-User-ID": "1001"})
        assert response.status == 200
        private_messages = [call for call in bot.called("send_message") if call[0][0] == 2001]
        assert len(private_messages) == 3
    finally:
        await client.close()


async def test_miniapp_unread_counts_are_per_employee(config, db, employee) -> None:
    bot = FakeBot()
    async with db.sessions() as session:
        await EmployeeService(session, config.admin_id).ensure_admin()
        tickets = TicketService(session, config.admin_id)
        assigned = await tickets.create(
            user_id=2001, username="client", display_name="Клиент", category="Другое", description="Первое"
        )
        await tickets.assign(assigned, employee)
        await tickets.save_message_link(assigned, 2001, 10, config.support_group_id, 20, body="Входящее 1")
        await tickets.save_message_link(assigned, 2001, 11, config.support_group_id, 21, body="Входящее 2")
        await tickets.save_message_link(
            assigned, config.support_group_id, 30, 2001, 31, body="Ответ", from_staff=True
        )
        new = await tickets.create(
            user_id=2002, username="new", display_name="Новый", category="Другое", description="Новое"
        )
        await tickets.save_message_link(new, 2002, 12, config.support_group_id, 22, body="Дополнение")
        archived = await tickets.create(
            user_id=2003, username="archive", display_name="Архив", category="Другое", description="Архивное"
        )
        await tickets.assign(archived, employee)
        await tickets.set_status(archived, "archive", {"key": "archive", "closed": True, "archive": True})
        await tickets.save_message_link(archived, 2003, 13, config.support_group_id, 23, body="После закрытия")
        assigned_number = assigned.number
        new_number = new.number
        archived_number = archived.number

    client = TestClient(TestServer(create_miniapp(config, db, bot, TopicService(bot, config))))
    await client.start_server()
    try:
        response = await client.get("/api/bootstrap")
        tickets_by_number = {item["number"]: item for item in (await response.json())["tickets"]}
        assert tickets_by_number[assigned_number]["unread_count"] == 3
        assert tickets_by_number[new_number]["unread_count"] == 2
        assert tickets_by_number[archived_number]["unread_count"] == 2

        response = await client.get(f"/api/tickets/{assigned_number}/messages")
        assert response.status == 200
        assert (await response.json())["ticket"]["unread_count"] == 0
        response = await client.get("/api/bootstrap", headers={"X-Dev-User-ID": "1001"})
        tickets_by_number = {item["number"]: item for item in (await response.json())["tickets"]}
        assert tickets_by_number[assigned_number]["unread_count"] == 3

        response = await client.post(f"/api/tickets/{new_number}/read", headers={"X-Dev-User-ID": "1001"})
        assert response.status == 200
        assert (await response.json())["ticket"]["unread_count"] == 1

        async with db.sessions() as session:
            tickets = TicketService(session, config.admin_id)
            assigned = await tickets.get(assigned_number)
            await tickets.save_message_link(assigned, 2001, 14, config.support_group_id, 24, body="Новое")
            await tickets.save_message_link(
                assigned, config.support_group_id, 32, 2001, 33, body="Ответ", from_staff=True
            )
        response = await client.get("/api/bootstrap")
        tickets_by_number = {item["number"]: item for item in (await response.json())["tickets"]}
        assert tickets_by_number[assigned_number]["unread_count"] == 1
        assert tickets_by_number[new_number]["unread_count"] == 2

        response = await client.get(f"/api/tickets/{assigned_number}/messages", headers={"X-Dev-User-ID": "1001"})
        assert response.status == 200
        response = await client.get("/api/bootstrap", headers={"X-Dev-User-ID": "1001"})
        tickets_by_number = {item["number"]: item for item in (await response.json())["tickets"]}
        assert tickets_by_number[assigned_number]["unread_count"] == 0
        response = await client.get("/api/bootstrap")
        tickets_by_number = {item["number"]: item for item in (await response.json())["tickets"]}
        assert tickets_by_number[assigned_number]["unread_count"] == 1
    finally:
        await client.close()


async def test_miniapp_read_rejects_ticket_assigned_to_another_employee(config, db, employee) -> None:
    bot = FakeBot()
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        other = await employees.create(1002, "Другой", "m", "Другой")
        ticket = await TicketService(session, config.admin_id).create(
            user_id=2001, username="client", display_name="Клиент", category="Другое", description="Чужое"
        )
        await TicketService(session, config.admin_id).assign(ticket, other)
        number = ticket.number
    client = TestClient(TestServer(create_miniapp(config, db, bot, TopicService(bot, config))))
    await client.start_server()
    try:
        response = await client.post(f"/api/tickets/{number}/read", headers={"X-Dev-User-ID": "1001"})
        assert response.status == 400
        response = await client.post(f"/api/tickets/{number}/read", headers={"X-Dev-User-ID": "1002"})
        assert response.status == 200
        assert (await response.json())["ticket"]["unread_count"] == 0
    finally:
        await client.close()
