from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from aiogram import F, Router
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from support_bot.config import Config
from support_bot.db.models import Review, Ticket
from support_bot.db.session import Database
from support_bot.services.employees import EmployeeError, EmployeeService, parse_schedule
from support_bot.services.reports import build_report, split_html
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import TicketError, TicketService
from support_bot.services.topics import TopicService, esc

router = Router(name="admin")


class AdminOnlyMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        config: Config = data["config"]
        if not user or user.id != config.admin_id:
            if isinstance(event, CallbackQuery):
                await event.answer("Только для администратора", show_alert=True)
            return None
        if isinstance(event, CallbackQuery) and not (event.data or "").startswith("staff_new_gender:"):
            state = data.get("state")
            if state is not None:
                await state.clear()
        return await handler(event, data)


router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


class AdminForm(StatesGroup):
    employee_telegram = State()
    employee_name = State()
    employee_gender = State()
    employee_nickname = State()
    employee_field = State()
    setting = State()
    category = State()
    status_add = State()
    status_edit = State()


SETTING_RULES = {
    "reminder_interval_minutes": (1, 10080, "минут"),
    "stale_threshold_hours": (1, 720, "часов"),
    "stale_summary_hours": (1, 168, "часов"),
    "archive_days": (1, 3650, "дней"),
}


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сотрудники", callback_data="admin:staff")],
            [InlineKeyboardButton(text="Настройки", callback_data="admin:settings")],
            [InlineKeyboardButton(text="Статусы", callback_data="admin:statuses")],
            [InlineKeyboardButton(text="Категории", callback_data="admin:categories")],
            [InlineKeyboardButton(text="Отзывы", callback_data="admin:reviews")],
            [InlineKeyboardButton(text="Сформировать отчёт", callback_data="admin:report")],
            [InlineKeyboardButton(text="Архив", callback_data="admin:archive")],
            [InlineKeyboardButton(text="Счётчик тикетов", callback_data="admin:counter")],
        ]
    )


async def edit_menu(callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in exc.message.lower():
            raise
    await callback.answer()


@router.message(Command("admin"))
async def admin_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("<b>Панель администратора</b>\n\nВыберите раздел:", reply_markup=main_keyboard())


async def admin_help(message: Message) -> None:
    await message.answer(
        "<b>Справка администратора</b>\n\n"
        "<b>Настройка</b>\n"
        "Выполните /setup в общем топике форум-группы. "
        "Через /admin добавьте сотрудников, задайте графики и выберите способ назначения.\n\n"
        "<b>Разделы панели</b>\n"
        "Сотрудники: профили, никнеймы, приветствия, графики и активность\n"
        "Настройки: назначение, напоминания, зависшие тикеты, архив и отчёты\n"
        "Статусы и категории: справочники для тикетов и формы клиента\n"
        "Отзывы: оценки и комментарии клиентов\n"
        "Отчёт: нагрузка, среднее время решения и оценки\n"
        "Архив: просмотр и удаление старых топиков с подтверждением\n\n"
        "<b>Команды</b>\n"
        "/setup: зарегистрировать системные топики\n"
        "/admin: открыть панель администратора\n"
        "/help: показать эту справку\n"
        "/report: отправить отчёт в «Админ-инфа»\n"
        "/reviews: показать отзывы\n"
        "/archive: открыть архив\n"
        "/profile: открыть профиль сотрудника\n"
        "/ticket: показать команды работы с тикетом\n"
        "/reset_counter N: установить счётчик, только в топике «Команды»\n\n"
        "Нельзя установить значение меньше уже использованных номеров. Это предотвращает повторную выдачу номеров."
    )


@router.callback_query(F.data == "admin:main")
async def admin_main(callback: CallbackQuery) -> None:
    await edit_menu(callback, "Администрирование", main_keyboard())


@router.callback_query(F.data == "admin:counter")
async def counter_info(callback: CallbackQuery, config: Config, db: Database) -> None:
    value = await db.counter_value()
    await edit_menu(
        callback,
        f"Текущий счётчик: {value}. Следующий номер: {value + 1}.\n\n"
        "В топике «Команды» используйте /reset_counter N. Значение меньше уже использованных номеров запрещено.",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="admin:main")]]),
    )


