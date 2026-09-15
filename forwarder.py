"""Durable Telegram-to-X forwarding with restart recovery."""

import argparse
import asyncio
import configparser
import logging
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from contextlib import suppress

import requests
from telethon import events, utils
from telethon.errors import FloodWaitError, ServerError, RpcCallFailError

from delivery_store import AlreadyRunning, DeliveryStore, data_directory, instance_lock
from load_Api_x import BASE_DIR, create_telegram, read_config
from browser_publisher import BrowserPublisher, BrowserSettings, BrowserAttention, BrowserUnavailable
from proxy_settings import resolve_proxy
from runtime_logging import configure_logging

log = logging.getLogger(__name__)


class NeedsAttention(RuntimeError):
    """Exit code 2 stops automatic restarts until configuration is fixed."""


def failure_delay(error, failures, now=None):
    if isinstance(error, FloodWaitError):
        return max(1, error.seconds)
    return min(300, 5 * 2 ** min(max(failures - 1, 0), 6))


async def wait_signal(signal, timeout):
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(signal.wait(), timeout=max(0.05, timeout))


class Forwarder:
    def __init__(self, publisher, telegram, entity, channel_id, store):
        self.publisher = publisher
        self.telegram, self.entity = telegram, entity
        self.channel_id, self.store = channel_id, store
        self.wake = asyncio.Event()
        self.history_wake = asyncio.Event()
        self.sent_since_idle = False

    async def on_message(self, event):
        # History ingestion alone owns the durable cursor. An out-of-order live
        # update must never jump over an older message that has not been saved.
        self.history_wake.set()

    async def initialize_history(self):
        if self.store.cursor(self.channel_id) is None:
            latest = await self.telegram.get_messages(self.entity, limit=1)
            baseline = latest[0].id if latest else 0
            self.store.initialize(self.channel_id, baseline)
            log.info("First run: channel %s starts after message %s; old history is not posted.",
                     self.channel_id, baseline)

    async def sync_history(self):
        cursor = self.store.cursor(self.channel_id)
        if cursor is None:
            raise RuntimeError("History cursor has not been initialized")
        count = 0
        async for message in self.telegram.iter_messages(
                self.entity, min_id=cursor, reverse=True, limit=200):
            self.store.ingest(self.channel_id, message)
            self.wake.set()
            count += 1
        if count < 200:
            self.store.finalize_albums(self.channel_id)
            self.wake.set()
        return count

    async def watch_history(self):
        while True:
            self.history_wake.clear()
            try:
                count = await self.sync_history()
            except FloodWaitError as error:
                log.warning("Telegram history is rate limited; waiting %ss.", error.seconds)
                await asyncio.sleep(max(1, error.seconds))
                continue
            if count == 200:
                await asyncio.sleep(0)
                continue
            # Periodic reconciliation also covers missed events/reconnections.
            await wait_signal(self.history_wake, 30)

    def record_failure(self, job, error, *, publishing=False):
        key = f"{job['channel_id']}:{job['message_id']}"
        if publishing:
            # Any failure after entering submit may have followed a successful
            # click, including a lost browser connection or an unrecognized UI.
            self.store.update(job, "uncertain", error="Browser publish result unconfirmed; check X before retry")
            raise NeedsAttention(f"Delivery {key}: browser publish result is uncertain. "
                                 "Check X before --retry; forwarding stopped to prevent duplicates.") from error
        if isinstance(error, BrowserUnavailable):
            delay = failure_delay(error, job['attempts'] + 1)
            self.store.update(job, 'pending', error=error, next_attempt=time.time() + delay)
            log.warning('Delivery %s saved: browser disconnected before publishing. Reconnecting in %ss.', key, delay)
            return
        if isinstance(error, BrowserAttention):
            self.store.update(job, "failed", error=error)
            raise NeedsAttention(f"Delivery {key}: before publish click: {error}") from error
        if isinstance(error, (requests.RequestException, OSError, FloodWaitError, ServerError, RpcCallFailError)):
            delay = failure_delay(error, job["attempts"] + 1)
            self.store.update(job, "pending", error=error, next_attempt=time.time() + delay)
            log.warning("Delivery %s failed (%s); saved for retry in %.0fs",
                        key, type(error).__name__, delay)
        else:
            self.store.update(job, "failed", error=error)
            log.error("Delivery %s needs manual correction/retry: %s", key, error)

    async def publish(self, job):
        self.store.update(job, "sending")
        try:
            tweet_id = await self.publisher.submit()
            if not isinstance(tweet_id, str) or not tweet_id.isdigit():
                raise ValueError("Browser returned no valid X post ID")
        except BrowserUnavailable as error:
            # Emitted only by submit's account check, before invoking click.
            self.record_failure(job, error)
            return
        except Exception as error:
            self.record_failure(job, error, publishing=True)
            return
        # A journal failure leaves 'sending' for conservative crash recovery.
        self.store.update(job, "sent", tweet_id=tweet_id)
        self.sent_since_idle = True
        log.info("Delivery %s:%s sent as X post https://x.com/i/status/%s",
                 job["channel_id"], job["message_id"], tweet_id)

    async def process(self, job):
        members = self.store.members(job)
        if any(row['state'] != 'pending' for row in members):
            raise NeedsAttention('Album contains previously sent or unresolved media; review before retrying')
        job = dict(job, member_ids=tuple(row['message_id'] for row in members))
        self.store.update(job, "processing", attempt=True)
        try:
            with tempfile.TemporaryDirectory(prefix="forward-", dir=BASE_DIR) as directory:
                media = [row for row in members if row['media_kind']]
                if len(media) > 4:
                    raise BrowserAttention('Telegram album has more than 4 media items; review it manually to fit one X post')
                paths = []
                for member in media:
                    message = await self.telegram.get_messages(self.entity, ids=member['message_id'])
                    if message is None:
                        raise ValueError("Telegram source media was deleted or is inaccessible")
                    # Separate folders prevent repeated Telegram filenames from overwriting each other.
                    folder = Path(directory) / str(member['message_id'])
                    folder.mkdir()
                    path = await message.download_media(file=str(folder) + "/")
                    if not path:
                        raise ValueError("Telegram media download returned no file")
                    paths.append(path)
                text = '\n\n'.join(row['text'] for row in members if row['text'])
                attachments = paths if len(paths) > 1 else paths[0] if paths else None
                # Playwright operations must finish before shutdown closes the
                # browser or removes an attached media file.
                preparing = asyncio.create_task(self.publisher.prepare(text, attachments))
                try:
                    await asyncio.shield(preparing)
                except asyncio.CancelledError:
                    with suppress(Exception):
                        await preparing
                    raise
                sending = asyncio.create_task(self.publish(job))
                try:
                    await asyncio.shield(sending)
                except asyncio.CancelledError:
                    # Finish recording the outcome before closing SQLite.
                    with suppress(NeedsAttention):
                        await sending
                    raise
        except (NeedsAttention, sqlite3.Error):
            raise
        except asyncio.CancelledError:
            if self.store.get(job["channel_id"], job["message_id"])["state"] == "processing":
                self.store.update(job, "pending")
            raise
        except Exception as error:
            state = self.store.get(job["channel_id"], job["message_id"])["state"]
            if state == "processing":
                self.record_failure(job, error)
            elif state == "sending":
                self.record_failure(job, error, publishing=True)
            else:
                # Cleanup can fail after X succeeded. Never change a recorded
                # outcome back to pending merely because deleting a file failed.
                log.error("Delivery %s:%s cleanup failed; keeping state %s: %s",
                          job["channel_id"], job["message_id"], state, error)

    async def run(self):
        while True:
            self.wake.clear()
            job = self.store.next_job(self.channel_id)
            if job is None:
                if self.sent_since_idle and not self.store.has_pending(self.channel_id):
                    await self.publisher.close_idle()
                    self.sent_since_idle = False
                await wait_signal(self.wake, 30)
            elif job["next_attempt"] > time.time():
                await wait_signal(self.wake, min(30, job["next_attempt"] - time.time()))
            else:
                await self.process(job)


