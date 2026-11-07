"""Tests use actual Bash processes and the actual Harness² library, without model stubs."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

import pytest
import pytest_asyncio

from terminal_app.agent import SYSTEM
from terminal_app.harness_memory import HarnessMemory
from terminal_app.skills import SkillRegistry
from terminal_app.store import Store
from terminal_app.terminal import TerminalSession


@pytest_asyncio.fixture
async def terminal(tmp_path: Path):
    # Keep the real child at the test runner's uid so pytest's private tmp dirs are reachable.
    before = os.environ.get('TERMINAL_UID')
    os.environ['TERMINAL_UID'] = str(os.getuid())
    os.environ['TERMINAL_GID'] = str(os.getgid())
    store = Store(tmp_path / 'state/memory.sqlite3')
    session = TerminalSession(tmp_path / 'state', tmp_path / 'workspace', store)
    await session.start()
    try:
        yield session
    finally:
        await session.close()
        store.close()
        if before is None:
            os.environ.pop('TERMINAL_UID', None)
        else:
            os.environ['TERMINAL_UID'] = before
        os.environ.pop('TERMINAL_GID', None)


async def test_real_pty_exit_codes_and_persistent_shell(terminal):
    first = await terminal.execute("mkdir -p nested; cd nested; export TERMINAL_TEST=retained; printf 'first\\n'")
    assert first['exit_code'] == 0
    second = await terminal.execute('printf "%s\\n" "$TERMINAL_TEST"; pwd; test -t 0')
    assert second['exit_code'] == 0
    assert 'retained' in second['output']
    assert second['cwd'].endswith('/nested')
    failed = await terminal.execute('false')
    assert failed['exit_code'] == 1


async def test_unicode_multiline_and_recording_bytes(terminal):
    result = await terminal.execute("python3 - <<'PY'\nprint('terminal.. £ café ✓')\nPY")
    assert result['exit_code'] == 0
    assert 'terminal.. £ café ✓' in result['output']
    events = [json.loads(line) for line in terminal.journal_path.read_text().splitlines()]
    raw = b''.join(base64.b64decode(e['data']) for e in events if e['type'] == 'output')
    assert 'terminal.. £ café ✓'.encode() in raw
    assert [e['seq'] for e in events] == list(range(1, len(events) + 1))


async def test_interactive_input_and_timeout(terminal):
    job = asyncio.create_task(terminal.execute('read -r answer; printf "received:%s\\n" "$answer"', timeout=5))
    await asyncio.sleep(0.1)
    await terminal.write(b'hello-linux\n')
    result = await job
    assert result['exit_code'] == 0
    assert 'received:hello-linux' in result['output']
    timed = await terminal.execute('sleep 20', timeout=1)
    assert timed['status'] == 'timed_out'
    assert timed['exit_code'] is None
    after = await terminal.execute('printf "still-alive\\n"')
    assert after['exit_code'] == 0


async def test_memory_evidence_and_harness_restart(terminal, tmp_path):
    task = terminal.store.create_task('Inspect Python version', [], ['python-workbench'])
    result = await terminal.execute('python3 --version', task['id'])
    terminal.store.task_status(task['id'], 'completed', 'Python version inspected.')
    skills = SkillRegistry(tmp_path / 'skills', terminal.store)
    harness = HarnessMemory(tmp_path / 'harness')
    prompt, tree = harness.prepare(task['id'], SYSTEM, skills.all(), [])
    assert 'Linux essentials' in prompt
    commands = terminal.store.commands(task['id'])
    adopted = harness.adopt(task['id'], tree, 'Inspect Python', 'Use python3 --version to inspect Python.', commands)
    assert adopted and (adopted / 'EXPERIENCE.md').is_file()
    terminal.store.remember('Inspect Python', 'Use python3 --version to inspect Python.', 'procedure', [result['id']])
    reopened = Store(tmp_path / 'state/memory.sqlite3')
    try:
        recalled = reopened.recall('Which Python version is installed?')
        assert recalled[0]['evidence'] == [result['id']]
        assert reopened.commands()[0]['exit_code'] == 0
        restored = HarnessMemory(tmp_path / 'harness')
        assert task['id'] in restored.library.entries()
        assert 'python3 --version' in restored.library.tree_of(task['id'])['LongTermMEMORY.md']
    finally:
        reopened.close()


async def test_failed_commands_cannot_be_learned_as_success(terminal):
    result = await terminal.execute('false')
    with pytest.raises(ValueError, match='exit code 0'):
        terminal.store.remember('Incorrect', 'This failed.', 'procedure', [result['id']])


async def test_terminal_dimensions_are_real(terminal):
    terminal.resize(120, 35)
    result = await terminal.execute('stty size')
    assert '35 120' in result['output']


def test_interrupted_tasks_and_commands_are_recovered(tmp_path):
    path = tmp_path / 'memory.sqlite3'
    store = Store(path)
    task = store.create_task('Interrupted task', [], [])
    store.task_status(task['id'], 'running')
    store.command_start(task['id'], 'sleep 100', '/workspace', 'session')
    store.close()
    reopened = Store(path)
    try:
        assert reopened.task(task['id'])['status'] == 'interrupted'
        assert reopened.commands()[0]['status'] == 'interrupted'
        assert reopened.commands()[0]['exit_code'] is None
    finally:
        reopened.close()


def test_skill_ids_cannot_escape_the_registry(tmp_path):
    store = Store(tmp_path / 'db.sqlite3')
    registry = SkillRegistry(tmp_path / 'skills', store)
    try:
        with pytest.raises(ValueError):
            registry.save('../outside', 'Invalid', 'Invalid', '# Invalid')
        registry.save('my-skill', 'Mine', 'A custom skill', 'Use pwd to inspect the current directory.')
        assert any(skill['id'] == 'my-skill' for skill in registry.all())
    finally:
        store.close()


async def test_weighted_checks_use_real_output_not_echoed_command(terminal):
    from terminal_app.learning import verify
    checks = [
        {'name': 'Expected stdout', 'command': "printf 'real-value'", 'expected': 'real-value', 'weight': 3},
        {'name': 'Echo is not evidence', 'command': "true # magic-token", 'expected': 'magic-token', 'weight': 1},
        {'name': 'Exit code matters', 'command': "printf 'present'; false", 'expected': 'present', 'weight': 2},
    ]
    report = await verify(terminal, 'test-task', terminal.workspace, checks, terminal.workspace / 'checks')
    assert report['score'] == 50
    assert [c['passed'] for c in report['checks']] == [True, False, False]
    assert all(terminal.store.rows('SELECT id FROM commands WHERE id=?', (c['command_id'],)) for c in report['checks'])


async def test_scoring_absent_timeout_and_durable_report(terminal):
    from terminal_app.learning import verify
    task = terminal.store.create_task('Verify output', [], [], {'enabled': True, 'rounds': 1, 'checks': []})
    empty = await verify(terminal, task['id'], terminal.workspace, [], terminal.workspace / 'checks')
    assert empty['score'] is None
    failed = await verify(terminal, task['id'], terminal.workspace, [
        {'name': 'Deadline', 'command': 'sleep 30', 'weight': 1, 'timeout': 1},
    ], terminal.workspace / 'checks')
    assert failed['score'] == 0 and failed['checks'][0]['exit_code'] is None
    terminal.store.save_trial(task['id'], 'base1', failed)
    terminal.store.learning_report(task['id'], {'baseline': 0, 'candidate': 100, 'delta': 100})
    assert terminal.store.task(task['id'])['learning']['enabled']
    assert terminal.store.task(task['id'])['learning_report']['delta'] == 100
    assert terminal.store.trials(task['id'])[0]['score'] == 0


def test_learning_requires_checks_and_browser_starts_offline(tmp_path):
    from pydantic import ValidationError

    from terminal_app.app import LearningInput
    from terminal_app.browser import status
    with pytest.raises(ValidationError, match='at least one'):
        LearningInput(enabled=True)
    with pytest.raises(ValidationError):
        LearningInput(rounds=10)
    assert status(tmp_path)['running'] is False
