from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from support_bot.config import Config
from support_bot.db.models import Employee, Review, Ticket
from support_bot.db.session import Database
from support_bot.services.employees import EmployeeService
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import PermissionDenied, TicketError, TicketService
from support_bot.services.topics import TopicService, esc

router = Router(name="staff")


class GreetingForm(StatesGroup):
    text = State()


async def current_ticket(
    message: Message, command: CommandObject | None, tickets: TicketService
) -> tuple[Ticket | None, str]:
    if message.message_thread_id:
        ticket = await tickets.by_thread(message.message_thread_id)
        if ticket:
            return ticket, (command.args if command else "") or ""
    args = (command.args if command else "") or ""
    match = re.match(r"^#(\d+)\s*(.*)$", args, flags=re.DOTALL)
    if match:
        return await tickets.get(int(match.group(1))), match.group(2)
    return None, args


async def staff_context(message: Message, config: Config, db: Database):
    session = db.sessions()
    employee_service = EmployeeService(session, config.admin_id)
    employee = await employee_service.get(message.from_user.id)
    if not employee or not employee.active:
        await session.close()
        await message.answer("Профиль сотрудника не найден. Обратитесь к администратору.")
        return None
    return session, employee, TicketService(session, config.admin_id), SettingsService(session, config.defaults)


def is_support_group(message: Message, config: Config) -> bool:
    return message.chat.id == config.support_group_id


@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.from_user.is_bot,
    F.content_type.in_({"forum_topic_edited", "pinned_message"}),
)
async def delete_bot_service_message(message: Message, config: Config) -> None:
    if is_support_group(message, config):
        await message.delete()


@router.message(Command("setup"))
async def setup_group(message: Message, config: Config, db: Database) -> None:
    if message.from_user.id != config.admin_id or not is_support_group(message, config):
        return
    async with db.sessions() as session:
        settings = SettingsService(session, config.defaults)
        await settings.set_topic("general", message.message_thread_id or 1)
        titles = {
            "commands": "Команды",
            "requests": "Заявки",
            "admin": "Админ-инфа",
            "quick_replies": "Быстрые ответы",
        }
        created = []
        for key, title in titles.items():
            thread_id = await settings.topic(key)
            if thread_id is None:
                topic = await message.bot.create_forum_topic(config.support_group_id, title)
                thread_id = topic.message_thread_id
                await settings.set_topic(key, thread_id)
                created.append(title)
        admin_thread = await settings.topic("admin")
    notice = "Системные топики зарегистрированы: " + ", ".join(titles.values())
    if created:
        notice += ". Созданы: " + ", ".join(created)
    await message.bot.send_message(config.support_group_id, notice, message_thread_id=admin_thread)


@router.callback_query(F.data.startswith("accept:"))
async def accept_request(callback: CallbackQuery, config: Config, db: Database) -> None:
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        employee = await employees.get(callback.from_user.id)
        if not employee or not employee.active:
            await callback.answer("Профиль сотрудника не найден", show_alert=True)
            return
        try:
            number = int(callback.data.split(":", 1)[1])
        except ValueError:
            await callback.answer()
            return
        ticket = await TicketService(session, config.admin_id).get(number)
        if not ticket or ticket.employee_id or ticket.status != "new":
            await callback.answer("Заявка уже принята", show_alert=True)
            return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подтвердить принятие", callback_data=f"accept_confirm:{number}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"accept_cancel:{number}")],
        ]
    )
    await callback.message.answer(
        f"{esc(employee.name)}, подтвердите принятие обращения #{number}.", reply_markup=keyboard
    )
    await callback.answer()


@router.callback_query(F.data.startswith("accept_cancel:"))
async def cancel_accept(callback: CallbackQuery, config: Config, db: Database) -> None:
    async with db.sessions() as session:
        employee = await EmployeeService(session, config.admin_id).get(callback.from_user.id)
        if not employee or not employee.active:
            await callback.answer("Профиль сотрудника не найден", show_alert=True)
            return
    await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data.startswith("accept_confirm:"))
