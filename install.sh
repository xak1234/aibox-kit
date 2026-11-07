#!/usr/bin/env bash
# aibox-kit installer: modelbench + boxagent + hf-fetch on llama-swap/llama.cpp, with the optional
# terminal.. agent, browser shell and coding harness. Debian/Ubuntu with systemd. Run as root.
#
#   sudo ./install.sh                       # GPU auto-detect, models in /srv/aibox/models
#   sudo ./install.sh --models /data/gguf --terminal
#   sudo ./install.sh --help
#
# Re-running upgrades the code and keeps your settings, token, models and state.
set -euo pipefail

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=/opt/aibox
DATA=/var/lib/aibox
ETC=/etc/aibox
ENVF=$ETC/aibox.env
LIBX=/usr/local/lib/aibox
SVC=aibox

MODELS="" BIND="" TRUSTED="" LLAMA="auto" LLAMA_SERVER="" SWAP_BIN="" OPEN_FW="" YES=0
WANT_TERMINAL=0 WANT_SHELL=0 WANT_HARNESS="" SWAP_LAN=0

usage() {
  cat <<'EOF'
Usage: sudo ./install.sh [options]

  --models DIR[:DIR...]   model folders to scan (default /srv/aibox/models). The first one holds the inbox
                          that downloads and drop-ins go to.
  --bind ADDR             address the web UIs listen on (default 0.0.0.0 = every interface)
  --trusted CIDR[,CIDR]   addresses allowed in WITHOUT the token (default: none, not even localhost)
  --llama vulkan|cpu      which llama.cpp build to download (default: vulkan when a GPU is found, else cpu)
  --llama-server PATH     use your own llama-server (e.g. a CUDA or ROCm build) instead of downloading one
  --llama-swap PATH       use your own llama-swap binary instead of downloading one
  --swap-on-lan           let the LAN reach the model API (llama-swap has no auth; default: this box only)
  --terminal              also install the terminal.. agent (Docker)
  --no-coding-harness     leave out modelbench's coding workspace (on by default; needs Docker, which the
                          installer adds). Its containers start through the root helper - the aibox account
                          is NOT put in the docker group.
  --web-shell             add a browser shell to boxagent (ttyd, asks for a normal Linux login)
  --open-firewall CIDR    if ufw is active, allow CIDR to the UI ports (e.g. 192.168.1.0/24)
  -y, --yes               do not ask before installing packages
  -h, --help              this text
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --models) MODELS="$2"; shift 2 ;;
    --bind) BIND="$2"; shift 2 ;;
    --trusted) TRUSTED="$2"; shift 2 ;;
    --llama) LLAMA="$2"; shift 2 ;;
    --llama-server) LLAMA_SERVER="$2"; shift 2 ;;
    --llama-swap) SWAP_BIN="$2"; shift 2 ;;
    --swap-on-lan) SWAP_LAN=1; shift ;;
    --terminal) WANT_TERMINAL=1; shift ;;
    --coding-harness) WANT_HARNESS=1; shift ;;
    --no-coding-harness) WANT_HARNESS=0; shift ;;
    --web-shell) WANT_SHELL=1; shift ;;
    --open-firewall) OPEN_FW="$2"; shift 2 ;;
    -y|--yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo ./install.sh)"
command -v systemctl >/dev/null && [ -d /run/systemd/system ] || die "systemd is required"
command -v apt-get >/dev/null || die "only Debian/Ubuntu (apt) is supported for now"

case "$(uname -m)" in
  x86_64) ARCH_LLAMA=x64; ARCH_GO=amd64 ;;
  aarch64|arm64) ARCH_LLAMA=arm64; ARCH_GO=arm64 ;;
  *) die "unsupported CPU architecture $(uname -m)" ;;
esac

