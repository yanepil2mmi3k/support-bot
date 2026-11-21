from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from support_bot.db.models import Employee, Shift


DAY_NAMES = {
    "пн": 0,
    "вт": 1,
    "ср": 2,
    "чт": 3,
    "пт": 4,
    "сб": 5,
    "вс": 6,
}


def parse_schedule(text: str) -> list[tuple[int, int, int]]:
    if text.strip() == "-":
        return []
    result: list[tuple[int, int, int]] = []
    for part in re.split(r"[;\n]+", text):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(
            r"(Пн|Вт|Ср|Чт|Пт|Сб|Вс)(?:-(Пн|Вт|Ср|Чт|Пт|Сб|Вс))?\s+(\d{2}):(\d{2})-(\d{2}):(\d{2})",
            part,
            flags=re.IGNORECASE,
        )
        if not match:
            raise ValueError(f"Неверный формат: {part}")
        first, last, h1, m1, h2, m2 = match.groups()
        start_day = DAY_NAMES[first.lower()]
        end_day = DAY_NAMES[last.lower()] if last else start_day
        start, end = int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)
        if start > 1439 or end > 1439:
            raise ValueError("Время должно быть от 00:00 до 23:59")
        day = start_day
        while True:
            result.append((day, start, end))
            if day == end_day:
                break
            day = (day + 1) % 7
    return result


class EmployeeError(ValueError):
    pass


def _validate_telegram_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise EmployeeError("Telegram ID должен быть положительным целым числом")
    return value


def _validate_name(value: object) -> str:
    if not isinstance(value, str):
        raise EmployeeError("Имя должно содержать от 1 до 100 символов")
    name = value.strip()
    if not 1 <= len(name) <= 100:
        raise EmployeeError("Имя должно содержать от 1 до 100 символов")
    return name


def _validate_nickname(value: object) -> str:
    if not isinstance(value, str):
        raise EmployeeError("Никнейм должен быть одним словом не длиннее 24 символов")
    nickname = value.strip()
    if not nickname or any(char.isspace() for char in nickname):
        raise EmployeeError("Никнейм должен быть одним словом")
    if len(nickname) > 24:
        raise EmployeeError("Никнейм должен быть не длиннее 24 символов")
    return nickname


def _validate_gender(value: object) -> str:
    if not isinstance(value, str) or value not in {"m", "f"}:
        raise EmployeeError("Пол должен быть m или f")
    return value


def _validate_active(value: object) -> bool:
    if not isinstance(value, bool):
        raise EmployeeError("Активность должна быть булевым значением")
    return value


def _validate_values(values: dict[str, object]) -> dict[str, object]:
    validators = {
        "telegram_id": _validate_telegram_id,
        "name": _validate_name,
        "gender": _validate_gender,
        "nickname": _validate_nickname,
        "active": _validate_active,
    }
    return {key: validators[key](value) for key, value in values.items() if key in validators}


