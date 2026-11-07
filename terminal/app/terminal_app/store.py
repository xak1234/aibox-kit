"""SQLite-backed tasks, recordings and evidence-linked procedural memory."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL,
                steps TEXT NOT NULL, skills TEXT NOT NULL, status TEXT NOT NULL,
                created TEXT NOT NULL, updated TEXT NOT NULL, result TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL,
                task_id TEXT, created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, task_id TEXT, command TEXT NOT NULL, output TEXT NOT NULL DEFAULT '',
                exit_code INTEGER, status TEXT NOT NULL, cwd TEXT NOT NULL,
                started TEXT NOT NULL, finished TEXT, session_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
                kind TEXT NOT NULL, evidence TEXT NOT NULL, created TEXT NOT NULL, uses INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS commands_task ON commands(task_id);
        ''')
        columns = {r['name'] for r in self.rows('PRAGMA table_info(tasks)')}
        for name in ('learning', 'learning_report'):
            if name not in columns:
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT NOT NULL DEFAULT '{{}}'")
        self.db.execute('CREATE TABLE IF NOT EXISTS trials (task_id TEXT, label TEXT, report TEXT, PRIMARY KEY(task_id,label))')
        self.db.execute("UPDATE tasks SET status='interrupted', result='Runtime restarted. Review the recording before retrying.' WHERE status='running'")
        self.db.execute("UPDATE commands SET status='interrupted', finished=? WHERE status='running'", (now(),))
        self.db.commit()

    def rows(self, query: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(query, args).fetchall()]

    def event(self, kind: str, text: str) -> None:
        self.db.execute('INSERT INTO events(kind,text,created) VALUES(?,?,?)', (kind, text, now()))
        self.db.commit()

    def message(self, role: str, content: str, task_id: str | None = None) -> None:
        self.db.execute('INSERT INTO messages(role,content,task_id,created) VALUES(?,?,?,?)',
                        (role, content, task_id, now()))
        self.db.commit()

    def create_task(self, goal: str, steps: list[str], skills: list[str], learning: dict[str, Any] | None = None) -> dict[str, Any]:
        ident, stamp = new_id(), now()
        title = goal.splitlines()[0][:90]
        self.db.execute('INSERT INTO tasks(id,title,goal,steps,skills,status,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                        (ident, title, goal, json.dumps(steps), json.dumps(skills), 'queued', stamp, stamp))
        self.db.execute('UPDATE tasks SET learning=? WHERE id=?', (json.dumps(learning or {}), ident))
        self.db.commit()
        self.event('task', f'Queued: {title}')
        return self.task(ident)

    def task(self, ident: str) -> dict[str, Any]:
        rows = self.rows('SELECT * FROM tasks WHERE id=?', (ident,))
        if not rows:
            raise KeyError(ident)
        return self.decode_task(rows[0])

    @staticmethod
    def decode_task(row: dict[str, Any]) -> dict[str, Any]:
        return {**row, 'steps': json.loads(row['steps']), 'skills': json.loads(row['skills']),
                'learning': json.loads(row['learning']), 'learning_report': json.loads(row['learning_report'])}

    def tasks(self) -> list[dict[str, Any]]:
        return [self.decode_task(t) for t in self.rows('SELECT * FROM tasks ORDER BY created DESC LIMIT 300')]

    def task_status(self, ident: str, status: str, result: str = '') -> None:
        self.db.execute('UPDATE tasks SET status=?,result=?,updated=? WHERE id=?', (status, result, now(), ident))
        self.db.commit()

    def save_trial(self, task_id: str, label: str, report: dict[str, Any]) -> None:
        self.db.execute('INSERT INTO trials(task_id,label,report) VALUES(?,?,?) ON CONFLICT(task_id,label) DO UPDATE SET report=excluded.report',
                        (task_id, label, json.dumps(report)))
        self.db.commit()

    def trials(self, task_id: str) -> list[dict[str, Any]]:
        return [json.loads(row['report']) for row in self.rows('SELECT report FROM trials WHERE task_id=? ORDER BY rowid', (task_id,))]

    def learning_report(self, task_id: str, report: dict[str, Any]) -> None:
        self.db.execute('UPDATE tasks SET learning_report=?,updated=? WHERE id=?', (json.dumps(report), now(), task_id))
        self.db.commit()

    def command_start(self, task_id: str | None, command: str, cwd: str, session_id: str) -> str:
        ident = new_id()
        self.db.execute('INSERT INTO commands(id,task_id,command,status,cwd,started,session_id) VALUES(?,?,?,?,?,?,?)',
                        (ident, task_id, command, 'running', cwd, now(), session_id))
        self.db.commit()
        return ident

    def command_end(self, ident: str, output: str, code: int | None, status: str, cwd: str) -> None:
        self.db.execute('UPDATE commands SET output=?,exit_code=?,status=?,cwd=?,finished=? WHERE id=?',
                        (output, code, status, cwd, now(), ident))
        self.db.commit()

    def commands(self, task_id: str | None = None) -> list[dict[str, Any]]:
        if task_id:
            return self.rows('SELECT * FROM commands WHERE task_id=? ORDER BY started', (task_id,))
        return self.rows('SELECT * FROM commands ORDER BY started DESC LIMIT 200')

    def remember(self, title: str, content: str, kind: str, evidence: list[str]) -> str:
        ident = new_id()
        if kind == 'procedure':
            if not evidence:
                raise ValueError('Procedures require successful command evidence.')
            for cmd_id in evidence:
                row = self.rows('SELECT exit_code,status FROM commands WHERE id=?', (cmd_id,))
                if not row or row[0]['exit_code'] != 0 or row[0]['status'] != 'completed':
                    raise ValueError('Evidence must refer to a completed command with exit code 0.')
        self.db.execute('INSERT INTO memories(id,title,content,kind,evidence,created) VALUES(?,?,?,?,?,?)',
                        (ident, title, content, kind, json.dumps(evidence), now()))
        self.db.commit()
        self.event('memory', f'Saved {kind}: {title}')
        return ident

    def memories(self) -> list[dict[str, Any]]:
        return [{**row, 'evidence': json.loads(row['evidence'])}
                for row in self.rows('SELECT * FROM memories ORDER BY created DESC')]

    def recall(self, text: str, limit: int = 5) -> list[dict[str, Any]]:
        words = set(re.findall(r'\b[a-z][a-z0-9_-]{2,}\b', text.lower()))
        words -= {'the', 'and', 'with', 'this', 'that', 'from', 'for', 'please', 'create'}
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in self.memories():
            tokens = set(re.findall(r'\b[a-z][a-z0-9_-]{2,}\b', (item['title'] + ' ' + item['content']).lower()))
            score = len(words & tokens)
            if score or item['kind'] == 'preference':
                scored.append((score, item))
        result = [item for _, item in sorted(scored, key=lambda pair: pair[0], reverse=True)[:limit]]
        for item in result:
            self.db.execute('UPDATE memories SET uses=uses+1 WHERE id=?', (item['id'],))
        self.db.commit()
        return result

    def get_setting(self, key: str, default: Any = None) -> Any:
        rows = self.rows('SELECT value FROM settings WHERE key=?', (key,))
        return json.loads(rows[0]['value']) if rows else default

    def set_setting(self, key: str, value: Any) -> None:
        self.db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key, json.dumps(value)))
        self.db.commit()

    def backup(self, target: Path) -> None:
        with sqlite3.connect(target) as dest:
            self.db.backup(dest)

    def close(self) -> None:
        self.db.close()