# keep settings from an earlier install
envget() { [ -f "$ENVF" ] && sed -n "s/^$1=//p" "$ENVF" | tail -1 | sed 's/^"\(.*\)"$/\1/' || true; }
[ -n "$MODELS" ] || MODELS="$(envget AIBOX_MODELS)"; [ -n "$MODELS" ] || MODELS=/srv/aibox/models
[ -n "$BIND" ] || BIND="$(envget AIBOX_BIND)"; [ -n "$BIND" ] || BIND=0.0.0.0
[ -n "$TRUSTED" ] || TRUSTED="$(envget AIBOX_TRUSTED)"
# a llama-server you supplied earlier (outside /opt/aibox) is kept unless you pass a new one
if [ -z "$LLAMA_SERVER" ]; then
  _prev="$(envget AIBOX_LLAMA_SERVER)"
  case "$_prev" in ""|"$PREFIX"/llama.cpp/*) ;; *) [ -x "$_prev" ] && LLAMA_SERVER="$_prev" ;; esac
fi
[ "$(envget AIBOX_TERMINAL)" = 1 ] && WANT_TERMINAL=1
[ "$(envget AIBOX_WEB_SHELL)" = 1 ] && WANT_SHELL=1
[ -n "$WANT_HARNESS" ] || WANT_HARNESS="$(envget AIBOX_CODING_HARNESS)"; [ -n "$WANT_HARNESS" ] || WANT_HARNESS=1
[ "$(envget AIBOX_SWAP_BIND)" = 0.0.0.0 ] && SWAP_LAN=1
NEED_DOCKER=$(( WANT_TERMINAL || WANT_HARNESS ))

# ---------------------------------------------------------------- GPU
GPU=none
for v in /sys/class/drm/card[0-9]*/device/vendor; do
  [ -r "$v" ] || continue
  case "$(cat "$v")" in
    0x1002) GPU=amd ;;
    0x10de) [ "$GPU" = none ] && GPU=nvidia ;;
    0x8086) [ "$GPU" = none ] && GPU=intel ;;
  esac
done
command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1 && [ "$GPU" = none ] && GPU=nvidia
if [ "$LLAMA" = auto ]; then [ "$GPU" = none ] && LLAMA=cpu || LLAMA=vulkan; fi
case "$LLAMA" in vulkan|cpu) ;; *) die "--llama must be vulkan or cpu" ;; esac
say "GPU: $GPU; llama.cpp build: ${LLAMA_SERVER:-$LLAMA}"

# ---------------------------------------------------------------- packages
PKGS="python3 python3-venv curl ca-certificates aria2 smartmontools iptables pciutils"
[ "$LLAMA" = vulkan ] && [ -z "$LLAMA_SERVER" ] && PKGS="$PKGS libvulkan1 mesa-vulkan-drivers vulkan-tools"
[ "$LLAMA" = cpu ] && [ -z "$LLAMA_SERVER" ] && PKGS="$PKGS libgomp1"
[ "$WANT_SHELL" = 1 ] && PKGS="$PKGS ttyd"
if [ "$NEED_DOCKER" = 1 ] && ! command -v docker >/dev/null; then PKGS="$PKGS docker.io"; fi
MISSING=""
for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"; done
if [ -n "$MISSING" ]; then
  say "packages to install:$MISSING"
  if [ "$YES" != 1 ] && [ -t 0 ]; then read -r -p "Install them now? [Y/n] " a; case "$a" in n*|N*) die "stopped" ;; esac; fi
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $MISSING
fi
# the ttyd package enables its own ttyd.service; aibox runs ttyd itself on loopback only
if dpkg -s ttyd >/dev/null 2>&1 && systemctl is-enabled ttyd.service >/dev/null 2>&1; then
  systemctl disable --now ttyd.service >/dev/null 2>&1 || true
fi
if [ "$NEED_DOCKER" = 1 ]; then
  systemctl enable --now docker >/dev/null 2>&1 || true
  if ! docker compose version >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-compose-v2 2>/dev/null \
      || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-compose-plugin 2>/dev/null \
      || die "docker compose is missing; install Docker Engine with the compose plugin (https://docs.docker.com/engine/install/) and re-run"
  fi
fi

# ---------------------------------------------------------------- account and folders
if ! id "$SVC" >/dev/null 2>&1; then
  say "creating system account $SVC"
  useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin --user-group "$SVC"
