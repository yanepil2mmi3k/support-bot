from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from support_bot.config import Config
from support_bot.db.models import Employee, Ticket
from support_bot.db.session import Database
from support_bot.services.employees import EmployeeService
from support_bot.services.interface import InterfaceService
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import PermissionDenied, TicketService
from support_bot.services.topics import TopicService, esc

router = Router(name="edit")


class EditForm(StatesGroup):
    value = State()


def menu_key(ticket: Ticket, employee: Employee) -> str:
    return f"edit:{ticket.id}:{employee.id}"


async def menu_keyboard(settings: SettingsService, ticket: Ticket) -> InlineKeyboardMarkup:
    closed = await settings.is_closed(ticket.status)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏷 Статус", callback_data="edit:status")],
            [InlineKeyboardButton(text="📝 Описание", callback_data="edit:note")],
            [InlineKeyboardButton(text="✏️ Название", callback_data="edit:title")],
            [InlineKeyboardButton(text="🔁 Передать", callback_data="edit:transfer")],
            [InlineKeyboardButton(text="⏰ Напоминание", callback_data="edit:remind")],
            [InlineKeyboardButton(
                text="🔄 Переоткрыть" if closed else "✅ Закрыть",
                callback_data="edit:open" if closed else "edit:close",
            )],
            [InlineKeyboardButton(text="✖ Закрыть меню", callback_data="edit:dismiss")],
        ]
    )


async def menu_text(settings: SettingsService, ticket: Ticket) -> str:
    status = await settings.status(ticket.status)
    note = ticket.note or "Не задано"
    note_preview = esc(note[:500]) + ("…" if len(note) > 500 else "")
    return (
        f"<b>Настройка обращения №{ticket.number}</b>\n\n"
        f"<b>Статус:</b> {esc(status['title'])}\n"
        f"<b>Ответственный:</b> {esc(ticket.employee.name if ticket.employee else 'Не назначен')}\n"
        f"<b>Описание:</b> {note_preview}\n\n"
        "Выберите действие:"
    )


async def send_menu(
    bot: Bot,
    config: Config,
    session,
    ticket: Ticket,
    employee: Employee,
    chat_id: int,
    thread_id: int,
) -> None:
    settings = SettingsService(session, config.defaults)
    interface = InterfaceService(session)
    sent = await bot.send_message(
        chat_id,
        await menu_text(settings, ticket),
        message_thread_id=thread_id,
        reply_markup=await menu_keyboard(settings, ticket),
    )
    key = menu_key(ticket, employee)
    await interface.track(key, chat_id, sent.message_id)
    await interface.trim(key, bot, keep=2)


async def render_menu(callback: CallbackQuery, config: Config, session, ticket: Ticket) -> None:
    settings = SettingsService(session, config.defaults)
    await callback.message.edit_text(
        await menu_text(settings, ticket),
        reply_markup=await menu_keyboard(settings, ticket),
    )


async def callback_context(callback: CallbackQuery, config: Config, db: Database):
    session = db.sessions()
    tickets = TicketService(session, config.admin_id)
    ticket = await tickets.by_thread(callback.message.message_thread_id or 0)
    employee = await EmployeeService(session, config.admin_id).get(callback.from_user.id)
    try:
        if not ticket or ticket.deleted_at:
            raise PermissionDenied("Тикет не найден")
        tickets.check_access(ticket, employee)
    except PermissionDenied as exc:
        await callback.answer(str(exc), show_alert=True)
        await session.close()
        return None
    return session, ticket, employee, tickets, SettingsService(session, config.defaults)


