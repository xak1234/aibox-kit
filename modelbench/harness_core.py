"""Persistence, bounded project snapshots and container execution for Modelbench.

Python 3.12 standard library only. The model never receives host execution tools.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

Json = dict[str, Any]
MAX_FILE = 2 * 1024 * 1024
MAX_TREE = 64 * 1024 * 1024
MAX_FILES = 5000
SKIP = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", ".next",
        ".ssh", ".aws", ".gnupg", ".config", ".cache", "harness-data"}
PRIVATE = {"secrets.json", "credentials.json", "token.txt", "id_rsa", "id_ed25519"}
ACTIVE = {"queued", "planning", "running", "validating", "reviewing", "cancelling", "applying"}


class HarnessError(RuntimeError):
    """An actionable failure which is safe to show in the UI."""


class Cancelled(HarnessError):
    """Explicit user cancellation or elapsed run deadline."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def private_path(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return any(p in SKIP or p.lower() in PRIVATE or p == ".env" or
               p.startswith(".env.") and p not in {".env.example", ".env.sample"} or
               p.lower().endswith((".pem", ".key", ".p12", ".pfx")) for p in parts)


def relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise HarnessError("A non-empty relative file path is required.")
    if "\\" in value or ":" in value or any(ord(c) < 32 for c in value):
        raise HarnessError("Use a relative path with forward slashes.")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in {"", ".", ".."} for x in value.split("/")) or private_path(value):
        raise HarnessError("Path is outside the project or is a protected file: " + value)
    return str(p)


def safe_path(root: Path, value: Any) -> Path:
    rel = relative_path(value)
    root = root.resolve(strict=True)
    target = root / rel
    cursor = root
    for part in PurePosixPath(rel).parts:
        cursor /= part
        if cursor.is_symlink():
            raise HarnessError("Symbolic links are not writable/readable through the harness: " + rel)
        if cursor.exists() and cursor.is_file() and cursor.stat().st_nlink != 1:
            raise HarnessError("Hard-linked files are not supported: " + rel)
    if not target.resolve().is_relative_to(root):
        raise HarnessError("Path escapes the project.")
    return target


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".harness-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode & 0o777)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def scan_tree(root: Path) -> dict[str, Json]:
    """Hash bounded ordinary project files. Never follow links or include credentials."""
    root = root.resolve(strict=True)
    result: dict[str, Json] = {}
    total = 0
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for name in dirs:
            if (Path(folder) / name).is_symlink():
                raise HarnessError("Project contains a directory symlink: " + str(Path(folder) / name))
        for name in sorted(names):
            path = Path(folder) / name
            rel = path.relative_to(root).as_posix()
            if private_path(rel) or name.startswith(".harness-"):
                continue
            path = safe_path(root, rel)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise HarnessError("Project contains a non-regular file: " + rel)
            if info.st_size > MAX_FILE:
                raise HarnessError(f"File exceeds the 2 MiB coding snapshot limit: {rel}")
            total += info.st_size
            if total > MAX_TREE or len(result) >= MAX_FILES:
                raise HarnessError("Project exceeds the 64 MiB / 5,000 file coding snapshot limit.")
            data = path.read_bytes()
            result[rel] = {"sha": digest(data), "size": len(data), "mode": stat.S_IMODE(info.st_mode)}
    return result


def copy_tree(source: Path, dest: Path) -> dict[str, Json]:
    manifest = scan_tree(source)
    if dest.exists():
        raise HarnessError("Snapshot destination already exists: " + str(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".harness-snapshot-", dir=dest.parent))
    try:
        for rel, info in manifest.items():
            data = safe_path(source, rel).read_bytes()
            if digest(data) != info["sha"]:
                raise HarnessError("Source changed while taking the snapshot: " + rel)
            atomic_write(temporary / rel, data, info["mode"])
        if scan_tree(source) != manifest:
            raise HarnessError("Project changed during the snapshot; retry when file writes finish.")
        os.rename(temporary, dest)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return manifest