fi
for g in video render; do getent group "$g" >/dev/null && usermod -aG "$g" "$SVC"; done

# Folders the aibox account owns are only ever CREATED by root here, never chowned/chmodded again on a
# re-run: aibox could have swapped one for a symlink, and root must not follow it.
mkdir_svc() {   # MODE DIR
  if [ -L "$2" ]; then die "$2 is a symlink - refusing to touch it"; fi
  [ -d "$2" ] || install -d -m "$1" -o "$SVC" -g "$SVC" "$2"
}
install -d -m 0755 "$PREFIX" "$PREFIX/bin" "$LIBX"
install -d -m 0750 -o root -g "$SVC" "$ETC"
mkdir_svc 0750 "$DATA"
mkdir_svc 0750 "$DATA/llama-swap"
mkdir_svc 0750 "$DATA/projects"
IFS=: read -ra MROOTS <<< "$MODELS"
for d in "${MROOTS[@]}"; do
  mkdir_svc 0775 "$d"
  sudo -u "$SVC" test -r "$d" || warn "the aibox account cannot read $d"
done
INBOX="${MROOTS[0]}/inbox"
mkdir_svc 0775 "$INBOX"

# ---------------------------------------------------------------- code
say "installing code to $PREFIX"
for part in lib modelbench boxagent hf-fetch llama-swap terminal sandbox; do
  rm -rf "${PREFIX:?}/$part.new"
  cp -a "$KIT/$part" "$PREFIX/$part.new"
  rm -rf "${PREFIX:?}/$part"
  mv "$PREFIX/$part.new" "$PREFIX/$part"
done
chown -R root:root "$PREFIX/lib" "$PREFIX/modelbench" "$PREFIX/boxagent" "$PREFIX/hf-fetch" "$PREFIX/llama-swap" "$PREFIX/terminal" "$PREFIX/sandbox"
chmod -R go-w "$PREFIX"
install -m 0755 -o root -g root "$KIT/lib/aibox-priv" "$LIBX/aibox-priv"
install -m 0755 -o root -g root "$KIT/terminal/aibox-terminal-net" "$LIBX/aibox-terminal-net"
install -m 0755 -o root -g root "$KIT/scripts/aibox" /usr/local/bin/aibox
cat > /etc/sudoers.d/aibox <<EOF
# aibox-kit: the aibox service account may run this one checked helper as root, nothing else
$SVC ALL=(root) NOPASSWD: $LIBX/aibox-priv
Defaults!$LIBX/aibox-priv !requiretty
EOF
chmod 0440 /etc/sudoers.d/aibox
visudo -cf /etc/sudoers.d/aibox >/dev/null || { rm -f /etc/sudoers.d/aibox; die "sudoers file rejected"; }

if [ ! -x "$PREFIX/venv/bin/python" ]; then python3 -m venv "$PREFIX/venv"; fi
"$PREFIX/venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
"$PREFIX/venv/bin/pip" install -q --upgrade ddgs >/dev/null 2>&1 || warn "could not install ddgs (web_search for models will not work until: $PREFIX/venv/bin/pip install ddgs)"

# ---------------------------------------------------------------- llama.cpp + llama-swap
gh_asset() {   # repo regex -> "tag url"
  curl -fsSL "https://api.github.com/repos/$1/releases/latest" | python3 -c '
import json, re, sys
r = json.load(sys.stdin); rx = re.compile(sys.argv[1])
for a in r.get("assets", []):
    if rx.search(a["name"]):
        print(r["tag_name"], a["browser_download_url"]); break' "$2"
}
if [ -n "$LLAMA_SERVER" ]; then
  [ -x "$LLAMA_SERVER" ] || die "$LLAMA_SERVER is not an executable"