@router.message(Command("edit"), F.chat.type.in_({"group", "supergroup"}))
async def open_edit_menu(message: Message, state: FSMContext, config: Config, db: Database) -> None:
    if message.chat.id != config.support_group_id or not message.message_thread_id:
        return
    await state.clear()
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.by_thread(message.message_thread_id)
        employee = await EmployeeService(session, config.admin_id).get(message.from_user.id)
        try:
            if not ticket or ticket.deleted_at:
                raise PermissionDenied("Тикет не найден")
            tickets.check_access(ticket, employee)
        except PermissionDenied as exc:
            await message.answer(str(exc))
            return
        await send_menu(
            message.bot,
            config,
            session,
            ticket,
            employee,
            message.chat.id,
            message.message_thread_id,
        )
    try:
        await message.delete()
    except TelegramAPIError:
        pass


@router.callback_query(F.data.startswith("edit:"))
async def edit_action(
    callback: CallbackQuery, state: FSMContext, config: Config, db: Database, topics: TopicService
) -> None:
    context = await callback_context(callback, config, db)
    if not context:
        return
    session, ticket, employee, tickets, settings = context
    try:
        await state.clear()
        action = callback.data.split(":", 1)[1]
        interface = InterfaceService(session)
        key = menu_key(ticket, employee)
        if action == "dismiss":
            await interface.clear(key, callback.bot)
            await callback.answer()
            return
        if action == "back":
            await render_menu(callback, config, session, ticket)
        elif action == "status":
            statuses = await settings.statuses()
            rows = [
                [InlineKeyboardButton(text=status["title"], callback_data=f"edit_set:{status['key']}")]
                for status in statuses
            ]
            rows.append([InlineKeyboardButton(text="Назад", callback_data="edit:back")])
            await callback.message.edit_text("Выберите новый статус:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        elif action == "transfer":
            employees = await EmployeeService(session, config.admin_id).all()
            rows = [
                [InlineKeyboardButton(text=item.name, callback_data=f"edit_transfer:{item.id}")]
                for item in employees
                if item.id != ticket.employee_id
            ]
            rows.append([InlineKeyboardButton(text="Назад", callback_data="edit:back")])
            await callback.message.edit_text("Выберите нового ответственного:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        elif action == "remind":
            rows = [
                [
                    InlineKeyboardButton(text="30 мин", callback_data="edit_remind:30"),
                    InlineKeyboardButton(text="1 час", callback_data="edit_remind:60"),
                    InlineKeyboardButton(text="3 часа", callback_data="edit_remind:180"),
                ],
                [InlineKeyboardButton(text="Свой интервал", callback_data="edit:remind_custom")],
                [InlineKeyboardButton(text="Отключить", callback_data="edit_remind:0")],
                [InlineKeyboardButton(text="Назад", callback_data="edit:back")],
            ]
            await callback.message.edit_text("Когда напомнить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        elif action in {"note", "title", "remind_custom"}:
            await state.set_state(EditForm.value)
            await state.set_data({"ticket_number": ticket.number, "action": action, "menu_key": key})
            prompts = {
                "note": "Введите рабочее описание тикета:",
                "title": "Введите новое название без номера и статуса:",
                "remind_custom": "Введите интервал в минутах от 1 до 10080:",
            }
            sent = await callback.message.answer(prompts[action])
            await interface.track(key, callback.message.chat.id, sent.message_id)
            await interface.trim(key, callback.bot, keep=2)
        elif action == "close":
            await topics.close(tickets, settings, ticket)
            await render_menu(callback, config, session, ticket)
        elif action == "open":
            try:
                await topics.reopen(tickets, settings, ticket)
            except ValueError as exc:
                await callback.answer(str(exc), show_alert=True)
                return
            await render_menu(callback, config, session, ticket)
        await callback.answer()
    finally:
        await session.close()


@router.callback_query(F.data.startswith("edit_set:"))
async def edit_status(
    callback: CallbackQuery, state: FSMContext, config: Config, db: Database, topics: TopicService
) -> None:
    context = await callback_context(callback, config, db)
    if not context:
        return
    session, ticket, _employee, tickets, settings = context
    try:
        await state.clear()
        status = await settings.status(callback.data.split(":", 1)[1])
        if status.get("closed") or status.get("archive"):
            await topics.close(tickets, settings, ticket, status)
        else:
            if await settings.is_closed(ticket.status):
                await topics.reopen(tickets, settings, ticket)
            await tickets.set_status(ticket, status["key"], status)
            await topics.update_pinned(tickets, settings, ticket)
        await render_menu(callback, config, session, ticket)
        await callback.answer("Статус обновлён")
    finally:
        await session.close()


@router.callback_query(F.data.startswith("edit_transfer:"))
async def edit_transfer(
    callback: CallbackQuery, state: FSMContext, config: Config, db: Database, topics: TopicService
) -> None:
    context = await callback_context(callback, config, db)
    if not context:
        return
    session, ticket, _employee, tickets, settings = context
    try:
        await state.clear()
        target = await EmployeeService(session, config.admin_id).get_by_id(int(callback.data.split(":", 1)[1]))
        if not target or not target.active:
            await callback.answer("Сотрудник недоступен", show_alert=True)
            return
        await topics.transfer(tickets, settings, ticket, target)
        await callback.message.edit_text(f"Обращение №{ticket.number} передано: {esc(target.name)}.", reply_markup=None)
        await callback.answer("Тикет передан")
    finally:
        await session.close()


@router.callback_query(F.data.startswith("edit_remind:"))
async def edit_reminder(callback: CallbackQuery, state: FSMContext, config: Config, db: Database) -> None:
    context = await callback_context(callback, config, db)
    if not context:
        return
    session, ticket, employee, tickets, _settings = context
    try:
        await state.clear()
        minutes = int(callback.data.split(":", 1)[1])
        if minutes:
            await tickets.set_reminder(ticket, employee, minutes)
            await callback.answer(f"Напоминание через {minutes} мин")
        else:
            await tickets.disable_reminder(ticket)
            await callback.answer("Напоминание отключено")
        await render_menu(callback, config, session, ticket)
    finally:
        await session.close()


@router.message(EditForm.value, F.text, ~F.text.startswith("/"), F.chat.type.in_({"group", "supergroup"}))
async def save_edit_value(
    message: Message, state: FSMContext, config: Config, db: Database, topics: TopicService
) -> None:
    data = await state.get_data()
    action = data["action"]
    value = message.text.strip()
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.get(int(data["ticket_number"]))
        employee = await EmployeeService(session, config.admin_id).get(message.from_user.id)
        try:
            if not ticket or ticket.deleted_at:
                raise PermissionDenied("Тикет не найден")
            tickets.check_access(ticket, employee)
        except PermissionDenied as exc:
            await state.clear()
            await message.answer(str(exc))
            return
        valid = bool(value)
        if action == "note":
            valid = valid and len(value) <= 3000
        elif action == "title":
            valid = valid and len(value) <= 128
        elif action == "remind_custom":
            try:
                minutes = int(value)
                valid = 1 <= minutes <= 10080
            except ValueError:
                valid = False
        interface = InterfaceService(session)
        key = data["menu_key"]
        if not valid:
            sent = await message.answer("Проверьте значение и попробуйте ещё раз.")
            await interface.track(key, message.chat.id, sent.message_id)
            await interface.trim(key, message.bot, keep=2)
            return
        if action == "note":
            ticket.note = value
            await tickets.session.commit()
            await topics.update_pinned(tickets, settings, ticket)
        elif action == "title":
            await topics.rename(tickets, settings, ticket, value)
        else:
            await tickets.set_reminder(ticket, employee, int(value))
        await interface.clear(key, message.bot)
    await state.clear()
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    async with db.sessions() as session:
        ticket = await TicketService(session, config.admin_id).get(int(data["ticket_number"]))
        employee = await EmployeeService(session, config.admin_id).get(message.from_user.id)
        await send_menu(
            message.bot,
            config,
            session,
            ticket,
            employee,
            message.chat.id,
            message.message_thread_id,
        )
