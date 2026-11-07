# Modelbench Coding Harness 3.0

The coding workspace in modelbench runs a durable master-and-agent workflow with isolated project copies, real validation commands, reviewable diffs, conflict detection, and rollback.

Open **http://&lt;box&gt;:9050/play** (on by default; `install.sh --no-coding-harness` leaves it out). Classic chat stays at **/play/chat**.

## Start a coding task

1. Click **+ Project**. Give it a name. Leave the folder blank to create a project under `/var/lib/aibox/projects` (AIBOX_PROJECTS), or choose an existing canonical Linux project folder.
2. Select a master and one to three tool-capable agents. The end-to-end verification used Gemma 4 12B as master, with Gemma E2B and Gemma 12B as agents. A model can be both master and agent.
3. Describe the change and expected behaviour. Add repository-specific required checks, one Bash command per line. For example, `python3 -m unittest discover -s tests -v`. Use the project's actual test command; an empty test discovery is rejected.
4. Start coding. The master creates a dependency plan, assigns file ownership, and defines checks. Agents edit private copies; their completed work is merged into the run's staged result.
5. Inspect **Activity**, **Checks**, **Code → View staged result**, and **Changes**. Passing checks and an affirmative master review make the run ready for your review.
6. Use **Review & apply changes** to copy the reviewed result into the project. Conflicting newer source edits stop the apply. **Roll back applied changes** restores the touched files only when their current contents still match what the run applied.

## Workspace layout

The left panel contains projects, files, and durable run history. The centre contains the task board, text editor, and diff. Activity, validation output, and an isolated terminal sit below it. The right panel configures the **next run**; the active run displays its own master and agents above the task board.

Task instructions and agent results expand on demand. Desktop and mobile layouts, dark/light themes, keyboard saving (`Ctrl+S`), and task submission (`Ctrl+Enter`) are supported. Unsaved editor changes trigger a discard confirmation. Run export downloads the run, diff, and all persisted events.

The editor saves directly to the selected source project using a content hash to detect conflicting edits. It is disabled for staged results and refused while that project has an active run. The terminal uses a disposable copy: commands can inspect and test the project but do not persist source edits. Use the editor or a coding run to change source.

## Team execution and recovery

- One active run executes at a time, with a queue of up to five jobs.
- A plan has one to eight tasks with explicit dependencies, agent assignments, and file ownership. Invalid plans fail visibly.
- Up to two independent tasks may execute concurrently in separate copies. Overlapping file scopes are scheduled sequentially. Model requests are serialized on the shared APU.
- Agents receive file listing, paginated reading, search, content-hash-checked writes, unique text replacement, deletion, and sandboxed commands. Repository instruction files are supplied as context.
- Repeated identical tool calls or repeated errors trigger a master redirection. A failed task can retry once with another selected agent. Failed checks can trigger up to two repair rounds.
- Runs have a 30-minute execution budget and workers have a 28-step attempt budget. Completed task checkpoints survive cancellation or service restart. **Resume from checkpoint** continues incomplete work and reruns validation.
- SQLite stores runs and ordered events. File transactions retain preimages and receipts. Startup recovery rolls back incomplete transactions or records completed ones; conflicting subsequent edits block automatic recovery.
- Failed validation, missing checks, empty test discovery, an unapproved review, and changed staged contents all prevent application.

## Execution boundary and limits

Commands run on Linux in the already-cached Docker image identified by:

```
aibox-sandbox:latest
```

install.sh builds it from `/opt/aibox/sandbox/Dockerfile` (Python 3.12, Node, npm, Git, Bash, GNU timeout). Add the toolchains your projects need there and run `sudo aibox sandbox-rebuild`. Runtime Python code uses the standard library. Images are never pulled automatically. Projects with other dependencies need a prepared local image configured before their checks can run.

Each command has no network, a read-only container root, no Linux capabilities, no privilege escalation, a non-root UID, 1 GiB memory, two CPUs, 128 processes, and a 180-second maximum. Only its private project copy is mounted at `/workspace`; neither the host home nor Docker socket is mounted. A timeout inside the container also bounds execution if the harness process disappears. Container command output and file sizes are bounded. The existing Docker daemon and host kernel remain part of the trusted execution base.

Source snapshots allow at most 5,000 files, 64 MiB total, and 2 MiB per file. They exclude Git metadata, dependencies, build outputs, common credential directories, environment files, and private-key files. Symbolic links and hard links are refused. Review is capped at a 60,000-character diff; larger changes require a smaller task. At least 1 GiB of free disk is required before a command starts.

The new harness validates Linux projects. Linux checks do not establish that a Windows GUI behaves correctly. Models can still produce bad plans or code; review and meaningful acceptance checks remain necessary.

## Files

| File | Responsibility |
| --- | --- |
| `bench.py` | The bench, classic chat and the routing adapter |
| `harness_integration.py` | Model capability filtering, HTTP routes, same-origin write checks |
| `coding_harness.py` | Master planning, agent scheduling, tools, checks, review, run actions |
| `harness_core.py` | Snapshots, path checks, transactional application, SQLite, Docker execution |
| `coding_harness.html`, `.css`, `.ts`, `.js` | Responsive coding interface; compiled JavaScript is included |
| `test_coding_harness.py` | Filesystem, workflow, persistence and real-container tests |

Optional configuration: `/var/lib/aibox/modelbench/coding_harness.json`

```json
{
  "data_dir": "/var/lib/aibox/modelbench/harness",
  "projects_dir": "/var/lib/aibox/projects",
  "allowed_roots": ["/var/lib/aibox/projects"],
  "image": "aibox-sandbox:latest"
}
```

Only folders under `allowed_roots` can be opened as projects, and the aibox account must be able to read them.
Access uses the normal aibox token. This is a single-owner LAN tool, not a multi-tenant hosted service.

**Security note.** Containers start through the root helper (`aibox-priv sandbox-run`) with fixed flags, the
configured image, aibox's own uid and a mount limited to the harness/project folders; the aibox account is not
in the docker group. Anyone holding the aibox token can drive the harness, so keep the token private.

## Development checks

```bash
cd modelbench
AIBOX_CONF=/nonexistent AIBOX_DATA=$(mktemp -d) PYTHONPATH=../lib python3 -m unittest -v test_coding_harness
npm install && npm run build      # rebuilds coding_harness.js from coding_harness.ts
```

Real-container tests skip when the sandbox image is missing; set `HARNESS_TEST_IMAGE` to test another image.

## Operation

Run snapshots, events and rollback receipts live under the data dir; the SQLite database is `runs.sqlite3`.
Use SQLite's backup API for a running database rather than copying the file while WAL writes are active.
`aibox-modelbench.service` uses `KillMode=process`, so restarting modelbench leaves loaded models running
and the bench adopts them again; unload from the page when you mean to.