else
  if [ "$LLAMA" = vulkan ]; then RX="bin-ubuntu-vulkan-$ARCH_LLAMA\\.(tar\\.gz|zip)$"; else RX="bin-ubuntu-$ARCH_LLAMA\\.(tar\\.gz|zip)$"; fi
  read -r TAG URL < <(gh_asset ggml-org/llama.cpp "$RX" || true) || true
  [ -n "${URL:-}" ] || die "could not find a llama.cpp $LLAMA build for $ARCH_LLAMA on GitHub; build llama.cpp yourself and pass --llama-server PATH"
  DEST="$PREFIX/llama.cpp/$TAG-$LLAMA"
  if [ ! -d "$DEST" ]; then
    say "downloading llama.cpp $TAG ($LLAMA)"
    TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
    curl -fL --retry 3 -o "$TMP/llama.pkg" "$URL"
    mkdir -p "$TMP/x"
    case "$URL" in *.zip) python3 -c 'import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$TMP/llama.pkg" "$TMP/x" ;; *) tar -xzf "$TMP/llama.pkg" -C "$TMP/x" ;; esac
    BIN="$(find "$TMP/x" -type f -name llama-server | head -1)"
    [ -n "$BIN" ] || die "llama-server not found in the llama.cpp download"
    mkdir -p "$PREFIX/llama.cpp"
    mv "$(dirname "$BIN")" "$DEST"
    chmod 0755 "$DEST"/llama-* 2>/dev/null || true
  fi
  ln -sfn "$DEST" "$PREFIX/llama.cpp/current"
  LLAMA_SERVER="$PREFIX/llama.cpp/current/llama-server"
fi
if [ -n "$SWAP_BIN" ]; then
  [ -x "$SWAP_BIN" ] || die "$SWAP_BIN is not an executable"
  install -m 0755 "$SWAP_BIN" "$PREFIX/bin/llama-swap"
elif [ ! -x "$PREFIX/bin/llama-swap" ]; then
  read -r STAG SURL < <(gh_asset mostlygeek/llama-swap "linux_$ARCH_GO\\.tar\\.gz$" || true) || true
  [ -n "${SURL:-}" ] || die "could not find a llama-swap release for linux/$ARCH_GO; pass --llama-swap PATH"
  say "downloading llama-swap $STAG"
  T2="$(mktemp -d)"
  curl -fL --retry 3 -o "$T2/s.tgz" "$SURL" && tar -xzf "$T2/s.tgz" -C "$T2"
  install -m 0755 "$(find "$T2" -type f -name llama-swap | head -1)" "$PREFIX/bin/llama-swap"
  rm -rf "$T2"
fi

# ---------------------------------------------------------------- settings
if [ ! -s "$ETC/token" ]; then
  python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$ETC/token"
  say "generated a new access token"
fi
chown root:"$SVC" "$ETC/token"; chmod 0640 "$ETC/token"
[ -f "$ETC/hf_token" ] || install -m 0640 -o root -g "$SVC" /dev/null "$ETC/hf_token"
[ -f "$ETC/secrets.json" ] || { echo '{}' > "$ETC/secrets.json"; chown root:"$SVC" "$ETC/secrets.json"; chmod 0640 "$ETC/secrets.json"; }

setenv() {   # KEY VALUE: set in aibox.env, keeping everything else
  touch "$ENVF"
  if grep -q "^$1=" "$ENVF"; then
    python3 - "$ENVF" "$1" "$2" <<'PY'
import sys
p, k, v = sys.argv[1:]
lines = open(p).read().splitlines()
open(p, "w").write("\n".join((f"{k}={v}" if l.startswith(k + "=") else l) for l in lines) + "\n")
PY
  else
    echo "$1=$2" >> "$ENVF"
  fi
}
if [ ! -f "$ENVF" ]; then
  cat > "$ENVF" <<'EOF'
