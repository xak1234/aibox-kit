# terminal..

A pixel-art operator at a real Linux terminal, with an expandable console, a task
builder, a conversation panel, its own Chromium browser, modular Markdown skills,
persistent memory and optional scored repeated learning.

**Full integration contract:** [docs/INTEGRATION.md](docs/INTEGRATION.md).

## Start

In aibox-kit this app is installed and started by `install.sh --terminal` (service `aibox-terminal`,
compose file `/opt/aibox/terminal/compose.yaml`). It defaults to the box's own model API
(`http://172.31.250.1:9010/v1`, OpenAI-compatible) - pick a model in **Runtime settings**.
Pairing token: `sudo aibox terminal-token`.

Stand-alone, with Docker and Compose installed: `bash start.sh` from this folder, then open
http://localhost:8765 and paste the printed token into **Runtime settings**.

The hosted interface is also usable as a client, but a browser can require local
network permission for the loopback connection. Opening the local interface at
http://localhost:8765 avoids that cross-origin dependency. For a remote runtime,
use HTTPS/WSS and set its exact UI origin in `TERMINAL_ORIGINS`.

## What is real

- The terminal is a Linux Bash PTY. The small screen and expanded console use the
  **same xterm.js instance and the same session**, with a fixed 100×28 cell grid.
- Every agent command goes through that terminal, including files, scripts,
  package commands and app launches. `cd` and exported variables persist.
- The display receives the PTY's original bytes. Command output is never generated
  for the interface. A separate pipe carries exit status and working-directory
  metadata. The displayed `eval -- '…'` wrapper is the actual Bash command.
- Interactive tools work in **Take control** mode. Exit the tool and return to a
  prompt before returning control. Taking control pauses the agent; a stale
  model action is discarded when control returns.
- Shell and process state last for one runtime session. Workspace files and memory
  persist across restarts. Interrupted tasks are recorded and do not silently resume.
- Docker provides a real Linux environment, sharing its host's Linux kernel. This
  is not a full machine emulator or a separately booted guest kernel.
- CLI, text-interface and web apps are supported. A graphical Linux desktop/VNC
  session is not included.

The hosted page does not itself run Linux. It must be paired with the supplied
runtime. Offline counters and messages do not pretend an agent is working.

## Harness² integration

The vendored, unmodified Harness² 0.1.0 wheel is built from Google's repository at
commit `1cfe254e8d25b15f37048c3730134686abe60335`.
Its Apache-2.0 license is included in the wheel. See `vendor/PROVENANCE.txt`.

This live adapter uses the actual `SurfaceSpec`, component-tree validation,
`read_tree`, `write_tree`, and persistent `Library` APIs. Before a task, relevant
memories and selected skills become a validated harness. After a successful task,
the adopted component tree and an evidence-linked experience entry are retained.
Later tasks can retrieve those procedures and their previous component context.

Enable **Repeated learning** per task to run the actual upstream `run_sequential`
Propose–Probe–Compose workflow through the live terminal adapter. One round runs
four actual task executions; two or three rounds run eight or twelve. Each trial
gets a fresh working folder and browser profile. Add explicit verification commands:
the score is the weighted percentage of passing checks, and the change compares
the final composed candidate against the highest baseline result. The candidate
is retained only if it improves the score; ties and regressions keep the baseline.
With learning off, tasks execute once and recording/memory remain enabled.

Trial folders are not separate containers and external services remain shared.
Use repeatable tasks. A score describes the supplied checks, not general capability
or model-weight training. See the integration guide for the precise adoption rule,
partial outcomes, fresh-input behavior and evidence limitations. Model access uses
a custom HTTP proposer transport, not the stock OpenCode executable.

Source: https://github.com/google-research/harness2

## Its own browser

Open **Browser → Start browser**, or run `terminal-browser start` in the terminal.
The agent uses `open`, `snapshot`, `click`, `fill`, `press`, `tabs` and other commands
to interact. The browser panel shows actual 1280×800 PNG pixels, refreshed about
every two seconds, and expands for a closer view. Manual clicks and keyboard
controls use recorded terminal commands. The profile and downloads persist in
`/workspace/.operator-home/.terminal-browser/`. It does not use your desktop cookies.

The Browser skill documents the command interface. Browser profiles are workspace
data, separate from the memory export. Latest screenshots are displayed; historical
video is not recorded. Use manual secret entry instead of recording passwords in
command arguments, as described in the integration guide.

## Persistent learning and recording

The `terminal-memory` Docker volume holds:

| Path | Purpose |
| --- | --- |
| `/data/memory.sqlite3` | Tasks, conversations, command records, preferences, procedures, events and non-secret model settings |
| `/data/sessions/*.jsonl` | Ordered raw terminal byte streams and dimensions for faithful replay |
| `/data/harness2/tasks/` | Per-task component trees |
| `/data/harness2/library/workspace/` | Harness² adopted trees and `EXPERIENCE.md` entries |
| `/data/skills/*.md` | Editable skill instructions |
| `/data/token` | Browser pairing token, separate from exports |
| `/data/model-key` | Optional model key, separate from exports |

The `terminal-workspace` volume holds files and the shell home. Do not use
`docker compose down -v` if you want to keep these volumes. Ordinary `down` and
`up` preserve them.

SQLite uses WAL mode and FULL synchronization. Terminal data is journalled as it
arrives and explicitly synced on recorded command completion. Command summaries
retain up to 250,000 characters; the raw session recording remains untruncated.
Long-running noisy processes therefore increase disk use. The controller replays
the full current-session journal on browser reconnect, including resize events.

