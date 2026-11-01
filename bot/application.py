import asyncio
import logging
from contextlib import suppress

import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from bot.config import Config, ConfigurationError
from bot.database import Database, advisory_key
from bot.handlers import create_router
from bot.moderation import Moderator
from bot.service import SubmissionService

logger = logging.getLogger(__name__)


async def verify_telegram(bot: Bot, config: Config):
    me = await bot.get_me()
    chat = await bot.get_chat(config.channel_id)
    if chat.type != ChatType.CHANNEL:
        raise ConfigurationError("CHANNEL_ID должен указывать на канал")
    membership = await bot.get_chat_member(chat.id, me.id)
    if membership.status != ChatMemberStatus.CREATOR and not (
        membership.status == ChatMemberStatus.ADMINISTRATOR and membership.can_post_messages
    ):
        raise ConfigurationError("Боту нужны права администратора канала с разрешением публикации")
    await bot.send_message(config.admin_id, "Бот запущен. Очередь: /pending. Управление: /help_admin.")
    public = [
        BotCommand(command="start", description="Отправить историю"),
        BotCommand(command="privacy", description="Как обрабатываются данные"),
    ]
    await bot.set_my_commands(public, scope=BotCommandScopeDefault())
    await bot.set_my_commands(
        public
        + [
            BotCommand(command="pending", description="Очередь модерации"),
            BotCommand(command="stats", description="Статистика"),
            BotCommand(command="help_admin", description="Управление ботом"),
        ],
        scope=BotCommandScopeChat(chat_id=config.admin_id),
    )


async def watch_lock(connection: asyncpg.Connection, dispatcher: Dispatcher):
    while True:
        await asyncio.sleep(15)
        try:
            await connection.fetchval("SELECT 1")
        except Exception:
            logger.critical("Process lock connection lost; stopping polling")
            await dispatcher.stop_polling()
            return


async def run():
    config = Config.from_env()
    logging.getLogger().setLevel(config.log_level)
    # aiogram's default failure logs may contain the raw update or API request.
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)
    database = Database(config.database_url)
    bot = Bot(config.token, default=DefaultBotProperties(parse_mode=None))
    lock_connection = None
    tasks = []
    try:
        # Keep a dedicated PostgreSQL session alive for single-instance recovery.
        lock_connection = await asyncpg.connect(config.database_url, timeout=15)
        locked = await lock_connection.fetchval("SELECT pg_try_advisory_lock($1)", advisory_key("ranhigs:poller"))
        if not locked:
            raise ConfigurationError("Другой экземпляр бота уже работает с этой базой")
        await database.open(config.auto_publish)
        moderator = Moderator(config.moderation_rules_path)
        await verify_telegram(bot, config)
        recovered = await database.recover()
        logger.info("Startup complete; recovered uncertain publications: %s", recovered)
        service = SubmissionService(bot, database, config, moderator)
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(service))
        tasks = [
            asyncio.create_task(service.maintenance()),
            asyncio.create_task(watch_lock(lock_connection, dispatcher)),
        ]
        await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"], close_bot_session=False)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await bot.session.close()
        await database.close()
        if lock_connection:
            await lock_connection.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except ConfigurationError as error:
        # Config errors are hand-written and contain no supplied values.
        logger.error("Configuration error: %s", error)
        raise SystemExit(1) from None
    except Exception as error:
        logger.error(
            "Startup/runtime failure (%s). Check PostgreSQL, bot token, channel rights and admin /start.",
            type(error).__name__,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
