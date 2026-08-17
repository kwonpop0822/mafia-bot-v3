import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_DIR / ".env"
PROBE = PROJECT_DIR / "tests" / "config_probe.py"


class EnvLoadingTests(unittest.TestCase):
    def setUp(self):
        self.original_env_file = ENV_FILE.read_bytes() if ENV_FILE.exists() else None

    def tearDown(self):
        if self.original_env_file is None:
            ENV_FILE.unlink(missing_ok=True)
        else:
            ENV_FILE.write_bytes(self.original_env_file)
        data_dir = PROJECT_DIR / "test_dotenv_data"
        if data_dir.exists():
            for child in data_dir.iterdir():
                child.unlink()
            data_dir.rmdir()

    def run_probe(self, extra_env=None):
        env = os.environ.copy()
        for key in ("TELEGRAM_TOKEN", "MAFIA_DEV_PASSWORD", "MAFIA_DATA_DIR"):
            env.pop(key, None)
        if extra_env:
            env.update(extra_env)
        completed = subprocess.run(
            [sys.executable, str(PROBE)],
            cwd=PROJECT_DIR,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    def test_project_env_file_is_loaded(self):
        ENV_FILE.write_text(
            "TELEGRAM_TOKEN=token-from-dotenv\n"
            "MAFIA_DEV_PASSWORD=password-from-dotenv\n"
            "MAFIA_DATA_DIR=./test_dotenv_data\n",
            encoding="utf-8",
        )
        values = self.run_probe()
        self.assertEqual(values["token"], "token-from-dotenv")
        self.assertEqual(values["password"], "password-from-dotenv")
        self.assertEqual(values["data_dir"], str(PROJECT_DIR / "test_dotenv_data"))

    def test_operating_environment_overrides_dotenv(self):
        ENV_FILE.write_text(
            "TELEGRAM_TOKEN=token-from-dotenv\n"
            "MAFIA_DEV_PASSWORD=password-from-dotenv\n",
            encoding="utf-8",
        )
        values = self.run_probe(
            {
                "TELEGRAM_TOKEN": "token-from-environment",
                "MAFIA_DEV_PASSWORD": "password-from-environment",
            }
        )
        self.assertEqual(values["token"], "token-from-environment")
        self.assertEqual(values["password"], "password-from-environment")

    def test_example_declares_all_supported_keys(self):
        example = (PROJECT_DIR / ".env.example").read_text(encoding="utf-8")
        for key in ("TELEGRAM_TOKEN", "MAFIA_DEV_PASSWORD", "MAFIA_DATA_DIR", "LOG_LEVEL"):
            self.assertIn(f"{key}=", example)


if __name__ == "__main__":
    unittest.main()
