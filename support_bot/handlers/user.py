from __future__ import annotations

import html
from datetime import datetime

from aiogram import F, Router
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from support_bot.config import Config
from support_bot.db.session import Database
from support_bot.services.employees import EmployeeService
from support_bot.services.interface import InterfaceService
from support_bot.services.settings import SettingsService
from support_bot.services.tickets import ActiveTicketExists, TicketService
from support_bot.services.topics import TopicService

router = Router(name="user")


class TicketForm(StatesGroup):
    category = State()
    description = State()
    media = State()
    confirm = State()


class ReviewForm(StatesGroup):
    comment = State()


def interface_key(message: Message) -> str:
    return f"user:{message.chat.id}"


async def clear_interface(message: Message, db: Database) -> None:
    async with db.sessions() as session:
        await InterfaceService(session).clear(interface_key(message), message.bot)


async def send_interface(
    message: Message,
    db: Database,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
    *,
    clear: bool = True,
) -> Message:
    async with db.sessions() as session:
        interface = InterfaceService(session)
        if clear:
            await interface.clear(interface_key(message), message.bot)
        sent = await message.answer(text, reply_markup=keyboard)
        await interface.track(interface_key(message), message.chat.id, sent.message_id)
    return sent


USER_HELP = (
    "<b>Центр поддержки</b>\n\n"
    "<b>Создание обращения</b>\n"
    "Нажмите «Написать в поддержку», выберите категорию, опишите проблему и при необходимости добавьте вложение.\n\n"
    "<b>Диалог</b>\n"
    "Пишите в этот чат. Текст, фото, документы и ответы на сообщения попадут оператору. "
    "Вопросы из FAQ не попадают в обращение.\n\n"
    "<b>Изменение сообщений</b>\n"
    "При редактировании сообщения его копия у оператора также изменится. "
    "Для удаления копии ответьте на своё сообщение командой /delete.\n\n"
    "<b>Завершение</b>\n"
    "После закрытия можно поставить оценку и оставить комментарий. "
    "Чтобы продолжить решение той же проблемы, нажмите «Переоткрыть обращение».\n\n"
    "<b>Команды</b>\n"
    "/start: показать частые вопросы и действия\n"
    "/help: показать эту справку\n"
    "/skip: продолжить создание обращения без вложения\n"
    "/delete: удалить копию своего сообщения у оператора, команда отправляется ответом"
)


def faq_keyboard(config: Config) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=item["question"], callback_data=f"faq:{item['id']}")]
        for item in config.faq
    ]
    rows.append([InlineKeyboardButton(text="Как работает поддержка", callback_data="help:user")])
    rows.append([InlineKeyboardButton(text=config.message("contact"), callback_data="contact")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart(), StateFilter("*"), F.chat.type == "private")
async def start(message: Message, state: FSMContext, config: Config, db: Database) -> None:
    await state.clear()
    await send_interface(message, db, config.message("start"), faq_keyboard(config))


@router.message(Command("help"), F.chat.type == "private")
async def user_help(message: Message, config: Config, db: Database) -> None:
    if message.from_user.id == config.admin_id:
        from support_bot.handlers.admin import admin_help

        await admin_help(message)
        return
    await send_interface(message, db, USER_HELP, faq_keyboard(config))


@router.callback_query(F.data == "help:user", F.message.chat.type == "private")
async def user_help_button(callback: CallbackQuery, config: Config, db: Database) -> None:
    await send_interface(callback.message, db, USER_HELP, faq_keyboard(config))
    await callback.answer()


@router.callback_query(F.data == "menu:main", F.message.chat.type == "private")
async def main_menu_button(callback: CallbackQuery, config: Config, db: Database) -> None:
    await send_interface(callback.message, db, config.message("menu"), faq_keyboard(config))
    await callback.answer()


@router.callback_query(F.data.startswith("faq:"), F.message.chat.type == "private")
async def show_faq(callback: CallbackQuery, config: Config, db: Database) -> None:
    faq_id = callback.data.split(":", 1)[1]
    item = next((entry for entry in config.faq if entry["id"] == faq_id), None)
    if item:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩ Вернуться к вопросам", callback_data="menu:main")],
            [InlineKeyboardButton(text=config.message("contact"), callback_data="contact")],
        ])
        await send_interface(
            callback.message,
            db,
            f"<b>{html.escape(item['question'])}</b>\n\n{html.escape(item['answer'])}",
            keyboard,
        )
    await callback.answer()