# aibox settings - read by every aibox service. Restart them after a change: sudo aibox restart
# All keys and defaults: /opt/aibox/lib/aibox_conf.py
EOF
fi
setenv AIBOX_HOME "$PREFIX"
setenv AIBOX_DATA "$DATA"
setenv AIBOX_MODELS "$MODELS"
setenv AIBOX_BIND "$BIND"
setenv AIBOX_TRUSTED "$TRUSTED"
setenv AIBOX_LLAMA_SERVER "$LLAMA_SERVER"
setenv AIBOX_LLAMA_SWAP "$PREFIX/bin/llama-swap"
setenv AIBOX_SWAP_CONFIG "$DATA/llama-swap/config.yaml"
setenv AIBOX_SWAP_BIND "$([ "$SWAP_LAN" = 1 ] && echo 0.0.0.0 || echo 127.0.0.1)"
grep -q '^AIBOX_SWAP_PORT=' "$ENVF" || setenv AIBOX_SWAP_PORT 9010
grep -q '^AIBOX_BENCH_PORT=' "$ENVF" || setenv AIBOX_BENCH_PORT 9050
grep -q '^AIBOX_BOX_PORT=' "$ENVF" || setenv AIBOX_BOX_PORT 9060
grep -q '^AIBOX_HF_PORT=' "$ENVF" || setenv AIBOX_HF_PORT 9070
grep -q '^AIBOX_TERMINAL_PORT=' "$ENVF" || setenv AIBOX_TERMINAL_PORT 8765
grep -q '^AIBOX_CTX=' "$ENVF" || setenv AIBOX_CTX 32768
grep -q '^AIBOX_MIN_FREE_DISK_GB=' "$ENVF" || setenv AIBOX_MIN_FREE_DISK_GB 10
setenv AIBOX_TERMINAL "$WANT_TERMINAL"
setenv AIBOX_WEB_SHELL "$WANT_SHELL"
setenv AIBOX_CODING_HARNESS "$WANT_HARNESS"
chown root:"$SVC" "$ENVF"; chmod 0640 "$ENVF"
set -a; . "$ENVF"; set +a

CFG="$DATA/llama-swap/config.yaml"
# the config lives in a folder aibox owns, so aibox (not root) writes it
if ! sudo -u "$SVC" test -e "$CFG"; then
  sed -e "s#@LLAMA_SERVER@#$LLAMA_SERVER#" "$KIT/config/llama-swap.base.yaml" | sudo -u "$SVC" tee "$CFG" >/dev/null
else   # point the llama macro at the current llama-server
  sudo -u "$SVC" sed -i "s#^  llama: .*#  llama: \"$LLAMA_SERVER\"#" "$CFG"
fi
sudo -u "$SVC" "$PREFIX/venv/bin/python" "$PREFIX/llama-swap/gen_models.py" --apply --quiet || warn "model scan failed (see above)"

# ---------------------------------------------------------------- systemd
say "installing services"
for u in aibox-llama-swap.service aibox-model-scan.service aibox-model-scan.timer aibox-modelbench.service \
         aibox-boxagent.service aibox-hf-fetch.service; do
  install -m 0644 "$KIT/systemd/$u" "/etc/systemd/system/$u"
done
if [ "$WANT_SHELL" = 1 ]; then
  install -m 0644 "$KIT/systemd/aibox-shell.service" /etc/systemd/system/
fi
if [ "$WANT_TERMINAL" = 1 ]; then
  GW="${AIBOX_TERMINAL_GATEWAY:-172.31.250.1}"
  for u in aibox-terminal.service aibox-terminal-net.service; do install -m 0644 "$KIT/systemd/$u" /etc/systemd/system/; done
  sed -e "s#@LISTEN@#$BIND:$AIBOX_TERMINAL_PORT#" "$KIT/systemd/aibox-terminal-ui.socket" > /etc/systemd/system/aibox-terminal-ui.socket
  sed -e "s#@TARGET@#127.0.0.1:${AIBOX_TERMINAL_LOCAL_PORT:-18765}#" "$KIT/systemd/aibox-terminal-ui.service" > /etc/systemd/system/aibox-terminal-ui.service
  sed -e "s#@LISTEN@#$GW:$AIBOX_SWAP_PORT#" "$KIT/systemd/aibox-terminal-models.socket" > /etc/systemd/system/aibox-terminal-models.socket
  sed -e "s#@TARGET@#127.0.0.1:$AIBOX_SWAP_PORT#" "$KIT/systemd/aibox-terminal-models.service" > /etc/systemd/system/aibox-terminal-models.service
  # browser origins terminal.. accepts: every name/address this box answers to on the UI port
  ORIGS=""
  for h in $(hostname) "$(hostname).local" $(hostname -I 2>/dev/null) ${AIBOX_PUBLIC_HOST:-}; do
    case "$h" in *:*) h="[$h]" ;; esac
    ORIGS="$ORIGS,http://$h:$AIBOX_TERMINAL_PORT"
  done
  [ -n "${AIBOX_URL_TERMINAL:-}" ] && ORIGS="$ORIGS,${AIBOX_URL_TERMINAL%/}"
  printf 'TERMINAL_ORIGINS=%s\n' "${ORIGS#,}" > "$ETC/terminal.env"
  chmod 0640 "$ETC/terminal.env"
