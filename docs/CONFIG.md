# Configuration

All settings live in **`/etc/aibox/aibox.env`**, written by `install.sh`. Every aibox service reads it at
startup, and an environment variable with the same name overrides the file. After you edit it, run
`sudo aibox restart`.

Re-running `install.sh` keeps your edits. It reads back the model folders, bind address, trusted list, LAN
setting of llama-swap, feature flags and a llama-server you supplied yourself. Installer options override
those. It always resets `AIBOX_HOME`, `AIBOX_DATA`, `AIBOX_SWAP_CONFIG` and `AIBOX_LLAMA_SWAP` to the standard
locations.

## Paths

| Key | Default | Meaning |
|---|---|---|
| `AIBOX_HOME` | `/opt/aibox` | code (root-owned, replaced on upgrade) |
| `AIBOX_DATA` | `/var/lib/aibox` | state: databases, logs, run history, llama-swap config |
| `AIBOX_MODELS` | `/srv/aibox/models` | model folders, colon-separated. All are scanned; the first holds the inbox. `install.sh --models` |
| `AIBOX_INBOX` | `<first model folder>/inbox` | where downloads and drop-ins go |
| `AIBOX_LLAMA_SERVER` | `/opt/aibox/llama.cpp/current/llama-server` | llama.cpp server binary. `install.sh --llama-server` |
| `AIBOX_LLAMA_SWAP` | `/opt/aibox/bin/llama-swap` | llama-swap binary. `install.sh --llama-swap` |
| `AIBOX_SWAP_CONFIG` | `/var/lib/aibox/llama-swap/config.yaml` | llama-swap config (hand entries go above the AUTO-GENERATED block) |
| `AIBOX_PROJECTS` | `/var/lib/aibox/projects` | where the coding workspace creates projects; also the only folder it may open |
| `AIBOX_TOKEN_FILE` | `/etc/aibox/token` | the shared access token (`sudo aibox token --rotate` replaces it) |
| `AIBOX_HF_TOKEN_FILE` | `/etc/aibox/hf_token` | optional Hugging Face token for gated repos |
| `AIBOX_SECRETS_FILE` | `/etc/aibox/secrets.json` | API keys for remote coordinators (see below) |

The model folders have to be readable by the `aibox` account. Downloads also need write access to the inbox
(created inside the first folder). To use an existing collection, run
`sudo chgrp -R aibox /path/to/models && sudo chmod -R g+rX /path/to/models`, or give `aibox` an ACL.

## Network and access

| Key | Default | Meaning |
|---|---|---|
| `AIBOX_BIND` | `0.0.0.0` | address the three web apps listen on. `127.0.0.1` keeps them on the box (for example behind a reverse proxy) |
| `AIBOX_BENCH_PORT` | `9050` | modelbench |
| `AIBOX_BOX_PORT` | `9060` | boxagent |
| `AIBOX_HF_PORT` | `9070` | Get models (hf-fetch) |
| `AIBOX_TERMINAL_PORT` | `8765` | terminal.. UI (host side) |
| `AIBOX_SWAP_BIND` | `127.0.0.1` | llama-swap listen address. `0.0.0.0` opens it to the LAN **without auth** (`--swap-on-lan`) |
| `AIBOX_SWAP_PORT` | `9010` | llama-swap |
| `AIBOX_TRUSTED` | empty | IPs or CIDRs let in without the token, e.g. `10.0.0.5,192.168.1.0/24`. Empty means nobody, not even localhost |
| `AIBOX_PUBLIC_HOST` | empty | host name to use in links between the apps. Empty uses whatever the browser used |
| `AIBOX_URL_BENCH`, `AIBOX_URL_BOX`, `AIBOX_URL_HF`, `AIBOX_URL_TERMINAL` | empty | full base URLs for the links, when the apps sit behind a reverse proxy on different names |

If you change a port, re-run `install.sh` so it updates the firewall hints and the terminal.. socket units.

## Models and memory

