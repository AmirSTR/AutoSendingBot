import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from typing import List, Optional

DATABASE_URL = os.getenv("DATABASE_URL")


class Database:
    def __init__(self):
        self._init_db()

    @contextmanager
    def _cursor(self, cursor_factory=None):
        conn = psycopg2.connect(DATABASE_URL)
        try:
            kwargs = {'cursor_factory': cursor_factory} if cursor_factory else {}
            with conn.cursor(**kwargs) as cur:
                yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id           SERIAL PRIMARY KEY,
                    message      TEXT NOT NULL,
                    peer_id      INTEGER NOT NULL,
                    next_run     TEXT NOT NULL,
                    repeat_type  TEXT NOT NULL,
                    repeat_value TEXT NOT NULL DEFAULT '',
                    paused       INTEGER NOT NULL DEFAULT 0,
                    created_at   TEXT DEFAULT (NOW()::text)
                )
            ''')

    def add_task(self, message: str, peer_id: int, next_run: str,
                 repeat_type: str, repeat_value: str) -> int:
        with self._cursor() as cur:
            cur.execute(
                '''INSERT INTO tasks (message, peer_id, next_run, repeat_type, repeat_value)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id''',
                (message, peer_id, next_run, repeat_type, repeat_value)
            )
            return cur.fetchone()[0]

    def get_task(self, task_id: int) -> Optional[dict]:
        with self._cursor(psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT * FROM tasks WHERE id = %s', (task_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_all_tasks(self) -> List[dict]:
        with self._cursor(psycopg2.extras.RealDictCursor) as cur:
            cur.execute('SELECT * FROM tasks ORDER BY next_run')
            return [dict(r) for r in cur.fetchall()]

    def delete_task(self, task_id: int):
        with self._cursor() as cur:
            cur.execute('DELETE FROM tasks WHERE id = %s', (task_id,))

    def set_paused(self, task_id: int, paused: bool):
        with self._cursor() as cur:
            cur.execute('UPDATE tasks SET paused = %s WHERE id = %s', (int(paused), task_id))

    def update_next_run(self, task_id: int, next_run: str):
        with self._cursor() as cur:
            cur.execute('UPDATE tasks SET next_run = %s WHERE id = %s', (next_run, task_id))

    def update_message(self, task_id: int, message: str):
        with self._cursor() as cur:
            cur.execute('UPDATE tasks SET message = %s WHERE id = %s', (message, task_id))

    def update_peer_id(self, task_id: int, peer_id: int):
        with self._cursor() as cur:
            cur.execute('UPDATE tasks SET peer_id = %s WHERE id = %s', (peer_id, task_id))
