# aibox-kit

Turn a Linux box with a GPU (or just a lot of RAM) into a local-model appliance you run from a browser:

- **modelbench**: see every model server that's running and what it uses, test models safely (it refuses
  anything that won't fit and kills a model that starts eating the box), load models, and chat with them.
  It includes team/overlord modes and an optional coding workspace where several local models plan, edit
  and test code in throw-away containers.
- **boxagent**: the box manager. It shows live CPU/GPU/RAM, processes, services, timers and 7 days of history,
  runs integrity scans against a saved baseline, sets CPU/GPU power levels, and fixes safe problems by itself
  (restarting a crashed service, killing a runaway process when RAM runs out, freeing disk). It has a model
  list with load/unload and a small "ask the model" chat.
- **Get models (hf-fetch)**: search Hugging Face for GGUF models, see which quants fit this box, and download
  them. Downloads resume and are checked against the repo's sha256.
- **terminal..** (optional): an agent that drives its own Linux terminal and Chromium browser inside a locked-down
  container, using Google Research's Harness² for memory and repeated learning.

All of it runs on **llama-swap** and **llama.cpp**, which serve one OpenAI-style API for every model on the
box: clients ask for a model by name and llama-swap loads it on demand. New `.gguf` files dropped into the
inbox folder show up in the list within about 30 seconds.

## Requirements

- Ubuntu 24.04 (tested) or another recent Debian/Ubuntu with systemd and Python 3.10+, x86_64 or arm64
- An AMD, NVIDIA or Intel GPU with a working Vulkan driver, or none (CPU-only works, slowly)
- Disk space for models. The installer itself needs about 1 GB, plus about 3 GB for terminal..
- For `--terminal` and `--coding-harness`: Docker with the compose plugin. On Ubuntu the installer adds it; on
  Debian install Docker Engine from docker.com first

## Install

```bash
git clone https://github.com/<you>/aibox-kit
cd aibox-kit
sudo ./install.sh                                   # GPU auto-detect, models in /srv/aibox/models
```

Then open `http://<box>:9050/` and paste the access token (`sudo aibox token`). The same token works for all
three pages, and a cookie remembers it.

Common options:

```bash
sudo ./install.sh --models /data/gguf               # your own model folder(s), colon-separated
sudo ./install.sh --terminal                        # add the terminal.. agent
sudo ./install.sh --coding-harness                  # add the coding workspace (read Security first)
sudo ./install.sh --web-shell                       # add a browser shell to boxagent (asks for a Linux login)
sudo ./install.sh --llama-server /opt/llama-cuda/llama-server   # bring your own llama.cpp build
sudo ./install.sh --open-firewall 192.168.1.0/24    # if ufw is on: let your LAN reach the pages
sudo ./install.sh --help
```

The installer downloads the latest llama.cpp release (Vulkan build when it finds a GPU, CPU build otherwise)
and the latest llama-swap release from GitHub. For CUDA or ROCm, build llama.cpp yourself and pass
`--llama-server`. Running the installer again upgrades the code and keeps your settings, token, models and state.

## Using it

| Page | Default address | What for |
|---|---|---|
| modelbench | `http://<box>:9050/` | running models, tests, load, chat (`/play`) |
| boxagent | `http://<box>:9060/` | health, processes, services, power, integrity, models |
| Get models | `http://<box>:9070/` | Hugging Face search and download |
| terminal.. | `http://<box>:8765/` | the terminal agent (pairing token: `sudo aibox terminal-token`) |
| Model API | `http://127.0.0.1:9010/v1` | OpenAI-style API for every model (this box only, unless `--swap-on-lan`) |

Other machines reach every model through modelbench with the token. Set your client's base URL to
`http://<box>:9050/swap/v1` and its API key to the token:
`curl -H "Authorization: Bearer $TOKEN" http://<box>:9050/swap/v1/models`.

Adding models:
- use **+ GET MODELS**, or
- copy `.gguf` files (or a folder with a model and its `mmproj` vision projector) into
  `<first model folder>/inbox`.

Hand-tuned model entries (sampling, context, extra flags) go in `/var/lib/aibox/llama-swap/config.yaml`, above
the AUTO-GENERATED block.

