# Integrating with aibox

## Authentication

Every endpoint on modelbench (`:9050`), boxagent (`:9060`) and hf-fetch (`:9070`) needs the access token
from `/etc/aibox/token`. Addresses listed in `AIBOX_TRUSTED` are the only exception. A request can carry the
token in any of these ways:

- the header `Authorization: Bearer <token>`. Use this from scripts and SDKs.
- the header `X-Key: <token>`
- the cookie `aibox_key`, which the sign-in page sets. One cookie works for all three apps on the same host.
- `?key=<token>`. This is accepted, but the token lands in logs and browser history, so avoid it.

A POST from a browser must come from the app's own page (`Origin` / `Sec-Fetch-Site` are checked).
Clients that send neither header, such as curl, scripts and SDKs, are fine. boxagent's POSTs also need the
header `X-Box: 1`.

## Using the models from your own apps (the main integration point)

Every model on the box is available through one OpenAI-compatible API. llama-swap loads the model you name,
on demand, and swaps out the previous one if memory is short.

| From | Base URL | Key |
|---|---|---|
| another machine | `http://<box>:9050/swap/v1` | the aibox token |
| the box itself | `http://127.0.0.1:9010/v1` | none |
| the terminal.. container | `http://172.31.250.1:9010/v1` | none |

```bash
TOKEN=...   # sudo aibox token
curl -H "Authorization: Bearer $TOKEN" http://box:9050/swap/v1/models
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"model":"qwen3-4b-instruct-q4_k_m","messages":[{"role":"user","content":"hi"}],"stream":true}' \
     http://box:9050/swap/v1/chat/completions
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://box:9050/swap/v1", api_key=TOKEN)
print(client.chat.completions.create(model="qwen3-4b-instruct-q4_k_m",
      messages=[{"role": "user", "content": "hi"}]).choices[0].message.content)
```

Model ids are the llama-swap ids (`GET /swap/v1/models`). They are the GGUF file name in lower case,
unless you gave an entry a name or aliases yourself. Everything llama-swap serves passes through
`/swap/...` unchanged: embeddings and reranking if you configure such models, `/swap/running`,
and `/swap/upstream/<id>/...`. (llama-swap's own web UI does not work under the `/swap` prefix; open it on the box at `http://127.0.0.1:9010/ui/`.)

The first request to a model that isn't loaded waits while it loads, which takes seconds to a minute for big
models. Set client timeouts to suit.

modelbench also has `http://<box>:9050/v1/...`. That endpoint covers only the models loaded in the bench
itself, and when several are loaded it races them and returns the first answer. Most integrations want
`/swap/v1`.

## modelbench (`:9050`)

| Method | Path | Body / query | Does |
|---|---|---|---|
| GET | `/api/state` | `?ctx=` | budget, memory, running servers, loaded models, model list with last test results |
| GET | `/api/result` | `?id=` | full result of one test (answers, timings, log tail) |
| POST | `/api/load` | `{"id": "<model id from /api/state>", "ctx": null, "tools": true}` | load a model for chat on a bench port |
| POST | `/api/run` | `{"id": "...", "ctx": null}` | queue a safe test run (load, checks, speed, unload, memory check) |
| POST | `/api/runfit` | `{}` | test every model that fits |
| POST | `/api/unload` | `{"port": 9031}` | unload a bench-loaded model (no port = all) |
| POST | `/api/stop` | `{}` | stop the running test and empty the queue |
| POST | `/api/killpid` / `/api/killall` | `{"pid": n}` / `{}` | stop llama-servers |
| POST | `/api/chat` | `?port=` + an OpenAI chat body | chat with a bench-loaded model. Streams SSE and runs web tools itself when the model asks |
| POST | `/api/team` | `{"task": "...", "coordinator": port or remote id, "workers": [ports]}` | team mode (SSE events) |
| POST | `/api/overlord` | same shape | overlord mode (SSE events) |
| GET | `/api/info` | | services, endpoints, inbox path |
| GET | `/api/harness/status` | | coding workspace status, models, sandbox readiness |
| POST | `/api/harness/workspace` | `{"name": "..."}` or `{"name": "...", "path": "<under AIBOX_PROJECTS>"}` | create or open a coding project |
| POST | `/api/harness/run` | see `modelbench/coding_harness.py` (`handle`) | start a coding run |
| POST | `/api/harness/terminal` | `{"workspace": id, "command": "..."}` | run one command in the project's sandbox (on a copy; nothing persists) |

## boxagent (`:9060`)

| Method | Path | Does |
|---|---|---|
| GET | `/api/stats` | CPU, RAM, GPU memory/temp/power, disk, load, loaded models |
| GET | `/api/history?h=24` | sampled history (30 s, kept 7 days) |
| GET | `/api/procs`, `/api/services`, `/api/cron` | processes, systemd units, timers and cron |
| GET | `/api/models` | llama-swap models with size, fit and status |
| GET | `/api/warnings`, `/api/integrity`, `/api/events` | active warnings and requirements, last scan, event log |
| GET | `/api/power` | CPU governor/EPP/max freq, GPU level and their options |
| GET | `/api/journal?unit=aibox-modelbench.service` | last 80 log lines |
| GET | `/api/drives`, `/api/drives/ls?path=` | mounts under /mnt and /media, and a read-only listing of one |
| POST | `/api/act` | one action, chosen by `type` (list below) |
| POST | `/api/chat` | `{"messages": [...]}`, which asks the loaded model with boxagent's tools |

`/api/act` types:
- `scan`, `baseline`
- `power` (`key`, `value`), `preset` (`name`: max, quiet or default)
- `svc` (`unit`, `op`: start, stop or restart; aibox units only)
- `timer` (`unit`, `op`: run, enable or disable; aibox timers only)
- `kill`, `renice` (`pid`, `n`; aibox or normal-user processes only)
- `model_load` (`id`), `model_unload` (`id`), `model_unload_all`
- `ack` (`key`), `unack`

Example:

```bash
curl -H "Authorization: Bearer $TOKEN" -H "X-Box: 1" -H "Content-Type: application/json" \
     -d '{"type":"model_load","id":"qwen3-4b-instruct-q4_k_m"}' http://box:9060/api/act
```

## hf-fetch (`:9070`)

| Method | Path | Body / query | Does |
|---|---|---|---|
| GET | `/api/search` | `?q=qwen coder&sort=downloads` | GGUF repos on Hugging Face |
| GET | `/api/files` | `?repo=owner/name` | quants with size and whether they fit this box |
| POST | `/api/download` | `{"repo": "owner/name", "name": "<file group from /api/files>", "mmproj": false}` | queue a download (resumes, sha256-checked, then added to llama-swap) |
| GET | `/api/jobs` | | download progress |
| POST | `/api/cancel` | `{"id": "..."}` | cancel a download |
| GET | `/api/installed`, `/api/partials`, `/api/history`, `/api/status` | | what's in the inbox, unfinished files, past downloads, free disk |
| POST | `/api/delete` | `{"name": "<folder in the inbox>"}` | delete a downloaded model |

Scripted download:

```bash
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"repo":"bartowski/SmolLM2-135M-Instruct-GGUF","name":"SmolLM2-135M-Instruct-Q4_K_M"}' \
     http://box:9070/api/download
```

## Adding models without the API

Copy `.gguf` files, or a folder holding a model and its single `mmproj` file, into the inbox. The
`aibox-model-scan` timer adds them to llama-swap within about 30 seconds. Files still being copied are left
until they stop changing.
