"""HTTP helpers: proxy probing, sessions, booru credentials, retries.

The machine's shell proxy is not reliable: some shells export a dead
HTTPS_PROXY, and the local clash port is not always listening.  So we probe
candidate network paths once per process ("direct first") and remember the
winner.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
    return s


def proxy_label() -> str:
    p = _PROBE_CACHE.get("proxy")
    if p == "__unset__":
        return "(未探测)"
    return p or "直连"


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
    login = os.environ.get("DANBOORU_LOGIN")
    key = os.environ.get("DANBOORU_API_KEY")
    if login and key:
        s.auth = (login, key)
    return s


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
