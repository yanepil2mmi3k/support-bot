from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatMember,
    CallbackQuery,
    ErrorEvent,
    Message,
)

from support_bot.config import Config, ConfigError, setup_logging
from support_bot.db.session import Database
from support_bot.db.storage import SqliteStorage
from support_bot.handlers import admin_router, edit_router, staff_router, user_router
from support_bot.jobs import BackgroundJobs
from support_bot.miniapp import start_miniapp
from support_bot.services.employees import EmployeeService
from support_bot.services.topics import TopicService

logger = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="start", description="Показать частые вопросы и действия"),
    BotCommand(command="help", description="Показать справку"),
    BotCommand(command="delete", description="Удалить копию сообщения у оператора"),
]
STAFF_COMMANDS = [
    BotCommand(command="help", description="Показать справку сотрудника"),
    BotCommand(command="ticket", description="Показать команды тикета"),
    BotCommand(command="edit", description="Открыть меню действий тикета"),
    BotCommand(command="profile", description="Открыть профиль сотрудника"),
    BotCommand(command="status", description="Показать или сменить статус"),
    BotCommand(command="note", description="Изменить описание в закрепе"),
    BotCommand(command="description", description="Изменить описание в закрепе"),
    BotCommand(command="rename", description="Изменить название топика"),
    BotCommand(command="transfer", description="Передать тикет сотруднику"),
    BotCommand(command="remind", description="Задать напоминание"),
    BotCommand(command="remind_off", description="Отключить напоминание"),
    BotCommand(command="pin", description="Обновить закреп"),
    BotCommand(command="close", description="Закрыть и архивировать тикет"),
    BotCommand(command="open", description="Переоткрыть тикет"),
    BotCommand(command="delete", description="Удалить сообщение в топике и у клиента"),
]
ADMIN_COMMANDS = STAFF_COMMANDS + [
    BotCommand(command="admin", description="Открыть панель администратора"),
    BotCommand(command="setup", description="Зарегистрировать системные топики"),
    BotCommand(command="report", description="Отправить отчёт в «Админ-инфа»"),
    BotCommand(command="reviews", description="Показать отзывы"),
    BotCommand(command="archive", description="Открыть архив"),
    BotCommand(command="reset_counter", description="Установить счётчик тикетов"),
]
ADMIN_PRIVATE_COMMANDS = [
    BotCommand(command="start", description="Показать начальное сообщение"),
    BotCommand(command="help", description="Показать справку"),
    BotCommand(command="profile", description="Открыть профиль сотрудника"),
    BotCommand(command="admin", description="Открыть панель администратора"),
    BotCommand(command="delete", description="Удалить копию сообщения у оператора"),
]


async def configure_commands(bot: Bot, config: Config) -> None:
    await bot.set_my_commands(USER_COMMANDS)
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(STAFF_COMMANDS, scope=BotCommandScopeChat(chat_id=config.support_group_id))
    await bot.set_my_commands(
        ADMIN_COMMANDS,
        scope=BotCommandScopeChatMember(chat_id=config.support_group_id, user_id=config.admin_id),
    )
    await bot.set_my_commands(ADMIN_PRIVATE_COMMANDS, scope=BotCommandScopeChat(chat_id=config.admin_id))


async def run() -> None:
    config = Config.load()
    setup_logging(config)
    db = Database(config.database_url, config.database_path)
    await db.init()
    bot = Bot(
        config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    topics = TopicService(bot, config)
    dispatcher = Dispatcher(storage=SqliteStorage(db))
    dispatcher.workflow_data.update(config=config, db=db, topics=topics)
    dispatcher.include_routers(admin_router, edit_router, staff_router, user_router)

    @dispatcher.error()
    async def handle_error(event: ErrorEvent) -> bool:
        logger.error("Ошибка обработки обновления", exc_info=event.exception)
        try:
            if isinstance(event.update.message, Message):
                await event.update.message.answer("Не удалось выполнить действие. Попробуйте ещё раз или сообщите администратору.")
            elif isinstance(event.update.callback_query, CallbackQuery):
                await event.update.callback_query.answer("Действие не выполнено", show_alert=True)
        except Exception:
            logger.exception("Не удалось отправить сообщение об ошибке")
        return True

    jobs = BackgroundJobs(config, db, topics)
    miniapp_runner = await start_miniapp(config, db, bot, topics)
    try:
        async with db.sessions() as session:
            await EmployeeService(session, config.admin_id).ensure_admin()
        await configure_commands(bot, config)
        await bot.delete_webhook(drop_pending_updates=False)
        jobs.start()
        logger.info("Бот поддержки запущен")
        await dispatcher.start_polling(bot, handle_signals=True)
    finally:
        await jobs.stop()
        await miniapp_runner.cleanup()
        await bot.session.close()
        await db.close()
        logger.info("Бот поддержки остановлен")


def main() -> None:
    try:
        asyncio.run(run())
    except ConfigError as exc:
        raise SystemExit(f"Ошибка конфигурации: {exc}") from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
