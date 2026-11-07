"""webtools - internet access for local models: web_search and web_fetch.

Used by modelbench (imported) and by aissh6-harness.ps1 (CLI: python webtools.py --b64 <base64 json>).
"""
import base64, gzip, html, json, re, sys, urllib.parse, urllib.request, zlib

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
MAX_BYTES = 3 * 1024 * 1024


def _get(url, data=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9", "Accept-Encoding": "gzip, deflate"})
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(MAX_BYTES + 1)
        enc = (r.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        ctype = r.headers.get("Content-Type") or ""
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        return r.geturl(), ctype, raw[:MAX_BYTES], (m.group(1) if m else None), len(raw) > MAX_BYTES


def _txt(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def web_search(query, max_results=8):
    """Web search through the ddgs package (browser-like requests, several engines behind it).
    Plain scraping of DuckDuckGo/Bing is not used: DDG serves a bot challenge and Bing quietly returns unrelated results."""
    q = (query or "").strip()
    if not q:
        return "error: empty query"
    n = max(1, min(15, int(max_results or 8)))
    try:
        from ddgs import DDGS
    except ImportError:
        return "error: web search needs the ddgs package (python -m pip install ddgs)"
    try:
        res = DDGS(timeout=20).text(q, max_results=n, region="uk-en") or []
    except Exception as ex:
        return "error: web search failed for %r: %s" % (q, str(ex)[:300])
    if not res:
        return "no results for %r" % q
    lines = ["Web results for %r:" % q]
    for i, r in enumerate(res[:n], 1):
        lines.append("%d. %s\n   %s\n   %s" % (i, (r.get("title") or "(no title)").strip(), r.get("href") or r.get("url") or "",
                                             re.sub(r"\s+", " ", r.get("body") or "").strip()[:300]))
    lines.append("Use web_fetch with a URL to read a page.")
    return "\n".join(lines)


_BLOCKS = r"script|style|noscript|svg|template|iframe|head|nav|footer|header|form|aside|button|select"


def html_to_text(page, base_url):
    title = _txt((re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I) or [None, ""])[1])
    body = re.sub(r"<!--.*?-->", " ", page, flags=re.S)
    body = re.sub(r"<(%s)\b[^>]*>.*?</\1\s*>" % _BLOCKS, " ", body, flags=re.S | re.I)
    links, seen = [], set()
    for href, label in re.findall(r'<a\b[^>]*href="([^"#][^"]*)"[^>]*>(.*?)</a>', body, re.S | re.I):
        u = urllib.parse.urljoin(base_url, html.unescape(href))
        t = _txt(label)
        if u.startswith("http") and t and u not in seen and len(links) < 25:
            seen.add(u)
            links.append("- %s: %s" % (t[:80], u))
    body = re.sub(r"<(br|/p|/div|/li|/tr|/h[1-6]|/section|/article|/blockquote|/pre)\b[^>]*>", "\n", body, flags=re.I)
    body = re.sub(r"<li\b[^>]*>", "\n- ", body, flags=re.I)
    body = re.sub(r"<h([1-6])\b[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", body, flags=re.I)
    body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    lines = [re.sub(r"[ \t\u00a0]+", " ", l).strip() for l in body.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return title, text, links


def web_fetch(url, max_chars=6000):
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", url, re.I):
            url = "https://" + url
        else:
            return "error: web_fetch needs an http(s) URL, got %r" % url[:200]
    cap = max(500, min(20000, int(max_chars or 6000)))
    try:
        final, ctype, raw, cs, cut = _get(url)
    except urllib.error.HTTPError as ex:
        return "error: HTTP %s from %s" % (ex.code, url)
    except Exception as ex:
        return "error fetching %s: %s" % (url, ex)
    ct = ctype.split(";")[0].strip().lower()
    head = "URL: %s\nType: %s%s\n" % (final, ct or "?", " (first 3 MB)" if cut else "")
    if "html" in ct or (not ct and raw[:200].lstrip().lower().startswith((b"<!doctype", b"<html"))):
        title, text, links = html_to_text(raw.decode(cs or "utf-8", "replace"), final)
        room = cap - len(head) - 40
        link_txt = ("\n\nLinks:\n" + "\n".join(links)) if links else ""
        link_txt = link_txt[:max(0, min(len(link_txt), room // 4))]
        body = text[:room - len(link_txt)]
        more = "\n[... %d more characters not shown]" % (len(text) - len(body)) if len(text) > len(body) else ""
        return ("Title: %s\n" % title if title else "") + head + "\n" + body + more + link_txt
    if ct.startswith("text/") or "json" in ct or "xml" in ct or "javascript" in ct:
        t = raw.decode(cs or "utf-8", "replace")
        return head + "\n" + t[:cap - len(head)] + ("\n[... truncated]" if len(t) > cap - len(head) else "")
    return head + "\nBinary content (%d bytes) - not shown as text." % len(raw)


def run(tool, args):
    tool = (tool or "").replace("aissh6_", "").strip()
    if tool == "web_search":
        return web_search(args.get("query") or args.get("q"), args.get("max_results"))
    if tool == "web_fetch":
        return web_fetch(args.get("url"), args.get("max_chars"))
    return "error: unknown web tool %r" % tool


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) >= 3 and sys.argv[1] == "--b64":
        j = json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))
        print(run(j.get("tool"), j.get("args") or {}))
    elif len(sys.argv) >= 3:
        print(run(sys.argv[1], {"query": " ".join(sys.argv[2:]), "url": sys.argv[2]}))
    else:
        print("usage: webtools.py web_search <query> | web_fetch <url> | --b64 <base64 json>")
