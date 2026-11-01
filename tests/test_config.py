import os
import unittest
from unittest.mock import patch

from bot.config import Config, ConfigurationError


class ConfigTests(unittest.TestCase):
    def load(self, **overrides):
        values = {
            "BOT_TOKEN": "test-token",
            "ADMIN_ID": "123",
            "CHANNEL_ID": "-100123",
            "DATABASE_URL": "postgresql://test:secret@localhost/test",
            **overrides,
        }
        with patch.dict(os.environ, values, clear=True), patch("bot.config.load_dotenv"):
            return Config.from_env()

    def test_valid_config_and_secret_redaction(self):
        config = self.load()
        self.assertEqual(config.channel_id, -100123)
        self.assertTrue(config.auto_publish)
        self.assertNotIn("test-token", repr(config))
        self.assertNotIn("secret", repr(config))

    def test_missing_required_variables(self):
        with self.assertRaisesRegex(ConfigurationError, "DATABASE_URL"):
            self.load(DATABASE_URL="")

    def test_invalid_values(self):
        for values in [
            {"ADMIN_ID": "0"},
            {"CHANNEL_ID": "123"},
            {"AUTO_PUBLISH": "yes"},
            {"MAX_TEXT_LENGTH": "5000"},
            {"RATE_LIMIT_COUNT": "0"},
            {"DATABASE_URL": "invalid"},
        ]:
            with self.subTest(values=values), self.assertRaises(ConfigurationError):
                self.load(**values)

    def test_username_channel_and_manual_mode(self):
        config = self.load(CHANNEL_ID="@ranhigs_channel", AUTO_PUBLISH="false")
        self.assertEqual(config.channel_id, "@ranhigs_channel")
        self.assertFalse(config.auto_publish)
