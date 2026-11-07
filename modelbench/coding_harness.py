"""Durable master/agent coding workflow, embedded into the existing bench HTTP server."""
from __future__ import annotations

import concurrent.futures
import http.client
import json
import queue
import re
import shutil
import socket
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from harness_core import (
    ACTIVE,
    MAX_FILE,
    Cancelled,
    Control,
    HarnessError,
    Json,
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

VERSION = "3.0.0"
DEFAULT_IMAGE = "aibox-sandbox:latest"   # built by install.sh --coding-harness from sandbox/Dockerfile


def function(name: str, description: str, properties: Json, required: list[str]) -> Json:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}}}


TEXT = {"type": "string"}
TOOLS = [
    function("list_files", "List ordinary project files and their SHA256 hashes.", {}, []),
    function("read_file", "Read a UTF-8 project file. Use its SHA256 for subsequent edits.",
             {"path": TEXT, "start_line": {"type": "integer", "minimum": 1},
              "limit": {"type": "integer", "minimum": 1, "maximum": 400}}, ["path"]),
    function("search_files", "Search project text for a literal string, case insensitive; at most 80 matches.",
             {"query": TEXT}, ["query"]),
    function("write_file", "Write a complete UTF-8 file in your assigned scope. expected_sha must match the latest read; empty string only for a new file.",
             {"path": TEXT, "content": TEXT, "expected_sha": TEXT}, ["path", "content", "expected_sha"]),
    function("replace_text", "Replace exactly one literal occurrence. Read the file first and supply its current SHA256.",
             {"path": TEXT, "old": TEXT, "new": TEXT, "expected_sha": TEXT}, ["path", "old", "new", "expected_sha"]),
    function("delete_file", "Delete one file in your scope, after reading its current SHA256.",
             {"path": TEXT, "expected_sha": TEXT}, ["path", "expected_sha"]),
    function("run_command", "Run Bash in your isolated Linux project copy. Python 3, Node, npm and Git are installed. Network and host credentials are unavailable. Inspect exit_code; nonzero is failure.",
             {"command": TEXT, "timeout": {"type": "integer", "minimum": 1, "maximum": 180}}, ["command"]),
]

PLAN_PROMPT = """You are the master of a professional coding team. Produce an executable plan, not code.
The user's source project is snapshotted; agents receive separate copies. Dependencies run first.
Workers can read the whole project, but may modify only declared files or directory prefixes.
Use '*' only when a task needs the whole project; it then runs exclusively. Prefer one task for a small change.
All execution is in an offline Linux container at /workspace, with Python 3, Node, npm and Git.
Do not claim Windows GUI, browser or external-service validation from Linux syntax checks.
Respect project AGENTS.md. Do not read credentials or unrelated host paths.
Return ONLY a JSON object:
{"summary":"short plan", "tasks":[{"id":"implement","title":"short title","agent":0,
"instruction":"self-contained task with acceptance criteria","files":["src/","tests/"],"depends_on":[]}],
"checks":["real command which exercises the result"]}
Agent indices are zero based. Use at most eight tasks and six checks. Do not invent existing test commands.
Include creating meaningful tests in the plan when the change needs them. Checks must fail on incorrect behaviour.
The user-supplied checks, if present, are mandatory and run in addition to your checks.
"""

WORKER_PROMPT = """You are a coding agent. Complete your assigned task by calling the supplied tools.
Write real files; returning code in chat alone does not implement the task. Read files before changing them.
Stay within the declared file ownership. Do not install dependencies over the network or access the host.
The overall request is background context. Do ONLY your assignment, not later tasks owned by another agent.
Do not create tests or documentation assigned to a different task. Use an inline command to check your own piece.
run_command uses Bash on Linux, not PowerShell, and starts at /workspace in your private copy.
Use write_file/replace_text for source edits; inspect actual exit codes and output when checking your work.
You cannot talk to the user or other workers; dependency results and project instructions are provided below.
Finish with a concise account of actual changes, tests executed, and any unresolved issue. Do not invent success.
"""


