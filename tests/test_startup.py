import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import GetChat, SendMessage, SetMyCommands

from bot.application import safe_telegram_reason, verify_telegram
from bot.config import Config, ConfigurationError


class TelegramStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Config(token="test-token", admin_id=123, channel_id=-100456)
        self.bot = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(id=99)),
            get_chat=AsyncMock(return_value=SimpleNamespace(id=-100456, type="channel")),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator", can_post_messages=True)),
            send_message=AsyncMock(),
            set_my_commands=AsyncMock(),
        )

    async def test_valid_startup(self):
        await verify_telegram(self.bot, self.config)
        self.bot.get_chat_member.assert_awaited_once_with(-100456, 99)
        self.assertEqual(self.bot.set_my_commands.await_count, 2)

    async def test_channel_not_found_has_actionable_diagnosis(self):
        self.bot.get_chat.side_effect = TelegramBadRequest(
            method=GetChat(chat_id=-100456),
            message="Bad Request: chat not found",
        )
        with self.assertRaises(ConfigurationError) as result:
            await verify_telegram(self.bot, self.config)
        self.assertIn("getChat(channel)", str(result.exception))
        self.assertIn("chat not found", str(result.exception))
        self.assertIn("CHANNEL_ID", str(result.exception))
        self.bot.send_message.assert_not_awaited()

    async def test_admin_not_started_does_not_prevent_polling(self):
        self.bot.send_message.side_effect = TelegramBadRequest(
            method=SendMessage(chat_id=123, text="startup"),
            message="Bad Request: chat not found",
        )
        with self.assertLogs("bot.application", level="WARNING") as logs:
            await verify_telegram(self.bot, self.config)
        self.assertIn("sendMessage(admin)", " ".join(logs.output))
        self.assertIn("ADMIN_ID", " ".join(logs.output))
        self.assertEqual(self.bot.set_my_commands.await_count, 2)

    async def test_blocked_admin_does_not_prevent_polling(self):
        self.bot.send_message.side_effect = TelegramForbiddenError(
            method=SendMessage(chat_id=123, text="startup"),
            message="Forbidden: bot was blocked by the user",
        )
        with self.assertLogs("bot.application", level="WARNING"):
            await verify_telegram(self.bot, self.config)

    async def test_menu_failure_is_nonfatal(self):
        self.bot.set_my_commands.side_effect = TelegramBadRequest(
            method=SetMyCommands(commands=[]),
            message="Bad Request: chat not found",
        )
        with self.assertLogs("bot.application", level="WARNING") as logs:
            await verify_telegram(self.bot, self.config)
        self.assertIn("setMyCommands(admin)", " ".join(logs.output))

    async def test_wrong_chat_type_and_missing_rights_are_fatal(self):
        self.bot.get_chat.return_value.type = "supergroup"
        with self.assertRaisesRegex(ConfigurationError, "канал"):
            await verify_telegram(self.bot, self.config)
        self.bot.get_chat.return_value.type = "channel"
        self.bot.get_chat_member.return_value.can_post_messages = False
        with self.assertRaisesRegex(ConfigurationError, "разрешением публикации"):
            await verify_telegram(self.bot, self.config)

    def test_api_details_are_never_logged(self):
        error = TelegramBadRequest(method=GetChat(chat_id=-100456), message="secret-token raw-text -100456")
        self.assertEqual(safe_telegram_reason(error), "TelegramBadRequest")
        error.message = "chat not found secret-token raw-text -100456"
        self.assertEqual(safe_telegram_reason(error), "TelegramBadRequest: chat not found")
