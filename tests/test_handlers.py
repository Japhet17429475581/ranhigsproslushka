import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage
from aiogram.types import Update

from bot.handlers import create_router


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 -- aiogram BaseSession API
        self.calls.append(method)
        if isinstance(method, SendMessage):
            from aiogram.types import Message

            return Message(
                message_id=500 + len(self.calls),
                date=datetime.now(timezone.utc),
                chat={"id": method.chat_id, "type": "private"},
                text=method.text,
            )
        return True

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = RecordingSession()
        self.bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk", session=self.session)
        self.db = SimpleNamespace(
            ban=AsyncMock(), set_auto_publish=AsyncMock(), get=AsyncMock(return_value={"status": "published"})
        )
        self.service = SimpleNamespace(
            config=SimpleNamespace(admin_id=999, rejected_retention_days=30),
            db=self.db,
            submit=AsyncMock(return_value=(1, "Принято")),
            publish=AsyncMock(return_value="published"),
            reject=AsyncMock(return_value=True),
        )
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(create_router(self.service))

    async def asyncTearDown(self):
        await self.bot.session.close()

    async def message(self, text=None, user_id=123, chat_type="private", **extra):
        raw = {
            "message_id": 1,
            "date": 1700000000,
            "chat": {"id": user_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            **extra,
        }
        if text is not None:
            raw["text"] = text
        await self.dispatcher.feed_update(self.bot, Update.model_validate({"update_id": 1, "message": raw}))

    async def callback(self, data, user_id=123):
        await self.dispatcher.feed_update(
            self.bot,
            Update.model_validate(
                {
                    "update_id": 2,
                    "callback_query": {
                        "id": "cb",
                        "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                        "chat_instance": "instance",
                        "data": data,
                        "message": {
                            "message_id": 5,
                            "date": 1700000000,
                            "chat": {"id": user_id, "type": "private"},
                            "text": "Card",
                        },
                    },
                }
            ),
        )

    async def test_admin_commands_are_denied(self):
        await self.message("/ban 456")
        self.db.ban.assert_not_awaited()
        self.service.submit.assert_not_awaited()
        self.assertIn("только администратору", self.session.calls[-1].text)

    async def test_admin_command_works(self):
        await self.message("/ban 456", user_id=999)
        self.db.ban.assert_awaited_once_with(456)

    async def test_admin_callback_denied(self):
        await self.callback("mod:publish:1")
        self.service.publish.assert_not_awaited()
        self.assertIsInstance(self.session.calls[-1], AnswerCallbackQuery)
        self.assertTrue(self.session.calls[-1].show_alert)

    async def test_admin_callback_works_and_removes_buttons(self):
        await self.callback("mod:publish:1", user_id=999)
        self.service.publish.assert_awaited_once_with(1)
        self.assertTrue(any(isinstance(call, EditMessageReplyMarkup) for call in self.session.calls))

    async def test_start_and_submit_button(self):
        await self.message("/start")
        self.assertEqual(self.session.calls[-1].reply_markup.inline_keyboard[0][0].callback_data, "submit")
        await self.callback("submit")
        self.assertIn("Напиши историю", self.session.calls[-1].text)
        self.service.submit.assert_not_awaited()

    async def test_unknown_command_not_published(self):
        await self.message("/unknown")
        self.service.submit.assert_not_awaited()

    async def test_unsupported_media_not_submitted(self):
        await self.message(
            sticker={
                "file_id": "sticker",
                "file_unique_id": "unique",
                "type": "regular",
                "width": 100,
                "height": 100,
                "is_animated": False,
                "is_video": False,
            }
        )
        self.assertIn("только текстовые", self.session.calls[-1].text)
        self.service.submit.assert_not_awaited()

    async def test_group_messages_are_ignored(self):
        await self.message("Обычная история в чужой группе", chat_type="group")
        self.service.submit.assert_not_awaited()
        self.assertEqual(self.session.calls, [])

    async def test_hidden_link_entities_are_flagged(self):
        text = "Вот ссылка на расписание занятий"
        await self.message(
            text, entities=[{"type": "text_link", "offset": 4, "length": 6, "url": "https://example.com"}]
        )
        self.service.submit.assert_awaited_once_with(123, text, 1, True)