class CodingHarness:
    def __init__(self, here: Path, models: Callable[[], list[Json]], *, start: bool = True) -> None:
        self.here = here.resolve()
        import aibox_conf as AC
        config_path = here / "coding_harness.json"            # next to the code (tests), else the data dir
        if not config_path.exists():
            config_path = Path(AC.data("modelbench", "coding_harness.json"))
        self.config: Json = json.loads(config_path.read_text()) if config_path.exists() else {}
        self.data = Path(self.config.get("data_dir", AC.data("modelbench", "harness"))).resolve()
        self.projects = Path(self.config.get("projects_dir", AC.get("AIBOX_PROJECTS"))).resolve()
        self.projects.mkdir(parents=True, exist_ok=True)
        self.allowed_roots = [Path(p).resolve() for p in self.config.get("allowed_roots", [str(self.projects)])]
        self.store = Store(self.data)
        self.models_provider = models
        self.sandbox = Sandbox(str(self.config.get("image", AC.get("AIBOX_SANDBOX_IMAGE", DEFAULT_IMAGE))))
        self.lock = threading.RLock()
        self.model_lock = threading.Lock()  # one inference request at a time on the shared APU
        self.controls: dict[str, Control] = {}
        self.pending: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None
        for run in self.store.runs(limit=10000):
            if run["status"] in ACTIVE:
                recovery = []
                home = self.data / "runs" / run["id"]
                for journal in home.glob("*-*"):
                    if journal.is_dir() and (journal / "receipt.json").exists():
                        try:
                            result = recover_journal(journal)
                            if result == "rolled_back_after_restart":
                                recovery.append(str(journal.name))
                        except HarnessError as exc:
                            run["recovery_blocked"] = str(exc)
                for task in run.get("tasks", []):
                    receipt = home / str(task.get("merge_journal") or "missing") / "receipt.json"
                    if task["status"] == "running" and receipt.exists():
                        outcome = json.loads(receipt.read_text())["status"]
                        if outcome == "applied":
                            task["status"] = "completed"
                action_receipt = home / str(run.get("action_journal") or "missing") / "receipt.json"
                if run.get("pending_action") and action_receipt.exists():
                    outcome = json.loads(action_receipt.read_text())["status"]
                    if outcome == "applied":
                        run["status"] = "applied" if run["pending_action"] == "apply" else "rolled_back"
                        run.pop("pending_action", None)
                        self.store.save(run)
                        self.store.event(run["id"], "recovered", message="Completed file transaction recovered after restart.")
                        continue
                run["status"] = "interrupted"
                run["error"] = "Service restarted. Completed task checkpoints are retained; resume explicitly."
                if recovery:
                    run["error"] += " Incomplete file transactions were rolled back."
                if run.get("recovery_blocked"):
                    run["error"] = run["recovery_blocked"]
                self.store.save(run)
                self.store.event(run["id"], "interrupted", message=run["error"])
        if start:
            self.worker = threading.Thread(target=self._loop, name="coding-harness", daemon=True)
            self.worker.start()

    def status(self) -> Json:
        return {"version": VERSION, "host": socket.gethostname(), "execution": "isolated Linux container",
                "sandbox": self.sandbox.available(), "models": self.models_provider(),
                "workspaces": self.store.workspaces(), "projects_dir": str(self.projects),
                "limits": {"parallel_agents": 2, "inference_requests": 1, "run_minutes": 30,
                           "command_seconds": 180, "command_memory": "1 GiB", "network": "disabled"}}

    def _project_path(self, value: str) -> Path:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raise HarnessError("Choose an absolute Linux project folder.")
        path = raw.resolve(strict=True)
        if str(raw) != str(path):
            raise HarnessError("Use the canonical project path; symbolic-link workspace roots are refused.")
        if not path.is_dir() or not any(path.is_relative_to(r) and path != r for r in self.allowed_roots):
            raise HarnessError("Choose a project folder below an allowed workspace root.")
        if path == self.here or path.is_relative_to(self.data) or self.data.is_relative_to(path):
            raise HarnessError("The live harness and its run database cannot be used as a coding workspace.")
        if any(part in {".ssh", ".config", ".aws", ".gnupg"} for part in path.parts):
            raise HarnessError("Credential and configuration directories cannot be coding workspaces.")
        if "," in str(path):
            raise HarnessError("A project path cannot contain a comma (container mount constraint).")
        return path

    def create_workspace(self, body: Json) -> Json:
        name = str(body.get("name") or "").strip()[:100]
        if not name:
            raise HarnessError("Give the project a name.")
        path_text = str(body.get("path") or "").strip()
        if path_text:
            path = self._project_path(path_text)
        else:
            slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "project"
            path = self.projects / (slug[:50] + "-" + uuid.uuid4().hex[:6])
            path.mkdir(mode=0o755)
        files = scan_tree(path)
        with self.lock:
            for existing in self.store.workspaces():
                if existing["path"] == str(path):
                    return existing
            workspace = {"id": uuid.uuid4().hex, "name": name, "path": str(path),
                         "host": socket.gethostname(), "created": time.time(), "file_count": len(files)}
            self.store.save_workspace(workspace)
        return workspace

    def root(self, ident: str) -> Path:
        return self._project_path(self.store.workspace(ident)["path"])

    def run_root(self, ident: str) -> Path:
        self.store.run(ident)
        if not re.fullmatch(r"[a-f0-9]{32}", ident):
            raise HarnessError("Invalid run ID.")
        return self.data / "runs" / ident

    def files(self, workspace: str, run_id: str = "") -> Json:
        root = self.run_root(run_id) / "stage" if run_id else self.root(workspace)
        return {"files": [{"path": k, **v} for k, v in scan_tree(root).items()]}

    def read_file(self, workspace: str, rel: str, run_id: str = "") -> Json:
        root = self.run_root(run_id) / "stage" if run_id else self.root(workspace)
        path = safe_path(root, rel)
        if not path.is_file() or path.stat().st_size > MAX_FILE:
            raise HarnessError("File missing or larger than 2 MiB.")
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HarnessError("This file is binary and cannot be edited as text.") from exc
        return {"path": rel, "content": text, "sha": digest(raw)}

    def save_file(self, body: Json) -> Json:
        with self.lock:
            root = self.root(str(body.get("workspace") or ""))
            if any(r["status"] in ACTIVE for r in self.store.runs(str(body["workspace"]))):
                raise HarnessError("Wait for or cancel the active run before editing the project.")
            return self._edit(root, "write_file", body, ["*"])

    def create_run(self, body: Json) -> Json:
        workspace = str(body.get("workspace") or "")
        self.root(workspace)
        task = str(body.get("task") or "").strip()
        if not 1 <= len(task) <= 16000:
            raise HarnessError("Describe the task in 1–16,000 characters.")
        available = {str(m["port"]): m for m in self.models_provider() if m.get("available", True)}
        master = available.get(str(body.get("master")))
        ports = body.get("agents")
        if not master or not isinstance(ports, list) or not 1 <= len(ports) <= 3:
            raise HarnessError("Choose an available master and one to three coding agents.")
        agents: list[Json] = []
        for port in dict.fromkeys(str(p) for p in ports):
            model = available.get(port)
            if not model or not model.get("tools"):
                raise HarnessError("All agents must be generation models with tool-call support.")
            agents.append(model)
        if not self.sandbox.available()["ready"]:
            raise HarnessError("The local coding container is unavailable. No host-shell fallback is allowed.")
        checks = body.get("checks", [])
        if not isinstance(checks, list) or len(checks) > 6 or any(not isinstance(c, str) or len(c) > 4000 for c in checks):
            raise HarnessError("Checks must be a list of up to six shell commands.")
        with self.lock:
            all_runs = self.store.runs(limit=1000)
            if any(r["workspace"] == workspace and r["status"] in ACTIVE for r in all_runs):
                raise HarnessError("This project already has an active run.")
            if sum(r["status"] in ACTIVE for r in all_runs) >= 5:
                raise HarnessError("The run queue is full (five jobs).")
            ident = uuid.uuid4().hex
            run: Json = {"id": ident, "workspace": workspace, "task": task, "master": master, "agents": agents,
                         "checks_requested": checks, "status": "queued", "created": time.time(),
                         "parallel": max(1, min(2, int(body.get("parallel", 1)))),
                         "tasks": [], "checks": [], "summary": "", "error": "", "repairs": 0,
                         "usage": {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}}
            self.store.save(run)
            self.controls[ident] = Control()
            self.store.event(ident, "queued", message="Queued for the local master/agent harness.")
            self.pending.put(ident)
        return run

    def action(self, ident: str, action: str, body: Json) -> Json:
        with self.lock:
            run = self.store.run(ident)
            if action == "cancel":
                control = self.controls.get(ident)
                if control:
                    control.cancel()
                if run["status"] == "queued":
                    run["status"] = "cancelled"
                elif run["status"] in ACTIVE:
                    run["status"] = "cancelling"
                self.store.save(run)
                self.store.event(ident, "cancel", message="Cancellation requested; active container and model request will stop.")
            elif action == "resume":
                if run.get("recovery_blocked"):
                    raise HarnessError(run["recovery_blocked"])
                if run["status"] not in {"failed", "cancelled", "interrupted", "needs_validation", "needs_changes"}:
                    raise HarnessError("Only incomplete runs can be resumed.")
                if any(r["id"] != ident and r["status"] in ACTIVE for r in self.store.runs(run["workspace"])):
                    raise HarnessError("Another run is active for this project.")
                if "checks" in body:
                    checks = body["checks"]
                    if not isinstance(checks, list) or len(checks) > 6 or any(not isinstance(c, str) or len(c) > 4000 for c in checks):
                        raise HarnessError("Invalid validation commands.")
                    run["checks_requested"] = checks
                if run["status"] == "needs_changes":
                    self._append_repair(run, str(run.get("review", {}).get("summary") or run.get("error")))
                for task in run["tasks"]:
                    if task["status"] in {"running", "failed", "cancelled"}:
                        task["status"] = "pending"
                run.update(status="queued", error="")
                self.store.save(run)
                self.controls[ident] = Control()
                self.store.event(ident, "resume", message="Resume requested from durable task checkpoints.")
                self.pending.put(ident)
            elif action in {"apply", "rollback"}:
                expected = "ready" if action == "apply" else "applied"
                if run["status"] != expected and not (action == "apply" and run["status"] == "rolled_back"):
                    raise HarnessError(f"Run must be {expected} before {action}.")
                if any(r["id"] != ident and r["status"] in ACTIVE for r in self.store.runs(run["workspace"])):
                    raise HarnessError("Another run is active for this project.")
                home = self.run_root(ident)
                if action == "apply" and scan_tree(home / "stage") != run.get("approved_tree"):
                    raise HarnessError("Staged files changed after review; revalidate before applying.")
                before, after = (home / "baseline", home / "stage") if action == "apply" else (home / "stage", home / "baseline")
                target = self.root(run["workspace"])
                run["status"] = "applying"
                run["pending_action"] = action
                run["action_journal"] = action + "-" + uuid.uuid4().hex[:8]
                self.store.save(run)
                try:
                    files = apply_tree(before, after, target, home / run["action_journal"])
                except BaseException:
                    run["status"] = expected
                    run.pop("pending_action", None)
                    self.store.save(run)
                    raise
                run["status"] = "applied" if action == "apply" else "rolled_back"
                run.pop("pending_action", None)
                self.store.save(run)
                self.store.event(ident, action, files=files, message=f"{action}: {len(files)} project files.")
            else:
                raise HarnessError("Unknown run action.")
            return run

    def _transition(self, run: Json, state: str, message: str) -> None:
        with self.lock:
            run["status"] = state
            self.store.save(run)
            self.store.event(run["id"], state, message=message)

    def _loop(self) -> None:
        while True:
            ident = self.pending.get()
            try:
                run = self.store.run(ident)
                if run["status"] != "queued":
                    continue
                control = self.controls[ident]
                control.deadline = time.monotonic() + 1800
                self._execute(run, control)
            except Cancelled as exc:
                run = self.store.run(ident)
                run["error"] = str(exc)
                self._transition(run, "cancelled", str(exc))
            except Exception as exc:  # noqa: BLE001 -- a failed job must not terminate the durable queue.
                run = self.store.run(ident)
                run["error"] = str(exc)[:3000]
                self._transition(run, "failed", run["error"])
            finally:
                with self.lock:
                    self.controls.pop(ident, None)
                self.pending.task_done()

    def _infer(self, run: Json, model: Json, messages: list[Json], control: Control,
               tools: bool = False, budget: int = 4096) -> Json:
        control.check()
        while not self.model_lock.acquire(timeout=0.2):
            control.check()
        connection: http.client.HTTPConnection | None = None
        sock: socket.socket | None = None
        try:
            control.check()
            current = next((m for m in self.models_provider() if m["port"] == model["port"]), None)
            if current is None or current["name"] != model["name"] or current.get("pid") != model.get("pid"):
                raise HarnessError("Selected model was unloaded or replaced. Start a new run with the current model.")
            # Keep the recent tool evidence intact; bound earlier tool output instead of dropping tool IDs.
            if sum(len(json.dumps(m)) for m in messages) > 65000:
                for message in messages[:-6]:
                    if message.get("role") == "tool" and len(str(message.get("content"))) > 1000:
                        message["content"] = str(message["content"])[:1000] + "\n[Older result trimmed; read the file again if needed.]"
            if sum(len(json.dumps(m)) for m in messages) > 100000:
                raise HarnessError("Agent context budget reached; narrow the task or resume with a smaller scope.")
            body: Json = {"model": model["name"], "messages": messages, "stream": False,
                          "max_tokens": budget, "temperature": 0.2,
                          "chat_template_kwargs": {"enable_thinking": False}}
            if tools:
                body["tools"] = TOOLS
            self.store.event(run["id"], "model_start", model=model["name"], role="agent" if tools else "master")
            connection = http.client.HTTPConnection("127.0.0.1", int(model["port"]), timeout=180)
            connection.connect()
            sock = connection.sock
            if sock:
                with control.lock:
                    control.sockets.append(sock)
            connection.request("POST", "/v1/chat/completions", body=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(4 * 1024 * 1024 + 1)
            control.check()
            if len(raw) > 4 * 1024 * 1024:
                raise HarnessError("Model response exceeded its size budget.")
            data = json.loads(raw)
            if response.status >= 400 or data.get("error"):
                raise HarnessError(f"{model['name']} returned HTTP {response.status}: {str(data.get('error', data))[:800]}")
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message")
            if not isinstance(message, dict):
                raise HarnessError("Model returned no assistant message.")
            if choice.get("finish_reason") == "length":
                raise HarnessError("Model response was cut off at its token limit; no partial tool call was executed.")
            usage = data.get("usage") or {}
            with self.lock:
                run["usage"]["requests"] += 1
                for key in ("prompt_tokens", "completion_tokens"):
                    run["usage"][key] += int(usage.get(key) or 0)
                self.store.save(run)
            return message
        except (OSError, http.client.HTTPException) as exc:
            control.check()
            raise HarnessError("Local model request failed: " + str(exc)) from exc
        finally:
            if sock:
                with control.lock:
                    if sock in control.sockets:
                        control.sockets.remove(sock)
            if connection:
                connection.close()
            self.model_lock.release()

    def _json_call(self, run: Json, prompt: str, user: str, control: Control,
                   validator: Callable[[Json], Json]) -> Json:
        messages: list[Json] = [{"role": "system", "content": prompt}, {"role": "user", "content": user}]
        for attempt in range(3):
            message = self._infer(run, run["master"], messages, control)
            content = str(message.get("content") or "")
            try:
                return validator(parse_object(content))
            except (ValueError, TypeError, HarnessError, KeyError) as exc:
                if attempt == 2:
                    raise HarnessError("Master did not provide valid structured output: " + str(exc)) from exc
                messages.extend([{"role": "assistant", "content": content},
                                 {"role": "user", "content": "Correct this JSON error and return only the object: " + str(exc)}])
        raise HarnessError("Master structured response unavailable.")

    def _instructions(self, root: Path) -> str:
        parts = []
        for rel in scan_tree(root):
            if Path(rel).name == "AGENTS.md" or rel in {"README.md", "package.json", "pyproject.toml"}:
                text = safe_path(root, rel).read_text(errors="replace")[:9000]
                parts.append(f"--- {rel} ---\n{text}")
        return "\n".join(parts)[:22000]

    def _execute(self, run: Json, control: Control) -> None:
        ident = run["id"]
        home = self.run_root(ident)
        home.mkdir(parents=True, exist_ok=True)
        control.check()
        if not (home / "baseline").exists():
            copy_tree(self.root(run["workspace"]), home / "baseline")
        if not (home / "stage").exists():
            copy_tree(home / "baseline", home / "stage")
        stage = home / "stage"
        if not run["tasks"]:
            self._transition(run, "planning", "Master is inspecting project context and assigning file ownership.")
            context = json.dumps({"task": run["task"], "agents": run["agents"],
                                  "files": list(scan_tree(stage)), "mandatory_checks": run["checks_requested"],
                                  "project_instructions": self._instructions(stage)})
            plan = self._json_call(run, PLAN_PROMPT, context, control,
                                   lambda value: validate_plan(value, len(run["agents"])))
            run["plan"] = plan
            run["tasks"] = [{**task, "status": "pending", "attempts": 0} for task in plan["tasks"]]
            self.store.save(run)
            self.store.event(ident, "plan", plan=plan, message=plan["summary"])
        while True:
            self._run_tasks(run, control)
            control.check()
            self._transition(run, "validating", "Executing checks against an isolated copy of the integrated result.")
            commands = list(dict.fromkeys(run["checks_requested"] + run["plan"].get("checks", [])))
            if not commands:
                run["error"] = "No executable validation was supplied. Add a real test command, then resume."
                self._transition(run, "needs_validation", run["error"])
                return
            check_root = home / ("check-" + uuid.uuid4().hex[:8])
            copy_tree(stage, check_root)
            run["checks"] = []
            for command in commands:
                control.check()
                result = self.sandbox.run(check_root, command, control, 180)
                if result["exit_code"] == 0 and re.search(r"Ran 0 tests|collected 0 items|No tests found", result["output"], re.IGNORECASE):
                    result["exit_code"] = 65
                    result["output"] += "\n[harness] No tests were executed; validation is incomplete."
                run["checks"].append(result)
                self.store.save(run)
                self.store.event(ident, "check", **result)
            failed = [c for c in run["checks"] if c["exit_code"] != 0]
            if failed:
                if run["repairs"] < 2:
                    self._append_repair(run, "Fix the failing validation commands without weakening tests:\n" + json.dumps(failed)[:14000])
                    continue
                run["error"] = "Validation failed after two repair attempts. See check outputs."
                self._transition(run, "needs_changes", run["error"])
                return
            break
        self._transition(run, "reviewing", "Master is reviewing actual changes and validation evidence.")
        diff = tree_diff(home / "baseline", stage)
        if not diff["files"]:
            run["error"] = "The agents produced no source changes. No implementation can be approved."
            self._transition(run, "needs_changes", run["error"])
            return
        if diff["truncated"] or len(diff["text"]) > 60000:
            run["error"] = "Changes exceed the master review budget; split this into smaller runs."
            self._transition(run, "needs_changes", run["error"])
            return
        def verdict(value: Json) -> Json:
            if type(value.get("approved")) is not bool or not isinstance(value.get("summary"), str) or not value["summary"].strip():
                raise HarnessError("Review must contain approved (boolean) and a non-empty summary.")
            return value
        review = self._json_call(run,
            "Review a completed coding task against its requested behaviour. Inspect the supplied real diff, task results and check outputs. "
            "Do not accept missing implementation, empty tests, fabricated evidence, or unrelated edits. Do not claim GUI testing from syntax. "
            "Return ONLY JSON: {\"approved\": true or false, \"summary\": \"result, evidence and remaining limitations\", \"issues\": [\"specific issue\"]}.",
            json.dumps({"request": run["task"], "diff": diff["text"], "files": diff["files"],
                        "tasks": [{"title": t["title"], "result": t.get("result")} for t in run["tasks"]],
                        "checks": [{**c, "output": c["output"][:6000]} for c in run["checks"]]}), control, verdict)
        run["review"] = review
        run["summary"] = review["summary"]
        run["approved_tree"] = scan_tree(stage)
        run["changed_files"] = len(diff["files"])
        self.store.event(ident, "review", **review)
        if review["approved"]:
            self._transition(run, "ready", "Checks passed and master review completed. Review the diff and apply when ready.")
        else:
            run["error"] = review["summary"]
            self._transition(run, "needs_changes", review["summary"])

    def _append_repair(self, run: Json, reason: str) -> None:
        run["repairs"] += 1
        run["tasks"].append({"id": "repair-" + str(run["repairs"]), "title": "Repair validation or review failures",
                             "agent": 0, "instruction": reason[:16000], "files": ["*"],
                             "depends_on": [t["id"] for t in run["tasks"] if t["status"] == "completed"],
                             "status": "pending", "attempts": 0})
        self.store.save(run)

    def _run_tasks(self, run: Json, control: Control) -> None:
        self._transition(run, "running", "Agents are implementing the plan in separate project copies.")
        while any(t["status"] != "completed" for t in run["tasks"]):
            control.check()
            completed = {t["id"] for t in run["tasks"] if t["status"] == "completed"}
            ready = [t for t in run["tasks"] if t["status"] == "pending" and all(d in completed for d in t["depends_on"])]
            if not ready:
                raise HarnessError("The task graph is blocked by a failed or incomplete dependency.")
            wave: list[Json] = []
            for task in ready:
                overlap = any(a == "*" or b == "*" or a.rstrip("/") == b.rstrip("/") or
                              a.endswith("/") and b.startswith(a) or b.endswith("/") and a.startswith(b)
                              for other in wave for a in task["files"] for b in other["files"])
                if not overlap and len(wave) < run["parallel"]:
                    wave.append(task)
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(wave)) as pool:
                futures = {pool.submit(self._worker_task, run, task, control): task for task in wave}
                errors = []
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as exc:  # noqa: BLE001 -- collect every worker failure before leaving the wave.
                        task = futures[future]
                        if not isinstance(exc, Cancelled) and task["attempts"] < 2 and len(run["agents"]) > 1:
                            with self.lock:
                                task["agent"] = (task["agent"] + 1) % len(run["agents"])
                                task["status"] = "pending"
                                self.store.save(run)
                            self.store.event(run["id"], "reassigned", task=task["id"], agent=task["agent"],
                                             message="Agent failed; retrying its isolated task once on another selected agent: " + str(exc)[:800])
                        else:
                            errors.append(exc)
                if errors:
                    raise errors[0]

    def _worker_task(self, run: Json, task: Json, control: Control) -> None:
        ident = run["id"]
        home = self.run_root(ident)
        with self.lock:
            task["attempts"] += 1
            task["status"] = "running"
            task.pop("error", None)
            task["started"] = time.time()
            base = home / f"task-{task['id']}-{task['attempts']}-base"
            work = home / f"task-{task['id']}-{task['attempts']}-work"
            copy_tree(home / "stage", base)
            copy_tree(base, work)
            self.store.save(run)
        self.store.event(ident, "task_start", task=task["id"], agent=task["agent"], message=task["title"])
        instructions = self._instructions(work)
        dependencies = [{"id": t["id"], "result": t.get("result", "")} for t in run["tasks"] if t["id"] in task["depends_on"]]
        messages: list[Json] = [{"role": "system", "content": WORKER_PROMPT + "\nProject instructions:\n" + instructions},
                                {"role": "user", "content": json.dumps({"overall_task": run["task"], "assignment": task,
                                  "dependency_results": dependencies, "files": list(scan_tree(work))})}]
        try:
            summary = ""
            repeats: dict[str, int] = {}
            consecutive_errors = 0
            redirects = 0
            for step in range(28):
                control.check()
                response = self._infer(run, run["agents"][task["agent"]], messages, control, True, 6144)
                calls = response.get("tool_calls") or []
                if not calls:
                    summary = re.sub(r"<think>.*?</think>", "", str(response.get("content") or ""), flags=re.DOTALL).strip()
                    if not summary:
                        raise HarnessError("Agent returned an empty completion.")
                    break
                if not isinstance(calls, list) or len(calls) > 10:
                    raise HarnessError("Invalid or excessive tool-call batch.")
                message: Json = {"role": "assistant", "content": response.get("content") or "", "tool_calls": calls}
                # Assign valid IDs before appending the assistant turn; keep all tool/result pairs balanced.
                for call in calls:
                    call["id"] = uuid.uuid4().hex[:9]
                messages.append(message)
                for call in calls:
                    control.check()
                    fn = call.get("function") or {}
                    name = str(fn.get("name") or "")
                    start = time.monotonic()
                    try:
                        args = fn.get("arguments") or "{}"
                        args = json.loads(args) if isinstance(args, str) else args
                        if not isinstance(args, dict):
                            raise HarnessError("Tool arguments must be a JSON object.")
                        result = self._tool(work, name, args, task["files"], control)
                        is_error = bool(result.get("error")) or result.get("exit_code", 0) != 0
                    except (ValueError, TypeError, OSError, HarnessError) as exc:
                        if isinstance(exc, Cancelled):
                            raise
                        result = {"error": str(exc)}
                        is_error = True
                    event: Json = {"task": task["id"], "tool": name, "error": is_error,
                                   "seconds": round(time.monotonic() - start, 2), "result": result}
                    if isinstance(args, dict):
                        event["args"] = {k: (str(v)[:240] if k in {"content", "new", "old"} else v) for k, v in args.items()}
                    self.store.event(ident, "tool", **event)
                    messages.append({"role": "tool", "tool_call_id": call["id"], "name": name,
                                     "content": json.dumps(result)[:26000]})
                    signature = digest(json.dumps({"name": name, "args": args}, sort_keys=True).encode())
                    repeats[signature] = repeats.get(signature, 0) + 1
                    consecutive_errors = consecutive_errors + 1 if is_error else 0
                if consecutive_errors >= 3 or any(count >= 3 for count in repeats.values()):
                    if redirects >= 2:
                        raise HarnessError("Agent repeated failing or unproductive tool calls after two master redirects.")
                    def redirect_object(value: Json) -> Json:
                        if not isinstance(value.get("instruction"), str) or not value["instruction"].strip():
                            raise HarnessError("Master redirect requires an instruction.")
                        return value
                    direction = self._json_call(run,
                        "A coding worker is stuck. Review its actual assignment, file ownership and recent tool results. "
                        "Give one precise correction. If its own assigned work is already done, tell it to finish with a concise summary and leave other tasks to their owners. "
                        "Do not expand its file scope. Return only JSON: {\"instruction\":\"precise next step\"}.",
                        json.dumps({"assignment": task, "files_now": scan_tree(work), "recent_steps": messages[-8:]}),
                        control, redirect_object)
                    messages.append({"role": "user", "content": "MASTER REDIRECT: " + direction["instruction"]})
                    redirects += 1
                    repeats.clear()
                    consecutive_errors = 0
                    self.store.event(ident, "redirect", task=task["id"], message=direction["instruction"])
            else:
                raise HarnessError("Agent reached the 28-step budget; work is retained for inspection.")
            control.check()
            changes = changed_files(scan_tree(base), scan_tree(work))
            unexpected = [p for p in changes if not owned(p, task["files"])]
            if unexpected:
                raise HarnessError("Agent changed files outside its ownership: " + ", ".join(unexpected))
            with self.lock:
                task.update(result=summary[:10000], files_changed=changes,
                            merge_journal="merge-" + uuid.uuid4().hex[:12])
                self.store.save(run)
                apply_tree(base, work, home / "stage", home / task["merge_journal"])
                task.update(status="completed", result=summary[:10000], files_changed=changes, finished=time.time())
                self.store.save(run)
            self.store.event(ident, "task_done", task=task["id"], message=summary[:10000], files=changes)
        except BaseException as exc:
            with self.lock:
                task.update(status="cancelled" if isinstance(exc, Cancelled) else "failed", error=str(exc), finished=time.time())
                self.store.save(run)
            self.store.event(ident, "task_failed", task=task["id"], message=str(exc))
            raise

    def _edit(self, root: Path, name: str, args: Json, claims: list[str]) -> Json:
        rel = relative_path(args.get("path"))
        if not owned(rel, claims):
            raise HarnessError("This file belongs to another task: " + rel)
        path = safe_path(root, rel)
        data = path.read_bytes() if path.exists() else b""
        actual = digest(data) if path.exists() else ""
        if args.get("expected_sha") != actual:
            raise HarnessError("File changed or was not read first; read its current SHA256 and retry.")
        if name == "delete_file":
            if not path.is_file():
                raise HarnessError("File does not exist.")
            path.unlink()
            return {"deleted": rel}
        if name == "replace_text":
            old, new = args.get("old"), args.get("new")
            if not isinstance(old, str) or not old or not isinstance(new, str):
                raise HarnessError("Replacement requires non-empty old text and string new text.")
            text = data.decode("utf-8")
            if text.count(old) != 1:
                raise HarnessError("Old text must occur exactly once; file was not changed.")
            content = text.replace(old, new, 1)
        else:
            supplied_content = args.get("content")
            if not isinstance(supplied_content, str):
                raise HarnessError("File content must be a string.")
            content = supplied_content
        output = content.encode("utf-8")
        if len(output) > MAX_FILE:
            raise HarnessError("File content exceeds 2 MiB.")
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        atomic_write(path, output, mode)
        return {"path": rel, "sha": digest(output), "bytes": len(output)}

    def _tool(self, root: Path, name: str, args: Json, claims: list[str], control: Control) -> Json:
        control.check()
        spec = next((d["function"]["parameters"] for d in TOOLS if d["function"]["name"] == name), None)
        if spec is None:
            raise HarnessError("Unknown tool; no operation was executed.")
        if any(k not in args for k in spec["required"]) or any(k not in spec["properties"] for k in args):
            raise HarnessError("Tool arguments do not match the declared schema.")
        if name == "list_files":
            return {"files": [{"path": p, **meta} for p, meta in scan_tree(root).items()]}
        if name == "read_file":
            path = safe_path(root, args.get("path"))
            if not path.is_file() or path.stat().st_size > MAX_FILE:
                raise HarnessError("File missing or too large.")
            raw = path.read_bytes()
            first, limit = args.get("start_line", 1), args.get("limit", 200)
            if type(first) is not int or type(limit) is not int or first < 1 or not 1 <= limit <= 400:
                raise HarnessError("Read range requires start_line >= 1 and limit between 1 and 400.")
            lines = raw.decode("utf-8").splitlines(keepends=True)
            content = "".join(lines[first - 1:first - 1 + limit])
            next_line = first + limit if first - 1 + limit < len(lines) else None
            return {"path": args["path"], "sha": digest(raw), "content": content[:22000],
                    "start_line": first, "total_lines": len(lines), "next_line": next_line,
                    "truncated": len(content) > 22000 or next_line is not None}
        if name == "search_files":
            needle = args.get("query")
            if not isinstance(needle, str) or not 1 <= len(needle) <= 500:
                raise HarnessError("Search requires 1–500 characters.")
            matches = []
            for rel in scan_tree(root):
                text = safe_path(root, rel).read_text(errors="replace")
                for number, line in enumerate(text.splitlines(), 1):
                    if needle.lower() in line.lower():
                        matches.append({"path": rel, "line": number, "text": line[:500]})
                        if len(matches) == 80:
                            return {"matches": matches, "truncated": True}
            return {"matches": matches, "truncated": False}
        if name in {"write_file", "replace_text", "delete_file"}:
            return self._edit(root, name, args, claims)
        if name == "run_command":
            command = args.get("command")
            if not isinstance(command, str):
                raise HarnessError("Command must be a string.")
            timeout = args.get("timeout", 90)
            if type(timeout) is not int:
                raise HarnessError("Command timeout must be an integer.")
            return self.sandbox.run(root, command, control, timeout)
        raise HarnessError("Unsupported tool.")

    def diff(self, ident: str) -> Json:
        home = self.run_root(ident)
        if not (home / "stage").exists():
            return {"files": [], "text": "Snapshot has not started yet.", "truncated": False}
        return tree_diff(home / "baseline", home / "stage")

    def terminal(self, body: Json) -> Json:
        workspace = str(body.get("workspace") or "")
        with self.lock:
            if any(r["status"] in ACTIVE for r in self.store.runs(workspace)):
                raise HarnessError("The project has an active run. Use its recorded tool output, or wait.")
            root = self.root(workspace)
            # Manual commands also use a disposable copy. They never modify the user's source behind the diff review.
            dest = self.data / "terminal" / uuid.uuid4().hex
            dest.parent.mkdir(parents=True, exist_ok=True)
            copy_tree(root, dest)
        try:
            return self.sandbox.run(dest, str(body.get("command") or ""), Control(190), 180)
        finally:
            shutil.rmtree(dest, ignore_errors=True)

    def handle(self, method: str, path: str, query: Json, body: Json) -> Json:
        def q(name: str) -> str:
            return str((query.get(name) or [""])[0])
        if method == "GET":
            if path == "/api/harness/status":
                return self.status()
            if path == "/api/harness/runs":
                return {"runs": self.store.runs(q("workspace"))}
            if path == "/api/harness/run":
                return self.store.run(q("id"))
            if path == "/api/harness/events":
                self.store.run(q("id"))
                return {"events": self.store.events(q("id"), max(0, int(q("since") or 0)))}
            if path == "/api/harness/files":
                return self.files(q("workspace"), q("run"))
            if path == "/api/harness/file":
                return self.read_file(q("workspace"), q("path"), q("run"))
            if path == "/api/harness/diff":
                return self.diff(q("id"))
        if method == "POST":
            if path == "/api/harness/workspace":
                return self.create_workspace(body)
            if path == "/api/harness/run":
                return self.create_run(body)
            if path == "/api/harness/action":
                return self.action(str(body.get("id") or ""), str(body.get("action") or ""), body)
            if path == "/api/harness/file":
                return self.save_file(body)
            if path == "/api/harness/terminal":
                return self.terminal(body)
        raise HarnessError("Unknown coding-harness endpoint.")
