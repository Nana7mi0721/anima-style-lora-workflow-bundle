"""HTTP helpers: proxy probing, sessions, booru credentials, retries.

The machine's shell proxy is not reliable: some shells export a dead
HTTPS_PROXY, and the local clash port is not always listening.  So we probe
candidate network paths once per process ("direct first") and remember the
winner.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

try:
    import requests
except Exception as exc:  # pragma: no cover
    raise SystemExit("[animasl] the 'requests' package is required for network stages") from exc

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_PROBE_CACHE: dict[str, Any] = {"proxy": "__unset__"}


def _candidates(cfg) -> list[str]:
    cands = list(cfg.get("proxy_candidates") or [""])
    env_proxy = os.environ.get("ANIMASL_PROXY")
    if env_proxy:
        cands = [env_proxy] + [c for c in cands if c != env_proxy]
    return [c for c in cands if c is not None]


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _needs_proxy(cfg, host: str) -> bool:
    """True when this host is known to reset direct connections (pawchive CDN)."""
    if os.environ.get("ANIMASL_FORCE_PROXY"):
        return True
    for pattern in (cfg.get("prefer_proxy_hosts") or []):
        pattern = str(pattern).lower()
        if host == pattern or host.endswith("." + pattern) or pattern in host:
            return True
    return False


def probe(cfg, probe_url: str, params: dict | None = None, timeout: int = 15) -> str:
    """Return the first proxy string that can reach `probe_url` ('' = direct).

    The winner is cached per host: pawchive's CDN resets direct connections
    (ConnectionResetError 10054) while yande.re is faster without a proxy, so a
    single global answer would be wrong for one of them.
    """
    host = _host(probe_url)
    cands = _candidates(cfg)
    if _needs_proxy(cfg, host):
        cands = [c for c in cands if c] + [""]      # proxies first, direct as last resort
    cache_key = f"proxy:{host}"
    if cache_key in _PROBE_CACHE:
        return _PROBE_CACHE[cache_key]
    winner = ""
    for cand in cands:
        proxies = {"http": cand, "https": cand} if cand else None
        s = requests.Session()
        s.trust_env = False
        try:
            r = s.get(probe_url, params=params or {}, headers={"User-Agent": UA},
                      timeout=timeout, proxies=proxies)
            if r.status_code < 500:
                winner = cand
                break
        except Exception:
            continue
    _PROBE_CACHE[cache_key] = winner
    _PROBE_CACHE["proxy"] = winner
    return winner


def session(cfg, probe_url: str = "https://yande.re/post.json",
            probe_params: dict | None = None, referer: str = "") -> requests.Session:
    proxy = probe(cfg, probe_url, probe_params)
    s = requests.Session()
    s.trust_env = False
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    s.headers.update(headers)
    s._animasl_proxy = proxy or ""          # curl 兜底要用同一个出口
    s._animasl_curl_ua = str(cfg.get("curl_user_agent") or "")
    return s


def proxy_label() -> str:
    p = _PROBE_CACHE.get("proxy")
    if p == "__unset__":
        return "(未探测)"
    return p or "直连"


# --------------------------------------------------------------------------
# curl 兜底：Cloudflare 按 TLS/HTTP 指纹挑战 requests —— 实测 danbooru 的
# posts.json 与 cdn.donmai.us 都回 403 "Just a moment..."，而同一个代理下
# curl 正常 200。所以识别到挑战就改用 curl 子进程重试，不引入新依赖。
# 想关掉：设 ANIMASL_NO_CURL=1；想换二进制：设 ANIMASL_CURL=<路径>。
# --------------------------------------------------------------------------
CURL_BIN = os.environ.get("ANIMASL_CURL", "curl")
# 关键：**不要**把浏览器 UA 转给 curl。danbooru 的 Cloudflare 规则是
# "Chrome UA + 非浏览器 TLS 指纹 = 机器人" —— 实测同一代理下，带 Chrome UA 的
# curl 一样吃 403 "Just a moment..."，而 curl 默认 UA / 自报家门 UA 都是 200。
# 想换：设 ANIMASL_CURL_UA，或配置项 curl_user_agent。
CURL_UA = os.environ.get("ANIMASL_CURL_UA", "animasl/0.1 (+curl)")
_CHALLENGE_MARKERS = ("just a moment", "cf-chl", "cf_chl", "attention required",
                      "enable javascript and cookies", "checking your browser")
_curl_ok: bool | None = None


def looks_like_challenge(status: int, body: Any) -> bool:
    """403/503 且回的是 Cloudflare 拦截页（而不是真的没权限）。"""
    if status not in (403, 503):
        return False
    text = body if isinstance(body, str) else (body or b"").decode("utf-8", "replace")
    head = text[:4000].lower()
    if any(m in head for m in _CHALLENGE_MARKERS):
        return True
    return head.lstrip().startswith("<!doctype html") or head.lstrip().startswith("<html")


def curl_available() -> bool:
    global _curl_ok
    if _curl_ok is None:
        if os.environ.get("ANIMASL_NO_CURL"):
            _curl_ok = False
        else:
            try:
                subprocess.run([CURL_BIN, "--version"], capture_output=True,
                               timeout=20, check=True)
                _curl_ok = True
            except Exception:
                _curl_ok = False
    return _curl_ok


def _curl_args(s: Any, timeout: int, url: str = "") -> list[str]:
    args = [CURL_BIN, "-sS", "-L", "--max-time", str(int(timeout)), "--retry", "2"]
    proxy = getattr(s, "_animasl_proxy", "") or ""
    if proxy:
        args += ["-x", proxy]
    auth = getattr(s, "auth", None)
    if auth and len(auth) == 2 and auth[0]:
        args += ["-u", f"{auth[0]}:{auth[1]}"]
    headers = getattr(s, "headers", None) or {}
    ua = getattr(s, "_animasl_curl_ua", "") or CURL_UA
    args += ["-H", f"User-Agent: {ua}"]
    for key in ("Referer", "Accept"):
        val = headers.get(key) if hasattr(headers, "get") else None
        if val:
            args += ["-H", f"{key}: {val}"]
    # 只转发与该主机匹配的 cookie，避免把一个站的登录态发给另一个站
    jar = getattr(s, "cookies", None)
    host = (urlparse(url).hostname or "") if url else ""
    if jar is not None and len(jar) > 0 and host:
        pairs = [f"{c.name}={c.value}" for c in jar
                 if not c.domain or host == (c.domain or "").lstrip(".")
                 or host.endswith("." + (c.domain or "").lstrip("."))]
        if pairs:
            args += ["-b", "; ".join(pairs)]
    return args


def curl_get_json(s: Any, url: str, params: dict | None = None, timeout: int = 60) -> Any:
    full = url + ("?" + urlencode(params) if params else "")
    args = _curl_args(s, timeout, full) + ["-H", "Accept: application/json", full]
    r = subprocess.run(args, capture_output=True, timeout=timeout + 60)
    if r.returncode != 0:
        raise RuntimeError(f"curl exit {r.returncode}: {r.stderr[:200]!r}")
    return json.loads(r.stdout.decode("utf-8", "replace"))


def curl_download(s: Any, url: str, dest: Path, timeout: int = 300) -> tuple[bool, int]:
    """curl 下载到 dest（失败即删掉半成品）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = _curl_args(s, timeout, url) + ["-o", str(dest), "-w", "%{http_code}", url]
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout + 60)
    except Exception:
        dest.unlink(missing_ok=True)
        return False, 0
    code = (r.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    code = code[-1] if code else ""
    if r.returncode == 0 and code.startswith("2") and dest.exists() and dest.stat().st_size > 0:
        return True, dest.stat().st_size
    dest.unlink(missing_ok=True)
    return False, 0


def get_json(s: requests.Session, url: str, params: dict | None = None,
             retries: int = 4, timeout: int = 60, sleep: float = 1.5) -> Any:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = s.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "5") or 5)
                time.sleep(wait)
                continue
            if looks_like_challenge(r.status_code, r.text) and curl_available():
                return curl_get_json(s, url, params, timeout)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(sleep * attempt)
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last}")