Learned procedures require at least one real command with exit code zero, and the
last command in the task must succeed. Supplied verification checks must also pass
for the task to be marked complete. Improved partial procedures are explicitly
labelled as still having unmet verification requirements. The task summary and lesson are still model
interpretations, not an independent proof that every goal is met. Open the evidence
links to inspect actual output. Failed commands remain in recordings, but are not
accepted as successful procedure evidence.

**Memory → Export** downloads a SQLite backup, recordings, skills and Harness²
entries. Tokens and API keys are excluded. Workspace files are a separate volume
and are not part of the memory export.

To restore an export, stop the controller first, then mount the archive for the
restore command (replace the host directory with the directory holding the export):

```bash
docker compose stop terminal
docker compose run --rm --no-deps -v /absolute/path/to/backups:/backups:ro terminal python scripts/restore.py /backups/terminal-memory.zip /data
docker compose up -d
```

The restore utility validates archive paths and SQLite integrity, and preserves
previous memory in a timestamped `/data/before-restore-*` directory. It leaves the
existing pairing token and model key in place. Run it only with the controller
stopped. A restored export can contain recorded command output; treat it as private.

## Models and execution

Ollama defaults to `http://host.docker.internal:11434` from inside Docker. Your
Ollama listener must be reachable from the container; a loopback-only Linux host
listener may need its listening address adjusted for Docker. Use an installed
model that reliably follows JSON action instructions. The UI discovers models
through `/api/tags`; it does not download models automatically.

For compatible APIs, provide the base URL including `/v1` when appropriate, a
model name and any required key. The client uses `/models` and `/chat/completions`.
It does not assume a provider's tools API. A strict JSON action contract tells the
agent to run, finish or report a blocker. Ollama additionally uses JSON format mode.

Tasks have a 24-action limit and commands a maximum 180-second foreground timeout.
Run longer services in the background with log files. Pause takes effect between
commands; Stop interrupts the foreground command and retains the recording.
Commands that deliberately ignore interruption may require manual control or a
runtime restart. The user can select **Run queue** to process queued tasks in order.

The model operates as an unprivileged Linux user inside the container. The root
controller holds private state, accepts authenticated requests, and gives the
shell a minimal environment. No Docker socket or host home directory is mounted.
The process-mode developer server is not itself a sandbox; use Docker for normal
operation. Authentication protects the controller and terminal WebSocket.

## Skills and modules

Use **Skills → New skill** or import a `.md` file. A skill has a stable ID, name,
description and instructions. Select it for a task, or disable it. Installing a
skill does not execute code. Instructions describe commands the agent should use
and the checks it should make; every resulting action still goes through Bash.

| Module | Responsibility |
| --- | --- |
| `terminal_app/terminal.py` | Real PTY, input/output, command status, journalling and terminal replay |
| `terminal_app/agent.py` | Model connections, bounded task loop, pause/stop and control handover |
| `terminal_app/learning.py` | Real Harness² sequential recursion, fresh trials and weighted verification |
| `terminal_app/browser.py` | Persistent Chromium service and terminal command client |
| `terminal_app/harness_memory.py` | Actual Harness² component-tree and persistent experience-library integration |
| `terminal_app/store.py` | Durable SQLite history, retrieval, evidence validation and backups |
| `terminal_app/skills.py` | Built-in and imported Markdown skill registry |
| `terminal_app/app.py` | FastAPI HTTP routes, authenticated WebSocket, export and static UI |
| `frontend/app.ts` | Task builder, conversation, skills, memory, settings and shared xterm view |
| `frontend/desk.ts` | Pixel-art desk; movement responds to actual agent state |

## Development and minimal test

Use Python 3.12 or 3.13 and Node.js 22. The runtime and UI dependencies are included
in the manifests; the Harness² wheel is included to preserve the inspected revision.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
npm ci
npm run typecheck
npm run lint
npm run build
.venv/bin/python -m pytest -q
.venv/bin/ruff check terminal_app tests scripts/restore.py
.venv/bin/mypy terminal_app
```

Expected test summary: `12 passed` (elapsed time and third-party deprecation warnings
vary). Tests execute actual Bash processes, use the actual Harness² library, and
exercise authentication, the WebSocket replay and export. There are no model stubs.

A minimal manual test that needs no model:

1. Start Docker and pair the browser.
2. Open **Recordings → App launchers → Inspect workspace**.
3. Expand the terminal. It shows the actual `pwd; ls -lah` execution.
4. The command recording must show **EXIT 0** and the current workspace listing.

For Harness²'s upstream diagnostic:

```bash
docker compose exec terminal harness2-demo --output /workspace/harness2-check --k 1
```

Expected final line: `Local demo: 2 tasks completed; 0 failed.` followed by the
report path. This checks the upstream recursive workflow independently of a model.

## Validation of this build

- Twelve real-runtime tests passed; Python lint/type checks and TypeScript lint/type
  checks passed; the frontend production bundle built successfully.
- Harness²'s upstream local diagnostic completed two tasks with zero failures.
- Browser checks covered layout, offline task drafting, persisted learning controls,
  verification forms, browser panel and terminal expansion. The hosted interface is a client; it needs a paired local runtime.
- Docker Engine was not available in the build environment, so the Docker image
  and Windows launcher have not been executed here. The bundled Chromium runtime
  acceptance script is included for target-machine/CI execution; its result is not
  claimed by the unit tests. No model credentials were
  supplied, so a live model-driven task has not been validated. Those remain the
  initial setup checks on the target machine.

This project is independent of Google. Harness² itself is a Google Research
project and not an officially supported Google product.
