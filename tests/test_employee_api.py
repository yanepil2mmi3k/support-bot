from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer
from conftest import FakeBot

from support_bot.miniapp import create_miniapp
from support_bot.services.employees import EmployeeService
from support_bot.services.topics import TopicService


EMPLOYEE_FIELDS = {"id", "telegram_id", "name", "nickname", "gender", "greeting", "active", "schedule"}


async def test_admin_employee_api_lifecycle_and_bootstrap_filter(config, db) -> None:
    bot = FakeBot()
    async with db.sessions() as session:
        admin = await EmployeeService(session, config.admin_id).ensure_admin()
        admin_id = admin.id
    client = TestClient(TestServer(create_miniapp(config, db, bot, TopicService(bot, config))))
    await client.start_server()
    try:
        response = await client.get("/api/employees")
        assert response.status == 200
        employees = (await response.json())["employees"]
        assert len(employees) == 1
        assert employees[0]["id"] == admin_id
        assert set(employees[0]) == EMPLOYEE_FIELDS
        assert employees[0]["schedule"] == "Не задан"

        response = await client.post(
            "/api/employees",
            json={"telegram_id": 1001, "name": "  Оператор  ", "nickname": "  Оператор  ", "gender": "f"},
        )
        assert response.status == 201
        created = (await response.json())["employee"]
        assert set(created) == EMPLOYEE_FIELDS
        assert created == {
            "id": created["id"],
            "telegram_id": 1001,
            "name": "Оператор",
            "nickname": "Оператор",
            "gender": "f",
            "greeting": "",
            "active": True,
            "schedule": "Не задан",
        }
        employee_id = created["id"]

        response = await client.get("/api/employees")
        assert response.status == 200
        assert [item["id"] for item in (await response.json())["employees"]] == [admin_id, employee_id]

        response = await client.patch(
            f"/api/employees/{employee_id}",
            json={
                "name": "  Новое имя  ",
                "nickname": "  НовыйНик  ",
                "gender": "m",
                "greeting": "Привет, {name}!",
                "schedule": "Пн-Пт 09:00-18:00; Сб 22:00-06:00",
            },
        )
        assert response.status == 200
        updated = (await response.json())["employee"]
        assert updated["name"] == "Новое имя"
        assert updated["nickname"] == "НовыйНик"
        assert updated["gender"] == "m"
        assert updated["greeting"] == "Привет, {name}!"
        assert updated["schedule"] == (
            "Пн 09:00-18:00; Вт 09:00-18:00; Ср 09:00-18:00; "
            "Чт 09:00-18:00; Пт 09:00-18:00; Сб 22:00-06:00"
        )

        response = await client.patch(f"/api/employees/{employee_id}", json={"schedule": "-"})
        assert response.status == 200
        assert (await response.json())["employee"]["schedule"] == "Не задан"

        response = await client.patch(f"/api/employees/{employee_id}", json={"active": False})
        assert response.status == 200
        assert (await response.json())["employee"]["active"] is False
        response = await client.get("/api/employees")
        assert response.status == 200
        assert employee_id in {item["id"] for item in (await response.json())["employees"]}
        response = await client.get("/api/bootstrap")
        assert response.status == 200
        assert employee_id not in {item["id"] for item in (await response.json())["employees"]}

        response = await client.patch(f"/api/employees/{employee_id}", json={"active": True})
        assert response.status == 200
        assert (await response.json())["employee"]["active"] is True
        response = await client.get("/api/bootstrap")
        assert employee_id in {item["id"] for item in (await response.json())["employees"]}

        response = await client.patch(f"/api/employees/{admin_id}", json={"active": False})
        assert response.status == 400
        response = await client.get("/api/employees")
        admin_data = next(item for item in (await response.json())["employees"] if item["id"] == admin_id)
        assert admin_data["active"] is True
    finally:
        await client.close()


async def test_employee_api_forbids_non_admin(config, db) -> None:
    bot = FakeBot()
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        await employees.ensure_admin()
        actor = await employees.create(1001, "Сотрудник", "f", "Сотрудник")
        target = await employees.create(1002, "Цель", "m", "Цель")
        target_id = target.id
    client = TestClient(TestServer(create_miniapp(config, db, bot, TopicService(bot, config))))
    await client.start_server()
    non_admin = {"X-Dev-User-ID": str(actor.telegram_id)}
    try:
        response = await client.get("/api/employees", headers=non_admin)
        assert response.status == 400
        response = await client.post(
            "/api/employees",
            headers=non_admin,
            json={"telegram_id": 1003, "name": "Новый", "nickname": "Новый", "gender": "m"},
        )
        assert response.status == 400
        response = await client.patch(f"/api/employees/{target_id}", headers=non_admin, json={"name": "Изменено"})
        assert response.status == 400
    finally:
        await client.close()
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        assert await employees.get(1003) is None
        target = await employees.get_by_id(target_id)
        assert target.name == "Цель"


async def test_employee_api_rejects_invalid_payloads(config, db) -> None:
    bot = FakeBot()
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        await employees.ensure_admin()
        target = await employees.create(1001, "Оператор", "f", "Оператор")
        target_id = target.id
    client = TestClient(TestServer(create_miniapp(config, db, bot, TopicService(bot, config))))
    await client.start_server()
    try:
        invalid_creates = [
            {},
            {"telegram_id": 0, "name": "Имя", "nickname": "Ник", "gender": "m"},
            {"telegram_id": "1002", "name": "Имя", "nickname": "Ник", "gender": "m"},
            {"telegram_id": 1002, "name": " ", "nickname": "Ник", "gender": "m"},
            {"telegram_id": 1002, "name": "Имя", "nickname": "два слова", "gender": "m"},
            {"telegram_id": 1002, "name": "Имя", "nickname": "Ник", "gender": "x"},
            {"telegram_id": 1001, "name": "Имя", "nickname": "Другой", "gender": "m"},
            {"telegram_id": 1002, "name": "Имя", "nickname": "Оператор", "gender": "m"},
        ]
        for payload in invalid_creates:
            response = await client.post("/api/employees", json=payload)
            assert response.status == 400, payload

        invalid_patches = [
            {},
            {"unknown": "value"},
            {"telegram_id": 1002},
            {"name": " "},
            {"nickname": "два слова"},
            {"gender": "x"},
            {"greeting": None},
            {"greeting": "{unknown}"},
            {"schedule": None},
            {"schedule": "Пн 25:00-26:00"},
            {"active": "false"},
        ]
        for payload in invalid_patches:
            response = await client.patch(f"/api/employees/{target_id}", json=payload)
            assert response.status == 400, payload

        response = await client.patch("/api/employees/999999", json={"name": "Имя"})
        assert response.status == 404
    finally:
        await client.close()
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        assert len(await employees.all(include_inactive=True)) == 2
        target = await employees.get_by_id(target_id)
        assert target.telegram_id == 1001
        assert target.name == "Оператор"
        assert target.nickname == "Оператор"
        assert target.gender == "f"
        assert target.greeting is None
        assert target.active is True
        assert target.shifts == []
