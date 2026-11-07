"""Dedicated Chromium profile, controlled by visible terminal-browser commands."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def browser_root() -> Path:
    return Path(os.environ.get('TERMINAL_BROWSER_DIR', str(Path.home() / '.terminal-browser')))


def status(root: Path) -> dict[str, Any]:
    try:
        value: dict[str, Any] = json.loads((root / 'state.json').read_text())
        os.kill(int(value['pid']), 0)
        value['running'] = time.time() - float(value['updated']) < 60
        return value
    except (OSError, ValueError, KeyError):
        return {'running': False, 'url': '', 'title': '', 'tabs': []}


async def request(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(str(root / 'control.sock'), limit=2_000_000)
    try:
        writer.write((json.dumps(payload) + '\n').encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), 50)
        value: dict[str, Any] = json.loads(line)
        return value
    finally:
        writer.close()
        await writer.wait_closed()


async def serve(root: Path) -> None:
    from playwright.async_api import async_playwright

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    lock = (root / 'lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    socket = root / 'control.sock'
    socket.unlink(missing_ok=True)
    stopping = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stopping.set)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(root / 'profile'), headless=True, viewport={'width': 1280, 'height': 800},
            accept_downloads=True, downloads_path=str(root / 'downloads'),
        )
        context.set_default_timeout(15000)
        page = context.pages[0] if context.pages else await context.new_page()
        operation_lock = asyncio.Lock()

        async def save_download(download: Any) -> None:
            name = Path(download.suggested_filename).name
            await download.save_as(str(root / 'downloads' / f'{time.time_ns()}-{name}'))

        def on_page(new_page: Any) -> None:
            nonlocal page
            page = new_page
            new_page.on('download', lambda d: asyncio.create_task(save_download(d)))

        for existing in context.pages:
            existing.on('download', lambda d: asyncio.create_task(save_download(d)))
        context.on('page', on_page)

        async def publish() -> None:
            nonlocal page
            if page.is_closed():
                page = context.pages[-1] if context.pages else await context.new_page()
            frame = await page.screenshot(type='png', timeout=5000)
            temp = root / 'frame.tmp'
            temp.write_bytes(frame)
            temp.replace(root / 'frame.png')
            value = {'running': True, 'pid': os.getpid(), 'updated': time.time(),
                     'url': page.url, 'title': await page.title(), 'width': 1280, 'height': 800,
                     'tabs': [{'index': i, 'url': tab.url, 'active': tab == page} for i, tab in enumerate(context.pages)]}
            tmp = root / 'state.tmp'
            tmp.write_text(json.dumps(value))
            tmp.replace(root / 'state.json')

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal page
            try:
                data = json.loads(await asyncio.wait_for(reader.readline(), 5))
                action, args = data['action'], data.get('args', [])
                async with operation_lock:
                    result: dict[str, Any] = {}
                    if action in ('open', 'new'):
                        if action == 'new':
                            page = await context.new_page()
                        url = str(args[0])
                        if not url.startswith(('http://', 'https://', 'about:', 'file://')):
                            raise ValueError('Use an http, https, about or file URL.')
                        response = await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                        result['http_status'] = response.status if response else None
                    elif action == 'snapshot':
                        result['snapshot'] = (await page.locator('body').aria_snapshot())[:100000]
                    elif action == 'click':
                        await page.locator(str(args[0])).click()
                    elif action == 'tap':
                        await page.mouse.click(float(args[0]), float(args[1]))
                    elif action == 'fill':
                        await page.locator(str(args[0])).fill(str(args[1]))
                    elif action == 'select':
                        loc = page.locator(str(args[0]))
                        try:
                            await loc.select_option(label=str(args[1]))
                        except Exception:
                            await loc.select_option(str(args[1]))
                    elif action == 'type':
                        await page.keyboard.insert_text(str(args[0]))
                    elif action == 'press':
                        await page.keyboard.press(str(args[0]))
                    elif action == 'scroll':
                        await page.mouse.wheel(0, float(args[0]))
                    elif action == 'tab':
                        page = context.pages[int(args[0])]
                    elif action == 'back':
                        await page.go_back(wait_until='domcontentloaded')
                    elif action == 'reload':
                        await page.reload(wait_until='domcontentloaded')
                    elif action == 'close':
                        await page.close()
                    elif action == 'stop':
                        stopping.set()
                    elif action not in ('status', 'screenshot', 'tabs'):
                        raise ValueError('Unknown browser command.')
                    if action != 'stop':
                        await publish()
                    result.update(status(root))
                    if action == 'screenshot':
                        result['path'] = str(root / 'frame.png')
                    with (root / 'events.jsonl').open('a') as journal:
                        # Text entered into forms is never duplicated into this event log.
                        journal.write(json.dumps({'at': time.time(), 'action': action, 'url': page.url}) + '\n')
                    writer.write((json.dumps({'ok': True, **result}) + '\n').encode())
            except Exception as exc:
                writer.write((json.dumps({'ok': False, 'error': str(exc)[:3000]}) + '\n').encode())
            finally:
                with contextlib.suppress(ConnectionError):
                    await writer.drain()
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_unix_server(handle, str(socket), limit=200000)
        socket.chmod(0o600)
        try:
            while not stopping.is_set():
                async with operation_lock:
                    with contextlib.suppress(Exception):
                        await publish()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stopping.wait(), 1.5)
        finally:
            server.close()
            await server.wait_closed()
            await context.close()
            socket.unlink(missing_ok=True)
            (root / 'state.json').write_text(json.dumps({'running': False, 'url': '', 'tabs': []}))
            lock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description='Your persistent Chromium browser, controlled from the terminal.')
    parser.add_argument('action', choices=['start', 'serve', 'open', 'new', 'snapshot', 'click', 'tap', 'fill', 'select',
                                          'type', 'press', 'scroll', 'tab', 'tabs', 'back', 'reload', 'close',
                                          'screenshot', 'status', 'stop'])
    parser.add_argument('args', nargs='*')
    values = parser.parse_args()
    root = browser_root()
    try:
        if values.action == 'serve':
            asyncio.run(serve(root))
            return
        if values.action == 'start':
            if not status(root)['running']:
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                with (root / 'browser.log').open('a') as log:
                    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent.parent / 'scripts/terminal-browser'), 'serve'],
                                             stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
                for _ in range(100):
                    if status(root)['running']:
                        break
                    if child.poll() is not None:
                        raise RuntimeError(f'Chromium could not start. Read {root / "browser.log"}.')
                    time.sleep(0.2)
                else:
                    raise RuntimeError(f'Browser startup timed out. Read {root / "browser.log"}.')
            print(json.dumps(status(root), indent=2))
            return
        result = asyncio.run(request(root, {'action': values.action, 'args': values.args}))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if not result['ok']:
            sys.exit(1)
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(f'Browser: {exc}. Use terminal-browser start to launch it.', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