Helper commands:

```
sudo aibox status            services, addresses, models
sudo aibox token [--rotate]  show or replace the access token
sudo aibox logs modelbench   last log lines of a service (llama-swap, boxagent, hf-fetch, terminal)
sudo aibox scan              rescan the model folders now
sudo aibox restart           restart every aibox service
```

Settings live in `/etc/aibox/aibox.env` (every key and its default is listed in `lib/aibox_conf.py`). After an
edit, run `sudo aibox restart`.

## Security

The pages can start processes, manage services and download files, so treat the token like a password and keep
the ports on your LAN. Don't forward them to the internet; use a VPN such as Tailscale or WireGuard for remote
access.

- **Every request needs the token**, including requests from localhost. To let a trusted reverse proxy or one
  admin PC in without it, set `AIBOX_TRUSTED=192.168.1.10,10.0.0.0/24`.
- **The services run as an unprivileged `aibox` account.** The only thing it may run as root is
  `/usr/local/lib/aibox/aibox-priv`, which has a fixed list of checked jobs: power settings in sysfs, integrity
  hashes, dpkg verify, SMART, signalling a process, systemctl on a unit, reading a unit's journal, and cleaning
  up logs. A bug in a dashboard therefore cannot become an arbitrary root command.
- **llama-swap has no authentication**, so by default it listens on 127.0.0.1 only. `--swap-on-lan` opens it.
  Only use that on a network you trust.
- **terminal..** runs in a container with all capabilities dropped and no Docker socket. Its network is locked
  down: it can reach the internet, DNS, and the box's model API only. It cannot reach your LAN, this box's other
  services or SSH. The UI is published through a systemd socket on the host, so the host firewall applies to
  it (Docker-published ports bypass ufw).
- **The coding harness** runs every command in a throw-away container with no network, a read-only root,
  no capabilities and resource limits. To start those containers the `aibox` account joins the `docker`
  group, **which is root-equivalent**. Anyone holding the token can use the harness. That's why it's off by
  default.
- **The browser shell** (`--web-shell`) is ttyd on loopback behind boxagent's token, and it still asks for a
  normal Linux login.

## What's in the box

```
install.sh / uninstall.sh   installer (re-run to upgrade) and remover
lib/aibox_conf.py           shared settings, token check, links between the pages
lib/aibox-priv              the root helper (see Security)
modelbench/                 bench, classic chat, coding harness
boxagent/                   box manager
hf-fetch/                   Hugging Face search and download
llama-swap/gen_models.py    keeps the llama-swap model list in step with the model folders
terminal/                   terminal.. app (app/), its compose file and network lock
sandbox/Dockerfile          image the coding harness runs commands in
systemd/                    unit files
scripts/aibox               the helper command
tests/                      smoke test, stand-in llama-server/llama-swap, systemd test box
```

## Optional: a Windows PC as a workspace

Classic chat (`/play/chat`) can give models a PowerShell workspace on a Windows PC through an
aissh6-compatible command daemon. Set `AIBOX_REMOTE_SHELL_URL` (and put its bearer token in
`/etc/aibox/remote_shell_token`) to turn on the workspace picker, terminal, jobs and build mode. Without it,
classic chat models get web search and web fetch only.

## Testing

`tests/systemd-box/Dockerfile` builds a throw-away Ubuntu box with systemd. With the stand-in `llama-server` and
`llama-swap` in `tests/stubs/`, you can run the whole install without a GPU:

```bash
docker build -t aibox-testbox tests/systemd-box
docker run -d --name aibox-test --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --tmpfs /run --tmpfs /run/lock -v "$PWD":/kit:ro aibox-testbox
docker exec aibox-test /kit/install.sh -y --llama-server /kit/tests/stubs/llama-server --llama-swap /kit/tests/stubs/llama-swap
docker exec aibox-test /kit/tests/smoke.sh
```

## Uninstall

```bash
sudo ./uninstall.sh            # keeps /etc/aibox, /var/lib/aibox and your models
sudo ./uninstall.sh --purge    # also removes settings, state and the aibox account (never your models)
```

## Licence

MIT, see `LICENSE`. Third-party parts and their licences are listed in `NOTICE`.