| Key | Default | Meaning |
|---|---|---|
| `AIBOX_CTX` | `32768` | context size for auto-added llama-swap entries |
| `AIBOX_MAX_MODEL_GB` | `0` (auto) | skip GGUFs bigger than this when building the model list and in "fits". Auto uses GPU memory, GTT on APUs, or 80 % of RAM with no GPU |
| `AIBOX_VRAM_GB` | `0` (detect) | override detected GPU memory if detection is wrong |
| `AIBOX_RAM_FLOOR_GB` | `1.5` | a test or bench-loaded model is killed if free RAM drops below this |
| `AIBOX_MIN_FREE_DISK_GB` | `10` | downloads refuse to take the disk below this |
| `AIBOX_LLAMA_DEVICE` | empty | `--device` for bench-loaded models (e.g. `Vulkan0`, `CUDA0`). Empty lets llama.cpp choose |
| `AIBOX_BENCH_LLAMA_PORTS` | `9030,9031,9032,9033` | ports modelbench uses for its own llama-servers. The first is the test port |
| `AIBOX_BENCH_MAX_LOADED` | `3` | how many models modelbench keeps loaded at once |
| `AIBOX_SMALL_MODELS` | empty | llama-swap ids boxagent's "Ask the model" prefers when nothing is loaded, comma-separated |

## Features

| Key | Default | Meaning |
|---|---|---|
| `AIBOX_CODING_HARNESS` | `1` | coding workspace at `/play` (`install.sh --no-coding-harness` turns it off) |
| `AIBOX_SANDBOX_IMAGE` | `aibox-sandbox:latest` | image the coding workspace runs commands in (`/opt/aibox/sandbox/Dockerfile`, `sudo aibox sandbox-rebuild`) |
| `AIBOX_TERMINAL` | `0` | terminal.. installed (`install.sh --terminal`) |
| `AIBOX_TERMINAL_MODEL` | empty | model terminal.. starts on (a llama-swap id). You can also pick it in its Runtime settings |
| `AIBOX_TERMINAL_SUBNET` / `AIBOX_TERMINAL_GATEWAY` | `172.31.250.0/24` / `.1` | the agent container's private network. Change it if it clashes with yours, then re-run install.sh |
| `AIBOX_TERMINAL_LOCAL_PORT` / `AIBOX_TERMINAL_WEB_PORT` | `18765` / `18081` | loopback ports the container publishes |
| `AIBOX_WEB_SHELL` | `0` | browser shell in boxagent (`install.sh --web-shell`) |
| `AIBOX_SHELL_PORT` | `9065` | loopback port for that shell |

## Remote coordinators (team and overlord modes)

Classic chat's team and overlord modes can use a hosted model as the coordinator. Two Anthropic entries come
built in, and they need a key. Put it in `/etc/aibox/secrets.json`, then run `sudo aibox restart`:

```json
{ "ANTHROPIC_API_KEY": "sk-ant-..." }
```

To add others (any OpenAI-compatible API), create `/var/lib/aibox/modelbench/config.json`, owned by aibox:

```json
{
  "remote_coordinators": {
    "my-gpt": { "label": "My hosted model", "kind": "openai", "url": "https://api.example.com/v1/chat/completions",
                "model": "model-name", "key_name": "my_gpt_key" }
  }
}
```

The key goes into `secrets.json` under `my_gpt_key`. This file replaces the built-in list, so copy the
Anthropic entries over if you still want them (see `CFG` at the top of `modelbench/bench.py`). The same file can
override any other `CFG` value in bench.py, such as `kv_type`, `vram_margin_gb` or `load_timeout`.

## Coding workspace

You can add an optional `/var/lib/aibox/modelbench/coding_harness.json`:

```json
{ "projects_dir": "/var/lib/aibox/projects", "allowed_roots": ["/var/lib/aibox/projects"], "image": "aibox-sandbox:latest" }
```

The root helper only mounts folders under `AIBOX_PROJECTS` and `/var/lib/aibox/modelbench`, so
`allowed_roots` outside those won't run. For your projects' toolchains (Go, Rust, extra Python packages), add
them to `/opt/aibox/sandbox/Dockerfile` and run `sudo aibox sandbox-rebuild`. The installer overwrites that
file on upgrade, so keep your own copy, or set `AIBOX_SANDBOX_IMAGE` to an image you build yourself.

## llama-swap entries

`gen_models.py` adds one entry per GGUF it finds, using `${common_auto}` with `-ngl 99 -fa on -ctk q8_0 -ctv q8_0`.
To tune a model (context, sampling, draft model, extra flags), copy its entry above the AUTO-GENERATED block in
`AIBOX_SWAP_CONFIG` and edit it. The generator then skips that file. Options are documented at
https://github.com/mostlygeek/llama-swap. llama-swap reloads the config by itself when the file changes.
