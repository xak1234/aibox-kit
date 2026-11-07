#!/usr/bin/env python3
"""hf_fetch.py - search Hugging Face for GGUF models and download them into the llama-swap inbox.
Open http://<box>:9070/ and enter the aibox access token. Optional Hugging Face token for gated repos in
AIBOX_HF_TOKEN_FILE (default /etc/aibox/hf_token). Downloads resume, are verified against the repo's sha256 and
land in <inbox>/<owner>__<repo>/, where gen_models.py adds them to llama-swap."""
import hashlib, json, os, re, secrets, shutil, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import aibox_conf as AC  # noqa: E402

INBOX = Path(AC.inbox())
PART = INBOX / ".partial"
STATE = Path(AC.data("hf-fetch"))
PORT = AC.port("hf")
GEN = Path(AC.get("AIBOX_HOME")) / "llama-swap" / "gen_models.py"
MAX_GB = AC.model_budget_gb()
FLOOR = AC.num("AIBOX_MIN_FREE_DISK_GB", 10) * 1e9
HF = "https://huggingface.co"
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
QUANT_RE = re.compile(r"(IQ\d(?:_[A-Z0-9]+)+|Q\d(?:_[A-Z0-9]+)+|BF16|F16|F32|MXFP4)", re.I)
SHARD_RE = re.compile(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$")

extdrives = None   # external-drive targets are not part of the kit


def ext_base(dest):
    """<drive>/models for a mounted external drive, or raise. '' / 'internal' -> None (the inbox)."""
    if not dest or dest == "internal":
        return None
    if not extdrives or not extdrives.is_target(dest):
        raise ValueError("that drive is not mounted")
    return Path(dest.rstrip("/")) / "models"


def ext_bases():
    if not extdrives:
        return []
    try:
        return [(d["mount"], d["label"], Path(d["mount"]) / "models") for d in extdrives.drives(usage=False) if d["mounted"]]
    except Exception:  # noqa: BLE001
        return []


STATE.mkdir(parents=True, exist_ok=True)
INBOX.mkdir(parents=True, exist_ok=True)
PART.mkdir(exist_ok=True)
def hf_token():
    try:
        return Path(AC.get("AIBOX_HF_TOKEN_FILE")).read_text().strip()
    except OSError:
        return ""


def hf_open(url):
    h = {"User-Agent": "hf-fetch/1"}
    if hf_token():
        h["Authorization"] = "Bearer " + hf_token()
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30)


def search(q, sort):
    sort = sort if sort in ("downloads", "likes", "lastModified", "trendingScore") else "downloads"
    url = (f"{HF}/api/models?search={urllib.parse.quote(q)}&filter=gguf&sort={sort}&direction=-1&limit=24"
           "&expand[]=downloads&expand[]=likes&expand[]=lastModified&expand[]=createdAt&expand[]=gated")
    with hf_open(url) as r:
        data = json.load(r)
    return [{"id": m["id"], "downloads": m.get("downloads", 0), "likes": m.get("likes", 0),
             "modified": (m.get("lastModified") or "")[:10], "modified_iso": m.get("lastModified") or "",
             "created_iso": m.get("createdAt") or "", "gated": bool(m.get("gated"))} for m in data]


def tree(repo):
    url, out = f"{HF}/api/models/{repo}/tree/main?recursive=true", []
    while url and len(out) < 4000:
        with hf_open(url) as r:
            out += json.load(r)
            m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link", ""))
            url = m.group(1) if m else None
    return out


def group_files(entries):
    groups, proj = {}, []
    for e in entries:
        p = e.get("path", "")
        if e.get("type") != "file" or not p.lower().endswith(".gguf"):
            continue
        lfs = e.get("lfs") or {}
        f = {"path": p, "size": lfs.get("size") or e.get("size") or 0, "oid": lfs.get("oid")}
        if "mmproj" in p.lower():
            proj.append(f)
            continue
        m = SHARD_RE.match(os.path.basename(p))
        key = (os.path.dirname(p), m.group(1)) if m else (os.path.dirname(p), os.path.basename(p)[:-5])
        groups.setdefault(key, []).append(f)
    rows = []
    for (d, name), files in groups.items():
        size = sum(f["size"] for f in files)
        q = QUANT_RE.search(name)
        rows.append({"name": name, "quant": q.group(1).upper() if q else "", "size": size, "shards": len(files),
                     "fits": size <= MAX_GB * 1e9, "files": files})
    rows.sort(key=lambda r: r["size"])
    return rows, sorted(proj, key=lambda f: f["size"])


