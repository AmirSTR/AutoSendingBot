import sqlite3
import os
from typing import List, Optional

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'tasks.db')


class Database:
    def __init__(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    message     TEXT NOT NULL,
                    peer_id     INTEGER NOT NULL,
                    next_run    TEXT NOT NULL,
                    repeat_type TEXT NOT NULL,
                    repeat_value TEXT NOT NULL DEFAULT '',
                    paused      INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT DEFAULT (datetime('now'))
                )
            ''')

    def add_task(self, message: str, peer_id: int, next_run: str,
                 repeat_type: str, repeat_value: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                'INSERT INTO tasks (message, peer_id, next_run, repeat_type, repeat_value) VALUES (?,?,?,?,?)',
                (message, peer_id, next_run, repeat_type, repeat_value)
            )
            return cur.lastrowid

    def get_task(self, task_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            return dict(row) if row else None

    def get_all_tasks(self) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute('SELECT * FROM tasks ORDER BY next_run').fetchall()
            return [dict(r) for r in rows]

    def delete_task(self, task_id: int):
        with self._conn() as conn:
            conn.execute('DELETE FROM tasks WHERE id=?', (task_id,))

    def set_paused(self, task_id: int, paused: bool):
        with self._conn() as conn:
            conn.execute('UPDATE tasks SET paused=? WHERE id=?', (int(paused), task_id))

    def update_next_run(self, task_id: int, next_run: str):
        with self._conn() as conn:
            conn.execute('UPDATE tasks SET next_run=? WHERE id=?', (next_run, task_id))