@router.message(Command("reset_counter"))
async def reset_counter(message: Message, command: CommandObject, config: Config, db: Database) -> None:
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        if message.message_thread_id != await settings.topic("commands"):
            await message.answer("Сброс доступен только в топике «Команды».")
            return
        try:
            target = int(command.args or "0")
            await TicketService(session, config.admin_id).reset_counter(target)
        except (ValueError, TicketError) as exc:
            await message.answer(str(exc) if isinstance(exc, TicketError) else "Укажите целое число: /reset_counter N")
            return
    await message.answer(f"Счётчик установлен: {target}. Следующий номер: {target + 1}.")


@router.callback_query(F.data == "admin:settings")
async def settings_menu(callback: CallbackQuery, config: Config, db: Database) -> None:
    async with db.sessions() as session:
        text = await settings_text(SettingsService(session, config.defaults))
    await edit_menu(callback, text, settings_keyboard())


async def settings_text(settings: SettingsService) -> str:
    mode = await settings.get("assignment_mode", "manual")
    return (
        "<b>Настройки</b>\n"
        f"Назначение: {'по графику' if mode == 'schedule' else 'ручной сбор'}\n"
        f"Период напоминаний: {await settings.get('reminder_interval_minutes', 60)} мин\n"
        f"Порог зависших: {await settings.get('stale_threshold_hours', 6)} ч\n"
        f"Период сводки: {await settings.get('stale_summary_hours', 8)} ч\n"
        f"Автоудаление архива: {'вкл' if await settings.get('archive_auto_delete', False) else 'выкл'}\n"
        f"Срок архива: {await settings.get('archive_days', 30)} дней\n"
        f"Автопредставление: {'вкл' if await settings.get('auto_greeting', True) else 'выкл'}\n"
        f"Оценки видны сотрудникам: {'вкл' if await settings.get('employee_ratings_visible', False) else 'выкл'}\n"
        f"Ежедневный отчёт: {await settings.get('report_time', '09:00')}"
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Переключить режим назначения", callback_data="setting:toggle:assignment_mode")],
            [InlineKeyboardButton(text="Период напоминаний", callback_data="setting:set:reminder_interval_minutes")],
            [InlineKeyboardButton(text="Порог зависших", callback_data="setting:set:stale_threshold_hours")],
            [InlineKeyboardButton(text="Период сводки", callback_data="setting:set:stale_summary_hours")],
            [InlineKeyboardButton(text="Вкл/выкл автоудаление", callback_data="setting:toggle:archive_auto_delete")],
            [InlineKeyboardButton(text="Срок автоудаления", callback_data="setting:set:archive_days")],
            [InlineKeyboardButton(text="Вкл/выкл автопредставление", callback_data="setting:toggle:auto_greeting")],
            [InlineKeyboardButton(text="Видимость оценок", callback_data="setting:toggle:employee_ratings_visible")],
            [InlineKeyboardButton(text="Время отчёта", callback_data="setting:set:report_time")],
            [InlineKeyboardButton(text="Назад", callback_data="admin:main")],
        ]
    )


@router.callback_query(F.data.startswith("setting:toggle:"))
async def toggle_setting(callback: CallbackQuery, config: Config, db: Database) -> None:
    key = callback.data.rsplit(":", 1)[1]
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        if key == "assignment_mode":
            current = await settings.get(key, "manual")
            await settings.set(key, "manual" if current == "schedule" else "schedule")
        elif key in {"archive_auto_delete", "auto_greeting", "employee_ratings_visible"}:
            await settings.set(key, not bool(await settings.get(key, False)))
        else:
            await callback.answer("Неизвестная настройка", show_alert=True)
            return
        text = await settings_text(settings)
    await edit_menu(callback, text, settings_keyboard())


