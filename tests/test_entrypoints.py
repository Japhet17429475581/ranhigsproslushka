import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class EntrypointTests(unittest.TestCase):
    def run_without_configuration(self, command, cwd):
        environment = os.environ.copy()
        for name in ("BOT_TOKEN", "ADMIN_ID", "CHANNEL_ID", "DATABASE_URL"):
            environment[name] = ""
        environment["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Configuration error:", result.stderr)
        self.assertIn("BOT_TOKEN", result.stderr)
        self.assertNotIn("No module named", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_explicit_entrypoint_outside_working_directory(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            self.run_without_configuration([sys.executable, str(root / "main.py")], directory)

    def test_module_entrypoint(self):
        root = Path(__file__).resolve().parents[1]
        self.run_without_configuration([sys.executable, "-m", "bot"], root)