def changed_files(before: dict[str, Json], after: dict[str, Json]) -> list[str]:
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def tree_diff(before: Path, after: Path) -> Json:
    old, new = scan_tree(before), scan_tree(after)
    files = changed_files(old, new)
    sections: list[str] = []
    total = 0
    for rel in files:
        a = (before / rel).read_bytes() if rel in old else b""
        b = (after / rel).read_bytes() if rel in new else b""
        if b"\0" in a or b"\0" in b:
            part = f"Binary file changed: {rel}\n"
        else:
            part = "".join(difflib.unified_diff(a.decode("utf-8", "replace").splitlines(True),
                       b.decode("utf-8", "replace").splitlines(True),
                       fromfile="a/" + rel if rel in old else "/dev/null",
                       tofile="b/" + rel if rel in new else "/dev/null"))
        if old.get(rel, {}).get("mode") != new.get(rel, {}).get("mode"):
            part += f"Mode {old.get(rel, {}).get('mode')} -> {new.get(rel, {}).get('mode')}: {rel}\n"
        total += len(part)
        if total <= 512_000:
            sections.append(part)
    return {"files": [{"path": f, "before": old.get(f), "after": new.get(f)} for f in files],
            "text": "\n".join(sections), "truncated": total > 512_000}


def apply_tree(before: Path, after: Path, target: Path, journal: Path) -> list[str]:
    """Compare-and-swap files with durable preimages; preserve unrelated local edits."""
    old, new = scan_tree(before), scan_tree(after)
    files = changed_files(old, new)
    current = scan_tree(target)
    conflicts = [f for f in files if current.get(f) != old.get(f)]
    if conflicts:
        raise HarnessError("Project changed since this run; resolve these conflicts first: " + ", ".join(conflicts))
    journal.mkdir(parents=True, exist_ok=False)
    receipt: Json = {"status": "preparing", "target": str(target), "files": files,
                     "before": {f: old.get(f) for f in files}, "after": {f: new.get(f) for f in files}}
    for rel in files:
        safe_path(target, rel)
        if rel in old:
            atomic_write(journal / "preimages" / rel, (before / rel).read_bytes(), old[rel]["mode"])
        if rel in new:
            atomic_write(journal / "postimages" / rel, (after / rel).read_bytes(), new[rel]["mode"])
    atomic_write(journal / "receipt.json", json.dumps(receipt).encode())
    done: list[str] = []
    try:
        for rel in files:
            path = safe_path(target, rel)
            # Recheck immediately before replacing each file, including new files.
            actual = {"sha": digest(path.read_bytes()), "size": path.stat().st_size,
                      "mode": stat.S_IMODE(path.stat().st_mode)} if path.exists() else None
            if actual != old.get(rel):
                raise HarnessError("Concurrent edit detected: " + rel)
            if rel in new:
                atomic_write(path, (journal / "postimages" / rel).read_bytes(), new[rel]["mode"])
            else:
                path.unlink()
            done.append(rel)
        receipt["status"] = "applied"
    except BaseException:
        for rel in reversed(done):
            path = safe_path(target, rel)
            if rel in old:
                atomic_write(path, (journal / "preimages" / rel).read_bytes(), old[rel]["mode"])
            elif path.exists():
                path.unlink()
        receipt["status"] = "rolled_back"
        raise
    finally:
        atomic_write(journal / "receipt.json", json.dumps(receipt).encode())
    return files


def recover_journal(journal: Path) -> str:
    """Recover an interrupted file transaction, refusing to overwrite subsequent edits."""
    receipt_path = journal / "receipt.json"
    if not receipt_path.exists():
        return "no_receipt"
    receipt = json.loads(receipt_path.read_text())
    if receipt["status"] != "preparing":
        return str(receipt["status"])
    root = Path(receipt["target"])
    current = scan_tree(root)
    conflicts = [f for f in receipt["files"] if current.get(f) not in
                 (receipt["before"].get(f), receipt["after"].get(f))]
    if conflicts:
        raise HarnessError("Interrupted transaction has newer edits; manual recovery required: " + ", ".join(conflicts))
    for rel in receipt["files"]:
        path = safe_path(root, rel)
        before = receipt["before"].get(rel)
        if before:
            data = (journal / "preimages" / rel).read_bytes()
            if digest(data) != before["sha"]:
                raise HarnessError("Recovery preimage failed its integrity check: " + rel)
            atomic_write(path, data, before["mode"])
        elif path.exists():
            path.unlink()
    receipt["status"] = "rolled_back_after_restart"
    atomic_write(receipt_path, json.dumps(receipt).encode())
    return "rolled_back_after_restart"