async def confirm_accept(callback: CallbackQuery, config: Config, db: Database, topics: TopicService) -> None:
    async with db.sessions() as session:
        employee_service = EmployeeService(session, config.admin_id)
        employee = await employee_service.get(callback.from_user.id)
        if not employee or not employee.active:
            await callback.answer("Профиль сотрудника не найден", show_alert=True)
            return
        number = int(callback.data.split(":", 1)[1])
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.get(number)
        if not ticket:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        can_recover = (
            ticket.employee_id == employee.id
            and ticket.thread_id is not None
            and ticket.status == "work"
        )
        if ticket.employee_id is None:
            if ticket.status != "new":
                await callback.answer("Заявка уже принята", show_alert=True)
                return
            if not await tickets.claim(ticket, employee):
                await callback.answer("Заявку уже принял другой сотрудник", show_alert=True)
                return
        elif not can_recover:
            await callback.answer("Заявка уже принята", show_alert=True)
            return
        try:
            await topics.create_ticket_topic(tickets, settings, ticket, employee)
        except Exception:
            await tickets.release_uncreated(ticket)
            raise
        should_greet = await settings.get("auto_greeting", True)
    await callback.message.edit_text(f"Обращение #{number} принято: {esc(employee.nickname)}")
    if ticket.user_id:
        await callback.bot.send_message(ticket.user_id, config.message("ticket_accepted", number=number))
        if should_greet:
            await topics.greet(ticket, employee)
    await callback.answer()


