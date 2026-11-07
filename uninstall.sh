#!/usr/bin/env bash
# Remove aibox-kit. Keeps settings (/etc/aibox), state (/var/lib/aibox), the terminal.. volumes and your
# models unless you pass --purge (models are never deleted).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
PURGE=0; [ "${1:-}" = --purge ] && PURGE=1
UNITS="aibox-terminal.service aibox-terminal-ui.socket aibox-terminal-ui.service aibox-terminal-models.socket
       aibox-terminal-models.service aibox-terminal-net.service aibox-shell.service aibox-model-scan.timer
       aibox-model-scan.service aibox-hf-fetch.service aibox-boxagent.service aibox-modelbench.service aibox-llama-swap.service"
for u in $UNITS; do systemctl disable --now "$u" >/dev/null 2>&1 || true; rm -f "/etc/systemd/system/$u"; done
systemctl daemon-reload
pkill -u aibox -f llama-server 2>/dev/null || true
if command -v docker >/dev/null && [ -f /opt/aibox/terminal/compose.yaml ]; then
  docker compose -p aibox-terminal -f /opt/aibox/terminal/compose.yaml down $([ "$PURGE" = 1 ] && echo -v) >/dev/null 2>&1 || true
fi
rm -f /etc/sudoers.d/aibox /usr/local/bin/aibox
rm -rf /usr/local/lib/aibox /opt/aibox
if [ "$PURGE" = 1 ]; then
  rm -rf /etc/aibox /var/lib/aibox
  userdel aibox 2>/dev/null || true
  echo "aibox removed, including settings and state. Model folders were left alone."
else
  echo "aibox removed. Kept /etc/aibox, /var/lib/aibox and your models (uninstall.sh --purge removes the first two)."
fi
