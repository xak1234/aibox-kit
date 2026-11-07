"""Small adapter between the legacy bench and the independently testable coding harness."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote, urlparse

from coding_harness import CodingHarness
from harness_core import HarnessError, Json

_lock = threading.Lock()
_instance: CodingHarness | None = None
_cache: tuple[float, list[Json]] = (0.0, [])


def chat_capable(server: Json) -> bool:
    """Reject non-generative GGUF architectures and explicit embedding/reranking processes."""
    name = str(server.get("model") or server.get("name") or "").lower()
    if any(word in name for word in ("bge-", "embedding", "embed-", "rerank", "e5-")):
        return False
    try:
        args = Path(f"/proc/{int(server['pid'])}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError, KeyError):
        args = []
    return not any(a in {b"--embedding", b"--embeddings", b"--reranking", b"--rerank"} for a in args)


def models(bench: ModuleType) -> list[Json]:
    global _cache
    now = time.monotonic()
    if now - _cache[0] < 3:
        return _cache[1]
    result: list[Json] = []
    candidates = [s for s in bench.SNAP["servers"] if s.get("kind") == "server" and chat_capable(s)]
    for server in candidates:
        port = server.get("port")
        if not port or server.get("health") != "ok":
            continue
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/props", timeout=3) as response:
                props = json.load(response)
            caps = props.get("chat_template_caps") or {}
            result.append({"port": int(port), "name": props.get("model_alias") or server["model"],
                           "pid": server["pid"], "tools": bool(caps.get("supports_tools") and caps.get("supports_tool_calls")),
                           "ctx": int((props.get("default_generation_settings") or {}).get("n_ctx") or 0)})
        except (OSError, ValueError, KeyError):
            continue
    _cache = (now, result)
    return result


def instance(bench: ModuleType) -> CodingHarness:
    global _instance
    with _lock:
        if _instance is None:
            _instance = CodingHarness(Path(bench.HERE), lambda: models(bench))
        return _instance


def handle_request(handler: Any, bench: ModuleType, method: str, path: str,
                   query: Json, raw: bytes = b"") -> bool:
    if path not in {"/play", "/coding"} and not path.startswith(("/coding-assets/", "/api/harness/")):
        return False
    if method == "GET" and path in {"/play", "/coding"}:
        page = (Path(bench.HERE) / "coding_harness.html").read_text()
        key = str((query.get("key") or [""])[0])
        if key:
            for asset in ("coding_harness.css", "coding_harness.js"):
                page = page.replace('/coding-assets/' + asset + '"', '/coding-assets/' + asset + '?key=' + quote(key, safe="") + '"')
        handler._send(200, page, "text/html; charset=utf-8")
        return True
    if method == "GET" and path.startswith("/coding-assets/"):
        filename = path.removeprefix("/coding-assets/")
        types = {"coding_harness.js": "text/javascript", "coding_harness.css": "text/css"}
        if filename not in types:
            handler._send(404, {"error": "Asset not found."})
        else:
            handler._send(200, (Path(bench.HERE) / filename).read_text(), types[filename] + "; charset=utf-8")
        return True
    if method == "POST":
        origin = handler.headers.get("Origin")
        host = handler.headers.get("Host")
        if origin and urlparse(origin).netloc != host or handler.headers.get("Sec-Fetch-Site") == "cross-site":
            handler._send(403, {"error": "Cross-origin coding requests are not allowed."})
            return True
        if len(raw) > 3 * 1024 * 1024:
            handler._send(413, {"error": "Request exceeds 3 MiB."})
            return True
    try:
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise HarnessError("Request body must be an object.")
        handler._send(200, instance(bench).handle(method, path, query, body))
    except (HarnessError, ValueError, OSError, TypeError, KeyError) as exc:
        handler._send(400, {"error": str(exc)[:2000]})
    return True