@router.message(Command("profile"))
async def profile(message: Message, config: Config, db: Database) -> None:
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        employee = await employees.get(message.from_user.id)
        if not employee:
            await message.answer("Профиль сотрудника не найден.")
            return
        visible = message.from_user.id == config.admin_id or await SettingsService(session, config.defaults).get(
            "employee_ratings_visible", False
        )
        rating_text = "Оценки скрыты настройкой администратора"
        if visible:
            count, average = (
                await session.execute(
                    select(func.count(Review.id), func.avg(Review.rating)).where(Review.employee_id == employee.id)
                )
            ).one()
            rating_text = f"Оценок: {count or 0}, средняя: {average:.2f}" if count else "Оценок пока нет"
        text = (
            f"<b>{esc(employee.name)}</b>\n"
            f"Пол: {'женский' if employee.gender == 'f' else 'мужской'}\n"
            f"Никнейм: {esc(employee.nickname)}\n"
            f"График: {esc(EmployeeService.format_schedule(employee))}\n"
            f"Приветствие: {esc(employee.greeting or config.message('default_greeting', name=employee.name))}\n"
            f"{rating_text}"
        )
    rows = []
    if visible:
        rows.append([InlineKeyboardButton(text="Мои отзывы", callback_data="profile:reviews")])
    rows.append([InlineKeyboardButton(text="Изменить приветствие", callback_data="profile:greeting")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "profile:reviews")
async def profile_reviews(callback: CallbackQuery, config: Config, db: Database) -> None:
    async with db.sessions() as session:
        employee = await EmployeeService(session, config.admin_id).get(callback.from_user.id)
        visible = callback.from_user.id == config.admin_id or await SettingsService(session, config.defaults).get(
            "employee_ratings_visible", False
        )
        if not employee or not visible:
            await callback.answer("Отзывы скрыты настройкой администратора", show_alert=True)
            return
        reviews = list(
            (
                await session.execute(
                    select(Review)
                    .where(Review.employee_id == employee.id)
                    .order_by(Review.created_at.desc())
                    .limit(20)
                )
            ).scalars()
        )
    text = "\n\n".join(
        f"#{review.ticket_number}: {review.rating}/5" + (f"\n{esc(review.comment)}" if review.comment else "")
        for review in reviews
    ) or "Отзывов пока нет"
    await callback.message.answer(text[:4000])
    await callback.answer()


@router.callback_query(F.data == "profile:greeting")
async def edit_greeting(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GreetingForm.text)
    await callback.message.answer("Отправьте новое приветствие. Используйте {name} там, где нужно подставить имя.")
    await callback.answer()


@router.message(GreetingForm.text, F.text)
async def save_greeting(message: Message, state: FSMContext, config: Config, db: Database) -> None:
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        employee = await employees.get(message.from_user.id)
        if not employee:
            return
        try:
            await employees.update_greeting(employee, message.text)
        except ValueError as exc:
            await message.answer(f"{exc}.")
            return
    await state.clear()
    await message.answer("Приветствие обновлено.")


@router.message(Command("ticket", "help"), F.chat.type.in_({"group", "supergroup"}))
async def ticket_help(message: Message, config: Config) -> None:
    if message.from_user.id == config.admin_id:
        from support_bot.handlers.admin import admin_help

        await admin_help(message)
        return
    await message.answer(
        "<b>Справка сотрудника</b>\n\n"
        "<b>Диалог с клиентом</b>\n"
        "Сообщения в топике тикета автоматически отправляются клиенту. "
        "Команды и служебные сообщения бота не пересылаются. При редактировании сообщения его копия у клиента также изменится. "
        "Для удаления сообщения в топике и у клиента ответьте на него командой /delete.\n\n"
        "<b>Общие команды</b>\n"
        "/profile: открыть профиль и изменить приветствие\n"
        "/edit: открыть меню всех действий с тикетом\n"
        "/ticket или /help: показать эту справку\n"
        "/delete: удалить своё сообщение в топике и у клиента, команда отправляется ответом\n\n"
        "<b>Статус и карточка</b>\n"
        "/status: показать доступные статусы\n"
        "/status work: перевести тикет в работу\n"
        "/status waiting: ожидать ответа клиента\n"
        "/status pause: отключить напоминания\n"
        "/status resolved: закрыть тикет и запросить оценку\n"
        "/note текст: обновить рабочее описание в закрепе\n"
        "/description текст: синоним команды /note\n"
        "/rename название: изменить название топика\n"
        "/pin: обновить закреп\n\n"
        "<b>Назначение и напоминания</b>\n"
        "/transfer ник: передать тикет сотруднику\n"
        "/transfer ник waiting: передать тикет и сменить статус\n"
        "/remind 30: напомнить через 30 минут, затем по общему периоду\n"
        "/remind_off: отключить напоминание\n\n"
        "<b>Завершение</b>\n"
        "/close: закрыть и архивировать тикет\n"
        "/open: переоткрыть тот же тикет\n\n"
        "<b>В топике «Команды»</b>\n"
        "/rename #43 Новое название: изменить название тикета\n"
        "/description #43 Текст: изменить описание тикета\n\n"
        "Сотрудник управляет только своими тикетами. Администратор может управлять любыми."
    )


@router.message(Command("status"))
async def change_status(
    message: Message, command: CommandObject, config: Config, db: Database, topics: TopicService
) -> None:
    if not is_support_group(message, config):
        return
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        employee = await employees.get(message.from_user.id)
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket, args = await current_ticket(message, command, tickets)
        if not ticket:
            return
        try:
            tickets.check_access(ticket, employee)
        except PermissionDenied as exc:
            await message.answer(str(exc))
            return
        if not args.strip():
            statuses = await settings.statuses()
            await message.answer("\n".join(f"{item['title']} ({item['key']}, {item['code']})" for item in statuses))
            return
        status = await settings.find_status(args)
        if not status:
            await message.answer("Статус не найден. Используйте ключ, название или аббревиатуру из /status.")
            return
        if status.get("closed") or status.get("archive"):
            await topics.close(tickets, settings, ticket, status)
        else:
            if await settings.is_closed(ticket.status):
                await topics.reopen(tickets, settings, ticket)
            await tickets.set_status(ticket, status["key"], status)
            await topics.update_pinned(tickets, settings, ticket)
    await message.answer(f"Статус тикета #{ticket.number}: {status['title']}")


@router.message(Command("close"))
async def close_ticket(message: Message, command: CommandObject, config: Config, db: Database, topics: TopicService) -> None:
    if not is_support_group(message, config):
        return
    async with db.sessions() as session:
        employee = await EmployeeService(session, config.admin_id).get(message.from_user.id)
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket, _ = await current_ticket(message, command, tickets)
        if not ticket:
            return
        try:
            tickets.check_access(ticket, employee)
            await topics.close(tickets, settings, ticket)
        except PermissionDenied as exc:
            await message.answer(str(exc))
            return


@router.message(Command("open"))
async def open_ticket(message: Message, command: CommandObject, config: Config, db: Database, topics: TopicService) -> None:
    if not is_support_group(message, config):
        return
    async with db.sessions() as session:
        employee = await EmployeeService(session, config.admin_id).get(message.from_user.id)
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket, _ = await current_ticket(message, command, tickets)
        if not ticket:
            return
        if not await settings.is_closed(ticket.status):
            await message.answer("Обращение уже открыто")
            return
        try:
            tickets.check_access(ticket, employee)
            await topics.reopen(tickets, settings, ticket)
        except (PermissionDenied, TicketError, ValueError) as exc:
            await message.answer(str(exc))
            return
    await message.answer(f"Обращение #{ticket.number} открыто.")


@router.message(Command("note", "description"))
async def change_note(message: Message, command: CommandObject, config: Config, db: Database, topics: TopicService) -> None:
    if not is_support_group(message, config):
        return
    async with db.sessions() as session:
        employee = await EmployeeService(session, config.admin_id).get(message.from_user.id)
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket, text = await current_ticket(message, command, tickets)
        if not ticket:
            return
        try:
            tickets.check_access(ticket, employee)
        except PermissionDenied as exc:
            await message.answer(str(exc))
            return
        if not text.strip() or len(text) > 3000:
            await message.answer("Укажите описание длиной до 3000 символов.")
            return
        ticket.note = text.strip()
        await tickets.session.commit()
        await topics.update_pinned(tickets, settings, ticket)
    await message.answer("Описание обновлено.")


@router.message(Command("rename"))
async def rename_ticket(message: Message, command: CommandObject, config: Config, db: Database, topics: TopicService) -> None:
    if not is_support_group(message, config):
        return
    async with db.sessions() as session:
        employee = await EmployeeService(session, config.admin_id).get(message.from_user.id)
        tickets = TicketService(session, config.admin_id)
        ticket, title = await current_ticket(message, command, tickets)
        if not ticket:
            return
        try:
            tickets.check_access(ticket, employee)
            await topics.rename(tickets, SettingsService(session, config.defaults), ticket, title)
        except (PermissionDenied, ValueError) as exc:
            await message.answer(str(exc))
            return
    await message.answer("Название обновлено.")


@router.message(Command("pin"))
async def refresh_pin(message: Message, command: CommandObject, config: Config, db: Database, topics: TopicService) -> None:
    if not is_support_group(message, config):
        return
    async with db.sessions() as session:
        employee = await EmployeeService(session, config.admin_id).get(message.from_user.id)
        tickets = TicketService(session, config.admin_id)
        ticket, _ = await current_ticket(message, command, tickets)
        if not ticket:
            return
        try:
            tickets.check_access(ticket, employee)
        except PermissionDenied as exc:
            await message.answer(str(exc))
            return
        await topics.update_pinned(tickets, SettingsService(session, config.defaults), ticket)
    await message.answer("Закреп обновлён.")


@router.message(Command("remind"))
async def remind(message: Message, command: CommandObject, config: Config, db: Database) -> None:
    if not is_support_group(message, config):
        return
    async with db.sessions() as session:
        employee = await EmployeeService(session, config.admin_id).get(message.from_user.id)
        tickets = TicketService(session, config.admin_id)
        ticket, args = await current_ticket(message, command, tickets)
        if not ticket:
            return
        try:
            tickets.check_access(ticket, employee)
        except PermissionDenied as exc:
            await message.answer(str(exc))
            return
        try:
            minutes = int(args) if args.strip() else int(
                await SettingsService(session, config.defaults).get("reminder_interval_minutes", 60)
            )
            if not 1 <= minutes <= 10080:
                raise ValueError
            await tickets.set_reminder(ticket, employee, minutes)
        except ValueError:
            await message.answer("Укажите интервал от 1 до 10080 минут, например /remind 30.")
            return
    await message.answer(f"Напоминание включено. Первое срабатывание через {minutes} мин.")


@router.message(Command("remind_off"))
async def remind_off(message: Message, command: CommandObject, config: Config, db: Database) -> None:
    if not is_support_group(message, config):
        return
    async with db.sessions() as session:
        employee = await EmployeeService(session, config.admin_id).get(message.from_user.id)
        tickets = TicketService(session, config.admin_id)
        ticket, _ = await current_ticket(message, command, tickets)
        if not ticket:
            return
        try:
            tickets.check_access(ticket, employee)
            await tickets.disable_reminder(ticket)
        except PermissionDenied as exc:
            await message.answer(str(exc))
            return
    await message.answer("Напоминание выключено.")


async def reminder_access(callback: CallbackQuery, config: Config, session, number: int):
    employees = EmployeeService(session, config.admin_id)
    employee = await employees.get(callback.from_user.id)
    tickets = TicketService(session, config.admin_id)
    ticket = await tickets.get(number)
    if not employee or not employee.active:
        await callback.answer("Профиль сотрудника не найден", show_alert=True)
        return None
    if not ticket or ticket.deleted_at:
        await callback.answer("Обращение не найдено", show_alert=True)
        return None
    try:
        tickets.check_access(ticket, employee)
    except PermissionDenied:
        await callback.answer("Обращение уже передано другому сотруднику", show_alert=True)
        return None
    return tickets, ticket


@router.callback_query(F.data.startswith("reminder_transfer:"))
async def reminder_transfer(callback: CallbackQuery, config: Config, db: Database) -> None:
    match = re.fullmatch(r"reminder_transfer:(\d+)", callback.data or "")
    if not match:
        await callback.answer("Некорректное действие", show_alert=True)
        return
    number = int(match.group(1))
    async with db.sessions() as session:
        context = await reminder_access(callback, config, session, number)
        if not context:
            return
        _, ticket = context
        targets = [
            item for item in await EmployeeService(session, config.admin_id).all()
            if item.id != ticket.employee_id
        ]
        if not targets:
            await callback.answer("Нет доступных сотрудников", show_alert=True)
            return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=item.nickname, callback_data=f"reminder_target:{number}:{item.id}")]
        for item in targets
    ])
    await callback.message.edit_text(f"Передача обращения №{number}: выберите сотрудника", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("reminder_target:"))
