"""One real Linux PTY shared by the agent and every terminal view."""
from __future__ import annotations

import asyncio
import base64
import codecs
import contextlib
import fcntl
import json
import os
import pty
import re
import shlex
import signal
import struct
import termios
import time
from pathlib import Path
from typing import Any, TextIO

from .store import Store, new_id, now

ANSI = re.compile(r'\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Z0-9]')


class TerminalSession:
    def __init__(self, data: Path, workspace: Path, store: Store) -> None:
        self.data, self.workspace, self.store = data, workspace, store
        self.session_id = new_id()
        self.pid = self.fd = self.meta_fd = -1
        self.cols, self.rows = 100, 28
        self.cwd = str(workspace)
        self.alive = False
        self.ready = asyncio.Event()
        self.lock = asyncio.Lock()
        self.pending: asyncio.Future[int] | None = None
        self.capture = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.metadata = b''
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.journal_path = data / 'sessions' / f'{self.session_id}.jsonl'
        self.journal: TextIO | None = None
        self.sequence = 0

    async def start(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        uid = int(os.environ.get('TERMINAL_UID', '1000' if os.getuid() == 0 else str(os.getuid())))
        gid = int(os.environ.get('TERMINAL_GID', str(uid)))
        home = self.workspace / '.operator-home'
        home.mkdir(exist_ok=True)
        if os.getuid() == 0:
            os.chown(self.workspace, uid, gid)
            os.chown(home, uid, gid)
        meta_read, meta_write = os.pipe()
        os.set_inheritable(meta_write, True)
        # Prompt metadata uses a separate pipe; no sentinels or fabricated text enter the display.
        rc = home / '.terminal-bashrc'
        rc.write_text(
            "export PS1='\\[\\e[38;5;114m\\]operator\\[\\e[0m\\]:\\w \\$ '\n"
            f"__terminal_prompt() {{ local code=$?; printf '%s\\0%s\\0' \"$code\" \"$PWD\" >&{meta_write}; }}\n"
            "PROMPT_COMMAND=__terminal_prompt\n"
            "bind 'set enable-bracketed-paste on'\n"
            "export HISTFILE=\"$HOME/.bash_history\"\n",
            encoding='utf-8',
        )
        rc.chmod(0o644)
        self.journal = self.journal_path.open('a', encoding='utf-8', buffering=1)
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.close(meta_read)
                os.chdir(self.workspace)
                if os.getuid() == 0 and (uid != os.getuid() or gid != os.getgid()):
                    os.setgroups([])
                    os.setgid(gid)
                    os.setuid(uid)
                env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home), 'SHELL': '/bin/bash',
                       'USER': 'operator', 'LOGNAME': 'operator', 'TERM': 'xterm-256color',
                       'LANG': 'C.UTF-8', 'COLORTERM': 'truecolor',
                       'TERMINAL_BROWSER_DIR': str(home / '.terminal-browser')}
                if 'PLAYWRIGHT_BROWSERS_PATH' in os.environ:
                    env['PLAYWRIGHT_BROWSERS_PATH'] = os.environ['PLAYWRIGHT_BROWSERS_PATH']
                os.execve('/bin/bash', ['/bin/bash', '--noprofile', '--rcfile', str(rc), '-i'], env)
            except BaseException as exc:
                os.write(2, f'Cannot start Linux shell: {type(exc).__name__}: {exc}\n'.encode())
                os._exit(127)
        os.close(meta_write)
        self.pid, self.fd, self.meta_fd, self.alive = pid, fd, meta_read, True
        os.set_blocking(fd, False)
        os.set_blocking(meta_read, False)
        loop = asyncio.get_running_loop()
        loop.add_reader(fd, self._read_output)
        loop.add_reader(meta_read, self._read_metadata)
        self.resize(self.cols, self.rows)
        await asyncio.wait_for(self.ready.wait(), timeout=10)
        self.store.event('session', f'Linux terminal session opened: {self.session_id}')

    def _emit(self, event: dict[str, Any]) -> None:
        self.sequence += 1
        event = {**event, 'seq': self.sequence, 'at': now()}
        if self.journal:
            self.journal.write(json.dumps(event, ensure_ascii=True) + '\n')
        for queue in tuple(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self.subscribers.discard(queue)
                # The consumer must reconnect and replay, never silently lose terminal bytes.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({'type': 'resync', 'reason': 'Viewer fell behind; reconnecting to replay.'})

    def _read_output(self) -> None:
        for _ in range(32):
            try:
                data = os.read(self.fd, 16384)
                if not data:
                    self._exited()
                    return
            except BlockingIOError:
                return
            except OSError:
                self._exited()
                return
            text = self.decoder.decode(data)
            if self.pending is not None:
                self.capture = (self.capture + text)[-250000:]
            self._emit({'type': 'output', 'data': base64.b64encode(data).decode('ascii')})

    def _read_metadata(self) -> None:
        try:
            data = os.read(self.meta_fd, 65536)
        except BlockingIOError:
            return
        if not data:
            asyncio.get_running_loop().remove_reader(self.meta_fd)
            return
        self.metadata += data
        while self.metadata.count(b'\0') >= 2:
            code, cwd, self.metadata = self.metadata.split(b'\0', 2)
            self.cwd = cwd.decode('utf-8', errors='replace')
            self._read_output()
            if self.pending is not None and not self.pending.done():
                try:
                    self.pending.set_result(int(code))
                except ValueError:
                    self.pending.set_exception(RuntimeError('Invalid shell status.'))
            self.ready.set()

    def _exited(self) -> None:
        if not self.alive:
            return
        self.alive = False
        loop = asyncio.get_running_loop()
        loop.remove_reader(self.fd)
        loop.remove_reader(self.meta_fd)
        if self.pending is not None and not self.pending.done():
            self.pending.set_exception(RuntimeError('The shell exited before reporting a status.'))
        self._emit({'type': 'shell_exit'})

    async def write(self, data: bytes) -> None:
        if not self.alive:
            raise RuntimeError('Terminal is closed. Restart the runtime to open a new session.')
        view = memoryview(data)
        while view:
            try:
                count = os.write(self.fd, view)
                view = view[count:]
            except BlockingIOError:
                await asyncio.sleep(0.01)

    async def execute(self, command: str, task_id: str | None = None, timeout: int = 60) -> dict[str, Any]:
        if '\x00' in command or len(command) > 30000:
            raise ValueError('Command is too long or contains a null byte.')
        async with self.lock:
            if not self.ready.is_set() or not self.alive:
                raise RuntimeError('The shell is not at a prompt. Return control after the current app exits.')
            self.ready.clear()
            self.capture = ''
            self.pending = asyncio.get_running_loop().create_future()
            ident = self.store.command_start(task_id, command, self.cwd, self.session_id)
            started = time.monotonic()
            code: int | None = None
            status = 'completed'
            try:
                # eval keeps cd/export in the SAME shell; quoting prevents injected wrapper syntax.
                await self.write(('eval -- ' + shlex.quote(command) + '\n').encode('utf-8'))
                code = await asyncio.wait_for(asyncio.shield(self.pending), timeout=timeout)
            except TimeoutError:
                status = 'timed_out'
                await self.interrupt()
                with contextlib.suppress(TimeoutError, RuntimeError):
                    await asyncio.wait_for(asyncio.shield(self.pending), timeout=3)
            except asyncio.CancelledError:
                status = 'cancelled'
                await self.interrupt()
                with contextlib.suppress(TimeoutError, RuntimeError):
                    await asyncio.wait_for(asyncio.shield(self.ready.wait()), timeout=3)
                self.store.command_end(ident, ANSI.sub('', self.capture), None, status, self.cwd)
                raise
            except Exception as exc:
                status = 'failed'
                self.capture += f'\nRuntime: {exc}'
            finally:
                self.pending = None
            output = ANSI.sub('', self.capture).replace('\r', '')
            self.store.command_end(ident, output, code, status, self.cwd)
            if self.journal:
                self.journal.flush()
                os.fsync(self.journal.fileno())
            return {'id': ident, 'command': command, 'output': output, 'exit_code': code,
                    'status': status, 'cwd': self.cwd, 'duration': round(time.monotonic() - started, 3)}

    async def interrupt(self) -> None:
        if self.alive:
            await self.write(b'\x03')

    def resize(self, cols: int, rows: int) -> None:
        self.cols = max(20, min(cols, 240))
        self.rows = max(8, min(rows, 100))
        if self.alive:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', self.rows, self.cols, 0, 0))
            self._emit({'type': 'resize', 'cols': self.cols, 'rows': self.rows})

    def subscribe(self) -> tuple[asyncio.Queue[dict[str, Any]], int]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=4096)
        self.subscribers.add(queue)
        return queue, self.sequence

    async def close(self) -> None:
        if self.alive:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGHUP)
            self._exited()
        for fd in (self.fd, self.meta_fd):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        if self.pid > 0:
            with contextlib.suppress(ChildProcessError):
                await asyncio.to_thread(os.waitpid, self.pid, 0)
        if self.journal:
            self.journal.close()
