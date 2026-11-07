# Running aibox

## Where things are

| What | Where |
|---|---|
| settings | `/etc/aibox/aibox.env`, plus `token`, `hf_token`, `secrets.json` and `terminal.env` in the same folder |
| code | `/opt/aibox` (replaced on upgrade). llama.cpp is in `/opt/aibox/llama.cpp/<release>`, with `current` linking to the one in use |
| state | `/var/lib/aibox`: `modelbench/` (bench.db, run logs, coding-harness runs), `boxagent/` (state, history), `hf-fetch/`, `llama-swap/config.yaml`, `projects/` |
| models | the folders in `AIBOX_MODELS` |
| services | `aibox-llama-swap`, `aibox-modelbench`, `aibox-boxagent`, `aibox-hf-fetch`, `aibox-model-scan.timer`, plus `aibox-shell` and `aibox-terminal*` when installed |
| root helper | `/usr/local/lib/aibox/aibox-priv`, allowed in `/etc/sudoers.d/aibox` |

## Everyday commands

```
sudo aibox status                 services, addresses, models
sudo aibox logs <service> [n]     modelbench, boxagent, hf-fetch, llama-swap, terminal, model-scan
sudo aibox restart                after editing aibox.env
sudo aibox scan                   rescan model folders now, with output
sudo aibox token --rotate         new token; everyone signs in again
sudo aibox terminal-token         terminal.. pairing token
sudo aibox sandbox-rebuild        after editing /opt/aibox/sandbox/Dockerfile
```

## Upgrading

`git pull && sudo ./install.sh` keeps settings, the token, state and models. Add the same options you used the
first time, or leave them off: the installer remembers `--models`, `--bind`, `--trusted`, `--terminal`,
`--web-shell` and the coding-harness setting.

Each re-run also moves you to the newest llama.cpp release, unless you supplied your own with `--llama-server`,
which is kept. Older releases stay in `/opt/aibox/llama.cpp/`. To roll back, point `current` at an older one
and run `sudo aibox restart`. To get a newer llama-swap, delete
`/opt/aibox/bin/llama-swap` and re-run.

## Backups

Worth keeping: `/etc/aibox`, `/var/lib/aibox` (stop `aibox-modelbench` first, or use `sqlite3 .backup` for the
`.db`/`.sqlite3` files), and your own hand-written llama-swap entries. Models can be downloaded again. The
terminal.. memory is in the Docker volume `aibox-terminal_terminal-memory`.

## HTTPS and reverse proxies

The apps speak plain HTTP. For HTTPS, or for names like `bench.lan.example`:

1. Put Caddy or nginx in front, and set `--bind 127.0.0.1` (re-run install.sh) so only the proxy reaches the
   apps.
2. Set `AIBOX_URL_BENCH`, `AIBOX_URL_BOX`, `AIBOX_URL_HF` and `AIBOX_URL_TERMINAL` to the public URLs, so the
   apps link to each other correctly.
3. Pass the original `Host` header through. The apps compare `Origin` with `Host` on POSTs, and a proxy that
   rewrites `Host` makes the browser's own POSTs fail with "cross-origin request refused".
4. Keep the token. `AIBOX_TRUSTED` with the proxy's address would let anyone who can reach the proxy straight
   in.

Caddy example (LAN names with Caddy's internal CA):

```
bench.lan.example {
	tls internal
	reverse_proxy 127.0.0.1:9050
}
box.lan.example {
	tls internal
	reverse_proxy 127.0.0.1:9060
}
hf.lan.example {
	tls internal
	reverse_proxy 127.0.0.1:9070
}
```

The cookie is per host name, so behind separate names each app asks for the token once.

For access from outside the house, use a VPN (Tailscale, WireGuard) rather than port forwarding.

## Firewall

The installer doesn't change the firewall, except that `--open-firewall CIDR` adds ufw allow rules for the UI
ports. With ufw active and nothing allowed, the pages work only from the box itself. llama-swap is on
127.0.0.1 unless you chose `--swap-on-lan`. The terminal.. UI is published through a systemd socket on the
host, so ufw covers it (ports Docker publishes would bypass ufw).

## Troubleshooting

**A page says "Enter the access token" every time.** The browser is dropping the cookie. That happens with
private windows or when the host name changes between visits. Bookmark one address.

**"cross-origin request refused" when clicking things.** A proxy is changing the `Host` header, or the page
was opened through another name than it posts to. See the reverse proxy section.

**No models in the list.** Run `sudo aibox scan`. It prints why files were skipped: too big for the box,
still being copied, a space in the path, or not a chat model (mmproj, embedding, reranker). Check the
`aibox` account can read the folder: `sudo -u aibox ls <folder>`.

**A model won't load, or loads on the CPU.**
1. Check the GPU: `vulkaninfo --summary` (Vulkan build) or `nvidia-smi`.
2. Look at the log: `sudo aibox logs llama-swap`.
3. For a bench-loaded model, read the log tail on modelbench's result panel, or
   `/var/lib/aibox/modelbench/runs/`.
4. The `aibox` account needs the `video` and `render` groups, which the installer adds. A reboot or re-login
   of the service may be needed after a driver install.
5. For CUDA or ROCm, build llama.cpp yourself and pass `--llama-server`.

**modelbench says a model won't fit but it should.** Memory is estimated as weights + KV cache at the chosen
context + 1.5 GB of buffers, against free GPU memory minus a 1.5 GB margin. Lower the context (the ctx box) or
set `AIBOX_VRAM_GB` if detection is wrong. On AMD APUs with a small BIOS carve-out, the GTT pool counts as GPU
memory.

**Downloads fail with 401/403.** The repo is gated. Accept its licence on Hugging Face, put a token in
`/etc/aibox/hf_token`, and run `sudo aibox restart`.

**"refused: … must keep N GB free".** The disk floor (`AIBOX_MIN_FREE_DISK_GB`) is doing its job. Free space
or lower the floor.

**Coding workspace says "Container unavailable".** Docker is missing or the image isn't built. Run
`sudo aibox sandbox-rebuild`, then check `sudo -u aibox sudo -n /usr/local/lib/aibox/aibox-priv sandbox-check`.

**terminal.. can't reach the model.** In terminal.. Runtime settings, the base URL must be
`http://172.31.250.1:9010/v1` (or your `AIBOX_TERMINAL_GATEWAY`). Check `systemctl status
aibox-terminal-models.socket aibox-terminal-net`. If that subnet clashes with your network, change
`AIBOX_TERMINAL_SUBNET`/`AIBOX_TERMINAL_GATEWAY` and re-run install.sh.

**boxagent warns about "Package files (dpkg --verify)".** Some images strip docs and man pages, and those
are ignored. Anything it still lists was changed after install. Look at it, and once it's expected, save a
new baseline.

**Power settings keep reverting.** boxagent puts the CPU/GPU settings back to the saved baseline. Change them
on its CPU and GPU tab (or save a new baseline) rather than by hand.