async def reminder_transfer_target(callback: CallbackQuery, config: Config, db: Database) -> None:
    match = re.fullmatch(r"reminder_target:(\d+):(\d+)", callback.data or "")
    if not match:
        await callback.answer("Некорректное действие", show_alert=True)
        return
    number, target_id = int(match.group(1)), int(match.group(2))
    async with db.sessions() as session:
        context = await reminder_access(callback, config, session, number)
        if not context:
            return
        _, ticket = context
        target = await session.get(Employee, target_id)
        if not target or not target.active or target.id == ticket.employee_id:
            await callback.answer("Выберите другого активного сотрудника", show_alert=True)
            return
        target_name = target.nickname
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Подтвердить передачу", callback_data=f"reminder_confirm:{number}:{target_id}"
        )],
        [InlineKeyboardButton(text="Назад", callback_data=f"reminder_transfer:{number}")],
    ])
    await callback.message.edit_text(
        f"Передать обращение №{number} сотруднику {esc(target_name)}?", reply_markup=keyboard
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reminder_confirm:"))
async def reminder_transfer_confirm(
    callback: CallbackQuery, config: Config, db: Database, topics: TopicService
) -> None:
    match = re.fullmatch(r"reminder_confirm:(\d+):(\d+)", callback.data or "")
    if not match:
        await callback.answer("Некорректное действие", show_alert=True)
        return
    number, target_id = int(match.group(1)), int(match.group(2))
    async with db.sessions() as session:
        context = await reminder_access(callback, config, session, number)
        if not context:
            return
        tickets, ticket = context
        target = await session.get(Employee, target_id)
        if not target or not target.active or target.id == ticket.employee_id:
            await callback.answer("Сотрудник уже изменён или недоступен", show_alert=True)
            return
        await topics.transfer(tickets, SettingsService(session, config.defaults), ticket, target)
        target_name = target.nickname
    await callback.message.edit_text(f"Обращение №{number} передано: {esc(target_name)}", reply_markup=None)
    await callback.answer()


@router.callback_query(F.data.startswith("reminder_disable:"))
async def reminder_disable(callback: CallbackQuery, config: Config, db: Database) -> None:
    match = re.fullmatch(r"reminder_disable:(\d+)", callback.data or "")
    if not match:
        await callback.answer("Некорректное действие", show_alert=True)
        return
    number = int(match.group(1))
    async with db.sessions() as session:
        context = await reminder_access(callback, config, session, number)
        if not context:
            return
        tickets, ticket = context
        await tickets.disable_reminder(ticket)
    await callback.message.edit_text(f"Напоминание по обращению №{number} отключено", reply_markup=None)
    await callback.answer()


@router.message(Command("transfer"))
async def transfer(message: Message, command: CommandObject, config: Config, db: Database, topics: TopicService) -> None:
    if not is_support_group(message, config):
        return
    async with db.sessions() as session:
        employees = EmployeeService(session, config.admin_id)
        employee = await employees.get(message.from_user.id)
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket, args = await current_ticket(message, command, tickets)
        if not ticket:
            return
        try:
            tickets.check_access(ticket, employee)
        except PermissionDenied as exc:
            await message.answer(str(exc))
            return
        parts = args.split(maxsplit=1)
        if not parts:
            await message.answer("Использование: /transfer ник [статус]")
            return
        target = await employees.by_nickname(parts[0])
        if not target and parts[0].isdigit():
            target = await employees.get(int(parts[0]))
        if not target or not target.active or target.id == ticket.employee_id:
            await message.answer("Выберите другого активного сотрудника.")
            return
        status = None
        if len(parts) > 1:
            status_data = await settings.find_status(parts[1])
            if not status_data:
                await message.answer("Статус не найден.")
                return
            status = status_data["key"]
        await topics.transfer(tickets, settings, ticket, target, status)
    await message.answer(f"Тикет передан: {esc(target.nickname)}.")


@router.edited_message(F.chat.type.in_({"group", "supergroup"}))
async def edit_staff_message(message: Message, config: Config, db: Database, topics: TopicService) -> None:
    if not is_support_group(message, config) or not message.message_thread_id:
        return
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.by_thread(message.message_thread_id)
        if not ticket or ticket.deleted_at or await settings.is_closed(ticket.status):
            return
        employee = await EmployeeService(session, config.admin_id).get(message.from_user.id)
        try:
            tickets.check_access(ticket, employee)
        except PermissionDenied:
            return
        link = await tickets.linked_message(ticket, message.chat.id, message.message_id)
        if not link or link.source_chat_id != config.support_group_id:
            return
        if await topics.relay_edit(message, link):
            tickets.update_message_metadata(link, message)
            await session.commit()


@router.message(Command("delete"), F.chat.type.in_({"group", "supergroup"}))
async def delete_staff_message(message: Message, config: Config, db: Database, topics: TopicService) -> None:
    if not is_support_group(message, config) or not message.message_thread_id:
        return
    if not message.reply_to_message:
        await message.answer("Ответьте командой /delete на своё сообщение, которое нужно удалить у клиента.")
        return
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        ticket = await tickets.by_thread(message.message_thread_id)
        if not ticket:
            return
        employee = await EmployeeService(session, config.admin_id).get(message.from_user.id)
        try:
            tickets.check_access(ticket, employee)
        except PermissionDenied:
            return
        link = await tickets.linked_message(ticket, message.chat.id, message.reply_to_message.message_id)
        if not link or link.source_chat_id != config.support_group_id:
            await message.answer("Здесь можно удалять только исходные сообщения сотрудников.")
            return
        await topics.delete_relayed(link, delete_source=True)
        await session.delete(link)
        await session.commit()
    await message.answer("Сообщение удалено в топике и у клиента.")


@router.message(StateFilter(None), F.chat.type.in_({"group", "supergroup"}))
async def relay_staff_message(message: Message, config: Config, db: Database, topics: TopicService) -> None:
    if message.from_user.is_bot or not is_support_group(message, config) or not message.message_thread_id:
        return
    if message.content_type not in {
        "text", "photo", "video", "document", "animation", "audio", "voice", "video_note",
        "sticker", "contact", "location", "venue", "poll",
    }:
        return
    if (message.text or message.caption or "").startswith("/"):
        return
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.by_thread(message.message_thread_id)
        if not ticket or ticket.deleted_at or await settings.is_closed(ticket.status):
            return
        employee = await EmployeeService(session, config.admin_id).get(message.from_user.id)
        try:
            tickets.check_access(ticket, employee)
        except PermissionDenied:
            return
        await topics.relay_staff(message, tickets, settings, ticket)