def danbooru_session(cfg) -> requests.Session:
    s = session(cfg, "https://danbooru.donmai.us/posts.json",
                {"tags": "rating:general", "limit": 1}, referer="https://danbooru.donmai.us/")
    login, key = danbooru_auth(cfg)
    if login and key:
        s.auth = (login, key)
    return s


def danbooru_auth(cfg) -> tuple[str, str]:
    """danbooru 凭据：配置层（设置页写的 runtime 配置）优先，其次环境变量。

    设置页把值写进 <home>/.animasl/animasl.config.json，插件 spawn 的子进程
    自然读得到；环境变量保留给"不想落盘"的用法。
    """
    login = str(cfg.get("danbooru_login") or "").strip() or os.environ.get("DANBOORU_LOGIN", "")
    key = str(cfg.get("danbooru_api_key") or "").strip() or os.environ.get("DANBOORU_API_KEY", "")
    return login.strip(), key.strip()


def load_cookies(path: str | Path) -> dict[str, str]:
    """Read a Netscape cookies.txt into a {name: value} dict."""
    jar: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"[animasl] cookies file not found: {p}")
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            jar[parts[5]] = parts[6]
    return jar


def load_cookie_jar(path: str | Path) -> "requests.cookies.RequestsCookieJar":
    """读 Netscape cookies.txt，**保留 domain 列**。

    与 load_cookies() 的区别：那份返回 {name: value} 裸字典，requests 会把它当
    "域为空"的 cookie —— 空域等于**任何主机都收到**，于是 exhentai 的登录 cookie
    会被发到 pawchive.pw（反之亦然）。一个文件里放多个站点的 cookie 时必须用这个。
    """
    jar: requests.cookies.RequestsCookieJar = requests.cookies.RequestsCookieJar()
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"[animasl] cookies file not found: {p}")
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, flag, cpath, _secure, _expires, name, value = parts[:7]
        domain = domain.strip()
        # Netscape 第 2 列 TRUE = 域级 cookie（补前导点，子域也发）；
        # FALSE = Host-Only（保持裸域）。IP 不加点。
        if (flag or "").strip().upper() == "TRUE" and domain and not domain.startswith(".") \
                and not domain.replace(".", "").isdigit():
            domain = "." + domain
        jar.set(name, value, domain=domain or None, path=cpath.strip() or "/")
    return jar


def download(s: requests.Session, url: str, dest: Path, retries: int = 4,
             sleep: float = 0.6, chunk: int = 1 << 20, expected: int | None = None) -> tuple[bool, int]:
    """Stream one file to `dest`; resumable-ish (skips a complete existing file)."""
    if dest.exists() and dest.stat().st_size > 0:
        size = dest.stat().st_size
        if expected is None or size == expected:
            return True, size
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, retries + 1):
        try:
            with s.get(url, stream=True, timeout=180) as r:
                # Cloudflare 挑战 → 交给 curl（cdn.donmai.us 实测 403 vs curl 200）
                if r.status_code in (403, 503) and curl_available() and looks_like_challenge(r.status_code, r.text):
                    ok, size = curl_download(s, url, dest)
                    if ok:
                        return True, size
                r.raise_for_status()
                total = 0
                with open(tmp, "wb") as fh:
                    for block in r.iter_content(chunk):
                        if block:
                            fh.write(block)
                            total += len(block)
            if total == 0:
                raise RuntimeError("empty body")
            tmp.replace(dest)
            return True, total
        except Exception as exc:
            if attempt < retries:
                time.sleep(sleep * attempt)
            else:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
                return False, 0
    return False, 0


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
