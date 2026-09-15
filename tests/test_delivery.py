import asyncio
import configparser
import os
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest
import subprocess
import sys
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import requests

from delivery_store import DeliveryStore, instance_lock
from forwarder import Forwarder, NeedsAttention, cli, main
from run_background import supervise
from browser_publisher import BrowserAttention, BrowserUnavailable


def message(id=1, text="hello", media=False):
    return SimpleNamespace(id=id, message=text, photo=media, video=False)


class StoreTests(unittest.TestCase):
    def test_abrupt_process_exit_preserves_queue_and_sending_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.sqlite3"
            script = """
import os, sys
from types import SimpleNamespace
from delivery_store import DeliveryStore
store = DeliveryStore(sys.argv[1])
store.initialize(-1001, 0)
store.ingest(-1001, SimpleNamespace(id=1, message='text', photo=False, video=False))
store.update(store.get(-1001, 1), 'sending')
os._exit(9)
"""
            result = subprocess.run([sys.executable, "-c", script, str(path)], check=False)
            self.assertEqual(result.returncode, 9)
            store = DeliveryStore(path)
            try:
                store.recover()
                self.assertEqual(store.cursor(-1001), 1)
                self.assertEqual(store.get(-1001, 1)["state"], "uncertain")
            finally:
                store.close()

    def test_crash_recovery_and_completed_state_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.sqlite3"
            store = DeliveryStore(path)
            store.initialize(-1001, 0)
            for i in range(1, 5):
                store.ingest(-1001, message(i))
            store.update(store.get(-1001, 1), "processing")
            store.update(store.get(-1001, 2), "sending")
            store.update(store.get(-1001, 3), "sent", tweet_id="123")
            store.update(store.get(-1001, 4), "pending", next_attempt=9999999999)
            store.close()
            store = DeliveryStore(path)
            try:
                store.recover()
                self.assertEqual(store.cursor(-1001), 4)
                self.assertEqual(store.get(-1001, 1)["state"], "pending")
                self.assertEqual(store.get(-1001, 2)["state"], "uncertain")
                self.assertEqual(store.get(-1001, 3)["tweet_id"], "123")
                self.assertEqual(store.get(-1001, 4)["next_attempt"], 9999999999)
            finally:
                store.close()

    def test_ingestion_replay_preserves_job_lifecycle(self):
        store = DeliveryStore(":memory:")
        self.addCleanup(store.close)
        store.initialize(-1001, 0)
        store.ingest(-1001, message())
        store.update(store.get(-1001, 1), "sent", tweet_id="123")
        store.ingest(-1001, message())
        self.assertEqual(store.counts(), {"sent": 1})
        self.assertEqual(store.get(-1001, 1)["tweet_id"], "123")

    def test_failed_transaction_does_not_advance_cursor(self):
        store = DeliveryStore(":memory:")
        self.addCleanup(store.close)
        store.initialize(-1001, 0)
        store.db.execute("""CREATE TRIGGER fail_insert BEFORE INSERT ON deliveries
                            BEGIN SELECT RAISE(ABORT, 'simulated disk error'); END""")
        with self.assertRaises(Exception):
            store.ingest(-1001, message())
        self.assertEqual(store.cursor(-1001), 0)

    def test_queue_handles_more_than_old_memory_limit(self):
        store = DeliveryStore(":memory:")
        self.addCleanup(store.close)
        store.initialize(-1001, 0)
        for i in range(1, 251):
            store.ingest(-1001, message(i))
        self.assertEqual(store.counts(), {"pending": 250})
        self.assertEqual(store.next_job(-1001)["message_id"], 1)

    def test_manual_retry_does_not_reset_sent_jobs(self):
        store = DeliveryStore(":memory:")
        self.addCleanup(store.close)
        store.initialize(-1001, 0)
        store.ingest(-1001, message())
        store.update(store.get(-1001, 1), "uncertain")
        store.retry(-1001, 1)
        self.assertEqual(store.get(-1001, 1)["state"], "pending")
        store.update(store.get(-1001, 1), "sent", tweet_id="1")
        with self.assertRaises(ValueError):
            store.retry(-1001, 1)

    def test_instance_lock_releases_on_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "instance.lock"
            with instance_lock(path):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with instance_lock(path):
                        pass
            with instance_lock(path):
                pass


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = DeliveryStore(":memory:")
        self.addCleanup(self.store.close)
        self.store.initialize(-1001, 0)
        self.publisher = Mock(prepare=AsyncMock(), submit=AsyncMock(return_value="999"),
                              start=AsyncMock(), close=AsyncMock(), close_idle=AsyncMock())
        self.telegram = Mock()
        self.service = Forwarder(self.publisher, self.telegram, "entity", -1001, self.store)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base_patch = patch("forwarder.BASE_DIR", self.directory.name)
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)

    def add_job(self, id=1, media=False):
        self.store.ingest(-1001, message(id, media=media))
        return self.store.get(-1001, id)

    async def test_success_records_id_only_after_submit_and_persists_sending_before_click(self):
        async def submit():
            self.assertEqual(self.store.get(-1001, 1)["state"], "sending")
            return "999"
        self.publisher.submit.side_effect = submit
        await self.service.process(self.add_job())
        self.assertEqual(self.store.get(-1001, 1)["tweet_id"], "999")
        self.assertEqual(self.store.get(-1001, 1)["state"], "sent")
        self.publisher.prepare.assert_awaited_once_with("hello", None)

    async def test_browser_closes_once_after_entire_batch_is_durably_sent(self):
        self.add_job(1)
        self.add_job(2)
        async def close_idle():
            self.assertEqual(self.store.counts(), {'sent': 2})
            self.assertEqual(self.publisher.submit.await_count, 2)
        self.publisher.close_idle.side_effect = close_idle
        with patch('forwarder.wait_signal', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.service.run()
        self.publisher.close_idle.assert_awaited_once()

    async def test_browser_stays_open_while_an_album_is_still_collecting(self):
        await self.service.process(self.add_job())
        self.store.ingest(-1001, SimpleNamespace(id=2, message='album', photo=True, video=False, grouped_id=42))
        self.assertIsNone(self.store.next_job(-1001))
        with patch('forwarder.wait_signal', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.service.run()
        self.publisher.close_idle.assert_not_awaited()

    async def test_journal_failure_after_publish_does_not_close_browser_or_requeue(self):
        job = self.add_job()
        update = self.store.update
        def fail_sent(job, state, **kwargs):
            if state == 'sent':
                raise sqlite3.OperationalError('disk full')
            return update(job, state, **kwargs)
        with patch.object(self.store, 'update', side_effect=fail_sent):
            with self.assertRaises(sqlite3.OperationalError):
                await self.service.process(job)
        self.assertEqual(self.store.get(-1001, 1)['state'], 'sending')
        self.assertFalse(self.service.sent_since_idle)
        self.publisher.close_idle.assert_not_awaited()

    async def test_publish_timeout_is_saved_for_review_and_stops_worker(self):
        self.publisher.submit.side_effect = BrowserAttention("no permalink")
        with self.assertRaises(NeedsAttention):
            await self.service.process(self.add_job())
        self.assertEqual(self.store.get(-1001, 1)["state"], "uncertain")
        self.assertIsNone(self.store.next_job(-1001))
        self.publisher.submit.assert_awaited_once()
        self.publisher.close_idle.assert_not_awaited()

    async def test_closed_browser_during_prepare_requeues_without_stopping(self):
        self.publisher.prepare.side_effect = BrowserUnavailable('tab closed before click')
        await self.service.process(self.add_job())
        job = self.store.get(-1001, 1)
        self.assertEqual(job['state'], 'pending')
        self.assertGreater(job['next_attempt'], 0)
        self.publisher.submit.assert_not_awaited()
        self.publisher.prepare.side_effect = None
        await self.service.process(job)
        self.assertEqual(self.store.get(-1001, 1)['state'], 'sent')
        self.publisher.submit.assert_awaited_once()

    async def test_closed_browser_at_preclick_check_can_retry_safely(self):
        self.publisher.submit.side_effect = BrowserUnavailable('closed before click')
        await self.service.process(self.add_job())
        self.assertEqual(self.store.get(-1001, 1)['state'], 'pending')

    async def test_connection_loss_after_click_is_never_retried(self):
        self.publisher.submit.side_effect = ConnectionError('lost after click')
        with self.assertRaises(NeedsAttention):
            await self.service.process(self.add_job())
        self.assertEqual(self.store.get(-1001, 1)['state'], 'uncertain')
        self.assertIsNone(self.store.next_job(-1001))

    async def test_cleanup_error_cannot_requeue_successfully_sent_message(self):
        context = Mock()
        context.__enter__ = Mock(return_value=self.directory.name)
        context.__exit__ = Mock(side_effect=PermissionError("file busy"))
        with patch("forwarder.tempfile.TemporaryDirectory", return_value=context):
            await self.service.process(self.add_job())
        self.assertEqual(self.store.get(-1001, 1)["state"], "sent")
        self.assertIsNone(self.store.next_job(-1001))

    async def test_persisted_retry_deadline_is_obeyed_by_worker(self):
        job = self.add_job()
        self.store.update(job, "pending", next_attempt=9999999999)
        with patch("forwarder.wait_signal", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.service.run()
        self.publisher.submit.assert_not_called()

    async def test_history_flood_wait_is_respected(self):
        from telethon.errors import FloodWaitError
        self.service.sync_history = AsyncMock(side_effect=FloodWaitError(None, capture=120))
        with patch("forwarder.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await self.service.watch_history()
        sleep.assert_awaited_once_with(120)

    async def test_composer_failure_preserves_job_and_stops_without_clicking(self):
        self.publisher.prepare.side_effect = BrowserAttention("login required")
        first = self.add_job()
        self.add_job(2)
        with self.assertRaises(NeedsAttention):
            await self.service.process(first)
        self.assertEqual(self.store.get(-1001, 1)["state"], "failed")
        self.assertEqual(self.store.next_job(-1001)["message_id"], 2)
        self.publisher.submit.assert_not_awaited()

    async def test_media_download_failure_retries_without_posting(self):
        self.telegram.get_messages = AsyncMock(side_effect=OSError("offline"))
        await self.service.process(self.add_job(media=True))
        self.assertEqual(self.store.get(-1001, 1)["state"], "pending")
        self.publisher.submit.assert_not_called()

    async def test_upload_failure_stops_and_cleans_media(self):
        paths = []
        async def download(file):
            path = Path(file) / "image.jpg"
            path.write_bytes(b"fake")
            paths.append(path)
            return str(path)
        self.telegram.get_messages = AsyncMock(return_value=SimpleNamespace(download_media=download))
        self.publisher.prepare.side_effect = BrowserAttention("upload blocked")
        with self.assertRaises(NeedsAttention):
            await self.service.process(self.add_job(media=True))
        self.assertEqual(self.store.get(-1001, 1)["state"], "failed")
        self.assertFalse(paths[0].exists())
        self.publisher.submit.assert_not_called()

    async def test_deleted_media_is_retained_as_failed(self):
        self.telegram.get_messages = AsyncMock(return_value=None)
        await self.service.process(self.add_job(media=True))
        self.assertEqual(self.store.get(-1001, 1)["state"], "failed")
        self.publisher.submit.assert_not_called()

    async def test_shutdown_finishes_recording_inflight_success(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def post():
            started.set()
            await release.wait()
            return "999"
        self.publisher.submit.side_effect = post
        task = asyncio.create_task(self.service.process(self.add_job()))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        self.assertEqual(self.store.get(-1001, 1)["state"], "sending")
        release.set()
        with suppress(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.get(-1001, 1)["state"], "sent")

    async def test_shutdown_during_prepare_never_clicks_publish(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def prepare(*args):
            started.set()
            await release.wait()
        self.publisher.prepare.side_effect = prepare
        task = asyncio.create_task(self.service.process(self.add_job()))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with suppress(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.get(-1001, 1)["state"], "pending")
        self.publisher.submit.assert_not_awaited()

    async def test_new_message_with_same_content_is_not_filtered(self):
        await self.service.process(self.add_job(1))
        await self.service.process(self.add_job(2))
        self.assertEqual(self.publisher.submit.call_count, 2)

    async def test_startup_history_baseline_does_not_dump_all_old_messages(self):
        self.telegram.get_messages = AsyncMock(return_value=[message(90)])
        other_service = Forwarder(self.publisher, self.telegram, "new", -1002, self.store)
        await other_service.initialize_history()
        self.assertEqual(self.store.cursor(-1002), 90)
        self.assertEqual(self.store.counts(), {})

    async def test_restart_ingests_offline_history_from_saved_cursor(self):
        self.store.ingest(-1001, message(5))
        async def history(*args, **kwargs):
            self.assertEqual(kwargs["min_id"], 5)
            self.assertTrue(kwargs["reverse"])
            yield message(6)
            yield message(8)
        self.telegram.iter_messages = history
        self.assertEqual(await self.service.sync_history(), 2)
        self.assertEqual(self.store.cursor(-1001), 8)
        self.assertEqual(self.store.counts(), {"pending": 3})

    async def test_live_update_does_not_jump_history_cursor(self):
        await self.service.on_message(message(100))
        self.assertEqual(self.store.cursor(-1001), 0)
        self.assertTrue(self.service.history_wake.is_set())

    async def test_history_resume_after_partial_page_failure(self):
        async def broken_history(*args, **kwargs):
            yield message(1)
            raise ConnectionError("disconnected")
        self.telegram.iter_messages = broken_history
        with self.assertRaises(ConnectionError):
            await self.service.sync_history()
        self.assertEqual(self.store.cursor(-1001), 1)
        self.assertEqual(self.store.counts(), {"pending": 1})

    async def test_invalid_channel_fails_once_and_cleans_up(self):
        config = configparser.ConfigParser()
        config.read_dict({"Settings": {"proxy_mode": "off"},
                          "Telegram": {"Telegram_CHANNEL_USERNAME": "invalid"}})
        telegram = Mock(start=AsyncMock(), disconnect=AsyncMock(),
                        get_input_entity=AsyncMock(side_effect=ValueError("invalid channel")))
        with patch("forwarder.read_config", return_value=config), \
             patch("forwarder.resolve_proxy", return_value=None), \
             patch("forwarder.BrowserPublisher", return_value=self.publisher), \
             patch("forwarder.create_telegram", return_value=telegram):
            with self.assertRaisesRegex(ValueError, "invalid channel"):
                await main(self.store)
        telegram.add_event_handler.assert_not_called()
        telegram.get_input_entity.assert_awaited_once()
        telegram.disconnect.assert_awaited_once()

    async def test_background_login_does_not_prompt(self):
        config = configparser.ConfigParser()
        config.read_dict({"Settings": {"proxy_mode": "off"}})
        telegram = Mock(connect=AsyncMock(), disconnect=AsyncMock(),
                        is_user_authorized=AsyncMock(return_value=False))
        with patch("forwarder.read_config", return_value=config), \
             patch("forwarder.resolve_proxy", return_value=None), \
             patch("forwarder.BrowserPublisher", return_value=self.publisher), \
             patch("forwarder.create_telegram", return_value=telegram):
            with self.assertRaises(NeedsAttention):
                await main(self.store, non_interactive=True)
        telegram.start.assert_not_called()

    async def test_startup_login_continues_directly_into_normal_workflow(self):
        config = configparser.ConfigParser()
        config.read_dict({'Settings': {}, 'Telegram': {'Telegram_CHANNEL_USERNAME': '-1001'}})
        for background in (False, True):
            telegram = Mock(start=AsyncMock(), connect=AsyncMock(), disconnect=AsyncMock(),
                            is_user_authorized=AsyncMock(return_value=True),
                            get_input_entity=AsyncMock(return_value='entity'),
                            run_until_disconnected=AsyncMock())
            service = Mock(initialize_history=AsyncMock(), run=AsyncMock(), watch_history=AsyncMock())
            self.publisher.start.reset_mock()
            with patch('forwarder.read_config', return_value=config), \
                 patch('forwarder.resolve_proxy', return_value=None), \
                 patch('forwarder.create_telegram', return_value=telegram), \
                 patch('forwarder.BrowserPublisher', return_value=self.publisher), \
                 patch('forwarder.utils.get_peer_id', return_value=-1001), \
                 patch('forwarder.Forwarder', return_value=service):
                with self.assertRaises(ConnectionError):
                    await main(self.store, non_interactive=background)
            self.publisher.start.assert_awaited_once_with(allow_login=not background)
            service.initialize_history.assert_awaited_once()
            service.run.assert_awaited_once()

    async def test_failed_startup_login_never_starts_forwarding(self):
        config = configparser.ConfigParser()
        config.read_dict({'Settings': {}, 'Telegram': {'Telegram_CHANNEL_USERNAME': '-1001'}})
        telegram = Mock(start=AsyncMock(), disconnect=AsyncMock(), get_input_entity=AsyncMock())
        self.publisher.start.side_effect = BrowserAttention('login timeout')
        with patch('forwarder.read_config', return_value=config), \
             patch('forwarder.resolve_proxy', return_value=None), \
             patch('forwarder.create_telegram', return_value=telegram), \
             patch('forwarder.BrowserPublisher', return_value=self.publisher), \
             patch('forwarder.utils.get_peer_id', return_value=-1001), \
             patch('forwarder.Forwarder') as service:
            with self.assertRaises(BrowserAttention):
                await main(self.store)
        service.assert_not_called()
        telegram.add_event_handler.assert_not_called()


class SupervisorTests(unittest.TestCase):
    def test_crash_restart_backoff_and_clean_exit(self):
        children = [Mock(wait=Mock(return_value=code)) for code in (1, 1, 0)]
        factory, sleep = Mock(side_effect=children), Mock()
        self.assertEqual(supervise(["test"], popen=factory, sleep=sleep, clock=lambda: 0), 0)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 10])
        self.assertEqual(factory.call_count, 3)

    def test_attention_exit_not_restarted(self):
        factory, sleep = Mock(return_value=Mock(wait=Mock(return_value=2))), Mock()
        self.assertEqual(supervise(["test"], popen=factory, sleep=sleep), 0)
        factory.assert_called_once()
        sleep.assert_not_called()

    def test_interrupt_terminates_hidden_child(self):
        child = Mock(wait=Mock(side_effect=[KeyboardInterrupt, 0]))
        self.assertEqual(supervise(["test"], popen=Mock(return_value=child)), 0)
        child.terminate.assert_called_once()


class ManagementTests(unittest.TestCase):
    def test_status_is_readonly_while_forwarder_holds_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir()
            store = DeliveryStore(state / "deliveries.sqlite3")
            try:
                store.initialize(-1001, 0)
                store.ingest(-1001, message())
                store.update(store.get(-1001, 1), "sending")
                with instance_lock(state / "forwarder.lock"), \
                     patch("forwarder.BASE_DIR", directory), \
                     patch("forwarder.configure_logging"), \
                     patch("sys.argv", ["forwarder.py", "--status"]):
                    self.assertEqual(cli(), 0)
                self.assertEqual(store.get(-1001, 1)["state"], "sending")
            finally:
                store.close()

    def test_mark_sent_and_retry_commands_do_not_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir()
            store = DeliveryStore(state / "deliveries.sqlite3")
            store.initialize(-1001, 0)
            store.ingest(-1001, message())
            store.update(store.get(-1001, 1), "uncertain")
            store.close()
            with patch("forwarder.BASE_DIR", directory), patch("forwarder.configure_logging"), \
                 patch("forwarder.main") as run:
                with patch("sys.argv", ["forwarder.py", "--mark-sent=-1001:1", "--tweet-id=999"]):
                    self.assertEqual(cli(), 0)
                with patch("sys.argv", ["forwarder.py", "--retry=-1001:1"]):
                    self.assertEqual(cli(), 2)
                run.assert_not_called()
            store = DeliveryStore(state / "deliveries.sqlite3")
            try:
                self.assertEqual(store.get(-1001, 1)["tweet_id"], "999")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
