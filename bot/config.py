import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Configuration error containing only safe, hand-written diagnostics."""


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    admin_id: int
    channel_id: int | str
    database_url: str = field(default="", repr=False)
    auto_publish: bool = True
    min_text_length: int = 10
    max_text_length: int = 3000
    rate_limit_count: int = 3
    rate_limit_window_seconds: int = 600
    min_submission_interval_seconds: int = 15
    duplicate_window_seconds: int = 86400
    rejected_retention_days: int = 30
    maintenance_interval_seconds: int = 60
    moderation_rules_path: str = "./moderation_rules.json"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        required = ("BOT_TOKEN", "ADMIN_ID", "CHANNEL_ID", "DATABASE_URL")
        missing = [name for name in required if not os.getenv(name, "").strip()]
        if missing:
            raise ConfigurationError("Заполните переменные: " + ", ".join(missing))

        def integer(name: str, default: int, minimum: int = 1) -> int:
            try:
                value = int(os.getenv(name, str(default)))
            except ValueError:
                raise ConfigurationError(f"{name}: требуется целое число") from None
            if value < minimum:
                raise ConfigurationError(f"{name}: минимум {minimum}")
            return value

        channel = os.environ["CHANNEL_ID"].strip()
        if channel.startswith("@") and len(channel) > 1:
            channel_id: int | str = channel
        else:
            try:
                channel_id = int(channel)
            except ValueError:
                raise ConfigurationError("CHANNEL_ID: используйте -100… или @username") from None
            if channel_id >= 0:
                raise ConfigurationError("CHANNEL_ID: числовой ID канала должен быть отрицательным")
        auto_publish = os.getenv("AUTO_PUBLISH", "true").strip().lower()
        if auto_publish not in {"true", "false"}:
            raise ConfigurationError("AUTO_PUBLISH: допустимы true или false")
        config = cls(
            token=os.environ["BOT_TOKEN"].strip(),
            admin_id=integer("ADMIN_ID", 0),
            channel_id=channel_id,
            database_url=os.environ["DATABASE_URL"].strip(),
            auto_publish=auto_publish == "true",
            min_text_length=integer("MIN_TEXT_LENGTH", 10),
            max_text_length=integer("MAX_TEXT_LENGTH", 3000),
            rate_limit_count=integer("RATE_LIMIT_COUNT", 3),
            rate_limit_window_seconds=integer("RATE_LIMIT_WINDOW_SECONDS", 600),
            min_submission_interval_seconds=integer("MIN_SUBMISSION_INTERVAL_SECONDS", 15, 0),
            duplicate_window_seconds=integer("DUPLICATE_WINDOW_SECONDS", 86400),
            rejected_retention_days=integer("REJECTED_RETENTION_DAYS", 30),
            maintenance_interval_seconds=integer("MAINTENANCE_INTERVAL_SECONDS", 60),
            moderation_rules_path=os.getenv("MODERATION_RULES_PATH", "./moderation_rules.json"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
        if not config.min_text_length <= config.max_text_length <= 3000:
            raise ConfigurationError("Длины должны удовлетворять MIN_TEXT_LENGTH <= MAX_TEXT_LENGTH <= 3000")
        if not config.database_url.startswith(("postgresql://", "postgres://")):
            raise ConfigurationError("DATABASE_URL: требуется URL PostgreSQL")
        if config.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("Некорректный LOG_LEVEL")
        if not Path(config.moderation_rules_path).is_file():
            raise ConfigurationError("Не найден MODERATION_RULES_PATH")
        return config
