"""Durable delivery journal and history cursor (no message-content deduplication)."""

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def instance_lock(path):
    """Only one process may recover/consume this journal at a time."""
    handle = open(path, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunning("Forwarder is already running. Stop it before changing its queue.") from exc
        yield
    finally:
        handle.close()  # OS also releases the lock after a crash/reboot.


class DeliveryStore:
    def __init__(self, path, read_only=False):
        self.db = (sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
                   if read_only else sqlite3.connect(path))
        self.db.row_factory = sqlite3.Row
        if read_only:
            return
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cursors (
                channel_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                text TEXT NOT NULL, media_kind TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0,
                tweet_id TEXT, last_error TEXT, updated_at REAL NOT NULL,
                PRIMARY KEY (channel_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS delivery_pending
                ON deliveries(channel_id, state, message_id);
        """)
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(deliveries)')}
        with self.db:
            for name, declaration in (
                    ('grouped_id', 'TEXT'), ('album_ready', 'INTEGER NOT NULL DEFAULT 1'),
                    ('album_due', 'REAL NOT NULL DEFAULT 0')):
                if name not in columns:
                    self.db.execute(f'ALTER TABLE deliveries ADD COLUMN {name} {declaration}')
            self.db.execute('CREATE INDEX IF NOT EXISTS delivery_album ON deliveries(channel_id, grouped_id)')

    def close(self):
        self.db.close()

    def recover(self):
        with self.db:
            self.db.execute("UPDATE deliveries SET state='pending' WHERE state='processing'")
            self.db.execute("UPDATE deliveries SET album_ready=0 WHERE grouped_id IS NOT NULL AND state='pending'")
            # A crash after create_tweet may have happened after X accepted it.
            self.db.execute("""UPDATE deliveries SET state='uncertain',
                last_error='Interrupted during publishing; check X before retrying'
                WHERE state='sending'""")

    def cursor(self, channel_id):
        row = self.db.execute("SELECT message_id FROM cursors WHERE channel_id=?", (channel_id,)).fetchone()
        return row[0] if row else None

    def initialize(self, channel_id, message_id):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO cursors VALUES (?, ?)", (channel_id, message_id))

    def ingest(self, channel_id, message):
        kind = "video" if message.video else "photo" if message.photo else ""
        text = message.message or ""
        group = getattr(message, 'grouped_id', None)
        group = str(group) if group is not None else None
        now = time.time()
        with self.db:
            if text or kind:
                inserted = self.db.execute("""INSERT OR IGNORE INTO deliveries
                    (channel_id, message_id, text, media_kind, updated_at, grouped_id, album_ready, album_due)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (channel_id, message.id, text, kind, now, group, int(group is None), now + 3 if group else 0))
                if group and inserted.rowcount:
                    blocked = self.db.execute("""SELECT 1 FROM deliveries WHERE channel_id=? AND grouped_id=?
                        AND state!='pending' LIMIT 1""", (channel_id, group)).fetchone()
                    if blocked:
                        self.db.execute("""UPDATE deliveries SET state='failed', last_error=?
                            WHERE channel_id=? AND message_id=?""",
                            ('Additional album media arrived after delivery started; review the whole album',
                             channel_id, message.id))
                    self.db.execute("""UPDATE deliveries SET album_ready=0, album_due=?
                        WHERE channel_id=? AND grouped_id=? AND state='pending'""", (now + 3, channel_id, group))
            # Commit the job and cursor atomically. Live events never advance this.
            self.db.execute("UPDATE cursors SET message_id=MAX(message_id, ?) WHERE channel_id=?",
                            (message.id, channel_id))

    def next_job(self, channel_id):
        job = self.db.execute("""SELECT * FROM deliveries
            WHERE channel_id=? AND state='pending' ORDER BY message_id LIMIT 1""", (channel_id,)).fetchone()
        return job if job is not None and job['album_ready'] else None

    def has_pending(self, channel_id):
        # Includes albums still collecting media and delayed retries.
        return self.db.execute("SELECT 1 FROM deliveries WHERE channel_id=? AND state='pending' LIMIT 1",
                               (channel_id,)).fetchone() is not None

    def finalize_albums(self, channel_id):
        """Only call after a successful history scan reaches its current end."""
        with self.db:
            self.db.execute("""UPDATE deliveries SET album_ready=1
                WHERE channel_id=? AND grouped_id IS NOT NULL AND state='pending' AND album_due<=?
                AND NOT EXISTS (SELECT 1 FROM deliveries other WHERE other.channel_id=deliveries.channel_id
                    AND other.grouped_id=deliveries.grouped_id AND other.state!='pending')""",
                (channel_id, time.time()))

    def members(self, job):
        if job['grouped_id'] is None:
            return [self.get(job['channel_id'], job['message_id'])]
        return self.db.execute('''SELECT * FROM deliveries WHERE channel_id=? AND grouped_id=?
            ORDER BY message_id''', (job['channel_id'], job['grouped_id'])).fetchall()

    def get(self, channel_id, message_id):
        return self.db.execute("SELECT * FROM deliveries WHERE channel_id=? AND message_id=?",
                               (channel_id, message_id)).fetchone()

    def update(self, job, state, *, error=None, next_attempt=0, tweet_id=None, attempt=False):
        # Freeze the membership before the first await; late arrivals must never
        # be marked sent along with files that were actually uploaded.
        ids = job['member_ids'] if 'member_ids' in job.keys() else [row['message_id'] for row in self.members(job)]
        placeholders = ','.join('?' for _ in ids)
        with self.db:
            self.db.execute(f"""UPDATE deliveries SET state=?, last_error=?, next_attempt=?,
                tweet_id=COALESCE(?, tweet_id), attempts=attempts+?, updated_at=?
                WHERE channel_id=? AND message_id IN ({placeholders})""",
                (state, str(error)[:2000] if error else None, next_attempt, tweet_id, int(attempt),
                 time.time(), job["channel_id"], *ids))

    def attention(self):
        return self.db.execute("""SELECT channel_id, message_id, state, attempts, tweet_id, last_error
            FROM deliveries WHERE state IN ('failed', 'uncertain')
            ORDER BY channel_id, message_id""").fetchall()

    def counts(self):
        return dict(self.db.execute("SELECT state, COUNT(*) FROM deliveries GROUP BY state"))

    def retry(self, channel_id, message_id):
        job = self.get(channel_id, message_id)
        if job is None or job["state"] not in ("failed", "uncertain", "pending"):
            raise ValueError("Only pending/failed/uncertain deliveries can be retried")
        if any(row['state'] not in ('failed', 'uncertain', 'pending') for row in self.members(job)):
            raise ValueError('Album includes a sent or active delivery; review it before retrying')
        self.update(job, "pending")
        if job['grouped_id']:
            with self.db:
                self.db.execute('UPDATE deliveries SET album_ready=0 WHERE channel_id=? AND grouped_id=?',
                                (channel_id, job['grouped_id']))


def data_directory(base_dir):
    path = Path(base_dir) / "state"
    path.mkdir(exist_ok=True)
    return path
