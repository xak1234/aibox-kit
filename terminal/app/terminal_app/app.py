"""Authenticated local controller and WebSocket terminal transport."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shlex
import tempfile
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from starlette.background import BackgroundTask

from .agent import Agent, ModelClient
from .browser import status as browser_status
from .harness_memory import HarnessMemory
from .skills import SkillRegistry
from .store import Store
from .terminal import TerminalSession

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('TERMINAL_DATA', str(ROOT / '.local-state/data')))
WORKSPACE = Path(os.environ.get('TERMINAL_WORKSPACE', str(ROOT / '.local-state/workspace')))
ORIGINS = ['http://localhost:8765', 'http://127.0.0.1:8765', 'http://terminal.local:4173']
ORIGINS += [s.strip() for s in os.environ.get('TERMINAL_ORIGINS', '').split(',') if s.strip()]


class Runtime:
    def __init__(self) -> None:
        DATA.mkdir(parents=True, exist_ok=True)
        DATA.chmod(0o700)
        path = DATA / 'token'
        if not path.exists():
            path.write_text(secrets.token_urlsafe(32), encoding='utf-8')
            path.chmod(0o600)
        self.token = path.read_text(encoding='utf-8').strip()
        self.store = Store(DATA / 'memory.sqlite3')
        self.terminal = TerminalSession(DATA, WORKSPACE, self.store)
        self.skills = SkillRegistry(DATA / 'skills', self.store)
        self.model = ModelClient(self.store, DATA)
        self.harness = HarnessMemory(DATA / 'harness2')
        self.agent = Agent(self.terminal, self.store, self.skills, self.model, self.harness)

    def browser_root(self) -> Path:
        return (self.agent.trial_workspace / '.browser' if self.agent.trial_workspace else
                WORKSPACE / '.operator-home' / '.terminal-browser')

    def state(self) -> dict[str, Any]:
        commands = self.store.commands()
        return {
            'browser': browser_status(self.browser_root()),
            'version': '1.0.0', 'agent': self.agent.state(), 'tasks': self.store.tasks(),
            'messages': self.store.rows('SELECT * FROM (SELECT * FROM messages ORDER BY id DESC LIMIT 150) ORDER BY id'),
            'commands': [{k: v for k, v in c.items() if k != 'output'} for c in commands],
            'memories': self.store.memories(), 'skills': self.skills.all(),
            'events': self.store.rows('SELECT * FROM events ORDER BY id DESC LIMIT 150'),
            'model': self.model.public_config(),
            'terminal': {'alive': self.terminal.alive, 'cwd': self.terminal.cwd,
                         'session_id': self.terminal.session_id, 'cols': self.terminal.cols,
                         'rows': self.terminal.rows, 'ready': self.terminal.ready.is_set()},
            'runtime': os.environ.get('TERMINAL_RUNTIME', 'linux-process'),
            'harness': {'name': 'Harness²', 'version': '0.1.0', 'mode': 'optional-sequential-recursion',
                        'commit': '1cfe254e8d25b15f37048c3730134686abe60335',
                        'recursive_rollouts': True, 'entries': len(self.harness.library.entries())},
            'counts': {t: self.store.rows(f'SELECT COUNT(*) AS n FROM {t}')[0]['n'] for t in
                       ('tasks', 'commands', 'memories', 'messages')},
        }


runtime: Runtime


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global runtime
    runtime = Runtime()
    await runtime.terminal.start()
    print(f'terminal.. ready. Pairing token is in {DATA / "token"}', flush=True)
    yield
    await runtime.agent.control('stop')
    await runtime.terminal.close()
    runtime.store.close()


app = FastAPI(title='terminal..', version='1.0.0', lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=['GET', 'POST', 'DELETE'],
                   allow_headers=['Authorization', 'Content-Type'])


async def authorized(request: Request) -> None:
    token = request.headers.get('authorization', '').removeprefix('Bearer ')
    if not secrets.compare_digest(token, runtime.token):
        raise HTTPException(401, 'Pair this browser with your runtime token.')


auth = [Depends(authorized)]


class VerificationInput(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    command: str = Field(min_length=1, max_length=10000)
    expected: str = Field(default='', max_length=2000)
    weight: int = Field(default=1, ge=1, le=100)
    timeout: int = Field(default=30, ge=1, le=180)


class LearningInput(BaseModel):
    enabled: bool = False
    rounds: int = Field(default=1, ge=1, le=3)
    inputs: str = Field(default='', max_length=1000)
    checks: list[VerificationInput] = Field(default_factory=list, max_length=20)

    @model_validator(mode='after')
    def require_checks(self) -> LearningInput:
        if self.enabled and not self.checks:
            raise ValueError('Repeated learning needs at least one explicit verification command.')
        return self


class TaskInput(BaseModel):
    goal: str = Field(min_length=1, max_length=20000)
    steps: list[str] = Field(default_factory=list, max_length=30)
    skills: list[str] = Field(default_factory=list, max_length=20)
    learning: LearningInput = Field(default_factory=LearningInput)


class StartInput(BaseModel):
    auto_queue: bool = False


class ControlInput(BaseModel):
    action: str


class CommandInput(BaseModel):
    command: str = Field(min_length=1, max_length=30000)


class ModelInput(BaseModel):
    provider: str
    base_url: str
    model: str
    api_key: str | None = None


class SkillInput(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    body: str = Field(min_length=1, max_length=50000)
    enabled: bool = True


class MemoryInput(BaseModel):
    title: str = Field(min_length=1, max_length=150)
    content: str = Field(min_length=1, max_length=10000)
    kind: Literal['note', 'preference'] = 'note'


@app.get('/api/health')
async def health() -> dict[str, str]:
    return {'status': 'ok', 'app': 'terminal..'}


@app.get('/api/state', dependencies=auth)
async def state() -> dict[str, Any]:
    return runtime.state()


@app.post('/api/tasks', dependencies=auth)
async def task_create(body: TaskInput) -> dict[str, Any]:
    task = runtime.store.create_task(body.goal.strip(), body.steps, body.skills, body.learning.model_dump())
    runtime.store.message('user', body.goal, task['id'])
    return task


@app.get('/api/tasks/{ident}/learning', dependencies=auth)
async def learning_detail(ident: str) -> dict[str, Any]:
    try:
        task = runtime.store.task(ident)
    except KeyError as exc:
        raise HTTPException(404, 'Task not found.') from exc
    return {'config': task['learning'], 'report': task['learning_report'], 'trials': runtime.store.trials(ident)}


@app.post('/api/tasks/{ident}/learning', dependencies=auth)
async def learning_update(ident: str, body: LearningInput) -> dict[str, str]:
    try:
        task = runtime.store.task(ident)
    except KeyError as exc:
        raise HTTPException(404, 'Task not found.') from exc
    if task['status'] == 'running':
        raise HTTPException(409, 'Stop the running task before changing its learning requirements.')
    runtime.store.db.execute('UPDATE tasks SET learning=? WHERE id=?', (body.model_dump_json(), ident))
    runtime.store.db.commit()
    return {'status': 'saved'}


class BrowserInput(BaseModel):
    action: Literal['start', 'open', 'new', 'back', 'reload', 'tab', 'close', 'stop', 'snapshot', 'tap', 'press', 'scroll', 'type']
    args: list[str] = Field(default_factory=list, max_length=3)


@app.get('/api/browser/frame', dependencies=auth)
async def browser_frame() -> FileResponse:
    root = runtime.browser_root()
    if not browser_status(root)['running'] or not (root / 'frame.png').is_file():
        raise HTTPException(409, 'Start the browser from its terminal launcher.')
    return FileResponse(root / 'frame.png', media_type='image/png', headers={'Cache-Control': 'no-store'})


@app.post('/api/browser/action', dependencies=auth)
async def browser_action(body: BrowserInput) -> dict[str, Any]:
    if runtime.agent.active or runtime.agent.manual:
        raise HTTPException(409, 'Stop the task and return terminal control before controlling the browser here.')
    command = shlex.join(['terminal-browser', body.action, '--', *body.args])
    try:
        result = await runtime.terminal.execute(command, timeout=60)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if result['exit_code'] != 0:
        raise HTTPException(409, 'Browser command failed. Open Recordings for its actual output.')
    return result


@app.post('/api/tasks/{ident}/run', dependencies=auth)
async def task_run(ident: str, body: StartInput) -> dict[str, str]:
    try:
        runtime.agent.start(ident, body.auto_queue)
    except KeyError as exc:
        raise HTTPException(404, 'Task not found.') from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'status': 'started'}


@app.post('/api/tasks/{ident}/cancel', dependencies=auth)
async def task_cancel(ident: str) -> dict[str, str]:
    try:
        task = runtime.store.task(ident)
    except KeyError as exc:
        raise HTTPException(404, 'Task not found.') from exc
    if task['status'] == 'running':
        raise HTTPException(409, 'Use Stop for the running task.')
    if task['status'] == 'queued':
        runtime.store.task_status(ident, 'cancelled', 'Removed from queue by user.')
    return {'status': 'ok'}


@app.post('/api/control', dependencies=auth)
async def control(body: ControlInput) -> dict[str, Any]:
    try:
        await runtime.agent.control(body.action)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return runtime.agent.state()


@app.post('/api/command', dependencies=auth)
async def command(body: CommandInput) -> dict[str, Any]:
    if runtime.agent.active or runtime.agent.manual:
        raise HTTPException(409, 'Finish the active task or return manual control first.')
    try:
        return await runtime.terminal.execute(body.command)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/api/commands/{ident}', dependencies=auth)
async def command_detail(ident: str) -> dict[str, Any]:
    rows = runtime.store.rows('SELECT * FROM commands WHERE id=?', (ident,))
    if not rows:
        raise HTTPException(404, 'Command not found.')
    return rows[0]


@app.post('/api/model', dependencies=auth)
async def model_save(body: ModelInput) -> dict[str, Any]:
    if runtime.agent.active:
        raise HTTPException(409, 'Stop the current task before changing models.')
    try:
        runtime.model.save(body.provider, body.base_url, body.model, body.api_key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return runtime.model.public_config()


@app.get('/api/models', dependencies=auth)
async def model_list() -> dict[str, Any]:
    try:
        return {'models': await runtime.model.models()}
    except Exception as exc:
        raise HTTPException(502, f'Model connection failed ({type(exc).__name__}). Check the endpoint and credentials.') from exc


@app.post('/api/skills', dependencies=auth)
async def skill_save(body: SkillInput) -> dict[str, str]:
    try:
        runtime.skills.save(body.id, body.name, body.description, body.body, body.enabled)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {'status': 'saved'}


@app.post('/api/memories', dependencies=auth)
async def memory_save(body: MemoryInput) -> dict[str, str]:
    return {'id': runtime.store.remember(body.title, body.content, body.kind, [])}


@app.get('/api/export', dependencies=auth)
async def export() -> FileResponse:
    temp = tempfile.TemporaryDirectory(prefix='terminal-export-')
    folder = Path(temp.name)
    runtime.store.backup(folder / 'memory.sqlite3')
    archive = folder / 'terminal-memory.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(folder / 'memory.sqlite3', 'memory.sqlite3')
        for name in ('sessions', 'skills', 'harness2'):
            for path in (DATA / name).rglob('*'):
                if path.is_file() and not path.is_symlink():
                    bundle.write(path, str(path.relative_to(DATA)))
        bundle.writestr('README.txt', 'terminal.. memory export\nContains task history, recordings, skills and Harness² experience.\nPairing tokens and model keys are excluded.\nRestore with scripts/restore.py while the runtime is stopped.\n')
    return FileResponse(archive, filename='terminal-memory.zip', background=BackgroundTask(temp.cleanup))


@app.websocket('/ws/terminal')
async def terminal_socket(ws: WebSocket) -> None:
    origin = ws.headers.get('origin', '')
    if origin and origin not in ORIGINS:
        await ws.close(code=1008)
        return
    await ws.accept()
    queue: asyncio.Queue[dict[str, Any]] | None = None
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if not secrets.compare_digest(str(hello.get('token', '')), runtime.token):
            await ws.close(code=1008)
            return
        queue, cutoff = runtime.terminal.subscribe()
        await ws.send_json({'type': 'reset', 'session_id': runtime.terminal.session_id})
        with runtime.terminal.journal_path.open(encoding='utf-8') as source:
            for index, line in enumerate(source):
                event = json.loads(line)
                if event['seq'] > cutoff:
                    break
                await ws.send_json(event)
                if index % 100 == 0:
                    await asyncio.sleep(0)
        await ws.send_json({'type': 'synced'})

        async def send() -> None:
            assert queue is not None
            while True:
                event = await queue.get()
                await ws.send_json(event)
                if event['type'] == 'resync':
                    await ws.close(code=1013)
                    return

        async def receive() -> None:
            while True:
                msg = await ws.receive_json()
                if msg.get('type') == 'input' and runtime.agent.manual:
                    data = str(msg.get('data', ''))[:16384]
                    if data:
                        runtime.terminal.ready.clear()
                    await runtime.terminal.write(data.encode('utf-8'))
                elif msg.get('type') == 'interrupt' and runtime.agent.manual:
                    await runtime.terminal.interrupt()

        sender, receiver = asyncio.create_task(send()), asyncio.create_task(receive())
        try:
            done, _ = await asyncio.wait([sender, receiver], return_when=asyncio.FIRST_COMPLETED)
            for job in done:
                job.result()
        finally:
            for job in (sender, receiver):
                job.cancel()
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    await job
    except (WebSocketDisconnect, TimeoutError, RuntimeError, ValueError):
        pass
    finally:
        if queue is not None:
            runtime.terminal.subscribers.discard(queue)


app.mount('/', StaticFiles(directory=ROOT / 'dist', html=True), name='ui')