class EmployeeService:
    def __init__(self, session: AsyncSession, admin_id: int) -> None:
        self.session = session
        self.admin_id = admin_id

    async def ensure_admin(self) -> Employee:
        employee = await self.get(self.admin_id)
        if employee is None:
            employee = Employee(
                telegram_id=self.admin_id,
                name="Администратор",
                gender="m",
                nickname="Админ",
                active=True,
            )
            self.session.add(employee)
            await self.session.commit()
        elif not employee.active:
            employee.active = True
            await self.session.commit()
        return employee

    async def get(self, telegram_id: int) -> Employee | None:
        return await self.session.scalar(
            select(Employee).options(selectinload(Employee.shifts)).where(Employee.telegram_id == telegram_id)
        )

    async def get_by_id(self, employee_id: int) -> Employee | None:
        return await self.session.scalar(
            select(Employee).options(selectinload(Employee.shifts)).where(Employee.id == employee_id)
        )

    async def by_nickname(self, nickname: str) -> Employee | None:
        return await self.session.scalar(select(Employee).where(Employee.nickname == nickname.strip()))

    async def all(self, include_inactive: bool = False) -> list[Employee]:
        query = select(Employee).options(selectinload(Employee.shifts)).order_by(Employee.id)
        if not include_inactive:
            query = query.where(Employee.active.is_(True))
        return list((await self.session.execute(query)).scalars())

    async def create(self, telegram_id: int, name: str, gender: str, nickname: str) -> Employee:
        values = _validate_values(
            {"telegram_id": telegram_id, "name": name, "gender": gender, "nickname": nickname}
        )
        if await self.get(values["telegram_id"]):
            raise EmployeeError("Сотрудник с таким Telegram ID уже существует")
        if await self.by_nickname(values["nickname"]):
            raise EmployeeError("Этот никнейм уже занят")
        count = await self.session.scalar(
            select(func.count(Employee.id)).where(Employee.telegram_id != self.admin_id)
        )
        if (count or 0) >= 5:
            raise EmployeeError("В демо доступно не более 5 сотрудников, не считая администратора")
        employee = Employee(**values, active=True)
        self.session.add(employee)
        await self.session.commit()
        return employee

    async def update(self, employee: Employee, **values: str | bool | int) -> Employee:
        validated = _validate_values(values)
        telegram_id = validated.get("telegram_id")
        if telegram_id is not None:
            existing = await self.get(telegram_id)
            if existing and existing.id != employee.id:
                raise EmployeeError("Сотрудник с таким Telegram ID уже существует")
        nickname = validated.get("nickname")
        if nickname is not None:
            existing = await self.by_nickname(nickname)
            if existing and existing.id != employee.id:
                raise EmployeeError("Этот никнейм уже занят")
        for key, value in validated.items():
            setattr(employee, key, value)
        if "greeting" in values:
            employee.greeting = values["greeting"]
        await self.session.commit()
        return employee

    async def update_greeting(self, employee: Employee, greeting: str) -> Employee:
        if len(greeting) > 2000:
            raise EmployeeError("Приветствие должно быть не длиннее 2000 символов")
        try:
            greeting.format(name=employee.name)
        except (AttributeError, IndexError, KeyError, ValueError) as exc:
            raise EmployeeError("В тексте допустим только параметр {name}") from exc
        employee.greeting = greeting
        await self.session.commit()
        return employee

    async def set_schedule(self, employee: Employee, shifts: list[tuple[int, int, int]]) -> None:
        shifts = list(dict.fromkeys(shifts))
        await self.session.execute(delete(Shift).where(Shift.employee_id == employee.id))
        self.session.add_all(
            [Shift(employee_id=employee.id, weekday=day, start_minutes=start, end_minutes=end) for day, start, end in shifts]
        )
        await self.session.commit()
        await self.session.refresh(employee, ["shifts"])

    async def on_duty(self, at: datetime) -> list[Employee]:
        return [employee for employee in await self.all() if self.is_on_shift(employee, at)]

    @staticmethod
    def is_on_shift(employee: Employee, at: datetime) -> bool:
        minute = at.hour * 60 + at.minute
        today = at.weekday()
        yesterday = (at - timedelta(days=1)).weekday()
        for shift in employee.shifts:
            if shift.start_minutes == shift.end_minutes and shift.weekday == today:
                return True
            if shift.weekday == today:
                if shift.start_minutes < shift.end_minutes and shift.start_minutes <= minute < shift.end_minutes:
                    return True
                if shift.start_minutes > shift.end_minutes and minute >= shift.start_minutes:
                    return True
            if shift.weekday == yesterday and shift.start_minutes > shift.end_minutes and minute < shift.end_minutes:
                return True
        return False

    @staticmethod
    def format_schedule(employee: Employee) -> str:
        if not employee.shifts:
            return "Не задан"
        days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        ordered = sorted(employee.shifts, key=lambda item: (item.weekday, item.start_minutes))
        return "; ".join(
            f"{days[shift.weekday]} {shift.start_minutes // 60:02d}:{shift.start_minutes % 60:02d}-"
            f"{shift.end_minutes // 60:02d}:{shift.end_minutes % 60:02d}"
            for shift in ordered
        )
