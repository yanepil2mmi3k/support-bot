from __future__ import annotations

import pytest

from support_bot.handlers import admin
from support_bot.services.employees import DAY_NAMES, EmployeeError, EmployeeService, parse_schedule


def test_parse_schedule_supports_ranges_separate_days_and_overnight() -> None:
    assert admin.parse_schedule is parse_schedule
    assert DAY_NAMES == {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
    assert parse_schedule("Пн-Пт 09:00-18:00") == [(day, 540, 1080) for day in range(5)]
    assert parse_schedule("Пн 09:00-18:00; Ср 10:00-16:00\nПт 11:00-15:00") == [
        (0, 540, 1080),
        (2, 600, 960),
        (4, 660, 900),
    ]
    assert parse_schedule("Пт-Сб 22:00-06:00") == [(4, 1320, 360), (5, 1320, 360)]
    assert parse_schedule("Сб-Вс 00:00-00:00") == [(5, 0, 0), (6, 0, 0)]
    assert parse_schedule("-") == []


@pytest.mark.parametrize("text", ["Пн 24:00-18:00", "Пн 09:00", "Пн 09:00-18:00, Вт 09:00-18:00"])
def test_parse_schedule_rejects_invalid_input(text: str) -> None:
    with pytest.raises(ValueError):
        parse_schedule(text)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("telegram_id", 0),
        ("telegram_id", -1),
        ("telegram_id", True),
        ("telegram_id", "1001"),
        ("telegram_id", 1.5),
        ("name", ""),
        ("name", "   "),
        ("name", "а" * 101),
        ("name", None),
        ("nickname", ""),
        ("nickname", "   "),
        ("nickname", "два слова"),
        ("nickname", "два\tслова"),
        ("nickname", "н" * 25),
        ("nickname", None),
        ("gender", "x"),
        ("gender", "M"),
        ("gender", None),
    ],
)
async def test_create_validates_fields(db, config, field: str, value) -> None:
    values = {"telegram_id": 1001, "name": "Оператор", "gender": "f", "nickname": "Оператор"}
    values[field] = value
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        with pytest.raises(EmployeeError):
            await employees.create(**values)
        assert await employees.all(include_inactive=True) == []


async def test_create_trims_name_and_nickname(db, config) -> None:
    async with db.sessions() as session:
        employee = await EmployeeService(session, config.admin_id).create(1001, "  Оператор  ", "f", "  Ник  ")
        assert employee.name == "Оператор"
        assert employee.nickname == "Ник"
        assert employee.active is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("telegram_id", 0),
        ("telegram_id", False),
        ("telegram_id", "1002"),
        ("name", " "),
        ("name", "а" * 101),
        ("nickname", "два слова"),
        ("nickname", "н" * 25),
        ("gender", "x"),
        ("active", 1),
        ("active", "true"),
        ("active", None),
    ],
)
async def test_update_validates_fields_without_changing_employee(db, config, field: str, value) -> None:
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        employee = await employees.create(1001, "Оператор", "f", "Оператор")
        with pytest.raises(EmployeeError):
            await employees.update(employee, **{field: value})
        assert employee.telegram_id == 1001
        assert employee.name == "Оператор"
        assert employee.nickname == "Оператор"
        assert employee.gender == "f"
        assert employee.active is True


async def test_update_normalizes_fields_and_allows_unchanged_unique_values(db, config) -> None:
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        employee = await employees.create(1001, "Оператор", "f", "Оператор")
        await employees.update(
            employee,
            telegram_id=1002,
            name="  Новое имя  ",
            nickname="  НовыйНик  ",
            gender="m",
            active=False,
        )
        assert employee.telegram_id == 1002
        assert employee.name == "Новое имя"
        assert employee.nickname == "НовыйНик"
        assert employee.gender == "m"
        assert employee.active is False
        await employees.update(employee, telegram_id=1002, nickname="НовыйНик")


async def test_create_and_update_enforce_unique_telegram_id_and_nickname(db, config) -> None:
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        first = await employees.create(1001, "Первый", "f", "Первый")
        second = await employees.create(1002, "Второй", "m", "Второй")
        with pytest.raises(EmployeeError, match="Telegram ID"):
            await employees.create(1001, "Третий", "m", "Третий")
        with pytest.raises(EmployeeError, match="никнейм"):
            await employees.create(1003, "Третий", "m", "Первый")
        with pytest.raises(EmployeeError, match="Telegram ID"):
            await employees.update(second, telegram_id=1001)
        with pytest.raises(EmployeeError, match="никнейм"):
            await employees.update(second, nickname="Первый")
        assert second.telegram_id == 1002
        assert second.nickname == "Второй"
        assert first.telegram_id == 1001


async def test_create_preserves_five_non_admin_limit(db, config) -> None:
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        await employees.ensure_admin()
        for index in range(5):
            await employees.create(1001 + index, f"Сотрудник {index}", "m", f"Ник{index}")
        with pytest.raises(EmployeeError, match="не более 5"):
            await employees.create(2000, "Лишний", "m", "Лишний")
        assert len(await employees.all(include_inactive=True)) == 6
