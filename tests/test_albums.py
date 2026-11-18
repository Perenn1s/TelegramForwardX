import _local_dir  # noqa: F401  (isolates local data; must stay first)
import asyncio
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from browser_publisher import BrowserAttention
from delivery_store import DeliveryStore
from forwarder import Forwarder, NeedsAttention


def photo(message_id, group='album', text=''):
    return SimpleNamespace(id=message_id, grouped_id=group, message=text, photo=True, video=False)


class AlbumTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = DeliveryStore(Path(self.directory.name) / 'queue.db')
        self.addCleanup(self.store.close)
        self.store.initialize(-1001, 0)
        self.publisher = Mock(prepare=AsyncMock(), submit=AsyncMock(return_value='999'))
        async def get_message(entity, ids):
            async def download(file):
                path = Path(file) / 'same-name.jpg'
                path.write_text(str(ids))
                return str(path)
            return SimpleNamespace(download_media=download)
        self.telegram = Mock(get_messages=AsyncMock(side_effect=get_message))
        self.service = Forwarder(self.publisher, self.telegram, 'entity', -1001, self.store)
        patcher = patch('forwarder.BASE_DIR', self.directory.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ingest(self, *messages):
        with patch('delivery_store.time.time', return_value=100):
            for message in messages:
                self.store.ingest(-1001, message)

    def ready(self):
        with patch('delivery_store.time.time', return_value=104):
            self.store.finalize_albums(-1001)

    async def test_album_uploads_in_order_and_records_one_post_for_all_members(self):
        self.ingest(photo(2), photo(1, text='caption'))
        self.ready()
        async def prepare(text, paths):
            self.assertEqual(text, 'caption')
            self.assertEqual([Path(path).read_text() for path in paths], ['1', '2'])
            self.assertNotEqual(paths[0], paths[1])
        async def submit():
            self.assertEqual(self.store.counts(), {'sending': 2})
            return '999'
        self.publisher.prepare.side_effect = prepare
        self.publisher.submit.side_effect = submit
        await self.service.process(self.store.next_job(-1001))
        self.publisher.submit.assert_awaited_once()
        self.assertEqual(self.store.counts(), {'sent': 2})
        self.assertEqual([self.store.get(-1001, i)['tweet_id'] for i in (1, 2)], ['999', '999'])
        self.assertIsNone(self.store.next_job(-1001))

    async def test_same_caption_on_different_albums_is_not_merged(self):
        self.ingest(photo(1, 'first', 'caption'), photo(2, 'second', 'caption'))
        self.ready()
        await self.service.process(self.store.next_job(-1001))
        await self.service.process(self.store.next_job(-1001))
        self.assertEqual(self.publisher.submit.await_count, 2)

    async def test_ten_images_publish_four_then_two_replies_to_main_post(self):
        self.ingest(*(photo(i, text='caption' if i == 10 else '') for i in range(1, 11)))
        self.ready()
        uploaded = []
        async def prepare(text, paths, **kwargs):
            uploaded.append((text, [Path(path).read_text() for path in paths], kwargs))
        self.publisher.prepare.side_effect = prepare
        self.publisher.submit.side_effect = ['900', '901', '902']
        while self.store.next_job(-1001):
            await self.service.process(self.store.next_job(-1001))
        self.assertEqual(uploaded, [('caption', ['1', '2', '3', '4'], {}),
                                    ('', ['5', '6', '7', '8'], {'reply_to': '900'}),
                                    ('', ['9', '10'], {'reply_to': '900'})])
        self.assertEqual(self.store.counts(), {'sent': 10})
        self.assertEqual([self.store.get(-1001, i)['tweet_id'] for i in range(1, 11)],
                         ['900'] * 4 + ['901'] * 4 + ['902'] * 2)

    async def test_restart_after_main_post_only_sends_remaining_photo(self):
        self.ingest(*(photo(i) for i in range(1, 6)))
        self.ready()
        await self.service.process(self.store.next_job(-1001))
        self.store.recover()
        self.ready()
        await self.service.process(self.store.next_job(-1001))
        self.assertEqual(self.publisher.submit.await_count, 2)
        self.assertEqual(self.publisher.prepare.call_args.kwargs, {'reply_to': '999'})
        self.assertEqual(self.store.counts(), {'sent': 5})

    async def test_uncertain_reply_can_retry_without_reposting_main_or_skipping_batch(self):
        self.ingest(*(photo(i) for i in range(1, 11)))
        self.ready()
        await self.service.process(self.store.next_job(-1001))
        self.publisher.submit.side_effect = BrowserAttention('lost after click')
        with self.assertRaises(NeedsAttention):
            await self.service.process(self.store.next_job(-1001))
        self.assertEqual(self.store.counts(), {'sent': 4, 'uncertain': 4, 'pending': 2})
        with self.assertRaises(NeedsAttention):
            await self.service.process(self.store.next_job(-1001))
        self.store.retry(-1001, 6)
        self.ready()
        self.assertEqual(self.store.next_job(-1001)['message_id'], 5)
        self.publisher.submit.side_effect = None
        while self.store.next_job(-1001):
            await self.service.process(self.store.next_job(-1001))
        self.assertEqual(self.store.counts(), {'sent': 10})
        self.assertEqual(self.publisher.submit.await_count, 4)

    async def test_old_limit_failure_is_requeued_on_recovery(self):
        self.ingest(*(photo(i) for i in range(1, 6)))
        self.store.update(self.store.get(-1001, 1), 'failed', error=
                          'Telegram album has more than 4 media items; review it manually to fit one X post')
        self.store.recover()
        self.ready()
        self.assertEqual(self.store.counts(), {'pending': 5})
        self.assertIsNotNone(self.store.next_job(-1001))

    async def test_crash_during_reply_preserves_main_and_blocks_remaining_media(self):
        self.ingest(*(photo(i) for i in range(1, 11)))
        self.ready()
        await self.service.process(self.store.next_job(-1001))
        self.store.update(self.store.batch_job(self.store.next_job(-1001)), 'sending')
        self.store.recover()
        self.ready()
        self.assertEqual(self.store.counts(), {'sent': 4, 'uncertain': 4, 'pending': 2})
        self.assertIsNone(self.store.next_job(-1001))
        # Equivalent durable resolution used by --mark-sent, then restart.
        self.store.update(self.store.batch_job(self.store.get(-1001, 6)), 'sent', tweet_id='901')
        self.store.recover()
        self.ready()
        await self.service.process(self.store.next_job(-1001))
        self.assertEqual(self.publisher.prepare.call_args.kwargs, {'reply_to': '999'})
        self.assertEqual(self.publisher.submit.await_count, 2)
        self.assertEqual(self.store.counts(), {'sent': 10})

    async def test_reply_download_failure_keeps_main_sent_and_retries_only_reply(self):
        self.ingest(*(photo(i) for i in range(1, 6)))
        self.ready()
        await self.service.process(self.store.next_job(-1001))
        self.telegram.get_messages.side_effect = OSError('offline')
        await self.service.process(self.store.next_job(-1001))
        self.assertEqual(self.store.counts(), {'sent': 4, 'pending': 1})
        self.assertGreater(self.store.get(-1001, 5)['next_attempt'], 0)
        self.publisher.submit.assert_awaited_once()

    async def test_uncertain_album_recovers_and_retry_by_any_member_requeues_whole_group(self):
        self.ingest(photo(1), photo(2))
        self.ready()
        self.publisher.submit.side_effect = BrowserAttention('connection lost after click')
        with self.assertRaises(NeedsAttention):
            await self.service.process(self.store.next_job(-1001))
        self.assertEqual(self.store.counts(), {'uncertain': 2})
        self.store.recover()
        self.assertEqual(self.store.counts(), {'uncertain': 2})
        self.store.retry(-1001, 2)
        self.assertEqual(self.store.counts(), {'pending': 2})
        self.assertIsNone(self.store.next_job(-1001))
        self.ready()
        self.assertEqual(self.store.next_job(-1001)['message_id'], 1)

    async def test_late_arrival_during_submit_is_not_falsely_marked_sent(self):
        self.ingest(photo(1), photo(2))
        self.ready()
        async def submit():
            self.store.ingest(-1001, photo(3))
            return '999'
        self.publisher.submit.side_effect = submit
        await self.service.process(self.store.next_job(-1001))
        self.assertEqual(self.store.counts(), {'sent': 2, 'failed': 1})
        self.assertIsNone(self.store.get(-1001, 3)['tweet_id'])
        with self.assertRaises(ValueError):
            self.store.retry(-1001, 3)

    async def test_page_boundary_and_settle_window_do_not_split_album(self):
        async def first_page(*args, **kwargs):
            for i in range(1, 201):
                yield photo(i, group='tail' if i == 200 else str(i))
        self.telegram.iter_messages = first_page
        with patch('delivery_store.time.time', return_value=100):
            self.assertEqual(await self.service.sync_history(), 200)
        self.assertIsNone(self.store.next_job(-1001))
        async def final_page(*args, **kwargs):
            self.assertEqual(kwargs['min_id'], 200)
            yield photo(201, group='tail')
        self.telegram.iter_messages = final_page
        with patch('delivery_store.time.time', return_value=105):
            await self.service.sync_history()
        self.assertFalse(self.store.get(-1001, 200)['album_ready'])
        self.assertFalse(self.store.get(-1001, 201)['album_ready'])
        async def empty(*args, **kwargs):
            if False:
                yield
        self.telegram.iter_messages = empty
        with patch('delivery_store.time.time', return_value=109):
            await self.service.sync_history()
        self.assertTrue(self.store.get(-1001, 200)['album_ready'])
        self.assertTrue(self.store.get(-1001, 201)['album_ready'])

    async def test_disconnect_mid_album_never_releases_partial_group(self):
        async def broken(*args, **kwargs):
            yield photo(1)
            raise ConnectionError('offline')
        self.telegram.iter_messages = broken
        with self.assertRaises(ConnectionError):
            await self.service.sync_history()
        self.store.recover()
        self.assertIsNone(self.store.next_job(-1001))
        self.assertEqual(self.store.cursor(-1001), 1)

    async def test_restart_keeps_membership_and_sent_ids(self):
        self.ingest(photo(1), photo(2))
        self.ready()
        await self.service.process(self.store.next_job(-1001))
        other = DeliveryStore(Path(self.directory.name) / 'queue.db')
        try:
            other.recover()
            self.assertEqual(len(other.members(other.get(-1001, 1))), 2)
            self.assertEqual(other.counts(), {'sent': 2})
            self.assertIsNone(other.next_job(-1001))
        finally:
            other.close()


class MigrationTests(unittest.TestCase):
    def test_existing_schema_preserves_previously_sent_posts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.db'
            connection = sqlite3.connect(path)
            connection.execute('''CREATE TABLE deliveries (
                channel_id INTEGER, message_id INTEGER, text TEXT, media_kind TEXT, state TEXT,
                attempts INTEGER, next_attempt REAL, tweet_id TEXT, last_error TEXT, updated_at REAL,
                PRIMARY KEY(channel_id, message_id))''')
            connection.execute("INSERT INTO deliveries VALUES(-1001,1,'old','photo','sent',1,0,'123',NULL,1)")
            connection.commit()
            connection.close()
            store = DeliveryStore(path)
            try:
                store.recover()
                self.assertEqual(store.get(-1001, 1)['tweet_id'], '123')
                self.assertIsNone(store.get(-1001, 1)['grouped_id'])
                self.assertEqual(store.counts(), {'sent': 1})
            finally:
                store.close()