@router.callback_query(F.data == "contact", F.message.chat.type == "private")
async def contact(callback: CallbackQuery, state: FSMContext, config: Config, db: Database) -> None:
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        active = await tickets.active_for_user(callback.from_user.id, await settings.closed_keys())
        if active:
            await clear_interface(callback.message, db)
            await callback.message.answer(f"У вас уже есть открытое обращение №{active.number}. Можно продолжить переписку здесь.")
            await callback.answer()
            return
        categories = await settings.categories()
    await state.clear()
    await state.set_state(TicketForm.category)
    await state.set_data({"offered_categories": categories})
    buttons = [
        [InlineKeyboardButton(text=category, callback_data=f"category:{index}")]
        for index, category in enumerate(categories)
    ]
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="ticket:cancel")])
    await send_interface(
        callback.message, db, config.message("choose_category"), InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@router.callback_query(TicketForm.category, F.data.startswith("category:"))
async def choose_category(callback: CallbackQuery, state: FSMContext, config: Config, db: Database) -> None:
    data = await state.get_data()
    try:
        category = data["offered_categories"][int(callback.data.split(":", 1)[1])]
    except (ValueError, IndexError, KeyError):
        await callback.answer("Категория устарела, начните форму заново", show_alert=True)
        return
    await state.update_data(category=category)
    await state.set_state(TicketForm.description)
    await send_interface(callback.message, db, config.message("describe"))
    await callback.answer()


@router.message(TicketForm.description, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def enter_description(message: Message, state: FSMContext, config: Config, db: Database) -> None:
    description = message.text.strip()
    if not 3 <= len(description) <= 4000:
        await message.answer("Описание должно содержать от 3 до 4000 символов.")
        return
    await state.update_data(description=description)
    await state.set_state(TicketForm.media)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Продолжить без вложения", callback_data="media:skip")],
            [InlineKeyboardButton(text="Отмена", callback_data="ticket:cancel")],
        ]
    )
    await send_interface(message, db, config.message("add_media"), keyboard)


@router.message(TicketForm.media, F.chat.type == "private", Command("skip"))
@router.callback_query(TicketForm.media, F.data == "media:skip")
async def skip_media(event: Message | CallbackQuery, state: FSMContext, config: Config, db: Database) -> None:
    await state.update_data(media_file_id=None, media_type=None, media_message_id=None)
    await show_confirmation(event, state, config, db)
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.message(TicketForm.media, F.chat.type == "private")
async def attach_media(message: Message, state: FSMContext, config: Config, db: Database) -> None:
    media = {
        ContentType.PHOTO: (message.photo[-1].file_id if message.photo else None),
        ContentType.VIDEO: message.video.file_id if message.video else None,
        ContentType.DOCUMENT: message.document.file_id if message.document else None,
        ContentType.ANIMATION: message.animation.file_id if message.animation else None,
        ContentType.AUDIO: message.audio.file_id if message.audio else None,
        ContentType.VOICE: message.voice.file_id if message.voice else None,
        ContentType.VIDEO_NOTE: message.video_note.file_id if message.video_note else None,
    }
    file_id = media.get(message.content_type)
    if not file_id:
        await message.answer("Отправьте фото, видео, файл, аудио или нажмите «Продолжить без вложения».")
        return
    await state.update_data(
        media_file_id=file_id, media_type=str(message.content_type), media_message_id=message.message_id
    )
    await show_confirmation(message, state, config, db)


async def show_confirmation(event: Message | CallbackQuery, state: FSMContext, config: Config, db: Database) -> None:
    data = await state.get_data()
    await state.set_state(TicketForm.confirm)
    text = (
        f"{config.message('confirm')}\n\n"
        f"Категория: {html.escape(data['category'])}\n"
        f"Описание: {html.escape(data['description'])}\n"
        f"Вложение: {'есть' if data.get('media_file_id') else 'нет'}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отправить обращение", callback_data="ticket:confirm")],
            [InlineKeyboardButton(text="Отмена", callback_data="ticket:cancel")],
        ]
    )
    target = event.message if isinstance(event, CallbackQuery) else event
    await send_interface(target, db, text, keyboard)


