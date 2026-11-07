"""Exercise authenticated HTTP and WebSocket routes against the real ASGI runtime."""
from __future__ import annotations

import importlib
import io
import os
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient


def test_auth_commands_memory_export_and_websocket(tmp_path: Path) -> None:
    # A private test runtime, with no user files and no model connection.
    names = ['TERMINAL_DATA', 'TERMINAL_WORKSPACE', 'TERMINAL_UID', 'TERMINAL_GID']
    previous = {name: os.environ.get(name) for name in names}
    os.environ.update(TERMINAL_DATA=str(tmp_path / 'data'), TERMINAL_WORKSPACE=str(tmp_path / 'workspace'),
                      TERMINAL_UID=str(os.getuid()), TERMINAL_GID=str(os.getgid()))
    try:
        module = importlib.import_module('terminal_app.app')
        with TestClient(module.app) as client:
            assert client.get('/api/health').status_code == 200
            assert client.get('/api/state').status_code == 401
            token = (tmp_path / 'data/token').read_text()
            headers = {'Authorization': 'Bearer ' + token}
            state = client.get('/api/state', headers=headers).json()
            assert state['harness']['name'] == 'Harness²'
            assert state['harness']['recursive_rollouts'] is True
            result = client.post('/api/command', headers=headers, json={'command': "printf 'api-real-linux\\n'"}).json()
            assert result['exit_code'] == 0
            assert 'api-real-linux' in result['output']
            task = client.post('/api/tasks', headers=headers, json={'goal': 'Check memory', 'steps': ['Inspect'], 'skills': []}).json()
            assert task['status'] == 'queued'
            # No credential / model means no invented agent execution.
            run = client.post(f'/api/tasks/{task["id"]}/run', headers=headers, json={})
            assert run.status_code == 409
            memory = client.post('/api/memories', headers=headers, json={'title': 'Style', 'content': 'Keep explanations short.', 'kind': 'preference'})
            assert memory.status_code == 200
            with client.websocket_connect('/ws/terminal', headers={'origin': 'http://localhost:8765'}) as websocket:
                websocket.send_json({'token': token})
                assert websocket.receive_json()['type'] == 'reset'
                kinds = []
                while True:
                    event = websocket.receive_json()
                    kinds.append(event['type'])
                    if event['type'] == 'synced':
                        break
                assert 'output' in kinds and 'resize' in kinds
            exported = client.get('/api/export', headers=headers)
            assert exported.status_code == 200
            with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                assert 'memory.sqlite3' in archive.namelist()
                assert any(name.startswith('sessions/') for name in archive.namelist())
                assert 'token' not in archive.namelist()
                assert 'model-key' not in archive.namelist()
            client.post(f'/api/tasks/{task["id"]}/cancel', headers=headers)
            assert client.get('/api/state', headers=headers).json()['tasks'][0]['status'] == 'cancelled'
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