@router.callback_query(F.data.startswith("setting:set:"))
async def ask_setting(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.rsplit(":", 1)[1]
    if key not in SETTING_RULES and key != "report_time":
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    await state.set_state(AdminForm.setting)
    await state.set_data({"setting_key": key})
    prompt = "Введите время в формате ЧЧ:ММ." if key == "report_time" else "Введите целое число."
    await callback.message.answer(prompt)
    await callback.answer()


@router.message(AdminForm.setting, F.text, ~F.text.startswith("/"))
async def save_setting(message: Message, state: FSMContext, config: Config, db: Database) -> None:
    key = (await state.get_data())["setting_key"]
    value: str | int = message.text.strip()
    if key == "report_time":
        try:
            datetime.strptime(str(value), "%H:%M")
        except ValueError:
            await message.answer("Нужен формат ЧЧ:ММ, например 09:00.")
            return
    else:
        minimum, maximum, unit = SETTING_RULES[key]
        try:
            value = int(str(value))
        except ValueError:
            await message.answer("Введите целое число.")
            return
        if not minimum <= value <= maximum:
            await message.answer(f"Допустимо от {minimum} до {maximum} {unit}.")
            return
    async with db.sessions() as session:
        await SettingsService(session, config.defaults).set(key, value)
    await state.clear()
    await message.answer("Настройка сохранена.")


@router.callback_query(F.data == "admin:staff")
async def staff_list(callback: CallbackQuery, config: Config, db: Database) -> None:
    async with db.sessions() as session:
        employees = await EmployeeService(session, config.admin_id).all(include_inactive=True)
    rows = [
        [InlineKeyboardButton(text=f"{employee.name}{' (неактивен)' if not employee.active else ''}", callback_data=f"staff:view:{employee.id}")]
        for employee in employees
    ]
    rows.append([InlineKeyboardButton(text="Добавить сотрудника", callback_data="staff:add")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="admin:main")])
    await edit_menu(callback, "Сотрудники", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "staff:add")
async def staff_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminForm.employee_telegram)
    await state.set_data({})
    await callback.message.answer("Введите Telegram ID сотрудника. Он должен сначала написать боту любое сообщение.")
    await callback.answer()


@router.message(AdminForm.employee_telegram, F.text, ~F.text.startswith("/"))
async def staff_add_id(message: Message, state: FSMContext) -> None:
    try:
        telegram_id = int(message.text)
        if telegram_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите положительный числовой Telegram ID.")
        return
    await state.update_data(telegram_id=telegram_id)
    await state.set_state(AdminForm.employee_name)
    await message.answer("Введите имя сотрудника.")


@router.message(AdminForm.employee_name, F.text, ~F.text.startswith("/"))
async def staff_add_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not 1 <= len(name) <= 100:
        await message.answer("Имя должно содержать от 1 до 100 символов.")
        return
    await state.update_data(name=name)
    await state.set_state(AdminForm.employee_gender)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Мужской", callback_data="staff_new_gender:m"),
                InlineKeyboardButton(text="Женский", callback_data="staff_new_gender:f"),
            ]
        ]
    )
    await message.answer("Выберите пол для корректных формулировок.", reply_markup=keyboard)


