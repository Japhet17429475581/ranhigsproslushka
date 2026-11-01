import asyncio
import html
import json
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import LinkPreviewOptions

from bot.config import Config
from bot.database import AdmissionError, Database
from bot.formatting import channel_text, moderation_keyboard, split_text
from bot.moderation import Moderator, canonical_text

logger = logging.getLogger(__name__)
PUBLISHED = "✅ Готово! Твоя история опубликована в канале"
QUEUED = "🕒 Твоя история отправлена на модерацию. После проверки она может появиться в канале"
APPROVED = "✅ Твоя публикация одобрена и появилась в канале!"
REJECTED = "❌ Публикация не прошла модерацию. Попробуй изменить текст и отправить заново"
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class SubmissionService:
    def __init__(self, bot: Bot, database: Database, config: Config, moderator: Moderator):
        self.bot = bot
        self.db = database
        self.config = config
        self.moderator = moderator
        self._notification_lock = asyncio.Lock()

    async def submit(
        self, user_id: int, text: str, source_message_id: int | None = None, entity_risk: bool = False
    ) -> tuple[int, str]:
        text = text.strip()
        if "\x00" in text:
            raise AdmissionError("В тексте есть неподдерживаемые управляющие символы. Пришли обычный текст.")
        visible = canonical_text(text).strip()
        if not self.config.min_text_length <= len(visible) <= self.config.max_text_length:
            raise AdmissionError(
                f"Пришли текст длиной от {self.config.min_text_length} до {self.config.max_text_length} символов."
            )
        # Validate UTF-16 size before persisting/claiming, including formatting overhead.
        try:
            channel_text(text)
        except (ValueError, UnicodeError):
            raise AdmissionError(
                "Слишком длинное сообщение для Telegram. Сократи текст или количество emoji."
            ) from None
        verdict = self.moderator.check(text, entity_risk=entity_risk)
        submission_id = await self.db.admit(user_id, text, verdict, self.config, source_message_id)
        if verdict.status == "safe" and await self.db.auto_publish():
            result = await self.publish(submission_id, manual=False)
            if result == "published":
                return submission_id, PUBLISHED
        await self.notify_admin(submission_id)
        return submission_id, QUEUED

    async def publish(self, submission_id: int, manual: bool = True) -> str:
        if not await self.db.claim_publish(submission_id, "manual" if manual else "auto"):
            row = await self.db.get(submission_id)
            return row["status"] if row else "missing"
        row = await self.db.get(submission_id)
        try:
            sent = await self.bot.send_message(
                self.config.channel_id,
                channel_text(row["text"]),
                parse_mode="HTML",
                link_preview_options=NO_PREVIEW,
            )
        except (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter):
            # Explicit Bot API rejection means the channel post was not created.
            await self.db.publication_failed(submission_id, uncertain=False)
            logger.warning("Publication rejected by Telegram for submission %s", submission_id)
            await self.notify_admin(submission_id)
            return "pending"
        except Exception:
            # Timeout, disconnect or 5xx can happen AFTER Telegram creates the post.
            # Never retry automatically; only a human can reconcile the channel.
            await self.db.publication_failed(submission_id, uncertain=True)
            logger.warning("Publication result unknown for submission %s", submission_id)
            await self.notify_admin(submission_id)
            return "uncertain"
        # If this write fails, keep 'publishing'; restart recovery marks it uncertain.
        await self.db.finish_publish(submission_id, sent.message_id)
        if manual:
            await self.notify_user(row["user_id"], APPROVED)
        return "published"

    async def notify_user(self, user_id: int, text: str):
        try:
            await self.bot.send_message(user_id, text, parse_mode=None)
        except TelegramAPIError:
            # A blocked bot must not roll back publication or repeat it.
            logger.warning("User notification could not be delivered")

    async def notify_admin(self, submission_id: int, force: bool = False):
        async with self._notification_lock:
            row = await self.db.get(submission_id)
            if not row or row["status"] not in {"pending", "uncertain"} or (row["admin_notified"] and not force):
                return
            reasons = list(dict.fromkeys(json.loads(row["moderation_reasons"])))
            reason = "; ".join(reasons) or "Автопубликация выключена или заявка восстановлена после перезапуска"
            header = (
                f"<b>🚨 Новая заявка на модерацию</b>\n\nЗаявка: #{submission_id}\n"
                f"Автор (только администратору): <code>{row['user_id']}</code>\n"
                f"Проверка: {row['moderation_status']}\nПричина: {html.escape(reason[:750])}\n\n"
            )
            if row["status"] == "uncertain":
                header += (
                    "<b>⚠️ Результат отправки неизвестен. Сначала проверьте канал.</b>\n"
                    f"Если пост есть: /resolve {submission_id} MESSAGE_ID\n"
                    f"Если поста нет: /retry {submission_id}\n\n"
                )
            chunks = split_text(row["text"], 2400)
            try:
                for index, chunk in enumerate(chunks):
                    await self.bot.send_message(
                        self.config.admin_id,
                        (header if index == 0 else f"Заявка #{submission_id}, продолжение:\n\n") + html.escape(chunk),
                        parse_mode="HTML",
                        link_preview_options=NO_PREVIEW,
                        protect_content=True,
                        reply_markup=moderation_keyboard(submission_id)
                        if row["status"] == "pending" and index == len(chunks) - 1
                        else None,
                    )
                await self.db.mark_notified(submission_id)
            except TelegramAPIError:
                logger.warning("Admin notification deferred for submission %s", submission_id)

    async def reject(self, submission_id: int, ban: bool = False) -> bool:
        row = await self.db.reject(submission_id, ban=ban)
        if not row:
            return False
        await self.notify_user(row["user_id"], "Отправка публикаций ограничена администратором." if ban else REJECTED)
        return True

    async def maintenance_once(self):
        await self.db.purge_rejected(self.config.rejected_retention_days)
        for row in await self.db.pending(limit=10, unnotified=True):
            await self.notify_admin(row["id"])

    async def maintenance(self):
        while True:
            try:
                await self.maintenance_once()
            except Exception as error:
                logger.error("Maintenance failed (%s)", type(error).__name__)
            await asyncio.sleep(self.config.maintenance_interval_seconds)
