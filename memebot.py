"""Single executable entry point for forwarding and local diagnostics."""

import asyncio
import logging
import os
import sys

import forwarder
import load_Api_x
import run_background
from browser_publisher import BrowserSettings, BrowserAttention, login_x
from proxy_settings import resolve_proxy
from runtime_logging import configure_logging
from manual_browser import open_browser
from delivery_store import data_directory, instance_lock


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--background":
        return run_background.main()
    if len(sys.argv) == 2 and sys.argv[1] in ("--check-config", "--check-proxy", "--login", "--login-x", "--open-browser"):
        configure_logging()
        try:
            if sys.argv[1] == "--check-config":
                config = load_Api_x.read_config()
                for section, keys in {
                    "Telegram": ("Telegram_API_ID", "Telegram_API_HASH", "Telegram_CHANNEL_USERNAME"),
                }.items():
                    for key in keys:
                        if not config.get(section, key).strip():
                            raise ValueError(f"Missing {section}.{key}")
                config.getint("Telegram", "Telegram_API_ID")
                config["Settings"]
                BrowserSettings.from_config(config)
                logging.info("Configuration OK. X publishing uses a local browser; no X API credentials required. No connections made.")
            elif sys.argv[1] == "--open-browser":
                config = load_Api_x.read_config()
                with instance_lock(data_directory(load_Api_x.BASE_DIR) / "forwarder.lock"):
                    open_browser(load_Api_x.BASE_DIR, BrowserSettings.from_config(config))
            elif sys.argv[1] == "--login-x":
                async def browser_login():
                    config = load_Api_x.read_config()
                    proxy = await asyncio.to_thread(resolve_proxy, config["Settings"])
                    await login_x(load_Api_x.BASE_DIR, config, proxy)
                asyncio.run(browser_login())
            else:
                asyncio.run(load_Api_x.main(check_proxy_only=sys.argv[1] == "--check-proxy"))
            return 0
        except KeyboardInterrupt:
            return 0
        except BrowserAttention as error:
            logging.error("Browser needs attention: %s", error)
            return 2
        except ValueError as error:
            logging.error("Check failed: %s", error)
            return 2
        except Exception as error:
            # ConfigParser errors can contain raw input; do not print their contents.
            logging.error("Check failed (%s). Check config.ini and browser login.", type(error).__name__)
            return 2
    return forwarder.cli()


def pause_on_error(code):
    """Retain an Explorer-launched console; never block BATs or the supervisor."""
    if not code or os.name != 'nt' or not getattr(sys, 'frozen', False) or len(sys.argv) != 1:
        return
    if not sys.stdin or not sys.stdin.isatty():
        return
    try:
        import ctypes
        processes = (ctypes.c_ulong * 8)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(processes, 8)
        if 1 <= count <= 2:
            print('\nMemeBot stopped. Details: state\\forwarder.log')
            input('Press Enter to close this window...')
    except (EOFError, OSError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    code = main()
    pause_on_error(code)
    sys.exit(code)
