import _local_dir  # noqa: F401  (isolates local data; must stay first)
import configparser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from load_Api_x import read_config
from browser_publisher import BrowserSettings, status_id
from run_background import forwarder_command
import memebot


class PackagingTests(unittest.TestCase):
    def test_explorer_error_window_waits_for_user(self):
        kernel = Mock(GetConsoleProcessList=Mock(return_value=2))
        with patch('memebot.os.name', 'nt'), patch('sys.frozen', True, create=True), \
             patch('sys.argv', ['MemeBot.exe']), patch('sys.stdin', Mock(isatty=Mock(return_value=True))), \
             patch('ctypes.windll', Mock(kernel32=kernel), create=True), patch('builtins.input') as wait, \
             patch('builtins.print'):
            memebot.pause_on_error(2)
        wait.assert_called_once()

    def test_terminal_and_background_errors_never_wait_for_input(self):
        kernel = Mock(GetConsoleProcessList=Mock(return_value=3))
        with patch('memebot.os.name', 'nt'), patch('sys.frozen', True, create=True), \
             patch('sys.argv', ['MemeBot.exe']), patch('sys.stdin', Mock(isatty=Mock(return_value=True))), \
             patch('ctypes.windll', Mock(kernel32=kernel), create=True), patch('builtins.input') as wait:
            memebot.pause_on_error(2)
            with patch('sys.argv', ['MemeBot.exe', '--non-interactive']):
                memebot.pause_on_error(2)
        wait.assert_not_called()

    def test_legacy_api_file_is_never_read(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "config.ini").write_text("[Telegram]\nTelegram_API_ID=123\n", encoding="utf-8")
            Path(directory, "x Api.txt").write_bytes(b"invalid old credentials")
            with patch("load_Api_x.BASE_DIR", directory):
                config = read_config()
            self.assertFalse(config.has_section("Twitter"))
            self.assertEqual(config.getint("Telegram", "Telegram_API_ID"), 123)

    def test_check_config_needs_no_x_keys_or_browser_login(self):
        config = configparser.ConfigParser()
        config.read_dict({"Settings": {}, "Telegram": {
            "Telegram_API_ID": "123", "Telegram_API_HASH": "fake", "Telegram_CHANNEL_USERNAME": "-1001"}})
        with patch("load_Api_x.read_config", return_value=config), patch("memebot.configure_logging"), patch("sys.argv", ["MemeBot", "--check-config"]), patch("memebot.login_x") as login:
            self.assertEqual(memebot.main(), 0)
            login.assert_not_called()

    def test_browser_settings_validate_identity_and_timeouts(self):
        config = configparser.ConfigParser()
        config.read_dict({"Browser": {"expected_username": "@example_user", "channel": "msedge"}})
        self.assertEqual(BrowserSettings.from_config(config).expected_username, "example_user")
        config.set("Browser", "timeout_seconds", "0")
        with self.assertRaises(ValueError):
            BrowserSettings.from_config(config)

    def test_success_permalink_must_belong_to_expected_account_on_x(self):
        self.assertEqual(status_id("https://x.com/example_user/status/123", "EXAMPLE_USER"), "123")
        for href in ("https://evil.invalid/example_user/status/123", "/someone/status/123", "/i/status/123", "/example_user/status/no-id"):
            self.assertIsNone(status_id(href, "example_user"))

    def test_frozen_supervisor_runs_exe_without_recursive_background(self):
        with patch("sys.frozen", True, create=True), patch("sys.executable", "C:/Meme Bot/MemeBot.exe"):
            self.assertEqual(forwarder_command(), ["C:/Meme Bot/MemeBot.exe", "--non-interactive"])


if __name__ == "__main__":
    unittest.main()
