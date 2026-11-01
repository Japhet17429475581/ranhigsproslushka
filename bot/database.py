import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import asyncpg

from bot.config import Config
from bot.moderation import ModerationResult, fingerprint


class AdmissionError(Exception):
    """User-facing rejection before a submission is accepted."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL,
    source_message_id BIGINT,
    text TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','publishing','uncertain','published','rejected')),
    moderation_status TEXT NOT NULL CHECK(moderation_status IN ('safe','review','blocked')),
    moderation_reasons JSONB NOT NULL DEFAULT '[]',
    channel_message_id BIGINT,
    publication_mode TEXT CHECK(publication_mode IN ('auto','manual')),
    admin_notified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    UNIQUE(user_id, source_message_id)
);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);
CREATE INDEX IF NOT EXISTS idx_submissions_user_id ON submissions(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_submissions_created_at ON submissions(created_at);
CREATE INDEX IF NOT EXISTS idx_submissions_fingerprint ON submissions(fingerprint, created_at);
CREATE TABLE IF NOT EXISTS banned_users (
    user_id BIGINT PRIMARY KEY,
    reason TEXT NOT NULL,
    banned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def utcnow():
    return datetime.now(timezone.utc)


def advisory_key(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big", signed=True)


class Database:
    def __init__(self, url: str):
        self._url = url
        self.pool: asyncpg.Pool | None = None

    async def open(self, default_auto_publish: bool = True):
        self.pool = await asyncpg.create_pool(self._url, min_size=1, max_size=5, command_timeout=30)
        async with self.transaction() as connection:
            await connection.execute("SELECT pg_advisory_xact_lock($1)", advisory_key("ranhigs:schema:v1"))
            await connection.execute(SCHEMA)
            await connection.execute(
                "INSERT INTO settings(key,value) VALUES('auto_publish',$1) ON CONFLICT DO NOTHING",
                "true" if default_auto_publish else "false",
            )

    async def close(self):
        if self.pool:
            await self.pool.close()
            self.pool = None

    @asynccontextmanager
    async def transaction(self):
        if self.pool is None:
            raise RuntimeError("Database is not open")
        async with self.pool.acquire() as connection, connection.transaction():
            yield connection

    async def admit(
        self, user_id: int, text: str, verdict: ModerationResult, config: Config, source_message_id: int | None = None
    ) -> int:
        digest = fingerprint(text)
        async with self.transaction() as connection:
            # Serialize admission by author and fingerprint across pool connections.
            # A consistent ordering prevents deadlocks between concurrent submissions.
            for key in sorted({advisory_key(f"user:{user_id}"), advisory_key(f"text:{digest}")}):
                await connection.execute("SELECT pg_advisory_xact_lock($1)", key)
            now = utcnow()
            if await connection.fetchval("SELECT 1 FROM banned_users WHERE user_id=$1", user_id):
                raise AdmissionError("Отправка публикаций для тебя ограничена администратором.")
            if source_message_id is not None and await connection.fetchval(
                "SELECT 1 FROM submissions WHERE user_id=$1 AND source_message_id=$2",
                user_id,
                source_message_id,
            ):
                raise AdmissionError("Это сообщение уже принято.")
            recent = await connection.fetchrow(
                "SELECT COUNT(*) AS count FROM submissions WHERE user_id=$1 AND created_at>$2",
                user_id,
                now - timedelta(seconds=config.rate_limit_window_seconds),
            )
            if recent["count"] >= config.rate_limit_count:
                raise AdmissionError("Слишком много заявок. Попробуй позже — лимит временный.")
            latest = await connection.fetchval("SELECT MAX(created_at) FROM submissions WHERE user_id=$1", user_id)
            if latest and (now - latest).total_seconds() < config.min_submission_interval_seconds:
                raise AdmissionError("Подожди немного перед следующей публикацией.")
            if await connection.fetchval(
                "SELECT 1 FROM submissions WHERE fingerprint=$1 AND created_at>$2",
                digest,
                now - timedelta(seconds=config.duplicate_window_seconds),
            ):
                raise AdmissionError("Такой текст уже был отправлен недавно. Пришли другую историю.")
            return await connection.fetchval(
                "INSERT INTO submissions(user_id,source_message_id,text,fingerprint,moderation_status,"
                "moderation_reasons) VALUES($1,$2,$3,$4,$5,$6::jsonb) RETURNING id",
                user_id,
                source_message_id,
                text,
                digest,
                verdict.status,
                json.dumps(verdict.reasons, ensure_ascii=False),
            )

    async def get(self, submission_id: int):
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow("SELECT * FROM submissions WHERE id=$1", submission_id)
            return dict(row) if row else None

    async def auto_publish(self) -> bool:
        async with self.pool.acquire() as connection:
            return await connection.fetchval("SELECT value FROM settings WHERE key='auto_publish'") == "true"

    async def set_auto_publish(self, enabled: bool):
        async with self.pool.acquire() as connection:
            await connection.execute(
                "UPDATE settings SET value=$1 WHERE key='auto_publish'", "true" if enabled else "false"
            )

    async def claim_publish(self, submission_id: int, mode: str) -> bool:
        async with self.transaction() as connection:
            user_id = await connection.fetchval("SELECT user_id FROM submissions WHERE id=$1", submission_id)
            if user_id is None:
                return False
            await connection.execute("SELECT pg_advisory_xact_lock($1)", advisory_key(f"user:{user_id}"))
            setting = await connection.fetchval("SELECT value FROM settings WHERE key='auto_publish' FOR SHARE")
            if mode == "auto" and setting != "true":
                return False
            return (
                await connection.fetchval(
                    "UPDATE submissions SET status='publishing',publication_mode=$1,"
                    "reviewed_at=CASE WHEN $1='manual' THEN NOW() ELSE NULL END "
                    "WHERE id=$2 AND status='pending' "
                    "AND NOT EXISTS(SELECT 1 FROM banned_users WHERE user_id=$3) "
                    "AND ($1='manual' OR moderation_status='safe') RETURNING id",
                    mode,
                    submission_id,
                    user_id,
                )
                is not None
            )

    async def finish_publish(self, submission_id: int, message_id: int) -> bool:
        async with self.pool.acquire() as connection:
            return (
                await connection.fetchval(
                    "UPDATE submissions SET status='published',channel_message_id=$1,published_at=NOW() "
                    "WHERE id=$2 AND status='publishing' RETURNING id",
                    message_id,
                    submission_id,
                )
                is not None
            )

    async def publication_failed(self, submission_id: int, uncertain: bool):
        reason = (
            "Результат отправки неизвестен — проверьте канал"
            if uncertain
            else "Telegram отклонил отправку — проверьте права и лимиты"
        )
        async with self.pool.acquire() as connection:
            await connection.execute(
                "UPDATE submissions SET status=$1,moderation_reasons=moderation_reasons || $2::jsonb,"
                "admin_notified=FALSE WHERE id=$3 AND status='publishing'",
                "uncertain" if uncertain else "pending",
                json.dumps([reason], ensure_ascii=False),
                submission_id,
            )

    async def recover(self) -> int:
        async with self.pool.acquire() as connection:
            result = await connection.execute(
                "UPDATE submissions SET status='uncertain',admin_notified=FALSE WHERE status='publishing'"
            )
            return int(result.split()[-1])

    async def pending(self, page: int = 0, limit: int = 5, unnotified: bool = False):
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT * FROM submissions WHERE status IN ('pending','uncertain') "
                "AND (NOT $1::boolean OR NOT admin_notified) ORDER BY id LIMIT $2 OFFSET $3",
                unnotified,
                limit,
                page * limit,
            )
            return [dict(row) for row in rows]

    async def mark_notified(self, submission_id: int):
        async with self.pool.acquire() as connection:
            await connection.execute("UPDATE submissions SET admin_notified=TRUE WHERE id=$1", submission_id)

    async def reject(self, submission_id: int, ban: bool = False):
        async with self.transaction() as connection:
            user_id = await connection.fetchval("SELECT user_id FROM submissions WHERE id=$1", submission_id)
            if user_id is None:
                return None
            await connection.execute("SELECT pg_advisory_xact_lock($1)", advisory_key(f"user:{user_id}"))
            row = await connection.fetchrow(
                "UPDATE submissions SET status='rejected',reviewed_at=NOW() "
                "WHERE id=$1 AND status='pending' RETURNING *",
                submission_id,
            )
            if row and ban:
                await self._ban(connection, user_id, "Блокировка при модерации")
            return dict(row) if row else None

    async def _ban(self, connection, user_id: int, reason: str):
        await connection.execute(
            "INSERT INTO banned_users(user_id,reason) VALUES($1,$2) "
            "ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason,banned_at=NOW()",
            user_id,
            reason,
        )
        await connection.execute(
            "UPDATE submissions SET status='rejected',reviewed_at=NOW() WHERE user_id=$1 AND status='pending'",
            user_id,
        )

    async def ban(self, user_id: int, reason: str = "Блокировка администратором"):
        async with self.transaction() as connection:
            await connection.execute("SELECT pg_advisory_xact_lock($1)", advisory_key(f"user:{user_id}"))
            await self._ban(connection, user_id, reason)

    async def unban(self, user_id: int):
        async with self.transaction() as connection:
            await connection.execute("SELECT pg_advisory_xact_lock($1)", advisory_key(f"user:{user_id}"))
            await connection.execute("DELETE FROM banned_users WHERE user_id=$1", user_id)

    async def resolve(self, submission_id: int, message_id: int | None) -> bool:
        async with self.pool.acquire() as connection:
            if message_id is not None:
                return (
                    await connection.fetchval(
                        "UPDATE submissions SET status='published',channel_message_id=$1,published_at=NOW(),"
                        "reviewed_at=NOW(),publication_mode=COALESCE(publication_mode,'manual') "
                        "WHERE id=$2 AND status='uncertain' RETURNING id",
                        message_id,
                        submission_id,
                    )
                    is not None
                )
            return (
                await connection.fetchval(
                    "UPDATE submissions SET status='pending',admin_notified=FALSE WHERE id=$1 AND status='uncertain' RETURNING id",
                    submission_id,
                )
                is not None
            )

    async def purge_rejected(self, days: int) -> int:
        async with self.pool.acquire() as connection:
            result = await connection.execute(
                "DELETE FROM submissions WHERE status='rejected' AND reviewed_at<=$1",
                utcnow() - timedelta(days=days),
            )
            return int(result.split()[-1])

    async def stats(self):
        async with self.pool.acquire() as connection:
            return dict(
                await connection.fetchrow("""SELECT COUNT(*) AS total,
                COUNT(*) FILTER(WHERE status='published' AND publication_mode='auto') AS auto,
                COUNT(*) FILTER(WHERE status='published' AND publication_mode='manual') AS manual,
                COUNT(*) FILTER(WHERE status='rejected') AS rejected,
                COUNT(*) FILTER(WHERE status='pending') AS pending,
                COUNT(*) FILTER(WHERE status IN ('publishing','uncertain')) AS uncertain,
                COUNT(*) FILTER(WHERE created_at >= NOW() - INTERVAL '1 day') AS today,
                (SELECT COUNT(*) FROM banned_users) AS banned FROM submissions""")
            )