@router.callback_query(AdminForm.employee_gender, F.data.startswith("staff_new_gender:"))
async def staff_add_gender(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(gender=callback.data.rsplit(":", 1)[1])
    await state.set_state(AdminForm.employee_nickname)
    await callback.message.answer("Введите краткий никнейм для топиков, одним словом.")
    await callback.answer()


@router.message(AdminForm.employee_nickname, F.text, ~F.text.startswith("/"))
async def staff_add_nickname(message: Message, state: FSMContext, config: Config, db: Database) -> None:
    nickname = message.text.strip()
    if not nickname or any(char.isspace() for char in nickname):
        await message.answer("Никнейм должен быть одним словом.")
        return
    data = await state.get_data()
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        try:
            employee = await employees.create(data["telegram_id"], data["name"], data["gender"], nickname)
        except EmployeeError as exc:
            await message.answer(str(exc))
            return
    await state.clear()
    await message.answer(f"Сотрудник {esc(employee.name)} добавлен. График задаётся в карточке сотрудника.")


@router.callback_query(F.data.startswith("staff:view:"))
async def staff_view(callback: CallbackQuery, config: Config, db: Database) -> None:
    employee_id = int(callback.data.rsplit(":", 1)[1])
    async with db.sessions() as session:
        employee = await EmployeeService(session, config.admin_id).get_by_id(employee_id)
        if not employee:
            await callback.answer("Сотрудник не найден", show_alert=True)
            return
        text = (
            f"<b>{esc(employee.name)}</b>\nTelegram ID: <code>{employee.telegram_id}</code>\n"
            f"Пол: {'женский' if employee.gender == 'f' else 'мужской'}\nНикнейм: {esc(employee.nickname)}\n"
            f"График: {esc(EmployeeService.format_schedule(employee))}\n"
            f"Приветствие: {esc(employee.greeting or 'Базовое')}\nАктивен: {'да' if employee.active else 'нет'}"
        )
    rows = [
        [InlineKeyboardButton(text="Изменить имя", callback_data=f"staff:edit:{employee.id}:name")],
        [InlineKeyboardButton(text="Изменить никнейм", callback_data=f"staff:edit:{employee.id}:nickname")],
        [InlineKeyboardButton(text="Изменить приветствие", callback_data=f"staff:edit:{employee.id}:greeting")],
        [InlineKeyboardButton(text="Сменить пол", callback_data=f"staff:gender:{employee.id}")],
        [InlineKeyboardButton(text="Задать график", callback_data=f"staff:edit:{employee.id}:schedule")],
        [InlineKeyboardButton(text="Активировать/деактивировать", callback_data=f"staff:active:{employee.id}")],
        [InlineKeyboardButton(text="К списку", callback_data="admin:staff")],
    ]
    await edit_menu(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("staff:gender:"))
async def staff_gender(callback: CallbackQuery, config: Config, db: Database) -> None:
    employee_id = int(callback.data.rsplit(":", 1)[1])
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        employee = await employees.get_by_id(employee_id)
        if employee:
            await employees.update(employee, gender="f" if employee.gender == "m" else "m")
    await staff_view(callback, config, db)


@router.callback_query(F.data.startswith("staff:active:"))
async def staff_active(callback: CallbackQuery, config: Config, db: Database) -> None:
    employee_id = int(callback.data.rsplit(":", 1)[1])
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        employee = await employees.get_by_id(employee_id)
        if employee and employee.telegram_id != config.admin_id:
            await employees.update(employee, active=not employee.active)
    await staff_view(callback, config, db)


@router.callback_query(F.data.startswith("staff:edit:"))
async def staff_edit(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, raw_id, field = callback.data.split(":", 3)
    if field not in {"name", "nickname", "greeting", "schedule"}:
        await callback.answer()
        return
    await state.set_state(AdminForm.employee_field)
    await state.set_data({"employee_id": int(raw_id), "field": field})
    prompts = {
        "name": "Введите новое имя.",
        "nickname": "Введите новый никнейм одним словом.",
        "greeting": "Введите приветствие. Доступен параметр {name}.",
        "schedule": "Введите смены через точку с запятой: Пн-Пт 09:00-18:00; Сб 10:00-16:00. Для очистки отправьте -.",
    }
    await callback.message.answer(prompts[field])
    await callback.answer()


@router.message(AdminForm.employee_field, F.text, ~F.text.startswith("/"))
async def save_staff_field(message: Message, state: FSMContext, config: Config, db: Database) -> None:
    data = await state.get_data()
    field = data["field"]
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        employee = await employees.get_by_id(int(data["employee_id"]))
        if not employee:
            await state.clear()
            return
        try:
            if field == "schedule":
                shifts = parse_schedule(message.text)
                await employees.set_schedule(employee, shifts)
            else:
                value = message.text.strip()
                if field == "nickname" and (not value or any(char.isspace() for char in value)):
                    raise EmployeeError("Никнейм должен быть одним словом")
                if field == "name" and not value:
                    raise EmployeeError("Имя не может быть пустым")
                if field == "greeting":
                    value.format(name=employee.name)
                await employees.update(employee, **{field: value})
        except (EmployeeError, ValueError, KeyError) as exc:
            await message.answer(f"Не удалось сохранить: {exc}")
            return
    await state.clear()
    await message.answer("Профиль обновлён.")


@router.callback_query(F.data == "admin:categories")
async def categories_menu(callback: CallbackQuery, config: Config, db: Database) -> None:
    async with db.sessions() as session:
        categories = await SettingsService(session, config.defaults).categories()
    rows = [
        [InlineKeyboardButton(text=f"Удалить: {category}", callback_data=f"category:delete:{index}")]
        for index, category in enumerate(categories)
    ]
    rows.extend(
        [
            [InlineKeyboardButton(text="Добавить категорию", callback_data="category:add")],
            [InlineKeyboardButton(text="Назад", callback_data="admin:main")],
        ]
    )
    await edit_menu(callback, "Категории:\n" + "\n".join(f"• {esc(item)}" for item in categories), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "category:add")
async def category_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminForm.category)
    await callback.message.answer("Введите название новой категории.")
    await callback.answer()


@router.message(AdminForm.category, F.text, ~F.text.startswith("/"))
async def category_save(message: Message, state: FSMContext, config: Config, db: Database) -> None:
    category = message.text.strip()
    if not 1 <= len(category) <= 200:
        await message.answer("Название должно содержать от 1 до 200 символов.")
        return
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        categories = await settings.categories()
        if category in categories:
            await message.answer("Такая категория уже есть.")
            return
        categories.append(category)
        await settings.set("categories", categories)
    await state.clear()
    await message.answer("Категория добавлена.")


@router.callback_query(F.data.startswith("category:delete:"))
async def category_delete(callback: CallbackQuery, config: Config, db: Database) -> None:
    index = int(callback.data.rsplit(":", 1)[1])
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        categories = await settings.categories()
        if len(categories) <= 1 or not 0 <= index < len(categories):
            await callback.answer("Нельзя удалить последнюю категорию", show_alert=True)
            return
        categories.pop(index)
        await settings.set("categories", categories)
    await categories_menu(callback, config, db)


@router.callback_query(F.data == "admin:statuses")
async def statuses_menu(callback: CallbackQuery, config: Config, db: Database) -> None:
    async with db.sessions() as session:
        statuses = await SettingsService(session, config.defaults).statuses()
    text = "\n".join(
        f"{esc(item['title'])}: {item['key']}, {esc(item['code'])}"
        + (", без напоминаний" if item.get("pauses_reminders") else "")
        + (", закрытый" if item.get("closed") else "")
        for item in statuses
    )
    rows = [
        [
            InlineKeyboardButton(text=f"Изменить {item['code']}", callback_data=f"status:edit:{item['key']}"),
            InlineKeyboardButton(text="Удалить", callback_data=f"status:delete:{item['key']}"),
        ]
        for item in statuses
    ]
    rows.extend(
        [
            [InlineKeyboardButton(text="Добавить статус", callback_data="status:add")],
            [InlineKeyboardButton(text="Назад", callback_data="admin:main")],
        ]
    )
    await edit_menu(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "status:add")
async def status_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminForm.status_add)
    await callback.message.answer("Формат: ключ | Название | КОД | pause, closed или none. Например: review | На проверке | ПР | none")
    await callback.answer()