@router.callback_query(TicketForm.confirm, F.data == "ticket:confirm")
async def create_ticket(
    callback: CallbackQuery, state: FSMContext, config: Config, db: Database, topics: TopicService
) -> None:
    data = await state.get_data()
    if not data.get("category") or not data.get("description"):
        await callback.answer("Форма устарела. Начните обращение заново.", show_alert=True)
        return
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        employees = EmployeeService(session, config.admin_id)
        mode = await settings.get("assignment_mode", "manual")
        assigned = None
        if mode == "schedule":
            on_duty = await employees.on_duty(datetime.now(config.timezone))
            assigned = on_duty[0] if on_duty else None
        closed_statuses = await settings.closed_keys()
        ticket = await tickets.active_for_user(callback.from_user.id, closed_statuses)
        if ticket is None and not assigned and await settings.topic("requests") is None:
            await callback.answer("Бот ещё не настроен. Обратитесь к администратору.", show_alert=True)
            return
        created = False
        if ticket is None:
            try:
                ticket = await tickets.create(
                    user_id=callback.from_user.id,
                    username=callback.from_user.username,
                    display_name=callback.from_user.full_name,
                    category=data["category"],
                    description=data["description"],
                    media_file_id=data.get("media_file_id"),
                    media_type=data.get("media_type"),
                    closed_statuses=closed_statuses,
                )
                created = True
            except ActiveTicketExists:
                ticket = await tickets.active_for_user(callback.from_user.id, closed_statuses)
                if ticket is None:
                    raise
        assignee = ticket.employee or assigned
        try:
            if assignee:
                if ticket.employee_id is None:
                    await tickets.assign(ticket, assignee)
                await topics.create_ticket_topic(
                    tickets, settings, ticket, assignee, data.get("media_message_id")
                )
            else:
                await topics.publish_request(tickets, settings, ticket, data.get("media_message_id"))
        except (TelegramAPIError, RuntimeError):
            canceled = await tickets.cancel_creation(ticket)
            if canceled:
                text = "Не удалось создать обращение. Попробуйте отправить форму ещё раз."
            else:
                await tickets.session.commit()
                text = (
                    f"Обращение №{ticket.number} сохранено, но публикация не завершена. "
                    "Нажмите «Отправить обращение» ещё раз."
                )
            await callback.message.answer(text)
            await callback.answer()
            return
        should_greet = created and assignee is not None and await settings.get("auto_greeting", True)
        if created and mode == "schedule" and not assignee:
            await callback.bot.send_message(
                config.support_group_id,
                f"Обращение #{ticket.number} ожидает принятия: сейчас нет сотрудника на смене.",
                message_thread_id=await settings.topic("requests"),
            )
    await clear_interface(callback.message, db)
    await state.clear()
    await callback.message.answer(config.message("ticket_created", number=ticket.number))
    if created and assignee:
        await callback.message.answer(config.message("ticket_accepted", number=ticket.number))
        if should_greet:
            await topics.greet(ticket, assignee)
    await callback.answer()


@router.callback_query(F.data == "ticket:cancel", StateFilter("*"))
async def cancel_ticket(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    current = await state.get_state()
    if current and current.startswith("TicketForm:"):
        await clear_interface(callback.message, db)
        await state.clear()
        await callback.message.answer("Создание обращения отменено.")
    await callback.answer()


@router.callback_query(F.data.startswith("rate:"))
async def rate_ticket(callback: CallbackQuery, state: FSMContext, config: Config, db: Database) -> None:
    try:
        _, raw_number, raw_rating = callback.data.split(":")
        number, rating = int(raw_number), int(raw_rating)
    except (ValueError, TypeError):
        await callback.answer()
        return
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.get(number)
        if not ticket or ticket.user_id != callback.from_user.id or not await settings.is_closed(ticket.status):
            await callback.answer("Оценка недоступна для этого обращения", show_alert=True)
            return
        await tickets.add_review(ticket, rating)
    await state.set_state(ReviewForm.comment)
    await state.set_data({"ticket_number": number, "rating": rating})
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Без комментария", callback_data=f"review:skip:{number}")]]
    )
    await send_interface(
        callback.message,
        db,
        "Спасибо! Можете оставить необязательный комментарий одним сообщением.",
        keyboard,
        clear=False,
    )
    await callback.answer("Оценка сохранена")


async def finish_feedback(message: Message, state: FSMContext, config: Config, db: Database) -> None:
    data = await state.get_data()
    async with db.sessions() as session:
        await InterfaceService(session).clear(f"rating:{data['ticket_number']}", message.bot)
    await state.clear()
    text = f"{config.message('feedback_done')}\n\n{config.message('menu')}"
    await send_interface(message, db, text, faq_keyboard(config))


