"""Configuration and client factories. Importing this module makes no connections."""

import asyncio
import configparser
import logging
import os
import sys

from telethon import TelegramClient

from proxy_settings import resolve_proxy

BASE_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))


def read_config():
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(os.path.join(BASE_DIR, "config.ini"), encoding="utf-8-sig"):
        raise ValueError("config.ini was not found next to the application; Telegram settings are required")
    # Legacy Twitter sections and x Api.txt are intentionally not consumed.
    return config


def create_telegram(config, proxy=None):
    return TelegramClient(
        os.path.join(BASE_DIR, "meme_bot_session"),
        config.getint("Telegram", "Telegram_API_ID"),
        config.get("Telegram", "Telegram_API_HASH"),
        proxy=proxy.telegram() if proxy else None,
        timeout=10, connection_retries=3, request_retries=3,
        retry_delay=5, flood_sleep_threshold=60)


async def main(check_proxy_only=False):
    config = read_config()
    proxy = await asyncio.to_thread(resolve_proxy, config["Settings"])
    if check_proxy_only:
        logging.info("Proxy diagnostic complete: %s", proxy.label if proxy else "direct/system TUN")
        return
    from browser_publisher import login_x
    await login_x(BASE_DIR, config, proxy)
    telegram = create_telegram(config, proxy)
    try:
        await telegram.start()
        me = await telegram.get_me()
        logging.info("Telegram connected: %s. Run forwarder.py to start forwarding.", me.id)
    finally:
        await telegram.disconnect()


if __name__ == "__main__":
    import argparse
    from runtime_logging import configure_logging
    configure_logging()
    parser = argparse.ArgumentParser(description="Check proxy or log in to X browser and Telegram")
    parser.add_argument("--check-proxy", action="store_true", help="Detect proxy only; no login or posting")
    args = parser.parse_args()
    try:
        asyncio.run(main(check_proxy_only=args.check_proxy))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.error("Connection test stopped (%s): %s", type(exc).__name__, exc)
        sys.exit(1)