def parse_object(text: str) -> Json:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise HarnessError("The master must return a JSON object.")
    return value


def validate_plan(plan: Json, agents: int) -> Json:
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 8:
        raise HarnessError("The master must return one to eight tasks.")
    ids: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise HarnessError("Invalid task object.")
        ident = task.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", ident) or ident in ids:
            raise HarnessError("Task IDs must be unique short names.")
        ids.add(ident)
        if type(task.get("agent")) is not int or not 0 <= task["agent"] < agents:
            raise HarnessError("Task refers to an unavailable agent.")
        if not isinstance(task.get("instruction"), str) or not task["instruction"].strip():
            raise HarnessError("Every task needs a complete instruction.")
        if len(task["instruction"]) > 12000:
            raise HarnessError("Task instruction exceeds its budget.")
        files = task.get("files")
        if not isinstance(files, list) or not files or len(files) > 100:
            raise HarnessError("Every task must declare files or directory prefixes it owns.")
        for path in files:
            if path != "*":
                relative_path(path.rstrip("/") if isinstance(path, str) else path)
        deps = task.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise HarnessError("Task dependencies must be a list of IDs.")
        task["depends_on"] = list(dict.fromkeys(deps))
        task["title"] = str(task.get("title") or ident)[:160]
    finished: set[str] = set()
    while len(finished) < len(tasks):
        ready = [t for t in tasks if t["id"] not in finished and all(d in finished for d in t["depends_on"])]
        if not ready:
            raise HarnessError("Task dependencies contain a cycle or an unknown task.")
        finished.update(t["id"] for t in ready)
    checks = plan.get("checks", [])
    if not isinstance(checks, list) or len(checks) > 6 or any(not isinstance(c, str) or len(c) > 4000 for c in checks):
        raise HarnessError("Validation checks must be up to six shell commands.")
    plan["checks"] = [c for c in checks if c.strip()]
    plan["summary"] = str(plan.get("summary") or "Implementation plan")[:1000]
    return plan