async def main(store, non_interactive=False):
    config = read_config()
    proxy = await asyncio.to_thread(resolve_proxy, config["Settings"])
    publisher = BrowserPublisher(BASE_DIR, BrowserSettings.from_config(config), proxy)
    telegram = create_telegram(config, proxy)
    tasks = []
    service = None
    try:
        if non_interactive:
            await telegram.connect()
            if not await telegram.is_user_authorized():
                raise NeedsAttention("Telegram login required. Run MemeBot.exe --login (or python load_Api_x.py) first.")
        else:
            await telegram.start()
        channel = config.get("Telegram", "Telegram_CHANNEL_USERNAME").strip()
        if channel.lstrip("-").isdigit():
            channel = int(channel)
        entity = await telegram.get_input_entity(channel)
        channel_id = utils.get_peer_id(entity)
        while True:
            try:
                await publisher.start(allow_login=not non_interactive)
                break
            except BrowserUnavailable:
                log.warning('Browser connection interrupted during startup; retrying in 5s.')
                await publisher.close()
                await asyncio.sleep(5)
        service = Forwarder(publisher, telegram, entity, channel_id, store)
        telegram.add_event_handler(service.on_message, events.NewMessage(chats=entity))
        await service.initialize_history()
        log.info("Listening to channel %s; saved deliveries: %s", channel_id, store.counts())
        tasks = [asyncio.create_task(service.run()), asyncio.create_task(service.watch_history()),
                 asyncio.create_task(telegram.run_until_disconnected())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        raise ConnectionError("Telegram disconnected; saved progress will resume on restart")
    finally:
        if service:
            telegram.remove_event_handler(service.on_message)
        for task in tasks:
            task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            try:
                await telegram.disconnect()
            finally:
                await publisher.close()


def delivery_key(value):
    try:
        channel, message = map(int, value.split(":"))
        if not channel or message <= 0:
            raise ValueError()
        return channel, message
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use CHANNEL_ID:MESSAGE_ID, e.g. -1001234567890:42") from error


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true", help="Show saved counts and tasks needing attention")
    action.add_argument("--retry", type=delivery_key, help="Requeue a task after checking/correcting it")
    action.add_argument("--mark-sent", type=delivery_key, help="Resolve an uncertain task already posted to X")
    parser.add_argument("--tweet-id", help="X post ID, required with --mark-sent")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()
    if args.mark_sent and not (args.tweet_id and args.tweet_id.isdigit()):
        parser.error("--mark-sent requires a numeric --tweet-id")
    state = data_directory(BASE_DIR)
    management = args.status or args.retry or args.mark_sent
    configure_logging(log_file=None if management else str(state / "forwarder.log"))
    try:
        if args.status:
            if not (state / "deliveries.sqlite3").exists():
                log.info("No saved deliveries yet. Start the forwarder to initialize its history cursor.")
                return 0
            store = DeliveryStore(state / "deliveries.sqlite3", read_only=True)
            try:
                log.info("Saved deliveries: %s", store.counts())
                for job in store.attention():
                    # Each task must remain visible; the flood filter groups
                    # equal format strings and would otherwise hide task #2.
                    log.warning(f"{job['channel_id']}:{job['message_id']} {job['state']} "
                                f"attempts={job['attempts']} error={job['last_error']}")
            finally:
                store.close()
            return 0
        with instance_lock(state / "forwarder.lock"):
            store = DeliveryStore(state / "deliveries.sqlite3")
            try:
                store.recover()
                if args.retry:
                    store.retry(*args.retry)
                    log.info("Delivery queued for next run: %s:%s", *args.retry)
                elif args.mark_sent:
                    job = store.get(*args.mark_sent)
                    if job is None or job["state"] != "uncertain":
                        raise ValueError("--mark-sent requires an uncertain delivery")
                    if any(row['state'] != 'uncertain' for row in store.members(job)):
                        raise ValueError('All album members must be uncertain before --mark-sent')
                    store.update(job, "sent", tweet_id=args.tweet_id)
                    log.info("Delivery recorded as sent: %s:%s", *args.mark_sent)
                else:
                    asyncio.run(main(store, args.non_interactive))
            finally:
                store.close()
        return 0
    except KeyboardInterrupt:
        log.info("Stopped; pending deliveries remain on disk.")
        return 0
    except (NeedsAttention, BrowserAttention, AlreadyRunning, ValueError, configparser.Error, KeyError, sqlite3.Error) as exc:
        log.error("Forwarder needs attention (%s): %s", type(exc).__name__, exc)
        return 2
    except Exception as exc:
        log.error("Forwarder stopped (%s): %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
