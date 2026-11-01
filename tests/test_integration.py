import asyncio
import os
import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage

from bot.config import Config
from bot.database import AdmissionError, Database, utcnow
from bot.moderation import ModerationResult, Moderator
from bot.service import SubmissionService


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL is required for PostgreSQL integration tests")
class PostgreSQLIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        self.db = Database(self.url)
        await self.db.open()
        # The test URL MUST point to a dedicated disposable database.
        async with self.db.pool.acquire() as connection:
            await connection.execute("TRUNCATE submissions, banned_users RESTART IDENTITY")
        await self.db.set_auto_publish(True)
        self.config = Config(
            token="123456:TEST",
            admin_id=999,
            channel_id=-100123,
            database_url=self.url,
            min_submission_interval_seconds=0,
        )
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
        self.service = SubmissionService(self.bot, self.db, self.config, Moderator("moderation_rules.json"))

    async def asyncTearDown(self):
        await self.db.close()

    def channel_calls(self):
        return [call for call in self.bot.send_message.call_args_list if call.args[0] == self.config.channel_id]

    async def submit(self, text="Сегодня на паре было очень весело!", user_id=123):
        return await self.service.submit(user_id, text)

    async def test_safe_autopublication(self):
        submission_id, response = await self.submit()
        self.assertIn("опубликована", response)
        row = await self.db.get(submission_id)
        self.assertEqual((row["status"], row["publication_mode"], row["channel_message_id"]), ("published", "auto", 42))
        self.assertEqual(len(self.channel_calls()), 1)
        self.assertNotIn("123", self.channel_calls()[0].args[1])

    async def test_suspicious_and_blocked_go_to_admin(self):
        for text in ["Это какой-то хyй написал на доске", "Я тебя убью после пары"]:
            submission_id, response = await self.submit(text)
            row = await self.db.get(submission_id)
            self.assertEqual(row["status"], "pending")
            self.assertIn("модерацию", response)
        self.assertFalse(self.channel_calls())
        self.assertEqual(self.bot.send_message.call_args.args[0], self.config.admin_id)
        self.assertTrue(self.bot.send_message.call_args.kwargs["reply_markup"])

    async def test_manual_approval_and_duplicate_clicks(self):
        await self.db.set_auto_publish(False)
        submission_id, _ = await self.submit()
        await asyncio.gather(*(self.service.publish(submission_id) for _ in range(10)))
        self.assertEqual(len(self.channel_calls()), 1)
        self.assertEqual((await self.db.get(submission_id))["publication_mode"], "manual")

    async def test_blocked_user_and_pending_queue(self):
        await self.db.set_auto_publish(False)
        submission_id, _ = await self.submit()
        await self.service.reject(submission_id, ban=True)
        with self.assertRaises(AdmissionError):
            await self.submit("Другая история на тему нашего общежития")
        self.assertEqual(await self.service.publish(submission_id), "rejected")
        await self.db.unban(123)
        await self.submit("После снятия блокировки могу отправить историю")

    async def test_rate_limit_and_concurrent_admission(self):
        results = await asyncio.gather(
            *(self.submit(f"Моя новая история под номером {index}") for index in range(8)),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, tuple) for result in results), 3)
        self.assertEqual(sum(isinstance(result, AdmissionError) for result in results), 5)
        self.assertEqual((await self.db.stats())["banned"], 0)

    async def test_global_duplicate_and_update_idempotency(self):
        results = await asyncio.gather(*(self.submit(user_id=user_id) for user_id in range(6)), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, tuple) for result in results), 1)
        verdict = ModerationResult("safe")
        await self.db.admit(777, "Первая отдельная история", verdict, self.config, source_message_id=1)
        with self.assertRaises(AdmissionError):
            await self.db.admit(777, "Другая отдельная история", verdict, self.config, source_message_id=1)

    async def test_minimum_interval(self):
        self.service.config = replace(self.config, min_submission_interval_seconds=15)
        await self.submit()
        with self.assertRaises(AdmissionError):
            await self.submit("Другая уникальная история отправлена слишком быстро")

    async def test_restart_preserves_queue_and_settings(self):
        await self.db.set_auto_publish(False)
        submission_id, _ = await self.submit()
        await self.db.close()
        await self.db.open(default_auto_publish=True)
        self.assertFalse(await self.db.auto_publish())
        self.assertEqual((await self.db.pending())[0]["id"], submission_id)

    async def test_crash_during_publication_becomes_uncertain(self):
        await self.db.set_auto_publish(False)
        submission_id, _ = await self.submit()
        self.assertTrue(await self.db.claim_publish(submission_id, "manual"))
        await self.db.close()
        await self.db.open()
        self.assertEqual(await self.db.recover(), 1)
        self.assertEqual(await self.service.publish(submission_id), "uncertain")
        self.assertFalse(self.channel_calls())
        self.assertTrue(await self.db.resolve(submission_id, 145))
        self.assertEqual((await self.db.get(submission_id))["channel_message_id"], 145)

    async def test_timeout_after_send_does_not_repeat(self):
        async def send(chat_id, *args, **kwargs):
            if chat_id == self.config.channel_id:
                raise TimeoutError
            return SimpleNamespace(message_id=42)

        self.bot.send_message.side_effect = send
        submission_id, response = await self.submit()
        self.assertIn("модерацию", response)
        self.assertEqual((await self.db.get(submission_id))["status"], "uncertain")
        await self.service.publish(submission_id)
        self.assertEqual(len(self.channel_calls()), 1)
        self.assertIsNone(self.bot.send_message.call_args.kwargs["reply_markup"])

    async def test_explicit_telegram_failure_is_retryable_by_admin(self):
        async def send(chat_id, *args, **kwargs):
            if chat_id == self.config.channel_id:
                raise TelegramBadRequest(method=SendMessage(chat_id=chat_id, text="test"), message="no rights")
            return SimpleNamespace(message_id=42)

        self.bot.send_message.side_effect = send
        submission_id, _ = await self.submit()
        self.assertEqual((await self.db.get(submission_id))["status"], "pending")
        self.bot.send_message.side_effect = None
        self.assertEqual(await self.service.publish(submission_id), "published")
        self.assertEqual(len(self.channel_calls()), 2)

    async def test_admin_delivery_retried_by_worker(self):
        await self.db.set_auto_publish(False)
        self.bot.send_message.side_effect = TelegramForbiddenError(
            method=SendMessage(chat_id=999, text="test"),
            message="blocked",
        )
        submission_id, _ = await self.submit()
        self.assertFalse((await self.db.get(submission_id))["admin_notified"])
        self.bot.send_message.side_effect = None
        await self.service.maintenance_once()
        self.assertTrue((await self.db.get(submission_id))["admin_notified"])

    async def test_database_failure_after_send_never_republishes(self):
        original = self.db.finish_publish
        self.db.finish_publish = AsyncMock(side_effect=asyncpg.PostgresConnectionError)
        with self.assertRaises(asyncpg.PostgresConnectionError):
            await self.submit()
        self.db.finish_publish = original
        self.assertEqual((await self.db.get(1))["status"], "publishing")
        await self.db.recover()
        await self.service.publish(1)
        self.assertEqual(len(self.channel_calls()), 1)

    async def test_retention_and_stats(self):
        await self.db.set_auto_publish(False)
        submission_id, _ = await self.submit()
        await self.service.reject(submission_id)
        async with self.db.pool.acquire() as connection:
            await connection.execute(
                "UPDATE submissions SET reviewed_at=$1 WHERE id=$2", utcnow() - timedelta(days=31), submission_id
            )
        self.assertEqual((await self.db.stats())["rejected"], 1)
        self.assertEqual(await self.db.purge_rejected(30), 1)
        self.assertIsNone(await self.db.get(submission_id))

    async def test_banned_author_cannot_approve_existing_submission(self):
        await self.db.set_auto_publish(False)
        submission_id, _ = await self.submit()
        await self.db.ban(123)
        self.assertEqual(await self.service.publish(submission_id), "rejected")
        self.assertFalse(self.channel_calls())

    async def test_size_and_hidden_entities(self):
        for text in ["Коротко", "Я" * 3001, "🥰" * 3000, "Текст с нулевым символом \x00"]:
            with self.assertRaises(AdmissionError):
                await self.submit(text)
        submission_id, _ = await self.service.submit(123, "Здесь ссылка скрыта в оформлении", entity_risk=True)
        self.assertEqual((await self.db.get(submission_id))["status"], "pending")