fi
systemctl daemon-reload
systemctl enable --now aibox-llama-swap.service aibox-modelbench.service aibox-boxagent.service aibox-hf-fetch.service aibox-model-scan.timer
systemctl restart aibox-llama-swap.service aibox-modelbench.service aibox-boxagent.service aibox-hf-fetch.service
if [ "$WANT_SHELL" = 1 ]; then systemctl enable --now aibox-shell.service; systemctl restart aibox-shell.service; fi

if [ "$WANT_HARNESS" = 1 ]; then
  say "building the coding-harness sandbox image (aibox-sandbox:latest)"
  docker build -q -t aibox-sandbox:latest "$PREFIX/sandbox" >/dev/null || warn "sandbox image build failed - coding runs will say 'container unavailable'"
fi
if [ "$WANT_TERMINAL" = 1 ]; then
  say "building the terminal.. image (first build downloads Chromium; a few minutes)"
  ( set -a; . "$ENVF"; . "$ETC/terminal.env"; set +a
    docker compose -p aibox-terminal -f "$PREFIX/terminal/compose.yaml" build -q ) || die "terminal.. image build failed"
  systemctl enable --now aibox-terminal-net.service aibox-terminal-models.socket aibox-terminal-ui.socket
  systemctl enable aibox-terminal.service
  systemctl restart aibox-terminal-net.service
  systemctl restart aibox-terminal.service
fi

# ---------------------------------------------------------------- firewall
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  PORTS="$AIBOX_BENCH_PORT,$AIBOX_BOX_PORT,$AIBOX_HF_PORT"
  [ "$WANT_TERMINAL" = 1 ] && PORTS="$PORTS,$AIBOX_TERMINAL_PORT"
  [ "$SWAP_LAN" = 1 ] && PORTS="$PORTS,$AIBOX_SWAP_PORT"
  if [ -n "$OPEN_FW" ]; then
    ufw allow from "$OPEN_FW" to any port "$PORTS" proto tcp comment aibox >/dev/null
    say "ufw: allowed $OPEN_FW to tcp $PORTS"
  else
    warn "ufw is active: allow your LAN to the UIs with e.g.  sudo ufw allow from 192.168.1.0/24 to any port $PORTS proto tcp"
  fi
fi

# ---------------------------------------------------------------- done
sleep 2
HOSTIP="$(hostname -I 2>/dev/null | awk '{print $1}')"; HOSTIP="${HOSTIP:-$(hostname)}"
echo
say "aibox is installed."
for u in aibox-llama-swap aibox-modelbench aibox-boxagent aibox-hf-fetch; do
  printf '   %-20s %s\n' "$u" "$(systemctl is-active "$u")"
done
TLINE=""
[ "$WANT_TERMINAL" = 1 ] && TLINE="
   terminal..   http://$HOSTIP:$AIBOX_TERMINAL_PORT/   (pairing token: sudo aibox terminal-token)"
cat <<EOF

   modelbench   http://$HOSTIP:$AIBOX_BENCH_PORT/
   boxagent     http://$HOSTIP:$AIBOX_BOX_PORT/
   get models   http://$HOSTIP:$AIBOX_HF_PORT/$TLINE

   Access token (all three pages):  sudo aibox token
   Model folders: $MODELS   (drop .gguf files into $INBOX)
   Settings: $ENVF        Status: sudo aibox status
EOF
