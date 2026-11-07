"""Run inside the started Docker container to test the real browser and persistence."""
from __future__ import annotations

import json
import os
import shlex
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def main() -> None:
    token = (Path(os.environ.get('TERMINAL_DATA', '/data')) / 'token').read_text().strip()

    def api(path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request('http://127.0.0.1:8765/api' + path,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=90) as response:
            result: dict[str, Any] = json.load(response)
            return result

    def command(value: str) -> dict[str, Any]:
        result = api('/command', {'command': value})
        assert result['status'] == 'completed' and result['exit_code'] == 0, result['output']
        return result

    # A unique localhost page gives browser storage a real origin.
    name = '.terminal-smoke-' + uuid.uuid4().hex
    page = Path('/workspace') / name / 'index.html'
    html = '''<!doctype html><title>terminal browser acceptance</title><label>Name<input id="name"></label><button id="save" onclick="localStorage.setItem('name',document.querySelector('#name').value);document.querySelector('#result').textContent='Saved '+localStorage.getItem('name')">Save</button><p id="result"></p><script>document.querySelector('#result').textContent=localStorage.getItem('name')||'Empty';</script>'''
    code = f'from pathlib import Path; p=Path({str(page)!r}); p.parent.mkdir(); p.write_text({html!r})'
    command('python3 -c ' + shlex.quote(code))
    command(f'python3 -m http.server 8081 --bind 0.0.0.0 --directory /workspace > /workspace/{name}/http.log 2>&1 &')
    command('terminal-browser start')
    command('terminal-browser open ' + shlex.quote('http://127.0.0.1:8081/' + name + '/index.html'))
    command("terminal-browser fill '#name' 'Persistent browser evidence'")
    command("terminal-browser click '#save'")
    snapshot = command('terminal-browser snapshot')['output']
    assert 'Saved Persistent browser evidence' in snapshot
    command('terminal-browser stop')
    # Wait for graceful shutdown without racing the old socket.
    command('for i in $(seq 1 50); do [ ! -S "$TERMINAL_BROWSER_DIR/control.sock" ] && break; sleep 0.1; done; test ! -S "$TERMINAL_BROWSER_DIR/control.sock"')
    command('terminal-browser start')
    command('terminal-browser open ' + shlex.quote('http://127.0.0.1:8081/' + name + '/index.html'))
    snapshot = command('terminal-browser snapshot')['output']
    assert 'Persistent browser evidence' in snapshot
    state = api('/state')
    assert state['browser']['running'] and state['harness']['recursive_rollouts']
    request = urllib.request.Request('http://127.0.0.1:8765/api/browser/frame', headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.read(8) == b'\x89PNG\r\n\x1a\n'
    print('Runtime acceptance passed: real Chromium navigation, form input, persistent profile, PNG view, terminal records.')


if __name__ == '__main__':
    main()
