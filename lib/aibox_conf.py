"""Shared settings and access control for the aibox services (modelbench, boxagent, hf-fetch).

Settings come from /etc/aibox/aibox.env (KEY=VALUE lines, written by install.sh); environment
variables of the same name override the file. Every service reads the same file, so paths, ports
and the access token stay in one place.
"""
import ipaddress
import os
import re
import secrets

CONF_FILE = os.environ.get("AIBOX_CONF", "/etc/aibox/aibox.env")

DEFAULTS = {
    "AIBOX_HOME": "/opt/aibox",                 # code
    "AIBOX_DATA": "/var/lib/aibox",             # state, databases, logs
    "AIBOX_MODELS": "/srv/aibox/models",        # model folders, colon separated; the first one is the download/inbox root
    "AIBOX_INBOX": "",                          # drop-in folder (default: <first model folder>/inbox)
    "AIBOX_LLAMA_SERVER": "/opt/aibox/llama.cpp/llama-server",
    "AIBOX_LLAMA_SWAP": "/opt/aibox/bin/llama-swap",
    "AIBOX_SWAP_CONFIG": "/var/lib/aibox/llama-swap/config.yaml",
    "AIBOX_BIND": "0.0.0.0",                    # address the web UIs listen on
    "AIBOX_SWAP_BIND": "127.0.0.1",             # address llama-swap (the model API) listens on
    "AIBOX_SWAP_PORT": "9010",
    "AIBOX_BENCH_PORT": "9050",
    "AIBOX_BOX_PORT": "9060",
    "AIBOX_HF_PORT": "9070",
    "AIBOX_TERMINAL_PORT": "8765",
    "AIBOX_TOKEN_FILE": "/etc/aibox/token",
    "AIBOX_TRUSTED": "",                        # IPs/CIDRs allowed in without the token (empty = nobody, not even localhost)
    "AIBOX_PUBLIC_HOST": "",                    # host name to put in links; empty = whatever the browser used
    "AIBOX_URL_BENCH": "", "AIBOX_URL_BOX": "", "AIBOX_URL_HF": "", "AIBOX_URL_TERMINAL": "",
    "AIBOX_TERMINAL": "0",                      # terminal.. agent installed (1/0)
    "AIBOX_WEB_SHELL": "0",                     # boxagent's browser shell (ttyd + login) installed (1/0)
    "AIBOX_CODING_HARNESS": "1",                # modelbench coding harness (Docker sandbox) enabled (1/0)
    "AIBOX_SANDBOX_IMAGE": "aibox-sandbox:latest",
    "AIBOX_PROJECTS": "/var/lib/aibox/projects",
    "AIBOX_CTX": "32768",                       # default context for auto-added models
    "AIBOX_MAX_MODEL_GB": "0",                  # skip GGUFs bigger than this when generating the model list (0 = no limit)
    "AIBOX_VRAM_GB": "0",                       # override detected GPU memory (0 = detect)
    "AIBOX_RAM_FLOOR_GB": "1.5",                # kill a test/model if free RAM falls below this
    "AIBOX_MIN_FREE_DISK_GB": "10",             # downloads refuse to go below this
    "AIBOX_HF_TOKEN_FILE": "/etc/aibox/hf_token",
    "AIBOX_SECRETS_FILE": "/etc/aibox/secrets.json",
}


