from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from support_bot.config import Config, setup_logging
from support_bot.db.session import Database
from support_bot.miniapp import start_miniapp
from support_bot.services.topics import TopicService


async def run() -> None:
    config = Config.load()
    setup_logging(config)
    db = Database(config.database_url, config.database_path)
    await db.init()
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    runner = await start_miniapp(config, db, bot, TopicService(bot, config))
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
