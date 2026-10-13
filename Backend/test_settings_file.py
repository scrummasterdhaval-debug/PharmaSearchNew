"""The settings file: a value pasted with stray control characters still loads whole."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config


class SettingsFileTests(unittest.TestCase):
    def load(self, text: str) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_bytes(text.encode("utf-8"))
            with patch.dict(os.environ, {}, clear=True):
                config.load_env_file(path)
                return dict(os.environ)

    def test_a_value_pasted_with_a_carriage_return_still_loads(self):
        loaded = self.load("FROM_EMAIL=sender@example.com\nBREVO_API_KEY=\rxkeysib-abc123\nOTHER=1\n")
        self.assertEqual(loaded["BREVO_API_KEY"], "xkeysib-abc123")
        self.assertEqual(loaded["FROM_EMAIL"], "sender@example.com")
        self.assertEqual(loaded["OTHER"], "1")

    def test_windows_line_endings_and_other_control_characters_are_dropped(self):
        loaded = self.load("A=one\r\nB=\x1b[200~two\x1b[201~\r\nC=\"quoted\"\r\n")
        self.assertEqual(loaded["A"], "one")
        self.assertNotIn("\x1b", loaded["B"])
        self.assertIn("two", loaded["B"])
        self.assertNotIn("\r", loaded["A"])
        self.assertEqual(loaded["C"], "quoted")

    def test_a_real_environment_variable_still_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("FROM_EMAIL=file@example.com\n", encoding="utf-8")
            with patch.dict(os.environ, {"FROM_EMAIL": "real@example.com"}, clear=True):
                config.load_env_file(path)
                self.assertEqual(os.environ["FROM_EMAIL"], "real@example.com")


if __name__ == "__main__":
    unittest.main()