@router.callback_query(ReviewForm.comment, F.data.startswith("review:skip:"))
async def skip_review_comment(callback: CallbackQuery, state: FSMContext, config: Config, db: Database) -> None:
    await finish_feedback(callback.message, state, config, db)
    await callback.answer()


@router.message(ReviewForm.comment, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def review_comment(
    message: Message, state: FSMContext, config: Config, db: Database, topics: TopicService
) -> None:
    data = await state.get_data()
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.get(int(data["ticket_number"]))
        if not ticket or ticket.user_id != message.from_user.id:
            await state.clear()
            await message.answer("Обращение не найдено.")
            return
        if not await settings.is_closed(ticket.status):
            await state.clear()
            await topics.relay_user(message, tickets, settings, ticket)
            return
        await tickets.add_review(ticket, int(data["rating"]), message.text[:4000])
    await finish_feedback(message, state, config, db)


@router.callback_query(F.data.startswith("reopen:"))
async def reopen_ticket(
    callback: CallbackQuery, state: FSMContext, config: Config, db: Database, topics: TopicService
) -> None:
    try:
        number = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.get(number)
        if not ticket or ticket.user_id != callback.from_user.id:
            await callback.answer("Обращение не найдено", show_alert=True)
            return
        if ticket.deleted_at:
            await callback.message.answer("Архивный топик удалён. Создайте новое обращение.")
            await callback.answer()
            return
        if not await settings.is_closed(ticket.status):
            await callback.answer("Обращение уже открыто")
            return
        try:
            await topics.reopen(tickets, settings, ticket)
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
    await clear_interface(callback.message, db)
    await state.clear()
    await callback.message.answer(config.message("reopened", number=number))
    await callback.answer()


@router.edited_message(F.chat.type == "private")
async def edit_user_message(message: Message, config: Config, db: Database, topics: TopicService) -> None:
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.active_for_user(message.from_user.id, await settings.closed_keys())
        if not ticket:
            return
        link = await tickets.linked_message(ticket, message.chat.id, message.message_id)
        if not link or link.source_chat_id != message.chat.id:
            return
        if await topics.relay_edit(message, link):
            tickets.update_message_metadata(link, message)
            await session.commit()


@router.message(Command("delete"), F.chat.type == "private")
async def delete_user_message(message: Message, config: Config, db: Database, topics: TopicService) -> None:
    if not message.reply_to_message:
        await message.answer("Ответьте командой /delete на своё сообщение, которое нужно удалить у оператора.")
        return
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.active_for_user(message.from_user.id, await settings.closed_keys())
        if not ticket:
            return
        link = await tickets.linked_message(ticket, message.chat.id, message.reply_to_message.message_id)
        if not link or link.source_chat_id != message.chat.id:
            await message.answer("Можно удалять только свои сообщения, отправленные в открытое обращение.")
            return
        await topics.delete_relayed(link, delete_source=False)
        await session.delete(link)
        await session.commit()
    await message.answer("Сообщение удалено у оператора. Своё исходное сообщение удалите в Telegram вручную.")


RELAY_TYPES = {
    ContentType.TEXT,
    ContentType.PHOTO,
    ContentType.VIDEO,
    ContentType.DOCUMENT,
    ContentType.ANIMATION,
    ContentType.AUDIO,
    ContentType.VOICE,
    ContentType.VIDEO_NOTE,
    ContentType.STICKER,
    ContentType.CONTACT,
    ContentType.LOCATION,
    ContentType.VENUE,
    ContentType.POLL,
}


@router.message(StateFilter(None), F.chat.type == "private")
async def relay_user_message(message: Message, config: Config, db: Database, topics: TopicService) -> None:
    if message.from_user.is_bot or message.content_type not in RELAY_TYPES:
        return
    if (message.text or message.caption or "").startswith("/"):
        return
    async with db.sessions() as session:
        tickets = TicketService(session, config.admin_id)
        settings = SettingsService(session, config.defaults)
        ticket = await tickets.active_for_user(message.from_user.id, await settings.closed_keys())
        if not ticket:
            await message.answer(config.message("start"), reply_markup=faq_keyboard(config))
            return
        await topics.relay_user(message, tickets, settings, ticket)