def owned(rel: str, claims: list[str]) -> bool:
    return any(c == "*" or rel == c or c.endswith("/") and rel.startswith(c) for c in claims)


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "runs.sqlite3"
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, workspace TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_run ON events(run_id, seq);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def workspaces(self) -> list[Json]:
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM workspaces ORDER BY rowid")]

    def workspace(self, ident: str) -> Json:
        with self.connection() as db:
            row = db.execute("SELECT data FROM workspaces WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise HarnessError("Unknown workspace.")
        return json.loads(row[0])  # type: ignore[no-any-return]

    def save_workspace(self, data: Json) -> None:
        with self.connection() as db:
            db.execute("INSERT INTO workspaces VALUES(?,?)", (data["id"], json.dumps(data)))

    def save(self, run: Json) -> None:
        run["updated"] = time.time()
        with self.connection() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                       "status=excluded.status,updated=excluded.updated,data=excluded.data",
                       (run["id"], run["workspace"], run["status"], run["created"], run["updated"], json.dumps(run)))

    def run(self, ident: str) -> Json:
        with self.connection() as db:
            row = db.execute("SELECT data FROM runs WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise HarnessError("Unknown run.")
        return json.loads(row[0])  # type: ignore[no-any-return]

    def runs(self, workspace: str = "", limit: int = 50) -> list[Json]:
        with self.connection() as db:
            rows = db.execute("SELECT data FROM runs WHERE (?='' OR workspace=?) ORDER BY created DESC LIMIT ?",
                              (workspace, workspace, limit))
            return [json.loads(row[0]) for row in rows]

    def event(self, ident: str, kind: str, **data: Any) -> None:
        with self.connection() as db:
            db.execute("INSERT INTO events(run_id,ts,data) VALUES(?,?,?)",
                       (ident, time.time(), json.dumps({"kind": kind, **data})))

    def events(self, ident: str, since: int = 0) -> list[Json]:
        with self.connection() as db:
            return [{"seq": r[0], "ts": r[1], **json.loads(r[2])} for r in
                    db.execute("SELECT seq,ts,data FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 150",
                               (ident, since))]


class Control:
    def __init__(self, seconds: int = 1800) -> None:
        self.cancelled = threading.Event()
        self.deadline = time.monotonic() + seconds
        self.lock = threading.Lock()
        self.sockets: list[socket.socket] = []

    def check(self) -> None:
        if self.cancelled.is_set():
            raise Cancelled("Cancelled by the user; completed checkpoints are retained.")
        if time.monotonic() >= self.deadline:
            raise Cancelled("Run time budget reached; completed checkpoints are retained.")

    def cancel(self) -> None:
        self.cancelled.set()
        with self.lock:
            for connection in self.sockets:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


# aibox-kit: when AIBOX_SANDBOX_PRIV is set, containers are started through that root helper (fixed flags,
# fixed image, mount limited to the harness folders) so the service account needs no docker group.
_PRIV = os.environ.get("AIBOX_SANDBOX_PRIV", "")


class Sandbox:
    def __init__(self, image: str, emit: Callable[..., None] | None = None) -> None:
        self.image = image
        self.emit = emit

    def available(self) -> Json:
        try:
            cmd = (["sudo", "-n", _PRIV, "sandbox-check"] if _PRIV
                   else ["docker", "image", "inspect", self.image, "--format", "{{.Id}}"])
            result = subprocess.run(cmd,
                                    capture_output=True, text=True, timeout=10, check=False)
            return {"ready": result.returncode == 0, "image": result.stdout.strip(),
                    "error": result.stderr.strip()[:300]}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ready": False, "error": str(exc)}

    def run(self, root: Path, command: str, control: Control, timeout: int = 90) -> Json:
        control.check()
        if not command.strip() or len(command) > 12000:
            raise HarnessError("Command must contain 1–12,000 characters.")
        timeout = max(1, min(180, timeout))
        if shutil.disk_usage(root).free < 1024 ** 3:
            raise HarnessError("Less than 1 GiB of free disk remains; command refused.")
        name = "mbh-" + uuid.uuid4().hex[:20]
        argv = ["sudo", "-n", _PRIV, "sandbox-run", name, str(timeout), str(root.resolve()), command] if _PRIV else ["docker", "run", "--rm", "--pull=never", "--name", name, "--label", "modelbench.harness=true",
                "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--user", f"{os.getuid()}:{os.getgid()}", "--memory=1g", "--memory-swap=1g", "--cpus=2",
                "--pids-limit=128", "--ulimit", "fsize=33554432:33554432", "--init",
                "--tmpfs", "/tmp:rw,nosuid,size=128m,mode=1777", "--env", "HOME=/tmp",
                "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "CI=1",
                "--mount", f"type=bind,src={root.resolve()},dst=/workspace", "--workdir", "/workspace",
                "--entrypoint", "/usr/bin/timeout", self.image, "--signal=TERM", "--kill-after=5s", str(timeout),
                "/bin/bash", "-o", "pipefail", "-c", command]
        start = time.monotonic()
        timed_out = False
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
            try:
                while process.poll() is None:
                    control.check()
                    if time.monotonic() - start >= timeout:
                        timed_out = True
                        break
                    if output.tell() > 8 * 1024 * 1024:
                        raise HarnessError("Command output exceeded 8 MiB.")
                    control.cancelled.wait(0.15)
            finally:
                if process.poll() is None:
                    subprocess.run(["sudo", "-n", _PRIV, "sandbox-kill", name] if _PRIV else ["docker", "rm", "-f", name],
                                   capture_output=True, timeout=15, check=False)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            output.seek(0)
            raw = output.read(64001)
        timed_out = timed_out or process.returncode == 124
        result = {"command": command, "exit_code": 124 if timed_out else process.returncode,
                  "output": raw[:64000].decode("utf-8", "replace"), "truncated": len(raw) > 64000,
                  "timed_out": timed_out, "seconds": round(time.monotonic() - start, 2),
                  "environment": "Linux container /workspace; network disabled; 1 GiB RAM; 2 CPUs"}
        if self.emit:
            self.emit(**result)
        return result
