"""Bounded background jobs with conservative restart recovery.

Only work that has not started sending media is replayed after a restart.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

current_job = contextvars.ContextVar('veltrix_job', default=None)
ACTIVE = ('queued', 'downloading', 'sending')


class JobManager:
    def __init__(self, path: Path, max_pending=20, per_user=3):
        self.path = path
        self.max_pending, self.per_user = max_pending, per_user
        self.tasks = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, uid INTEGER, chat INTEGER, url TEXT,
                    mode TEXT, thread INTEGER, state TEXT, updated REAL,
                    created REAL, recoveries INTEGER DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_job ON jobs(uid, chat, url, mode)
                WHERE state IN ('queued', 'downloading', 'sending');
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def admit(self, uid, chat, url, mode, thread=None):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("DELETE FROM jobs WHERE state NOT IN ('queued','downloading','sending') AND updated < ?",
                       (time.time() - 86400,))
            active = db.execute("SELECT * FROM jobs WHERE state IN ('queued','downloading','sending')").fetchall()
            if any((r['uid'], r['chat'], r['url'], r['mode']) == (uid, chat, url, mode) for r in active):
                return 'duplicate', None
            if sum(r['uid'] == uid for r in active) >= self.per_user:
                return 'user_full', None
            if len(active) >= self.max_pending:
                return 'full', None
            row = dict(id=uuid.uuid4().hex[:16], uid=uid, chat=chat, url=url, mode=mode,
                       thread=thread, state='queued', updated=time.time(), created=time.time(), recoveries=0)
            db.execute('INSERT INTO jobs VALUES (:id,:uid,:chat,:url,:mode,:thread,:state,:updated,:created,:recoveries)', row)
            return 'accepted', row

    def state(self, job_id, state):
        with closing(self.connect()) as db, db:
            db.execute('UPDATE jobs SET state=?, updated=? WHERE id=?', (state, time.time(), job_id))

    def recent(self, uid, chat):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute('SELECT * FROM jobs WHERE uid=? AND chat=? ORDER BY created DESC LIMIT 5', (uid, chat))]

    def recover(self):
        ready, uncertain = [], []
        with closing(self.connect()) as db, db:
            for row in db.execute("SELECT * FROM jobs WHERE state IN ('queued','downloading','sending') ORDER BY created").fetchall():
                row = dict(row)
                if row['state'] == 'sending':
                    db.execute("UPDATE jobs SET state='interrupted' WHERE id=?", (row['id'],))
                    uncertain.append(row)
                elif row['recoveries'] >= 3 or row['created'] < time.time() - 86400:
                    db.execute("UPDATE jobs SET state='expired' WHERE id=?", (row['id'],))
                else:
                    db.execute("UPDATE jobs SET state='queued', recoveries=recoveries+1 WHERE id=?", (row['id'],))
                    ready.append(row)
        return ready, uncertain

    def launch(self, row, run):
        async def work():
            token = current_job.set((self, row['id']))
            try:
                await run(row)
            except asyncio.CancelledError:
                # Preserve queued/downloading/sending for restart classification.
                raise
            except Exception as exc:
                self.state(row['id'], 'failed')
                logging.getLogger('veltrix').error('background job %s failed: %s', row['id'], type(exc).__name__)
            finally:
                current_job.reset(token)
        task = asyncio.create_task(work(), name='media-' + row['id'])
        self.tasks[row['id']] = task
        task.add_done_callback(lambda _: self.tasks.pop(row['id'], None))
        return task

    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def mark_state(state):
    current = current_job.get()
    if current:
        manager, job_id = current
        manager.state(job_id, state)


class ChatTarget:
    """Reply interface for a recovered request, retaining its forum topic."""
    def __init__(self, bot, chat_id, thread=None):
        self.bot, self.chat_id, self.thread = bot, chat_id, thread

    def __getattr__(self, name):
        if not name.startswith('reply_'):
            raise AttributeError(name)
        method = getattr(self.bot, 'send_message' if name == 'reply_text' else 'send_' + name.removeprefix('reply_'))
        async def send(*args, **kwargs):
            if args:
                kwargs['text'] = args[0]
            if self.thread is not None:
                kwargs['message_thread_id'] = self.thread
            return await method(chat_id=self.chat_id, **kwargs)
        return send
