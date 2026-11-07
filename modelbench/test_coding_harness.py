"""Regression checks using real files, SQLite and (when present) the real Docker runner.

Run: python3 -m unittest -v test_coding_harness
No model or command-execution mocks are used.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import ClassVar

from coding_harness import DEFAULT_IMAGE, CodingHarness
from harness_core import (
    Cancelled,
    Control,
    HarnessError,
    Sandbox,
    Store,
    apply_tree,
    atomic_write,
    changed_files,
    copy_tree,
    digest,
    owned,
    parse_object,
    recover_journal,
    relative_path,
    safe_path,
    scan_tree,
    tree_diff,
    validate_plan,
)
from harness_integration import chat_capable


class FilesystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.py").write_text("value = 1\n")

    def test_snapshot_preserves_source_and_excludes_credentials(self) -> None:
        (self.source / ".env").write_text("SECRET=never-copy")
        (self.source / ".env.example").write_text("SECRET=")
        (self.source / ".git").mkdir()
        (self.source / ".git" / "config").write_text("private")
        copy = self.root / "copy"
        manifest = copy_tree(self.source, copy)
        self.assertEqual(set(manifest), {"main.py", ".env.example"})
        (copy / "main.py").write_text("value = 2\n")
        self.assertEqual((self.source / "main.py").read_text(), "value = 1\n")

    def test_paths_refuse_traversal_credentials_and_windows_drives(self) -> None:
        for value in ("../escape", "/etc/passwd", "a/../../b", "a//b", "./x", ".git/config", ".env", "x.pem", "C:\\tmp\\x", "a\0b"):
            with self.subTest(value=value), self.assertRaises(HarnessError):
                safe_path(self.source, value)

    def test_symlink_cannot_escape(self) -> None:
        (self.source / "outside").symlink_to(self.root)
        with self.assertRaises(HarnessError):
            safe_path(self.source, "outside/secret")
        with self.assertRaises(HarnessError):
            scan_tree(self.source)

    def test_hardlink_cannot_alias_another_file(self) -> None:
        os.link(self.source / "main.py", self.source / "alias.py")
        with self.assertRaises(HarnessError):
            scan_tree(self.source)

    def test_equal_size_changes_are_detected(self) -> None:
        before = scan_tree(self.source)
        (self.source / "main.py").write_text("value = 2\n")
        self.assertEqual(changed_files(before, scan_tree(self.source)), ["main.py"])

    def test_diff_includes_created_and_deleted_files(self) -> None:
        after = self.root / "after"
        copy_tree(self.source, after)
        (after / "main.py").unlink()
        (after / "other.py").write_text("value = 2\n")
        result = tree_diff(self.source, after)
        self.assertEqual([f["path"] for f in result["files"]], ["main.py", "other.py"])
        self.assertIn("/dev/null", result["text"])

    def test_apply_and_rollback_preserve_unrelated_edits(self) -> None:
        before, after = self.root / "before", self.root / "after"
        copy_tree(self.source, before)
        copy_tree(before, after)
        (after / "main.py").write_text("value = 2\n")
        (after / "tests.py").write_text("assert True\n")
        (self.source / "local.txt").write_text("unrelated edit")
        changed = apply_tree(before, after, self.source, self.root / "apply")
        self.assertEqual(changed, ["main.py", "tests.py"])
        self.assertEqual((self.source / "main.py").read_text(), "value = 2\n")
        apply_tree(after, before, self.source, self.root / "rollback")
        self.assertEqual((self.source / "main.py").read_text(), "value = 1\n")
        self.assertFalse((self.source / "tests.py").exists())
        self.assertEqual((self.source / "local.txt").read_text(), "unrelated edit")

    def test_conflict_refuses_to_overwrite_newer_changes(self) -> None:
        before, after = self.root / "before", self.root / "after"
        copy_tree(self.source, before)
        copy_tree(before, after)
        (after / "main.py").write_text("value = 2\n")
        (self.source / "main.py").write_text("value = 9\n")
        with self.assertRaisesRegex(HarnessError, "conflicts"):
            apply_tree(before, after, self.source, self.root / "apply")
        self.assertEqual((self.source / "main.py").read_text(), "value = 9\n")

    def test_partial_apply_rolls_back_on_filesystem_error(self) -> None:
        before, after = self.root / "before", self.root / "after"
        copy_tree(self.source, before)
        copy_tree(before, after)
        (after / "main.py").write_text("value = 2\n")
        (after / "z.txt").write_text("new")
        (self.source / "z.txt").mkdir()
        with self.assertRaises(OSError):
            apply_tree(before, after, self.source, self.root / "apply")
        self.assertEqual((self.source / "main.py").read_text(), "value = 1\n")
        receipt = json.loads((self.root / "apply" / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "rolled_back")

    def test_interrupted_transaction_recovers_from_durable_preimage(self) -> None:
        before, after = self.root / "before", self.root / "after"
        copy_tree(self.source, before)
        copy_tree(before, after)
        (after / "main.py").write_text("value = 2\n")
        journal = self.root / "apply"
        apply_tree(before, after, self.source, journal)
        receipt = json.loads((journal / "receipt.json").read_text())
        receipt["status"] = "preparing"
        atomic_write(journal / "receipt.json", json.dumps(receipt).encode())
        self.assertEqual(recover_journal(journal), "rolled_back_after_restart")
        self.assertEqual((self.source / "main.py").read_text(), "value = 1\n")

    def test_interrupted_recovery_preserves_later_user_edits(self) -> None:
        before, after = self.root / "before", self.root / "after"
        copy_tree(self.source, before)
        copy_tree(before, after)
        (after / "main.py").write_text("value = 2\n")
        journal = self.root / "apply"
        apply_tree(before, after, self.source, journal)
        receipt = json.loads((journal / "receipt.json").read_text())
        receipt["status"] = "preparing"
        atomic_write(journal / "receipt.json", json.dumps(receipt).encode())
        (self.source / "main.py").write_text("user edit\n")
        with self.assertRaisesRegex(HarnessError, "newer edits"):
            recover_journal(journal)
        self.assertEqual((self.source / "main.py").read_text(), "user edit\n")


class PlanningTests(unittest.TestCase):
    def test_valid_dependency_plan(self) -> None:
        value = {"tasks": [{"id": "code", "agent": 0, "instruction": "Implement", "files": ["src/"]},
                           {"id": "tests", "agent": 1, "instruction": "Test", "files": ["tests/"], "depends_on": ["code"]}],
                 "checks": ["python3 -m unittest discover -s tests -v"]}
        plan = validate_plan(value, 2)
        self.assertEqual(plan["tasks"][1]["depends_on"], ["code"])

    def test_cycles_unknown_dependencies_and_agents_fail_closed(self) -> None:
        for dependency, agent in ((["a"], 0), (["unknown"], 0), ([], 9)):
            with self.subTest(dependency=dependency, agent=agent), self.assertRaises(HarnessError):
                validate_plan({"tasks": [{"id": "a", "agent": agent, "instruction": "Do work", "files": ["*"], "depends_on": dependency}]}, 1)

    def test_no_plan_is_not_silently_fanned_out(self) -> None:
        with self.assertRaises(HarnessError):
            validate_plan({"tasks": []}, 3)

    def test_duplicate_ids_fail(self) -> None:
        task = {"id": "a", "agent": 0, "instruction": "Do work", "files": ["*"]}
        with self.assertRaises(HarnessError):
            validate_plan({"tasks": [task.copy(), task.copy()]}, 1)

    def test_ownership_does_not_match_sibling_prefix(self) -> None:
        self.assertTrue(owned("src/main.py", ["src/"]))
        self.assertFalse(owned("src-other/main.py", ["src/"]))
        self.assertFalse(owned("tests.py", ["test.py"]))
        self.assertEqual(relative_path("tests/test_main.py"), "tests/test_main.py")

    def test_malformed_master_response_is_rejected(self) -> None:
        for text in ('{"approved":', '[]', 'The answer is {"approved":true}'):
            with self.subTest(text=text), self.assertRaises((ValueError, HarnessError)):
                parse_object(text)
        self.assertEqual(parse_object('```json\n{"approved":false}\n```'), {"approved": False})

    def test_embedding_model_is_not_a_coding_model(self) -> None:
        self.assertFalse(chat_capable({"model": "bge-m3-q8_0", "pid": 0}))
        self.assertFalse(chat_capable({"model": "nomic-embed-text", "pid": 0}))
        self.assertTrue(chat_capable({"model": "coder-model", "pid": 0}))


class PersistenceAndToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = self.root / "app"
        self.app.mkdir()
        self.projects = self.root / "projects"
        self.projects.mkdir()
        (self.app / "coding_harness.json").write_text(json.dumps({"projects_dir": str(self.projects),
                                                               "allowed_roots": [str(self.projects)]}))
        self.harness = CodingHarness(self.app, list, start=False)
        self.workspace = self.harness.create_workspace({"name": "test project"})
        self.project = Path(self.workspace["path"])

    def test_editor_uses_optimistic_concurrency(self) -> None:
        initial = self.harness.save_file({"workspace": self.workspace["id"], "path": "a.py", "content": "x = 1\n", "expected_sha": ""})
        with self.assertRaises(HarnessError):
            self.harness.save_file({"workspace": self.workspace["id"], "path": "a.py", "content": "x = 2\n", "expected_sha": ""})
        result = self.harness.save_file({"workspace": self.workspace["id"], "path": "a.py", "content": "x = 2\n", "expected_sha": initial["sha"]})
        self.assertEqual(result["sha"], digest(b"x = 2\n"))

    def test_replace_requires_unique_text(self) -> None:
        (self.project / "a.py").write_text("x x\n")
        with self.assertRaisesRegex(HarnessError, "exactly once"):
            self.harness._edit(self.project, "replace_text", {"path": "a.py", "old": "x", "new": "y", "expected_sha": digest(b"x x\n")}, ["*"])
        self.assertEqual((self.project / "a.py").read_text(), "x x\n")

    def test_tool_schema_rejects_unknown_tool_and_scope(self) -> None:
        with self.assertRaises(HarnessError):
            self.harness._tool(self.project, "host_shell", {"command": "true"}, ["*"], Control())
        with self.assertRaises(HarnessError):
            self.harness._tool(self.project, "write_file", {"path": "other.py", "content": "x", "expected_sha": ""}, ["main.py"], Control())
        with self.assertRaises(HarnessError):
            self.harness._tool(self.project, "write_file", {"path": "main.py", "content": "x"}, ["*"], Control())

    def test_cancelled_control_prevents_any_tool_execution(self) -> None:
        control = Control()
        control.cancel()
        with self.assertRaises(Cancelled):
            self.harness._tool(self.project, "write_file", {"path": "a.py", "content": "x", "expected_sha": ""}, ["*"], control)
        self.assertFalse((self.project / "a.py").exists())

    def test_sqlite_event_cursors_are_durable_and_ordered(self) -> None:
        store = Store(self.root / "db")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(store.event, "run", "test", number=i) for i in range(40)]
            for future in futures:
                future.result()
        reopened = Store(self.root / "db")
        events = reopened.events("run")
        self.assertEqual(len(events), 40)
        self.assertEqual([e["seq"] for e in events], sorted(e["seq"] for e in events))
        self.assertEqual(len(reopened.events("run", events[19]["seq"])), 20)

    def test_restart_marks_inflight_runs_interrupted(self) -> None:
        run = {"id": "a" * 32, "workspace": self.workspace["id"], "status": "running", "created": time.time()}
        self.harness.store.save(run)
        recovered = CodingHarness(self.app, list, start=False)
        self.assertEqual(recovered.store.run(run["id"])["status"], "interrupted")

    def test_workspace_root_cannot_be_service_or_credential_folder(self) -> None:
        for folder in (self.app, self.projects):
            with self.assertRaises(HarnessError):
                self.harness.create_workspace({"name": "bad", "path": str(folder)})

    def test_failed_run_cannot_be_applied(self) -> None:
        run = {"id": "b" * 32, "workspace": self.workspace["id"], "status": "failed", "created": time.time()}
        self.harness.store.save(run)
        with self.assertRaisesRegex(HarnessError, "must be ready"):
            self.harness.action(run["id"], "apply", {})

    def test_apply_rollback_and_reapply_actions(self) -> None:
        ident = "c" * 32
        home = self.harness.data / "runs" / ident
        home.mkdir(parents=True)
        copy_tree(self.project, home / "baseline")
        copy_tree(self.project, home / "stage")
        (home / "stage" / "main.py").write_text("answer = 42\n")
        run = {"id": ident, "workspace": self.workspace["id"], "status": "ready", "created": time.time(),
               "approved_tree": scan_tree(home / "stage")}
        self.harness.store.save(run)
        self.assertEqual(self.harness.action(ident, "apply", {})["status"], "applied")
        self.assertEqual((self.project / "main.py").read_text(), "answer = 42\n")
        self.assertEqual(self.harness.action(ident, "rollback", {})["status"], "rolled_back")
        self.assertFalse((self.project / "main.py").exists())
        self.assertEqual(self.harness.action(ident, "apply", {})["status"], "applied")

    def test_staged_edit_after_validation_cannot_be_applied(self) -> None:
        ident = "d" * 32
        home = self.harness.data / "runs" / ident
        home.mkdir(parents=True)
        copy_tree(self.project, home / "baseline")
        copy_tree(self.project, home / "stage")
        (home / "stage" / "main.py").write_text("answer = 42\n")
        run = {"id": ident, "workspace": self.workspace["id"], "status": "ready", "created": time.time(),
               "approved_tree": scan_tree(home / "stage")}
        self.harness.store.save(run)
        (home / "stage" / "main.py").write_text("answer = 99\n")
        with self.assertRaisesRegex(HarnessError, "changed after review"):
            self.harness.action(ident, "apply", {})


class RealSandboxTests(unittest.TestCase):
    sandbox: ClassVar[Sandbox]

    @classmethod
    def setUpClass(cls) -> None:
        cls.sandbox = Sandbox(os.environ.get("HARNESS_TEST_IMAGE", DEFAULT_IMAGE))
        if not cls.sandbox.available()["ready"]:
            raise unittest.SkipTest("Real Docker image unavailable on this host; build it with: docker build -t aibox-sandbox:latest sandbox/")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_real_exit_code_and_stderr_are_retained(self) -> None:
        result = self.sandbox.run(self.root, "echo failure >&2; exit 7", Control())
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("failure", result["output"])

    def test_terminal_is_disposable_and_cleans_its_copy(self) -> None:
        here = self.root / "service"
        here.mkdir()
        source = self.root / "project"
        source.mkdir()
        (source / "keep.txt").write_text("original")
        (here / "coding_harness.json").write_text(json.dumps({
            "data_dir": str(here / "data"), "projects_dir": str(self.root / "projects"),
            "allowed_roots": [str(self.root)], "image": self.sandbox.image,
        }))
        harness = CodingHarness(here, list, start=False)
        workspace = harness.create_workspace({"name": "terminal-test", "path": str(source)})
        result = harness.terminal({"workspace": workspace["id"], "command": "printf changed > keep.txt; exit 7"})
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual((source / "keep.txt").read_text(), "original")
        self.assertEqual(list((harness.data / "terminal").iterdir()), [])

    def test_isolation_has_no_host_home_or_docker_socket(self) -> None:
        command = "python3 -c \"from pathlib import Path; assert not Path('/root/.ssh').exists(); assert not Path('/var/run/docker.sock').exists(); assert Path('/workspace').is_dir(); caps=[x for x in Path('/proc/self/status').read_text().splitlines() if x.startswith('CapEff:')][0]; assert int(caps.split()[1],16)==0; print('isolation OK')\""
        result = self.sandbox.run(self.root, command, Control())
        self.assertEqual(result["exit_code"], 0, result["output"])
        self.assertIn("isolation OK", result["output"])

    def test_timeout_kills_the_actual_container(self) -> None:
        start = time.monotonic()
        result = self.sandbox.run(self.root, "sleep 30", Control(), timeout=1)
        self.assertEqual(result["exit_code"], 124)
        self.assertTrue(result["timed_out"])
        self.assertLess(time.monotonic() - start, 10)

    def test_cancellation_kills_the_actual_container(self) -> None:
        control = Control()
        timer = threading.Timer(1.0, control.cancel)
        timer.start()
        self.addCleanup(timer.cancel)
        start = time.monotonic()
        with self.assertRaises(Cancelled):
            self.sandbox.run(self.root, "sleep 30", control)
        self.assertLess(time.monotonic() - start, 10)


if __name__ == "__main__":
    unittest.main()