def _parse(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if re.match(r"^[A-Z0-9_]+$", k):
                    out[k] = v
    except OSError:
        pass
    return out


C = dict(DEFAULTS)
C.update(_parse(CONF_FILE))
C.update({k: v for k, v in os.environ.items() if k.startswith("AIBOX_")})


def get(key, default=None):
    v = C.get(key)
    return default if v in (None, "") else v


def num(key, default=0.0):
    try:
        return float(get(key, default))
    except (TypeError, ValueError):
        return float(default)


def flag(key):
    return str(get(key, "0")).strip().lower() in ("1", "yes", "true", "on")


def port(name):
    return int(get("AIBOX_%s_PORT" % name.upper()))


def data(*parts):
    return os.path.join(get("AIBOX_DATA"), *parts)


def model_roots():
    return [r.rstrip("/") for r in str(get("AIBOX_MODELS", "")).split(":") if r.strip()]


def inbox():
    roots = model_roots()
    return get("AIBOX_INBOX") or os.path.join(roots[0] if roots else data("models"), "inbox")


def swap_url():
    host = get("AIBOX_SWAP_BIND")
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return "http://%s:%s" % (host, get("AIBOX_SWAP_PORT"))


# ---------------------------------------------------------------- access
_TOKEN = {"v": None, "m": None}


def token():
    """The shared access token. Read from AIBOX_TOKEN_FILE (re-read when it changes)."""
    path = get("AIBOX_TOKEN_FILE")
    try:
        m = os.path.getmtime(path)
    except OSError:
        m = None
    if _TOKEN["v"] is None or m != _TOKEN["m"]:
        try:
            with open(path, encoding="utf-8") as fh:
                _TOKEN["v"] = fh.read().strip()
        except OSError:
            _TOKEN["v"] = ""
        _TOKEN["m"] = m
    return _TOKEN["v"]


def _nets():
    nets = []
    for part in str(get("AIBOX_TRUSTED", "")).replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                pass
    return nets


def trusted(ip):
    ip = (ip or "").split("%")[0]
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return any(a in n for n in _nets())


COOKIE = "aibox_key"


def presented(headers, query=None):
    """The token a request carries: X-Key header, Bearer token, ?key= or the aibox_key cookie."""
    auth = headers.get("Authorization") or ""
    got = headers.get("X-Key") or (auth[7:].strip() if auth.lower().startswith("bearer ") else "")
    if not got and query:
        got = (query.get("key") or [""])[0]
    if not got:
        m = re.search(r"(?:^|;\s*)%s=([^;]+)" % COOKIE, headers.get("Cookie") or "")
        got = m.group(1) if m else ""
    return got


def allowed(handler, query=None):
    """True when the caller is on the trusted list or presents the token. No token configured = nobody gets in."""
    if trusted(handler.client_address[0]):
        return True
    tok = token()
    got = presented(handler.headers, query)
    return bool(tok) and bool(got) and secrets.compare_digest(got, tok)


def same_origin(handler):
    """Browser requests that change something must come from the app's own page: reject when the browser
    says the request is cross-site/other-origin (other ports on this host count as other origins).
    Clients that send neither header (curl, scripts) pass; they still need the token."""
    site = handler.headers.get("Sec-Fetch-Site")
    if site and site not in ("same-origin", "none"):
        return False
    origin = handler.headers.get("Origin")
    if origin and origin != "null":
        from urllib.parse import urlparse
        if urlparse(origin).netloc.lower() != (handler.headers.get("Host") or "").lower():
            return False
    elif origin == "null":
        return False
    return True


LOGIN_PATH = "/__aibox_login"


def handle_login(handler, app):
    """POST /__aibox_login (form field key): set the cookie and go to the app, so the token never sits in a URL."""
    from urllib.parse import parse_qs
    try:
        n = min(int(handler.headers.get("Content-Length") or 0), 4096)
    except ValueError:
        n = 0
    got = (parse_qs(handler.rfile.read(n).decode("utf-8", "replace")).get("key") or [""])[0].strip()
    tok = token()
    if tok and got and secrets.compare_digest(got, tok) and same_origin(handler):
        handler.send_response(303)
        handler.send_header("Set-Cookie", cookie_header())
        handler.send_header("Location", "/")
    else:
        body = login_page(app, failed=True).encode()
        handler.send_response(401)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return
    handler.send_header("Content-Length", "0")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()


def cookie_header():
    """Set-Cookie value that lets the browser skip ?key= next time. Cookies are shared across ports of one host."""
    return "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000" % (COOKIE, token())


# ---------------------------------------------------------------- links between the services
def link(service, host_header=""):
    """Base URL (no trailing slash) of a sibling service as the browser should see it."""
    explicit = get("AIBOX_URL_%s" % service.upper())
    if explicit:
        return explicit.rstrip("/")
    host = get("AIBOX_PUBLIC_HOST") or (host_header or "").rsplit(":", 1)[0].strip("[]") or "localhost"
    if ":" in host:
        host = "[%s]" % host
    return "http://%s:%s" % (host, port(service))


def enabled(service):
    return {"terminal": flag("AIBOX_TERMINAL"), "shell": flag("AIBOX_WEB_SHELL"),
            "harness": flag("AIBOX_CODING_HARNESS")}.get(service, True)


def login_page(app, failed=False):
    """Small page shown instead of a bare 401: paste the token once, the cookie keeps you in."""
    return ("<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s - sign in</title><style>body{font-family:system-ui,sans-serif;background:#141414;color:#ddd;display:grid;"
            "place-items:center;min-height:90vh;margin:0 16px}form{background:#1e1e1e;border:1px solid #333;border-radius:10px;padding:22px;"
            "max-width:420px;width:100%%}input{width:100%%;box-sizing:border-box;padding:9px;margin:10px 0;border-radius:6px;border:1px solid #444;"
            "background:#111;color:#eee;font:inherit}button{padding:8px 16px;border-radius:6px;border:1px solid #555;background:#2a2a2a;color:#eee;"
            "font:inherit;cursor:pointer}p{color:#999;font-size:13px}.e{color:#e66}</style></head><body><form method=post action='%s'>"
            "<b>%s</b>%s<p>Enter the access token. On the box it is in <code>%s</code> (or run <code>sudo aibox token</code>).</p>"
            "<input name=key type=password autocomplete=current-password autofocus placeholder='access token'><button>Open</button></form>"
            "</body></html>") % (app, LOGIN_PATH, app, "<p class=e>That token is not right.</p>" if failed else "", get("AIBOX_TOKEN_FILE"))


def ram_pool_gb(total_gb):
    """Without a GPU, models live in system RAM: leave max(2 GB, 20 %) for everything else."""
    return round(max(1.0, total_gb - max(2.0, total_gb * 0.2)), 1)


def model_budget_gb():
    """Rough memory a model (weights + some context) may use on this box, in GB.
    AIBOX_MAX_MODEL_GB wins; otherwise AMD VRAM/GTT from sysfs, NVIDIA VRAM from nvidia-smi, else system RAM."""
    import glob
    import shutil
    import subprocess
    fixed = num("AIBOX_MAX_MODEL_GB", 0)
    if fixed > 0:
        return fixed
    GB = 1024 ** 3

    def rd(p):
        try:
            with open(p) as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            return 0
    ram = 0
    try:
        with open("/proc/meminfo") as fh:
            for ln in fh:
                if ln.startswith("MemTotal:"):
                    ram = int(ln.split()[1]) * 1024
    except OSError:
        pass
    for d in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
        vt, gt = rd(d + "/mem_info_vram_total"), rd(d + "/mem_info_gtt_total")
        if vt:
            if vt < 8 * GB and gt > 16 * GB:          # APU with a small carve-out: models live in GTT
                return round(min(gt, ram - 6 * GB) / GB - 2, 1)
            return round(vt / GB - 2, 1)
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            out = subprocess.run([smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=10).stdout
            mib = sum(float(x) for x in out.split() if x.replace(".", "", 1).isdigit())
            if mib:
                return round(mib / 1024 - 1.5, 1)
        except Exception:
            pass
    return ram_pool_gb(ram / GB)
