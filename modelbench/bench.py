#!/usr/bin/env python3
"""modelbench - see, test and talk to local GGUF models on an aibox without wrecking the box.

 * Running now: every llama-server on the box (router, children, single, bench) with its model,
   port, ctx, per-process VRAM / shared / RAM, uptime and health. Stop any of them from the page.
 * Test: one model at a time on a bench port. Preflight refuses anything that won't fit in
   GPU memory; watchdog kills on low RAM or shared-memory spill; teardown checks the
   GPU memory came back.
 * Load: keep a model up (same guards) to talk to it in the playground.
 * Playground (/play): chat with any running server, streamed, via this box - works for
   127.0.0.1-only servers too.
CLI:  python bench.py list | run <name> [--ctx N] [--allow-concurrent] | serve
"""
import ctypes, hashlib, http.client, json, os, re, secrets, sqlite3, struct, subprocess, sys, threading, time
import urllib.request, urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
import aibox_conf as AC
DATA = AC.data("modelbench")
os.makedirs(DATA, exist_ok=True)
locklib = None   # model locks are not part of the kit
GB = 1024 ** 3
# Optional: an aissh6-compatible command daemon (POST /command with a bearer token) that runs PowerShell on a
# Windows PC. When set, classic chat gets that PC as a workspace + terminal. Off by default.
REMOTE = bool(AC.get("AIBOX_REMOTE_SHELL_URL"))

_TE = {"key": None, "val": {}}
TE_EXCLUDE = set()


def task_employed():
    """Task Employed: worker models the automated services depend on - model-lock
    locked models plus members of persistent llama-swap groups. Shown pinned at the top, tagged, kept apart
    from the other models. Returns {swap_id: gguf path}; cached on config.yaml/locks.json mtimes."""
    if not locklib:
        return {}
    try:
        key = (os.path.getmtime(locklib.CFG), os.path.getmtime(locklib.LOCKS))
    except OSError:
        key = None
    if key is not None and key == _TE["key"]:
        return _TE["val"]
    try:
        ids = set(locklib.locked_ids())
        for g in locklib.groups().values():
            if g.get("persistent"):
                ids.update(m for m in (g.get("members") or []) if m not in TE_EXCLUDE)
        cfg = locklib.cfg_models()
        val = {i: (cfg.get(i) or {}).get("path") for i in ids if i in cfg}
    except Exception:
        return _TE["val"]
    _TE.update(key=key, val=val)
    return val


def te_status():
    """Task Employed rows for the top of Running now. These run as llama-server-r (hidden from the bench's
    process scans, kill-all and earlyoom on purpose), so they are read from llama-swap and shown read-only."""
    te = task_employed()
    if not te:
        return []
    try:
        run = locklib.running()
    except Exception:
        run = {}
    lk = locklib.locked_ids()
    out = []
    for i in te:
        r = run.get(i) or {}
        port = r.get("port")
        ram = 0.0
        try:
            pid = locklib.pid_on_port(port) if port else None
            if pid:
                with open("/proc/%d/status" % pid) as fh:
                    for ln in fh:
                        if ln.startswith("VmRSS:"):
                            ram = round(int(ln.split()[1]) / 1048576, 2)
        except Exception:
            pass
        out.append({"id": i, "file": os.path.basename(te[i] or ""), "state": r.get("state") or "not loaded",
                    "port": port, "locked": i in lk, "ram_gb": ram})
    return sorted(out, key=lambda t: (0 if t["locked"] else 1, t["id"]))


def _te_paths():
    out = set()
    for p in task_employed().values():
        if p:
            out.add(os.path.normcase(p))
            out.add(os.path.normcase(os.path.realpath(p)))
    return out
LINUX = sys.platform.startswith("linux")
NOWIN = 0 if LINUX else 0x08000000
NEWGRP = 0 if LINUX else 0x00000200
_POPEN_EXTRA = {"start_new_session": True} if LINUX else {}
_BP = [int(x) for x in str(AC.get("AIBOX_BENCH_LLAMA_PORTS", "9030,9031,9032,9033")).split(",") if x.strip().isdigit()]
CFG = {
    "roots": AC.model_roots(),
    "exe": AC.get("AIBOX_LLAMA_SERVER"),
    "exe_rules": [],
    "device": AC.get("AIBOX_LLAMA_DEVICE", ""),   # e.g. Vulkan0 / CUDA0; empty = let llama.cpp pick
    "bench_port": _BP[0],
    "bench_ports": _BP,
    "max_loaded": int(AC.num("AIBOX_BENCH_MAX_LOADED", 3)),
    "chat_reasoning": "auto",
    "auto_ctx_max": 65536,
    "chat_thinking": False,     # default for /api/chat when the client does not say; the playground Think control overrides per chat     # cap for automatic context on LOAD (a number in the bench ctx box can go higher)   # thinking for playground loads: on/off/auto (bench runs are always off)
    "remote_coordinators": {
        "claude-haiku": {"label": "Claude Haiku 4.5 (Anthropic, fast, cheap)", "kind": "anthropic", "url": "https://api.anthropic.com/v1/messages", "model": "claude-haiku-4-5-20251001", "env": "ANTHROPIC_API_KEY"},
        "claude-sonnet": {"label": "Claude Sonnet 5.5 (Anthropic, top class)", "kind": "anthropic", "url": "https://api.anthropic.com/v1/messages", "model": "claude-sonnet-5-5", "env": "ANTHROPIC_API_KEY", "thinking": {"type": "between_tools"}}},
    "secrets_file": AC.get("AIBOX_SECRETS_FILE"),
    "coopmat_off": [],          # name fragments of models that crash with Vulkan coopmat; empty = on for all
    "tool_desc_max": 200,       # trim tool descriptions injected into every request
    "ui_host": AC.get("AIBOX_BIND"),
    "ui_port": AC.port("bench"),
    "ctx": 16384,
    "kv_type": "q8_0",
    "vram_gb": AC.num("AIBOX_VRAM_GB", 0),
    "vram_margin_gb": 1.5,
    "buffers_gb": 1.5,
    "ram_floor_gb": AC.num("AIBOX_RAM_FLOOR_GB", 1.5),
    "shared_limit_gb": 2.0,
    "load_timeout": 600,
    "gen_tokens": 256,
    "snapshot_seconds": 3,
    "mcp_config": os.path.join(DATA, "mcp.bench.json"),
    "aissh6_url": AC.get("AIBOX_REMOTE_SHELL_URL", ""),
    "agent_max_steps": 20,
    "tool_result_max": 6000,
    "tool_max_chars": 6000,
}
_cp = os.path.join(DATA, "config.json")   # optional local overrides
if os.path.exists(_cp):
    with open(_cp, encoding="utf-8") as f:
        CFG.update(json.load(f))

def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    with open(os.path.join(DATA, "bench.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")

class _MEM(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

def host_mem():
    m = _MEM(); m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return {"avail_gb": m.ullAvailPhys / GB, "total_gb": m.ullTotalPhys / GB,
            "commit_gb": (m.ullTotalPageFile - m.ullAvailPageFile) / GB}

def ps(cmd, timeout=30):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, text=True, timeout=timeout, creationflags=NOWIN)
    return r.stdout.strip()

def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0

def gpu_dedicated_used():
    return _num(ps(r"((Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage').CounterSamples | "
                   r"Measure-Object CookedValue -Maximum).Maximum")) / GB

_VRAM = None
def vram_total():
    global _VRAM
    if CFG["vram_gb"]:
        return float(CFG["vram_gb"])
    if _VRAM is None:
        v = _num(ps(r"(Get-ItemProperty 'HKLM:\SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0*' "
                    r"-EA SilentlyContinue | ? { $_.'HardwareInformation.qwMemorySize' } | "
                    r"% { [double]$_.'HardwareInformation.qwMemorySize' } | Measure -Maximum).Maximum")) / GB
        _VRAM = v or 48.0
    return _VRAM

def llama_pids_ports():
    out = ps("Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | % { ($_.CommandLine -replace '.*--port\\s+(\\d+).*','$1') }")
    return [int(x) for x in out.split() if x.isdigit()]

def llama_pids():
    out = ps("Get-Process llama-server -EA SilentlyContinue | % Id")
    return [int(x) for x in out.split() if x.isdigit()]

class _NoFile:
    def close(self):
        pass


class _PidProc:
    """Stand-in for a Popen this process did not start (a server re-adopted after a restart)."""
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(self.pid))
        if not h:
            return 1
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return None if ok and code.value == 259 else (code.value or 1)


def kill_tree(pid):
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, creationflags=NOWIN)

def kill_all_llama():
    subprocess.run(["taskkill", "/T", "/F", "/IM", "llama-server.exe"], capture_output=True, creationflags=NOWIN)


if LINUX:
    import glob, signal
    def _drm():
        for d in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
            if os.path.exists(d + "/mem_info_vram_total"):
                return d
        return None
    def _rd(path):
        try:
            with open(path) as f:
                return int(f.read().strip())
        except Exception:
            return 0
    def host_mem():
        mi = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                k, _, v = ln.partition(":")
                try:
                    mi[k] = int(v.split()[0]) * 1024
                except Exception:
                    pass
        return {"avail_gb": mi.get("MemAvailable", 0) / GB, "total_gb": mi.get("MemTotal", 0) / GB,
                "commit_gb": mi.get("Committed_AS", 0) / GB}
    import shutil as _shutil
    _NVSMI = _shutil.which("nvidia-smi")
    _NVC = {"t": 0.0, "tot": 0, "used": 0, "pids": {}}
    def _nv():
        """NVIDIA totals and per-process memory from nvidia-smi, cached 1 s."""
        if not _NVSMI or time.time() - _NVC["t"] < 1:
            return _NVC
        try:
            o = subprocess.run([_NVSMI, "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=10).stdout
            tot = used = 0
            for ln in o.splitlines():
                a, _, b = ln.partition(",")
                tot += float(a.strip() or 0) * 1048576; used += float(b.strip() or 0) * 1048576
            o = subprocess.run([_NVSMI, "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=10).stdout
            pids = {}
            for ln in o.splitlines():
                a, _, b = ln.partition(",")
                if a.strip().isdigit():
                    pids[int(a)] = pids.get(int(a), 0) + float(b.strip() or 0) * 1048576
            _NVC.update(t=time.time(), tot=int(tot), used=int(used), pids=pids)
        except Exception:
            _NVC["t"] = time.time()
        return _NVC
    def ps(cmd, timeout=30):
        """PowerShell runs on the PC the optional remote command daemon is on (AIBOX_REMOTE_SHELL_URL)."""
        if not REMOTE:
            return ""
        req = urllib.request.Request(_MCPC["aissh6_url"].rstrip("/") + "/command",
                                     data=json.dumps({"command": "& {\n" + cmd + "\n} | Out-String -Width 220",
                                                      "session_id": "modelbench"}).encode(),
                                     method="POST", headers={"Content-Type": "application/json",
                                                             "Authorization": "Bearer " + _MCPC["bearer"]})
        try:
            with urllib.request.urlopen(req, timeout=timeout + 30) as r:
                d = json.loads(r.read())
        except Exception:
            return ""
        return ((d.get("stdout") or d.get("output") if isinstance(d, dict) else str(d)) or "").strip()
    def pool_layout():
        """Small BIOS carve-out + big GTT (strixhaloguide layout): models live in GTT, so GTT is the GPU pool, not 'shared spill'."""
        d = _drm()
        return bool(d) and _rd(d + "/mem_info_vram_total") < 8 * GB and _rd(d + "/mem_info_gtt_total") > 16 * GB
    def gpu_dedicated_used():
        d = _drm()
        if not d:
            return _nv()["used"] / GB if _NVSMI else 0.0
        used = _rd(d + "/mem_info_vram_used")
        if pool_layout():
            used += _rd(d + "/mem_info_gtt_used")
        return used / GB
    def vram_total():
        if CFG["vram_gb"]:
            return float(CFG["vram_gb"])
        d = _drm()
        if d and pool_layout():
            # GPU pool = carve-out + GTT, but GTT is system RAM: leave 6 GB for the OS
            return _rd(d + "/mem_info_vram_total") / GB + min(_rd(d + "/mem_info_gtt_total") / GB, host_mem()["total_gb"] - 6)
        if d:
            return _rd(d + "/mem_info_vram_total") / GB
        if _NVSMI and _nv()["tot"]:
            return _nv()["tot"] / GB
        return AC.ram_pool_gb(host_mem()["total_gb"])   # no GPU: models run from system RAM
    def _llama_procs():
        out = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/comm" % pid) as f:
                    if f.read().strip() != "llama-server":
                        continue
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
                out.append((int(pid), argv))
            except Exception:
                continue
        return out
    def _cmdline(argv):
        return " ".join('"%s"' % a if " " in a else a for a in argv)
    def llama_pids_ports():
        r = []
        for pid, argv in _llama_procs():
            if "--port" in argv:
                i = argv.index("--port")
                if i + 1 < len(argv) and argv[i + 1].isdigit():
                    r.append(int(argv[i + 1]))
        return r
    def llama_pids():
        return [p for p, _ in _llama_procs()]
    def _poll_linux(self):
        try:
            with open("/proc/%d/stat" % int(self.pid)) as f:
                st = f.read().rsplit(")", 1)[1].split()[0]
        except Exception:
            return 1
        return 1 if st in ("Z", "X") else None
    _PidProc.poll = _poll_linux
    def _children(pid):
        kids = {}
        for p in os.listdir("/proc"):
            if p.isdigit():
                try:
                    with open("/proc/%s/stat" % p) as f:
                        pp = int(f.read().rsplit(")", 1)[1].split()[1])
                    kids.setdefault(pp, []).append(int(p))
                except Exception:
                    pass
        out, todo = [], [pid]
        while todo:
            c = kids.get(todo.pop(), [])
            out += c
            todo += c
        return out
    def kill_tree(pid):
        pids = [int(pid)] + _children(int(pid))
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for p in reversed(pids):
                try:
                    os.kill(p, sig)
                except OSError:
                    pass
            if sig == signal.SIGTERM:
                time.sleep(0.5)
    def _is_infra(pid):
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                return b"--reranking" in f.read()
        except OSError:
            return False
    def kill_all_llama():
        _lp = locklib.locked_pids() if locklib else {}
        for p in llama_pids():
            if not _is_infra(p) and p not in _lp:
                kill_tree(p)
    def _gpu_mem_by_pid(pid):
        if not _drm():
            if _NVSMI:
                return int(_nv()["pids"].get(int(pid), 0)), 0
            try:   # CPU only: the model lives in RAM, so count its resident memory as "model memory"
                with open("/proc/%d/status" % pid) as f:
                    for ln in f:
                        if ln.startswith("VmRSS"):
                            return int(ln.split()[1]) * 1024, 0
            except OSError:
                pass
            return 0, 0
        ded = shared = 0
        seen = set()
        fd = "/proc/%d/fdinfo" % pid
        try:
            names = os.listdir(fd)
        except OSError:
            return 0, 0
        for n in names:
            try:
                with open(fd + "/" + n) as f:
                    t = f.read()
            except OSError:
                continue
            if "drm-driver" not in t:
                continue
            m = re.search(r"drm-client-id:\s*(\d+)", t)
            if m:
                if m.group(1) in seen:
                    continue
                seen.add(m.group(1))
            v = re.search(r"drm-memory-vram:\s*(\d+)\s*KiB", t)
            g = re.search(r"drm-memory-gtt:\s*(\d+)\s*KiB", t)
            ded += int(v.group(1)) * 1024 if v else 0
            shared += int(g.group(1)) * 1024 if g else 0
        if pool_layout():
            ded, shared = ded + shared, 0      # everything the model holds is GPU memory; nothing is "spilled"
        return ded, shared
    def snap_data():
        btime = 0
        with open("/proc/stat") as f:
            for ln in f:
                if ln.startswith("btime"):
                    btime = int(ln.split()[1])
        hz = os.sysconf("SC_CLK_TCK")
        procs, gpu = [], {}
        for pid, argv in _llama_procs():
            try:
                with open("/proc/%d/stat" % pid) as f:
                    fields = f.read().rsplit(")", 1)[1].split()
                ppid = int(fields[1]); start = btime + int(fields[19]) / hz
                rss = 0
                with open("/proc/%d/status" % pid) as f:
                    for ln in f:
                        if ln.startswith("VmRSS"):
                            rss = int(ln.split()[1]) * 1024
            except Exception:
                continue
            d, h = _gpu_mem_by_pid(pid)
            gpu[str(pid)] = {"d": d, "s": h}
            procs.append({"pid": pid, "ppid": ppid, "cmd": _cmdline(argv), "ws": rss,
                          "start": datetime.utcfromtimestamp(start).strftime("%Y-%m-%dT%H:%M:%SZ")})
        dd = _drm()
        return {"procs": procs, "gpu": gpu, "adapter": float(_rd(dd + "/mem_info_vram_used") + (_rd(dd + "/mem_info_gtt_used") if pool_layout() else 0)) if dd else
                (float(_nv()["used"]) if _NVSMI else float(sum(g["d"] for g in gpu.values())))}
    def _watch_linux(self, pid, res):
        """Samples this server's GPU memory (amdgpu fdinfo / nvidia-smi) and host RAM; kills it on breach."""
        breach = {"why": None, "done": False}
        def gpu_reader():
            while not breach["done"]:
                d, h = _gpu_mem_by_pid(pid)
                d, h = d / GB, h / GB
                res["peak_ded_gb"] = round(max(res["peak_ded_gb"], d), 2)
                res["peak_shared_gb"] = round(max(res["peak_shared_gb"], h), 2)
                if h > CFG["shared_limit_gb"] and not breach["why"]:
                    breach["why"] = f"killed: spilled {h:.1f} GB into shared/system memory (limit {CFG['shared_limit_gb']} GB)"
                    kill_tree(pid)
                time.sleep(1)
        def ram_guard():
            while not breach["done"]:
                a = host_mem()["avail_gb"]
                res["min_avail_ram_gb"] = round(a if res["min_avail_ram_gb"] is None else min(res["min_avail_ram_gb"], a), 2)
                if a < CFG["ram_floor_gb"] and not breach["why"]:
                    breach["why"] = f"killed: available RAM fell to {a:.1f} GB (floor {CFG['ram_floor_gb']} GB)"
                    kill_tree(pid)
                time.sleep(0.5)
        threading.Thread(target=gpu_reader, daemon=True).start()
        threading.Thread(target=ram_guard, daemon=True).start()
        return breach

# ------------------------------------------------------------------ what's running
SNAP_PS = r"""
$procs = @(Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" | % { $gp = Get-Process -Id $_.ProcessId -EA SilentlyContinue
  [pscustomobject]@{ pid=[int]$_.ProcessId; ppid=[int]$_.ParentProcessId; cmd=[string]$_.CommandLine;
    start=$(if($_.CreationDate){ $_.CreationDate.ToUniversalTime().ToString('o') } else { '' }); ws=[double]$(if($gp){ $gp.WorkingSet64 } else { 0 }) } })
$gpu = @{}; $adapter = 0.0
try { $s = (Get-Counter '\GPU Process Memory(*)\Dedicated Usage','\GPU Process Memory(*)\Shared Usage','\GPU Adapter Memory(*)\Dedicated Usage' -EA Stop).CounterSamples } catch { $s = @() }
foreach($x in $s){ if($x.Path -like '*gpu adapter memory*'){ if($x.CookedValue -gt $adapter){ $adapter = $x.CookedValue }; continue }
  if($x.InstanceName -match 'pid_(\d+)_'){ $k = $matches[1]; if(-not $gpu.ContainsKey($k)){ $gpu[$k] = @{ d = 0.0; s = 0.0 } }
    if($x.Path -like '*dedicated usage'){ $gpu[$k].d += $x.CookedValue } else { $gpu[$k].s += $x.CookedValue } } }
@{ procs = $procs; gpu = $gpu; adapter = $adapter } | ConvertTo-Json -Depth 5 -Compress
"""
SNAP = {"ts": 0.0, "servers": [], "adapter_gb": 0.0, "error": None}

def parse_cmd(cmd):
    toks = [a or b for a, b in re.findall(r'"([^"]*)"|(\S+)', cmd or "")]
    opts, i = {}, 1
    while i < len(toks):
        t = toks[i]
        if t.startswith("-") and not re.match(r"^-\d", t):
            nxt = toks[i + 1] if i + 1 < len(toks) and (not toks[i + 1].startswith("-") or re.match(r"^-\d", toks[i + 1])) else None
            opts[t] = nxt if nxt is not None else True
            i += 2 if nxt is not None else 1
        else:
            i += 1
    return (toks[0] if toks else ""), opts

def _opt(o, *names):
    for n in names:
        if n in o:
            return o[n]
    return None

def _probe(port):
    out = {"health": "down", "models": []}
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            out["health"] = "ok" if r.status == 200 else str(r.status)
    except urllib.error.HTTPError as e:
        out["health"] = "loading" if e.code == 503 else f"http {e.code}"
    except Exception:
        return out
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3) as r:
            for d in json.loads(r.read()).get("data", []):
                out["models"].append({"id": d.get("id"), "status": (d.get("status") or {}).get("value", "loaded")})
    except Exception:
        pass
    return out

def model_card_id(path):
    if not isinstance(path, str):
        return None
    p = path if LINUX else path.replace("/", "\\")
    if not p.lower().endswith(".gguf"):
        return None
    return hashlib.sha1(p.lower().encode()).hexdigest()[:10]

def take_snapshot(bench_pid=None):
    if LINUX:
        j = snap_data()
    else:
        raw = ps(SNAP_PS, timeout=40)
        j = json.loads(raw) if raw else {}
    procs = j.get("procs") or []
    if isinstance(procs, dict):
        procs = [procs]
    gpu = j.get("gpu") or {}
    pids = {p["pid"] for p in procs}
    servers = []
    for p in procs:
        exe, o = parse_cmd(p.get("cmd"))
        g = gpu.get(str(p["pid"])) or {}
        mp = _opt(o, "-m", "--model")
        if "--reranking" in (p.get("cmd") or "") or (isinstance(mp, str) and "rerank" in mp.lower()):
            continue   # infrastructure (llama-swap keeps the reranker loaded); not a model you can stop or chat with
        port = _opt(o, "--port")
        if _opt(o, "--models-preset", "--models-dir"):
            kind = "router"
        elif p["pid"] == bench_pid:
            kind = "bench"
        elif p["ppid"] in pids:
            kind = "router-child"
        else:
            kind = "server"
        up = None
        if p.get("start"):
            try:
                up = time.time() - datetime.fromisoformat(p["start"].replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        s = {"pid": p["pid"], "ppid": p["ppid"], "kind": kind, "exe": exe,
             "build": os.path.basename(os.path.dirname(exe)) or "?",
             "model": (os.path.basename(mp)[:-5] if isinstance(mp, str) and mp.lower().endswith(".gguf") else (_opt(o, "--alias") or ("router" if kind == "router" else "?"))),
             "card": model_card_id(mp),
             "alias": _opt(o, "--alias"), "port": int(port) if isinstance(port, str) and port.isdigit() else 8080,
             "host": _opt(o, "--host") or "127.0.0.1", "ctx": _opt(o, "-c", "--ctx-size"),
             "ded_gb": round(_num(g.get("d")) / GB, 2), "shared_gb": round(_num(g.get("s")) / GB, 2),
             "ram_gb": round(_num(p.get("ws")) / GB, 2), "uptime_s": int(up) if up is not None else None,
             "health": None, "models": []}
        if kind != "router-child":
            s.update(_probe(s["port"]))
        servers.append(s)
    for s in servers:     # a router's VRAM lives in its children - roll it up
        if s["kind"] == "router":
            kids = [c for c in servers if c["ppid"] == s["pid"]]
            s["ded_gb"] = round(s["ded_gb"] + sum(c["ded_gb"] for c in kids), 2)
            s["shared_gb"] = round(s["shared_gb"] + sum(c["shared_gb"] for c in kids), 2)
            s["ram_gb"] = round(s["ram_gb"] + sum(c["ram_gb"] for c in kids), 2)
    try:
        _run = locklib.running() if locklib else {}
        _lk = locklib.locked_ids() if locklib else set()
        _te = task_employed()
        _byport = {v["port"]: k for k, v in _run.items()}
        for s_ in servers:
            s_["swap_id"] = _byport.get(str(s_["port"]))
            s_["locked"] = bool(s_["swap_id"] and s_["swap_id"] in _lk)
            s_["task_employed"] = bool(s_["swap_id"] and s_["swap_id"] in _te)
    except Exception:
        pass
    SNAP.update(ts=time.time(), servers=servers, adapter_gb=round(_num(j.get("adapter")) / GB, 2), error=None)

# --- live usage sparklines (9 Oct 2026): tokens/s per running llama-server, sampled every second ---
SPARK = {}           # port -> {"tps": [...], "busy": [...], "last": {slot: (task, n_decoded)}, "t": ts}
SPARK_N = 90         # samples kept (1 s apart)

_TESP = {"t": 0.0, "v": set()}


def _te_spark_ports():
    """ports of the running Task Employed models (from llama-swap), cached 5 s, for the activity sparklines"""
    if time.time() - _TESP["t"] < 5:
        return _TESP["v"]
    try:
        te = task_employed()
        v = set()
        if te and locklib:
            for i, r in (locklib.running() or {}).items():
                p = str((r or {}).get("port") or "")
                if i in te and p.isdigit():
                    v.add(int(p))
        _TESP.update(t=time.time(), v=v)
    except Exception:
        _TESP["t"] = time.time()
    return _TESP["v"]


def _spark_loop():
    while True:
        t0 = time.time()
        try:
            ports = {s["port"] for s in SNAP.get("servers", []) if s.get("kind") not in ("router", "router-child")}
            ports |= _te_spark_ports()   # Task Employed workers run under llama-swap, hidden from the process scan (6 Nov 2026)
            for p in list(SPARK):
                if p not in ports:
                    SPARK.pop(p, None)
            for p in ports:
                st = SPARK.setdefault(p, {"tps": [], "busy": [], "last": {}, "t": t0})
                dt = max(0.2, t0 - st["t"]); st["t"] = t0
                tok, busy = 0, 0
                try:
                    with urllib.request.urlopen("http://127.0.0.1:%d/slots" % p, timeout=0.8) as r:
                        slots = json.loads(r.read())
                    for sl in slots if isinstance(slots, list) else []:
                        nt = sl.get("next_token")
                        nt = nt[0] if isinstance(nt, list) and nt else (nt if isinstance(nt, dict) else {})
                        nd, task, sid = int(nt.get("n_decoded") or 0), sl.get("id_task"), sl.get("id")
                        if sl.get("is_processing"):
                            busy = 1
                            if sid in st["last"]:      # first sight of a slot: no baseline yet, don't count its history
                                pt, pn = st["last"][sid]
                                tok += max(0, nd - pn) if pt == task else nd
                        st["last"][sid] = (task, nd)
                except Exception:
                    pass
                st["tps"].append(round(tok / dt, 1)); st["busy"].append(busy)
                del st["tps"][:-SPARK_N], st["busy"][:-SPARK_N]
        except Exception:
            pass
        time.sleep(max(0.1, 1.0 - (time.time() - t0)))


def monitor():
    while True:
        try:
            take_snapshot(BENCH.proc.pid if BENCH.proc else None)
        except Exception as e:
            SNAP["error"] = str(e)
        try:
            BENCH.reap()
        except Exception as e:
            log("reap failed: %s" % e)
        time.sleep(CFG["snapshot_seconds"])
# ------------------------------------------------------------------ GGUF header
_SC = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}

def gguf_meta(path):
    out = {}
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            return out
        f.read(4)
        _, nkv = struct.unpack("<QQ", f.read(16))
        def rstr():
            n, = struct.unpack("<Q", f.read(8)); return f.read(n).decode("utf-8", "replace")
        def rval(t, keep):
            if t in _SC:
                fmt = _SC[t]; return struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]
            if t == 8:
                return rstr()
            if t == 9:
                it, = struct.unpack("<I", f.read(4)); n, = struct.unpack("<Q", f.read(8))
                if it in _SC:
                    sz = struct.calcsize(_SC[it])
                    if not keep:
                        f.seek(sz * n, 1); return None
                    data = f.read(sz * n)
                    return [struct.unpack_from(_SC[it], data, i * sz)[0] for i in range(n)]
                if it == 8:
                    for _ in range(n):
                        L, = struct.unpack("<Q", f.read(8)); f.seek(L, 1)
                    return None
                for _ in range(n):
                    rval(it, False)
                return None
            raise ValueError(f"gguf type {t}")
        for _ in range(nkv):
            k = rstr(); t, = struct.unpack("<I", f.read(4))
            keep = not k.startswith("tokenizer.")
            v = rval(t, keep)
            if keep:
                out[k] = v
    return out

def kv_gb(meta, ctx, kvtype):
    a = meta.get("general.architecture", "")
    g = lambda k, d=None: meta.get(f"{a}.{k}", d)
    L = g("block_count", 0) or 0
    hc = g("attention.head_count", 0) or 0
    if isinstance(hc, list):
        hc = max(hc) if hc else 0
    hkv = g("attention.head_count_kv", hc)
    emb = g("embedding_length", 0) or 0
    kl = g("attention.key_length") or (emb // hc if hc else 128)
    vl = g("attention.value_length") or kl
    if isinstance(hkv, list):
        heads = sum(h for h in hkv if h)
    else:
        n_attn = L
        fai = g("full_attention_interval")
        if fai:
            n_attn = max(1, L // int(fai))
        heads = (hkv or 0) * n_attn
    bpe = {"f16": 2.0, "q8_0": 1.0625, "q4_0": 0.5625}.get(kvtype, 2.0)
    return ctx * heads * (kl + vl) * bpe / GB

# ------------------------------------------------------------------ storage
DB = os.path.join(DATA, "bench.db")
_dbl = threading.Lock()

def db():
    c = sqlite3.connect(DB, timeout=10)
    c.execute("create table if not exists meta(path text primary key, mtime real, json text)")
    c.execute("create table if not exists results(id integer primary key, path text, ts real, json text)")
    c.execute("create table if not exists loads(path text primary key, ts real)")
    return c

def mtp_sidecar(path):
    """mtp-*.gguf beside the model whose stem prefixes the model filename (else the only one there)."""
    d, name = os.path.dirname(path), os.path.basename(path).lower()
    if name.startswith("mtp-"):
        return None
    c = sorted(f for f in os.listdir(d) if f.lower().startswith("mtp-") and f.lower().endswith(".gguf"))
    hit = [f for f in c if name.startswith(f[4:-5].lower())]
    return os.path.join(d, (hit or c)[0]) if (hit or len(c) == 1) else None

def cached_meta(path, mtime):
    with _dbl:
        c = db(); row = c.execute("select mtime, json from meta where path=?", (path,)).fetchone(); c.close()
    if row and row[0] == mtime:
        return json.loads(row[1])
    try:
        m = gguf_meta(path)
    except Exception as e:
        m = {"_error": str(e)}
    keep = {k: v for k, v in m.items() if not (isinstance(v, list) and len(v) > 512)}
    with _dbl:
        c = db(); c.execute("insert or replace into meta values(?,?,?)", (path, mtime, json.dumps(keep))); c.commit(); c.close()
    return keep

def save_result(res):
    with _dbl:
        c = db(); cur = c.execute("insert into results(path, ts, json) values(?,?,?)", (res["path"], res["ts"], json.dumps(res)))
        c.commit(); rid = cur.lastrowid; c.close()
    return rid

def mark_loaded(path):
    with _dbl:
        c = db(); c.execute("insert or replace into loads(path, ts) values(?,?)", (path, time.time())); c.commit(); c.close()


def ever_loaded():
    with _dbl:
        c = db(); rows = {r[0] for r in c.execute("select path from loads").fetchall()}; c.close()
    return rows


def last_results():
    with _dbl:
        c = db()
        rows = c.execute("select r.id, r.path, r.json from results r join (select path, max(id) mid from results group by path) m "
                         "on r.id = m.mid").fetchall()
        c.close()
    return {p: dict(json.loads(j), id=i) for i, p, j in rows}

def get_result(rid):
    with _dbl:
        c = db(); row = c.execute("select json from results where id=?", (rid,)).fetchone(); c.close()
    return json.loads(row[0]) if row else None

# ------------------------------------------------------------------ models
_SHARD = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)

_DL_EXES = ("aria2c", "curl", "wget", "hf", "huggingface-cli")
_DL_CACHE = [0.0, {}]

def dl_live():
    """.gguf files currently open by a download process (cached for 2 s)."""
    if time.time() - _DL_CACHE[0] < 2:
        return _DL_CACHE[1]
    live = {}
    try:
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % d, "rb") as fh:
                    first = fh.read().split(b"\0")[0].decode(errors="replace")
                if os.path.basename(first) not in _DL_EXES:
                    continue
                for fd in os.listdir("/proc/%s/fd" % d):
                    try:
                        t = os.readlink("/proc/%s/fd/%s" % (d, fd))
                    except OSError:
                        continue
                    if t.lower().endswith(".gguf"):
                        live[t] = "downloading"
            except OSError:
                continue
    except Exception:
        pass
    _DL_CACHE[0], _DL_CACHE[1] = time.time(), live
    return live

def dl_state(paths, live):
    """'downloading' if a download process has any of these files open, 'incomplete' if they sit in a .partial folder or
    have an aria2 control file beside them (interrupted download), else None."""
    st = None
    for x in paths:
        if live.get(x):
            return "downloading"
        if (os.sep + ".partial" + os.sep) in x or os.path.exists(x + ".aria2"):
            st = "incomplete"
    return st

_COPY_CACHE = [0.0, {}]

def llama_copies(fresh=False):
    """path -> {pid, port, owner} for every llama-server running right now (cached 2 s)."""
    if not fresh and time.time() - _COPY_CACHE[0] < 2:
        return _COPY_CACHE[1]
    out = {}
    try:
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % d, "rb") as fh:
                    argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
                if not argv or os.path.basename(argv[0]) != "llama-server" or "-m" not in argv:
                    continue
                path = argv[argv.index("-m") + 1]
                port = argv[argv.index("--port") + 1] if "--port" in argv else "?"
                with open("/proc/%s/stat" % d) as fh:
                    ppid = fh.read().rsplit(")", 1)[1].split()[1]
                with open("/proc/%s/cmdline" % ppid, "rb") as fh:
                    parent = fh.read()
                out.setdefault(path, {"pid": int(d), "port": port, "owner": "llama-swap" if b"llama-swap" in parent else "another process"})
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        pass
    _COPY_CACHE[0], _COPY_CACHE[1] = time.time(), out
    return out

def llama_copy_of(path, exclude=(), fresh=False):
    """A llama-server already serving this model file that the bench did not start (llama-swap's, usually)."""
    c = llama_copies(fresh).get(path)
    return c if c and c["pid"] not in exclude else None

def swap_unload_pid(pid):
    """If this llama-server belongs to llama-swap, unload it through llama-swap (clean, no crash) and return its model id."""
    try:
        import urllib.parse
        with open("/proc/%d/stat" % pid) as fh:
            ppid = fh.read().rsplit(")", 1)[1].split()[1]
        with open("/proc/%s/cmdline" % ppid, "rb") as fh:
            if b"llama-swap" not in fh.read():
                return None
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            argv = fh.read().split(b"\0")
        port = argv[argv.index(b"--port") + 1].decode()
        with urllib.request.urlopen(AC.swap_url() + "/running", timeout=5) as r:
            running = json.loads(r.read()).get("running", [])
        mid = next((x["model"] for x in running if str(x.get("proxy", "")).rstrip("/").endswith(":" + port)), None)
        if not mid:
            return None
        req = urllib.request.Request(AC.swap_url() + "/api/models/unload/" + urllib.parse.quote(mid), method="POST")
        urllib.request.urlopen(req, timeout=60).read()
        return mid
    except Exception:
        return None

# --- share with llama-swap: never start a second copy of a model it already holds ---
SWAP_URL = AC.swap_url()
SWAP_CFG = AC.get("AIBOX_SWAP_CONFIG")
_FAM_Q = re.compile(r"(?i)[-_.](?:ud-)?(?:i?q\d+(?:_[a-z0-9]+)*|f16|bf16|f32|fp16|mxfp4(?:_moe)?)(?=[-_.]|$)")
_FAM_DROP = re.compile(r"(?i)^(?:apex|mtp|fixed|gguf|imatrix|i1|a\d+(?:\.\d+)?b|\d+x\d+(?:\.\d+)?b)$")
_SHARE_CACHE = [0.0, None]

def model_family(path):
    """Same base model regardless of quant/packaging: Ornith-1.5-35B-Q4_K_M and Ornith-1.5-35B-A3B-APEX-MTP-fixed -> ornith-1.5-35b."""
    s = os.path.basename(path or "")
    s = re.sub(r"(?i)-\d{5}-of-\d{5}", "", s)
    s = re.sub(r"(?i)\.gguf$", "", s)
    s = _FAM_Q.sub("", s)
    return "-".join(t for t in re.split(r"[-_ ]+", s.lower()) if t and not _FAM_DROP.match(t))

def swap_cfg_models():
    out, cur = {}, None
    try:
        with open(SWAP_CFG, encoding="utf-8") as f:
            for ln in f:
                m = re.match(r'^  "?([^"\s:#][^"\s:]*)"?:\s*$', ln)
                if m:
                    cur = m.group(1); continue
                if cur and ln.strip().startswith("cmd:"):
                    mm = re.search(r"-m\s+(\S+?\.gguf)", ln)
                    if mm:
                        out.setdefault(cur, mm.group(1))
    except OSError:
        pass
    return out

def share_state(fresh=False):
    """Models llama-swap holds right now: [{id, path, family, why}]. Cached 3 s."""
    if not fresh and _SHARE_CACHE[1] is not None and time.time() - _SHARE_CACHE[0] < 3:
        return _SHARE_CACHE[1]
    held, cfg = [], swap_cfg_models()
    try:
        with urllib.request.urlopen(SWAP_URL + "/running", timeout=3) as r:
            for x in json.loads(r.read()).get("running", []):
                mm = re.search(r"-m\s+(\S+?\.gguf)", x.get("cmd") or "")
                pth = mm.group(1) if mm else cfg.get(x.get("model"))
                if pth and "rerank" not in pth.lower():
                    held.append({"id": x.get("model"), "path": pth, "family": model_family(pth), "why": "already loaded in llama-swap"})
    except Exception:
        pass
    _SHARE_CACHE[0], _SHARE_CACHE[1] = time.time(), held
    return held

def shared_copy(path, fresh=False):
    """The llama-swap model this file would duplicate (same file or same base model), else None."""
    fam = model_family(path)
    for h in share_state(fresh):
        if h["path"] == path or (fam and h["family"] == fam):
            return h
    return None

def _swap_warm(mid):
    """Ask llama-swap to have the shared model ready (no-op if it already is)."""
    try:
        req = urllib.request.Request(SWAP_URL + "/v1/chat/completions", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"model": mid, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}).encode())
        urllib.request.urlopen(req, timeout=600).read()
    except Exception:
        pass


def _scan_roots():
    """Configured roots plus every mounted external drive (27 Oct 2026, via ~/llama-swap/extdrives.py)."""
    return list(CFG["roots"])

def scan_models(ctx=None):
    ctx = ctx or CFG["ctx"]
    seen, models = set(), []
    live = dl_live()
    copies = llama_copies()
    try:
        own = {k.get("pid") for k in BENCH.loaded.values()}
    except NameError:
        own = set()
    tep = _te_paths()   # a Task Employed reranker is listed (pinned, tagged) so it can be tracked; others stay hidden
    for root in _scan_roots():
        if not os.path.isdir(root):
            continue
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if d.lower() not in ("blobs", "manifests", ".cache", ".hf-cache")]
            for n in fn:
                if not n.lower().endswith(".gguf") or "mmproj" in n.lower() or ("rerank" in n.lower() and os.path.normcase(os.path.join(dp, n)) not in tep) or n.lower().startswith("mtp-"):   # mtp-* = draft heads, not runnable models
                    continue
                p = os.path.join(dp, n)
                if os.path.islink(p) or not os.path.isfile(p):   # moved to an external drive: the real file is listed from the drive
                    continue
                m = _SHARD.search(n)
                size = os.path.getsize(p)
                if m:
                    if m.group(1) != "00001":
                        continue
                    for i in range(2, int(m.group(2)) + 1):
                        sp = p[:m.start()] + f"-{i:05d}-of-{m.group(2)}.gguf"
                        size += os.path.getsize(sp) if os.path.exists(sp) else 0
                parts = [p] + ([p[:m.start()] + f"-{i:05d}-of-{m.group(2)}.gguf" for i in range(2, int(m.group(2)) + 1)] if m else [])
                dl = dl_state(parts, live)
                added = int(max((os.path.getmtime(x) for x in parts if os.path.exists(x)), default=0))
                key = (n.lower(), size)
                dup = key in seen
                seen.add(key)
                meta = cached_meta(p, os.path.getmtime(p))
                a = meta.get("general.architecture", "?")
                if str(a).lower().startswith(IMAGE_ARCH_PREFIXES):   # diffusion/image weights: llama-server can't run them
                    continue
                kv = kv_gb(meta, ctx, CFG["kv_type"]) if "_error" not in meta else 0.0
                models.append({
                    "id": hashlib.sha1(p.lower().encode()).hexdigest()[:10], "path": p, "name": n[:-5],
                    "folder": dp, "size_gb": round(size / GB, 2), "arch": a,
                    "native_ctx": meta.get(f"{a}.context_length"), "kv_gb": round(kv, 2),
                    "est_gb": round(size / GB + kv + CFG["buffers_gb"], 2), "duplicate": dup,
                    "exe": pick_exe(p), "meta_error": meta.get("_error"), "downloading": dl, "added": added, "elsewhere": (copies.get(p) if copies.get(p) and copies[p]["pid"] not in own else None), "shared": shared_copy(p), "infra": "rerank" in n.lower()})
    return sorted(models, key=lambda m: m["size_gb"])

IMAGE_ARCH_PREFIXES = ("qwen_image", "flux", "sd1", "sd2", "sdxl", "sd3", "wan", "chroma", "lumina", "hidream", "z_image", "hunyuan_image", "stable_diffusion")

def _fix_tool_ids(calls):
    """Mistral-family chat templates raise unless every tool-call id is exactly 9 alphanumeric chars; normalise all ids."""
    for tc in calls:
        if not re.fullmatch(r"[A-Za-z0-9]{9}", str(tc.get("id") or "")):
            tc["id"] = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(9))
    return calls

def pick_exe(path):
    for frag, exe in CFG["exe_rules"]:
        if frag.lower() in path.lower():
            return exe
    return CFG["exe"]
# ------------------------------------------------------------------ tests
FILLER = " ".join(f"Record {i}: the quick brown fox jumps over the lazy dog near gate {i % 17}." for i in range(140))

def _strip(t):
    return re.sub(r"<think>.*?</think>", "", t or "", flags=re.S).strip()

def _json_ok(t):
    s = t.strip().strip("`")
    s = s[s.find("{"): s.rfind("}") + 1] if "{" in s else s
    try:
        d = json.loads(s)
        return str(d.get("city", "")).lower() == "tokyo" and str(d.get("country", "")).lower() == "japan"
    except Exception:
        return False

CHECKS = [
    ("arithmetic", "A bat and a ball cost 1.10 in total. The bat costs 1.00 more than the ball. How much is the ball? Give just the amount.",
     lambda t: bool(re.search(r"\b0?\.05\b|\b5 ?(p|pence|cents?)\b", t.lower()))),
    ("instruction", "Reply with exactly three words, all lowercase, and nothing else.",
     lambda t: len(t.split()) == 3 and t == t.lower()),
    ("json", 'Return only a JSON object with keys "city" and "country" for the capital of Japan. No prose, no code fences.',
     _json_ok),
    ("fact", "What is the chemical symbol for tungsten? Answer with the symbol only.",
     lambda t: t.strip().strip(".").strip() == "W"),
    ("code", "Write a Python function is_prime(n) that returns True if n is prime. Code only.",
     lambda t: "def is_prime" in t and "return" in t),
]

def _post(port, path, body, timeout=600):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def _healthy(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False

# ------------------------------------------------------------------ the bench
class Bench:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.state = {"busy": False, "model": None, "phase": "idle", "detail": "", "queue": 0}
        self.queue = []
        self.proc = None
        self.loaded = {}            # port -> info for models loaded for chat

    @property
    def kept(self):
        # back-compat: the most-recently loaded model, or None
        if not self.loaded:
            return None
        return self.loaded[max(self.loaded, key=lambda k: self.loaded[k]["since"])]

    def free_port(self):
        used = set(self.loaded) | set(llama_pids_ports())
        # chat loads take the other ports first, so the test port stays free for a test run
        for p in [x for x in CFG["bench_ports"] if x != CFG["bench_port"]] + [CFG["bench_port"]]:
            if p not in used and p not in self.loaded:
                import socket as _s   # 23 Oct 2026: skip ports another service already holds (s1gate took 9020)
                _k = _s.socket(); _k.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
                try:
                    _k.bind(("127.0.0.1", p))
                except OSError:
                    continue
                finally:
                    _k.close()
                return p
        return None

    def budget(self):
        measured = SNAP["adapter_gb"] if time.time() - SNAP["ts"] < 15 else gpu_dedicated_used()
        # on the APU a resident model does not always show in the dedicated counter, so also count what is loaded
        committed = sum(((l.get("res") or {}).get("est_gb") or 0.0) for l in list(self.loaded.values()))
        return vram_total() - max(measured, committed) - CFG["vram_margin_gb"]

    def _set(self, phase, detail=""):
        self.state.update(phase=phase, detail=detail)
        log(f"[{self.state.get('model')}] {phase} {detail}")

    def _est(self, m, ctx):
        meta = cached_meta(m["path"], os.path.getmtime(m["path"]))
        return m["size_gb"] + kv_gb(meta, ctx, CFG["kv_type"]) + CFG["buffers_gb"]

    def _newres(self, m, ctx, est):
        return {"path": m["path"], "name": m["name"], "ts": time.time(), "ctx": ctx, "kv_type": CFG["kv_type"],
                "est_gb": round(est, 2), "verdict": "", "ok": False, "load_s": None, "pp_tps": None,
                "tg_tps": None, "ttft_s": None, "peak_ded_gb": 0.0, "peak_shared_gb": 0.0,
                "min_avail_ram_gb": None, "released": None, "score": None, "answers": [], "log_tail": ""}

    def _spawn(self, m, ctx, res, mcp=None, port=None):
        exe = pick_exe(m["path"]); kvt = CFG["kv_type"]
        args = [exe, "-m", m["path"], "-ngl", "99", "-c", str(ctx), "-np", "1", "-fa", "on",
                "-ctk", kvt, "-ctv", kvt] + (["--device", CFG["device"]] if CFG.get("device") else []) + ["--host", "127.0.0.1",
                "--port", str(port or CFG["bench_port"]), "--alias", m["name"][:64], "--reasoning", ("off" if port is None else CFG.get("chat_reasoning", "auto"))]
        if mcp:
            args += ["--jinja", "--mcp-servers-config", mcp]
        env = {k: v for k, v in os.environ.items() if not k.startswith("LLAMA_")}
        if LINUX:
            env["LD_LIBRARY_PATH"] = os.path.dirname(exe)
        if any(f.lower() in m["name"].lower() for f in CFG["coopmat_off"]):
            env["GGML_VK_DISABLE_COOPMAT"] = "1"
        else:
            env.pop("GGML_VK_DISABLE_COOPMAT", None)
        try:
            meta = cached_meta(m["path"], os.path.getmtime(m["path"]))
            arch = meta.get("general.architecture", "")
            if meta.get(arch + ".attention.sliding_window") or arch.startswith("gemma"):
                args.append("--swa-full")   # keep the full cache so multi-turn chat reuses it (Gemma: 11.9s -> 0.2s per turn)
            if meta.get(arch + ".nextn_predict_layers") and m["path"] not in mtp_off():
                args += ["--spec-type", "draft-mtp", "--spec-draft-n-max", "5"]   # MTP heads: Qwen3.8-27B 12.7 -> 32-39 t/s (28 Sep 2026); n-gram modes are slower, keep them off
            else:
                side = mtp_sidecar(m["path"])
                if side:
                    args += ["--model-draft", side, "--spec-type", "draft-mtp", "--spec-draft-n-max", "3", "-ngld", "99"]   # separate MTP draft file (Gemma 4 mtp-*.gguf)
        except Exception:
            pass
        logp = os.path.join(DATA, "runs", time.strftime("%Y%m%d-%H%M%S-") + re.sub(r"[^\w.-]", "_", m["name"])[:60] + ".log")
        os.makedirs(os.path.dirname(logp), exist_ok=True)
        res["log"] = logp
        lf = open(logp, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(args, cwd=os.path.dirname(exe), env=env, stdout=lf, stderr=subprocess.STDOUT,
                                creationflags=NOWIN | NEWGRP, **_POPEN_EXTRA)
        return proc, lf, logp

    def _wait_ready(self, proc, breach, port=None):
        t0 = time.time()
        while True:
            if breach["why"]:
                raise RuntimeError(breach["why"])
            if self.stop.is_set():
                raise RuntimeError("stopped by user")
            if proc.poll() is not None:
                raise RuntimeError(f"server exited during load (code {proc.returncode})")
            if _healthy(port or CFG["bench_port"]):
                return round(time.time() - t0, 1)
            if time.time() - t0 > CFG["load_timeout"]:
                raise RuntimeError(f"load timed out after {CFG['load_timeout']}s")
            time.sleep(1)

    def _teardown(self, proc, lf, logp, base, breach, res):
        breach["done"] = True
        self._set("unloading")
        kill_tree(proc.pid)
        for _ in range(40):
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        lf.close()
        try:
            with open(logp, encoding="utf-8", errors="replace") as f:
                res["log_tail"] = "".join(f.readlines()[-25:])
        except OSError:
            pass
        self._set("checking release")
        released = False
        for _ in range(20):
            if gpu_dedicated_used() <= base + 0.75:
                released = True
                break
            time.sleep(2)
        res["released"] = released
        if not released:
            res["verdict"] += " | GPU memory NOT released after unload"

    def run(self, m, ctx=None, allow_concurrent=False):
        ctx = int(ctx or CFG["ctx"]); port = CFG["bench_port"]
        est = self._est(m, ctx)
        res = self._newres(m, ctx, est)
        self.state.update(busy=True, model=m["name"])
        try:
            # tests run on the shared bench port; block only if that port is taken by a loaded chat model
            if CFG["bench_port"] in BENCH.loaded and not allow_concurrent:
                res["verdict"] = "blocked: a chat model is loaded on the test port - unload it or use another"
                return self._finish(res)
            budget = self.budget(); res["budget_gb"] = round(budget, 2)
            if est > budget:
                res["verdict"] = f"refused: needs ~{est:.1f} GB, only {budget:.1f} GB dedicated VRAM free"
                return self._finish(res)
            base = gpu_dedicated_used()
            self._set("loading", f"ctx {ctx}, est {est:.1f} GB of {budget:.1f} GB")
            proc, lf, logp = self._spawn(m, ctx, res)
            self.proc = proc
            breach = self._watch(proc.pid, res)
            try:
                res["load_s"] = self._wait_ready(proc, breach)
                self._set("speed test")
                r = _post(port, "/completion", {"prompt": FILLER + "\n\nOne-line summary:", "n_predict": CFG["gen_tokens"],
                                                "ignore_eos": True, "temperature": 0, "cache_prompt": False})
                tm = r.get("timings", {})
                res["pp_tps"] = round(tm.get("prompt_per_second") or 0, 1)
                res["tg_tps"] = round(tm.get("predicted_per_second") or 0, 1)
                res["ttft_s"] = round((tm.get("prompt_ms") or 0) / 1000, 2)
                res["prompt_tokens"] = tm.get("prompt_n")
                passed = 0
                for i, (name, prompt, check) in enumerate(CHECKS):
                    if breach["why"] or self.stop.is_set():
                        raise RuntimeError(breach["why"] or "stopped by user")
                    self._set("checks", f"{i + 1}/{len(CHECKS)} {name}")
                    t1 = time.time()
                    r = _post(port, "/v1/chat/completions", {"temperature": 0.2, "max_tokens": 512,
                                                             "messages": [{"role": "user", "content": prompt}]})
                    ans = _strip(r["choices"][0]["message"].get("content"))
                    ok = bool(check(ans)); passed += ok
                    res["answers"].append({"check": name, "pass": ok, "secs": round(time.time() - t1, 1), "prompt": prompt, "answer": ans})
                res["score"] = f"{passed}/{len(CHECKS)}"
                res["ok"] = True
                res["verdict"] = "ok"
            except Exception as e:
                res["verdict"] = str(breach["why"] or e)
            finally:
                self._teardown(proc, lf, logp, base, breach, res)
                self.proc = None
            return self._finish(res)
        finally:
            self.state.update(busy=bool(self.queue), phase="idle" if not self.queue else "queued", detail="", model=None)

    def load(self, m, ctx=None, tools=False):
        """Load a model on its own port and keep it up for the playground. Multiple allowed."""
        if m.get("downloading"):
            return {"error": "%s is still downloading - wait for it to finish" % m.get("name", "model")}
        other = llama_copy_of(m.get("path", ""), {k.get("pid") for k in self.loaded.values()}, fresh=True)
        if other:
            return {"error": "%s is already running (%s, port %s) - use that copy, a second one will not fit next to it" % (m.get("name", "model"), other["owner"], other["port"])}
        ctx = int(ctx or CFG["ctx"])
        with self.lock:
            if self.state["busy"]:
                return {"error": "bench busy - a test or another load is in progress"}
            if len(self.loaded) >= CFG["max_loaded"]:
                return {"error": "already at max loaded models (" + str(CFG["max_loaded"]) + ") - unload one first"}
            port = self.free_port()
            if not port:
                return {"error": "no free bench port"}
            self.state.update(busy=True, model=m["name"])
        try:
            est = self._est(m, ctx); budget = self.budget()
            if est > budget:
                return {"error": "refused: needs ~%.1f GB, only %.1f GB VRAM free (other loaded models take their share)" % (est, budget)}
            res = self._newres(m, ctx, est)
            base = gpu_dedicated_used()
            self._set("loading " + m["name"][:24] + " on :" + str(port), "ctx %d, est %.1f GB of %.1f GB" % (ctx, est, budget))
            self.stop.clear()
            proc, lf, logp = self._spawn(m, ctx, res, mcp=(CFG["mcp_config"] if tools else None), port=port)
            breach = self._watch(proc.pid, res)
            try:
                load_s = self._wait_ready(proc, breach, port)
            except Exception as e:
                res["verdict"] = str(breach["why"] or e)
                self._teardown(proc, lf, logp, base, breach, res)
                return {"error": res["verdict"]}
            mark_loaded(m["path"])
            self.loaded[port] = {"name": m["name"], "path": m["path"], "ctx": ctx, "port": port, "pid": proc.pid,
                                 "since": time.time(), "load_s": load_s, "proc": proc, "lf": lf, "logp": logp,
                                 "base": base, "breach": breach, "res": res, "tools": bool(tools), "rec": rec_settings(m["path"], ctx)}
            log("[" + m["name"] + "] loaded for chat in " + str(load_s) + "s on 127.0.0.1:" + str(port) + (" with tools" if tools else ""))
            return {"ok": True, "port": port, "load_s": load_s}
        finally:
            self.state.update(busy=False, phase="idle", detail="", model=None)

    def unload(self, port=None):
        """Unload one loaded model by port, or all if port is None."""
        with self.lock:
            if not self.loaded:
                return {"error": "nothing loaded"}
            targets = [port] if port is not None else list(self.loaded)
            targets = [t for t in targets if t in self.loaded]
            if not targets:
                return {"error": "no model loaded on that port"}
        released = {}
        for t in targets:
            k = self.loaded.pop(t, None)
            if not k:
                continue
            self.state.update(busy=True, model=k["name"])
            try:
                self._teardown(k["proc"], k["lf"], k["logp"], k["base"], k["breach"], k["res"])
                released[t] = k["res"]["released"]
            finally:
                self.state.update(busy=False, phase="idle", detail="", model=None)
        return {"released": released}

    def loaded_list(self):
        out = []
        for k in sorted(self.loaded.values(), key=lambda x: x["since"]):
            out.append({"name": k["name"], "ctx": k["ctx"], "port": k["port"], "pid": k["pid"], "load_s": k["load_s"],
                        "alive": k["proc"].poll() is None, "killed": k["breach"]["why"], "tools": k.get("tools", False), "rec": k.get("rec"),
                        "peak_ded_gb": k["res"]["peak_ded_gb"], "peak_shared_gb": k["res"]["peak_shared_gb"]})
        return out

    def kept_info(self):
        lst = self.loaded_list()
        return lst[-1] if lst else None

    def loaded_ports(self):
        return set(self.loaded)

    def reap(self):
        """Clean up chat models whose server died (crash, guard kill, closed by hand) - no button needed."""
        dead = []
        with self.lock:
            for port, k in list(self.loaded.items()):
                if k["proc"].poll() is not None:
                    dead.append(self.loaded.pop(port))
        for k in dead:
            threading.Thread(target=self._reap_one, args=(k,), daemon=True).start()

    def _reap_one(self, k):
        k["breach"]["done"] = True          # stops the RAM/VRAM sampler
        why = k["breach"].get("why")
        try:
            k["lf"].close()
        except Exception:
            pass
        try:
            with open(k["logp"], encoding="utf-8", errors="replace") as f:
                k["res"]["log_tail"] = "".join(f.readlines()[-25:])
        except (OSError, TypeError):
            pass
        released = False
        for _ in range(20):
            if gpu_dedicated_used() <= k["base"] + 0.75:
                released = True
                break
            time.sleep(2)
        msg = "%s on :%d %s - cleaned up automatically%s" % (
            k["name"], k["port"], ("was stopped: " + why) if why else "exited",
            "" if released else " (GPU memory not back yet)")
        log(msg)
        NOTICE.update(text=msg, ts=time.time())

    def adopt(self):
        """Re-attach llama-servers this bench started before a restart, so loaded models survive it."""
        try:
            out = ("\n".join("%d\t%s" % (_p, _cmdline(_a)) for _p, _a in _llama_procs()) if LINUX else ps("Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | % { [string]$_.ProcessId + [char]9 + $_.CommandLine }", timeout=30))
        except Exception as ex:
            log("adopt: could not list llama-servers: %s" % ex)
            return
        models = None
        for line in out.splitlines():
            pid_s, _, cmd = line.partition("\t")
            mp = re.search(r"--port\s+(\d+)", cmd)
            ma = re.search(r"--alias\s+(\S+)", cmd)
            mm = re.search(r"\s-m\s+(\"[^\"]+\"|\S+)", cmd)
            mc = re.search(r"\s-c\s+(\d+)", cmd)
            if not (pid_s.strip().isdigit() and mp and ma and mm) or "--device" not in cmd:
                continue
            port, pid = int(mp.group(1)), int(pid_s)
            if port not in CFG["bench_ports"] or port in self.loaded:
                continue
            path = mm.group(1).strip('"')
            ctx = int(mc.group(1)) if mc else CFG["ctx"]
            if models is None:
                try:
                    models = scan_models(ctx)
                except Exception:
                    models = []
            m = next((x for x in models if os.path.normcase(x["path"]) == os.path.normcase(path)), None) or {"name": ma.group(1), "path": path}
            try:
                est = self._est(m, ctx)
            except Exception:
                est = 0.0
            res = self._newres(m, ctx, est)
            tools = "--mcp-servers-config" in cmd
            self.loaded[port] = {"name": m["name"], "path": path, "ctx": ctx, "port": port, "pid": pid, "since": time.time(),
                                 "load_s": None, "proc": _PidProc(pid), "lf": _NoFile(), "logp": "",
                                 "base": max(0.0, gpu_dedicated_used() - est), "breach": self._watch(pid, res), "res": res,
                                 "tools": tools, "adopted": True, "rec": rec_settings(path, ctx)}
            log("adopted running %s on :%d (pid %d%s)" % (m["name"], port, pid, ", tools" if tools else ""))

    def _finish(self, res):
        res["id"] = save_result(res)
        log(f"[{res['name']}] {res['verdict']} load={res['load_s']} pp={res['pp_tps']} tg={res['tg_tps']} "
            f"vram={res['peak_ded_gb']} shared={res['peak_shared_gb']} score={res['score']} released={res['released']}")
        return res
    def _watch(self, pid, res):
        """Samples this server's GPU memory and Windows RAM; kills it on breach."""
        breach = {"why": None, "done": False}
        sampler = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "while($true){ try { $s=(Get-Counter \"\\GPU Process Memory(pid_%d_*)\\Dedicated Usage\",\"\\GPU Process Memory(pid_%d_*)\\Shared Usage\" -EA Stop).CounterSamples;"
             " $d=($s|?{$_.Path -match 'dedicated'}|Measure CookedValue -Sum).Sum; $h=($s|?{$_.Path -match 'shared'}|Measure CookedValue -Sum).Sum;"
             " [Console]::Out.WriteLine(\"$d $h\") } catch { [Console]::Out.WriteLine('0 0') }; [Console]::Out.Flush(); Start-Sleep -Milliseconds 1000 }" % (pid, pid)],
            stdout=subprocess.PIPE, text=True, creationflags=NOWIN)

        def gpu_reader():
            for line in sampler.stdout:
                if breach["done"]:
                    break
                p = line.split()
                if len(p) == 2:
                    d, h = _num(p[0]) / GB, _num(p[1]) / GB
                    res["peak_ded_gb"] = round(max(res["peak_ded_gb"], d), 2)
                    res["peak_shared_gb"] = round(max(res["peak_shared_gb"], h), 2)
                    if h > CFG["shared_limit_gb"] and not breach["why"]:
                        breach["why"] = f"killed: spilled {h:.1f} GB into shared/system memory (limit {CFG['shared_limit_gb']} GB)"
                        kill_tree(pid)

        def ram_guard():
            while not breach["done"]:
                a = host_mem()["avail_gb"]
                res["min_avail_ram_gb"] = round(a if res["min_avail_ram_gb"] is None else min(res["min_avail_ram_gb"], a), 2)
                if a < CFG["ram_floor_gb"] and not breach["why"]:
                    breach["why"] = f"killed: Windows available RAM fell to {a:.1f} GB (floor {CFG['ram_floor_gb']} GB)"
                    kill_tree(pid)
                time.sleep(0.5)
            try:
                sampler.kill()
            except Exception:
                pass

        threading.Thread(target=gpu_reader, daemon=True).start()
        threading.Thread(target=ram_guard, daemon=True).start()
        return breach

    def enqueue(self, models, ctx=None):
        models = [m for m in models if not m.get("downloading")]
        with self.lock:
            self.queue.extend((m, ctx) for m in models)
            self.state["queue"] = len(self.queue)
            if not self.state["busy"]:
                self.state["busy"] = True
                threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        self.stop.clear()
        while True:
            with self.lock:
                if not self.queue or self.stop.is_set():
                    self.queue.clear(); self.state.update(busy=False, queue=0, phase="idle")
                    return
                m, ctx = self.queue.pop(0)
                self.state["queue"] = len(self.queue)
            try:
                self.run(m, ctx)
            except Exception as e:
                log(f"run crashed: {e!r}")

BENCH = Bench()
# ------------------------------------------------------------------ web UI
NOTICE = {"text": "", "ts": 0}
_TOOLS_CACHE = {}
CONTEXT = {"dir": ""}
CTX_FILE = os.path.join(DATA, "workfolder.json")
PINS_FILE = os.path.join(DATA, "pins.json")
_pin_lock = threading.Lock()
_pins = []

def _ctx_load():
    try:
        with open(CTX_FILE, encoding="utf-8") as f:
            CONTEXT["dir"] = (json.load(f).get("dir") or "").strip()
    except Exception:
        CONTEXT["dir"] = ""

def _ctx_save():
    tmp = CTX_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"dir": CONTEXT.get("dir") or ""}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CTX_FILE)

def _pins_load():
    global _pins
    try:
        with open(PINS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        _pins = [str(x) for x in raw if isinstance(x, str)]
    except Exception:
        _pins = []

def _pins_save():
    tmp = PINS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_pins, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PINS_FILE)

def pin_set(mid, on):
    mid = (mid or "").strip()
    if not mid:
        return
    with _pin_lock:
        if mid in _pins:
            _pins.remove(mid)
        if on:
            _pins.insert(0, mid)
        _pins_save()

MTP_OFF_FILE = os.path.join(DATA, "mtp_off.json")
_mtp_lock = threading.Lock()


def mtp_off():
    """Models whose MTP head drafts badly (e.g. a fine-tune carrying its base model's head) - loaded without MTP."""
    try:
        with open(MTP_OFF_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def mtp_off_add(path, why):
    with _mtp_lock:
        d = mtp_off()
        if path in d:
            return False
        d[path] = {"why": why, "ts": time.strftime("%Y-%m-%d %H:%M")}
        tmp = MTP_OFF_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, MTP_OFF_FILE)
    msg = "MTP turned off for %s (%s) - reload it to run without MTP" % (os.path.basename(path), why)
    log(msg)
    NOTICE.update(text=msg, ts=time.time())
    return True


CTX_STEPS = [4096, 8192, 16384, 32768, 49152, 65536, 98304, 131072]
_FAMILY_SAMPLING = [(("qwen",), 0.6, 0.95, 20), (("gemma",), 1.0, 0.95, 64), (("llama", "mistral"), 0.7, 0.9, 40), (("glm",), 0.7, 0.95, 40)]


def auto_ctx(m, budget):
    """Largest context the model supports that still fits the free VRAM, keeping 1 GB spare."""
    try:
        meta = cached_meta(m["path"], os.path.getmtime(m["path"]))
    except Exception:
        return CFG["ctx"]
    native = int(m.get("native_ctx") or meta.get("%s.context_length" % meta.get("general.architecture")) or CFG["ctx"])
    best = 0   # 0 = does not fit even at the smallest step right now
    for c in CTX_STEPS:
        if c > native or c > CFG.get("auto_ctx_max", 65536):
            break
        if m.get("size_gb", 0) + kv_gb(meta, c, CFG["kv_type"]) + CFG["buffers_gb"] <= budget - 1.0:
            best = c
    return min(best, native) if (best and native >= 2048) else best


def rec_settings(path, ctx):
    """Sampling + max tokens for a loaded model: GGUF general.sampling.* when present, else the family's usual values."""
    try:
        meta = cached_meta(path, os.path.getmtime(path))
    except Exception:
        meta = {}
    arch = str(meta.get("general.architecture") or "").lower()
    t, p, k, src = 0.7, 0.95, 40, "default"
    for fams, ft, fp, fk in _FAMILY_SAMPLING:
        if arch.startswith(fams):
            t, p, k, src = ft, fp, fk, arch + " family"
            break
    g = [meta.get("general.sampling." + x) for x in ("temp", "top_p", "top_k")]
    if any(v is not None for v in g):
        src = "GGUF"
        t = round(float(g[0]), 2) if g[0] is not None else t
        p = round(float(g[1]), 2) if g[1] is not None else p
        k = int(g[2]) if g[2] is not None else k
    opts = [512, 1024, 2048, 4096]
    mt = max([o for o in opts if o <= max(512, int(ctx) // 4)] or [512])
    return {"ctx": int(ctx), "max_tokens": mt, "temp": t, "top_p": p, "top_k": k, "source": src}


def sort_for_bench(models):
    with _pin_lock:
        rank = {i: n for n, i in enumerate(_pins)}
    tep = _te_paths()
    for m in models:
        m["pinned"] = m["id"] in rank
        m["task_employed"] = bool(m.get("path")) and (os.path.normcase(m["path"]) in tep or os.path.normcase(os.path.realpath(m["path"])) in tep)
    return sorted(models, key=lambda m: (0 if m["task_employed"] else 1, 0 if m["pinned"] else 1, -m.get("added", 0), m["name"].lower()))

_ctx_load()
_pins_load()
if LINUX:
    Bench._watch = _watch_linux

# ---- per-device workspace: each browser sends the machine + folder it works in ----
import gzip, base64
_WS = threading.local()


def cur_ws():
    """Workspace of the request being served: {'host': 'local' or a peer id, 'dir': path}.
    Requests without workspace headers (API callers, old pages) use the saved server folder on this PC."""
    v = getattr(_WS, "v", None)
    if v:
        return v
    return {"host": "local", "dir": (CONTEXT.get("dir") or "").strip()}


def ws_set(v):
    _WS.v = v


def ws_thread(fn, *a):
    """A thread that keeps the caller's workspace (team workers run in their own threads)."""
    w = cur_ws()

    def run():
        ws_set(w)
        fn(*a)
    return threading.Thread(target=run, daemon=True)


def ws_label(w=None):
    return target_label((w or cur_ws())["host"])


def ws_tool(w=None):
    w = w or cur_ws()
    return "aissh6_aissh_run" if w["host"] == "local" else "aissh6_%s_run" % w["host"]


def _norm_dir(d):
    d = (d or "").strip().replace("/", "\\")
    d = d[:2] + re.sub(r"\\{2,}", r"\\", d[2:])   # collapse doubled separators, keep a UNC \\server prefix
    if len(d) > 3:
        d = d.rstrip("\\")
    if len(d) == 2 and d[1] == ":":
        d += "\\"
    return d


def _ps_str(v):
    return "'" + str(v).replace("'", "''") + "'"


def _ps_on(host, script, timeout=30):
    """PowerShell on this PC or on a peer; returns stdout text."""
    if host in (None, "", "local"):
        return ps(script, timeout=timeout)
    out, _ = peer_run(script, int(timeout * 1000), peer=host)
    return (out or "").strip()

def context_note():
    w = cur_ws()
    d = (w.get("dir") or "").strip()
    if not d:
        return ""
    name, tool = ws_label(w), ws_tool(w)
    s = ("\n\nWORKING FOLDER: " + d + " on " + name + "\n"
         "This folder is selected and mandatory. It is on " + name + ", so run every shell command for this task with "
         + tool + " - commands already start in the folder. "
         "Use relative paths (Get-ChildItem . or .\\file). Do not Set-Location away from it. "
         "Do not read or write any other drive or folder unless the user named that exact path in the task. "
         "Ignore any example that starts at C:\\ .")
    if w["host"] != "local":
        s += " Do not use aissh6_aissh_run for this folder: that tool runs on " + LOCAL_HOST + ", a different PC."
    return s

def _with_folder(text):
    note = context_note().strip()
    if not note:
        return text or ""
    return note + "\n\n" + (text or "")

def _command_in_folder(raw_cmd, args):
    """Selected working folder wins. The model cannot point the shell somewhere else."""
    w = cur_ws()
    wd = ((w.get("dir") or "").strip() if w["host"] == "local" else "") or ((args or {}).get("working_directory") or (args or {}).get("workingDirectory") or "").strip()
    if wd:
        raw_cmd = "Set-Location -LiteralPath '%s'\n%s" % (wd.replace("'", "''"), raw_cmd or "")
    return raw_cmd, wd

CHG_DIR = os.path.join(DATA, "changes")
_chg_lock = threading.Lock()
_CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".json", ".md", ".ps1", ".cs", ".txt", ".xml", ".yml", ".yaml", ".toml", ".vue", ".rs", ".go", ".java", ".c", ".h", ".cpp", ".hpp", ".sql", ".sh", ".cmd", ".bat"}
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", "bin", "obj", "dist", ".venv", "venv", ".vs", "changes"}

def _snap_root():
    """(host, folder) of the current workspace, or None when there is no usable folder."""
    w = cur_ws()
    d = _norm_dir(w.get("dir"))
    if not d or len(d) < 4:
        return None
    if w["host"] == "local":
        if not os.path.isdir(d):
            return None
        root = os.path.abspath(d)
        return None if len(root) < 4 else ("local", root)
    return (w["host"], d)


_REMOTE_STATE_PS = r"""
$root = __ROOT__
if (-not (Test-Path -LiteralPath $root -PathType Container)) { [string][char]31 + 'NOFOLDER'; return }
$ext = @(__EXT__); $skip = @(__SKIP__)
$base = (Get-Item -LiteralPath $root).FullName.TrimEnd('\')
$out = [ordered]@{}; $n = 0; $sw = [Diagnostics.Stopwatch]::StartNew()
$strict = New-Object Text.UTF8Encoding($false, $true)
$stack = New-Object System.Collections.Stack; $stack.Push($base)
while ($stack.Count -gt 0 -and $n -lt 400 -and $sw.Elapsed.TotalSeconds -lt 25) {
  $d = $stack.Pop()
  foreach ($c in @(Get-ChildItem -LiteralPath $d -Force -EA SilentlyContinue)) {
    if ($c.PSIsContainer) { if (($skip -notcontains $c.Name.ToLower()) -and -not $c.Name.StartsWith('.')) { $stack.Push($c.FullName) }; continue }
    if (($ext -notcontains $c.Extension.ToLower()) -or $c.Length -gt 200000) { continue }
    try { $t = [IO.File]::ReadAllText($c.FullName, $strict) } catch { continue }
    $out[$c.FullName.Substring($base.Length).TrimStart('\')] = $t; $n++
    if ($n -ge 400) { break }
  }
}
$j = [Text.Encoding]::UTF8.GetBytes(($out | ConvertTo-Json -Compress -Depth 2))
$ms = New-Object IO.MemoryStream; $gz = New-Object IO.Compression.GZipStream($ms, [IO.Compression.CompressionMode]::Compress)
$gz.Write($j, 0, $j.Length); $gz.Close()
[string][char]31 + 'STATE ' + [Convert]::ToBase64String($ms.ToArray())
"""

_REMOTE_REVERT_PS = r"""
$tmp = "__TMP__"
try {
  $root = (Get-Item -LiteralPath __ROOT__).FullName.TrimEnd('\')
  $b = [Convert]::FromBase64String((Get-Content -LiteralPath $tmp -Raw).Trim())
  $in = New-Object IO.MemoryStream(,$b)
  $gz = New-Object IO.Compression.GZipStream($in, [IO.Compression.CompressionMode]::Decompress)
  $j = (New-Object IO.StreamReader($gz, [Text.Encoding]::UTF8)).ReadToEnd() | ConvertFrom-Json -AsHashtable
  $enc = New-Object Text.UTF8Encoding($false)
  function Inside($k) { if ([string]::IsNullOrEmpty($k) -or $k -match '(^|\\)\.\.(\\|$)' -or [IO.Path]::IsPathRooted($k)) { return $null }
    $p = [IO.Path]::GetFullPath((Join-Path $root $k)); if ($p.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { $p } else { $null } }
  foreach ($k in @($j['write'].Keys)) { $p = Inside $k; if (-not $p) { continue }
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($p)) | Out-Null; [IO.File]::WriteAllText($p, [string]$j['write'][$k], $enc) }
  foreach ($k in @($j['delete'])) { $p = Inside $k; if ($p -and (Test-Path -LiteralPath $p -PathType Leaf)) { Remove-Item -LiteralPath $p -Force } }
  [string][char]31 + 'REVERTED'
} finally { Remove-Item -LiteralPath $tmp -Force -EA SilentlyContinue }
"""


def _remote_code_state(host, root):
    script = (_REMOTE_STATE_PS.replace("__ROOT__", _ps_str(root))
              .replace("__EXT__", ",".join(_ps_str(e) for e in sorted(_CODE_EXT)))
              .replace("__SKIP__", ",".join(_ps_str(e) for e in sorted(_SKIP_DIRS))))
    out, _ = peer_run(script, 90000, peer=host)
    if "\x1fNOFOLDER" in (out or ""):
        return None
    m = re.search("\x1fSTATE ([A-Za-z0-9+/=]+)", out or "")
    if not m:
        raise RuntimeError("no snapshot from %s: %s" % (target_label(host), (out or "")[-200:]))
    return json.loads(gzip.decompress(base64.b64decode(m.group(1))).decode("utf-8"))


def _remote_revert(host, root, state):
    cur = _remote_code_state(host, root) or {}
    blob = base64.b64encode(gzip.compress(json.dumps({"write": state, "delete": [r for r in cur if r not in state]}).encode("utf-8"))).decode()
    tmp = "$env:TEMP\\mb_revert_%s.b64" % secrets.token_hex(4)
    for i in range(0, len(blob), 15000):
        out, code = peer_run("Add-Content -LiteralPath \"%s\" -Value '%s' -NoNewline" % (tmp, blob[i:i + 15000]), 60000, peer=host)
        if code not in (None, 0):
            return "could not send the saved state to %s: %s" % (target_label(host), (out or "")[:200])
    out, _ = peer_run(_REMOTE_REVERT_PS.replace("__TMP__", tmp).replace("__ROOT__", _ps_str(root)), 180000, peer=host)
    if "\x1fREVERTED" not in (out or ""):
        return "revert on %s failed: %s" % (target_label(host), (out or "")[-300:])
    return ""


def _code_state(root):
    if not root:
        return None
    host, path = root
    if host == "local":
        return _code_state_local(path)
    try:
        return _remote_code_state(host, path)
    except Exception as ex:
        log("remote snapshot of %s on %s failed: %s" % (path, host, ex))
        return None


def _ws_match(item, root):
    if not root:
        return False
    w = item.get("ws")
    if not w:
        return root[0] == "local"
    return str(w[0]).lower() == str(root[0]).lower() and str(w[1]).lower() == str(root[1]).lower()


def _code_state_local(root):
    if not root:
        return None
    out, n = {}, 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d.lower() not in _SKIP_DIRS and not d.startswith(".")]
        for name in fns:
            if os.path.splitext(name)[1].lower() not in _CODE_EXT:
                continue
            full = os.path.join(dp, name)
            try:
                if os.path.getsize(full) > 200000:
                    continue
                with open(full, encoding="utf-8") as f:
                    text = f.read()
            except Exception:
                continue
            out[os.path.relpath(full, root)] = text
            n += 1
            if n >= 400:
                return out
    return out

def _chg_load():
    try:
        with open(os.path.join(CHG_DIR, "index.json"), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except Exception:
        pass
    return {"current": "", "items": []}

def _chg_save_index(idx):
    os.makedirs(CHG_DIR, exist_ok=True)
    path = os.path.join(CHG_DIR, "index.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def _chg_write(sid, state):
    os.makedirs(CHG_DIR, exist_ok=True)
    path = os.path.join(CHG_DIR, sid + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def _chg_read(sid):
    with open(os.path.join(CHG_DIR, sid + ".json"), encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}

def _chg_push(state, label, idx, wsid=None):
    sid = secrets.token_hex(4)
    _chg_write(sid, state)
    idx.setdefault("items", []).append({"id": sid, "ts": time.time(), "label": (label or "change")[:80], "nfiles": len(state), "ws": wsid})
    idx["current"] = sid
    while sum(1 for it in idx["items"] if it.get("ws") == wsid) > 5:
        old = next(it for it in idx["items"] if it.get("ws") == wsid)
        idx["items"].remove(old)
        try:
            os.remove(os.path.join(CHG_DIR, old["id"] + ".json"))
        except OSError:
            pass
        if idx.get("current") == old["id"]:
            idx["current"] = idx["items"][-1]["id"] if idx["items"] else ""
    _chg_save_index(idx)

def _record_change(before, after, label, root=None):
    if before is None or after is None or before == after:
        return
    wsid = list(root) if root else None
    with _chg_lock:
        idx = _chg_load()
        mine = [it for it in idx["items"] if it.get("ws") == wsid]
        last_id = mine[-1]["id"] if mine else ""
        last_state = None
        if last_id:
            try:
                last_state = _chg_read(last_id)
            except Exception:
                last_state = None
        if last_state != before:
            _chg_push(before, "before " + (label or "change"), idx, wsid)
            idx = _chg_load()
        _chg_push(after, label or "change", idx, wsid)

def changes_list():
    with _chg_lock:
        idx = _chg_load()
    cur = idx.get("current") or ""
    root = _snap_root()
    rows = []
    for it in reversed(idx.get("items") or []):
        if not _ws_match(it, root):
            continue
        rows.append({"id": it.get("id") or "", "label": it.get("label") or "change",
                     "when": time.strftime("%d %b %H:%M:%S", time.localtime(it.get("ts") or 0)),
                     "nfiles": it.get("nfiles") or 0, "current": it.get("id") == cur})
    return rows

def _safe_join(root, rel):
    rel = (rel or "").replace("/", "\\").lstrip("\\")
    if not rel or any(part == ".." for part in rel.split("\\")):
        return ""
    full = os.path.abspath(os.path.join(root, rel))
    base = os.path.abspath(root).rstrip("\\")
    if full != base and not full.startswith(base + "\\"):
        return ""
    return full

def _chg_id(sid):
    sid = (sid or "").strip()
    if len(sid) != 8 or any(c not in "0123456789abcdef" for c in sid):
        return ""
    return sid

def revert_change(sid):
    sid = _chg_id(sid)
    root = _snap_root()
    if not sid:
        return "no such saved change"
    if not root:
        return "set a working folder first"
    with _chg_lock:
        idx = _chg_load()
        item = next((it for it in idx.get("items") or [] if it.get("id") == sid), None)
        if not item:
            return "no such saved change"
        if not _ws_match(item, root):
            return "that saved change belongs to a different working folder"
        try:
            state = _chg_read(sid)
        except Exception:
            return "saved change could not be read"
        if root[0] != "local":
            err = _remote_revert(root[0], root[1], state)
            if err:
                return err
            idx["current"] = sid
            _chg_save_index(idx)
            return ""
        root = root[1]
        for rel, text in state.items():
            if not isinstance(rel, str) or not isinstance(text, str):
                continue
            dest = _safe_join(root, rel)
            if not dest:
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        for rel in (_code_state_local(root) or {}):
            if rel in state:
                continue
            dest = _safe_join(root, rel)
            if dest and os.path.isfile(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
        idx["current"] = sid
        _chg_save_index(idx)
    return ""

def _bind_tools(defs):
    w = cur_ws()
    d = (w.get("dir") or "").strip()
    if not d or not defs:
        return defs
    where = ws_label(w)
    out = []
    for item in defs:
        fn = item.get("function") or {}
        name = fn.get("name") or ""
        if not ("aissh" in name and any(k in name for k in ("run", "command", "submit"))):
            out.append(item)
            continue
        target = _peer_of_tool(name) or "local"
        if target.lower() != w["host"].lower():
            if w["host"] != "local" and target == "local":
                item = json.loads(json.dumps(item))
                fn = item.setdefault("function", {})
                fn["description"] = ((fn.get("description") or "").rstrip() + " NOTE: this runs on " + LOCAL_HOST + ", NOT on "
                                     + where + " where the working folder " + d + " is. Use " + ws_tool(w) + " for the working folder.")
            out.append(item)
            continue
        if True:
            item = json.loads(json.dumps(item))
            fn = item.setdefault("function", {})
            fn["description"] = ((fn.get("description") or "").rstrip()
                                 + " Commands already start in the working folder " + d + " on " + where
                                 + ". Use relative paths. Do not pass working_directory.")
            params = fn.setdefault("parameters", {})
            props = params.get("properties") or {}
            props.pop("working_directory", None)
            props.pop("workingDirectory", None)
            params["properties"] = props
            params["required"] = [x for x in (params.get("required") or []) if x not in ("working_directory", "workingDirectory")]
        out.append(item)
    return out

def _load_mcp_conf():
    if REMOTE:
        try:
            with open(AC.get("AIBOX_REMOTE_SHELL_TOKEN_FILE", "/etc/aibox/remote_shell_token"), encoding="utf-8") as f:
                tok = f.read().strip()
        except OSError:
            tok = ""
        return {"bearer": tok, "aissh6_url": CFG["aissh6_url"]}
    try:
        with open(CFG["mcp_config"], encoding="utf-8") as f:
            j = json.load(f)
        srv = j.get("mcpServers", {})
        return {"bearer": (srv.get("aissh6", {}).get("env", {}) or {}).get("AISSH6_BEARER", ""),
                "aissh6_url": (srv.get("aissh6", {}).get("env", {}) or {}).get("AISSH6_URL", CFG["aissh6_url"]),
                }
    except Exception:
        return {"bearer": "", "aissh6_url": CFG["aissh6_url"]}

_MCPC = _load_mcp_conf()
def _run_tool_raw(name, args):
    """Execute one MCP tool the model asked for. Returns a string result."""
    cap = CFG["tool_result_max"]
    try:
        if _peer_of_tool(name):
            pr = _peer_of_tool(name)
            w = cur_ws()
            wd = (w.get("dir") or "").strip() if w["host"].lower() == pr.lower() else ""
            cmd = ("Set-Location -LiteralPath %s\n" % _ps_str(wd) if wd else "") + "& {\n" + (args.get("command", "") or "") + "\n} | Out-String -Width 220"
            out, code = peer_run(cmd, args.get("timeout_ms") or 120000, peer=pr)
            out = "[" + peer_label(pr) + "] " + ("[cwd] " + wd + "\n" if wd else "") + (out or "(ran ok, no output)") + ("" if code in (None, 0) else "\n[exit %s]" % code)
            return out[:cap] + ("\n[... truncated]" if len(out) > cap else "")
        if name in ("aissh6_web_search", "aissh6_web_fetch", "web_search", "web_fetch"):
            import webtools   # webtools.py next to this file
            a = dict(args) if isinstance(args, dict) else {}
            if "fetch" in name:
                a["max_chars"] = min(int(a.get("max_chars") or cap), cap)
            out = webtools.run(name, a)
            return out[:cap] + ("\n[... truncated]" if len(out) > cap else "")
        if name.startswith("aissh6_") and not REMOTE:
            return "error: no command runner is configured on this box (classic chat only has web_search and web_fetch)"
        if name.startswith("aissh6_"):
            wd = ""
            if name == "aissh6_aissh_health":
                path, payload = "/health", None
            else:
                raw_cmd, wd = _command_in_folder(args.get("command", "") or "", args)
                # render objects / Format-Table output to console text - the daemon otherwise returns format-object type names
                payload = {"command": "& {\n" + raw_cmd + "\n} | Out-String -Width 220", "session_id": "modelbench"}
                if args.get("timeout_ms"):
                    payload["timeout_ms"] = args["timeout_ms"]
                if wd:
                    payload["workingDirectory"] = wd
                path = "/command"
            req = urllib.request.Request(_MCPC["aissh6_url"].rstrip("/") + path,
                                         data=(json.dumps(payload).encode() if payload is not None else None),
                                         method=("POST" if payload is not None else "GET"),
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "Bearer " + _MCPC["bearer"]})
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read())
            if isinstance(d, dict) and ("stdout" in d or "exitCode" in d):
                parts = []
                so = (d.get("stdout") or "").rstrip()
                se = (d.get("stderr") or "").rstrip()
                if so:
                    parts.append(so)
                if se:
                    parts.append("[stderr] " + se)
                fr = d.get("failureReason")
                if fr and fr != "null":
                    parts.append("[error] " + str(fr))
                out = "\n".join(parts)
                if not out:
                    out = "(ran ok, no output; exit " + str(d.get("exitCode")) + ")"
            else:
                out = d.get("output") or d.get("status") or json.dumps(d)
            if wd:
                out = "[cwd] " + wd + "\n" + out
            return out[:cap] + ("\n[... truncated]" if len(out) > cap else "")
        return f"error: unknown tool {name}"
    except Exception as e:
        return f"error running {name}: {e}"

# ---- activity feed, terminal and job lists for the playground panel ----
ACT = {"lock": threading.Lock(), "seq": 0, "items": []}
TERM_CWD = {}


def act_add(**kw):
    with ACT["lock"]:
        ACT["seq"] += 1
        e = dict(kw)
        e["seq"] = e["id"] = ACT["seq"]
        e["ts"] = time.time()
        ACT["items"].append(e)
        del ACT["items"][:-300]
        return e


def act_set(e, **kw):
    with ACT["lock"]:
        ACT["seq"] += 1
        e.update(kw)
        e["seq"] = ACT["seq"]


def act_since(since):
    with ACT["lock"]:
        return ACT["seq"], [dict(e) for e in ACT["items"] if e["seq"] > since]


def _aissh_req(path, payload=None, timeout=30, accept=None, method=None):
    if not REMOTE:
        raise RuntimeError("no remote command daemon configured (AIBOX_REMOTE_SHELL_URL)")
    req = urllib.request.Request(_MCPC["aissh6_url"].rstrip("/") + path,
                                 data=(json.dumps(payload).encode() if payload is not None else None),
                                 method=method or ("POST" if payload is not None else "GET"),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + _MCPC["bearer"],
                                          "Accept": accept or "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _mcp_call(tool, arguments, timeout=200):
    raw = _aissh_req("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": tool, "arguments": arguments}},
                     timeout=timeout, accept="application/json, text/event-stream")
    data = [l for l in raw.splitlines() if l.startswith("data:")]
    d = json.loads(data[-1][5:].strip() if data else raw)
    if d.get("error"):
        raise RuntimeError(str(d["error"].get("message") or d["error"]))
    return "".join(c.get("text", "") for c in ((d.get("result") or {}).get("content") or []) if c.get("type") == "text")


def peer_run(command, timeout_ms=120000, session="peer", peer="peer"):
    """PowerShell on a peer PC through the local daemon's peer_run_command. Returns (output, exit_code)."""
    txt = _mcp_call("peer_run_command", {"peer": peer, "command": command,
                                         "timeout_ms": int(timeout_ms), "session_id": session},
                    timeout=int(int(timeout_ms) / 1000) + 30)
    try:
        d = json.loads(txt)
    except ValueError:
        return txt, None
    r = d.get("result") if isinstance(d, dict) else None
    if not isinstance(r, dict):
        return txt, None
    out = (r.get("output") or "").rstrip()
    if r.get("timed_out"):
        out += "\n[timed out]"
    return out, r.get("exit_code")


def run_tool(name, args, who=None):
    """run the tool and record it in the activity feed"""
    a = args if isinstance(args, dict) else {}
    pr = _peer_of_tool(name)
    host = peer_label(pr) if pr else ("web" if name.endswith(("web_search", "web_fetch")) else LOCAL_HOST)
    detail = a.get("command") or a.get("query") or a.get("url") or a.get("slug") or json.dumps(a)[:300]
    e = act_add(kind="tool", who=who or "agent", tool=name, host=host, detail=str(detail)[:600], state="run")
    t0 = time.time()
    try:
        res = _run_tool_raw(name, args)
    except Exception as ex:
        act_set(e, state="fail", secs=round(time.time() - t0, 1), result=str(ex)[:600])
        raise
    act_set(e, state=("fail" if res.startswith("error") else "done"), secs=round(time.time() - t0, 1), result=res[:1500])
    return res


def _term_wrap(cmd, host):
    # aissh6 caps persistent sessions, so the terminal runs session-less and
    # modelbench replays the last known folder on every command instead.
    cwd = TERM_CWD.get(host)
    w = cur_ws()
    if cwd:
        pre = "Set-Location -LiteralPath '%s' -ErrorAction SilentlyContinue\n" % cwd.replace("'", "''")
    elif (w.get("dir") or "").strip() and w["host"] == host:
        pre = "Set-Location -LiteralPath '%s' -ErrorAction SilentlyContinue\n" % w["dir"].replace("'", "''")
    elif host != "local":
        pre = "if (Test-Path 'E:\\git') { Set-Location 'E:\\git' } else { Set-Location 'C:\\' }\n"
    else:
        start = (CONTEXT.get("dir") or "").strip() or DATA
        pre = "Set-Location -LiteralPath '%s' -ErrorAction SilentlyContinue\n" % start.replace("'", "''")
    return (pre + "& {\n" + cmd + "\n} 2>&1 | Out-String -Stream -Width 200\n"
            "[string][char]31 + 'CWD ' + (Get-Location).Path\n"
            "[string][char]31 + 'HOST ' + $env:COMPUTERNAME\n")


def _find_jobs(o, depth=0):
    if depth > 6:
        return None
    if isinstance(o, str):
        t = o.strip()
        if t[:1] in "[{":
            try:
                return _find_jobs(json.loads(t), depth + 1)
            except ValueError:
                return None
        return None
    if isinstance(o, list):
        if o and all(isinstance(x, dict) and "jobId" in x for x in o):
            return o
        for x in o:
            r = _find_jobs(x, depth + 1)
            if r is not None:
                return r
        return None if o else []
    if isinstance(o, dict):
        for v in o.values():
            r = _find_jobs(v, depth + 1)
            if r is not None:
                return r
    return None


def list_jobs(host, limit=25):
    if host != "local":
        txt = _mcp_call("peer_call", {"peer": host, "tool": "aissh_list_jobs",
                                      "arguments_json": json.dumps({"limit": limit})}, timeout=30)
        jobs = _find_jobs(txt)
    else:
        jobs = _find_jobs(_aissh_req("/jobs?limit=%d" % limit, timeout=15))
    return (jobs or [])[:limit]


# ---- machine detection: what PC is this, and what does each tool really reach ----
import socket as _socket
LOCAL_HOST = (AC.get("AIBOX_REMOTE_SHELL_HOST") if REMOTE else "") or _socket.gethostname().upper() or "THIS-BOX"
MACH = {"lock": threading.Lock(), "ts": 0.0, "data": None}


def _peer_of_tool(name):
    m = re.match(r"^aissh6_([A-Za-z0-9-]+)_run$", name or "")
    if not m or m.group(1).lower() == "aissh":
        return None
    return m.group(1)


def _last_line(t):
    lines = [l.strip() for l in str(t or "").splitlines() if l.strip()]
    return lines[-1] if lines else ""


_WHO_PS = "$env:COMPUTERNAME + '|' + ((Get-NetIPAddress -AddressFamily IPv4 -EA SilentlyContinue | ? { $_.IPAddress -notmatch '^(127\\.|169\\.254\\.)' }).IPAddress -join ',')"


def _parse_who(line):
    name, _, ips = (line or "").partition("|")
    return (name.strip().upper() or None), [x.strip() for x in ips.split(",") if x.strip()]


def client_machine(handler):
    """Which known PC the browser is on, from its IP. None when unknown (e.g. through a tunnel)."""
    ip = (handler.client_address[0] or "").split("%")[0]
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    fwd = handler.headers.get("CF-Connecting-IP") or handler.headers.get("X-Forwarded-For")
    if ip in ("127.0.0.1", "::1"):
        if fwd:
            return {"ip": fwd.split(",")[0].strip(), "machine": None, "via": "proxy"}
        return {"ip": ip, "machine": "local"}
    m = machines()
    if ip in (m["local"].get("ips") or []):
        return {"ip": ip, "machine": "local"}
    for p in m.get("peers") or []:
        if ip in (p.get("ips") or []):
            return {"ip": ip, "machine": p["id"]}
    return {"ip": ip, "machine": None}


def machines(force=False):
    """Ask the machines who they are instead of trusting names. Cached for 5 minutes."""
    with MACH["lock"]:
        if MACH["data"] is not None and not force and time.time() - MACH["ts"] < 300:
            return MACH["data"]
    d = {"local": {"id": "local", "name": LOCAL_HOST, "exec": None}, "daemon_self": None, "peers": [], "warnings": []}
    if not REMOTE:
        with MACH["lock"]:
            MACH["data"], MACH["ts"] = d, time.time()
        return d
    try:
        r = json.loads(_aissh_req("/command", {"command": _WHO_PS}, timeout=20))
        d["local"]["exec"], d["local"]["ips"] = _parse_who(_last_line(r.get("stdout") or r.get("output") or ""))
    except Exception as ex:
        d["warnings"].append("could not reach the aissh6 daemon at %s: %s" % (_MCPC.get("aissh6_url"), ex))
    try:
        pl = json.loads(_mcp_call("peer_list", {}, timeout=20))
        d["daemon_self"] = str(pl.get("self") or "").upper() or None
        for p in pl.get("peers") or []:
            pid = str(p.get("name") or "").strip()
            if not re.match(r"^[A-Za-z0-9-]+$", pid):
                continue
            ent = {"id": pid, "name": pid.upper(), "exec": None, "ok": p.get("http") == 200}
            if ent["ok"]:
                try:
                    out, _ = peer_run(_WHO_PS, 20000, peer=pid)
                    ent["exec"], ent["ips"] = _parse_who(_last_line(out))
                    if ent["exec"]:
                        ent["name"] = ent["exec"]
                except Exception as ex:
                    ent["ok"] = False
                    ent["error"] = str(ex)[:200]
            d["peers"].append(ent)
    except Exception as ex:
        d["warnings"].append("could not list aissh6 peers: %s" % ex)
    ex = d["local"]["exec"]
    if ex and ex != LOCAL_HOST:
        d["warnings"].append("aissh6 at %s runs commands on %s, not on this PC (%s)" % (_MCPC.get("aissh6_url"), ex, LOCAL_HOST))
    if d["daemon_self"] and d["daemon_self"] != LOCAL_HOST:
        d["warnings"].append("the aissh6 daemon calls itself %s but this PC is %s" % (d["daemon_self"], LOCAL_HOST))
    for p in d["peers"]:
        if p["exec"] and p["exec"] == LOCAL_HOST:
            d["warnings"].append("peer %s is this same PC (%s)" % (p["id"], LOCAL_HOST))
    with MACH["lock"]:
        MACH["data"], MACH["ts"] = d, time.time()
    if d["warnings"]:
        log("machine check: " + "; ".join(d["warnings"]))
    return d


def peer_label(pid):
    for p in ((MACH["data"] or {}).get("peers") or []):
        if p["id"].lower() == str(pid or "").lower():
            return p["name"]
    return str(pid or "?").upper()


def resolve_target(h):
    h = str(h or "local").strip().lower()
    if h in ("", "local", LOCAL_HOST.lower()):
        return "local"
    for p in machines().get("peers") or []:
        if p["id"].lower() == h or p["name"].lower() == h:
            return p["id"]
    return None


def target_label(t):
    return LOCAL_HOST if t == "local" else peer_label(t)


def machine_facts():
    if not REMOTE:
        return ("MACHINE FACTS (detected by modelbench): you run on %s. web_search searches the internet and web_fetch "
                "reads a web page. You cannot run commands or read files on this computer." % LOCAL_HOST)
    m = machines()
    lines = ["MACHINE FACTS (detected by modelbench; these override anything else you were told):",
             "- You and modelbench run on %s." % LOCAL_HOST,
             "- aissh6_aissh_run executes on %s (this PC)." % LOCAL_HOST]
    for p in m.get("peers") or []:
        lines.append("- aissh6_%s_run executes on %s, the other PC%s." % (p["id"], p["name"], "" if p.get("ok") else " (unreachable right now)"))
    lines.append("- aissh6_web_search searches the internet and aissh6_web_fetch reads a web page (text + links). Use them for anything online - do not use PowerShell for web requests.")
    lines.append("- Results from another PC start with that PC's name in brackets. Always say which PC a result came from, and never claim a command ran on a machine other than the one its tool executes on.")
    return "\n".join(lines)


def _inject_facts(msgs):
    facts = machine_facts()
    if msgs and msgs[0].get("role") == "system":
        if "MACHINE FACTS" not in (msgs[0].get("content") or ""):
            msgs[0] = dict(msgs[0])
            msgs[0]["content"] = facts + "\n\n" + (msgs[0].get("content") or "")
    else:
        msgs.insert(0, {"role": "system", "content": facts})


threading.Thread(target=machines, daemon=True).start()


CSS = r""":root{--bg:#111316;--p:#1a1d22;--l:#2a2f36;--t:#d8dde3;--m:#8a939e;--g:#5fbf7f;--r:#e06c6c;--y:#d9b44a;--b:#6aa6e0}
#tasks{display:none;margin-bottom:8px}#tasks.on{display:block}
#tasks .tk{display:flex;gap:10px;align-items:center;background:var(--p);border:1px solid var(--l);border-left-width:3px;border-radius:6px;padding:5px 10px;margin-bottom:4px;font-family:ui-monospace,Consolas,monospace;font-size:12px}
#tasks .tk.run{border-left-color:var(--y)}#tasks .tk.done{border-left-color:var(--g)}#tasks .tk.fail{border-left-color:var(--r)}#tasks .tk.plan{border-left-color:var(--b)}
#tasks .who{font-weight:700;min-width:120px}#tasks .el{margin-left:auto;font-weight:700;font-size:14px;min-width:56px;text-align:right}
#tasks .desc{color:var(--m);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:46vw}
.md{white-space:normal}.md .h1,.md .h2{font-size:15px;font-weight:700;color:#e6ebf0;border-bottom:1px solid var(--l);padding-bottom:3px;margin:12px 0 6px}
.md .h3,.md .h4{font-size:13px;font-weight:700;color:var(--b);margin:10px 0 4px}.md .p{margin:3px 0}.md .sp{height:6px}
.md ul,.md ol{margin:4px 0 4px 22px;padding:0}.md li{margin:2px 0}.md hr{border:0;border-top:1px solid var(--l);margin:10px 0}
.md code{background:#0a0c0e;border:1px solid var(--l);border-radius:3px;padding:0 4px;color:#e6c07b}
.tblw{overflow-x:auto;margin:6px 0}table.mdt{border-collapse:collapse;width:auto;min-width:50%}
table.mdt th{background:#20252c;color:var(--t);text-align:left;padding:5px 10px;border:1px solid var(--l);white-space:nowrap;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
table.mdt td{padding:4px 10px;border:1px solid var(--l);white-space:normal}table.mdt tbody tr:nth-child(odd){background:rgba(255,255,255,.025)}
.report{background:#0f1215;border:1px solid var(--l);border-left:4px solid var(--g);border-radius:8px;margin-bottom:10px;overflow:hidden}
.report.partial{border-left-color:var(--y)}.report.fail{border-left-color:var(--r)}
.rhead{display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:#161b20;padding:7px 12px;border-bottom:1px solid var(--l)}
.rtitle{font-weight:700;letter-spacing:.14em;font-size:12px}.rbadge{font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;letter-spacing:.06em}
.rbadge.ok{background:#173325;color:#8fdca9}.rbadge.partial{background:#3a3218;color:#e8cf7a}.rbadge.fail{background:#3a2020;color:#e59a9a}
.racts{display:flex;gap:8px;padding:10px 12px;border-top:1px solid var(--l);background:#12161a}.rnote{padding:8px 12px;border-top:1px solid var(--l);color:var(--m)}
.rmeta{color:var(--m);font-size:11px}.rcopy{margin-left:auto;font-size:11px;padding:1px 8px}
.rtask{padding:6px 12px;border-bottom:1px solid var(--l);font-size:12px}.rbody{padding:8px 14px}
.msg.rc{border-left-color:var(--y)}
.dd{position:relative;display:inline-block}
.dd>.pop{display:none;position:absolute;top:110%;right:0;z-index:40;background:var(--p);border:1px solid var(--l);border-radius:8px;padding:12px;width:300px;box-shadow:0 6px 24px rgba(0,0,0,.5)}
.dd.open>.pop{display:block}
.dd .pop label{display:block;color:var(--m);margin:8px 0 3px}
#teambtn.on{border-color:var(--g);color:var(--g)}
#readout{display:flex;gap:26px;align-items:baseline;background:#0a0c0e;border:1px solid var(--l);border-radius:8px;padding:10px 16px;margin-bottom:10px;font-family:ui-monospace,Consolas,monospace}
#readout .lab{color:var(--m);font-size:12px;text-transform:uppercase;letter-spacing:.08em;margin-right:8px}
#readout .big{font-size:34px;font-weight:700;line-height:1;letter-spacing:.02em}
#readout .unit{font-size:15px;color:var(--m);margin-left:3px}
#readout .sub{font-size:12px;color:var(--m);margin-left:6px}
tr.row-loaded>td{background:rgba(106,166,224,.14)}.spk{display:inline-flex;align-items:center;gap:5px;margin-left:8px;vertical-align:middle}
tr.row-loaded>td:first-child{box-shadow:inset 3px 0 0 var(--b)}
tr.row-pass>td{background:rgba(95,191,127,.09)}
tr.row-pass>td:first-child{box-shadow:inset 3px 0 0 var(--g)}
tr.row-fail>td{background:rgba(224,108,108,.10)}
tr.row-fail>td:first-child{box-shadow:inset 3px 0 0 var(--r)}
tr.row-untested>td:first-child{box-shadow:inset 3px 0 0 #3a3f47}
.sbadge.sb-new{background:var(--y);color:#1b1a18;font-weight:700}
.sbadge{font-size:10px;padding:1px 6px;border-radius:8px;margin-left:6px;vertical-align:middle}
.sb-loaded{background:#1c3247;color:#9cc6f2}.sb-te{background:#3a2a12;color:#f0c674;font-weight:600}.te-h{margin:6px 0 4px;font-weight:600;color:#f0c674}.card.te{border-left:3px solid #f0c674}tr.te-sep td{background:#2a2112;color:#f0c674;font-weight:600;padding:4px 8px}tr.te td.name{border-left:3px solid #f0c674}#rows td.name{max-width:31vw;width:31vw}#rows td.name>.dim{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:31vw}#rows>tr>td:last-child{width:1%;white-space:nowrap}#rows>tr>td{padding-top:3px;padding-bottom:3px;vertical-align:middle}.sb-pass{background:#173325;color:#8fdca9}.sb-fail{background:#3a2020;color:#e59a9a}.sb-untested{background:#23262b;color:var(--m)}
*{box-sizing:border-box}body{background:var(--bg);color:var(--t);font:13px/1.45 ui-monospace,Consolas,monospace;margin:0;padding:14px}
h1{font-size:15px;margin:0 0 8px}h2{font-size:13px;color:var(--m);font-weight:normal;margin:14px 0 6px;text-transform:uppercase;letter-spacing:.06em}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:var(--p);padding:8px 10px;border:1px solid var(--l);border-radius:6px;margin-bottom:10px}
button,a.btn{background:#23303d;color:var(--t);border:1px solid var(--l);border-radius:4px;padding:3px 9px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}
button:hover,a.btn:hover{border-color:var(--b)}button:disabled{opacity:.4;cursor:default}button.bad{background:#3a2323}button.go{background:#1f3a2a}
input,textarea,select{background:#0c0e10;color:var(--t);border:1px solid var(--l);border-radius:4px;padding:3px 6px;font:inherit}
table{border-collapse:collapse;width:100%}th,td{padding:4px 7px;border-bottom:1px solid var(--l);text-align:left;vertical-align:top;white-space:nowrap}
th{color:var(--m);font-weight:normal;position:sticky;top:0;background:var(--bg)}td.n{text-align:right}.dim{color:var(--m);font-size:11px}
.ok{color:var(--g)}.no{color:var(--r)}.warn{color:var(--y)}.name{white-space:normal;max-width:380px}
.card{background:var(--p);border:1px solid var(--l);border-radius:6px;padding:8px 10px;margin-bottom:6px;display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap}
.card .big{font-size:14px}.kv span{margin-right:12px}.dot{font-size:16px;line-height:1}
#detail{white-space:pre-wrap;background:var(--p);border:1px solid var(--l);border-radius:6px;padding:10px;margin-top:12px;display:none}
html[data-theme="gray"]{--bg:#080808;--p:#101010;--l:#222222;--t:#d4d4d4;--m:#8a8a8a;--g:#c8c8c8;--r:#bdbdbd;--y:#9a9a9a;--b:#9e9e9e}
html[data-theme="pro"]{--bg:#0a0c0a;--p:#101410;--l:#1e2a1e;--t:#d7e0d4;--m:#7d8c7a;--g:#7CFF68;--r:#C45C4A;--y:#E6B84A;--b:#7aa2c4}
html[data-theme] body{font-family:"JetBrains Mono","IBM Plex Mono","Cascadia Code",ui-monospace,Consolas,monospace;font-size:12px}
html[data-theme] h1{color:var(--g);font-weight:400;letter-spacing:.02em}
html[data-theme] h1::after{content:"";display:inline-block;width:7px;height:13px;background:var(--g);margin-left:6px;vertical-align:-2px;animation:mbblink 1.1s steps(1) infinite}
@keyframes mbblink{50%{opacity:0}}
html[data-theme] .bar,html[data-theme] .card,html[data-theme] #detail,html[data-theme] .dd>.pop,html[data-theme] .report{background:var(--bg);border-color:var(--l);border-radius:0}
html[data-theme] #readout{background:transparent;border:1px solid var(--l);border-radius:0;padding:0;gap:0;align-items:stretch}
html[data-theme] #readout>div{flex:1;padding:10px 14px 12px;border-right:1px solid var(--l)}
html[data-theme] #readout>div:last-child{border-right:0}
html[data-theme] #readout .big{font-size:26px}
html[data-theme] button,html[data-theme] a.btn,html[data-theme] input,html[data-theme] select,html[data-theme] textarea{font-family:inherit;background:transparent;color:var(--t);border:1px solid var(--l);border-radius:2px;box-shadow:none}
html[data-theme] button:hover,html[data-theme] a.btn:hover{background:#161616;border-color:#3a3a3a;color:var(--g)}
html[data-theme="pro"] button:hover,html[data-theme="pro"] a.btn:hover{background:#162016;border-color:#3a5a3a}
html[data-theme] button.go,html[data-theme] a.btn.go,html[data-theme] #playlink{color:var(--g);border-color:var(--g)}
html[data-theme] button.bad{color:var(--r);border-color:var(--r);background:transparent}
html[data-theme] #teambtn.on{color:var(--g);border-color:var(--g)}
html[data-theme] th{color:#5a5a5a;font-weight:500;text-transform:uppercase;letter-spacing:.08em;font-size:10px;border-bottom:1px solid #3a3a3a;background:var(--bg)}
html[data-theme="pro"] th{color:#5d6b5a;border-bottom-color:#2a3a2a}
html[data-theme] td{border-bottom:1px solid var(--l)}
html[data-theme] tr:hover>td{background:#161616}
html[data-theme="pro"] tr:hover>td{background:#162016}
html[data-theme="gray"] tr.row-loaded>td{background:#121212}
html[data-theme="gray"] tr.row-loaded>td:first-child{box-shadow:inset 2px 0 0 #c8c8c8}
html[data-theme="gray"] tr.row-pass>td{background:#101010}
html[data-theme="gray"] tr.row-pass>td:first-child{box-shadow:inset 2px 0 0 #7a7a7a}
html[data-theme="gray"] tr.row-fail>td{background:#141414}
html[data-theme="gray"] tr.row-fail>td:first-child{box-shadow:inset 2px 0 0 #bdbdbd}
html[data-theme="pro"] tr.row-loaded>td{background:#101810}
html[data-theme="pro"] tr.row-loaded>td:first-child{box-shadow:inset 2px 0 0 #7CFF68}
html[data-theme="pro"] tr.row-pass>td{background:#0e140e}
html[data-theme="pro"] tr.row-pass>td:first-child{box-shadow:inset 2px 0 0 #3d7a38}
html[data-theme="pro"] tr.row-fail>td{background:#1a1210}
html[data-theme="pro"] tr.row-fail>td:first-child{box-shadow:inset 2px 0 0 #C45C4A}
html[data-theme] tr.row-untested>td:first-child{box-shadow:none}
html[data-theme] .sbadge{background:transparent;padding:0;border-radius:0}
html[data-theme] .sb-loaded,html[data-theme] .sb-pass{color:var(--g)}
html[data-theme] .sb-fail,html[data-theme] .sb-untested{color:var(--y)}
html[data-theme] .report,html[data-theme] .rhead{background:var(--p)}
html[data-theme] .rbadge{background:transparent;border:1px solid currentColor;border-radius:2px}
html[data-theme] .md code,html[data-theme] pre.code,html[data-theme] #readout{background:var(--bg)}
html[data-theme] table.mdt th{background:var(--p);color:var(--t)}
html[data-theme] .md .h1,html[data-theme] .md .h2{color:var(--t)}
html[data-theme] input[type="checkbox"]{appearance:none;width:12px;height:12px;border:1px solid var(--m);background:transparent;vertical-align:-2px}
html[data-theme] input[type="checkbox"]:checked{background:var(--g);box-shadow:inset 0 0 0 2px var(--bg)}
html[data-theme="gray"] .bar,html[data-theme="gray"] .card,html[data-theme="gray"] #detail,html[data-theme="gray"] #readout,html[data-theme="gray"] .report{box-shadow:inset 1px 1px 0 rgba(255,255,255,.08),inset -1px -1px 0 rgba(0,0,0,.55),0 6px 16px rgba(0,0,0,.55)}
html[data-theme="gray"] button,html[data-theme="gray"] a.btn{background:#141414;box-shadow:inset 1px 1px 0 rgba(255,255,255,.10),inset -1px -1px 0 rgba(0,0,0,.65),0 2px 4px rgba(0,0,0,.45)}
html[data-theme="gray"] button:active,html[data-theme="gray"] a.btn:active{box-shadow:inset 1px 2px 4px rgba(0,0,0,.75);transform:translateY(1px)}
html[data-theme="gray"] input,html[data-theme="gray"] select,html[data-theme="gray"] textarea{background:#050505;box-shadow:inset 2px 2px 4px rgba(0,0,0,.55)}
@keyframes mbflash-warn{0%,100%{opacity:1}50%{opacity:.35}}
@keyframes mbflash-info{0%,100%{opacity:1}50%{opacity:.6}}
html[data-theme="gray"] .sb-fail,html[data-theme="gray"] .sb-untested{animation:mbflash-warn 1.25s ease-in-out infinite}
html[data-theme="gray"] .sb-loaded,html[data-theme="gray"] .sb-pass{animation:mbflash-info 2.2s ease-in-out infinite}
@media (prefers-reduced-motion:reduce){html[data-theme="gray"] .sb-fail,html[data-theme="gray"] .sb-untested,html[data-theme="gray"] .sb-loaded,html[data-theme="gray"] .sb-pass{animation:none}}

/* model list: raised cards, lift on hover */
table:has(>tbody#rows){border-collapse:separate;border-spacing:0 8px}
#rows>tr.clk{cursor:pointer;filter:drop-shadow(0 2px 1px rgba(0,0,0,.55)) drop-shadow(0 7px 10px rgba(0,0,0,.38));transition:transform .15s ease,filter .15s ease}
#rows>tr.clk>td{background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.012)),var(--p);border-top:1px solid rgba(255,255,255,.14);border-bottom:1px solid rgba(0,0,0,.6);vertical-align:middle;padding-top:7px;padding-bottom:7px}
#rows>tr.clk>td:first-child{border-left:1px solid rgba(255,255,255,.10);border-radius:10px 0 0 10px;padding-left:12px}
#rows>tr.clk>td:last-child{border-right:1px solid rgba(0,0,0,.5);border-radius:0 10px 10px 0}
#rows>tr.clk:hover{transform:translateY(-3px) scale(1.006);filter:brightness(1.09) drop-shadow(0 5px 3px rgba(0,0,0,.55)) drop-shadow(0 16px 20px rgba(0,0,0,.5))}
#rows>tr.clk:hover>td{border-top-color:rgba(255,255,255,.26)}
#rows>tr.clk:active{transform:translateY(-1px) scale(1.002);transition-duration:.05s}
#rows>tr.row-loaded>td{background:linear-gradient(180deg,rgba(106,166,224,.22),rgba(106,166,224,.08)),var(--p)}
#rows>tr.row-pass>td{background:linear-gradient(180deg,rgba(95,191,127,.15),rgba(95,191,127,.05)),var(--p)}
#rows>tr.row-fail>td{background:linear-gradient(180deg,rgba(224,108,108,.17),rgba(224,108,108,.06)),var(--p)}
#rows>tr.row-loaded>td:first-child{box-shadow:inset 4px 0 0 var(--b)}
#rows>tr.row-pass>td:first-child{box-shadow:inset 4px 0 0 var(--g)}
#rows>tr.row-fail>td:first-child{box-shadow:inset 4px 0 0 var(--r)}
#rows>tr.rowload{opacity:.6;transform:none}

/* panel headers: capitals, terminal font */
h1,h2,th{font-family:"Cascadia Code","Cascadia Mono","JetBrains Mono",Consolas,ui-monospace,monospace}
h1{text-transform:uppercase;letter-spacing:.08em;font-size:14px}
h2{text-transform:uppercase;letter-spacing:.1em;font-size:11.5px;font-weight:600}
th{text-transform:uppercase;letter-spacing:.08em;font-size:10.5px}
"""

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>modelbench - __HOST__</title><style>__CSS__</style><script>try{var t=localStorage.getItem('mb_theme');if(t==='gray'||t==='pro')document.documentElement.dataset.theme=t}catch(e){}</script><style>
.uimask{position:fixed;inset:0;background:rgba(0,0,0,.55);backdrop-filter:blur(2px);display:flex;align-items:center;justify-content:center;z-index:99999;animation:uifade .12s ease}
.uibox{min-width:300px;max-width:min(520px,92vw);background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,0)),var(--p,#1a1d22);color:var(--t,#d8dde3);border:1px solid var(--l,#2a2f36);border-top-color:rgba(255,255,255,.16);border-radius:12px;box-shadow:0 18px 50px rgba(0,0,0,.6),0 2px 6px rgba(0,0,0,.5);padding:16px 18px 14px;animation:uipop .14s ease;font-family:var(--ui,inherit)}
.uibox h3{margin:0 0 10px;font-family:"Cascadia Code","Cascadia Mono","JetBrains Mono",Consolas,ui-monospace,monospace;font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.09em;color:var(--t,#d8dde3)}
.uibox .uimsg{white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.45;max-height:50vh;overflow:auto}
.uibox .uibtns{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
.uibox button{padding:6px 14px;border-radius:7px;border:1px solid var(--l,#2a2f36);background:rgba(255,255,255,.06);color:var(--t,#d8dde3);cursor:pointer;font:inherit;font-size:13px}
.uibox button:hover{background:rgba(255,255,255,.11)}
.uibox button.uiok{background:var(--ac,var(--b,#6aa6e0));border-color:transparent;color:var(--bg,#111316);font-weight:600}
.uibox button.uiok.danger{background:var(--r,#e06c6c)}
.uibox button.uiok:hover{filter:brightness(1.1)}
.uibox button:focus-visible{outline:2px solid var(--ac,var(--b,#6aa6e0));outline-offset:2px}
@keyframes uifade{from{opacity:0}}@keyframes uipop{from{opacity:0;transform:translateY(6px) scale(.97)}}
</style>
<script>
function uiDialog(msg,o){o=o||{};return new Promise(function(res){var prev=document.activeElement,m=document.createElement('div'),b=document.createElement('div'),bt=document.createElement('div'),ok,c;
 m.className='uimask';b.className='uibox';b.setAttribute('role','dialog');b.setAttribute('aria-modal','true');bt.className='uibtns';
 if(o.title){var h=document.createElement('h3');h.textContent=o.title;b.appendChild(h)}
 var p=document.createElement('div');p.className='uimsg';p.textContent=String(msg==null?'':msg);b.appendChild(p);
 function done(v){document.removeEventListener('keydown',kd,true);m.remove();try{if(prev&&prev.focus)prev.focus()}catch(e){}res(v)}
 if(o.cancel!==false){c=document.createElement('button');c.textContent=o.cancelText||'Cancel';c.onclick=function(){done(false)};bt.appendChild(c)}
 ok=document.createElement('button');ok.className='uiok'+(o.danger?' danger':'');ok.textContent=o.ok||'OK';ok.onclick=function(){done(true)};bt.appendChild(ok);
 b.appendChild(bt);m.appendChild(b);m.addEventListener('mousedown',function(e){if(e.target===m)done(o.cancel===false)});
 function kd(e){var f=[].slice.call(bt.querySelectorAll('button')),i=f.indexOf(document.activeElement);
  if(e.key==='Escape'){e.preventDefault();e.stopPropagation();done(o.cancel===false)}
  else if(e.key==='Enter'){e.preventDefault();e.stopPropagation();(i>=0?f[i]:ok).click()}
  else if(e.key==='Tab'){e.preventDefault();e.stopPropagation();f[((i<0?0:i)+(e.shiftKey?-1:1)+f.length)%f.length].focus()}
  else{e.stopPropagation()}}
 document.addEventListener('keydown',kd,true);document.body.appendChild(m);((o.danger&&c)?c:ok).focus()})}
function uiAlert(msg,o){return uiDialog(msg,Object.assign({cancel:false},o||{}))}
function uiConfirm(msg,o){return uiDialog(msg,o||{})}
window.alert=function(m){uiAlert(m)};
</script>
</head><body><h1>modelbench &middot; __HOST__</h1>
<div id="readout"><div><span class="lab">VRAM free</span><span class="big" id="ro-vram">--</span><span class="unit">GB</span><span class="sub" id="ro-vram-sub"></span></div><div><span class="lab">RAM free</span><span class="big" id="ro-ram">--</span><span class="unit">GB</span><span class="sub" id="ro-ram-sub"></span></div><div><span class="lab">loaded</span><span class="big" id="ro-loaded">0</span></div></div>
<div class="bar"><span id="st">...</span><span id="host" class="dim"></span>
<span style="margin-left:auto">ctx <input id="ctx" type="number" step="1024" style="width:90px" placeholder="auto" title="Context size for LOAD. Empty = auto: the largest context each model supports that fits the free VRAM" onchange="load()"></span>
<label title="give the loaded chat model the web_search / web_fetch tools"><input type="checkbox" id="tools" style="width:auto" checked> tools</label>
<label>theme <select id="theme" onchange="setTheme(this.value)" title="terminal gray and terminal pro are the downloaded packs. current is the original bench theme"><option value="current">current</option><option value="gray">terminal gray</option><option value="pro">terminal pro</option></select></label>
<a class="btn" id="playlink">PLAYGROUND</a><a class="btn" id="boxlink" href="__BOXURL__" title="Box manager: health, processes, services, timers, power">BOX</a>__NAVEXTRA__<a class="btn" style="cursor:pointer" onclick="location.href='/play/chat#info'" title="Services, endpoints, how to add models">INFO</a><a class="btn hflink" target="_blank" rel="noopener" href="#" style="cursor:pointer" title="Search Hugging Face and download models onto the box">+ GET MODELS</a>
<button onclick="act('runfit')">Test all that fit</button><button onclick="act('stop')">Stop</button>
<button class="bad" onclick="uiConfirm('Kill EVERY llama-server on this box?',{title:'Kill all',ok:'Kill all',danger:1}).then(y=>{if(y)act('killall')})">Kill all</button></div>
<h2>Running now</h2><div id="running"></div>
<h2 style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">Models <input id="msearch" type="search" autocomplete="off" spellcheck="false" placeholder="search models - name, arch, quant...  ( / to focus, Esc to clear )" title="Space-separated words must all match. Filter is remembered." oninput="mfilter()" style="flex:1;min-width:220px;max-width:520px;font:inherit;font-size:13px;font-weight:normal;padding:4px 8px"><span id="mcount" class="dim" style="font-weight:normal;font-size:12px"></span></h2>
<table><thead><tr><th>model</th><th>size GB</th><th>est GB @ctx</th><th>fits</th><th>last test</th><th>load s</th><th>pp t/s</th><th>tg t/s</th><th>VRAM pk</th><th>shared pk</th><th>min RAM</th><th>freed</th><th>checks</th><th></th></tr></thead><tbody id="rows"></tbody></table>
<div id="detail"></div>
<script>
const K=new URLSearchParams(location.search).get('key')||'';const H={'Content-Type':'application/json','X-Key':K};
var HFURL='';fetch('/api/info',{headers:H}).then(r=>r.json()).then(j=>{HFURL=j.hf_url||'';document.querySelectorAll('.hflink').forEach(a=>{if(HFURL)a.href=HFURL});if(j.box_url){var bl=document.getElementById('boxlink');if(bl)bl.href=j.box_url}}).catch(()=>{});
function openHF(){if(HFURL)window.open(HFURL,'_blank');else alert('The Hugging Face link is not ready (hf-fetch has no key yet?) - try again in a moment')}
const $=id=>document.getElementById(id);let first=true;$('playlink').href='/play?key='+encodeURIComponent(K);
function setTheme(v){if(v==='gray'||v==='pro')document.documentElement.dataset.theme=v;else delete document.documentElement.dataset.theme;try{localStorage.setItem('mb_theme',v||'current')}catch(e){}}
try{$('theme').value=localStorage.getItem('mb_theme')||'current'}catch(e){}
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const f=(v,d=1)=>v==null?'':(+v).toFixed(d);
const up=s=>s==null?'?':s<60?s+'s':s<3600?Math.floor(s/60)+'m':Math.floor(s/3600)+'h'+String(Math.floor(s%3600/60)).padStart(2,'0');
const play=(port,model)=>`/play?key=${encodeURIComponent(K)}&port=${port}${model?'&model='+encodeURIComponent(model):''}`;
let rowT=0;$('rows').addEventListener('click',e=>{if(e.target.closest('button,a,input,select,label,summary'))return;const tr=e.target.closest('tr[data-id]');if(!tr)return;
 if(String(window.getSelection&&window.getSelection()||'').trim())return;if(Date.now()-rowT<4000)return;
 if(tr.dataset.ok!=='1'){uiAlert(tr.dataset.why,{title:'Cannot load this model'});return}rowT=Date.now();tr.classList.add('rowload');act('load',{id:tr.dataset.id,tools:$('tools').checked})});
async function act(a,body){body=Object.assign({ctx:+$('ctx').value||null},body||{});const r=await fetch('/api/'+a,{method:'POST',headers:H,body:JSON.stringify(body)});const j=await r.json().catch(()=>({}));if(j.attach){location.href=play(j.attach.port,j.attach.model);return}if(j.error)uiAlert(j.error,{title:'modelbench'});load()}
async function det(rid){const r=await (await fetch('/api/result?id='+rid,{headers:H})).json();let s=`${r.name}\nverdict: ${r.verdict}\nctx ${r.ctx}  est ${r.est_gb} GB  budget ${r.budget_gb??'-'} GB  ttft ${r.ttft_s??'-'}s  prompt ${r.prompt_tokens??'-'} tok\n\n`;
for(const a of r.answers||[])s+=`[${a.pass?'PASS':'FAIL'}] ${a.check} (${a.secs}s)\n> ${a.prompt}\n${a.answer}\n\n`;s+='--- server log tail ---\n'+(r.log_tail||'');$('detail').textContent=s;$('detail').style.display='block';$('detail').scrollIntoView()}
function apiAddr(s,lan,bench){const h=location.hostname;if(lan)return 'http://'+h+':'+s.port+'/v1';if(bench)return 'http://'+h+':9050/v1';return ''}
function cp(a){const t=a.dataset.copy,ok=()=>{a.textContent='copied';setTimeout(()=>{a.textContent='copy'},1500)};if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(ok)}else{const x=document.createElement('textarea');x.value=t;document.body.appendChild(x);x.select();try{document.execCommand('copy')}catch(e){}document.body.removeChild(x);ok()}}
function apiSpan(s,lan,bench){const a=apiAddr(s,lan,bench);if(!a)return '';const tip=lan?'OpenAI-compatible endpoint of this server, reachable from other machines':'This model only listens on this box. Other machines use the bench API, which sends requests to the loaded models. Send the bench key as X-Key or a Bearer token.';return '<span title="'+tip+'">API <b class="mono">'+esc(a)+'</b> <a class="btn" href="#" data-copy="'+esc(a)+'" onclick="cp(this);return false">copy</a></span>'+(bench?'<span>model id <b class="mono">'+esc(s.model)+'</b></span>':'')}
function teBlock(j){const t=j.task_employed||[];if(!t.length)return '';
 return '<div class="te-h">&#128274; Task Employed <span class="dim">worker models for the automated services - locked in, kept loaded (manage with model-lock)</span></div>'+t.map(x=>`<div class="card te"><span class="dot ${x.state==='ready'?'ok':'no'}">&#9679;</span><div style="min-width:320px"><div class="big">${esc(x.id)} <span class="sbadge sb-te" title="worker model used by the automated services - locked, do not unload">&#128274; Task Employed</span>${x.locked?' <span class="ok" title="locked with model-lock">locked</span>':' <span class="dim" title="persistent llama-swap group: never swapped out">persistent</span>'}</div><div class="dim">${esc(x.file)}</div></div><div class="kv"><span>port ${esc(x.port||'-')}</span><span>RAM ${f(x.ram_gb)} GB</span><span class="${x.state==='ready'?'ok':'dim'}">${esc(x.state)}</span></div></div>`).join('')+'<div class="te-h dim">Other models</div>'}
function running(j){const sv=j.servers.filter(s=>s.kind!=='router-child').sort((a,b)=>(b.task_employed?1:0)-(a.task_employed?1:0));
 if(!sv.length)return `<div class="card dim">Nothing running. ${f(j.budget)} GB of ${f(j.vram)} GB VRAM free for a model.</div>`;
 return sv.map((s,i)=>{const hdr=(i===0&&s.task_employed)?'<div class="te-h">&#128274; Task Employed <span class="dim">worker models for the automated services - locked in, keep loaded</span></div>':((i>0&&!s.task_employed&&sv[i-1].task_employed)?'<div class="te-h dim">Other models</div>':'');const h=s.health==='ok'?'ok':(s.health==='loading'?'warn':'no');const lan=s.host==='0.0.0.0'||s.host==='::';
  const kids=j.servers.filter(c=>c.ppid===s.pid);const ld=(j.loaded||[]).find(x=>x.pid===s.pid);const bench=!!ld;
  const title=(s.kind==='router'?'router ('+s.models.length+' models)':esc(s.model))+(s.kind!=='router'?`<span class="spk" data-port="${s.port}"></span>`:'');
  const mc=(j.models||[]).find(m=>s.card&&m.id===s.card);
  const tasks=mc?` <span class="dim" title="times used in team tasks / score. Success adds 1, failure subtracts 1">tasks ${mc.tasks_used||0}/${mc.tasks_score||0}</span>`:'';
  const models=s.models.length?s.models.map(m=>`<div>${esc(m.id)} <span class="${m.status==='loaded'?'ok':'dim'}">${esc(m.status)}</span> ${s.health==='ok'&&m.status==='loaded'?`<a class="btn" href="${play(s.port,m.id)}">chat</a>`:''}</div>`).join(''):'';
  return hdr+`<div class="card${s.task_employed?' te':''}"><span class="dot ${h}">&#9679;</span><div style="min-width:320px"><div class="big">${title}${s.task_employed?' <span class="sbadge sb-te" title="worker model used by the automated services - locked, do not unload">&#128274; Task Employed</span>':''}${tasks}</div>
   <div class="dim">${esc(s.kind)}${bench?' (bench, for chat)':''} &middot; ${esc(s.build)} &middot; pid ${s.pid}${kids.length?' &middot; children '+kids.map(k=>k.pid+' '+esc(k.alias||k.model)).join(', '):''}</div>${s.kind==='router'?models:''}</div>
   <div class="kv"><span>${esc(s.host)}:${s.port} <span class="${lan?'warn':'dim'}">${lan?'LAN':'local only'}</span></span>${apiSpan(s,lan,bench)}<span>ctx ${esc(s.ctx??'default')}</span>${bench&&j.kept&&j.kept.tools?'<span class="ok">tools on</span>':''}
   <span>VRAM ${f(s.ded_gb)} GB</span><span class="${s.shared_gb>1?'warn':''}">shared ${f(s.shared_gb)} GB</span><span>RAM ${f(s.ram_gb)} GB</span><span>up ${up(s.uptime_s)}</span><span class="${h}">${esc(s.health)}</span></div>
   <div style="margin-left:auto">${s.kind!=='router'&&s.health==='ok'?`<a class="btn go" href="${play(s.port,s.models[0]?.id)}">chat</a> `:''}${lan?`<a class="btn" target="mbwebui" href="http://${location.hostname}:${s.port}/">web ui</a> `:''}
   ${bench?`<button onclick="act('unload',{port:${ld.port}})">unload</button>`:s.locked?`<span class="ok" title="locked with model-lock: nothing can swap it out or unload it">&#128274; locked</span> <button onclick="act('lock',{id:'${esc(s.swap_id)}',on:0})">unlock</button>`:`${s.swap_id?`<button onclick="act('lock',{id:'${esc(s.swap_id)}',on:1})" title="pin it: nothing can swap it out or unload it until you unlock it">lock</button> `:''}<button class="bad" onclick="uiConfirm('Stop ${esc(s.model)} (pid ${s.pid})?',{title:'Stop server',ok:'Stop',danger:1}).then(y=>{if(y)act('killpid',{pid:${s.pid}})})">stop</button>`}</div></div>`}).join('')}
async function load(){const q=$('ctx').value?('&ctx='+$('ctx').value):'';let j;try{j=await (await fetch('/api/state?x=1'+q,{headers:H})).json()}catch(e){$('st').textContent='bench unreachable';return}
 if(first){first=false}const s=j.state;
 $('st').innerHTML=(s.busy?`<span class="warn">&#9679; ${esc(s.phase)}</span> ${esc(s.model||'')} <span class="dim">${esc(s.detail||'')} ${s.queue?('| queued '+s.queue):''}</span>`:'<span class="ok">&#9679; idle</span>')+(j.notice?` <span class="warn">${esc(j.notice)}</span>`:'');
 $('host').textContent=`commit ${f(j.host.commit_gb)} GB | VRAM used ${f(j.adapter_gb)} of ${f(j.vram)} GB`;const vf=+j.budget;$('ro-vram').textContent=f(vf);$('ro-vram').style.color=vf<4?'var(--r)':(vf<10?'var(--y)':'var(--g)');$('ro-vram-sub').textContent='of '+f(j.vram)+' ('+f(j.adapter_gb)+' used)';const rf=+j.host.avail_gb;$('ro-ram').textContent=f(rf);$('ro-ram').style.color=rf<2?'var(--r)':(rf<4?'var(--y)':'var(--t)');$('ro-ram-sub').textContent='of '+f(j.host.total_gb);{const lp=new Set([...(j.servers||[]).filter(s=>s.kind!=='router').map(s=>s.port),...(j.loaded||[]).map(x=>x.port)].filter(Boolean));$('ro-loaded').textContent=lp.size;$('ro-loaded').title='Models running on the box, including llama-swap ones. Bench slots used: '+(j.loaded||[]).length+(j.max_loaded?(' of '+j.max_loaded):'');}
 $('running').innerHTML=running(j)+((j.loaded||[]).filter(x=>!x.alive).map(x=>`<div class="card no">model ${esc(x.name)} died: ${esc(x.killed||'exited')} <span class="dim">cleaning up automatically</span></div>`).join(''));
 $('rows').innerHTML=j.models.map((m,mi)=>{const r=m.last||{};const fit=m.est_gb<=j.budget;const v=r.verdict||'';const vc=r.ok?(r.released===false?'warn':'ok'):(v?'no':'dim');const isLoaded=(j.loaded||[]).some(x=>x.name===m.name);const rowcls=isLoaded?'row-loaded':(r.ok?'row-pass':(v?'row-fail':'row-untested'));const badge=(m.shared?'<span class="sbadge" style="background:#1f2a3a;color:#8fb2d6" title="'+esc(m.shared.why)+' - LOAD opens a chat on that copy instead of starting a second one">llama-swap: '+esc(m.shared.id)+'</span> ':(m.elsewhere?'<span class="sbadge" style="background:#1f2a3a;color:#8fb2d6" title="a copy of this model is already loaded, so a second one cannot be started">running in '+esc(m.elsewhere.owner)+' :'+m.elsewhere.port+'</span> ':''))+(m.downloading?'<span class="sbadge" style="background:#3a3218;color:#e8cf7a" title="cannot be loaded until the download finishes">'+(m.downloading=='downloading'?'downloading':'incomplete download')+'</span> ':'')+(isLoaded?'<span class="sbadge sb-loaded">loaded</span>':(r.ok?'<span class="sbadge sb-pass">tested '+(r.score||'')+'</span>':(v?'<span class="sbadge sb-fail">failed</span>':'<span class="sbadge sb-untested">untested</span>')))+(m.new&&!isLoaded?' <span class="sbadge sb-new">new</span>':'');
 const canL=!(m.infra||s.busy||!fit||m.downloading||(m.elsewhere&&!m.shared)||(j.loaded&&j.loaded.length>=j.max_loaded));const isL=(j.loaded||[]).some(x=>x.name===m.name);const why=m.infra?'reranker - not a chat model':isL?'already loaded - chat with it in the playground':m.elsewhere?('already running in '+m.elsewhere.owner+' (port '+m.elsewhere.port+') - use that copy instead of loading a second one'):m.downloading?'still downloading - it can be loaded once the download finishes':s.busy?'bench busy - a load or test is running':!fit?'does not fit in the free VRAM':'already at the max number of loaded models - unload one first';return (mi===0&&m.task_employed?'<tr class="te-sep"><td colspan="14">&#128274; Task Employed - worker models (locked)</td></tr>':(mi>0&&!m.task_employed&&j.models[mi-1].task_employed?'<tr class="te-sep"><td colspan="14">Other models</td></tr>':''))+`<tr class="${rowcls}${m.task_employed?' te':''} clk" data-id="${m.id}" data-ok="${canL&&!isL?1:0}" data-why="${esc(why)}" title="${canL&&!isL?'Click to LOAD':esc(why)}"><td class="name">${m.task_employed?'<span class="sbadge sb-te" title="worker model used by the automated services - locked, do not unload">&#128274; Task Employed</span> ':''}${m.pinned?'<span class="sbadge sb-loaded">pinned</span> ':''}${esc(m.name)}${(()=>{const sv=(j.servers||[]).find(x=>x.kind!=='router'&&x.model===m.name);const te=!sv&&m.task_employed&&m.path?(j.task_employed||[]).find(t=>t.port&&t.file===m.path.split('/').pop()):null;const pt=sv?sv.port:(te?te.port:null);return pt?`<span class="spk" data-port="${pt}"></span>`:''})()}${badge} <span class="dim" title="times used in team tasks / score. Success adds 1, failure subtracts 1">tasks ${m.tasks_used||0}/${m.tasks_score||0}</span>${m.duplicate?' <span class="dim">(dup)</span>':''}<div class="dim">added ${fmtAdded(m.added)} | ${esc(m.arch)} | native ctx ${m.native_ctx??'?'} | ${$('ctx').value?('loads at '+$('ctx').value):(m.auto_ctx?('loads at '+m.auto_ctx):'won\'t fit now')} | ${esc(m.folder)}</div></td>
 <td class="n">${f(m.size_gb)}</td><td class="n">${f(m.est_gb)}</td><td class="${fit?'ok':'no'}">${fit?'yes':'no'}</td>
 <td class="${vc}" style="white-space:normal;max-width:260px">${esc(v)}</td><td class="n">${f(r.load_s)}</td><td class="n">${f(r.pp_tps)}</td><td class="n">${f(r.tg_tps)}</td>
 <td class="n">${f(r.peak_ded_gb)}</td><td class="n ${r.peak_shared_gb>1?'warn':''}">${f(r.peak_shared_gb)}</td><td class="n">${f(r.min_avail_ram_gb)}</td>
 <td class="${r.released===false?'no':'ok'}">${r.released==null?'':(r.released?'yes':'NO')}</td><td>${r.score||''}</td>
 <td><button onclick="act('pin',{id:'${m.id}',on:${m.pinned?0:1}})">${m.pinned?'unpin':'pin'}</button> <button onclick="act('run',{id:'${m.id}'})" ${m.infra||s.busy||m.downloading||m.elsewhere||(j.loaded&&j.loaded.length)?'disabled':''}>test</button> <button class="go" onclick="act('load',{id:'${m.id}',tools:$('tools').checked})" ${m.infra||s.busy||!fit||m.downloading||(m.elsewhere&&!m.shared)||(j.loaded&&j.loaded.length>=j.max_loaded)?'disabled':''}>LOAD</button> ${r.id?`<button onclick="det(${r.id})">details</button>`:''}</td></tr>`}).join('');mfilter()}
function fmtAdded(t){if(!t)return '?';return new Date(t*1000).toLocaleString('en-GB',{day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'})}
function mfilter(){const el=$('msearch');if(!el)return;const raw=el.value;try{localStorage.setItem('mb_msearch',raw)}catch(e){}const toks=raw.trim().toLowerCase().split(/\s+/).filter(Boolean);let n=0,t=0;document.querySelectorAll('#rows>tr:not(.te-sep)').forEach(tr=>{t++;const td=tr.querySelector('td.name');const hay=(td?td.textContent:tr.textContent).toLowerCase();const ok=toks.every(k=>hay.includes(k));tr.style.display=ok?'':'none';if(ok)n++});$('mcount').textContent=toks.length?(n+' of '+t+' shown'):(t+' models')}
(function(){const el=$('msearch');if(!el)return;try{el.value=localStorage.getItem('mb_msearch')||''}catch(e){}el.addEventListener('keydown',e=>{if(e.key==='Escape'){el.value='';mfilter();el.blur()}});document.addEventListener('keydown',e=>{const a=document.activeElement;if(e.key==='/'&&!(a&&(/INPUT|TEXTAREA|SELECT/.test(a.tagName)||a.isContentEditable))){e.preventDefault();el.focus();el.select();el.scrollIntoView({block:'center',behavior:'smooth'})}})})();
let SPK={};const SPN=90;
function sparkSvg(d){if(!d||!d.tps||!d.tps.length)return '';const w=110,h=22,t=d.tps,b=d.busy||[],mx=Math.max(20,...t),st=w/(SPN-1),x0=w-(t.length-1)*st;
const pts=t.map((v,i)=>(x0+i*st).toFixed(1)+','+(h-3-(v/mx)*(h-6)).toFixed(1)).join(' ');
const bars=b.map((v,i)=>v?`<rect x="${(x0+i*st-st/2).toFixed(1)}" y="${h-2}" width="${st.toFixed(1)}" height="2" fill="#e0a052"/>`:'').join('');
const now=t[t.length-1],on=b[b.length-1];
return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="vertical-align:middle;background:rgba(127,127,127,.08);border-radius:3px"><polyline points="${pts}" fill="none" stroke="#6aa6e0" stroke-width="1.5" stroke-linejoin="round"/>${bars}</svg><span class="dim" style="min-width:58px;font-size:11px">${on?(now?now.toFixed(0)+' tok/s':'thinking'):'idle'}</span>`}
function drawSparks(){document.querySelectorAll('.spk').forEach(el=>{const d=SPK[el.dataset.port];el.innerHTML=sparkSvg(d);el.title=d?('last 90 s: tokens/s (line), busy (orange) - peak '+Math.max(...d.tps).toFixed(0)+' tok/s'):''})}
async function pollSpark(){if(document.hidden)return;try{SPK=await (await fetch('/api/spark',{headers:H})).json()}catch(e){}drawSparks()}
{const _ld=load;load=async function(){const r=await _ld.apply(this,arguments);drawSparks();return r}}load();setInterval(load,3000);pollSpark();setInterval(pollSpark,1000);
</script></body></html>""".replace("__CSS__", CSS)
PLAY = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>playground - __HOST__</title><script>try{var t=localStorage.getItem('mb_theme');if(t==='gray'||t==='pro')document.documentElement.dataset.pal=t;if(localStorage.getItem('mb_side')==='0')document.documentElement.dataset.side='off';if(localStorage.getItem('mb_showtools')==='0')document.documentElement.dataset.tools='off';if(localStorage.getItem('mb_panel')==='1')document.documentElement.dataset.panel='on'}catch(e){}</script><style>__CSS__
:root{--bg:#1b1a18;--p:#211f1c;--p2:#2a2724;--l:#33302b;--l2:#45403a;--t:#ece7de;--m:#9c968c;--ac:#d97757;--acs:rgba(217,119,87,.14);--g:#86b98f;--y:#d8aa55;--r:#e27d6f;--b:#8fb2d6;--code:#161513;--ui:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,sans-serif;--mono:"Cascadia Code","Cascadia Mono","JetBrains Mono",Consolas,ui-monospace,monospace}
html[data-pal="gray"]{--bg:#0c0c0c;--p:#131313;--p2:#1c1c1c;--l:#262626;--l2:#363636;--t:#d8d8d8;--m:#8a8a8a;--ac:#cfcfcf;--acs:rgba(207,207,207,.1);--g:#c8c8c8;--y:#a8a8a8;--r:#cfa3a3;--b:#aaaaaa;--code:#080808}
html[data-pal="pro"]{--bg:#0a0c0a;--p:#0f130f;--p2:#161d16;--l:#1e2a1e;--l2:#2c3d2c;--t:#d7e0d4;--m:#7d8c7a;--ac:#7CFF68;--acs:rgba(124,255,104,.1);--g:#7CFF68;--y:#E6B84A;--r:#C45C4A;--b:#7aa2c4;--code:#070907}
html,body{height:100%}
body{margin:0;padding:0;background:var(--bg);color:var(--t);font:14px/1.55 var(--ui);display:grid;grid-template-rows:44px minmax(0,1fr) 24px;overflow:hidden}
button,a.btn,select,input,textarea{font-family:inherit;font-size:13px}
button,a.btn{background:transparent;color:var(--t);border:1px solid var(--l2);border-radius:6px;padding:4px 11px;line-height:1.4;cursor:pointer;box-shadow:none}
button:hover,a.btn:hover{background:var(--p2);border-color:var(--l2);color:var(--t)}
button:disabled,button:disabled:hover{opacity:.38;cursor:default;background:transparent}
button.go{background:var(--ac);border-color:var(--ac);color:#1b1a18;font-weight:600}
button.go:hover{background:var(--ac);filter:brightness(1.08);color:#1b1a18}
button.go:disabled,button.go:disabled:hover{background:var(--ac);filter:none}
button.bad{color:var(--r);border-color:var(--l2);background:transparent}
button.bad:hover{border-color:var(--r);background:transparent}
input,select,textarea{background:var(--code);color:var(--t);border:1px solid var(--l);border-radius:6px;padding:4px 8px}
input[type="checkbox"]{accent-color:var(--ac);width:auto;margin:0}
:focus-visible{outline:2px solid var(--ac);outline-offset:1px}
.dim{color:var(--m);font-size:12px}
/* title bar */
.top{display:flex;align-items:center;gap:10px;padding:0 12px;background:var(--p);border-bottom:1px solid var(--l);min-width:0;white-space:nowrap}
.top .ib.side-t{font-size:15px;padding:2px 8px}
.brand{color:var(--t);text-decoration:none;font-weight:600;display:flex;align-items:center;gap:8px}
.brand::before{content:"";width:10px;height:10px;border-radius:3px;background:var(--ac)}
.brand:hover{color:var(--ac)}
.crumb{color:var(--m)}
.pick{display:flex;align-items:center;gap:6px;margin-left:12px;min-width:0;padding-left:14px;border-left:1px solid var(--l)}
.pick select{max-width:300px;background:var(--bg);border-color:var(--l);font-family:var(--mono);font-size:12px;padding:4px 6px}
.top .grow{flex:1}
#teambtn.on{color:var(--ac);border-color:var(--ac)}
.dd>.pop{background:var(--p);border:1px solid var(--l2);border-radius:10px;padding:14px;width:320px;box-shadow:0 16px 40px rgba(0,0,0,.45);white-space:normal;font-size:13px}
.dd .pop label{color:var(--m)}
.dd .pop select,.dd .pop input:not([type]){width:100%}
/* workspace */
.ws{display:flex;min-height:0;min-width:0}
.side{flex:0 0 300px;background:var(--p);border-right:1px solid var(--l);overflow:auto;font-size:13px;display:flex;flex-direction:column}
html[data-side="off"] .side{display:none}
.sec{border-bottom:1px solid var(--l)}
.sec>summary{list-style:none;cursor:pointer;padding:10px 14px;font-weight:600;font-size:12.5px;display:flex;align-items:center;gap:8px;user-select:none}
.sec>summary::-webkit-details-marker{display:none}
.sec>summary::before{content:"";border-left:5px solid var(--m);border-top:4px solid transparent;border-bottom:4px solid transparent;transition:transform .12s}
.sec[open]>summary::before{transform:rotate(90deg)}
.sec>summary:hover{background:var(--p2)}
.sec>summary .hint{margin-left:auto;font-weight:400;color:var(--m);font-family:var(--mono);font-size:11px;overflow:hidden;text-overflow:ellipsis;max-width:130px}
.sbody{padding:0 14px 14px}
#ctxcur{font-family:var(--mono);font-size:12px;word-break:break-all;margin:0 0 10px;line-height:1.45}
#ctxcur.ok{color:var(--t)}
.row{display:flex;gap:6px;flex-wrap:wrap}
.tl{color:var(--m);margin-bottom:8px;line-height:1.45}
.tl b{color:var(--g);font-weight:600}
.tools{display:flex;flex-wrap:wrap;gap:5px}
.chip{font-family:var(--mono);font-size:11px;padding:1px 8px;border:1px solid var(--l2);border-radius:999px;color:var(--t);background:var(--bg)}
.grid2{display:grid;grid-template-columns:1fr 96px;gap:8px 12px;align-items:center}
.grid2 label{color:var(--m)}
.grid2 input,.grid2 select{width:100%;font-family:var(--mono);font-size:12px;text-align:right}
.chk{display:flex;gap:8px;align-items:center;color:var(--m);margin-top:12px;cursor:pointer}
.side textarea{width:100%;font-family:var(--mono);font-size:12px;line-height:1.5;resize:vertical;min-height:140px;padding:8px}
.side .under{margin-top:8px}
#chglist{font-size:12.5px}
.chg{margin:0;padding:8px 0;border-bottom:1px solid var(--l)}
.chg:last-child{border-bottom:0}
.chg .dim{font-family:var(--mono);font-size:11px;margin-top:2px}
.chg button{margin-top:6px;padding:2px 9px;font-size:12px}
.sfoot{margin-top:auto;padding:12px 14px;display:flex;gap:8px;align-items:center;border-top:1px solid var(--l)}
.sfoot select{flex:1;font-size:12px}
/* transcript */
.main{flex:1;display:flex;flex-direction:column;min-height:0;min-width:0}
#log{flex:1;overflow:auto;padding:28px 24px 8px;padding-right:24px}
#log>*{max-width:860px;margin-left:auto;margin-right:auto}
.msg{background:none;border:0;border-radius:0;padding:0;margin-bottom:26px;white-space:normal}
.msg.u .ub{display:flex;gap:10px;background:var(--p);border:1px solid var(--l);border-radius:12px;padding:11px 15px;font-family:var(--mono);font-size:13px;line-height:1.55}
.msg.u .pr{color:var(--ac);font-weight:700;user-select:none}
.msg.u .ut{white-space:pre-wrap;word-wrap:break-word;min-width:0;flex:1}
.mh{display:flex;align-items:center;gap:9px;margin-bottom:6px;font-size:12.5px;min-height:24px;min-width:0}
.mh .av{flex:0 0 8px;height:8px;border-radius:50%;background:var(--g)}
.msg.e .av{background:var(--r)}.msg.rc .av{background:var(--y)}
.mh .mn{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:46%}
.mh .mm{color:var(--m);font-family:var(--mono);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mh .ma{margin-left:auto;opacity:0;transition:opacity .12s}
.msg:hover .ma,.msg:focus-within .ma{opacity:1}
.ib{border:0;background:none;padding:2px 8px;color:var(--m);font-size:12px;border-radius:5px}
.ib:hover{color:var(--t);background:var(--p2)}
.msg .md{font-size:14.5px;line-height:1.68;padding-left:17px;overflow-wrap:anywhere}
.msg.e .md{color:var(--r)}
.msg.rc .md{border-left:2px solid var(--y);padding-left:15px}
.md .h1,.md .h2{font-size:16px;font-weight:600;color:var(--t);border-bottom:1px solid var(--l);padding-bottom:4px;margin:18px 0 8px}
.md .h3,.md .h4{font-size:14.5px;font-weight:600;color:var(--t);margin:14px 0 6px}
.md .p{margin:4px 0}.md .sp{height:8px}
.md ul,.md ol{margin:6px 0 6px 22px}.md li{margin:3px 0}
.md code{background:var(--code);border:1px solid var(--l);border-radius:4px;padding:0 5px;color:var(--t);font-family:var(--mono);font-size:12.5px}
table.mdt{font-size:13px}
table.mdt th{background:var(--p2);text-transform:none;letter-spacing:0;font-size:12.5px;font-weight:600;border-color:var(--l)}
table.mdt td{border-color:var(--l)}
.cb{border:1px solid var(--l);border-radius:9px;background:var(--code);margin:10px 0;overflow:hidden}
.cbh{display:flex;align-items:center;justify-content:space-between;padding:3px 6px 3px 12px;background:var(--p);border-bottom:1px solid var(--l);font-family:var(--mono);font-size:11px;color:var(--m)}
.cb pre.code,pre.code{margin:0;border:0;border-radius:0;background:transparent;padding:11px 13px;font-family:var(--mono);font-size:12.5px;line-height:1.55;overflow:auto;white-space:pre}
.tline{font-family:var(--mono);font-size:12px;line-height:1.5;color:var(--b);background:var(--code);border:1px solid var(--l);border-bottom:0;padding:5px 10px;white-space:pre-wrap;overflow-wrap:anywhere;margin-top:6px;border-radius:7px 7px 0 0}
.tline.res{color:var(--m);border-bottom:1px solid var(--l);border-top-style:dashed;margin-top:0;border-radius:0 0 7px 7px}
.msg.u pre.code{background:var(--code);border:1px solid var(--l);border-radius:8px;margin:6px 0}
html[data-tools="off"] .tline,html[data-tools="off"] .md>.sp:first-child,html[data-tools="off"] .tline+.sp{display:none}
.tcount{font-family:var(--mono);font-size:11px;color:var(--m);border:1px solid var(--l2);border-radius:999px;padding:0 8px;line-height:18px;background:none;white-space:nowrap}
.tcount:hover{color:var(--t);border-color:var(--ac);background:none}
html:not([data-tools="off"]) .tcount{display:none}
.think{margin:0 0 10px 17px;border:0;padding:0;color:var(--m)}
.tt{border:0;background:none;padding:2px 0;color:var(--m);font-size:12.5px}
.tt:hover{background:none;color:var(--t)}
.tt::before{content:"\25B8  ";font-size:10px}.think.open .tt::before{content:"\25BE  "}
.tbody{margin-top:6px;padding:8px 14px;border-left:2px solid var(--l2);font-size:13px;line-height:1.55;white-space:pre-wrap;max-height:340px;overflow:auto}
.gen{display:inline-flex;gap:4px;align-items:center;height:20px;padding-left:17px}
.gen.sm{padding-left:2px;height:12px}
.gen i{width:6px;height:6px;border-radius:50%;background:var(--ac);opacity:.25;animation:mbp 1.1s infinite ease-in-out}
.gen.sm i{width:4px;height:4px}
.gen i:nth-child(2){animation-delay:.16s}.gen i:nth-child(3){animation-delay:.32s}
@keyframes mbp{0%,80%,100%{opacity:.25}40%{opacity:1}}
.empty{max-width:640px;padding-top:12vh}
.eh{font-family:var(--mono);font-size:24px;font-weight:600;letter-spacing:-.01em;display:flex;align-items:center;gap:10px;overflow-wrap:anywhere}
.eh .blk{flex:0 0 11px;height:24px;background:var(--ac);animation:mbblink 1.1s steps(1) infinite}
.es{color:var(--m);margin:8px 0 22px;max-width:56ch}
.sugg{display:grid;gap:8px}
.sugg button{text-align:left;padding:11px 15px;border-radius:10px;background:var(--p);border-color:var(--l);font-size:13.5px}
.sugg button:hover{border-color:var(--ac);background:var(--p)}
/* team report */
.report{background:var(--p);border:1px solid var(--l);border-left:3px solid var(--g);border-radius:10px;margin-bottom:26px}
.mbsp{animation:mbspin .85s linear infinite;flex:none}@keyframes mbspin{to{transform:rotate(360deg)}}
.mbbusy{display:inline-flex;align-items:center;gap:6px;margin-left:10px;color:var(--ac);font-size:12px;font-weight:500}
.mbbusyrow{display:flex;align-items:center;gap:9px;color:var(--ac);font-size:13px;padding:8px 0}.mbbusyrow .mbsp{width:20px;height:20px}
.sbbusy{display:inline-flex;align-items:center;gap:6px;color:var(--ac);font-weight:600;margin:0 10px}.sbbusy:empty{display:none}
#send .mbsp{vertical-align:-3px;margin-right:3px}
.rhead{background:var(--p2);border-bottom:1px solid var(--l);padding:8px 12px}
.rtitle{letter-spacing:0;font-size:13px;font-weight:600}
.rbadge{letter-spacing:0;font-size:11px;font-weight:600;border-radius:999px}
.rtask{border-bottom:1px solid var(--l);font-size:13px;padding:8px 12px}
.rbody{padding:10px 16px}
.racts{background:var(--p);border-top:1px solid var(--l)}
.rnote{border-top:1px solid var(--l)}
.rcopy{border:0;color:var(--m)}
/* processes */
#tasks{max-width:908px;width:100%;margin:0 auto;padding:0 24px;max-height:150px;overflow:auto}
#tasks .tk{background:var(--p);border:1px solid var(--l);border-left-width:3px;border-radius:7px;font-family:var(--mono);font-size:12px;padding:5px 10px}
#tasks .tk.run{border-left-color:var(--y)}#tasks .tk.done{border-left-color:var(--g)}#tasks .tk.fail{border-left-color:var(--r)}#tasks .tk.plan{border-left-color:var(--b)}#tasks .tk.stopped{border-left-color:var(--m)}
/* composer */
.composer{padding:8px 24px 14px}
.cbox{max-width:860px;margin:0 auto;background:var(--p);border:1px solid var(--l2);border-radius:14px;padding:10px 12px 8px;transition:border-color .12s}
.cbox:focus-within{border-color:var(--ac)}
.cbox textarea{display:block;width:100%;background:transparent;border:0;outline:none;resize:none;font-family:var(--mono);font-size:13.5px;line-height:1.55;min-height:54px;max-height:260px;padding:2px 2px 0}
.cbox textarea:focus-visible{outline:none}
.crow{display:flex;align-items:center;gap:8px;margin-top:6px}
.hints{color:var(--m);font-size:11.5px;display:flex;gap:12px;flex-wrap:wrap;flex:1}
kbd{font-family:var(--mono);font-size:10.5px;border:1px solid var(--l2);border-bottom-width:2px;border-radius:4px;padding:0 4px;color:var(--t);margin-right:3px}
#send{padding:5px 16px}#stop{margin-left:auto}
/* bottom panel: terminal, activity, jobs */
.panel{display:none;flex:0 0 var(--ph,280px);flex-direction:column;min-height:0;border-top:1px solid var(--l2);background:var(--code);position:relative}
html[data-panel="on"] .panel{display:flex}
.pgrip{position:absolute;left:0;right:0;top:-4px;height:8px;cursor:ns-resize;z-index:5}
.phead{display:flex;align-items:center;gap:2px;padding:0 8px;height:34px;flex:0 0 34px;background:var(--p);border-bottom:1px solid var(--l)}
.ptab{border:0;border-bottom:2px solid transparent;border-radius:0;padding:7px 11px 6px;color:var(--m);font-size:12.5px;background:none}
.ptab:hover{color:var(--t);background:none}
.ptab.on{color:var(--t);border-bottom-color:var(--ac)}
.bd{display:inline-block;min-width:16px;padding:0 5px;margin-left:4px;border-radius:999px;background:var(--y);color:#1b1a18;font-size:10.5px;font-weight:700;line-height:16px;text-align:center}
.bd:empty{display:none}
.phead .grow{flex:1}
.phead select{font-size:12px;padding:2px 6px;background:var(--bg);font-family:var(--mono)}
.pbody{flex:1;min-height:0;overflow:auto;font-family:var(--mono);font-size:12.5px;line-height:1.5}
#termout{margin:0;padding:8px 14px 0;white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;color:var(--t)}
.tprompt{color:var(--ac)}.terr{color:var(--r)}.tdim{color:var(--m)}
.tin{display:flex;align-items:center;gap:8px;padding:0 14px 10px}
.tin input{flex:1;min-width:0;background:transparent;border:0;outline:none;font:inherit;color:var(--t);padding:0}
.tin input:focus-visible{outline:none}
.arow{display:grid;grid-template-columns:62px minmax(90px,170px) 64px minmax(0,1fr) 64px;gap:10px;padding:4px 14px;border-bottom:1px solid var(--l);cursor:pointer;align-items:baseline}
.arow:hover{background:var(--p)}
.arow .w,.arow .d{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.arow .w{color:var(--t)}.arow .t{color:var(--m)}
.hb{font-size:10.5px;border:1px solid var(--l2);border-radius:4px;padding:0 5px;text-align:center;color:var(--b)}
.hb.peer{color:var(--ac);border-color:var(--ac)}#sbhost.bad{color:var(--r);font-weight:600}.twarn{color:var(--r);font-weight:600}.hb.web{color:var(--g)}
.st{text-align:right}.st.run{color:var(--y)}.st.done{color:var(--g)}.st.fail{color:var(--r)}
.ares{grid-column:1/-1;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--m);padding:4px 0 8px;max-height:260px;overflow:auto}
.jrow{display:grid;grid-template-columns:62px 110px 90px 64px minmax(0,1fr);gap:10px;padding:4px 14px;border-bottom:1px solid var(--l);align-items:baseline}
.jrow .d{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.jrow .ses{color:var(--m);overflow:hidden;text-overflow:ellipsis}
.jrow.Running .s,.jrow.Queued .s{color:var(--y)}.jrow.Completed .s{color:var(--g)}.jrow.Failed .s,.jrow.TimedOut .s{color:var(--r)}.jrow.Cancelled .s{color:var(--m)}
.pnote{padding:6px 14px;color:var(--m);border-bottom:1px solid var(--l);font-size:12px}
.pempty{padding:14px;color:var(--m)}
.sbb{border:0;background:none;padding:0 6px;font:inherit;color:var(--t);border-radius:3px}
.sbb:hover{background:var(--p2)}
#sbact{color:var(--y)}
@media (max-width:860px){.arow{grid-template-columns:54px minmax(0,1fr) 58px}.arow .t,.arow .hb{display:none}.jrow{grid-template-columns:54px 80px minmax(0,1fr)}.jrow .ses,.jrow .du{display:none}}
/* status bar */
.sbar{display:flex;align-items:center;gap:18px;padding:0 12px;background:var(--p);border-top:1px solid var(--l);font-family:var(--mono);font-size:11px;color:var(--m);white-space:nowrap;overflow:hidden}
.sbar .grow{flex:1}
.sbar #sbdir{overflow:hidden;text-overflow:ellipsis;min-width:0}
.sbar .ok{color:var(--g)}
/* folder picker */
#browser{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:60}
.bdlg{max-width:680px;margin:8vh auto;background:var(--p);border:1px solid var(--l2);border-radius:12px;display:flex;flex-direction:column;max-height:78vh;box-shadow:0 24px 60px rgba(0,0,0,.5);overflow:hidden}
.bhead{display:flex;gap:8px;align-items:center;padding:12px 14px;border-bottom:1px solid var(--l)}
.bhead b{font-weight:600}
#bpath{flex:1;font-family:var(--mono);font-size:12px;color:var(--m);word-break:break-all}
#blist{overflow:auto;font-family:var(--mono);font-size:12.5px}
.brow{padding:7px 14px;cursor:pointer;border-bottom:1px solid var(--l)}
.brow:hover{background:var(--p2)}
.bfile{padding:6px 14px;color:var(--m);border-bottom:1px solid var(--l)}
@media (max-width:860px){
 .side{position:fixed;top:44px;bottom:24px;left:0;width:min(320px,88vw);z-index:40;box-shadow:12px 0 40px rgba(0,0,0,.45);transform:translateX(-104%);transition:transform .16s}
 html[data-side] .side{display:flex}
 html[data-side="on"] .side{transform:none}
 .hints{display:none}.crumb{display:none}.brand{font-size:0;gap:0}.top{gap:6px;padding:0 8px}.top .grow{display:none}
 .pick{flex:1 1 auto;margin-left:0;padding-left:8px}.pick select{flex:1 1 0;min-width:0;max-width:none}#mdl{display:none}
 #log{padding:18px 14px 8px}.composer{padding:8px 12px 12px}#tasks{padding:0 12px}
}
@media (prefers-reduced-motion:reduce){.gen i,.eh .blk{animation:none;opacity:1}.side{transition:none}}

/* sidebar: aligned label/control grid, compact */
.sec>summary{font-family:var(--mono);text-transform:uppercase;letter-spacing:.1em;font-size:11px;font-weight:600;color:var(--m);padding:9px 14px}
.sec[open]>summary{color:var(--t)}
.sbody{padding:0 14px 12px}
.kv{display:grid;grid-template-columns:78px minmax(0,1fr);gap:7px 10px;align-items:center}
.kv>label{font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--m);white-space:nowrap}
.kv select,.kv input:not([type=checkbox]){width:100%;min-width:0;height:28px;font-family:var(--mono);font-size:12px;padding:3px 6px}
.fld{display:flex;align-items:center;gap:4px;min-width:0}
.fld #ctxcur{flex:1;min-width:0;margin:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;word-break:normal;font-size:11.5px}
.ic{flex:none;width:28px;height:28px;padding:0;display:inline-flex;align-items:center;justify-content:center;font-size:13px;line-height:1}
.tgl{display:flex;gap:14px;flex-wrap:wrap}.tgl .chk{margin:0;font-size:12px}
.tlist>summary{cursor:pointer;list-style:none;font-family:var(--mono);font-size:12px;color:var(--m);user-select:none}
.tlist>summary::-webkit-details-marker{display:none}
.tlist>summary::after{content:" \25BE"}.tlist[open]>summary::after{content:" \25B4"}
.tlist b{color:var(--g);font-weight:600}.tlist .tools{margin-top:8px}
#sys{margin-top:8px;min-height:110px}
.chg{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:1px 8px;padding:6px 0}
.chg>div{grid-column:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chg button{grid-column:2;grid-row:1/span 2;margin:0;padding:2px 9px;font-size:11.5px}
.sfoot{padding:10px 14px;gap:6px}.sfoot button{font-size:12px;padding:4px 10px;white-space:nowrap}
/* panel tabs: capitals, terminal font */
.ptab{font-family:var(--mono);text-transform:uppercase;letter-spacing:.09em;font-size:11px}

/* sidebar sections as raised panels */
.side{background:var(--bg);padding:10px 10px 0;gap:10px}
.sec{border:1px solid var(--l);border-top-color:rgba(255,255,255,.10);border-bottom-color:rgba(0,0,0,.55);border-radius:10px;
 background:linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,0) 60%),var(--p);
 box-shadow:0 1px 0 rgba(255,255,255,.04) inset,0 2px 1px rgba(0,0,0,.4),0 6px 14px rgba(0,0,0,.3);flex:none}
.sec>summary{border-radius:10px}
.sec>summary:hover{background:rgba(255,255,255,.035)}
.sec[open]>summary{border-bottom:1px solid var(--l);border-radius:10px 10px 0 0;margin-bottom:10px}
.sfoot{margin:auto -10px 0;background:var(--p)}
</style><style>
.uimask{position:fixed;inset:0;background:rgba(0,0,0,.55);backdrop-filter:blur(2px);display:flex;align-items:center;justify-content:center;z-index:99999;animation:uifade .12s ease}
.uibox{min-width:300px;max-width:min(520px,92vw);background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,0)),var(--p,#1a1d22);color:var(--t,#d8dde3);border:1px solid var(--l,#2a2f36);border-top-color:rgba(255,255,255,.16);border-radius:12px;box-shadow:0 18px 50px rgba(0,0,0,.6),0 2px 6px rgba(0,0,0,.5);padding:16px 18px 14px;animation:uipop .14s ease;font-family:var(--ui,inherit)}
.uibox h3{margin:0 0 10px;font-family:"Cascadia Code","Cascadia Mono","JetBrains Mono",Consolas,ui-monospace,monospace;font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.09em;color:var(--t,#d8dde3)}
.uibox .uimsg{white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.45;max-height:50vh;overflow:auto}
.uibox .uibtns{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
.uibox button{padding:6px 14px;border-radius:7px;border:1px solid var(--l,#2a2f36);background:rgba(255,255,255,.06);color:var(--t,#d8dde3);cursor:pointer;font:inherit;font-size:13px}
.uibox button:hover{background:rgba(255,255,255,.11)}
.uibox button.uiok{background:var(--ac,var(--b,#6aa6e0));border-color:transparent;color:var(--bg,#111316);font-weight:600}
.uibox button.uiok.danger{background:var(--r,#e06c6c)}
.uibox button.uiok:hover{filter:brightness(1.1)}
.uibox button:focus-visible{outline:2px solid var(--ac,var(--b,#6aa6e0));outline-offset:2px}
@keyframes uifade{from{opacity:0}}@keyframes uipop{from{opacity:0;transform:translateY(6px) scale(.97)}}
</style>
<script>
function uiDialog(msg,o){o=o||{};return new Promise(function(res){var prev=document.activeElement,m=document.createElement('div'),b=document.createElement('div'),bt=document.createElement('div'),ok,c;
 m.className='uimask';b.className='uibox';b.setAttribute('role','dialog');b.setAttribute('aria-modal','true');bt.className='uibtns';
 if(o.title){var h=document.createElement('h3');h.textContent=o.title;b.appendChild(h)}
 var p=document.createElement('div');p.className='uimsg';p.textContent=String(msg==null?'':msg);b.appendChild(p);
 function done(v){document.removeEventListener('keydown',kd,true);m.remove();try{if(prev&&prev.focus)prev.focus()}catch(e){}res(v)}
 if(o.cancel!==false){c=document.createElement('button');c.textContent=o.cancelText||'Cancel';c.onclick=function(){done(false)};bt.appendChild(c)}
 ok=document.createElement('button');ok.className='uiok'+(o.danger?' danger':'');ok.textContent=o.ok||'OK';ok.onclick=function(){done(true)};bt.appendChild(ok);
 b.appendChild(bt);m.appendChild(b);m.addEventListener('mousedown',function(e){if(e.target===m)done(o.cancel===false)});
 function kd(e){var f=[].slice.call(bt.querySelectorAll('button')),i=f.indexOf(document.activeElement);
  if(e.key==='Escape'){e.preventDefault();e.stopPropagation();done(o.cancel===false)}
  else if(e.key==='Enter'){e.preventDefault();e.stopPropagation();(i>=0?f[i]:ok).click()}
  else if(e.key==='Tab'){e.preventDefault();e.stopPropagation();f[((i<0?0:i)+(e.shiftKey?-1:1)+f.length)%f.length].focus()}
  else{e.stopPropagation()}}
 document.addEventListener('keydown',kd,true);document.body.appendChild(m);((o.danger&&c)?c:ok).focus()})}
function uiAlert(msg,o){return uiDialog(msg,Object.assign({cancel:false},o||{}))}
function uiConfirm(msg,o){return uiDialog(msg,o||{})}
window.alert=function(m){uiAlert(m)};
</script>
</head><body>
<header class="top">
<button class="ib side-t" onclick="toggleSide()" title="Show or hide the sidebar (Ctrl+B)" aria-label="Toggle sidebar">&#9776;</button>
<a class="brand" id="benchlink" title="Back to the bench">modelbench</a><span class="crumb">Classic chat · tools on __HOST__</span><a class="btn" href="/play">CODING WORKSPACE</a>
<div class="pick"><select id="tgt" title="Server"></select><select id="mdl" title="Model on that server"></select></div>
<span class="grow"></span>
<span class="dd" id="teamdd"><button id="teambtn" onclick="toggleDD()">TEAM &#9662;</button><div class="pop" onclick="event.stopPropagation()"><label style="color:var(--t);margin-top:0;display:flex;gap:8px;align-items:flex-start"><input type="checkbox" id="team" style="margin-top:4px"><span><b>Team mode</b><br><span class="dim">The coordinator splits the task, workers run the pieces in parallel, then the coordinator combines them.</span></span></label><label style="color:var(--t);margin-top:8px;display:flex;gap:8px;align-items:flex-start"><input type="checkbox" id="overlord" style="margin-top:4px"><span><b>Overlord mode</b><br><span class="dim">A supervisor turns your request into a precise instruction, watches every step the worker takes, redirects it if it drifts, and reports back - including any problems it flags. Defaults to Mistral Medium (cloud) - top class, cheap, about half a second per check. Claude Haiku 4.5 and Sonnet 5.5 (Anthropic, your ANTHROPIC_API_KEY) are in Coordinator too.</span></span></label><label>Coordinator</label><select id="coord"></select><label>Workers</label><div id="workers"></div><label>Max pieces</label><input id="maxsub" value="4" style="width:70px"></div></span>
<button onclick="clearContext()" title="Start an empty conversation (Ctrl+L)">NEW</button>
</header>
<div class="ws">
<aside class="side">
<details class="sec" open><summary>Workspace</summary><div class="sbody"><div class="kv"><label for="wshost">Machine</label><select id="wshost" onchange="wsHost(this.value)" title="Which PC the working folder, agent commands and terminal use. Defaults to the PC this browser is on."><option value="local">__HOST__</option></select><label>Folder</label><div class="fld"><span id="ctxcur" class="dim">no folder - models can reach every PC</span><button class="ic" onclick="openBrowser()" title="Choose folder">&#128193;</button><button class="ic" onclick="clearCtx()" title="Forget the working folder">&#10005;</button></div></div></div></details>
<details class="sec" open><summary>Tools</summary><div class="sbody"><div id="toolbox"></div></div></details>
<details class="sec" open><summary>Generation</summary><div class="sbody"><div class="kv"><label for="temp">Temp</label><select id="temp" title="Temperature"><option value="0">0</option><option value="0.2">0.2</option><option value="0.4">0.4</option><option value="0.6">0.6</option><option value="0.7" selected>0.7</option><option value="0.8">0.8</option><option value="1.0">1.0</option><option value="1.2">1.2</option></select><label for="topp">Top p</label><select id="topp"><option value="0.8">0.8</option><option value="0.9">0.9</option><option value="0.95" selected>0.95</option><option value="1.0">1.0</option></select><label for="maxt">Max tokens</label><select id="maxt"><option value="512">512</option><option value="1024">1024</option><option value="2048" selected>2048</option><option value="4096">4096</option><option value="8192">8192</option><option value="16384">16384</option></select><label for="thk">Think</label><select id="thk" title="Let the model reason before answering. Off is much faster; On shows its reasoning in the Thought process panel."><option value="off" selected>Off - fast</option><option value="on">On - slower</option></select><label>Show</label><div class="tgl"><label class="chk"><input type="checkbox" id="think" checked> Reasoning</label><label class="chk"><input type="checkbox" id="showtools" checked onchange="setShowTools(this.checked)"> Tool calls</label></div></div></div></details>
<details class="sec"><summary>System prompt</summary><div class="sbody"><div class="kv"><label for="sysp">Preset</label><select id="sysp" onchange="if(this.value==='tool'){$('sys').value=DEFAULT_SYS}else if(this.value==='none'){$('sys').value=''}this.value='';savePlay()"><option value="">choose&hellip;</option><option value="tool">Tool prompt</option><option value="none">Empty</option></select></div><textarea id="sys" rows="7" placeholder="Optional instructions sent before every message"></textarea></div></details>
<details class="sec" open><summary>Changes<span class="hint">last 5 code states</span></summary><div class="sbody"><div id="chglist" class="dim">no saved changes yet</div></div></details>
<div class="sfoot"><select id="theme" onchange="setTheme(this.value)" title="Colour theme, shared with the bench"><option value="current">Claude dark</option><option value="gray">Terminal gray</option><option value="pro">Terminal pro</option></select><button onclick="copyAll(this)" title="Copy the whole conversation">Copy transcript</button></div>
</aside>
<main class="main"><div id="log"></div><div id="tasks"></div>
<div class="composer"><div class="cbox"><textarea id="in" rows="2" placeholder="Message the model"></textarea><div class="crow"><span class="hints"><span><kbd>Enter</kbd>send</span><span><kbd>Shift</kbd><kbd>Enter</kbd>new line</span><span><kbd>&uarr;</kbd>history</span><span><kbd>Esc</kbd>stop</span></span><button id="stop" onclick="stopGen()" disabled>Stop</button><button class="go" id="send" onclick="send()">Send</button></div></div></div>
<section class="panel" id="panel"><div class="pgrip" id="pgrip" title="Drag to resize"></div>
<div class="phead"><button class="ptab" data-t="term" onclick="showTab('term')">Terminal</button><button class="ptab" data-t="act" onclick="showTab('act')">Activity<span class="bd" id="actbd"></span></button><button class="ptab" data-t="jobs" onclick="showTab('jobs')">Jobs</button><button class="ptab" data-t="info" onclick="showTab('info')">Info</button><span class="grow"></span><select id="phost" onchange="setHost(this.value)" title="Machine"><option value="local">this PC</option></select><button class="ib" id="tstop" onclick="termStop()" disabled title="Stop the running command (Ctrl+C)">Stop</button><button class="ib" onclick="panelClear()" title="Clear this view">Clear</button><button class="ib" onclick="togglePanel(false)" title="Close the panel (Ctrl+`)" aria-label="Close panel">&#10005;</button></div>
<div class="pbody pview" data-t="term" id="termwrap"><pre id="termout"></pre><div class="tin"><span class="tprompt" id="tps"></span><input id="tcmd" autocomplete="off" spellcheck="false" aria-label="Terminal command"></div></div>
<div class="pbody pview" data-t="act" id="actv"></div>
<div class="pbody pview" data-t="jobs" id="jobsv"></div>
<div class="pbody pview" data-t="info" id="infov"></div>
</section>
</main></div>
<footer class="sbar"><button class="sbb" onclick="togglePanel()" title="Terminal, agent activity and jobs (Ctrl+`)">&#9638; Terminal</button><a class="sbb hflink" target="_blank" rel="noopener" href="#" style="text-decoration:none" title="Search Hugging Face and download models onto the box">&#43; Get models</a><span id="sbbusy" class="sbbusy"></span><span id="sbact"></span><span id="caps" class="dim"></span><span id="stats"></span><span class="grow"></span><span id="sbdir"></span><span id="sbhost" title="The machine this IDE runs on, detected at startup">checking machines...</span></footer>
<div id="browser" onclick="if(event.target===this)closeBrowser()"><div class="bdlg"><div class="bhead"><b>Working folder</b><span id="bpath">/</span><button id="buse" class="go" onclick="useBrowsed()">Use this folder</button><button onclick="closeBrowser()">Close</button></div><div id="blist"></div></div></div>
<script>
const REMOTE=false;const P=new URLSearchParams(location.search);const K=P.get('key')||'';const H={'Content-Type':'application/json','X-Key':K};
const $=id=>document.getElementById(id);
function setTheme(v){if(v==='gray'||v==='pro')document.documentElement.dataset.pal=v;else delete document.documentElement.dataset.pal;try{localStorage.setItem('mb_theme',v||'current')}catch(e){}}
try{$('theme').value=localStorage.getItem('mb_theme')||'current'}catch(e){}
function toggleDD(){$('teamdd').classList.toggle('open')}
document.addEventListener('click',e=>{const dd=$('teamdd');if(dd&&!dd.contains(e.target))dd.classList.remove('open')});
document.addEventListener('change',e=>{
  if(e.target&&e.target.id==='team'){const on=e.target.checked;if(on&&$('overlord'))$('overlord').checked=false;$('teambtn').classList.toggle('on',on||($('overlord')&&$('overlord').checked));$('teambtn').innerHTML=(($('overlord')&&$('overlord').checked)?'OVERLORD: ON':(on?'TEAM: ON':'TEAM'))+' \u25be'}
  if(e.target&&e.target.id==='overlord'){const on=e.target.checked;if(on){$('team').checked=false;if(!window.__ovCoordTouched){const c=overlordPick(window.__lastSv||[],window.__lastRm||[]);if(c)$('coord').value=c}}$('teambtn').classList.toggle('on',on||$('team').checked);$('teambtn').innerHTML=(on?'OVERLORD: ON':($('team').checked?'TEAM: ON':'TEAM'))+' \u25be'}
  if(e.target&&e.target.id==='coord'){window.__ovCoordTouched=true}
});

let ctxDir='';let browsePath='';
const WS={host:'local',dir:'',set:false};const ME={machine:null,ip:''};
try{const s=JSON.parse(localStorage.getItem('mb_ws')||'null');if(s&&s.host){WS.host=s.host;WS.dir=s.dir||'';WS.set=true;H['X-WS-Host']=WS.host;H['X-WS-Dir']=encodeURIComponent(WS.dir)}}catch(e){}
function wsApply(){H['X-WS-Host']=WS.host;H['X-WS-Dir']=encodeURIComponent(WS.dir||'');ctxDir=WS.dir||'';const nm=hostName(WS.host);
 $('ctxcur').textContent=ctxDir?(ctxDir+' on '+nm):('no folder on '+nm+' - models can reach every PC');$('ctxcur').className=ctxDir?'ok':'dim';$('ctxcur').title=$('ctxcur').textContent;
 const sel=$('wshost');if(sel&&[...sel.options].some(o=>o.value===WS.host))sel.value=WS.host;sbDir()}
function wsSave(){WS.set=true;try{localStorage.setItem('mb_ws',JSON.stringify({host:WS.host,dir:WS.dir}))}catch(e){}wsApply()}
function wsFill(){const sel=$('wshost');if(!sel)return;const opts=[['local',MB.local||'__HOST__']].concat((MB.peers||[]).map(p=>[p.id,p.name+(p.ok?'':' (offline)')]));
 if(!opts.some(o=>o[0]===WS.host))opts.push([WS.host,String(WS.host).toUpperCase()+' (unknown)']);
 sel.innerHTML=opts.map(o=>'<option value="'+esc(o[0])+'">'+esc(o[1])+(o[0]===ME.machine?' (this device)':'')+'</option>').join('');sel.value=WS.host}
async function loadCtx(){try{const j=await (await fetch('/api/whoami',{headers:H})).json();ME.machine=j.machine||null;ME.ip=j.ip||''}catch(e){}
 if(!WS.set){WS.host=ME.machine||'local';WS.dir='';
  if(WS.host==='local'){try{const j=await (await fetch('/api/context',{headers:{'X-Key':K}})).json();WS.dir=j.server_dir||''}catch(e){}}
  wsSave()}
 wsFill();wsApply();loadChanges();if(MB.loaded)setHost(WS.host)}
function wsHost(h){if(h===WS.host)return;WS.host=h;WS.dir='';wsSave();loadChanges();if(MB.loaded)setHost(h);openBrowser()}
function clearCtx(){setCtx('');}
async function loadChanges(){try{const j=await (await fetch('/api/changes',{headers:H})).json();const rows=j.changes||[];$('chglist').innerHTML=rows.length?rows.map(function(c){return '<div class="chg"><div>'+esc(c.label)+'</div><div class="dim">'+esc(c.when)+' · '+c.nfiles+' files'+(c.current?' · current':'')+'</div>'+(c.current?'':'<button onclick="revertTo(\''+c.id+'\')">Revert</button>')+'</div>'}).join(''):'no saved changes yet';$('chglist').className=rows.length?'':'dim'}catch(e){}}
async function revertTo(id){if(!(await uiConfirm('Revert the working folder to this saved code state?',{title:'Revert',ok:'Revert',danger:1})))return;const r=await fetch('/api/revert',{method:'POST',headers:H,body:JSON.stringify({id:id})});const j=await r.json().catch(function(){return {}});if(!r.ok||j.error){uiAlert(j.error||'revert failed',{title:'Revert'});return}loadChanges()}
async function setCtx(d){try{const r=await fetch('/api/context',{method:'POST',headers:H,body:JSON.stringify({dir:d,host:WS.host})});const j=await r.json();if(!r.ok||j.error){uiAlert(j.error||'could not set folder',{title:'Working folder'});return}
 WS.host=j.host||WS.host;WS.dir=j.dir||'';wsSave();loadChanges();closeBrowser();PNL.cwd[WS.host]='';if(MB.loaded)setHost(WS.host)}catch(e){uiAlert('could not set folder',{title:'Working folder'})}}
function openBrowser(){$('browser').style.display='block';browseTo(ctxDir||'');}
function closeBrowser(){$('browser').style.display='none';}
function useBrowsed(){setCtx(browsePath);}
function childPath(base, name){
 if(/^[A-Za-z]:\\/.test(name) || name.indexOf('\\\\')===0) return name;
 const root = (base||'').replace(/[\\/]+$/,'');
 return (root ? root+'\\' : '') + name;
}
async function browseTo(path){let j;try{j=await (await fetch('/api/browse?host='+encodeURIComponent(WS.host)+'&path='+encodeURIComponent(path),{headers:H})).json();}catch(e){$('blist').innerHTML='<div class="no" style="padding:8px">error reading folder</div>';return}
 if(j.error){$('blist').innerHTML='<div class="no" style="padding:8px">'+esc(j.error)+'</div>';return}
 browsePath=j.path||'';
 const usable=/^[A-Za-z]:\\/.test(browsePath) || browsePath.indexOf('\\\\')===0;
 $('bpath').textContent=browsePath?(browsePath+' on '+hostName(WS.host)):(hostName(WS.host)+' (pick a drive)');
 $('buse').style.display=usable?'inline-block':'none';
 const row=(label, dest)=>`<div class="brow" onclick="browseTo(${JSON.stringify(dest).replace(/&/g,'&amp;').replace(/\"/g,'&quot;')})">${label}</div>`;
 let rows='';
 if(browsePath){rows+=row('&#128193; '+esc(hostName(WS.host)),''); if(j.parent) rows+=row('&#8598; ..', j.parent);}
 rows+=(j.dirs||[]).map(d=>row('&#128193; '+esc(d), childPath(browsePath, d))).join('');
 rows+=(j.files||[]).map(f=>`<div class="bfile">&#128196; ${esc(f)}</div>`).join('');
 $('blist').innerHTML=rows||'<div class="dim" style="padding:8px">(empty)</div>';}
let DEFAULT_SYS=REMOTE?"You control __HOST__ (a Windows PC) through tools. For ANYTHING about that computer - files, folders, drives, processes, apps, running a command - call aissh6_aissh_run with one PowerShell command. When a working folder is selected, commands already start there: use relative paths such as Get-ChildItem . and do not Set-Location away from it or scan other drives unless the user named that path. PowerShell only. For anything on the internet call web_search, then web_fetch to read a result. One tool per step. Put real values in arguments, never placeholders. After a tool returns, answer in plain text using the result; do not repeat the same call.":"You can use two tools: web_search to search the internet and web_fetch to read a web page. Use them for anything current or anything you are not sure about, then answer in plain text from what they returned. One tool per step. Put real values in arguments, never placeholders. Do not repeat the same call.";
$('benchlink').href='/?key='+encodeURIComponent(K);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt=t=>esc(t).replace(/```[\w+-]*\n?([\s\S]*?)```/g,(m,c)=>`<pre class="code">${c}</pre>`);
function mdInline(t){return t.replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')}
function fmtMd(src){const out=[];const L=esc(src||'').split('\n');let i=0;
 const isRow=x=>/^\s*\|.*\|\s*$/.test(x);const row=x=>x.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>c.trim());
 while(i<L.length){const l=L[i];let m;
  if(/^\s*```/.test(l)){const lang=l.trim().slice(3).trim()||'text';const b=[];i++;while(i<L.length&&!/^\s*```/.test(L[i])){b.push(L[i]);i++}i++;out.push(codeBlock(b.join('\n'),lang));continue}
  if(isRow(l)&&i+1<L.length&&/^\s*\|?[\s:|-]+\|?\s*$/.test(L[i+1])&&L[i+1].indexOf('-')>=0){const h=row(l);i+=2;const bd=[];while(i<L.length&&isRow(L[i])){bd.push(row(L[i]));i++}
   out.push('<div class="tblw"><table class="mdt"><thead><tr>'+h.map(x=>'<th>'+mdInline(x)+'</th>').join('')+'</tr></thead><tbody>'+bd.map(r=>'<tr>'+r.map(c=>'<td>'+mdInline(c)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>');continue}
  if((m=l.match(/^(#{1,4})\s+(.*)$/))){out.push('<div class="h'+m[1].length+'">'+mdInline(m[2])+'</div>');i++;continue}
  if(/^\s*([-*]|\d+[.)])\s+/.test(l)){const ord=/^\s*\d+[.)]/.test(l);const it=[];while(i<L.length&&/^\s*([-*]|\d+[.)])\s+/.test(L[i])){it.push(L[i].replace(/^\s*([-*]|\d+[.)])\s+/,''));i++}
   out.push((ord?'<ol>':'<ul>')+it.map(x=>'<li>'+mdInline(x)+'</li>').join('')+(ord?'</ol>':'</ul>'));continue}
  if(/^\s*(---|\*\*\*)\s*$/.test(l)){out.push('<hr>');i++;continue}
  if(/^\[tool:/.test(l)){out.push('<div class="tline">'+l+'</div>');i++;continue}
  if(/^\[result\]/.test(l)){const rb=[l];i++;while(i<L.length&&L[i].trim()){rb.push(L[i]);i++}out.push('<div class="tline res">'+rb.join('\n')+'</div>');continue}
  if(!l.trim()){out.push('<div class="sp"></div>');i++;continue}
  out.push('<div class="p">'+mdInline(l)+'</div>');i++}
 return out.join('')}
function copyText(t,btn){const done=()=>{if(btn){const o=btn.textContent;btn.textContent='Copied';setTimeout(()=>{btn.textContent=o},1200)}};
 if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(done,()=>fallbackCopy(t,done))}else fallbackCopy(t,done)}
function fallbackCopy(t,done){const a=document.createElement('textarea');a.value=t;a.style.position='fixed';a.style.opacity='0';document.body.appendChild(a);a.select();try{document.execCommand('copy');done()}catch(e){}a.remove()}
function cpCode(btn){copyText(btn.closest('.cb').querySelector('pre').innerText,btn)}
function copyMsg(ix,btn){const m=msgs[ix];if(m)copyText(m.content||'',btn)}
function toggleReason(ix){const m=msgs[ix];if(m){m.rOpen=!m.rOpen;render()}}
function codeBlock(h,lang){return '<div class="cb"><div class="cbh"><span>'+lang+'</span><button class="ib" onclick="cpCode(this)">Copy</button></div><pre class="code">'+h+'</pre></div>'}
function ntc(t){return (String(t||'').match(/^\[tool:/gm)||[]).length}
function setShowTools(on){const d=document.documentElement;if(on)delete d.dataset.tools;else d.dataset.tools='off';try{localStorage.setItem('mb_showtools',on?'1':'0')}catch(e){}const c=$('showtools');if(c)c.checked=!!on}
function sbDir(){const s=$('sbdir');if(s)s.textContent=(ctxDir?('folder '+ctxDir+' on '):'no folder on ')+hostName(WS.host)}
function toggleSide(){const d=document.documentElement;if(matchMedia('(max-width:860px)').matches){if(d.dataset.side==='on')delete d.dataset.side;else d.dataset.side='on';return}
 if(d.dataset.side==='off'){delete d.dataset.side;try{localStorage.setItem('mb_side','1')}catch(e){}}else{d.dataset.side='off';try{localStorage.setItem('mb_side','0')}catch(e){}}}
let msgs=[],ac=null;
const SPIN='<svg class="mbsp" viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.34-5.66" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/><path d="M20.5 3.5v5h-5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
let busyT0=0,baseTitle='';
function busyText(){const s=busyT0?Math.floor((performance.now()-busyT0)/1000):0;const m=msgs[msgs.length-1]||{};const c=m.content||'';
 const lt=c.lastIndexOf('[tool: '),lr=c.lastIndexOf('[result]');let ph='working';
 if(lt>lr){ph='running '+(c.slice(lt+7).split(/[\s\]]/)[0]||'tool')}else if(!c&&m.reason){ph='thinking'}else if(c&&m.role!=='user'){ph='writing'}
 return ph+' \u00b7 '+(s>=60?Math.floor(s/60)+'m '+(s%60)+'s':s+'s')}
setInterval(function(){const busy=!!ac;if(busy&&!busyT0)busyT0=performance.now();if(!busy)busyT0=0;if(!baseTitle)baseTitle=document.title;
 const sb=document.getElementById('sbbusy'),sd=document.getElementById('send');
 if(busy){const t=busyText();document.querySelectorAll('[data-busy]').forEach(function(e){e.textContent=t});
  if(sb)sb.innerHTML=SPIN+'<span>'+esc(t)+'</span>';if(document.title.indexOf('\u27f3')!==0)document.title='\u27f3 '+baseTitle;
  if(sd&&!sd.dataset.working){sd.dataset.working='1';sd.innerHTML=SPIN+' Working'}}
 else{if(sb&&sb.innerHTML)sb.innerHTML='';if(document.title!==baseTitle)document.title=baseTitle;if(sd&&sd.dataset.working){delete sd.dataset.working;sd.textContent='Send'}}},250);
(function(){try{const g=JSON.parse(localStorage.getItem('mb_gen')||'{}');['temp','topp','maxt','thk'].forEach(function(k){const el=document.getElementById(k);if(!el)return;
 if(g[k]!=null){if(![].some.call(el.options,function(o){return o.value==g[k]}))el.add(new Option(g[k],g[k]));el.value=g[k]}
 el.addEventListener('change',function(){try{const o=JSON.parse(localStorage.getItem('mb_gen')||'{}');o[k]=el.value;localStorage.setItem('mb_gen',JSON.stringify(o))}catch(e){}})})}catch(e){}})();
let TASKS=[];let taskTimer=null;
function tkStart(){if(!taskTimer)taskTimer=setInterval(renderTasks,1000)}
function tkAdd(o){o.t0=performance.now();o.state=o.state||'run';o.started=o.started||Date.now();TASKS.push(o);tkStart();renderTasks();return o}
function tkEnd(o,state,secs){if(!o)return;o.state=state;o.fixed=(secs!=null?secs:((performance.now()-o.t0)/1000).toFixed(1));renderTasks();if(!TASKS.some(x=>x.state==='run')&&taskTimer){clearInterval(taskTimer);taskTimer=null}}
function tkReset(){TASKS=[];if(taskTimer){clearInterval(taskTimer);taskTimer=null}renderTasks()}
function fmtEl(s){s=Math.max(0,Math.round(s));return s<60?s+'s':Math.floor(s/60)+'m'+String(s%60).padStart(2,'0')}
function renderTasks(){const box=$('tasks');if(!box)return;box.classList.toggle('on',TASKS.length>0);
 box.innerHTML=TASKS.map(o=>{const el=o.fixed!=null?+o.fixed:((performance.now()-o.t0)/1000);
  const started=new Date(o.started).toLocaleTimeString();
  return '<div class="tk '+(o.cls||o.state)+'"><span class="who">'+esc(o.who)+'</span><span class="desc">'+esc(o.desc||'')+'</span>'+
   '<span class="dim" style="font-size:11px">start '+started+'</span><span class="el">'+(o.fixed!=null?o.fixed+'s':fmtEl(el))+(o.state==='stopped'?' stopped':'')+'</span></div>'}).join('');savePlay()}

let cmdHist=[];try{cmdHist=JSON.parse(localStorage.getItem('mb_cmdhist')||'[]')}catch(e){}
let histIdx=cmdHist.length,histDraft='';
function autoGrow(){const el=$('in');el.style.height='auto';el.style.height=Math.min(Math.max(el.scrollHeight,54),260)+'px'}
function firstLine(el){return el.value.slice(0,el.selectionStart).indexOf('\n')<0}
function lastLine(el){return el.value.slice(el.selectionStart).indexOf('\n')<0}
function toEnd(){const el=$('in');const p=el.value.length;el.setSelectionRange(p,p);el.scrollTop=el.scrollHeight}
function histPrev(){if(!cmdHist.length)return;if(histIdx===cmdHist.length)histDraft=$('in').value;if(histIdx>0)histIdx--;$('in').value=cmdHist[histIdx]||'';autoGrow();toEnd()}
function histNext(){if(histIdx>=cmdHist.length)return;histIdx++;$('in').value=(histIdx===cmdHist.length)?histDraft:cmdHist[histIdx];autoGrow();toEnd()}
function pushHist(t){if(t&&cmdHist[cmdHist.length-1]!==t){cmdHist.push(t);if(cmdHist.length>200)cmdHist.shift();try{localStorage.setItem('mb_cmdhist',JSON.stringify(cmdHist))}catch(e){}}histIdx=cmdHist.length;histDraft=''}
function applyRec(k){const r=k&&k.rec;window.__rec=r||null;if(!r)return;const key='v2:'+k.name+'@'+k.port+'#'+k.ctx;let last='';try{last=localStorage.getItem('mb_gen_model')||''}catch(e){}
 if(last===key)return;   // same model as before: keep the user's own tweaks
 const setSel=(id,v)=>{const el=document.getElementById(id);if(!el||v==null)return;const n=Number(v);let o=[].find.call(el.options,x=>Number(x.value)===n);if(!o){o=new Option(String(v),String(v));el.add(o)}el.value=o.value};
 setSel('temp',r.temp);setSel('topp',r.top_p);setSel('maxt',r.max_tokens);
 try{localStorage.setItem('mb_gen_model',key);localStorage.setItem('mb_gen',JSON.stringify({temp:$('temp').value,topp:$('topp').value,maxt:$('maxt').value}))}catch(e){}
 const st=document.getElementById('stats');if(st)st.textContent='auto: ctx '+k.ctx+' | max '+r.max_tokens+' | temp '+r.temp+' | top p '+r.top_p+' | top k '+r.top_k+' ('+r.source+')'}
async function drawTools(k){const tb=document.getElementById('toolbox');if(!tb)return;if(k&&k.tools&&!$('sys').value.trim())$('sys').value=DEFAULT_SYS;if(!k||!k.tools){tb.innerHTML='<div class="tl">Off for this model. Load it on the bench with <b>tools</b> ticked to give it web search.</div>';return;}let names=[];try{const port=document.getElementById('tgt').value;const r=await fetch('/api/tools?port='+port,{headers:H});const j=await r.json();names=(j.tools||[]).map(x=>x.name).filter(Boolean);}catch(e){}tb.innerHTML='<details class="tlist"><summary><b>On</b> &middot; '+(names.length?names.length+' tools':'web tools')+'</summary><div class="tools">'+names.map(n=>'<span class="chip">'+esc(n)+'</span>').join('')+'</div></details>';}
function overlordPick(sv,rm){const r=(rm||[]).find(x=>x.id==='mistral-medium'&&x.ready);return r?r.id:cheapestLoaded(sv)}
function cheapestLoaded(sv){let best=null,bestSize=Infinity;(sv||[]).forEach(s=>{const sz=(window.__modelSizes||{})[s.model];if(sz!=null&&sz<bestSize){bestSize=sz;best=s}});return best?String(best.port):((sv&&sv[0])?String(sv[0].port):'')}
async function targets(){const j=await (await fetch('/api/state',{headers:H})).json();const sv=j.servers.filter(s=>s.kind!=='router-child'&&s.health==='ok'&&s.chat_capable!==false);window.__lastSv=sv;window.__lastRm=j.remotes||[];window.__modelSizes={};(j.models||[]).forEach(m=>{window.__modelSizes[m.name]=m.size_gb});
 const want=$('tgt').value||P.get('port');window.__n=sv.length;fillTeam(sv,j.remotes||[]);$('tgt').innerHTML=sv.map(s=>`<option value="${s.port}" ${String(s.port)===want?'selected':''}>:${s.port} ${esc(s.kind==='router'?'router':s.model)}</option>`).join('')||'<option value="">nothing running - load one from the bench</option>';
 const k=(j.loaded||[]).find(l=>String(l.port)===String($('tgt').value))||j.kept;window.__ctx=k?k.ctx:0;applyRec(k);$('caps').textContent=k?('ctx '+k.ctx+(k.tools?' | tools ON':' | no tools')):'no model loaded';$('caps').className=k&&k.tools?'ok':'dim';drawTools(k);if(!msgs.length)render();
 await models()}
async function models(){const port=$('tgt').value;if(!port){$('mdl').innerHTML='';return}
 const j=await (await fetch('/api/models?port='+port,{headers:H})).json();const want=P.get('model');
 $('mdl').innerHTML=(j.data||[]).filter(m=>!m.status||m.status.value==='loaded').map(m=>`<option ${m.id===want?'selected':''}>${esc(m.id)}</option>`).join('')}
$('tgt').onchange=()=>{models();targets();if(!msgs.length)render()};
const SUGG=REMOTE?['List the files in the working folder and tell me what the project does','What is using the most memory on this PC right now?','Search the web for the latest llama.cpp release notes']:['Search the web for the latest llama.cpp release and summarise what changed','Explain mixture-of-experts models in two paragraphs','Write a Python function that checks whether a string is a palindrome'];
function useSugg(i){$('in').value=SUGG[i];autoGrow();$('in').focus();savePlay()}
function nearBottom(){const l=$('log');return l.scrollHeight-l.scrollTop-l.clientHeight<90}
function emptyState(){const o=$('tgt').selectedOptions[0];const has=!!(o&&o.value);const name=has?o.text.replace(/^:\d+\s*/,''):'No model running';
 return '<div class="empty"><div class="eh"><span>'+esc(name)+'</span><span class="blk"></span></div><div class="es">'+(has?(REMOTE?'Ask anything. With tools on it can run PowerShell on '+esc(MB.local||'the PC')+' and work in the chosen folder.':'Ask anything. With tools on it can search the web and read pages.'):'Load a model on the bench, then come back here to chat with it.')+'</div>'+(has?'<div class="sugg">'+SUGG.map((s,i)=>'<button onclick="useSugg('+i+')">'+esc(s)+'</button>').join('')+'</div>':'')+'</div>'}
function render(){const lg=$('log');const stick=nearBottom()||(msgs.length&&msgs[msgs.length-1].role==='user');
 if(!msgs.length){lg.innerHTML=emptyState();savePlay();return}
 const live=!!ac;
 lg.innerHTML=msgs.map((m,ix)=>{
 if(m.report){const r=m.report;const lab=r.status==='ok'?'Completed':(r.status==='partial'?'Completed after recovery':'Incomplete');
  const recs=reportRecs(m.content);const gate=!r.decision&&recs?'<div class="racts"><button class="go" onclick="goAhead('+ix+')">Go Ahead</button> <button class="bad" onclick="dontGo('+ix+')">Don\'t</button></div>':(r.decision==='go'?'<div class="rnote">Go Ahead — the team is applying these fixes.</div>':(r.decision==='dont'?'<div class="rnote">Don\'t — cancelled. No fixes will be made.</div>':''));
  return '<div class="report '+r.status+'"><div class="rhead"><span class="rtitle">Team report</span><span class="rbadge '+r.status+'">'+lab+'</span><span class="rmeta">'+esc(r.coord)+' &middot; '+r.workers+' worker(s) &middot; '+r.pieces+' piece(s) &middot; '+r.total+'s total'+(r.recovered?' &middot; '+r.recovered+' recovered':'')+(r.unresolved?' &middot; '+r.unresolved+' unresolved':'')+' &middot; '+esc(r.when)+'</span><button class="rcopy" onclick="copyText(msgs['+ix+'].content,this)">copy</button></div><div class="rtask"><span class="dim">Task&nbsp;</span> '+esc(r.task)+'</div><div class="md rbody">'+fmtMd(m.content)+'</div>'+gate+'</div>'}
 if(m.role==='user')return '<div class="msg u"><div class="ub"><span class="pr">&gt;</span><div class="ut">'+fmt(m.content)+'</div></div></div>';
 const cls=m.err?'e':'a';const streaming=live&&ix===msgs.length-1;const name=m.tag||m.model||'assistant';
 let th='';if(m.reason&&$('think').checked){const w=m.reason.trim().split(/\s+/).length;const lab=(streaming&&!m.content)?'Thinking':'Thought process';
  th='<div class="think'+(m.rOpen?' open':'')+'"><button class="tt" onclick="toggleReason('+ix+')">'+lab+' <span class="dim">'+w+' words</span></button>'+(m.rOpen?'<div class="tbody">'+esc(m.reason)+'</div>':'')+'</div>'}
 const body=(!m.content&&streaming)?'<div class="mbbusyrow">'+SPIN+'<span data-busy>'+esc(busyText())+'</span></div>':'<div class="md">'+fmtMd(m.content)+'</div>';
 return '<div class="msg '+cls+(m.rc?' rc':'')+'"><div class="mh"><span class="av"></span><span class="mn">'+esc(name)+'</span>'+(streaming&&m.content?'<span class="mbbusy">'+SPIN+'<span data-busy>'+esc(busyText())+'</span></span>':'')+(m.meta?'<span class="mm">'+esc(m.meta)+'</span>':'')+(ntc(m.content)?'<button class="tcount" onclick="setShowTools(true)" title="Show tool calls">'+ntc(m.content)+' tool call'+(ntc(m.content)>1?'s':'')+' hidden</button>':'')+'<span class="ma"><button class="ib" onclick="copyMsg('+ix+',this)">Copy</button></span></div>'+th+body+'</div>'}).join('');
 if(stick)lg.scrollTop=lg.scrollHeight;savePlay()}
function clearContext(){if(ac){ac.abort();ac=null}msgs=[];tkReset();$('stats').textContent='';$('in').value='';autoGrow();$('send').disabled=false;$('stop').disabled=true;render();savePlayNow()}
function clearChat(){clearContext()}
function copyAll(btn){copyText(msgs.map(m=>`## ${m.role}\n${m.content}`).join('\n\n'),btn)}
function stopGen(){if(ac)ac.abort()}
$('in').addEventListener('input',function(){autoGrow();savePlay()});
$('sys').addEventListener('input',savePlay);
$('in').addEventListener('keydown',e=>{
 if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();return}
 if(e.key==='ArrowUp'&&(e.ctrlKey||firstLine($('in')))){e.preventDefault();histPrev();return}
 if(e.key==='ArrowDown'&&(e.ctrlKey||lastLine($('in')))){e.preventDefault();histNext();return}
 if(e.key==='Escape'){e.preventDefault();if(ac){stopGen()}else{$('in').value='';histIdx=cmdHist.length;autoGrow();savePlay()}return}
 if((e.ctrlKey||e.metaKey)&&(e.key==='l'||e.key==='L')){e.preventDefault();clearChat();return}
});
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='/'){e.preventDefault();$('in').focus()}if((e.ctrlKey||e.metaKey)&&(e.key==='b'||e.key==='B')){e.preventDefault();toggleSide()}if(e.ctrlKey&&(e.key==='`'||e.code==='Backquote')){e.preventDefault();togglePanel()}});
async function sendOne(port,out,body,tag){body=Object.assign({},body);const t0=performance.now();let tFirst=0,n=0,tm=null;try{const r=await fetch('/api/chat?port='+port,{method:'POST',headers:H,body:JSON.stringify(body),signal:ac.signal});if(!r.ok){out.err=true;out.content=(tag?tag+' ':'')+'HTTP '+r.status+': '+await r.text();render();return}const rd=r.body.getReader(),dec=new TextDecoder();let buf='';for(;;){const {done,value}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});let i;while((i=buf.indexOf('\n'))>=0){const line=buf.slice(0,i).trim();buf=buf.slice(i+1);if(!line.startsWith('data:'))continue;const d=line.slice(5).trim();if(d==='[DONE]')continue;let j;try{j=JSON.parse(d)}catch{continue}if(j.error){out.err=true;out.content+='\n[error] '+(j.error.message||JSON.stringify(j.error));continue}const dl=(j.choices&&j.choices[0]&&j.choices[0].delta)||{};if(dl.reasoning_content)out.reason+=dl.reasoning_content;if(dl.content){if(!tFirst)tFirst=performance.now();out.content+=dl.content;n++}if(j.timings)tm=j.timings}render()}}catch(e){if(e.name!=='AbortError'){out.err=true;out.content+='\n['+e.message+']'}else{out.content+='\n[stopped]'}}if(tm){const acc=tm.draft_n?(' | MTP '+Math.round(100*(tm.draft_n_accepted||0)/tm.draft_n)+'% accepted'):'';out.meta=(tag?tag+' | ':'')+(tm.predicted_per_second||0).toFixed(1)+' t/s | '+tm.predicted_n+' tok | prompt '+tm.prompt_n+' tok @ '+(tm.prompt_per_second||0).toFixed(0)+' t/s'+acc;if(!tag)$('stats').textContent='last reply '+(tm.predicted_per_second||0).toFixed(1)+' t/s'+acc;}render();}
function setTeamOn(on){const box=$('team');if(!box)return;box.checked=!!on;const b=$('teambtn');if(!b)return;b.classList.toggle('on',!!on);b.innerHTML=(on?'TEAM: ON':'TEAM')+' \u25be'}
function fillTeam(sv,rm){rm=rm||[];const ports=sv.map(s=>String(s.port));const key=ports.slice().sort().join(',');const fresh=key!==(window.__teamKey||'');window.__teamKey=key;const multi=sv.length>1;if(fresh)setTeamOn(multi);
 let prefer=$('coord').value||'';
 if($('overlord')&&$('overlord').checked&&!window.__ovCoordTouched){const c=overlordPick(sv,rm);if(c)prefer=c}
 else if(fresh&&multi)prefer=ports[0];
 else if(!prefer)prefer=((rm.find(r=>r.ready)||{}).id)||ports[0]||'';
 $('coord').innerHTML=rm.map(r=>`<option value="${r.id}" ${r.id===prefer?'selected':''}>${esc(r.label)} - ${esc(r.model)}${r.ready?'':' (no key yet)'}</option>`).join('')+sv.map(s=>`<option value="${s.port}" ${String(s.port)===prefer?'selected':''}>:${s.port} ${esc(s.model)}</option>`).join('');
 const c=$('coord').value;const prevW=new Set([...document.querySelectorAll('.wk:checked')].map(x=>x.value));
 $('workers').innerHTML=sv.map(s=>{const on=fresh?(multi||sv.length===1||String(s.port)!==c):prevW.has(String(s.port));return `<label style="display:block;margin:2px 0;color:var(--t)"><input type="checkbox" class="wk" value="${s.port}" style="width:auto" ${on?'checked':''}> :${s.port} ${esc(s.model)}</label>`}).join('')||'<span class="dim">load models on the bench first</span>'}
async function send(){const text=$('in').value.trim();if(!text||ac)return;
 if($('overlord')&&$('overlord').checked)return sendOverlord(text);
 if($('team').checked)return sendTeam(text);
 const port=$('tgt').value;if(!port)return;pushHist(text);$('in').value='';autoGrow();
 const sys=$('sys').value.trim();msgs.push({role:'user',content:text});
 const base=(sys?[{role:'system',content:sys}]:[]).concat(msgs.filter(m=>!m.err&&!m.aux&&(m.role==='user'||m.role==='assistant')).map(m=>({role:m.role,content:m.content})));
 const out={role:'assistant',content:'',reason:'',model:(($('tgt').selectedOptions[0]||{}).text||(':'+port)).trim()};msgs.push(out);render();
 const modelName=(($('tgt').selectedOptions[0]||{}).text||(':'+port)).trim();
 const tk=tkAdd({who:modelName,desc:text.slice(0,80),started:Date.now()});const _t0=performance.now();
 ac=new AbortController();$('send').disabled=true;$('stop').disabled=false;
 await sendOne(port,out,{model:($('mdl').value||undefined),messages:base,stream:true,chat_template_kwargs:{enable_thinking:!!($('thk')&&$('thk').value==='on')},top_k:(window.__rec&&window.__rec.top_k)||undefined,temperature:+$('temp').value,top_p:+$('topp').value,max_tokens:+$('maxt').value},'');
 tkEnd(tk,out.err?'fail':'done',((performance.now()-_t0)/1000).toFixed(1));
 ac=null;$('send').disabled=false;$('stop').disabled=true;render();loadChanges()}
async function sendOverlord(text){const coord=$('coord').value;const ws=[...document.querySelectorAll('.wk:checked')].map(x=>+x.value);
 if(!coord||!ws.length){uiAlert('Pick an overlord and at least one worker.',{title:'Overlord mode'});return}
 pushHist(text);$('in').value='';autoGrow();
 const history=msgs.filter(m=>!m.err&&!m.aux&&(m.role==='user'||m.role==='assistant')).map(m=>({role:m.role,content:m.content}));
 msgs.push({role:'user',content:text});
 const status={role:'assistant',content:'translating...',reason:'',tag:'overlord',aux:true};msgs.push(status);tkReset();
 const ovName=(($('coord').selectedOptions[0]||{}).text||('overlord :'+coord)).trim();
 const tkOv=tkAdd({who:'overlord',cls:'plan',desc:ovName+' - translating',started:Date.now()});const tkw={};render();
 ac=new AbortController();$('send').disabled=true;$('stop').disabled=false;const wm={};let ask=null;
 try{const r=await fetch('/api/overlord',{method:'POST',headers:H,signal:ac.signal,body:JSON.stringify({task:text,coordinator:(isNaN(+coord)?coord:+coord),workers:ws,system:$('sys').value.trim(),history})});
  if(!r.ok){status.err=true;status.content='HTTP '+r.status+': '+await r.text();render();return}
  const rd=r.body.getReader(),dec=new TextDecoder();let buf='';
  for(;;){const {done,value}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});let i;
   while((i=buf.indexOf('\n'))>=0){const line=buf.slice(0,i).trim();buf=buf.slice(i+1);if(!line.startsWith('data:'))continue;const d=line.slice(5).trim();if(d==='[DONE]')continue;
    let e;try{e=JSON.parse(d)}catch{continue}
    if(e.type==='overlord_stage'){status.content=e.text+'...';if(tkOv){tkOv.desc=ovName+' - '+e.text;renderTasks()}}
    else if(e.type==='overlord_translate'){if(tkOv)tkOv.desc=ovName+' - instruction ready ('+e.secs+'s)';status.tag='overlord \u22c5 instruction \u22c5 '+e.secs+'s';status.content='**Instruction sent to the worker:**\n'+e.instruction+(e.success?'\n\n**Success looks like:** '+e.success:'')}
    else if(e.type==='overlord_worker_start'){tkw[e.name]=tkAdd({who:'worker '+e.name,desc:e.name,started:Date.now()});const m={role:'assistant',content:'working...',reason:'',tag:'worker \u22c5 '+e.name,aux:true,who:e.name};wm[e.name]=m;msgs.push(m)}
    else if(e.type==='overlord_tool'){const m=wm[e.worker];if(m){if(m.content==='working...')m.content='';m.content+='[tool: '+e.name+' '+e.args+']\n[result] '+e.result+'\n\n'}}
    else if(e.type==='overlord_redirect'){const m=wm[e.worker];if(m){if(m.content==='working...')m.content='';m.content+='[overlord: '+e.text+']\n\n'}}
    else if(e.type==='overlord_flag'){const m=wm[e.worker];if(m){if(m.content==='working...')m.content='';m.content+='[flag: '+e.text+']\n\n'}}
    else if(e.type==='overlord_worker_done'){tkEnd(tkw[e.name],'done',e.secs);const m=wm[e.name];if(m){m.content=(m.content==='working...'?'':m.content)+e.text;m.meta=e.secs+'s'+(e.tps?' \u22c5 '+e.tps.toFixed(1)+' t/s':'')+(e.redirects?' \u22c5 '+e.redirects+' redirect'+(e.redirects>1?'s':''):'')}}
    else if(e.type==='overlord_report'){status.tag='overlord \u22c5 report';const pr=e.problems||[];let t=e.text||'';
     if(pr.length)t+='\n\n**What the agent did wrong:**\n'+pr.map(p=>'- '+p).join('\n');
     if(e.advice)t+='\n\n**Overlord advice:** '+e.advice;
     status.content=t;if(pr.length||e.ok===false)ask={summary:e.text||'',problems:pr,advice:e.advice||'',retry:e.retry_prompt||text}}
    else if(e.type==='error'){status.err=true;status.content='[error] '+e.text}
    render()}}
 }catch(e){if(e.name!=='AbortError'){status.err=true;status.content='['+e.message+']'}else{status.content+='\n[stopped]'}}
 ac=null;$('send').disabled=false;$('stop').disabled=true;render();loadChanges();if(ask)overlordAsk(ask)}
function overlordAsk(a){const m=document.createElement('div'),b=document.createElement('div');m.className='uimask';b.className='uibox';b.setAttribute('role','dialog');b.setAttribute('aria-modal','true');b.style.maxWidth='680px';
 const h=document.createElement('h3');h.textContent='Overlord: the agent ran into problems';b.appendChild(h);
 const p=document.createElement('div');p.className='uimsg';p.style.whiteSpace='pre-wrap';
 p.textContent=(a.summary?a.summary+'\n\n':'')+(a.problems.length?'What the agent did wrong:\n'+a.problems.map(x=>'- '+x).join('\n')+'\n\n':'')+(a.advice?'Advice: '+a.advice+'\n\n':'')+'What do you want to do? Retry with the prompt below (edit it if you like), or cancel the job.';b.appendChild(p);
 const ta=document.createElement('textarea');ta.id='ovretry';ta.value=a.retry;ta.style.cssText='width:100%;min-height:96px;box-sizing:border-box;margin:8px 0';b.appendChild(ta);
 const bt=document.createElement('div');bt.className='uibtns';const c=document.createElement('button');c.textContent='Cancel job';const r=document.createElement('button');r.className='uiok';r.textContent='Retry with this prompt';bt.appendChild(c);bt.appendChild(r);b.appendChild(bt);m.appendChild(b);document.body.appendChild(m);ta.focus();
 c.onclick=()=>{m.remove();msgs.push({role:'assistant',content:'Job cancelled. The overlord\'s findings are above.',tag:'overlord',aux:true});render()};
 r.onclick=()=>{const t=ta.value.trim();m.remove();if(t){$('in').value=t;send()}};
 m.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();c.click()}else if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();r.click()}})}
async function sendTeam(text){const coord=$('coord').value;const ws=[...document.querySelectorAll('.wk:checked')].map(x=>+x.value);
 if(!coord||!ws.length){uiAlert('Pick a coordinator and at least one worker.',{title:'Team mode'});return}
 pushHist(text);$('in').value='';autoGrow();
 const history=msgs.filter(m=>!m.err&&!m.aux&&(m.role==='user'||m.role==='assistant')).map(m=>({role:m.role,content:m.content}));
 msgs.push({role:'user',content:text});
 const status={role:'assistant',content:'planning...',reason:'',tag:'coordinator :'+coord,aux:true};msgs.push(status);tkReset();const coName=(($('coord').selectedOptions[0]||{}).text||('coord :'+coord)).trim();const tkCoord=tkAdd({who:'coordinator',cls:'plan',desc:coName+' - planning',started:Date.now()});const tkw={};render();
 ac=new AbortController();$('send').disabled=true;$('stop').disabled=false;const t0=performance.now();const wm={};
 try{const r=await fetch('/api/team',{method:'POST',headers:H,signal:ac.signal,body:JSON.stringify({task:text,coordinator:(isNaN(+coord)?coord:+coord),workers:ws,system:$('sys').value.trim(),max_subtasks:+$('maxsub').value||4,history})});
  if(!r.ok){status.err=true;status.content='HTTP '+r.status+': '+await r.text();render();return}
  const rd=r.body.getReader(),dec=new TextDecoder();let buf='';
  for(;;){const {done,value}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});let i;
   while((i=buf.indexOf('\n'))>=0){const line=buf.slice(0,i).trim();buf=buf.slice(i+1);if(!line.startsWith('data:'))continue;const d=line.slice(5).trim();if(d==='[DONE]')continue;
    let e;try{e=JSON.parse(d)}catch{continue}
    if(e.type==='stage'){status.content=e.text+'...';if(tkCoord){tkCoord.desc=coName+' - '+e.text;renderTasks()}}
    else if(e.type==='plan'){if(tkCoord)tkCoord.desc=coName+' - planned '+e.subtasks.length+' piece(s) in '+e.secs+'s';status.tag='coordinator · plan · '+e.secs+'s';status.content=(e.text?e.text+'\n\n':'')+e.subtasks.map((x,k)=>(k+1)+'. ['+x.worker+']'+(x.after&&x.after.length?' (after '+x.after.map(a=>a+1).join(',')+')':'')+' '+x.instruction).join('\n')}
    else if(e.type==='worker_start'){tkw[e.idx]=tkAdd({who:'worker '+e.who,desc:(e.name||'')+' - '+(e.instruction||'').slice(0,70),started:Date.now()});const m={role:'assistant',content:'working...',reason:'',tag:'worker '+e.who+' · '+e.name,aux:true,who:e.who};wm[e.idx]=m;msgs.push(m)}
    else if(e.type==='tool'){const m=Object.values(wm).filter(x=>x.who===e.who).pop();if(m){if(m.content==='working...')m.content='';m.content+='[tool: '+e.name+' '+e.args+']\n[result] '+e.result+'\n\n'}}
    else if(e.type==='worker_done'){tkEnd(tkw[e.idx],e.failed?'fail':'done',e.secs);const m=wm[e.idx];if(m){m.content=(m.content==='working...'?'':m.content)+e.text;m.meta=e.secs+'s'+(e.tps?' · '+e.tps.toFixed(1)+' t/s':'')}}
    else if(e.type==='recover'){if(e.stage==='diagnosing'){tkw['d'+e.idx]=tkAdd({who:'coordinator',cls:'plan',desc:'diagnosing piece '+(e.idx+1)+' (worker '+e.who+' failed)'})}else if(e.stage==='diagnosis'){tkEnd(tkw['d'+e.idx],'done')}
    const txt=e.stage==='diagnosis'?('**Diagnosis:** '+e.text+'\n\n**Next:** '+(e.retry==='all'?'every worker takes it together':(e.retry==='other'?'retry on a different worker':'retry on the same worker'))+'\n\n**Corrected instruction:** '+(e.fix||'')):e.text;
    msgs.push({role:'assistant',content:txt,reason:'',tag:'recovery - piece '+(e.idx+1)+' - '+e.stage,aux:true,rc:true})}
    else if(e.type==='final'){tkEnd(tkCoord,'done',e.secs);msgs.push({report:{status:(e.unresolved?(e.unresolved>=e.pieces?'fail':'partial'):(e.recovered?'partial':'ok')),coord:coName,workers:e.workers||ws.length,pieces:e.pieces||0,total:e.total,recovered:e.recovered||0,unresolved:e.unresolved||0,task:text,when:new Date().toLocaleString()},role:'assistant',content:e.text,reason:'',tag:'coordinator · final answer',meta:e.secs+'s'+(e.tps?' · '+e.tps.toFixed(1)+' t/s':'')})}
    else if(e.type==='error'){tkEnd(tkCoord,'fail');Object.values(tkw).forEach(t=>{if(t&&t.state==='run')tkEnd(t,'fail')});msgs.push({role:'assistant',content:'[team error] '+e.text,err:true,aux:true})}
    $('stats').textContent='team · '+((performance.now()-t0)/1000).toFixed(0)+'s';render()}}
 }catch(e){if(e.name!=='AbortError'){msgs.push({role:'assistant',content:'['+e.message+']',err:true,aux:true})}else{status.content+='\n[stopped]'}}
 finally{ac=null;$('send').disabled=false;$('stop').disabled=true;render();loadChanges()}}
function reportRecs(text){const m=String(text||'').match(/##\s*recommend[^\n]*\n+([\s\S]*?)(?=\n##\s|$)/i);return !!(m&&m[1].trim().length>8)}
function extractRecs(text){const s=String(text||'');const m=s.match(/##\s*recommend[\s\S]*?(?=\n##\s|$)/i);return (m?m[0]:s).trim().slice(0,6000)}
async function goAhead(ix){const m=msgs[ix];if(!m||!m.report||m.report.decision||ac)return;const coord=$('coord').value;const ws=[...document.querySelectorAll('.wk:checked')];if(!coord||!ws.length){uiAlert('Pick a coordinator and at least one worker, then press Go Ahead again.',{title:'Team mode'});return}m.report.decision='go';render();const task='The organiser already found the solution. Apply the recommended fixes below and complete them in the working folder. Make the changes. Do not stop at another recommendation list unless a step cannot be done.\n\nOriginal task:\n'+(m.report.task||'')+'\n\n'+extractRecs(m.content);await sendTeam(task)}
function dontGo(ix){const m=msgs[ix];if(!m||!m.report||m.report.decision)return;m.report.decision='dont';render()}
let _playSaveT=null;
function savePlayNow(){
  if(_playSaveT){clearTimeout(_playSaveT);_playSaveT=null}
  var draft=($('in')&&$('in').value)||'';
  var sys=($('sys')&&$('sys').value)||'';
  try{localStorage.setItem('mb_play_code',JSON.stringify({msgs:msgs.slice(-150),draft:draft,sys:sys}))}
  catch(e){
    // storage full: keep fewer messages at full length rather than cutting replies short
    var saved=false,ns=[80,40,20,10,4];
    for(var q=0;q<ns.length&&!saved;q++){try{localStorage.setItem('mb_play_code',JSON.stringify({msgs:msgs.slice(-ns[q]),draft:draft,sys:sys}));saved=true}catch(e2){}}
    if(!saved){try{
      var slim=msgs.slice(-10).map(function(m){var c={},k,v;for(k in m){v=m[k];c[k]=(typeof v==='string'&&v.length>20000)?v.slice(0,20000)+'\n[... trimmed when saving - browser storage is full]':v}return c});
      localStorage.setItem('mb_play_code',JSON.stringify({msgs:slim,draft:draft,sys:sys}));
    }catch(e3){}}
  }
  try{
    var tasks=TASKS.map(function(o){
      var live=o.state==='run';
      var el=o.fixed!=null?String(o.fixed):((performance.now()-(o.t0||performance.now()))/1000).toFixed(1);
      return {who:o.who||'',desc:o.desc||'',cls:o.cls||'',state:o.state||'',started:o.started||0,fixed:el,live:live?1:0};
    });
    localStorage.setItem('mb_play_proc',JSON.stringify({tasks:tasks,stats:($('stats')&&$('stats').textContent)||''}));
  }catch(e3){}
}
function savePlay(){if(_playSaveT)clearTimeout(_playSaveT);_playSaveT=setTimeout(savePlayNow,200)}
function restorePlay(){
  try{
    var c=JSON.parse(localStorage.getItem('mb_play_code')||'null');
    if(c&&Array.isArray(c.msgs)){
      msgs=c.msgs;
      if(typeof c.draft==='string'){$('in').value=c.draft;autoGrow()}
      if(typeof c.sys==='string')$('sys').value=c.sys;
    }
  }catch(e){}
  try{
    var p=JSON.parse(localStorage.getItem('mb_play_proc')||'null');
    var tasks=(p&&Array.isArray(p.tasks))?p.tasks:(Array.isArray(p)?p:null);
    if(tasks&&tasks.length){
      TASKS=tasks.map(function(o){
        var live=!!(o&&(o.live||o.state==='run'));
        return {who:(o&&o.who)||'',desc:(o&&o.desc)||'',cls:live?'stopped':((o&&(o.cls||o.state))||'done'),state:live?'stopped':((o&&o.state)||'done'),started:(o&&o.started)||Date.now(),t0:performance.now(),fixed:(o&&o.fixed!=null)?o.fixed:'0'};
      });
    }
    if(p&&typeof p.stats==='string')$('stats').textContent=p.stats;
  }catch(e2){}
  render();renderTasks();
}
window.addEventListener('pagehide',savePlayNow);
window.addEventListener('beforeunload',savePlayNow);
document.addEventListener('visibilitychange',function(){if(document.visibilityState==='hidden')savePlayNow()});
$('benchlink').addEventListener('mousedown',savePlayNow);
$('benchlink').addEventListener('click',savePlayNow);
const DEFAULT_SYS_BASE=DEFAULT_SYS;
const MB={local:'',peers:[],opts:['local'],warn:[],loaded:false};
async function loadMachines(refresh){try{const j=await (await fetch('/api/machines'+(refresh?'?refresh=1':''),{headers:H})).json();
 MB.local=(j.local&&j.local.name)||'';MB.peers=j.peers||[];MB.warn=j.warnings||[];MB.opts=['local'].concat(MB.peers.map(p=>p.id));MB.loaded=true;
 const sel=$('phost');sel.innerHTML='<option value="local">'+esc(MB.local)+' (this PC)</option>'+MB.peers.map(p=>'<option value="'+esc(p.id)+'">'+esc(p.name)+(p.ok?'':' (offline)')+'</option>').join('');
 PNL.host=(MB.opts.indexOf(WS.host)>=0)?WS.host:(MB.opts.indexOf(PNL.host)>=0?PNL.host:'local');sel.value=PNL.host;wsFill();wsApply();
 const sb=$('sbhost');sb.classList.toggle('bad',MB.warn.length>0);
 sb.textContent=MB.warn.length?('machine check: '+MB.warn.length+' problem'+(MB.warn.length>1?'s':'')):('on '+MB.local);
 sb.title=MB.warn.length?MB.warn.join('\n'):('This IDE runs on '+MB.local+(MB.peers.length?'. Other PCs: '+MB.peers.map(p=>p.name+(p.ok?'':' (offline)')).join(', '):''));
 const peerTxt=MB.peers.map(p=>'For '+p.name+' call aissh6_'+p.id+'_run instead.').join(' ');
 DEFAULT_SYS=DEFAULT_SYS_BASE+(REMOTE&&peerTxt?' '+peerTxt:'');
 if($('sys').value.trim()===DEFAULT_SYS_BASE.trim()){$('sys').value=DEFAULT_SYS;savePlay()}
 if(!msgs.length)render();tPrompt()}
 catch(e){const sb=$('sbhost');sb.textContent='machine check failed';sb.classList.add('bad');sb.title=String(e)}}
const PNL={tab:'term',host:'local',seq:0,acts:[],open:{},ac:null,hist:[],hi:0,cwd:{},greet:{},real:{}};
try{PNL.tab=localStorage.getItem('mb_ptab')||'term';PNL.host=localStorage.getItem('mb_thost')||'local';if(!REMOTE&&(PNL.tab==='term'||PNL.tab==='jobs'))PNL.tab='act';PNL.hist=JSON.parse(localStorage.getItem('mb_termhist')||'[]')}catch(e){}
PNL.hi=PNL.hist.length;
function panelOn(){return document.documentElement.dataset.panel==='on'}
function togglePanel(on){const d=document.documentElement;const v=(on===undefined)?!panelOn():!!on;if(v)d.dataset.panel='on';else delete d.dataset.panel;try{localStorage.setItem('mb_panel',v?'1':'0')}catch(e){}if(v)showTab(PNL.tab);else $('in').focus()}
function showTab(t){PNL.tab=t;try{localStorage.setItem('mb_ptab',t)}catch(e){}
 document.querySelectorAll('.ptab').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
 document.querySelectorAll('.pview').forEach(v=>v.style.display=(v.dataset.t===t)?'':'none');
 $('phost').style.display=(t==='act'||t==='info')?'none':'';$('tstop').style.display=(t==='term')?'':'none';
 if(t==='term'){greet();tPrompt();setTimeout(()=>$('tcmd').focus(),0)}if(t==='jobs')loadJobs();if(t==='info')loadInfo();if(t==='act')renderAct(true)}

async function loadInfo(){const v=$('infov');v.innerHTML='<div class="dim" style="padding:12px">loading...</div>';
 try{const r=await fetch('/api/info',{headers:H});if(!r.ok)throw new Error('HTTP '+r.status);renderInfo(await r.json())}catch(e){v.innerHTML='<div class="terr" style="padding:12px">Info failed: '+esc(e.message)+'</div>'}}
function cp(t,b){try{navigator.clipboard.writeText(t);const o=b.textContent;b.textContent='copied';setTimeout(()=>{b.textContent=o},900)}catch(e){prompt('Copy:',t)}}
var HFURL='';fetch('/api/info',{headers:H}).then(r=>r.json()).then(j=>{HFURL=j.hf_url||'';document.querySelectorAll('.hflink').forEach(a=>{if(HFURL)a.href=HFURL});if(j.box_url){var bl=document.getElementById('boxlink');if(bl)bl.href=j.box_url}}).catch(()=>{});
function openHF(){if(HFURL)window.open(HFURL,'_blank');else alert('The Hugging Face link is not ready (hf-fetch has no key yet?) - try again in a moment')}
function renderInfo(j){const dot=a=>'<span style="color:'+(a==='active'?'#4c8':'#d66')+'">&#9679;</span> ';
 let h='<style>#infov{padding:10px 14px;line-height:1.5;overflow:auto}#infov h4{margin:14px 0 4px;font-size:11px;letter-spacing:.07em;text-transform:uppercase;opacity:.65}#infov table{border-collapse:collapse}#infov td{padding:2px 16px 2px 0;vertical-align:top}#infov code{background:rgba(128,128,128,.2);padding:1px 6px;border-radius:3px}#infov button{margin-left:6px}#infov .nt{opacity:.9;margin:3px 0}</style>';
 h+='<div><a class="go hflink" target="_blank" rel="noopener" href="'+esc(j.hf_url||'#')+'" style="text-decoration:none;display:inline-block;padding:5px 12px;border-radius:4px;cursor:pointer">Search Hugging Face and download models</a><button onclick="loadInfo()">Refresh</button></div>';
 h+='<h4>Services</h4><table>'+j.services.map(s=>'<tr><td>'+dot(s.state)+esc(s.name)+'</td><td>'+esc(s.state)+'</td><td class="dim">'+esc(s.note||'')+'</td></tr>').join('')+'</table>';
 h+='<h4>Endpoints</h4><table>'+j.links.map(l=>'<tr><td>'+esc(l.label)+'</td><td><code>'+esc(l.show||l.url)+'</code><button data-u="'+esc(l.url)+'" onclick="cp(this.dataset.u,this)">copy</button>'+(l.open?'<button data-u="'+esc(l.url)+'" onclick="window.open(this.dataset.u)">open</button>':'')+'</td></tr>').join('')+'</table>';
 h+='<h4>Add models</h4><div class="nt">Drop folder <code>'+esc(j.inbox.path)+'</code> ('+j.inbox.free_gb+' GB free, '+j.inbox.files+' file(s) in it). Copy from a PC: <code>'+esc(j.inbox.scp)+'</code><button data-u="'+esc(j.inbox.scp)+'" onclick="cp(this.dataset.u,this)">copy</button></div>';
 h+='<h4>How it fits together</h4>'+j.notes.map(n=>'<div class="nt">&bull; '+esc(n)+'</div>').join('');
 $('infov').innerHTML=h}

function setHost(h){PNL.host=(MB.opts.indexOf(h)>=0)?h:'local';try{localStorage.setItem('mb_thost',PNL.host)}catch(e){}$('phost').value=PNL.host;if(PNL.tab==='term'){greet();tPrompt();$('tcmd').focus()}if(PNL.tab==='jobs')loadJobs()}
function hostName(h){if(h==='local')return MB.local||'this PC';const p=(MB.peers||[]).find(x=>x.id===h);return p?p.name:String(h).toUpperCase()}
function tPrompt(){$('tps').textContent='PS '+(PNL.real[PNL.host]||hostName(PNL.host))+(PNL.cwd[PNL.host]?' '+PNL.cwd[PNL.host]:'')+'>'}
function tWrite(h){const o=$('termout');o.insertAdjacentHTML('beforeend',h);while(o.childNodes.length>3000)o.removeChild(o.firstChild);const w=$('termwrap');w.scrollTop=w.scrollHeight}
function tNL(){const t=$('termout').textContent;if(t&&!t.endsWith('\n'))tWrite('\n')}
function greet(){if(PNL.greet[PNL.host])return;PNL.greet[PNL.host]=1;tNL();MB.warn.forEach(w=>tWrite('<span class="twarn">Machine check: '+esc(w)+'</span>\n'));tWrite('<span class="tdim">PowerShell on '+hostName(PNL.host)+' through aissh6, as the daemon account. cls clears, Ctrl+C stops, arrow keys recall history.'+(PNL.host!=='local'?' Output from '+hostName(PNL.host)+' arrives when the command finishes.':'')+'</span>\n')}
async function termRun(){const inp=$('tcmd');const cmd=inp.value;if(PNL.ac)return;inp.value='';const host=PNL.host;
 if(cmd.trim()){if(PNL.hist[PNL.hist.length-1]!==cmd){PNL.hist.push(cmd);if(PNL.hist.length>300)PNL.hist.shift();try{localStorage.setItem('mb_termhist',JSON.stringify(PNL.hist))}catch(e){}}}PNL.hi=PNL.hist.length;
 tNL();tWrite('<span class="tprompt">'+esc($('tps').textContent)+'</span> '+esc(cmd)+'\n');
 if(!cmd.trim())return;
 if(/^\s*(cls|clear)\s*$/i.test(cmd)){$('termout').innerHTML='';return}
 PNL.ac=new AbortController();$('tstop').disabled=false;inp.placeholder='running, Ctrl+C to stop';
 try{const r=await fetch('/api/term',{method:'POST',headers:H,body:JSON.stringify({cmd:cmd,host:host}),signal:PNL.ac.signal});
  if(!r.ok){tWrite('<span class="terr">HTTP '+r.status+': '+esc(await r.text())+'</span>\n')}
  else{const rd=r.body.getReader(),dec=new TextDecoder();let buf='';
   for(;;){const {done,value}=await rd.read();if(done)break;buf+=dec.decode(value,{stream:true});let i;
    while((i=buf.indexOf('\n\n'))>=0){const blk=buf.slice(0,i);buf=buf.slice(i+2);const ln=blk.split('\n').find(x=>x.startsWith('data:'));if(!ln)continue;const d=ln.slice(5).trim();if(d==='[DONE]')continue;let e;try{e=JSON.parse(d)}catch(_){continue}
     if(e.t==='out'){if(e.text)tWrite(e.err?'<span class="terr">'+esc(e.text)+'</span>':esc(e.text))}
     else if(e.t==='end'){if(e.cwd)PNL.cwd[host]=e.cwd;if(e.host)PNL.real[host]=e.host;if(e.mismatch){tNL();tWrite('<span class="twarn">Warning: this was meant for '+esc(e.expected)+' but it ran on '+esc(e.host)+'.</span>\n')}if(e.status!=='Completed'||(e.exit!=null&&e.exit!==0)){tNL();tWrite('<span class="tdim">['+esc(e.status)+(e.exit!=null?', exit '+e.exit:'')+', '+e.secs+'s]</span>\n')}}
     else if(e.t==='error'){tNL();tWrite('<span class="terr">'+esc(e.text)+'</span>\n')}}}}}
 catch(err){tNL();tWrite('<span class="terr">'+(err.name==='AbortError'?'^C stopped':esc(err.message))+'</span>\n')}
 finally{PNL.ac=null;$('tstop').disabled=true;inp.placeholder='';tNL();tPrompt();inp.focus()}}
function termStop(){if(PNL.ac)PNL.ac.abort()}
$('tcmd').addEventListener('keydown',e=>{
 if(e.key==='Enter'){e.preventDefault();termRun();return}
 if(e.key==='ArrowUp'){e.preventDefault();if(PNL.hi>0){PNL.hi--;$('tcmd').value=PNL.hist[PNL.hi]||''}return}
 if(e.key==='ArrowDown'){e.preventDefault();if(PNL.hi<PNL.hist.length){PNL.hi++;$('tcmd').value=PNL.hist[PNL.hi]||''}return}
 if(e.ctrlKey&&(e.key==='c'||e.key==='C')&&!getSelection().toString()){e.preventDefault();if(PNL.ac)termStop();else{tNL();tWrite('<span class="tprompt">'+esc($('tps').textContent)+'</span> '+esc($('tcmd').value)+'^C\n');$('tcmd').value=''}return}
 if(e.ctrlKey&&(e.key==='l'||e.key==='L')){e.preventDefault();$('termout').innerHTML='';return}
 if(e.key==='Escape'){e.preventDefault();$('in').focus()}});
$('termwrap').addEventListener('mouseup',()=>{if(!getSelection().toString())$('tcmd').focus()});
function panelClear(){if(PNL.tab==='term'){$('termout').innerHTML='';$('tcmd').focus()}else if(PNL.tab==='act'){PNL.acts=[];PNL.open={};renderAct(true)}}
function hms(t){const d=new Date(t);return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')+':'+String(d.getSeconds()).padStart(2,'0')}
function secs(s){s=+s||0;return s<60?s.toFixed(1)+'s':Math.floor(s/60)+'m'+String(Math.round(s%60)).padStart(2,'0')}
async function pollAct(){try{const j=await (await fetch('/api/activity?since='+PNL.seq,{headers:H})).json();if(!j||j.seq==null)return;
 if(j.seq<PNL.seq){PNL.seq=0;return}PNL.seq=j.seq;
 for(const e of j.items||[]){const ix=PNL.acts.findIndex(x=>x.id===e.id);if(ix>=0)PNL.acts[ix]=e;else PNL.acts.push(e)}
 if(PNL.acts.length>300)PNL.acts=PNL.acts.slice(-300);renderAct()}catch(e){}}
function renderAct(force){const run=PNL.acts.filter(a=>a.state==='run').length;$('actbd').textContent=run?String(run):'';$('sbact').textContent=run?(run+' running'):'';
 if(!panelOn()||PNL.tab!=='act')return;const v=$('actv');const stick=force||v.scrollHeight-v.scrollTop-v.clientHeight<40;
 if(!PNL.acts.length){v.innerHTML='<div class="pempty">Nothing yet. Tool calls from chat and team agents, and commands from the terminal, show up here as they run.</div>';return}
 const now=Date.now()/1000;
 v.innerHTML=PNL.acts.map(a=>{const st=a.state==='run'?secs(now-a.ts):(a.state==='fail'?'failed':secs(a.secs));const tool=(a.tool||'').replace(/^aissh6_/,'');
  const line=String(a.detail||'').split('\n').find(x=>x.trim()&&!/^Set-Location /.test(x))||'';
  return '<div class="arow" onclick="PNL.open['+a.id+']=!PNL.open['+a.id+'];renderAct()"><span class="t">'+hms(a.ts*1000)+'</span><span class="w" title="'+esc(a.who)+'">'+esc(a.who)+'</span><span class="hb '+(a.host===MB.local?'':(a.host==='web'?'web':'peer'))+'">'+esc(a.host)+'</span><span class="d" title="'+esc(a.detail)+'"><span class="tdim">'+esc(tool)+'</span> '+esc(line)+'</span><span class="st '+a.state+'">'+st+'</span>'+
   (PNL.open[a.id]?'<div class="ares">'+esc(a.detail)+(a.result?'\n\n'+esc(a.result):(a.state==='run'?'\n\nrunning...':''))+'</div>':'')+'</div>'}).join('');
 if(stick)v.scrollTop=v.scrollHeight}
function jclean(c){return String(c||'').replace(/\s+/g,' ').replace(/^Set-Location -LiteralPath '[^']*'( -ErrorAction SilentlyContinue)? ?/,'').replace(/^if \(Test-Path 'E:\\git'\)[^}]*\}[^}]*\} ?/,'').replace(/^& \{ ?/,'').replace(/ ?\} ?(2>&1 )?\| ?Out-String.*$/,'').trim()}
async function loadJobs(){if(!panelOn()||PNL.tab!=='jobs')return;const h=PNL.host;const v=$('jobsv');
 try{const j=await (await fetch('/api/jobs?host='+h+'&limit=40',{headers:H})).json();if(PNL.tab!=='jobs'||PNL.host!==h)return;
  const jobs=j.jobs||[];const run=jobs.filter(x=>/Running|Queued/i.test(x.status)).length;
  const now=Date.now();
  v.innerHTML='<div class="pnote">aissh6 jobs on '+hostName(h)+': '+jobs.length+' recent, '+run+' running. Session shows who asked: modelbench = agents here, peer = the other box, oracle = the watchdog, none = one-off commands such as this terminal.'+(j.error?' <span class="terr">'+esc(j.error)+'</span>':'')+'</div>'+
   (jobs.length?jobs.map(x=>{const c=Date.parse(x.createdAt||'')||now;const s=Date.parse(x.startedAt||'')||c;const f=Date.parse(x.finishedAt||'')||now;
    return '<div class="jrow '+esc(x.status)+'"><span class="tdim">'+hms(c)+'</span><span class="ses" title="session">'+esc(x.sessionId||'-')+'</span><span class="s">'+esc(x.status)+(x.exitCode!=null&&x.exitCode!==0?' '+x.exitCode:'')+'</span><span class="du tdim">'+secs(Math.max(0,(f-s)/1000))+'</span><span class="d" title="'+esc(x.commandPreview)+'">'+esc(jclean(x.commandPreview))+'</span></div>'}).join(''):'<div class="pempty">No jobs.</div>')}
 catch(e){v.innerHTML='<div class="pempty terr">Could not read jobs from '+hostName(h)+'.</div>'}}
(function(){const g=$('pgrip'),pn=$('panel');let y0=0,h0=0;
 try{const ph=+localStorage.getItem('mb_ph');if(ph>=120)pn.style.setProperty('--ph',ph+'px')}catch(e){}
 const mv=e=>{const h=Math.max(120,Math.min(window.innerHeight*0.8,h0+(y0-e.clientY)));pn.style.setProperty('--ph',h+'px')};
 const up=()=>{document.removeEventListener('mousemove',mv);document.removeEventListener('mouseup',up);document.body.style.userSelect='';try{localStorage.setItem('mb_ph',parseInt(getComputedStyle(pn).flexBasis))}catch(e){}};
 g.addEventListener('mousedown',e=>{y0=e.clientY;h0=pn.getBoundingClientRect().height;document.body.style.userSelect='none';document.addEventListener('mousemove',mv);document.addEventListener('mouseup',up)})})();
$('phost').value=PNL.host;loadMachines().then(()=>{if(panelOn())showTab(PNL.tab)});if(location.hash==='#info'){togglePanel(true);showTab('info')}
pollAct();setInterval(pollAct,1500);setInterval(()=>{if(PNL.acts.some(a=>a.state==='run'))renderAct()},1000);setInterval(loadJobs,4000);
$('think').addEventListener('change',render);$('showtools').checked=document.documentElement.dataset.tools!=='off';
sbDir();restorePlay();
loadCtx();loadChanges();targets();setInterval(()=>{if(!ac)fetch('/api/state',{headers:H}).then(r=>r.json()).then(j=>{const ok=j.servers.filter(s=>s.kind!=='router-child'&&s.health==='ok'&&s.chat_capable!==false);if(!ok.some(s=>String(s.port)===$('tgt').value)||ok.length!==window.__n)targets()}).catch(()=>{})},5000);
</script></body></html>""".replace("__CSS__", CSS)


# ------------------------------------------------------------------ team: coordinator + workers
TEAM_COORD_SYS = ("You coordinate a small team of local AI models working on one task for the user. "
    "You plan the work, hand each worker a self-contained piece, then check and combine what they return into one answer. "
    "Workers cannot see the task, the plan, or each other - only the instruction you give them. Be concise and practical.")

def _team_worker_sys(letter, tools):
    s = ("You are worker %s on a small team. Do only the piece you are given, properly and completely. "
         "Return the result itself - no preamble, no questions back. " % letter)
    if tools and not REMOTE:
        s += ("You have tools: web_search to search the internet and web_fetch to read a page. "
              "Put real values in tool arguments. After a tool returns, use its result.")
    elif tools:
        s += ("You have tools: for anything about this computer (" + LOCAL_HOST + ") - files, folders, processes, running a command - "
              "call aissh6_aissh_run with one PowerShell command (for another PC use its aissh6_<name>_run tool). Commands already start in the selected working folder; "
              "use relative paths and do not Set-Location away from it. "
              "Put real values in tool arguments. After a tool returns, use its result.")
    else:
        s += "You have no tools: reason and write only, and say plainly if the piece needs data you cannot get."
    return s

def tools_for_port(port):
    now = time.time(); c = _TOOLS_CACHE.get(port)
    if c and now - c[0] < 30:
        return _bind_tools(c[1])
    defs = []
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/tools" % port, timeout=5) as r:
            for t in json.loads(r.read()):
                d = t.get("definition")
                if not d:
                    continue
                fn = d.get("function") or {}
                cap = CFG["tool_desc_max"]
                if isinstance(fn.get("description"), str) and len(fn["description"]) > cap:
                    fn["description"] = fn["description"][:cap].rstrip() + "..."
                for pv in ((fn.get("parameters") or {}).get("properties") or {}).values():
                    if isinstance(pv, dict) and isinstance(pv.get("description"), str) and len(pv["description"]) > 120:
                        pv["description"] = pv["description"][:120].rstrip() + "..."
                defs.append(d)
    except Exception:
        pass
    if not defs and CFG.get("builtin_tools", True) and not REMOTE:
        defs = WEB_TOOLS
    elif not defs and CFG.get("builtin_tools", True):
        defs = WEB_TOOLS + [{"type": "function", "function": {"name": "aissh6_aissh_run", "description": "Run one PowerShell command on this computer; it starts in the selected working folder. Returns the command output.", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "the PowerShell command"}}, "required": ["command"]}}}]
    _TOOLS_CACHE[port] = (now, defs)
    return _bind_tools(defs)

WEB_TOOLS = [
    {"type": "function", "function": {"name": "web_search", "description": "Search the web. Returns titles, links and snippets.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "what to search for"},
                    "max_results": {"type": "integer", "description": "1-15, default 8"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "web_fetch", "description": "Read a web page as text (with its links).",
     "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "http(s) URL"}}, "required": ["url"]}}},
]

class ToolCallParseError(RuntimeError):
    """llama-server could not parse the worker's tool call (usually cut off at max_tokens mid-string)."""

TOOLPARSE_NOTE = ("OVERLORD: your last tool call was cut off or malformed and was NOT run (its JSON arguments were broken, "
                  "usually because the script you were writing was too long). Do not resend it. Write the file in small steps: "
                  "at most 40 lines per command, the first with Set-Content and the rest with Add-Content, each using a "
                  "single-quoted here-string (@' ... '@). Keep every command short.")

def _team_upstream(port, body, timeout=900):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("POST", "/v1/chat/completions", body=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        resp = conn.getresponse(); raw = resp.read()
    finally:
        conn.close()
    d = json.loads(raw or b"{}")
    if resp.status >= 400 or "error" in d:
        _m = "model on :%d returned %s: %s" % (port, resp.status, str(d.get("error", d))[:300])
        if "parse tool call arguments" in str(d.get("error", d)):
            raise ToolCallParseError(_m)
        raise RuntimeError(_m)
    return d

def agent_complete(port, messages, tools_on, emit=None, tag="", max_tokens=1024, temperature=0.4, stop=None):
    """One model, one job: runs the tool loop to a final text answer."""
    body = {"messages": list(messages), "stream": False, "chat_template_kwargs": {"enable_thinking": False}, "max_tokens": max_tokens, "temperature": temperature}
    if tools_on:
        defs = tools_for_port(port)
        if defs:
            body["tools"] = defs
        _inject_facts(body["messages"])
    tm = None
    _tpf = 0
    for step in range(CFG["agent_max_steps"] + 1):
        if stop and stop():
            raise RuntimeError("stopped")
        try:
            data = _team_upstream(port, body)
        except ToolCallParseError:
            _tpf += 1
            if _tpf > 3:
                return "(stopped: the model kept producing malformed tool calls)", tm
            body["messages"].append({"role": "user", "content": TOOLPARSE_NOTE})
            continue
        tm = data.get("timings") or tm
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        calls = _fix_tool_ids(msg.get("tool_calls") or [])
        if not calls:
            return _strip(msg.get("content") or ""), tm
        if step == CFG["agent_max_steps"]:
            return "(stopped: tool-step limit reached)", tm
        body["messages"].append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for tc in calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            result = run_tool(name, args, who=tag)
            if emit:
                emit({"type": "tool", "who": tag, "name": name, "args": json.dumps(args)[:200], "result": result[:400]})
            body["messages"].append({"role": "tool", "tool_call_id": tc.get("id") or name, "name": name, "content": result})
    return "", tm

def _parse_plan(txt, letters, task, cap):
    s = (txt or "").strip()
    a, b = s.find("{"), s.rfind("}")
    subs, plan = [], ""
    if a >= 0 and b > a:
        try:
            d = json.loads(s[a:b + 1])
            plan = str(d.get("plan") or "")[:300]
            for i, st in enumerate(d.get("subtasks") or []):
                if not isinstance(st, dict):
                    continue
                ins = str(st.get("instruction") or "").strip()
                if not ins:
                    continue
                w = str(st.get("worker") or "").strip().upper()[:1]
                if w not in letters:
                    w = letters[i % len(letters)]
                subs.append({"worker": w, "instruction": ins, "_after": st.get("after") or []})
        except ValueError:
            pass
    for i, st in enumerate(subs):
        raw = st.pop("_after", [])
        dd = set()
        for x in (raw if isinstance(raw, list) else [raw]):
            try:
                d = int(x) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= d < i:
                dd.add(d)
        st["after"] = sorted(dd)
    if not subs:
        raise RuntimeError("The coordinator did not return a valid task plan. No workers were started; use the coding workspace for a recoverable run.")
    return plan, subs[:cap]


def _secret(name):
    try:
        with open(CFG["secrets_file"], encoding="utf-8") as f:
            return (json.load(f).get(name) or "").strip()
    except Exception:
        return ""

def _env_secret(var):
    """An API key from the environment; falls back to the user's registry so a newly set key works without a restart."""
    v = (os.environ.get(var) or "").strip()
    if v:
        return v
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, var)[0]).strip()
    except Exception:
        return ""


def remote_key(name):
    rc = CFG["remote_coordinators"].get(name) or {}
    if rc.get("env"):
        return _env_secret(rc["env"])
    return _secret(rc.get("key_name") or (name + "_api_key"))

def remote_list():
    return [{"id": k, "label": v.get("label", k), "model": v.get("model"), "ready": bool(remote_key(k))}
            for k, v in CFG["remote_coordinators"].items()]

def _anthropic_complete(name, rc, key, messages, max_tokens, temperature):
    """Anthropic Messages API: system prompt as its own field, roles merged so they alternate user/assistant."""
    system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
    conv = []
    for m in messages:
        if m.get("role") == "system":
            continue
        r = "assistant" if m.get("role") == "assistant" else "user"
        c = m.get("content")
        c = c if isinstance(c, str) else json.dumps(c)
        if conv and conv[-1]["role"] == r:
            conv[-1]["content"] += "\n\n" + c
        else:
            conv.append({"role": r, "content": c})
    if not conv or conv[0]["role"] != "user":
        conv.insert(0, {"role": "user", "content": "(start)"})
    body = {"model": rc["model"], "max_tokens": max_tokens, "messages": conv}
    if not rc.get("_no_temperature"):
        body["temperature"] = temperature
    if rc.get("thinking"):
        body["thinking"] = rc["thinking"]      # Sonnet 5.5 thinks by default, which eats the budget and can end in a refusal
    if system:
        body["system"] = system
    t0 = time.time()
    for attempt in (1, 2):
        req = urllib.request.Request(rc["url"], data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                d = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            err = e.read()[:300].decode("utf-8", "replace")
            if attempt == 1 and e.code == 400 and "temperature" in err and "temperature" in body:
                body.pop("temperature", None)          # newer models (e.g. Sonnet 5.5) reject temperature
                rc["_no_temperature"] = True
                continue
            raise RuntimeError("%s API error %s: %s" % (name, e.code, err))
    if d.get("stop_reason") == "refusal":
        raise RuntimeError("%s declined to answer (stop_reason refusal)" % name)
    txt = "".join(b.get("text", "") for b in (d.get("content") or []) if b.get("type") == "text")
    u = d.get("usage") or {}
    secs = max(time.time() - t0, 0.001)
    return _strip(txt), {"remote": name, "prompt_n": u.get("input_tokens"), "predicted_n": u.get("output_tokens"),
                         "predicted_per_second": (u.get("output_tokens") or 0) / secs}


def remote_complete(name, messages, max_tokens=1024, temperature=0.3):
    rc = CFG["remote_coordinators"][name]
    key = remote_key(name)
    if not key:
        if rc.get("env"):
            raise RuntimeError("no API key for %s - set the %s environment variable" % (name, rc["env"]))
        raise RuntimeError("no API key for %s yet - add %s to %s" % (name, rc.get("key_name", name + "_api_key"), CFG["secrets_file"]))
    if rc.get("kind") == "anthropic":
        return _anthropic_complete(name, rc, key, messages, max_tokens, temperature)
    body = {"model": rc["model"], "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    req = urllib.request.Request(rc["url"], data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError("%s API error %s: %s" % (name, e.code, e.read()[:300].decode("utf-8", "replace")))
    txt = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if isinstance(txt, list):
        txt = "".join(c.get("text", "") for c in txt if isinstance(c, dict))
    u = d.get("usage") or {}
    secs = max(time.time() - t0, 0.001)
    return _strip(txt), {"remote": name, "prompt_n": u.get("prompt_tokens"), "predicted_n": u.get("completion_tokens"),
                         "predicted_per_second": (u.get("completion_tokens") or 0) / secs}

TEAM_STATS = os.path.join(DATA, "team_stats.json")
_ts_lock = threading.Lock()
_ts = {}

def _ts_load():
    global _ts
    try:
        with open(TEAM_STATS, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        _ts = {}
        return
    except Exception as e:
        log("team stats load failed: %s" % e)
        _ts = {}
        return
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if not isinstance(v, dict):
                continue
            try:
                out[str(k)] = {"times_used": int(v.get("times_used") or 0), "score": int(v.get("score") or 0)}
            except (TypeError, ValueError):
                continue
    _ts = out

def _ts_save():
    tmp = TEAM_STATS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_ts, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, TEAM_STATS)

def team_stat(card_id):
    with _ts_lock:
        v = _ts.get(card_id) or {}
        return {"times_used": int(v.get("times_used") or 0), "score": int(v.get("score") or 0)}

def team_stat_add(card_id, used=0, score=0):
    if not card_id:
        return
    used, score = int(used or 0), int(score or 0)
    if used == 0 and score == 0:
        return
    with _ts_lock:
        rec = _ts.setdefault(card_id, {"times_used": 0, "score": 0})
        rec["times_used"] += used
        rec["score"] += score
        try:
            _ts_save()
        except Exception as e:
            log("team stats save failed: %s" % e)

def _team_text_ok(txt):
    t = (txt or "").strip()
    if not t:
        return False
    return not t.startswith("(stopped:") and not t.startswith("(worker failed")

def _team_delta(ok):
    return 1 if ok else -1

_ts_load()

def card_id_for_port(port):
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    k = BENCH.loaded.get(port)
    if k and k.get("path"):
        return model_card_id(k["path"])
    for sv in SNAP.get("servers") or []:
        if sv.get("port") == port and sv.get("card"):
            return sv["card"]
    return None

OVERLORD_TRANSLATE_SYS = (
    "You are the Overlord: a planning layer between the user and a worker AI that controls this PC via tools. "
    "The user's message may be vague, shorthand, or assume context you can see but the worker cannot. Turn it into one precise, "
    "complete, unambiguous instruction the worker can execute without guessing, plus a short description of what a correct "
    "result looks like so it can be checked afterwards. Spell out every step it needs, in order - including creating folders that may not exist, "
    "using full absolute paths, and running or checking the result. Do not do the task yourself and do not call any tools. "
    "Commands run in Windows PowerShell, not bash. Every tool call is a NEW shell that starts in the working folder: Set-Location / cd never carries over to the next call, so the worker must use full absolute paths in every command and never rely on a previous cd. Quote for PowerShell: inside single quotes, write a single quote as two single quotes; never use the bash '\"'\"' trick. "
    "To write a file whose content has $, quotes or backticks, use a single-quoted here-string so nothing is expanded: "
    "@'<newline>content<newline>'@ | Set-Content -Path C:\\full\\path.ext (the closing '@ must start its own line). "

    'Reply with ONLY JSON, no prose, no code fences: {"instruction": "precise instruction for the worker", '
    '"success": "what a correct result looks like, one or two sentences"}'
)
OVERLORD_REVIEW_SYS = (
    "You are the Overlord, watching a worker AI mid-task. You see the instruction you gave it, its earlier steps, and the tool call it just made. "
    "If it repeats a command that already failed, or a command errored, redirect it with the exact corrected command. "
    "Commands run in Windows PowerShell, not bash. Every tool call is a NEW shell that starts in the working folder: Set-Location / cd never carries over to the next call, so the worker must use full absolute paths in every command and never rely on a previous cd. Quote for PowerShell: inside single quotes, write a single quote as two single quotes; never use the bash '\"'\"' trick. "
    "To write a file whose content has $, quotes or backticks, use a single-quoted here-string so nothing is expanded: "
    "@'<newline>content<newline>'@ | Set-Content -Path C:\\full\\path.ext (the closing '@ must start its own line). "

    'Reply with ONLY JSON: {"verdict": "continue"|"redirect"|"stop", "note": "one sentence", "flag": true|false, '
    '"flag_reason": "only if flag is true - one sentence for the user"}. '
    'Use "redirect" if it has drifted off task, is about to do something wrong or wasteful, or misread the instruction - '
    '"note" becomes the correction sent to it. Use "stop" only if it should not continue at all (looping, doing something '
    'harmful, or clearly pointless). Otherwise use "continue". Set "flag" true whenever something is worth telling the user '
    "about, whether or not you also redirect or stop - a flag does not have to mean failure. Notes must be concrete and actionable. "
    "Always check: (1) a script written with double quotes or a plain here-string had its $variables and $(commands) expanded at write time, "
    "so the file holds fixed values - a script that should print the current time must contain Get-Date, not a fixed date; "
    "(2) commands start in the working folder - if the user named a different folder and the worker used a relative path, the file is in the wrong place; "
    "(3) a command that printed nothing when output was expected did not do the job."
)
OVERLORD_FINAL_SYS = (
    "You are the Overlord. The worker AI says it has finished. Decide if the task is actually done. It is NOT done if the worker only "
    "described, planned or printed what it would do instead of doing it with its tools (for example pasting code instead of writing the "
    "file and running it), if a step it relied on failed, or if the success criteria are not met. "
    'Reply with ONLY JSON: {"done": true|false, "note": "if not done: the exact next thing the worker must do", '
    '"flag": true|false, "flag_reason": "only if flag is true - one sentence for the user"}'
)
OVERLORD_REPORT_SYS = (
    "You are the Overlord reporting back to the user on a task you supervised. You see every step the worker took. "
    'Reply with ONLY JSON, no prose, no code fences: {"summary": "2-3 sentences: what the worker actually did and whether the task is done", '
    '"ok": true|false, "problems": ["one item per thing the worker did wrong, stated exactly: the step or command, what was wrong with it, '
    'and what it should have done instead"], "advice": "your recommendation to the user to get this done - what to change in the request, '
    'the model or the approach", "retry_prompt": "a rewritten, more precise request the user can retry with; empty if ok"}. '
    "The retry_prompt must keep every concrete detail of the original request (machine, folder, file names, features) and must ask for "
    "the work to be done with tools - files written to disk with full absolute paths, then run or checked - never for code to be printed "
    "in the reply. Put the fix for each problem you found into it. "
    "problems must be an empty list if nothing went wrong - do not invent issues."
)


TEAM_TOOLS_DESC = ("tools: can run PowerShell on this PC" if REMOTE else "tools: can search the web and read pages")
if not REMOTE:   # no command runner: workers only have web_search / web_fetch
    _PS_RULES = re.compile(r"Commands run in Windows PowerShell.*?must start its own line\)\. ", re.S)
    _WEB_ONLY = "The worker's only tools are web_search and web_fetch: it cannot run commands or touch files. "
    OVERLORD_TRANSLATE_SYS = _PS_RULES.sub(_WEB_ONLY, OVERLORD_TRANSLATE_SYS).replace("controls this PC via tools", "uses tools")
    OVERLORD_REVIEW_SYS = _PS_RULES.sub(_WEB_ONLY, OVERLORD_REVIEW_SYS)


def _overlord_json(txt):
    s = (txt or "").strip()
    a, b = s.find("{"), s.rfind("}")
    if a >= 0 and b > a:
        body = s[a:b + 1]
        # models write Windows paths with invalid JSON escapes (\S, \B); double only those, keep valid pairs like \\ and \"
        fixed = re.sub(r'\\(.)', lambda m: m.group(0) if m.group(1) in '"\\/bfnrtu' else '\\\\' + m.group(1), body, flags=re.S)
        for cand in (body, fixed):
            try:
                v = json.loads(cand, strict=False)
                if isinstance(v, dict):
                    return v
            except ValueError:
                pass
    return _overlord_salvage(s)


def _overlord_salvage(s):
    """The supervisor's reply was cut off or malformed: pull out whatever fields are still readable."""
    out = {}
    for k in ("verdict", "note", "flag_reason", "instruction", "success", "advice", "retry_prompt", "summary"):
        m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)' % k, s, re.S)
        if m:
            out[k] = m.group(1).replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")
    for k in ("flag", "done", "ok"):
        m = re.search(r'"%s"\s*:\s*(true|false)' % k, s)
        if m:
            out[k] = m.group(1) == "true"
    return out


def _same_note(a, b):
    """Rough check that two corrections say the same thing (worker ignored the first)."""
    wa = set(re.findall(r"[a-z0-9:\\._-]{3,}", a.lower())); wb = set(re.findall(r"[a-z0-9:\\._-]{3,}", b.lower()))
    return bool(wa and wb) and len(wa & wb) / max(1, min(len(wa), len(wb))) >= 0.8


def overlord_run(task, coord, workers, emit, history=None, extra_sys="", max_redirects=4, stop=None):
    """Overlord mode: one supervisor model turns the user's request into a precise instruction, watches every
    tool call each worker makes, can redirect a worker mid-task or stop it, and reports back what happened."""
    def coord_call(msgs, mt, temp):
        if stop and stop():
            raise RuntimeError("stopped")
        if isinstance(coord, str):
            return remote_complete(coord, msgs, mt, temp)
        return agent_complete(coord, msgs, False, max_tokens=mt, temperature=temp, stop=stop)

    coord_card = None if isinstance(coord, str) else card_id_for_port(coord)
    for w in workers:
        w["card"] = card_id_for_port(w.get("port"))
    roster = "\n".join("- %s (%s)" % (w["name"], TEAM_TOOLS_DESC if w["tools"] else "no tools - reasoning/writing only") for w in workers)
    ctx = ""
    if history:
        ctx = "\n\nEarlier in this session:\n" + "\n".join("%s: %s" % (h.get("role"), str(h.get("content"))[:600]) for h in history[-4:])
    folder_rule = ""
    if (cur_ws().get("dir") or "").strip():
        folder_rule = (" The working folder is %s on %s. Unless the user names a different path, tell the worker to stay inside it and use relative paths; if the user names a path, use that path and do not mention the working folder."
                       % (cur_ws()["dir"].strip(), ws_label()))
    tsys = OVERLORD_TRANSLATE_SYS
    emit({"type": "overlord_stage", "text": "translating your request"})
    t0 = time.time()
    try:
        txt, _ = coord_call([{"role": "system", "content": tsys}, {"role": "user", "content":
            "USER REQUEST:\n%s%s\n\nWORKER(S):\n%s%s" % (task, folder_rule, roster, ctx)}], 1200, 0.2)
    except Exception as e:
        if str(e) != "stopped":
            team_stat_add(coord_card, 1, -1)
        raise
    team_stat_add(coord_card, 1, _team_delta(_team_text_ok(txt)))
    blind = 0
    plan = _overlord_json(txt)
    if not plan.get("instruction"):
        blind += 1
    instruction = (plan.get("instruction") or task).strip()
    success = (plan.get("success") or "").strip()
    emit({"type": "overlord_translate", "instruction": instruction, "success": success, "secs": round(time.time() - t0, 1)})

    all_flags = []
    reports = []
    for w in workers:
        if stop and stop():
            raise RuntimeError("stopped")
        wsys = _team_worker_sys("A", w["tools"]) + context_note() + (("\n\n" + extra_sys) if extra_sys else "")
        body = {"messages": [{"role": "system", "content": wsys}, {"role": "user", "content": instruction}],
                "stream": False, "chat_template_kwargs": {"enable_thinking": False}, "max_tokens": 3000, "temperature": 0.4}
        if w["tools"]:
            defs = tools_for_port(w["port"])
            if defs:
                body["tools"] = defs
            _inject_facts(body["messages"])
        emit({"type": "overlord_worker_start", "name": w["name"], "port": w["port"]})
        t1 = time.time()
        redirects = 0
        stopped_note = None
        final_text = ""
        unmet = None
        trail = []
        notes_given = []
        tm = None
        tpf = 0
        for step in range(CFG["agent_max_steps"] + 1):
            if stop and stop():
                raise RuntimeError("stopped")
            try:
                data = _team_upstream(w["port"], body)
            except ToolCallParseError:
                tpf += 1
                fr = "worker's tool call was cut off / malformed (broken JSON arguments, attempt %d) and was not run" % tpf
                all_flags.append("%s: %s" % (w["name"], fr))
                emit({"type": "overlord_flag", "text": fr, "worker": w["name"]})
                if tpf > 3:
                    stopped_note = "kept producing malformed tool calls after %d corrections" % (tpf - 1)
                    emit({"type": "overlord_redirect", "text": "STOPPED - " + stopped_note, "worker": w["name"]})
                    break
                emit({"type": "overlord_redirect", "text": TOOLPARSE_NOTE, "worker": w["name"]})
                body["messages"].append({"role": "user", "content": TOOLPARSE_NOTE})
                continue
            tm = data.get("timings") or tm
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            calls = _fix_tool_ids(msg.get("tool_calls") or [])
            if not calls:
                final_text = _strip(msg.get("content") or "")
                try:
                    ftxt, _ = coord_call([{"role": "system", "content": OVERLORD_FINAL_SYS}, {"role": "user", "content":
                        "INSTRUCTION GIVEN TO WORKER:\n%s\n\nSUCCESS LOOKS LIKE:\n%s\n\nWORKER HAS TOOLS: %s\n\nSTEPS IT TOOK:\n%s\n\nWORKER'S FINAL ANSWER:\n%s"
                        % (instruction, success or "(not specified)", "yes" if w["tools"] else "no",
                           "\n".join(trail[-8:]) or "(none)", final_text[:2500])}], 600, 0.0)
                except Exception:
                    ftxt = ""
                fv = _overlord_json(ftxt)
                if "done" not in fv:
                    blind += 1
                emit({"type": "overlord_check", "stage": "final", "verdict": "done" if fv.get("done", True) else "not done",
                      "note": str(fv.get("note") or "")[:300], "worker": w["name"]})
                if fv.get("flag"):
                    fr = str(fv.get("flag_reason") or fv.get("note") or "").strip()
                    if fr:
                        all_flags.append("%s: %s" % (w["name"], fr))
                        emit({"type": "overlord_flag", "text": fr, "worker": w["name"]})
                if fv.get("done", True) is False:
                    note = str(fv.get("note") or "").strip() or "The task is not finished - do it with your tools."
                    if redirects < max_redirects and step < CFG["agent_max_steps"]:
                        redirects += 1
                        emit({"type": "overlord_redirect", "text": note, "worker": w["name"]})
                        body["messages"].append({"role": "assistant", "content": msg.get("content") or ""})
                        body["messages"].append({"role": "user", "content": "OVERLORD: " + note})
                        final_text = ""
                        continue
                    unmet = note
                break
            if step == CFG["agent_max_steps"]:
                final_text = "(stopped: tool-step limit reached)"
                break
            body["messages"].append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for tc in calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                result = run_tool(name, args, who="overlord " + w["name"])
                emit({"type": "overlord_tool", "name": name, "args": json.dumps(args)[:200], "result": result[:400], "worker": w["name"]})
                body["messages"].append({"role": "tool", "tool_call_id": tc.get("id") or name, "name": name, "content": result})
                try:
                    rtxt, _ = coord_call([{"role": "system", "content": OVERLORD_REVIEW_SYS}, {"role": "user", "content":
                        "INSTRUCTION GIVEN TO WORKER:\n%s\n\nSUCCESS LOOKS LIKE:\n%s\n\nEARLIER STEPS:\n%s\n\nWORKER JUST DID:\ntool %s(%s) -> %s"
                        % (instruction, success or "(not specified)", "\n".join(trail[-6:]) or "(none)", name, json.dumps(args)[:300], result[:600])}], 700, 0.0)
                except Exception:
                    rtxt = ""
                trail.append("%s(%s) -> %s" % (name, json.dumps(args)[:160], result[:200].replace("\n", " ")))
                verdict = _overlord_json(rtxt)
                if not verdict.get("verdict"):
                    blind += 1
                v = str(verdict.get("verdict") or "continue").lower()
                if v == "continue" and str(verdict.get("note") or "").strip() and "[stderr]" in result:
                    v = "redirect"          # it named a fix for a command that just errored: send the fix, do not wave it through
                if v == "stop" and redirects == 0:
                    v = "redirect"          # a review runs after the command already ran, so a first stop saves nothing
                emit({"type": "overlord_check", "stage": "step", "verdict": v, "note": str(verdict.get("note") or "")[:300], "worker": w["name"]})
                if verdict.get("flag"):
                    fr = str(verdict.get("flag_reason") or verdict.get("note") or "").strip()
                    if fr:
                        all_flags.append("%s: %s" % (w["name"], fr))
                        emit({"type": "overlord_flag", "text": fr, "worker": w["name"]})
                note = str(verdict.get("note") or "").strip() or "Reconsider your last step."
                cmd_txt = " ".join(str(x) for x in args.values()) if isinstance(args, dict) else str(args)
                followed = bool(notes_given) and _same_note(cmd_txt, notes_given[-1])
                ignored = v == "redirect" and not followed and any(_same_note(note, g) for g in notes_given)
                if v == "redirect" and redirects < max_redirects and not ignored:
                    redirects += 1
                    notes_given.append(note)
                    emit({"type": "overlord_redirect", "text": note, "worker": w["name"]})
                    body["messages"].append({"role": "user", "content": "OVERLORD: " + note})
                elif v in ("stop", "redirect"):
                    base = str(verdict.get("note") or "").strip()
                    stopped_note = (base or "The overlord ended this task early.") if v == "stop" else \
                        ("ignored the same correction twice: %s" % base if ignored else
                         "still going wrong after %d corrections: %s" % (redirects, base or "no further detail"))
                    emit({"type": "overlord_redirect", "text": "STOPPED - " + stopped_note, "worker": w["name"]})
                    if not verdict.get("flag"):
                        all_flags.append("%s: stopped - %s" % (w["name"], stopped_note))
                        emit({"type": "overlord_flag", "text": "stopped - " + stopped_note, "worker": w["name"]})
                    break
            if stopped_note:
                break
        secs = round(time.time() - t1, 1)
        if stopped_note and not final_text:
            final_text = "(stopped by overlord: %s)" % stopped_note
        if unmet:
            all_flags.append("%s: did not finish - %s" % (w["name"], unmet))
            emit({"type": "overlord_flag", "text": "did not finish - " + unmet, "worker": w["name"]})
        ok = not stopped_note and not unmet and not final_text.startswith("(stopped")
        team_stat_add(w.get("card"), 1, _team_delta(ok))
        emit({"type": "overlord_worker_done", "name": w["name"], "text": final_text, "secs": secs,
             "tps": (tm.get("predicted_per_second") if tm else None), "redirects": redirects})
        reports.append("### %s\nSTEPS:\n%s\nFINAL ANSWER:\n%s" % (w["name"], "\n".join(trail[-12:]) or "(none)", final_text[:2000]))

    if blind:
        msg = "Overlord: %d of its own checks came back unreadable (the supervisor model's answer was cut off or malformed) and were skipped, so this run was partly unsupervised." % blind
        all_flags.append(msg)
        emit({"type": "overlord_flag", "text": msg, "worker": ""})
    emit({"type": "overlord_stage", "text": "reporting"})
    try:
        rtxt, _ = coord_call([{"role": "system", "content": OVERLORD_REPORT_SYS}, {"role": "user", "content":
            "USER'S ORIGINAL REQUEST:\n%s\n\nINSTRUCTION SENT TO WORKER(S):\n%s\n\nWORKER OUTPUT:\n%s\n\nFLAGS RAISED DURING THE TASK:\n%s"
            % (task, instruction, "\n\n".join(reports)[:6000], "\n".join("- " + f for f in all_flags) or "(none)")}], 1600, 0.2)
    except Exception as e:
        rtxt = "(overlord could not produce a final report: %s)" % e
    rj = _overlord_json(rtxt)
    if rj.get("summary"):
        problems = [str(p).strip() for p in (rj.get("problems") or []) if str(p).strip()]
        emit({"type": "overlord_report", "text": str(rj.get("summary")).strip(), "ok": bool(rj.get("ok", True)) and not problems,
              "problems": problems, "advice": str(rj.get("advice") or "").strip(),
              "retry_prompt": str(rj.get("retry_prompt") or "").strip(), "flags": all_flags})
    else:
        emit({"type": "overlord_report", "text": rtxt.strip(), "ok": not all_flags, "problems": list(all_flags),
              "advice": "", "retry_prompt": "", "flags": all_flags})



# ------------------------------------------------------------------ build mode: one step at a time, the coordinator verifies each step
BUILD_RE = re.compile(r"^\s*\[build\]|\b(create|build|write|make|develop|implement|generate)\b[^.\n]{0,100}\b(script|app|application|manager|tool|program|gui|utility|installer|\.ps1|\.py|\.js)\b", re.I)

def _is_build(task):
    if not REMOTE:
        return False   # build mode edits files through the remote PowerShell runner
    t = task or ""
    if re.match(r"\s*\[parallel\]", t, re.I):
        return False
    return bool(BUILD_RE.search(t)) and bool((cur_ws().get("dir") or "").strip())

TEAM_BUILD_SYS = (
    "You are the lead of a small team of local AI models that build ONE deliverable together, on the same file, strictly one step at a time. "
    "You do not write the code yourself. Each step you give ONE worker ONE specific instruction: the exact file name, what to add or change and where "
    "(for example: append after the function X), and every name, parameter and shared variable it must define or use so it fits the code already there. "
    "A worker sees only your instruction - never the task or the other workers' work - so spell out the interfaces. "
    "Plan the file as an ordered list of sections that are APPENDED one after another (helpers and data functions first, then the form and its controls, "
    "then the event handlers, and last the line that shows the form). Every step must leave the file syntactically valid, so each section is complete functions or statements. "
    "Keep a step small enough to finish in one reply (about 150 lines at most). Tell the worker to read the file first, then change it with a tool call "
    "(never paste the code in its reply), and to answer with one line saying what it changed. "
    "After each step the system checks the real file on disk - exists, size, syntax, function list, last lines - and shows you the result. "
    "Move on only when the check passes. If it failed or nothing changed, give a specific fix instruction, switch worker, or revert. "
    "Finish with done only when the file is complete and its last check passed.")

BUILD_WORKER_NOTE = ("\n\nHow to do this step: first read the current file with Get-Content (if it exists). Then change it on disk with one tool call "
    "(for example Add-Content with a here-string to append, or a targeted replace). Do not paste the code into your reply, do not rewrite parts you were not asked to change, "
    "and keep the file syntactically valid. When the tool call has succeeded, reply with one line saying what you changed.")

_BUILD_STATE_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$f = __F__
if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { 'EXISTS: no' } else {
  $i = Get-Item -LiteralPath $f
  'EXISTS: yes'
  'SIZE: ' + $i.Length + ' bytes'
  'HASH: ' + (Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash
  $txt = Get-Content -LiteralPath $f -Raw
  if ($null -eq $txt) { $txt = '' }
  'LINES: ' + @(Get-Content -LiteralPath $f).Count
  if ($f -match '\.ps1$') {
    $t = $null; $e = $null
    [void][System.Management.Automation.Language.Parser]::ParseInput($txt, [ref]$t, [ref]$e)
    if ($e -and $e.Count) { 'SYNTAX: ERRORS ' + $e.Count; $e | Select-Object -First 4 | ForEach-Object { '  line ' + $_.Extent.StartLineNumber + ': ' + $_.Message } } else { 'SYNTAX: OK' }
  } else { 'SYNTAX: not checked (not a .ps1)' }
  'FUNCTIONS: ' + ((Select-String -LiteralPath $f -Pattern '^\s*function\s+([A-Za-z0-9_-]+)' | ForEach-Object { $_.Matches[0].Groups[1].Value }) -join ', ')
  'TAIL:'
  Get-Content -LiteralPath $f -Tail 10
}
"""
_BUILD_NAME_OK = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_ .\-]*(?:[\\/][A-Za-z0-9_][A-Za-z0-9_ .\-]*)*")

def _json_obj(txt):
    txt = txt or ""
    a, b = txt.find("{"), txt.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        j = json.loads(txt[a:b + 1])
    except ValueError:
        return None
    return j if isinstance(j, dict) else None

def team_run_build(task, coord, workers, emit, history=None, extra_sys="", stop=None, max_steps=14):
    t0 = time.time()
    letters = [chr(65 + i) for i in range(len(workers))]
    by = dict(zip(letters, workers))
    for w in workers:
        w["card"] = card_id_for_port(w.get("port"))
    coord_card = None if isinstance(coord, str) else card_id_for_port(coord)
    tool_letters = [L for L in letters if by[L]["tools"]]
    roster = "\n".join("- %s: %s (%s)" % (L, w["name"], "can run PowerShell on this PC and edit files" if w["tools"] else "no tools - cannot write files, never assign a step to it") for L, w in by.items())
    ws = cur_ws(); wdir = (ws.get("dir") or "").strip(); host = ws.get("host") or "local"
    folder_rule = " The working folder is %s on %s. Every file lives inside it and is named with a relative path." % (wdir, ws_label())
    csys = TEAM_BUILD_SYS + ((" " + extra_sys) if extra_sys else "")
    def coord_call(msgs, mt, temp):
        if stop and stop():
            raise RuntimeError("stopped")
        if isinstance(coord, str):
            return remote_complete(coord, msgs, mt, temp)
        return agent_complete(coord, msgs, False, max_tokens=mt, temperature=temp, stop=stop)
    def run_ps(script, timeout=60):
        pre = ("Set-Location -LiteralPath %s\n" % _ps_str(wdir)) if wdir else ""
        return (_ps_on(host, pre + script, timeout=timeout) or "").strip()
    def state_of(f):
        return run_ps(_BUILD_STATE_PS.replace("__F__", _ps_str(f)))
    def parse_state(st):
        m = re.search(r"SIZE: (\d+)", st); l = re.search(r"LINES: (\d+)", st)
        return {"exists": "EXISTS: yes" in st, "size": int(m.group(1)) if m else 0, "lines": int(l.group(1)) if l else 0,
                "syntax_ok": "SYNTAX: OK" in st, "hash": (re.search(r"HASH: ([A-Fa-f0-9]{64})", st).group(1) if re.search(r"HASH: ([A-Fa-f0-9]{64})", st) else "")}
    def snapshot(f, n):
        run_ps("$d = Join-Path $env:TEMP 'mb-build'; New-Item -ItemType Directory -Force $d | Out-Null; "
               "if (Test-Path -LiteralPath %s) { Copy-Item -LiteralPath %s -Destination (Join-Path $d 'step%d.bak') -Force }" % (_ps_str(f), _ps_str(f), n))
    def restore(f, n):
        return run_ps("$b = Join-Path (Join-Path $env:TEMP 'mb-build') 'step%d.bak'; if (Test-Path $b) { Copy-Item -LiteralPath $b -Destination %s -Force; 'restored the file as it was before step %d' } "
                      "else { Remove-Item -LiteralPath %s -Force -ErrorAction SilentlyContinue; 'there was no file before that step - removed it' }" % (n, _ps_str(f), n, _ps_str(f)))
    steps, cur_file, state_text, note = [], "", "", ""
    fail_streak = fails_total = 0
    finished, last_snap, why_stop = False, 0, ""
    emit({"type": "plan", "who": "coordinator", "secs": 0.0, "subtasks": [],
          "text": "Build mode: one step at a time on the same file. The coordinator gives a specific instruction, waits for the worker, checks the real file on disk (exists, size, syntax, functions, last lines), and only then plans the next step."})
    def decide(extra):
        prog = "\n".join("Step %d [worker %s]%s: %s -> %s" % (s["n"], s["w"], " (fix)" if s.get("fix") else "", s["ins"][:260].replace("\n", " "), s["res"][:260].replace("\n", " ")) for s in steps[-8:]) or "(nothing yet - this is the first step)"
        fs = state_text if cur_file else "(no file yet - the first step must create it)"
        msg = ("TASK:\n%s%s\n\nWORKERS:\n%s\n\nPROGRESS SO FAR:\n%s\n\nFILE ON DISK NOW (%s):\n%s\n%s\n\n"
               'Reply with ONLY a JSON object, no prose, no code fences: {"action": "assign" or "revert" or "done", "worker": "A", "file": "relative file name", '
               '"instruction": "the one specific step for this worker", "check": "optional read-only PowerShell command that proves this step", "why": "one line"}'
               % (task, folder_rule, roster, prog, cur_file or "-", fs, extra))
        base = [{"role": "system", "content": csys}, {"role": "user", "content": msg}]
        txt, _ = coord_call(base, 1400, 0.2)
        j = _json_obj(txt)
        if j is None:
            txt, _ = coord_call(base + [{"role": "assistant", "content": txt or ""}, {"role": "user", "content": "That was not valid JSON. Reply with ONLY the JSON object."}], 1400, 0.1)
            j = _json_obj(txt)
        return j
    if not tool_letters:
        emit({"type": "final", "who": "coordinator", "text": "Build mode needs at least one worker with tools to write files; none of the selected workers has any.", "secs": 0.0, "total": 0.0,
              "recovered": 0, "unresolved": 1, "pieces": 0, "workers": len(letters)})
        return
    for n in range(1, max_steps + 1):
        if stop and stop():
            return
        emit({"type": "stage", "who": "coordinator", "text": "step %d: deciding the next instruction" % n})
        try:
            j = decide(note)
        except Exception as e:
            if str(e) == "stopped":
                return
            why_stop = "coordinator call failed: %s" % e
            break
        note = ""
        if not j:
            why_stop = "the coordinator did not return a usable instruction"
            break
        act = str(j.get("action") or "assign").strip().lower()
        if act == "done":
            if cur_file:
                st = parse_state(state_text)
                if not (st["exists"] and st["syntax_ok"]):
                    note = "You answered done, but the last file check is failing (see FILE ON DISK NOW). Fix it before finishing."
                    continue
            finished = True
            break
        if act == "revert" and cur_file and last_snap:
            r = restore(cur_file, last_snap); state_text = state_of(cur_file); fail_streak = 0
            steps.append({"n": n, "w": "-", "ins": "revert the last step", "res": r, "fix": True})
            emit({"type": "stage", "who": "coordinator", "text": "step %d: %s" % (n, r)})
            continue
        L = str(j.get("worker") or "").strip().upper()[:1]
        if L not in tool_letters:
            L = tool_letters[(n - 1) % len(tool_letters)]
        f = str(j.get("file") or cur_file or "").strip()
        if not f or ".." in f or ":" in f or not _BUILD_NAME_OK.fullmatch(f):
            note = "The file name %r is not valid. Use a plain relative name such as AppManager.ps1 inside the working folder." % f
            continue
        ins = str(j.get("instruction") or "").strip()
        if not ins:
            note = "Your answer had no instruction. Give the worker one specific step."
            continue
        cur_file = f; w = by[L]
        before = parse_state(state_of(f))
        snapshot(f, n); last_snap = n
        emit({"type": "worker_start", "who": L, "name": w["name"], "idx": n, "instruction": ins[:300], "after": []})
        t1 = time.time(); tm = None
        try:
            out, tm = agent_complete(w["port"], [{"role": "system", "content": _team_worker_sys(L, True) + context_note()},
                                                 {"role": "user", "content": _with_folder(ins + BUILD_WORKER_NOTE)}], True, emit=emit, tag=L,
                                     max_tokens=CFG.get("team_worker_max_tokens", 12000), stop=stop)
        except Exception as e:
            if str(e) == "stopped":
                return
            out = "(worker failed: %s)" % e
        state_text = state_of(f); after = parse_state(state_text)
        chk, chk_out = str(j.get("check") or "").strip(), ""
        if chk and after["exists"] and after["syntax_ok"]:
            chk_out = run_ps(chk, 90)[:1200]
        ok = after["exists"] and after["syntax_ok"] and bool(after["hash"]) and after["hash"] != before["hash"]
        if not after["exists"]:
            verdict = "FAILED: the file does not exist on disk"
        elif not after["syntax_ok"]:
            verdict = "FAILED: syntax validation did not pass. Non-PowerShell projects should use the coding workspace."
        elif not after["hash"] or after["hash"] == before["hash"]:
            verdict = "FAILED: file content did not change, or SHA256 verification was unavailable"
        else:
            verdict = "PASSED: %d -> %d bytes, %d lines, syntax OK" % (before["size"], after["size"], after["lines"])
        res = (verdict + (" | check output: " + chk_out.replace("\n", " ")[:300] if chk_out else "") + " | worker said: " + (out or "")[:200].replace("\n", " "))
        steps.append({"n": n, "w": L, "ins": ins, "res": res, "ok": ok, "name": w["name"], "verdict": verdict})
        team_stat_add(w.get("card"), 1, _team_delta(ok))
        if ok:
            fail_streak = 0
        else:
            fail_streak += 1; fails_total += 1
            if fail_streak >= 3:
                note = "The last %d steps in a row failed. Switch to the other worker, simplify the step, or revert." % fail_streak
        emit({"type": "worker_done", "who": L, "name": w["name"], "idx": n, "failed": (not ok), "secs": round(time.time() - t1, 1),
              "tps": (tm or {}).get("predicted_per_second"),
              "text": (out or "") + "\n\n--- coordinator check of %s on disk ---\n%s\n%s%s" % (f, verdict, state_text[:900], ("\n--- check command ---\n" + chk_out) if chk_out else "")})
    else:
        why_stop = "reached the %d-step limit without the coordinator finishing" % max_steps
    fin = parse_state(state_of(cur_file)) if cur_file else {"exists": False, "size": 0, "lines": 0, "syntax_ok": False}
    good = finished and fin["exists"] and fin["syntax_ok"]
    lines = []
    lines.append("## Summary")
    if good:
        lines.append("Built `%s` (%d bytes, %d lines) in %d step(s). The file exists on disk and %s." % (cur_file, fin["size"], fin["lines"], len(steps), "passes the syntax check" if cur_file.lower().endswith(".ps1") else "was checked"))
    else:
        lines.append("Not finished: %s. `%s` is %s." % (why_stop or "the coordinator stopped early", cur_file or "(no file)",
                     ("%d bytes, %d lines, syntax %s" % (fin["size"], fin["lines"], "OK" if fin["syntax_ok"] else "BROKEN")) if fin["exists"] else "not on disk"))
    lines.append("\n## Steps")
    for s_ in steps:
        lines.append("%d. [%s %s] %s -> %s" % (s_["n"], s_["w"], s_.get("name", ""), s_["ins"][:160].replace("\n", " "), (s_.get("verdict") or s_["res"])[:140]))
    lines.append("\n## Not verified")
    lines.append("The file was parsed and measured on disk, not launched. A GUI needs an interactive desktop: run `powershell -STA -File .\\%s` in the working folder to try it." % (cur_file or "file.ps1"))
    emit({"type": "final", "who": "coordinator", "text": "\n".join(lines), "secs": 0.0, "tps": None, "total": round(time.time() - t0, 1),
          "recovered": fails_total, "unresolved": 0 if good else 1, "pieces": len(steps), "workers": len(letters)})

def team_run(task, coord, workers, emit, history=None, extra_sys="", max_subtasks=4, stop=None, simulate_fail=0):
    if _is_build(task) and not simulate_fail and any(w["tools"] for w in workers):
        return team_run_build(task, coord, workers, emit, history=history, extra_sys=extra_sys, stop=stop)
    letters = [chr(65 + i) for i in range(len(workers))]
    by = dict(zip(letters, workers))
    for w in workers:
        w["card"] = card_id_for_port(w.get("port"))
    coord_card = None if isinstance(coord, str) else card_id_for_port(coord)
    roster = "\n".join("- %s: %s (%s)" % (L, w["name"], TEAM_TOOLS_DESC if w["tools"] else "no tools - reasoning/writing only") for L, w in by.items())
    csys = TEAM_COORD_SYS + (("\n\n" + extra_sys) if extra_sys else "") + context_note()
    ctx = ""
    if history:
        ctx = "\n\nEarlier in this session:\n" + "\n".join("%s: %s" % (h.get("role"), str(h.get("content"))[:600]) for h in history[-4:])
    emit({"type": "stage", "who": "coordinator", "text": "planning"})
    t0 = time.time()
    folder_rule = ""
    if (cur_ws().get("dir") or "").strip():
        folder_rule = (" The working folder is %s. Every piece that reads or writes files must tell the worker to stay "
                       "inside that folder and use relative paths. Do not send a worker to another drive unless the task names that path."
                       % (cur_ws()["dir"].strip() + " on " + ws_label()))
    plan_msgs = [{"role": "system", "content": csys}, {"role": "user", "content":
        "TASK:\n%s%s%s\n\nWORKERS:\n%s\n\nSplit the task into 1 to %d pieces and assign each to a worker. "
        "Reply with ONLY a JSON object, no prose, no code fences:\n"
        '{"plan": "one line", "subtasks": [{"worker": "A", "instruction": "complete self-contained instruction", "after": []}]}\n'
        "Pieces run at the same time. If a piece truly needs another piece's output, list those piece numbers (1-based) in \"after\" - it then runs later and is given their results. Do not make a piece just to combine or recommend; you do that yourself at the end. Pieces that need this computer or the notes must go to a worker with tools. If the task is small, one piece is fine."
        % (task, folder_rule, ctx, roster, max_subtasks)}]
    def coord_call(msgs, mt, temp):
        if stop and stop():
            raise RuntimeError("stopped")
        if isinstance(coord, str):
            return remote_complete(coord, msgs, mt, temp)
        return agent_complete(coord, msgs, False, max_tokens=mt, temperature=temp, stop=stop)
    try:
        txt, _ = coord_call(plan_msgs, 900, 0.2)
    except Exception as e:
        if str(e) != "stopped":
            team_stat_add(coord_card, 1, -1)
        raise
    team_stat_add(coord_card, 1, _team_delta(_team_text_ok(txt)))
    plan, subs = _parse_plan(txt, letters, task, max_subtasks)
    emit({"type": "plan", "who": "coordinator", "text": plan, "subtasks": subs, "secs": round(time.time() - t0, 1)})
    results = [None] * len(subs)
    done = set()
    stats = {"recovered": 0, "unresolved": 0}
    SIM = {0: int(simulate_fail or 0)}   # test hook: force piece 1 to fail N times
    SOFT = re.compile(r"\b(unable to|could not|couldn't|cannot|can't|failed to|no access|not able to|not permitted)\b")

    def dep_block(i):
        deps = [d for d in subs[i].get("after", []) if d in done]
        if not deps:
            return ""
        return "\n\nResults from earlier pieces you need:\n" + "\n\n".join(
            "### Piece %d result:\n%s" % (d + 1, (results[d] or "(no result)")[:3000]) for d in deps)

    def trace_text(trace):
        return "\n".join("tool %s %s -> %s" % (t.get("name"), t.get("args"), str(t.get("result"))[:300])
                         for t in trace if t.get("type") == "tool")[-2500:]

    def judge(i, ins, out, trace):
        """True if the piece failed. Hard failures are obvious; soft ones get a quick coordinator check."""
        o = (out or "").strip()
        tools = [t for t in trace if t.get("type") == "tool"]
        if not o or o.startswith("(worker failed") or o.startswith("(stopped: tool-step limit"):
            return True
        if tools and all(str(t.get("result", "")).startswith("error") for t in tools):
            return True
        stderr_only = tools and all(("[stderr]" in str(t.get("result", "")) or "[error]" in str(t.get("result", ""))) for t in tools)
        soft = stderr_only or (len(o) < 600 and SOFT.search(o.lower()))
        if not soft:
            return False
        try:
            jt, _ = coord_call([{"role": "system", "content": csys}, {"role": "user", "content":
                "Did this worker complete its piece? A correct report that something does not exist or is empty COUNTS as success. "
                "Failure means it errored, gave up, or did not actually do the piece.\n\nPIECE:\n%s\n\nWORKER OUTPUT:\n%s\n\nTOOL CALLS:\n%s\n\n"
                "Reply with ONLY JSON: {\"ok\": true or false, \"why\": \"short reason\"}" % (ins[:1500], o[:1500], trace_text(trace) or "(none)")}], 200, 0.0)
            a, b = jt.find("{"), jt.rfind("}")
            if a >= 0 and b > a:
                return json.loads(jt[a:b + 1]).get("ok") is not True
        except Exception:
            pass
        return True  # an unreadable reviewer result must not silently approve the worker

    def attempt(i, L, ins, tag_idx):
        w = by[L]
        trace = []
        if SIM.get(i, 0) > 0 and not str(tag_idx).startswith("%ds" % i):
            SIM[i] -= 1
            emit({"type": "worker_start", "who": L, "name": w["name"], "idx": tag_idx, "instruction": ins[:300], "after": []})
            out = "(worker failed: simulated failure for testing)"
            team_stat_add(w.get("card"), 1, -1)
            emit({"type": "worker_done", "who": L, "name": w["name"], "idx": tag_idx, "text": out, "failed": True, "secs": 0.0, "tps": None})
            return out, True, []
        def em(ev):
            trace.append(ev); emit(ev)
        emit({"type": "worker_start", "who": L, "name": w["name"], "idx": tag_idx, "instruction": ins[:300], "after": subs[i].get("after", [])})
        t1 = time.time()
        ran = True
        try:
            out, tm = agent_complete(w["port"], [{"role": "system", "content": _team_worker_sys(L, w["tools"]) + context_note()},
                                                 {"role": "user", "content": _with_folder(ins)}], w["tools"], emit=em, tag=L, max_tokens=CFG.get("team_worker_max_tokens", 12000), stop=stop)
        except Exception as e:
            out, tm = "(worker failed: %s)" % e, None
            ran = str(e) != "stopped" or bool(trace)
        bad = judge(i, ins, out, trace)
        if ran:
            team_stat_add(w.get("card"), 1, _team_delta(not bad))
        emit({"type": "worker_done", "who": L, "name": w["name"], "idx": tag_idx, "text": out, "failed": bool(bad),
              "secs": round(time.time() - t1, 1), "tps": (tm or {}).get("predicted_per_second")})
        return out, bad, trace

    def run_piece(i):
        st = subs[i]; L = st["worker"]
        ins = st["instruction"] + dep_block(i)
        out, bad, trace = attempt(i, L, ins, i)
        if not bad or (stop and stop()):
            results[i] = out
            return
        stats["recovered"] += 1
        # 1) coordinator diagnoses and writes a fix
        emit({"type": "recover", "idx": i, "who": L, "stage": "diagnosing",
              "text": "piece %d failed on worker %s - coordinator diagnosing" % (i + 1, L)})
        diag, fix, mode = "", ins, "same"
        try:
            dtxt, _ = coord_call([{"role": "system", "content": csys}, {"role": "user", "content":
                "A worker failed its piece of the task.\n\nTASK:\n%s\n\nPIECE %d (worker %s: %s, %s):\n%s\n\nWHAT HAPPENED:\nfinal output: %s\ntool calls:\n%s\n\n"
                "Diagnose the failure in one or two sentences, then write a corrected, self-contained instruction that avoids the problem "
                "(" + ("a working PowerShell approach, " if REMOTE else "") + "a narrower scope, or no tools if they are not needed). "
                "Choose who retries: \"same\" (same worker), \"other\" (a different worker) or \"all\" (every worker tries it at once). "
                "Reply with ONLY JSON: {\"diagnosis\": \"...\", \"fix\": \"corrected instruction\", \"retry\": \"same|other|all\"}"
                % (task, i + 1, L, by[L]["name"], "has tools" if by[L]["tools"] else "no tools", ins[:2000], (out or "")[:1500], trace_text(trace) or "(none)")}], 800, 0.2)
            a, b = dtxt.find("{"), dtxt.rfind("}")
            if a >= 0 and b > a:
                dj = json.loads(dtxt[a:b + 1])
                diag = str(dj.get("diagnosis") or "")[:600]
                fix = (str(dj.get("fix") or "").strip() or st["instruction"]) + dep_block(i)
                mode = str(dj.get("retry") or "same").lower()
        except Exception as e:
            diag = "coordinator could not diagnose (%s) - every worker will try it" % e
            mode = "all"
        emit({"type": "recover", "idx": i, "who": L, "stage": "diagnosis", "text": diag or "(no diagnosis)", "retry": mode, "fix": fix[:500]})
        # 2) one targeted retry
        if mode in ("same", "other") and not (stop and stop()):
            R = L
            if mode == "other":
                others = [x for x in letters if x != L]
                if others:
                    R = ([x for x in others if by[x]["tools"] == by[L]["tools"]] or others)[0]
            emit({"type": "recover", "idx": i, "who": R, "stage": "retry", "text": "retrying piece %d on worker %s" % (i + 1, R)})
            out2, bad2, _ = attempt(i, R, fix, "%dr" % i)
            if not bad2:
                results[i] = out2
                emit({"type": "recover", "idx": i, "who": R, "stage": "fixed", "text": "piece %d fixed on retry by worker %s" % (i + 1, R)})
                return
        if stop and stop():
            results[i] = out
            return
        # 3) everyone takes it together
        emit({"type": "recover", "idx": i, "who": "all", "stage": "swarm",
              "text": "all %d workers taking piece %d together" % (len(letters), i + 1)})
        outs = {}
        def sw(X):
            o2, b2, _ = attempt(i, X, fix, "%ds%s" % (i, X))
            outs[X] = (o2, b2)
        if any(w["tools"] for w in workers):
            for X in letters:
                if stop and stop():
                    return
                sw(X)
                if not outs[X][1]:
                    break
        else:
            ths = [ws_thread(sw, X) for X in letters]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
        good = {X: o2 for X, (o2, b2) in outs.items() if not b2}
        if not good:
            stats["unresolved"] += 1
            results[i] = "(piece %d could not be completed after recovery. Diagnosis: %s)\nLast output: %s" % (i + 1, diag, (out or "")[:800])
            emit({"type": "recover", "idx": i, "who": "all", "stage": "gave_up", "text": "piece %d still failing after recovery" % (i + 1)})
            return
        if len(good) == 1:
            results[i] = list(good.values())[0]
        else:
            try:
                mt, _ = coord_call([{"role": "system", "content": csys}, {"role": "user", "content":
                    "Several workers each attempted the same piece.\n\nPIECE:\n%s\n\n%s\n\n"
                    "Merge them into one correct result for this piece: keep what is backed by tool output, drop contradictions. Output only the merged result."
                    % (fix[:1500], "\n\n".join("### Worker %s (%s):\n%s" % (X, by[X]["name"], o2[:2500]) for X, o2 in good.items()))}], 1500, 0.2)
                results[i] = mt
            except Exception:
                results[i] = list(good.values())[0]
        emit({"type": "recover", "idx": i, "who": "all", "stage": "merged",
              "text": "piece %d recovered - merged results from worker(s) %s" % (i + 1, ", ".join(sorted(good)))})

    pending = list(range(len(subs)))
    while pending:
        if stop and stop():
            return
        ready = [i for i in pending if all(d in done for d in subs[i].get("after", []))] or list(pending)
        lanes = {}
        for i in ready:
            lanes.setdefault(subs[i]["worker"], []).append(i)
        def lane(ids):
            for i in ids:
                if stop and stop():
                    return
                run_piece(i)
        if any(w["tools"] for w in workers):
            lanes = {"shared-workspace": ready}  # legacy tools share a folder; isolated parallel coding uses /play
        ths = [ws_thread(lane, ids) for ids in lanes.values()]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        done.update(ready)
        pending = [i for i in pending if i not in done]
    if stop and stop():
        return
    emit({"type": "stage", "who": "coordinator", "text": "combining results"})
    t2 = time.time()
    parts = "\n\n".join("### Piece %d - worker %s (%s)\nInstruction: %s\nResult:\n%s" % (i + 1, st["worker"], by[st["worker"]]["name"], st["instruction"], (results[i] or "(no result)")[:3500]) for i, st in enumerate(subs))
    synth = [{"role": "system", "content": csys}, {"role": "user", "content":
        "TASK:\n%s%s\n\nYour workers returned:\n\n%s\n\nNow write the final answer for the user. Check the results against the task, re-check every number and sum yourself, point out duplicates, "
        "fix mistakes, fill small gaps yourself, and resolve disagreements. If something essential is still missing, say exactly what. Format the answer as a professional IT report in Markdown: start with ## Summary (2-4 lines, the direct answer), then ## Findings (use Markdown tables for any lists of items, values, paths or sizes), then ## Recommended actions (numbered; exact commands in ```powershell code blocks), then ## Notes (risks, assumptions, anything unverified or unresolved). Leave out a section if it has nothing. Be precise and concise - no filler, no pleasantries." % (task, ctx, parts)}]
    try:
        final, tm = coord_call(synth, 2000, 0.3)
    except Exception as e:
        if str(e) != "stopped":
            team_stat_add(coord_card, 1, -1)
        raise
    team_stat_add(coord_card, 1, _team_delta(_team_text_ok(final)))
    emit({"type": "final", "who": "coordinator", "text": final, "secs": round(time.time() - t2, 1), "tps": (tm or {}).get("predicted_per_second"),
          "total": round(time.time() - t0, 1), "recovered": stats["recovered"], "unresolved": stats["unresolved"], "pieces": len(subs), "workers": len(letters)})


def browse_dir(path, host="local"):
    """List drives (empty path) or the subfolders + a few files of a folder on the chosen PC."""
    if not path:
        out = _ps_on(host, r"Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object { $_.DeviceID + [char]92 }")
        roots = []
        for x in out.splitlines():
            x = x.strip().replace("/", "\\")
            if len(x) == 2 and x[1] == ":":
                x += "\\"
            if x:
                roots.append(x)
        return {"path": "", "parent": None, "dirs": roots, "files": []}
    path = path.replace("/", "\\")
    if len(path) == 2 and path[1] == ":":
        path += "\\"
    q = ("$p = " + json.dumps(path) + "; if (!(Test-Path -LiteralPath $p -PathType Container)) { 'ERR:not a folder' } else { "
         "$d = Get-ChildItem -LiteralPath $p -Directory -Force -EA SilentlyContinue | Sort Name | % { $_.Name }; "
         "$f = Get-ChildItem -LiteralPath $p -File -Force -EA SilentlyContinue | Sort Name | Select -First 40 | % { $_.Name }; "
         "[pscustomobject]@{ dirs = @($d); files = @($f); parent = (Split-Path -Path $p -Parent) } | ConvertTo-Json -Depth 3 -Compress }")
    raw = _ps_on(host, q, timeout=20)
    if raw.startswith("ERR:"):
        return {"error": raw[4:]}
    try:
        j = json.loads(raw)
    except ValueError:
        return {"error": "could not read folder"}
    def arr(v):
        return v if isinstance(v, list) else ([v] if v else [])
    return {"path": path, "parent": j.get("parent") or "", "dirs": arr(j.get("dirs")), "files": arr(j.get("files"))}

def _bg(fn, *a):
    threading.Thread(target=fn, args=a, daemon=True).start()

def _bg_load(m, ctx, tools=False):
    r = BENCH.load(m, ctx, tools)
    NOTICE.update(text=(f"loaded {m['name']} for chat in {r['load_s']}s at ctx {ctx}" + (" (tools on)" if tools else "") + " - hit chat in Running now" if r.get("ok") else f"load failed: {r.get('error')}"), ts=time.time())

def _bg_unload(port=None):
    r = BENCH.unload(port)
    NOTICE.update(text=("unloaded, GPU memory released" if r.get("released") else f"unloaded: {r}"), ts=time.time())

def _compact_tool_msgs(msgs, keep_last=2, limit=1200):
    """Shrink older tool results so long web/tool sessions don't run the model out of context."""
    idx = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    for i in (idx[:-keep_last] if keep_last else idx):
        c = msgs[i].get("content") or ""
        if len(c) > limit:
            msgs[i] = dict(msgs[i])
            msgs[i]["content"] = c[:limit] + "\n[... older tool output shortened to save context]"


def _split_reasoning(msg):
    """(thinking, answer) from a chat message: reasoning_content plus any <think> blocks left in the content."""
    think = [str(msg.get("reasoning_content") or "").strip()]
    text = str(msg.get("content") or "")
    think += [t.strip() for t in re.findall(r"<think>(.*?)</think>", text, flags=re.S)]
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "<think>" in text:   # unclosed: the model ran out of tokens mid-thought
        text, _, tail = text.partition("<think>")
        think.append(tail.strip())
    return "\n\n".join(t for t in think if t), text.strip()



def info_payload(host_header):
    import shutil
    host = (AC.get("AIBOX_PUBLIC_HOST") or (host_header or "").split(":")[0] or _socket.gethostname())
    def active(unit):
        try:
            return subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
        except Exception:
            return "unknown"
    def get(url):
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                return json.loads(r.read().decode())
        except Exception:
            return None
    models = get(AC.swap_url() + "/v1/models")
    running = [m["model"] for m in ((get(AC.swap_url() + "/running") or {}).get("running") or [])]
    n = len(models["data"]) if models else 0
    inbox = AC.inbox()
    try:
        os.makedirs(inbox, exist_ok=True)
        files = sum(1 for _, _, fs in os.walk(inbox) for f in fs if f.lower().endswith(".gguf"))
        free_gb = round(shutil.disk_usage(inbox).free / 1e9)
    except OSError:
        files, free_gb = 0, 0
    hf_url = AC.link("hf", host_header) + "/"
    box_url = AC.link("box", host_header) + "/"
    swap_pub = AC.get("AIBOX_SWAP_BIND") not in ("127.0.0.1", "::1", "localhost")
    services = [
        {"name": "aibox-llama-swap", "state": active("aibox-llama-swap"), "note": f"{n} models listed; loaded now: {', '.join(running) or 'none'}"},
        {"name": "aibox-hf-fetch", "state": active("aibox-hf-fetch"), "note": "Hugging Face search and download page"},
        {"name": "aibox-model-scan.timer", "state": active("aibox-model-scan.timer"), "note": "adds files dropped in the inbox, every 30 s"},
        {"name": "aibox-boxagent", "state": active("aibox-boxagent"), "note": "box manager"},
        {"name": "aibox-modelbench", "state": active("aibox-modelbench"), "note": "this page"},
    ] + ([{"name": "aibox-terminal", "state": active("aibox-terminal"), "note": "terminal.. agent"}] if AC.enabled("terminal") else [])
    links = [
        {"label": "Model API (OpenAI style)", "url": (f"http://{host}:{AC.get('AIBOX_SWAP_PORT')}/v1" if swap_pub else AC.swap_url() + "/v1 (this box only)")},
        {"label": "Model API for other machines (every model, token needed)", "url": AC.link("bench", host_header) + "/swap/v1"},
        {"label": "Bench API (models loaded here, token needed)", "url": AC.link("bench", host_header) + "/v1"},
        {"label": "Hugging Face fetch", "url": hf_url, "open": True},
        {"label": "Box manager", "url": box_url, "open": True},
    ]
    notes = [
        "Pick a model by name: any client sends a model id from GET /v1/models. llama-swap loads it on demand and unloads the previous one.",
        "Add models by dropping .gguf files (or a folder) into the inbox, or with + GET MODELS. They join the list within about 30 seconds. A single vision projector (mmproj) in the same folder is paired automatically.",
        "Access: every page and API needs the token in %s (send it as ?key=, an X-Key header or Authorization: Bearer). Trusted addresses (AIBOX_TRUSTED) skip it." % AC.get("AIBOX_TOKEN_FILE"),
        "Settings: /etc/aibox/aibox.env. llama-swap config: %s (hand entries above the AUTO-GENERATED block). Rebuild the list: sudo -u aibox python3 %s/llama-swap/gen_models.py --apply." % (AC.get("AIBOX_SWAP_CONFIG"), AC.get("AIBOX_HOME")),
        "Gated Hugging Face repos need a token in %s." % AC.get("AIBOX_HF_TOKEN_FILE"),
    ]
    return {"services": services, "links": links, "notes": notes, "hf_url": hf_url, "box_url": box_url,
            "inbox": {"path": inbox, "free_gb": free_gb, "files": files,
                      "scp": f"scp model.gguf you@{host}:/tmp/ && sudo -u aibox mv /tmp/model.gguf {inbox}/"}}


def play_page():
    """Classic chat page. Without a remote command daemon the workspace, terminal, jobs and change list are hidden."""
    pg = PLAY.replace("__HOST__", LOCAL_HOST).replace("const REMOTE=false;", "const REMOTE=%s;" % ("true" if REMOTE else "false"))
    css = ""
    if not REMOTE:
        css += ("aside.side details.sec:first-of-type,aside.side details.sec:has(#chglist),.ptab[data-t=term],.ptab[data-t=jobs],#phost,#tstop,"
                ".pview[data-t=term],.pview[data-t=jobs],#sbhost,#sbdir{display:none!important}")
        pg = pg.replace("&#9638; Terminal</button>", "&#9638; Activity</button>", 1)
    if not AC.enabled("harness"):
        css += 'header.top a.btn[href="/play"]{display:none!important}'
    if css:
        pg = pg.replace("</head>", "<style>" + css + "</style></head>", 1)
    return pg


class UI(BaseHTTPRequestHandler):
    server_version = "modelbench/2"
    def log_message(self, *a):
        pass
    def _authed(self):
        return AC.allowed(self, parse_qs(urlparse(self.path).query))
    def _ws_from_headers(self):
        h = (self.headers.get("X-WS-Host") or "").strip()
        if not h:
            return None
        from urllib.parse import unquote
        data = MACH["data"]
        if h.lower() in ("local", LOCAL_HOST.lower()):
            host = "local"
        elif data:   # cached machine list: no re-probe of the other PCs on every request
            host = next((p["id"] for p in data.get("peers") or [] if h.lower() in (p["id"].lower(), p["name"].lower())), None)
        else:
            host = resolve_target(h)
        if not host:
            return {"host": "local", "dir": ""}   # unknown PC: no folder rather than the wrong PC's folder
        return {"host": host, "dir": _norm_dir(unquote(self.headers.get("X-WS-Dir") or ""))}
    def _send(self, code, obj, ctype="application/json"):
        b = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)
    def _ports(self):
        from harness_integration import chat_capable
        ports = {s["port"] for s in SNAP["servers"] if s["kind"] != "router-child" and chat_capable(s)}
        for pt in BENCH.loaded_ports():
            if chat_capable(BENCH.loaded.get(pt) or {}):
                ports.add(pt)
        ports.add(int(AC.get("AIBOX_SWAP_PORT")))   # llama-swap router: its models are chatted through it
        return ports
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if not self._authed():
            if u.path in ("/", "/play", "/play/chat", "/coding"):
                return self._send(401, AC.login_page("modelbench"), "text/html; charset=utf-8")
            return self._send(401, {"error": "token required (?key=, X-Key or Authorization: Bearer)"})
        if (q.get("key") or [""])[0] and u.path in ("/", "/play", "/play/chat", "/coding"):
            # swap ?key= for a cookie so the token does not stay in the address bar
            from urllib.parse import urlencode
            rest = urlencode([(k, v) for k, vs in q.items() if k != "key" for v in vs])
            self.send_response(302); self.send_header("Set-Cookie", AC.cookie_header())
            self.send_header("Location", u.path + ("?" + rest if rest else "")); self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store"); self.end_headers(); return
        if u.path.startswith("/swap/"):
            return self._swap_proxy("GET")
        ws_set(self._ws_from_headers())
        if u.path == "/":
            hh = self.headers.get("Host", "")
            nav = ""
            if AC.enabled("shell"):
                nav += '<a class="btn" href="%s/term/" target="boxshell" title="Browser shell on this box (normal Linux login)">SHELL</a>' % AC.link("box", hh)
            if AC.enabled("terminal"):
                nav += '<a class="btn" href="%s/" target="_blank" rel="noopener" title="terminal.. agent: a model driving its own Linux container and browser">terminal..</a>' % AC.link("terminal", hh)
            return self._send(200, PAGE.replace("__HOST__", LOCAL_HOST).replace("__BOXURL__", AC.link("box", hh) + "/")
                              .replace("__NAVEXTRA__", nav), "text/html; charset=utf-8")
        from harness_integration import handle_request
        if (AC.enabled("harness") or u.path not in ("/play", "/coding")) and handle_request(self, sys.modules[__name__], "GET", u.path, q):
            return
        if u.path in ("/play/chat", "/play", "/coding"):
            return self._send(200, play_page(), "text/html; charset=utf-8")
        if u.path == "/api/info":
            return self._send(200, info_payload(self.headers.get("Host", "")))
        if u.path == "/api/spark":
            return self._send(200, {str(k): {"tps": v["tps"], "busy": v["busy"]} for k, v in list(SPARK.items())})
        if u.path == "/api/state":
            ctx = int((q.get("ctx") or [CFG["ctx"]])[0] or CFG["ctx"])
            last = last_results()
            ms = sort_for_bench(scan_models(ctx))
            _b = BENCH.budget()
            _loaded_ever = ever_loaded()
            for m in ms:
                m["auto_ctx"] = auto_ctx(m, _b)
                m["last"] = last.get(m["path"])
                m["new"] = m["path"] not in _loaded_ever and not m["last"]
                st = team_stat(m["id"])
                m["tasks_used"] = st["times_used"]
                m["tasks_score"] = st["score"]
            return self._send(200, {"state": BENCH.state, "ctx": ctx, "host": host_mem(), "budget": round(BENCH.budget(), 2),
                                    "vram": round(vram_total(), 1), "adapter_gb": SNAP["adapter_gb"], "servers": [dict(s, chat_capable=__import__("harness_integration").chat_capable(s)) for s in SNAP["servers"]],
                                    "snap_age": round(time.time() - SNAP["ts"], 1), "kept": BENCH.kept_info(), "loaded": BENCH.loaded_list(), "max_loaded": CFG["max_loaded"], "remotes": remote_list(),
                                    "notice": NOTICE["text"] if time.time() - NOTICE["ts"] < 90 else "", "models": ms, "task_employed": te_status()})
        if u.path == "/api/result":
            return self._send(200, get_result(int(q.get("id", ["0"])[0])) or {})
        if u.path == "/api/browse":
            host = resolve_target((q.get("host") or [cur_ws()["host"]])[0])
            if not host:
                return self._send(200, {"error": "unknown machine"})
            try:
                return self._send(200, browse_dir((q.get("path") or [""])[0], host))
            except Exception as ex:
                return self._send(200, {"error": "%s: %s" % (target_label(host), ex)})
        if u.path == "/api/context":
            w = cur_ws()
            return self._send(200, {"dir": w["dir"], "host": w["host"], "label": ws_label(w), "server_dir": _norm_dir(CONTEXT["dir"])})
        if u.path == "/api/whoami":
            c = client_machine(self)
            c["label"] = target_label(c["machine"]) if c.get("machine") else None
            return self._send(200, c)
        if u.path == "/api/changes":
            return self._send(200, {"changes": changes_list()})
        if u.path == "/api/machines":
            return self._send(200, machines(force=bool((q.get("refresh") or [""])[0])))
        if u.path == "/api/activity":
            seq, items = act_since(int((q.get("since") or ["0"])[0] or 0))
            return self._send(200, {"seq": seq, "items": items})
        if u.path == "/api/jobs":
            host = resolve_target((q.get("host") or ["local"])[0])
            if not host:
                return self._send(200, {"host": None, "jobs": [], "error": "unknown machine"})
            lim = max(1, min(60, int((q.get("limit") or ["25"])[0] or 25)))
            try:
                return self._send(200, {"host": host, "label": target_label(host), "jobs": list_jobs(host, lim)})
            except Exception as ex:
                return self._send(200, {"host": host, "jobs": [], "error": str(ex)})
        if u.path == "/api/tools":
            port = int((q.get("port") or ["0"])[0] or 0)
            if port not in self._ports():
                return self._send(404, {"error": "no llama-server on that port"})
            defs = self._tools_for(port)
            return self._send(200, {"tools": [ {"name": (d.get("function") or {}).get("name")} for d in defs ]})
        if u.path == "/api/models":
            port = int((q.get("port") or ["0"])[0] or 0)
            if port not in self._ports():
                return self._send(404, {"error": "no llama-server on that port"})
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=5) as r:
                    return self._send(200, r.read().decode())
            except Exception as e:
                return self._send(502, {"error": str(e)})
        if u.path == "/v1/models":
            # OpenAI-compatible model list, aggregated across every loaded port, for clients pointed at /v1 directly.
            data, seen = [], set()
            for port in sorted(BENCH.loaded_ports()):
                k = BENCH.loaded.get(port) or {}
                name = k.get("name") or ("port-%d" % port)
                if name in seen:
                    continue
                seen.add(name)
                data.append({"id": name, "object": "model", "created": int(k.get("since") or time.time()), "owned_by": "modelbench"})
            return self._send(200, {"object": "list", "data": data})
        return self._send(404, {"error": "not found"})
    def do_POST(self):
        if urlparse(self.path).path == AC.LOGIN_PATH:
            return AC.handle_login(self, "modelbench")
        if not self._authed():
            return self._send(403, {"error": "forbidden"})
        if not AC.same_origin(self):
            return self._send(403, {"error": "cross-origin request refused"})
        u = urlparse(self.path); q = parse_qs(u.query)
        ws_set(self._ws_from_headers())
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > 4 * 1024 * 1024:
                return self._send(413, {"error": "request body exceeds 4 MiB"})
        except ValueError:
            return self._send(400, {"error": "invalid Content-Length"})
        raw = self.rfile.read(length) or b"{}"
        if u.path.startswith("/swap/"):
            return self._swap_proxy("POST", raw)
        from harness_integration import handle_request
        if AC.enabled("harness") and handle_request(self, sys.modules[__name__], "POST", u.path, q, raw):
            return
        if u.path == "/api/team":
            return self._team(raw)
        if u.path == "/api/overlord":
            return self._overlord(raw)
        if u.path == "/api/term":
            return self._term(raw)
        if u.path == "/api/chat":
            return self._chat(int((q.get("port") or ["0"])[0] or 0), raw)
        if u.path == "/v1/chat/completions":
            return self._v1_race(raw)
        if u.path == "/api/context":
            b = json.loads(raw or b"{}")
            d = _norm_dir(b.get("dir"))
            host = resolve_target(b.get("host") or "local")
            if not host:
                return self._send(400, {"error": "unknown machine %r" % b.get("host")})
            if d:
                try:
                    ok = "OK" in _ps_on(host, "if (Test-Path -LiteralPath %s -PathType Container) { 'OK' } else { 'NO' }" % _ps_str(d), 25)
                except Exception as ex:
                    return self._send(400, {"error": "could not reach %s: %s" % (target_label(host), ex)})
                if not ok:
                    return self._send(400, {"error": "%s is not a folder on %s" % (d, target_label(host))})
            if host == "local":
                CONTEXT["dir"] = d
                _ctx_save()
            TERM_CWD.pop(host, None)
            log("workspace set to %r on %s" % (d, target_label(host)))
            return self._send(200, {"dir": d, "host": host, "label": target_label(host)})
        body = json.loads(raw)
        if u.path == "/api/pin":
            pin_set(str(body.get("id") or ""), bool(body.get("on")))
            return self._send(200, {"ok": True})
        if u.path == "/api/revert":
            err = revert_change(str(body.get("id") or ""))
            if err:
                return self._send(400, {"error": err})
            return self._send(200, {"ok": True})
        ctx = body.get("ctx") or CFG["ctx"]
        if u.path in ("/api/run", "/api/load"):
            m = next((m for m in scan_models(ctx) if m["id"] == body.get("id")), None)
            if not m:
                return self._send(404, {"error": "no such model"})
            if m.get("downloading"):
                return self._send(409, {"error": "%s is still downloading - wait for it to finish" % m["name"]})
            sh = shared_copy(m["path"], fresh=True)
            if sh and not any(k.get("path") == m["path"] for k in BENCH.loaded.values()):
                if u.path == "/api/run":
                    return self._send(409, {"error": "%s: %s (%s) - a second copy will not fit next to it; unload it there first" % (m["name"], sh["why"], sh["id"])})
                note = "%s: %s as %s - opened a chat on that copy instead of loading a second one" % (m["name"], sh["why"], sh["id"])
                _bg(_swap_warm, sh["id"]); log(note); NOTICE.update(text=note, ts=time.time())
                return self._send(200, {"attach": {"port": int(AC.get("AIBOX_SWAP_PORT")), "model": sh["id"]}, "note": note})
            other = llama_copy_of(m["path"], {k.get("pid") for k in BENCH.loaded.values()}, fresh=True)
            if other:
                return self._send(409, {"error": "%s is already running (%s, port %s) - use that copy, a second one will not fit next to it" % (m["name"], other["owner"], other["port"])})
            if u.path == "/api/run":
                BENCH.enqueue([m], ctx); return self._send(200, {"queued": m["name"]})
            if BENCH.state["busy"]:
                return self._send(409, {"error": "bench busy - a load or test is in progress"})
            lctx = int(body.get("ctx") or 0) or auto_ctx(m, BENCH.budget()) or CTX_STEPS[0]   # empty ctx box on the bench = auto
            _bg(_bg_load, m, lctx, bool(body.get("tools", True))); return self._send(200, {"loading": m["name"], "ctx": lctx})
        if u.path == "/api/unload":
            _bg(_bg_unload, body.get("port")); return self._send(200, {"unloading": True})
        if u.path == "/api/runfit":
            b = BENCH.budget()
            ms = [m for m in scan_models(ctx) if m["est_gb"] <= b and not m["duplicate"] and not m.get("downloading")]
            BENCH.enqueue(ms, ctx); return self._send(200, {"queued": len(ms)})
        if u.path == "/api/stop":
            BENCH.stop.set()
            with BENCH.lock:
                BENCH.queue.clear()
            return self._send(200, {"stopping": True})
        if u.path == "/api/lock":
            if not locklib:
                return self._send(500, {"error": "locklib missing"})
            mid, on = str(body.get("id") or ""), bool(body.get("on"))
            try:
                mid = locklib.resolve(mid)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            subprocess.Popen(["/usr/local/bin/model-lock", "lock" if on else "unlock", mid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            msg = ("locking %s - llama-swap reloads once (it waits until no model is mid-reply), then it stays loaded" if on else "unlocking %s") % mid
            log(msg); NOTICE.update(text=msg, ts=time.time())
            return self._send(200, {"ok": True, "msg": msg})
        if u.path == "/api/killpid":
            pid = int(body.get("pid") or 0)
            _lp = locklib.locked_pids() if locklib else {}
            if pid in _lp:
                return self._send(423, {"error": "%s is locked - unlock it first (lock button, or: model-lock unlock %s)" % (_lp[pid], _lp[pid])})
            for pt, k in list(BENCH.loaded.items()):
                if k["pid"] == pid:
                    _bg(_bg_unload, pt); return self._send(200, {"unloading": True})
            if pid not in llama_pids():
                return self._send(404, {"error": f"pid {pid} is not a llama-server"})
            mid = swap_unload_pid(pid)
            if mid:
                log("unloaded %s through llama-swap (pid %d) from the UI" % (mid, pid))
                NOTICE.update(text="unloaded %s through llama-swap" % mid, ts=time.time()); return self._send(200, {"unloaded": mid})
            kill_tree(pid); log(f"stopped llama-server pid {pid} from the UI")
            NOTICE.update(text=f"stopped pid {pid}", ts=time.time()); return self._send(200, {"killed": pid})
        if u.path == "/api/killall":
            BENCH.stop.set(); kill_all_llama(); BENCH.loaded.clear()
            return self._send(200, {"killed": True})
        return self._send(404, {"error": "not found"})
    def _tools_for(self, port):
        return tools_for_port(port)

    def _overlord(self, raw):
        try:
            body = json.loads(raw)
        except Exception:
            return self._send(400, {"error": "bad json"})
        task = (body.get("task") or "").strip()
        ports = self._ports()
        c = body.get("coordinator")
        try:
            coord = c if (isinstance(c, str) and c in CFG["remote_coordinators"]) else int(c or 0)
            wports = [int(x) for x in (body.get("workers") or [])]
        except (TypeError, ValueError):
            return self._send(400, {"error": "bad overlord or worker ports"})
        wports = [x for x in wports if x in ports]
        if isinstance(coord, str) and not remote_key(coord):
            return self._send(400, {"error": "no API key for " + coord + " yet"})
        if not task or (not isinstance(coord, str) and coord not in ports) or not wports:
            return self._send(400, {"error": "need a task, a running overlord and at least one running worker"})
        wl = []
        for x in wports:
            k = BENCH.loaded.get(x)
            name = k["name"] if k else next((sv["model"] for sv in SNAP["servers"] if sv["port"] == x), "model")
            wl.append({"port": x, "name": name, "tools": bool(k and k.get("tools")) or (k is None and CFG.get("builtin_tools", True))})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        lock = threading.Lock(); alive = {"ok": True}
        def emit(ev):
            if not alive["ok"]:
                return
            with lock:
                try:
                    self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n"); self.wfile.flush()
                except Exception:
                    alive["ok"] = False
        log("overlord: supervisor %s, workers %s, task %r" % (coord, [w["port"] for w in wl], task[:80]))
        code_before = _code_state(_snap_root())
        try:
            overlord_run(task, coord, wl, emit, history=body.get("history") or [], extra_sys=(body.get("system") or "").strip(),
                         stop=lambda: not alive["ok"])
        except Exception as e:
            emit({"type": "error", "text": str(e)})
        finally:
            root = _snap_root()
            if code_before is not None and root:
                _record_change(code_before, _code_state(root), (task or "overlord").strip().splitlines()[0][:80], root)
        emit({"type": "done"})
        try:
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
        except Exception:
            pass

    def _team(self, raw):
        try:
            body = json.loads(raw)
        except Exception:
            return self._send(400, {"error": "bad json"})
        task = (body.get("task") or "").strip()
        ports = self._ports()
        c = body.get("coordinator")
        try:
            coord = c if (isinstance(c, str) and c in CFG["remote_coordinators"]) else int(c or 0)
            wports = [int(x) for x in (body.get("workers") or [])]
        except (TypeError, ValueError):
            return self._send(400, {"error": "bad coordinator or ports"})
        wports = [x for x in wports if x in ports]
        if isinstance(coord, str) and not remote_key(coord):
            return self._send(400, {"error": "no API key for " + coord + " yet"})
        if not task or (not isinstance(coord, str) and coord not in ports) or not wports:
            return self._send(400, {"error": "need a task, a running coordinator and at least one running worker"})
        wl = []
        for x in wports:
            k = BENCH.loaded.get(x)
            name = k["name"] if k else next((sv["model"] for sv in SNAP["servers"] if sv["port"] == x), "model")
            wl.append({"port": x, "name": name, "tools": bool(k and k.get("tools")) or (k is None and CFG.get("builtin_tools", True))})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        lock = threading.Lock(); alive = {"ok": True}
        def emit(ev):
            if not alive["ok"]:
                return
            with lock:
                try:
                    self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n"); self.wfile.flush()
                except Exception:
                    alive["ok"] = False
        log("team: coordinator %s, workers %s, task %r" % (coord, [w["port"] for w in wl], task[:80]))
        code_before = _code_state(_snap_root())
        try:
            team_run(task, coord, wl, emit, history=body.get("history") or [], extra_sys=(body.get("system") or "").strip(),
                     max_subtasks=max(1, min(8, int(body.get("max_subtasks") or 4))), stop=lambda: not alive["ok"],
                     simulate_fail=int(body.get("simulate_fail") or 0))
        except Exception as e:
            emit({"type": "error", "text": str(e)})
        finally:
            root = _snap_root()
            if code_before is not None and root:
                _record_change(code_before, _code_state(root), (task or "team").strip().splitlines()[0][:80], root)
        emit({"type": "done"})
        try:
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
        except Exception:
            pass

    def _term(self, raw):
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        cmd = (body.get("cmd") or "").rstrip()
        host = resolve_target(body.get("host"))
        if not host:
            return self._send(400, {"error": "unknown machine %r" % body.get("host")})
        expect = target_label(host)
        if not cmd.strip():
            return self._send(400, {"error": "empty command"})
        wrapped = _term_wrap(cmd, host)
        e = act_add(kind="term", who="terminal", tool="terminal", host=expect,
                    detail=cmd[:600], state="run")
        t0 = time.time()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        st8 = {"cwd": None, "jid": None, "status": "Completed", "code": None, "keep": [], "real": None}

        def emit_text(text, err=False):
            m = re.search("\x1fCWD ([^\r\n]*)", text)
            if m:
                st8["cwd"] = m.group(1).strip()
                text = text[:m.start()] + text[m.end():]
                text = text.rstrip("\r\n") + ("\n" if text.strip() else "")
            m2 = re.search("\x1fHOST ([^\r\n]*)", text)
            if m2:
                st8["real"] = m2.group(1).strip().upper() or None
                text = text[:m2.start()] + text[m2.end():]
                text = text.rstrip("\r\n") + ("\n" if text.strip() else "")
            if text:
                if len(st8["keep"]) < 60:
                    st8["keep"].append(text)
                self._sse({"t": "out", "text": text, "err": err})
        try:
            if host != "local":
                self._sse({"t": "out", "text": "", "err": False})
                out, code = peer_run(wrapped, 600000, peer=host)
                emit_text(out + ("\n" if out and not out.endswith("\n") else ""))
                st8["code"] = code
                st8["status"] = "Completed" if code in (None, 0) else "Failed"
            else:
                j = json.loads(_aissh_req("/jobs", {"command": wrapped, "timeoutMs": 600000}))
                st8["jid"] = j.get("jobId")
                if not st8["jid"]:
                    raise RuntimeError("aissh6 did not start the job: " + json.dumps(j)[:200])
                since = 0
                while True:
                    time.sleep(0.25)
                    st = json.loads(_aissh_req("/jobs/%s?since=%d" % (st8["jid"], since)))
                    for f in st.get("frames") or []:
                        if f.get("text"):
                            emit_text(f["text"], err=(str(f.get("stream") or "").lower() in ("stderr", "error")))
                    since = st.get("nextCursor") or since
                    if st.get("status") in ("Completed", "Failed", "Cancelled", "TimedOut"):
                        st8["status"], st8["code"] = st.get("status"), st.get("exitCode")
                        if st.get("failureReason") and st8["status"] != "Completed":
                            emit_text(str(st.get("failureReason")) + "\n", err=True)
                        break
            if st8["cwd"]:
                TERM_CWD[host] = st8["cwd"]
            self._sse({"t": "end", "status": st8["status"], "exit": st8["code"],
                       "secs": round(time.time() - t0, 1), "cwd": st8["cwd"], "host": st8["real"], "expected": expect,
                       "mismatch": bool(st8["real"] and st8["real"] != expect.upper())})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            st8["status"] = "Cancelled"
            if st8["jid"]:
                try:
                    _aissh_req("/jobs/" + st8["jid"], timeout=10, method="DELETE")
                except Exception:
                    pass
        except Exception as ex:
            st8["status"] = "Failed"
            try:
                self._sse({"t": "error", "text": str(ex)})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass
        finally:
            act_set(e, host=(st8["real"] or expect), state=("done" if st8["status"] == "Completed" else "fail"), secs=round(time.time() - t0, 1),
                    result=("".join(st8["keep"]) or st8["status"])[:1500])

    def _sse(self, obj):
        self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n"); self.wfile.flush()

    def _upstream(self, port, body):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=900)
        try:
            conn.request("POST", "/v1/chat/completions", body=json.dumps(body).encode(),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read())
        finally:
            conn.close()
        return data

    def _chat(self, port, raw):
        if port not in self._ports():
            return self._send(404, {"error": "no llama-server on that port"})
        try:
            body = json.loads(raw)
        except Exception:
            return self._send(400, {"error": "bad json"})
        k = BENCH.loaded.get(port)
        tools_on = bool(body.get("tools")) or (k and k.get("tools")) or (k is None and CFG.get("builtin_tools", True) and bool(context_note().strip()))
        note = context_note()
        if note:
            msgs = body.setdefault("messages", [])
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") == "user":
                    msgs[i]["content"] = _with_folder(msgs[i].get("content") or "")
                    break
        _kw = body.get("chat_template_kwargs") if isinstance(body.get("chat_template_kwargs"), dict) else {}
        if "enable_thinking" not in _kw:
            _kw = dict(_kw); _kw["enable_thinking"] = bool(CFG.get("chat_thinking", False))
            body["chat_template_kwargs"] = _kw
        # No tools: transparent streaming passthrough (original fast path).
        if not tools_on:
            return self._passthru(port, json.dumps(body).encode())
        if not body.get("tools"):
            defs = self._tools_for(port)
            if defs:
                body["tools"] = defs
        if note:
            msgs = body.get("messages") or []
            if msgs and msgs[0].get("role") == "system":
                msgs[0]["content"] = (msgs[0].get("content") or "") + note
            else:
                body["messages"] = [{"role": "system", "content": "You control " + LOCAL_HOST + " via tools." + note}] + msgs
        _inject_facts(body.setdefault("messages", []))
        # Tools on: run the agent loop, execute tool calls here, stream progress as SSE.
        body["stream"] = False
        _rec = (k or {}).get("rec") or {}
        _asked = int(body.get("max_tokens") or 0)
        _client = (self.headers.get("User-Agent") or "?")[:40]
        if CFG.get("chat_reasoning", "auto") != "off" and body["chat_template_kwargs"].get("enable_thinking") and (not _asked or _asked < 1024):
            body["max_tokens"] = max(1024, int(_rec.get("max_tokens") or 1024))   # thinking eats the budget first
            if _asked:
                log("chat :%d max_tokens %d raised to %d (reasoning model needs room to think; client %s)" % (port, _asked, body["max_tokens"], _client))
        cid, created = "chatcmpl-" + secrets.token_hex(8), int(time.time())
        model = body.get("model") or "model"
        code_before = _code_state(_snap_root())
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for step in range(CFG["agent_max_steps"] + 1):
                data = self._upstream(port, body)
                _ctx = int((BENCH.loaded.get(port) or {}).get("ctx") or CFG["ctx"])
                _tm = data.get("timings") or {}
                if (_tm.get("draft_n") or 0) >= 150 and (_tm.get("draft_n_accepted") or 0) / _tm["draft_n"] < 0.35 and k:
                    mtp_off_add(k["path"], "draft acceptance %d%% over %d tokens" % (round(100 * _tm["draft_n_accepted"] / _tm["draft_n"]), _tm["draft_n"]))
                if ((data.get("usage") or {}).get("prompt_tokens") or 0) > 0.6 * _ctx:
                    _compact_tool_msgs(body["messages"])
                msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
                calls = _fix_tool_ids(msg.get("tool_calls") or [])
                think, said = _split_reasoning(msg)
                if think:
                    self._sse({"choices": [{"index": 0, "delta": {"reasoning_content": think + "\n\n"}, "finish_reason": None}]})
                if not calls:
                    text = said
                    if not text.strip() and step > 0:   # tools ran but the model gave no closing text: ask once for a summary
                        try:
                            b2 = dict(body); b2.pop("tools", None); b2.pop("tool_choice", None)
                            b2["messages"] = list(body["messages"]) + [{"role": "user", "content": "Summarize briefly what you just did and the result. Plain text, no tool calls."}]
                            d2 = self._upstream(port, b2)
                            t2, text = _split_reasoning((d2.get("choices") or [{}])[0].get("message") or {})
                            if t2:
                                self._sse({"choices": [{"index": 0, "delta": {"reasoning_content": t2 + "\n\n"}, "finish_reason": None}]})
                        except Exception:
                            pass
                    fin = (data.get("choices") or [{}])[0].get("finish_reason")
                    for _ in range(2):   # reply stopped at max_tokens: ask it to carry on
                        if fin != "length" or not text.strip() or ((data.get("usage") or {}).get("total_tokens") or 0) >= _ctx - 256:
                            break
                        try:
                            b3 = dict(body); b3.pop("tools", None); b3.pop("tool_choice", None)
                            b3["max_tokens"] = max(int(body.get("max_tokens") or 0), int(_rec.get("max_tokens") or 0), 1024)
                            b3["messages"] = list(body["messages"]) + [{"role": "assistant", "content": text},
                                {"role": "user", "content": "Continue exactly where you stopped. Do not repeat anything already written."}]
                            data = self._upstream(port, b3)
                            t3, more = _split_reasoning((data.get("choices") or [{}])[0].get("message") or {})
                            if t3:
                                self._sse({"choices": [{"index": 0, "delta": {"reasoning_content": t3 + "\n\n"}, "finish_reason": None}]})
                            if not more.strip():
                                break
                            text = text + ("" if text.endswith((" ", "\n")) else " ") + more
                            fin = (data.get("choices") or [{}])[0].get("finish_reason")
                        except Exception:
                            break
                    _u = data.get("usage") or {}
                    log("chat :%d final: finish=%s steps=%d chars=%d think=%d prompt=%s total=%s ctx=%d max_tokens=%s asked=%s client=%s tail=%r" % (
                        port, fin, step, len(text), len(think or ""), _u.get("prompt_tokens"), _u.get("total_tokens"), _ctx,
                        body.get("max_tokens"), _asked or "-", _client, text[-60:]))
                    if fin == "length":
                        text += "\n\n[reply cut off: " + (("the context window (%d tokens) is full - start a new chat, or load the model with a larger context" % _ctx)
                                 if (_u.get("total_tokens") or 0) >= _ctx - 256 else
                                 ("it hit the Max tokens limit (%s) - raise Max tokens in the sidebar" % body.get("max_tokens"))) + "]"
                    if not text.strip():
                        text = "[model returned no text - %d tool step(s), finish=%s]" % (step, (data.get("choices") or [{}])[0].get("finish_reason"))
                    self._sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                               "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})
                    self._sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                               "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "timings": data.get("timings")})
                    self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
                    return
                if step == CFG["agent_max_steps"]:
                    self._sse({"choices": [{"index": 0, "delta": {"content": "\n[stopped: tool-step limit reached]"}, "finish_reason": "stop"}]})
                    self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
                    return
                body["messages"].append({"role": "assistant", "content": said, "tool_calls": calls})
                for tc in calls:
                    fn = (tc.get("function") or {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    self._sse({"choices": [{"index": 0, "delta": {"content": f"\n[tool: {name} {json.dumps(args)[:160]}]\n"}, "finish_reason": None}]})
                    result = run_tool(name, args, who=":%d %s" % (port, model))
                    self._sse({"choices": [{"index": 0, "delta": {"content": f"[result] {result[:400]}{' ...' if len(result) > 400 else ''}\n\n"}, "finish_reason": None}]})
                    body["messages"].append({"role": "tool", "tool_call_id": tc.get("id") or name, "name": name, "content": result})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as e:
            try:
                self._sse({"error": {"message": str(e)}}); self.wfile.write(b"data: [DONE]\n\n")
            except Exception:
                pass
        finally:
            root = _snap_root()
            if code_before is not None and root:
                _record_change(code_before, _code_state(root), "chat", root)

    def _swap_proxy(self, method, raw=None):
        """/swap/<path> -> llama-swap /<path>, behind the aibox token. Lets other machines use every model on
        the box (loaded on demand) without opening llama-swap itself: base URL http://<box>:<bench port>/swap/v1."""
        u = urlparse(self.path)
        path = u.path[len("/swap"):] or "/"
        q = [(k, v) for k, v in parse_qs(u.query).items() if k != "key"]
        if q:
            from urllib.parse import urlencode
            path += "?" + urlencode(q, doseq=True)
        host = AC.swap_url().split("//", 1)[1]
        h, _, pt = host.rpartition(":")
        conn = http.client.HTTPConnection(h, int(pt), timeout=900)
        try:
            conn.request(method, path, body=raw, headers={"Content-Type": self.headers.get("Content-Type") or "application/json"})
            resp = conn.getresponse()
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type", "application/json"))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                chunk = resp.read1(8192)
                if not chunk:
                    break
                self.wfile.write(chunk); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            try:
                self._send(502, {"error": "llama-swap: %s" % e})
            except Exception:
                pass
        finally:
            conn.close()

    def _passthru(self, port, raw):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=900)
        try:
            conn.request("POST", "/v1/chat/completions", body=raw, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type", "application/json"))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                chunk = resp.read1(8192)
                if not chunk:
                    break
                self.wfile.write(chunk); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            try:
                self._send(502, {"error": str(e)})
            except Exception:
                pass
        finally:
            conn.close()

    def _v1_race(self, raw):
        """OpenAI-compatible /v1/chat/completions that fans one request out to every loaded model at once
        and returns whichever answers first. One loaded model: plain passthrough, same as /api/chat.
        The request is forwarded as-is (tool_calls included) - the caller's own loop executes any tools,
        the same way a single llama-server would; this endpoint never runs the bench's own tool loop, since
        racing the same tool call through two models at once could let both act on the machine at the same time."""
        ports = sorted(BENCH.loaded_ports())
        if not ports:
            return self._send(503, {"error": "no model loaded"})
        if len(ports) == 1:
            return self._passthru(ports[0], raw)
        try:
            body = json.loads(raw)
        except Exception:
            return self._send(400, {"error": "bad json"})
        want_stream = bool(body.get("stream"))
        rbody = dict(body); rbody["stream"] = False
        payload = json.dumps(rbody).encode()
        lock = threading.Lock()
        result, conns, errors = {}, {}, []
        done = threading.Event()

        def worker(port):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=900)
            with lock:
                conns[port] = conn
            try:
                conn.request("POST", "/v1/chat/completions", body=payload, headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                data = resp.read()
                if resp.status >= 400:
                    raise RuntimeError("status %d" % resp.status)
                with lock:
                    if "winner" not in result:
                        result["winner"] = (port, data)
                        done.set()
            except Exception as e:
                with lock:
                    errors.append("%d: %s" % (port, e))

        t0 = time.time()
        threads = [threading.Thread(target=worker, args=(p,), daemon=True) for p in ports]
        for t in threads:
            t.start()
        done.wait(900)
        with lock:
            winner = result.get("winner")
            losers = [p for p in conns if not winner or p != winner[0]]
        for p in losers:               # closing the socket stops that model generating - no wasted GPU time
            try:
                conns[p].close()
            except Exception:
                pass
        if not winner:
            return self._send(502, {"error": "every loaded model failed to respond: " + "; ".join(errors)})
        port, data = winner
        name = (BENCH.loaded.get(port) or {}).get("name", "?")
        log("race: %s (:%d) won in %.1fs over %s" % (name, port, time.time() - t0, [p for p in ports if p != port]))
        try:
            d = json.loads(data)
        except Exception:
            d = None
        if want_stream and d:
            msg = (d.get("choices") or [{}])[0].get("message") or {}
            cid = d.get("id") or ("chatcmpl-" + secrets.token_hex(8))
            created = d.get("created") or int(time.time())
            model = d.get("model") or name
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self._sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                           "choices": [{"index": 0, "delta": {"role": "assistant", "content": msg.get("content") or "",
                                        "tool_calls": msg.get("tool_calls")} if msg.get("tool_calls") else
                                       {"content": msg.get("content") or ""}, "finish_reason": None}]})
                fin = (d.get("choices") or [{}])[0].get("finish_reason", "stop")
                self._sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                           "choices": [{"index": 0, "delta": {}, "finish_reason": fin}], "usage": d.get("usage")})
                self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

def serve():
    BENCH.adopt()
    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(target=_spark_loop, daemon=True).start()
    srv = ThreadingHTTPServer((CFG["ui_host"], CFG["ui_port"]), UI)
    if not AC.token():
        log("no access token in %s - every request will be refused" % AC.get("AIBOX_TOKEN_FILE"))
    log(f"modelbench UI on http://{CFG['ui_host']}:{CFG['ui_port']}/  model memory={vram_total():.1f} GB")
    srv.serve_forever()

def main():
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").add_argument("--ctx", type=int)
    r = sub.add_parser("run"); r.add_argument("name"); r.add_argument("--ctx", type=int); r.add_argument("--allow-concurrent", action="store_true")
    sub.add_parser("serve"); sub.add_parser("running")
    a = ap.parse_args()
    if a.cmd == "serve":
        return serve()
    if a.cmd == "running":
        take_snapshot()
        print(json.dumps({"adapter_gb": SNAP["adapter_gb"], "servers": SNAP["servers"]}, indent=1)); return
    if a.cmd == "list":
        b = BENCH.budget()
        print(f"VRAM {vram_total():.1f} GB, free for a model {b:.1f} GB, ctx {a.ctx or CFG['ctx']}")
        for m in scan_models(a.ctx):
            print(f"{'FITS' if m['est_gb'] <= b else 'NO  '} {m['size_gb']:6.1f} GB  est {m['est_gb']:6.1f}  kv {m['kv_gb']:5.2f}  {m['arch']:<12} {m['name']}{'  (dup)' if m['duplicate'] else ''}")
        return
    if a.cmd == "run":
        ms = [m for m in scan_models(a.ctx) if a.name.lower() in m["name"].lower() and not m["duplicate"]]
        if not ms:
            sys.exit(f"no model matching {a.name!r}")
        res = BENCH.run(ms[0], a.ctx, a.allow_concurrent)
        print(json.dumps({k: v for k, v in res.items() if k not in ("answers", "log_tail")}, indent=1))
        for x in res["answers"]:
            print(f"[{'PASS' if x['pass'] else 'FAIL'}] {x['check']}: {x['answer'][:160]!r}")

if __name__ == "__main__":
    main()

