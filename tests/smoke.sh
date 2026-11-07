#!/usr/bin/env bash
# Smoke test for an installed aibox (run on the box as root). Checks access control, the three
# web apps, llama-swap and the model scan. Exit code = number of failed checks.
set -uo pipefail
. /etc/aibox/aibox.env
T=$(cat /etc/aibox/token); F=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; F=$((F+1)); }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
for p in "$AIBOX_BENCH_PORT" "$AIBOX_BOX_PORT" "$AIBOX_HF_PORT"; do
  [ "$(code http://127.0.0.1:$p/)" = 401 ] && ok "port $p refuses a request without the token" || bad "port $p answers without the token"
  [ "$(code -H "X-Key: wrong" http://127.0.0.1:$p/)" = 401 ] && ok "port $p refuses a wrong token" || bad "port $p accepts a wrong token"
  [ "$(code -H "X-Key: $T" http://127.0.0.1:$p/)" = 200 ] && ok "port $p accepts the token" || bad "port $p rejects the token"
  [ "$(code -b "aibox_key=$T" http://127.0.0.1:$p/)" = 200 ] && ok "port $p accepts the cookie" || bad "port $p rejects the cookie"
  loc=$(curl -s -o /dev/null -D - "http://127.0.0.1:$p/?key=$T" | tr -d '\r' | awk 'tolower($1)=="location:"{print $2}')
  [ -n "$loc" ] && [ "${loc#*key=}" = "$loc" ] && ok "port $p swaps ?key= for a cookie" || bad "port $p keeps the token in the URL ($loc)"
done
[ "$(code -H "X-Key: $T" "http://127.0.0.1:$AIBOX_BENCH_PORT/api/state")" = 200 ] && ok "modelbench /api/state" || bad "modelbench /api/state"
[ "$(code -b "aibox_key=$T" "http://127.0.0.1:$AIBOX_BOX_PORT/api/stats")" = 200 ] && ok "boxagent /api/stats" || bad "boxagent /api/stats"
[ "$(code -H "X-Key: $T" "http://127.0.0.1:$AIBOX_HF_PORT/api/status")" = 200 ] && ok "hf-fetch /api/status" || bad "hf-fetch /api/status"
curl -fs "http://127.0.0.1:$AIBOX_SWAP_PORT/v1/models" >/dev/null && ok "llama-swap answers on $AIBOX_SWAP_PORT" || bad "llama-swap not answering"
sudo -u aibox /opt/aibox/venv/bin/python /opt/aibox/llama-swap/gen_models.py --quiet >/dev/null && ok "model scan runs" || bad "model scan fails"
sudo -u aibox sudo -n /usr/local/lib/aibox/aibox-priv integrity >/dev/null && ok "aibox-priv works for the aibox account" || bad "aibox-priv denied"
sudo -u aibox sudo -n /bin/true 2>/dev/null && bad "aibox can run arbitrary commands as root" || ok "aibox cannot sudo anything else"
sudo -u aibox sudo -n /usr/local/lib/aibox/aibox-priv sysfs 1 /etc/passwd 2>/dev/null && bad "aibox-priv wrote outside sysfs" || ok "aibox-priv refuses a non-sysfs path"
echo; [ "$F" = 0 ] && echo "all checks passed" || echo "$F check(s) failed"
exit "$F"