# ---------------------------------------------------------------- download jobs (one at a time)
JOBS, QUEUE, LOCK = {}, [], threading.Lock()


def run_gen():
    r = subprocess.run([sys.executable, str(GEN), "--apply"], capture_output=True, text=True)
    return (r.stdout.strip().splitlines() or ["(no output)"])[-1]


def download_job(job):
    repo, files = job["repo"], job["files"]
    base = ext_base(job.get("dest"))
    if base is not None:
        extdrives.ensure_rw(job["dest"])
        base.mkdir(parents=True, exist_ok=True)
    part_dir = (base / ".partial" if base is not None else PART) / repo.replace("/", "__")
    dest_dir = (base if base is not None else INBOX) / repo.replace("/", "__")
    part_dir.mkdir(parents=True, exist_ok=True)
    have = sum(p.stat().st_size for p in part_dir.glob("*.gguf"))
    need = max(0, int(job["total"] - have))
    free = shutil.disk_usage(part_dir).free
    if free - need < FLOOR:
        raise RuntimeError("refused: %.1f GB needed, %.1f GB free, and the disk must keep %.0f GB free (AIBOX_MIN_FREE_DISK_GB)"
                           % (need / 1e9, free / 1e9, FLOOR / 1e9))
    for f in files:
        name = os.path.basename(f["path"])
        url = f"{HF}/{repo}/resolve/main/{urllib.parse.quote(f['path'])}"
        hdr = ["Authorization: Bearer " + hf_token()] if hf_token() else []
        if shutil.which("aria2c"):
            cmd = ["aria2c", "-x", "8", "-s", "8", "-k", "8M", "--file-allocation=none", "-c", "--console-log-level=error",
                   "--summary-interval=0", "-d", str(part_dir), "-o", name] + [f"--header={h}" for h in hdr] + [url]
        else:
            cmd = ["curl", "-L", "-C", "-", "--fail", "-s", "-o", str(part_dir / name)] + [x for h in hdr for x in ("-H", h)] + [url]
        job["state"] = "downloading"
        job.setdefault("t0", time.time())
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        job["proc"] = proc
        while proc.poll() is None:
            job["done"] = sum(p.stat().st_size for p in part_dir.glob("*.gguf"))
            if job.get("cancel"):
                proc.kill()
                raise RuntimeError("cancelled")
            time.sleep(1)
        if proc.returncode != 0:
            raise RuntimeError(f"download failed ({name}): " + (proc.stderr.read() or "")[-200:])
    job["state"], job["done"] = "verifying", job["total"]
    for f in files:
        if not f.get("oid"):
            continue
        h, p = hashlib.sha256(), part_dir / os.path.basename(f["path"])
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(8 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != f["oid"]:
            p.unlink()
            raise RuntimeError(f"checksum mismatch on {p.name}, file removed")
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        os.replace(part_dir / os.path.basename(f["path"]), dest_dir / os.path.basename(f["path"]))
    shutil.rmtree(part_dir, ignore_errors=True)
    if base is not None:
        extdrives.register_root(str(base))
    run_gen()
    job["msg"] = "installed; it joins the model list within about 30 seconds"
    job["state"] = "done"


def worker():
    while True:
        with LOCK:
            job = next((j for j in JOBS.values() if j["state"] == "queued"), None)
        if not job:
            time.sleep(1)
            continue
        try:
            download_job(job)
        except Exception as exc:  # noqa: BLE001
            job["state"], job["msg"] = ("cancelled" if str(exc) == "cancelled" else "failed"), str(exc)
        job.pop("proc", None)
        job["finished"] = int(time.time())
        hist_add(public(job))


def public(j):
    o = {k: v for k, v in j.items() if k not in ("proc", "files", "cancel", "t0")}
    if j.get("state") == "downloading" and j.get("t0"):
        sp = (j.get("done") or 0) / max(1.0, time.time() - j["t0"])
        o["speed"] = int(sp)
        o["eta"] = int((j.get("total", 0) - (j.get("done") or 0)) / sp) if sp > 0 else None
    return o


HIST_FILE = STATE / "history.json"


def hist_load():
    try:
        return json.loads(HIST_FILE.read_text())
    except Exception:
        return []


def hist_add(rec):
    h = hist_load()
    h.append(rec)
    HIST_FILE.write_text(json.dumps(h[-200:]))


def installed():
    out = []
    for dest, where, base in [("", "internal", INBOX)] + ext_bases():
        try:
            items = sorted(base.iterdir()) if base.is_dir() else []
        except OSError:
            continue
        for p in items:
            if p.name.startswith("."):
                continue
            try:
                files = [x for x in ([p] if p.is_file() else p.rglob("*")) if x.is_file() and x.suffix.lower() == ".gguf"]
                if files:
                    out.append({"name": p.name, "size": sum(x.stat().st_size for x in files), "files": [x.name for x in files],
                                "modified": int(max(x.stat().st_mtime for x in files)), "dest": dest, "where": where,
                                "linked": any(x.is_symlink() for x in ([p] if p.is_file() else p.rglob("*.gguf")))})
            except OSError:
                pass
    return out


# ---------------------------------------------------------------- downloads started outside hf-fetch
EXT, EXT_LOCK, REMOTE_SIZE = {}, threading.Lock(), {}
EXT_EXES = ("aria2c", "curl", "wget", "hf", "huggingface-cli")
HF_URL_RE = re.compile(r"https?://huggingface\.co/([^/\s]+)/([^/\s]+)/resolve/[^/\s]+/(\S+)")


def aria_progress(ctl):
    """(done, total) bytes from an aria2 .aria2 control file, or None."""
    try:
        b = open(ctl, "rb").read()
        if b[:2] != b"\x00\x01":
            return None
        i = 6
        i += 4 + int.from_bytes(b[i:i + 4], "big")
        piece = int.from_bytes(b[i:i + 4], "big"); i += 4
        total = int.from_bytes(b[i:i + 8], "big"); i += 16
        bl = int.from_bytes(b[i:i + 4], "big"); i += 4
        bits = bin(int.from_bytes(b[i:i + bl], "big")).count("1")
        return min(total, bits * piece), total
    except Exception:
        return None


def scan_external():
    out, boot, clk = {}, 0, os.sysconf("SC_CLK_TCK")
    for ln in open("/proc/stat"):
        if ln.startswith("btime"):
            boot = int(ln.split()[1])
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            args = [a.decode(errors="replace") for a in open(f"/proc/{d}/cmdline", "rb").read().split(b"\0") if a]
            if not args or os.path.basename(args[0]) not in EXT_EXES:
                continue
            url = next((a for a in args if HF_URL_RE.match(a)), None)
            if not url or any(str(PART) in a for a in args):
                continue
            m = HF_URL_RE.match(url)
            name = dd = None
            for k, a in enumerate(args):
                nxt = args[k + 1] if k + 1 < len(args) else None
                if a in ("-o", "--out", "--output", "-O") and nxt:
                    name = nxt
                elif a.startswith("--out="):
                    name = a[6:]
                elif a in ("-d", "--dir") and nxt:
                    dd = nxt
                elif a.startswith("--dir="):
                    dd = a[6:]
            name = name or os.path.basename(urllib.parse.unquote(m.group(3).split("?")[0]))
            path = name if os.path.isabs(name) else os.path.join(dd or os.readlink(f"/proc/{d}/cwd"), name)
            st = open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()
            out[int(d)] = {"pid": int(d), "repo": m.group(1) + "/" + m.group(2), "name": os.path.basename(name), "path": path,
                           "started": int(boot + int(st[19]) / clk), "via": os.path.basename(args[0])}
        except Exception:
            continue
    return out


def remote_size(repo, name):
    k = (repo, name)
    if k not in REMOTE_SIZE:
        REMOTE_SIZE[k] = 0
        try:
            for e in tree(repo):
                if os.path.basename(e.get("path", "")) == name:
                    REMOTE_SIZE[k] = (e.get("lfs") or {}).get("size") or e.get("size") or 0
        except Exception:
            pass
    return REMOTE_SIZE[k]


def ext_tick():
    now, cur = time.time(), scan_external()
    with EXT_LOCK:
        for pid in [x for x in EXT if x not in cur]:
            r = EXT.pop(pid)
            have = os.path.getsize(r["path"]) if os.path.exists(r["path"]) else 0
            complete = os.path.exists(r["path"]) and not os.path.exists(r["path"] + ".aria2") and (not r["total"] or have >= r["total"])
            hist_add({"id": "ext%d" % pid, "repo": r["repo"], "name": r["name"], "state": "done" if complete else "stopped",
                      "msg": "started outside hf-fetch (%s); saved to %s" % (r["via"], r["path"]), "done": have if complete else r["done"],
                      "total": r["total"] or have, "started": r["started"], "finished": int(now)})
    for pid, rec in cur.items():
        r = EXT.get(pid)
        if r is None:
            r = EXT[pid] = {**rec, "done": 0, "total": 0, "speed": 0, "eta": None, "samples": []}
        try:
            w = int([ln for ln in open(f"/proc/{pid}/io") if ln.startswith("wchar")][0].split()[1])
        except Exception:
            w = None
        pr = aria_progress(r["path"] + ".aria2")
        if pr:
            if pr[0] != r.get("ctl") or r.get("w_at") is None:
                r["ctl"], r["w_at"] = pr[0], w
            extra = max(0, w - r["w_at"]) if (w is not None and r["w_at"] is not None) else 0
            r["done"], r["total"] = min(pr[1], pr[0] + extra), pr[1]
        else:
            r["done"] = os.path.getsize(r["path"]) if os.path.exists(r["path"]) else 0
        if not r["total"]:
            r["total"] = remote_size(r["repo"], r["name"])
        r["samples"] = (r["samples"] + [(now, w if w is not None else r["done"])])[-8:]
        (t0, d0), (t1, d1) = r["samples"][0], r["samples"][-1]
        r["speed"] = int(max(0, d1 - d0) / (t1 - t0)) if t1 - t0 >= 2 else 0
        r["eta"] = int((r["total"] - r["done"]) / r["speed"]) if r["speed"] and r["total"] > r["done"] else None


def ext_watch():
    while True:
        try:
            ext_tick()
        except Exception:
            pass
        time.sleep(3)


def ext_list():
    with EXT_LOCK:
        return [{k: v for k, v in r.items() if k != "samples"} for r in EXT.values()]


def partials():
    busy = {j["repo"] for j in JOBS.values() if j["state"] in ("queued", "downloading", "verifying")}
    out = []
    for dest, where, base in [("", "internal", INBOX)] + ext_bases():
        pd = PART if not dest else base / ".partial"
        try:
            dirs = sorted(pd.iterdir()) if pd.is_dir() else []
        except OSError:
            continue
        for d in dirs:
            if not d.is_dir() or d.name.replace("__", "/", 1) in busy:
                continue
            for f in sorted(d.glob("*.gguf")):
                m = SHARD_RE.match(f.name)
                st = f.stat()
                out.append({"repo": d.name.replace("__", "/", 1), "name": m.group(1) if m else f.name[:-5], "file": f.name,
                            "size": st.st_size, "modified": int(st.st_mtime), "dest": dest, "where": where})
    return out


def api(method, path, q, body):
    if path == "/api/search":
        return search(q.get("q", [""])[0], q.get("sort", ["downloads"])[0])
    if path == "/api/files":
        repo = q.get("repo", [""])[0]
        if not REPO_RE.match(repo):
            raise ValueError("bad repo")
        rows, proj = group_files(tree(repo))
        return {"models": rows, "mmproj": proj}
    if path == "/api/download" and method == "POST":
        repo, name = body.get("repo", ""), body.get("name", "")
        if not REPO_RE.match(repo):
            raise ValueError("bad repo")
        rows, proj = group_files(tree(repo))
        row = next((r for r in rows if r["name"] == name), None)
        if not row:
            raise ValueError("no such file group")
        files = list(row["files"]) + (proj[:1] if body.get("mmproj") and proj else [])
        dest = (body.get("dest") or "").strip()
        where = "internal"
        if dest and dest != "internal":
            ext_base(dest)
            where = (extdrives.find(dest) or {}).get("label") or dest
        else:
            dest = ""
        jid = secrets.token_hex(4)
        JOBS[jid] = {"id": jid, "repo": repo, "name": name, "state": "queued", "done": 0, "msg": "", "files": files,
                     "total": sum(f["size"] for f in files), "started": int(time.time()), "dest": dest, "where": where}
        return {"id": jid}
    if path == "/api/jobs":
        return [public(j) for j in sorted(JOBS.values(), key=lambda j: -j["started"])][:12]
    if path == "/api/cancel" and method == "POST":
        if body.get("id") in JOBS:
            JOBS[body["id"]]["cancel"] = True
        return {"ok": True}
    if path == "/api/installed":
        return installed()
    if path == "/api/repo":
        repo = q.get("repo", [""])[0]
        if not REPO_RE.match(repo):
            raise ValueError("bad repo")
        with hf_open(f"{HF}/api/models/{repo}") as r:
            m = json.load(r)
        return {"id": m.get("id", repo), "downloads": m.get("downloads", 0), "likes": m.get("likes", 0),
                "modified_iso": m.get("lastModified") or "", "created_iso": m.get("createdAt") or "", "gated": bool(m.get("gated"))}
    if path == "/api/history":
        return list(reversed(hist_load()))
    if path == "/api/status":
        du = shutil.disk_usage(INBOX)
        return {"free": du.free, "total": du.total, "max_gb": MAX_GB, "inbox": str(INBOX)}
    if path == "/api/delete" and method == "POST":
        name = body.get("name", "")
        base = ext_base(body.get("dest")) or INBOX
        target = base / name
        if not name or "/" in name or name.startswith(".") or not target.exists() or target.resolve().parent != base.resolve():
            raise ValueError("bad name")
        if base != INBOX:
            extdrives.ensure_rw(body["dest"])
        shutil.rmtree(target) if target.is_dir() else target.unlink()
        return {"result": run_gen()}
    if path == "/api/external":
        return ext_list()
    if path == "/api/partials":
        return partials()
    if path == "/api/discard" and method == "POST":
        repo, fn = body.get("repo", ""), body.get("file", "")
        _b = ext_base(body.get("dest"))
        target = (_b / ".partial" if _b is not None else PART) / repo.replace("/", "__") / fn
        if not REPO_RE.match(repo) or "/" in fn or not fn.endswith(".gguf") or not target.is_file():
            raise ValueError("bad partial")
        target.unlink()
        try:
            target.parent.rmdir()
        except OSError:
            pass
        return {"ok": True}
    raise KeyError(path)


PAGE = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Model fetch</title>
<style>body{font:14px system-ui;margin:0;background:#111;color:#ddd}header{padding:12px 16px;background:#1b1b1b;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input,select,button{font:inherit;padding:6px 10px;background:#222;color:#ddd;border:1px solid #444;border-radius:4px}button{cursor:pointer}button:hover{background:#2c2c2c}
main{display:grid;grid-template-columns:1fr 380px;gap:16px;padding:16px}.card{background:#1a1a1a;border:1px solid #333;border-radius:6px;padding:10px;margin-bottom:8px}
.muted{color:#888}.bar{height:6px;background:#333;border-radius:3px;margin-top:4px}.bar i{display:block;height:6px;background:#4a9;border-radius:3px}
table{width:100%;border-collapse:collapse;margin-top:6px}td,th{padding:3px 6px;text-align:left}.no{color:#c66}.ok{color:#6c6}a{color:#8ab4f8}h3{margin:6px 0}
@media(max-width:900px){main{grid-template-columns:1fr}}</style>
<header><b>Model fetch</b> <input id=q size=36 placeholder="search Hugging Face GGUF models (e.g. qwen 27b coder)" onkeydown="if(event.key==='Enter')go()">
<select id=sort><option value=downloads>most downloaded</option><option value=trendingScore>trending</option><option value=likes>most liked</option><option value=lastModified>recently updated</option></select>
<button onclick=go()>Search</button><span class=muted id=st></span></header>
<main><section id=res><p class=muted>Search above. Fits = under __MAXGB__ GB, which loads on this box with room for context.</p></section>
<aside><h3>Downloads</h3><div id=jobs class=muted>none yet</div><h3>Installed (inbox)</h3><div id=inst></div></aside></main>
<script>
const key=new URLSearchParams(location.search).get('key')||'';
const call=(p,o)=>fetch(p+(p.includes('?')?'&':'?')+'key='+encodeURIComponent(key),o).then(async r=>{const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j});
const post=(p,b)=>call(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
const gb=b=>(b/1e9).toFixed(1)+' GB';
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const $=id=>document.getElementById(id);
async function go(){const q=$('q').value.trim();$('st').textContent='searching...';
 try{const r=await call('/api/search?q='+encodeURIComponent(q)+'&sort='+$('sort').value);$('st').textContent=r.length+' results';
 $('res').innerHTML=r.map((m,i)=>`<div class=card><b><a href="https://huggingface.co/${esc(m.id)}" target=_blank>${esc(m.id)}</a></b> ${m.gated?'<span class=no>gated</span>':''}
 <span class=muted> &#8595;${m.downloads.toLocaleString()} &#9829;${m.likes} updated ${esc(m.modified)}</span> <button onclick="files('${esc(m.id)}',${i})">files</button><div id=f${i}></div></div>`).join('')||'<p class=muted>nothing found</p>'}
 catch(e){$('st').textContent='error: '+e.message}}
async function files(repo,i){const d=$('f'+i);d.innerHTML='<span class=muted>loading...</span>';
 try{const r=await call('/api/files?repo='+encodeURIComponent(repo));
 if(!r.models.length){d.innerHTML='<span class=no>no GGUF files in this repo</span>';return}
 d.innerHTML='<table>'+r.models.map((m,j)=>`<tr><td><input type=radio name=r${i} value="${esc(m.name)}" ${j==0?'checked':''}></td><td>${esc(m.name)}</td><td>${esc(m.quant)}</td><td>${gb(m.size)}${m.shards>1?' ('+m.shards+' parts)':''}</td><td class="${m.fits?'ok':'no'}">${m.fits?'fits':'too big'}</td><td><button onclick="dl1('${esc(repo)}','${esc(m.name)}',${i})">Download</button></td></tr>`).join('')+'</table>'
 +(r.mmproj.length?`<label><input type=checkbox id=mm${i}> include vision projector (${gb(r.mmproj[0].size)})</label> `:'')+`<button onclick="dl('${esc(repo)}',${i})">Download selected</button>`}
 catch(e){d.innerHTML='<span class=no>'+esc(e.message)+'</span>'}}
async function dl1(repo,name,i){try{await post('/api/download',{repo,name,mmproj:!!($('mm'+i)&&$('mm'+i).checked)});refresh()}catch(e){alert(e.message)}}
async function dl(repo,i){const sel=document.querySelector('input[name=r'+i+']:checked');if(!sel)return;
 try{await post('/api/download',{repo,name:sel.value,mmproj:!!($('mm'+i)&&$('mm'+i).checked)});refresh()}catch(e){alert(e.message)}}
async function refresh(){try{const j=await call('/api/jobs');$('jobs').innerHTML=j.map(x=>`<div class=card>${esc(x.repo)}<br><span class=muted>${esc(x.name)}</span><br>${esc(x.state)} ${esc(x.msg||'')}
 <div class=bar><i style="width:${x.total?Math.min(100,100*x.done/x.total):0}%"></i></div><span class=muted>${gb(x.done)} / ${gb(x.total)}</span>
 ${['queued','downloading'].includes(x.state)?` <button onclick="post('/api/cancel',{id:'${x.id}'})">cancel</button>`:''}</div>`).join('')||'none yet';
 const n=await call('/api/installed');$('inst').innerHTML=n.map(x=>`<div class=card>${esc(x.name)} <span class=muted>${gb(x.size)}</span> <button onclick="rm('${esc(x.name)}')">remove</button></div>`).join('')||'<span class=muted>empty. Files dropped into __INBOX__ appear here too.</span>'}catch(e){}}
async function rm(n){if(confirm('Delete '+n+' from the box?')){await post('/api/delete',{name:n});refresh()}}
refresh();setInterval(refresh,2000);
</script>"""


class H(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self, method):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if not AC.allowed(self, q):
            if u.path == "/":
                return self.reply(401, AC.login_page("Model fetch"), "text/html; charset=utf-8")
            return self.reply(401, {"error": "bad or missing key"})
        if u.path == "/" and "key" in q:
            self.send_response(302)
            self.send_header("Set-Cookie", AC.cookie_header())
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if u.path == "/":
            return self.reply(200, PAGE.replace("__MAXGB__", "%g" % MAX_GB).replace("__INBOX__", str(INBOX)), "text/html; charset=utf-8")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}") if method == "POST" else {}
            self.reply(200, api(method, u.path, q, body))
        except KeyError:
            self.reply(404, {"error": "not found"})
        except urllib.error.HTTPError as exc:
            self.reply(502, {"error": f"Hugging Face said {exc.code}" + (" (gated: put a Hugging Face token in %s)" % AC.get("AIBOX_HF_TOKEN_FILE") if exc.code in (401, 403) else "")})
        except Exception as exc:  # noqa: BLE001
            self.reply(400, {"error": str(exc)})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=ext_watch, daemon=True).start()
    print(f"hf-fetch on :{PORT}, inbox {INBOX}", flush=True)
    ThreadingHTTPServer((AC.get("AIBOX_BIND"), PORT), H).serve_forever()