@router.message(AdminForm.status_add, F.text, ~F.text.startswith("/"))
async def status_save(message: Message, state: FSMContext, config: Config, db: Database) -> None:
    parts = [part.strip() for part in message.text.split("|")]
    if len(parts) not in {3, 4}:
        await message.answer("Используйте формат: ключ | Название | КОД | pause")
        return
    key, title, code = parts[:3]
    flag = parts[3].lower() if len(parts) == 4 else "none"
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,29}", key) or not title or not code or flag not in {"pause", "closed", "none"}:
        await message.answer("Проверьте ключ латиницей, название, код и флаг pause/closed/none.")
        return
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        statuses = await settings.statuses()
        if any(item["key"] == key or item["code"] == code for item in statuses):
            await message.answer("Ключ или код уже используется.")
            return
        item: dict[str, Any] = {"key": key, "title": title, "code": code}
        if flag == "pause":
            item["pauses_reminders"] = True
        elif flag == "closed":
            item["closed"] = True
        statuses.append(item)
        await settings.set("statuses", statuses)
    await state.clear()
    await message.answer("Статус добавлен.")


@router.callback_query(F.data.startswith("status:edit:"))
async def status_edit(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.rsplit(":", 1)[1]
    await state.set_state(AdminForm.status_edit)
    await state.set_data({"status_key": key})
    await callback.message.answer("Введите: Новое название | КОД")
    await callback.answer()


@router.message(AdminForm.status_edit, F.text, ~F.text.startswith("/"))
async def status_edit_save(message: Message, state: FSMContext, config: Config, db: Database) -> None:
    parts = [part.strip() for part in message.text.split("|")]
    if len(parts) != 2 or not all(parts):
        await message.answer("Нужен формат: Название | КОД")
        return
    key = (await state.get_data())["status_key"]
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        statuses = await settings.statuses()
        if any(item["code"] == parts[1] and item["key"] != key for item in statuses):
            await message.answer("Этот код уже используется.")
            return
        for item in statuses:
            if item["key"] == key:
                item["title"], item["code"] = parts
                break
        else:
            await message.answer("Статус не найден.")
            return
        await settings.set("statuses", statuses)
    await state.clear()
    await message.answer("Статус обновлён.")


@router.callback_query(F.data.startswith("status:delete:"))
async def status_delete(callback: CallbackQuery, config: Config, db: Database) -> None:
    key = callback.data.rsplit(":", 1)[1]
    if key in {"new", "work", "archive"}:
        await callback.answer("Системный статус нельзя удалить", show_alert=True)
        return
    async with db.sessions() as session:
        used = await session.scalar(select(Ticket.id).where(Ticket.status == key).limit(1))
        if used:
            await callback.answer("Статус используется в тикетах", show_alert=True)
            return
        settings = SettingsService(session, config.defaults)
        statuses = [item for item in await settings.statuses() if item["key"] != key]
        await settings.set("statuses", statuses)
    await statuses_menu(callback, config, db)


@router.callback_query(F.data == "admin:report")
@router.message(Command("report"))
async def report(event: CallbackQuery | Message, config: Config, db: Database) -> None:
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        admin_thread = await settings.topic("admin")
        if admin_thread is None:
            target = event.message if isinstance(event, CallbackQuery) else event
            await target.answer("Сначала выполните /setup.")
        else:
            text = await build_report(session, await settings.closed_keys())
            for part in split_html(text):
                await event.bot.send_message(config.support_group_id, part, message_thread_id=admin_thread)
    if isinstance(event, CallbackQuery):
        await event.answer("Отчёт отправлен в «Админ-инфа»")


@router.callback_query(F.data == "admin:reviews")
@router.message(Command("reviews"))
async def reviews_list(event: CallbackQuery | Message, config: Config, db: Database) -> None:
    async with db.sessions() as session:
        reviews = list(
            (
                await session.execute(
                    select(Review).options(selectinload(Review.employee)).order_by(Review.created_at.desc()).limit(30)
                )
            ).scalars()
        )
    lines = []
    rows = []
    for review in reviews:
        lines.append(
            f"#{review.ticket_number}: {review.rating}/5, {esc(review.employee.name if review.employee else 'не указан')}"
            + (f"\n{esc(review.comment)}" if review.comment else "")
        )
        rows.append([InlineKeyboardButton(text=f"Удалить отзыв #{review.id}", callback_data=f"review:delete:{review.id}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="admin:main")])
    text = "\n\n".join(lines) if lines else "Отзывов пока нет"
    if isinstance(event, CallbackQuery):
        await edit_menu(event, text[:4000], InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        await event.answer(text[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("review:delete:"))
async def review_delete(callback: CallbackQuery) -> None:
    review_id = callback.data.rsplit(":", 1)[1]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подтвердить удаление", callback_data=f"review:delete_confirm:{review_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data="admin:reviews")],
        ]
    )
    await edit_menu(callback, f"Удалить отзыв №{review_id}?", keyboard)


@router.callback_query(F.data.startswith("review:delete_confirm:"))
async def review_delete_confirm(callback: CallbackQuery, config: Config, db: Database) -> None:
    review_id = int(callback.data.rsplit(":", 1)[1])
    async with db.sessions() as session:
        await session.execute(delete(Review).where(Review.id == review_id))
        await session.commit()
    await reviews_list(callback, config, db)


@router.callback_query(F.data == "admin:archive")
@router.message(Command("archive"))
async def archive_list(event: CallbackQuery | Message, config: Config, db: Database) -> None:
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        archive_statuses = await settings.archive_keys()
        tickets = list(
            (
                await session.execute(
                    select(Ticket)
                    .options(selectinload(Ticket.employee))
                    .where(Ticket.status.in_(archive_statuses), Ticket.deleted_at.is_(None))
                    .order_by(Ticket.archived_at.desc())
                    .limit(40)
                )
            ).scalars()
        )
    rows = []
    lines = []
    for ticket in tickets:
        employee = ticket.employee.nickname if ticket.employee else "не назначен"
        lines.append(f"#{ticket.number}: {esc(employee)}, {esc(ticket.category)}, {ticket.archived_at:%d.%m.%Y}")
        rows.append([InlineKeyboardButton(text=f"Удалить #{ticket.number}", callback_data=f"archive:delete:{ticket.number}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="admin:main")])
    text = "Архивные тикеты\n\n" + ("\n".join(lines) if lines else "Архив пуст")
    if isinstance(event, CallbackQuery):
        await edit_menu(event, text[:4000], InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        await event.answer(text[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("archive:delete:"))
async def archive_delete(callback: CallbackQuery) -> None:
    number = callback.data.rsplit(":", 1)[1]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подтвердить удаление топика", callback_data=f"archive:delete_confirm:{number}")],
            [InlineKeyboardButton(text="Отмена", callback_data="admin:archive")],
        ]
    )
    await edit_menu(callback, f"Удалить топик тикета #{number}? Переписка будет очищена без восстановления.", keyboard)


@router.callback_query(F.data.startswith("archive:delete_confirm:"))
async def archive_delete_confirm(callback: CallbackQuery, config: Config, db: Database, topics: TopicService) -> None:
    number = int(callback.data.rsplit(":", 1)[1])
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.get(number)
        if not ticket or ticket.status not in await settings.archive_keys() or ticket.deleted_at:
            await callback.answer("Архивный тикет не найден", show_alert=True)
            return
        await topics.delete(tickets, ticket)
    await callback.answer("Топик удалён")
    await archive_list(callback, config, db)
