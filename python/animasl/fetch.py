"""Dataset fetching: danbooru, yande.re, pawchive (kemono-style), exhentai.

Every fetcher writes originals into `<dataset>/00_raw/` and one metadata row per
file into `<dataset>/_pipeline/raw_posts.jsonl`.  The metadata row is the single
source of truth for the wash stage (booru tags, post date, rating, source id) --
never rely on the file name, yande.re/Danbooru file names routinely carry an
incomplete tag list.

Common row schema:
    {source, post_id, page_url, url, filename, ext, bytes, width, height,
     created_at, rating, tags[], tags_artist[], tags_character[], parent_id, md5}
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

from . import net

MAX_PATH_LEN = 250
RAW_POOL_SLACK = 1.7


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "unnamed"


def build_name(prefix: str, post_id: Any, tag_hint: str, ext: str, out_dir: Path, used: set[str]) -> str:
    base = sanitize(f"{prefix} {post_id} {tag_hint}".strip())
    while len(str(out_dir / (base + ext))) > MAX_PATH_LEN and len(base) > 24:
        base = base[:-8].rstrip()
    cand, n = base, 1
    while cand + ext in used:
        n += 1
        cand = f"{base} ({n})"
    used.add(cand + ext)
    return cand + ext


def post_date(value: Any) -> str:
    """Normalise a source timestamp to YYYY-MM-DD."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return time.strftime("%Y-%m-%d", time.localtime(float(value)))
    text = str(value).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return m.group(0)
    return text[:10]


def ext_of(url: str, fallback: str = ".jpg") -> str:
    path = url.split("?")[0]
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif") else fallback


# --------------------------------------------------------------------------
# yande.re  (moebooru: parent/child families, newest member wins)
# --------------------------------------------------------------------------
YANDE_API = "https://yande.re/post.json"
YANDE_LIMIT = 100


def _yande_pages(s, tags: str, want: int) -> dict[int, dict]:
    pool: dict[int, dict] = {}
    page = 1
    while len(pool) < want and page <= 40:
        try:
            data = net.get_json(s, YANDE_API, {"tags": tags, "page": page, "limit": YANDE_LIMIT})
        except Exception as exc:
            print(f"  ! yande.re page {page} failed: {exc}")
            break
        if not data:
            break
        for post in data:
            pool[post["id"]] = post
        page += 1
        time.sleep(0.4)
    return pool


def _yande_expand(s, pool: dict[int, dict]) -> None:
    """Pull in parents that the page window did not cover."""
    for _ in range(3):
        missing = [p["parent_id"] for p in pool.values()
                   if p.get("parent_id") and p["parent_id"] not in pool]
        if not missing:
            return
        ids = ",".join(f"id:{i}" for i in sorted(set(missing))[:100])
        try:
            data = net.get_json(s, YANDE_API, {"tags": ids, "limit": 100})
        except Exception:
            return
        if not data:
            return
        for post in data:
            pool[post["id"]] = post


def _yande_families(pool: dict[int, dict]) -> dict[int, str]:
    """Union-find over parent links -> {post_id: root_id}."""
    parent = {i: i for i in pool}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for pid, post in pool.items():
        pp = post.get("parent_id")
        if pp and pp in pool:
            union(pp, pid)
    return {i: find(i) for i in pool}


def _yande_keep(pool: dict[int, dict]) -> tuple[list[dict], dict[int, str]]:
    """Keep only the newest member of each family (created_at, then id)."""
    roots = _yande_families(pool)
    groups: dict[int, list[int]] = {}
    for pid, root in roots.items():
        groups.setdefault(root, []).append(pid)
    keep: list[dict] = []
    dropped: dict[int, str] = {}
    for root, members in groups.items():
        members.sort(key=lambda i: (pool[i].get("created_at") or 0, i), reverse=True)
        winner = members[0]
        keep.append(pool[winner])
        for loser in members[1:]:
            dropped[loser] = f"family-{root}-kept-{winner}"
    keep.sort(key=lambda p: (p.get("created_at") or 0, p["id"]), reverse=True)
    return keep, dropped


def _yande_source(post: dict) -> str:
    if post.get("file_url"):
        return post["file_url"]
    if post.get("source") and str(post["source"]).startswith("http"):
        return post["source"]
    return post.get("jpeg_url") or post.get("sample_url") or ""


def write_posts(ds, rows: list[dict], dry_run: bool = False) -> int:
    """Merge downloaded-post metadata into <dataset>/_pipeline/raw_posts.jsonl.

    This file is source ``A`` for everything downstream: rename copies
    ``post_id``/``tags_full`` into rename_map.csv, wash reads those as the base
    tags, screen reads ``created_at``.  A fetch that forgets to write it does
    not fail loudly -- it silently degrades the whole dataset to
    "trigger word only" captions, so the merge lives here next to the fetchers.
    """
    if dry_run or not rows:
        return 0
    path = ds.pipe_dir / "raw_posts.jsonl"
    existing = net.read_jsonl(path)
    known = {(r.get("filename"), r.get("post_id")) for r in existing}
    fresh = [r for r in rows if (r.get("filename"), r.get("post_id")) not in known]
    net.write_jsonl(path, existing + fresh)
    return len(fresh)


def fetch_yandere(cfg, ds, tags: str, limit: int, dry_run: bool = False) -> dict:
    s = net.session(cfg, YANDE_API, {"tags": "rating:safe", "limit": 1}, referer="https://yande.re/")
    print(f"[fetch] yande.re  tags={tags!r}  limit={limit}  proxy={net.proxy_label()}")
    want = int(limit * RAW_POOL_SLACK) + 20
    pool = _yande_pages(s, tags, want)
    print(f"  raw candidates: {len(pool)}")
    _yande_expand(s, pool)
    keep, dropped = _yande_keep(pool)
    print(f"  after family dedup: {len(keep)} (dropped children/parents: {len(dropped)})")
    keep = keep[:limit]

    rows: list[dict] = []
    used: set[str] = set()
    ok = fail = 0
    for idx, post in enumerate(keep, 1):
        url = _yande_source(post)
        tags_all = (post.get("tags") or "").split()
        ext = ext_of(url or ".jpg")
        fname = build_name("yande.re", post["id"], "_".join(tags_all[:6]), ext, ds.raw_dir, used)
        row = {
            "source": "yandere",
            "post_id": post["id"],
            "page_url": f"https://yande.re/post/show/{post['id']}",
            "url": url,
            "filename": fname,
            "ext": ext,
            "bytes": int(post.get("file_size") or 0),
            "width": int(post.get("width") or 0),
            "height": int(post.get("height") or 0),
            "created_at": post_date(post.get("created_at")),
            "rating": post.get("rating") or "",
            "tags": tags_all,
            "parent_id": post.get("parent_id") or None,
            "md5": post.get("md5") or "",
        }
        if dry_run:
            row["status"] = "planned"
        else:
            good, size = net.download(s, url, ds.raw_dir / fname)
            row["status"] = "ok" if good else "failed"
            row["bytes"] = size or row["bytes"]
            ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
            time.sleep(0.5)
        rows.append(row)
        if idx % 25 == 0 or idx == len(keep):
            print(f"  [{idx}/{len(keep)}] ok={ok} failed={fail}")
    return {"rows": rows, "kept": len(keep), "dropped": len(dropped), "ok": ok, "failed": fail}


# --------------------------------------------------------------------------
# danbooru  (official JSON API; auth via DANBOORU_LOGIN / DANBOORU_API_KEY)
# --------------------------------------------------------------------------
DANBOORU_API = "https://danbooru.donmai.us/posts.json"
DANBOORU_LIMIT = 200


def fetch_danbooru(cfg, ds, tags: str, limit: int, dry_run: bool = False) -> dict:
    s = net.danbooru_session(cfg)
    authed = bool(getattr(s, "auth", None))
    print(f"[fetch] danbooru  tags={tags!r}  limit={limit}  auth={'yes' if authed else 'ANONYMOUS'}  "
          f"proxy={net.proxy_label()}")
    if not authed:
        print("  ! 未配置 danbooru 凭据：匿名只能看 safe 内容且最多 2 个标签。"
              "去 danbooru 个人设置页拿 API key，填进设置页（danbooru 账号 / API key）"
              "或设环境变量 DANBOORU_LOGIN + DANBOORU_API_KEY")
    posts: list[dict] = []
    page = 1
    while len(posts) < limit and page <= 50:
        try:
            data = net.get_json(s, DANBOORU_API,
                                {"tags": tags, "limit": DANBOORU_LIMIT, "page": page})
        except Exception as exc:
            print(f"  ! danbooru page {page} failed: {exc}")
            break
        if not data:
            break
        posts.extend(data)
        page += 1
        time.sleep(0.6)
    print(f"  posts: {len(posts)}")
    posts = posts[:limit]

    rows: list[dict] = []
    used: set[str] = set()
    ok = fail = 0
    for idx, post in enumerate(posts, 1):
        url = post.get("file_url") or post.get("large_file_url") or ""
        if not url:
            rows.append({"source": "danbooru", "post_id": post.get("id"), "status": "no-file-url"})
            continue
        tag_string = post.get("tag_string") or ""
        tags_all = tag_string.split()
        ext = "." + (post.get("file_ext") or ext_of(url).lstrip("."))
        fname = build_name("danbooru", post["id"], "_".join(tags_all[:6]), ext, ds.raw_dir, used)
        row = {
            "source": "danbooru",
            "post_id": post["id"],
            "page_url": f"https://danbooru.donmai.us/posts/{post['id']}",
            "url": url,
            "filename": fname,
            "ext": ext,
            "bytes": int(post.get("file_size") or 0),
            "width": int(post.get("image_width") or 0),
            "height": int(post.get("image_height") or 0),
            "created_at": post_date(post.get("created_at")),
            "rating": post.get("rating") or "",
            "tags": tags_all,
            "tags_artist": (post.get("tag_string_artist") or "").split(),
            "tags_character": (post.get("tag_string_character") or "").split(),
            "tags_copyright": (post.get("tag_string_copyright") or "").split(),
            "parent_id": post.get("parent_id") or None,
            "md5": post.get("md5") or "",
        }
        if dry_run:
            row["status"] = "planned"
        else:
            good, size = net.download(s, url, ds.raw_dir / fname)
            row["status"] = "ok" if good else "failed"
            row["bytes"] = size or row["bytes"]
            ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
            time.sleep(0.4)
        rows.append(row)
        if idx % 25 == 0 or idx == len(posts):
            print(f"  [{idx}/{len(posts)}] ok={ok} failed={fail}")
    return {"rows": rows, "kept": len(posts), "dropped": 0, "ok": ok, "failed": fail}


# --------------------------------------------------------------------------
# pawchive.pw  (kemono-style public API: Patreon / Fanbox / Discord archives)
# --------------------------------------------------------------------------
PAW_API = "https://pawchive.pw/api/v1"


def pawchive_session(cfg, cookies_file: str = "", user_agent: str = "") -> Any:
    s = net.session(cfg, PAW_API + "/posts?o=0", referer="https://pawchive.pw/")
    if user_agent:
        s.headers["User-Agent"] = user_agent
    if cookies_file:
        jar = net.load_cookie_jar(cookies_file)
        s.cookies.update(jar)
        print(f"  cookies: {len(jar)} 条 from {cookies_file}")
    return s


def fetch_pawchive(cfg, ds, creator: str, service: str = "patreon",
                   limit: int = 0, dry_run: bool = False, cookies_file: str = "",
                   user_agent: str = "", title_filter: str = "") -> dict:
    """`creator` = numeric creator id (the /user/<id> part of a pawchive URL)."""
    s = pawchive_session(cfg, cookies_file, user_agent)
    print(f"[fetch] pawchive  service={service}  creator={creator}  limit={limit or 'all'}  "
          f"proxy={net.proxy_label()}")
    posts: list[dict] = []
    offset = 0
    while True:
        try:
            data = net.get_json(s, f"{PAW_API}/{service}/user/{creator}", {"o": offset})
        except Exception as exc:
            print(f"  ! pawchive offset {offset} failed: {exc}")
            break
        if not data:
            break
        posts.extend(data)
        offset += 50
        print(f"  offset {offset}: total {len(posts)}")
        if limit and len(posts) >= limit:
            break
        if len(data) < 50:
            break
        time.sleep(0.8)
    if title_filter:
        rx = re.compile(title_filter, re.I)
        posts = [p for p in posts if rx.search(p.get("title") or "")]
        print(f"  after title filter {title_filter!r}: {len(posts)}")
    if limit:
        posts = posts[:limit]

    rows: list[dict] = []
    used: set[str] = set()
    ok = fail = 0
    for idx, post in enumerate(posts, 1):
        files = []
        if post.get("file"):
            files.append(post["file"])
        files.extend(post.get("attachments") or [])
        for fobj in files:
            path = fobj.get("path") or ""
            node = fobj.get("node") or 1
            if not path:
                continue
            url = f"https://n{node}.pawchive.pw/data{path}"
            name = fobj.get("name") or Path(path).name
            ext = os.path.splitext(name)[1].lower() or ext_of(path)
            if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif", ".psd", ".zip"):
                continue  # skip video/archives for a LoRA dataset
            if ext in (".psd", ".zip"):
                continue
            fname = build_name("pawchive", post["id"], sanitize(Path(name).stem)[:40], ext,
                               ds.raw_dir, used)
            row = {
                "source": "pawchive",
                "post_id": post["id"],
                "creator": post.get("user"),
                "service": post.get("service"),
                "page_url": f"https://pawchive.pw/{post.get('service')}/user/{post.get('user')}/post/{post['id']}",
                "url": url,
                "title": post.get("title") or "",
                "filename": fname,
                "ext": ext,
                "bytes": 0,
                "width": 0,
                "height": 0,
                "created_at": post_date(post.get("published")),
                "rating": "",
                "tags": [],
            }
            if dry_run:
                row["status"] = "planned"
            else:
                good, size = net.download(s, url, ds.raw_dir / fname)
                row["status"] = "ok" if good else "failed"
                row["bytes"] = size
                ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
                time.sleep(0.6)
            rows.append(row)
        if idx % 10 == 0 or idx == len(posts):
            print(f"  [{idx}/{len(posts)}] files={len(rows)} ok={ok} failed={fail}")
    return {"rows": rows, "kept": len(posts), "dropped": 0, "ok": ok, "failed": fail}


# --------------------------------------------------------------------------
# exhentai  (cookies + api.php gdata; gallery -> /s/ pages -> #img)
# --------------------------------------------------------------------------
EX_API = "https://exhentai.org/api.php"


def fetch_exhentai(cfg, ds, gallery: str, limit: int = 0, dry_run: bool = False,
                   cookies_file: str = "", user_agent: str = "") -> dict:
    """`gallery` = full gallery URL or 'gid/token' pair."""
    s = net.session(cfg, "https://exhentai.org/", referer="https://exhentai.org/")
    if cookies_file:
        jar = net.load_cookie_jar(cookies_file)
        s.cookies.update(jar)
        print(f"  cookies loaded from {cookies_file}（{len(jar)} 条）")
    else:
        # 配置层（设置页填的 exhentai 三项）优先，其次环境变量
        for key, cfg_key, env in (("ipb_member_id", "exhentai_member_id", "EXHENTAI_MEMBER_ID"),
                                  ("ipb_pass_hash", "exhentai_pass_hash", "EXHENTAI_PASS_HASH"),
                                  ("igneous", "exhentai_igneous", "EXHENTAI_IGNEOUS")):
            val = str(cfg.get(cfg_key) or "").strip() or os.environ.get(env, "")
            if val:
                s.cookies.set(key, val, domain=".exhentai.org")
        if not s.cookies.get("ipb_member_id"):
            raise SystemExit("[fetch] exhentai 需要登录态：在设置页填 exhentai 三项"
                             "（ipb_member_id / ipb_pass_hash / igneous）、给一个 cookies.txt，"
                             "或设 EXHENTAI_MEMBER_ID / EXHENTAI_PASS_HASH / EXHENTAI_IGNEOUS 环境变量")
    m = re.search(r"/(?:g|mpv)/(\d+)/([0-9a-f]{10})", gallery)
    gid, token = (m.group(1), m.group(2)) if m else tuple((gallery.split("/") + [""])[:2])
    if not gid or not token:
        raise SystemExit("[fetch] exhentai 需要形如 https://exhentai.org/g/<gid>/<token>/ 的地址")

    meta = s.post(EX_API, json={"method": "gdata", "gidlist": [[int(gid), token]], "namespace": 1},
                  timeout=60)
    if meta.status_code != 200:
        raise SystemExit(f"[fetch] exhentai api.php HTTP {meta.status_code}（cookies 可能已失效）")
    info = (meta.json().get("gmetadata") or [{}])[0]
    tags = [t for t in (info.get("tags") or [])]
    title = info.get("title") or gid
    print(f"[fetch] exhentai  {title}  files={info.get('filecount')}  proxy={net.proxy_label()}")

    pages: list[str] = []
    pageno = 0
    while True:
        url = f"https://exhentai.org/g/{gid}/{token}/?p={pageno}"
        r = s.get(url, timeout=60)
        if r.status_code != 200:
            break
        found = re.findall(r'https://exhentai\.org/s/[0-9a-f]+/\d+-\d+', r.text)
        if not found:
            break
        pages.extend(dict.fromkeys(found))
        pageno += 1
        time.sleep(0.7)
        if limit and len(pages) >= limit:
            break
    pages = pages[:limit] if limit else pages
    print(f"  image pages: {len(pages)}")

    rows: list[dict] = []
    used: set[str] = set()
    ok = fail = 0
    for idx, page in enumerate(pages, 1):
        r = s.get(page, timeout=60)
        img = re.search(r'<img id="img" src="([^"]+)"', r.text)
        orig = re.search(r'<a href="([^"]+)">\s*Download original', r.text) or \
            re.search(r'id="i3".*?<a href="([^"]+)"', r.text, re.S)
        url = (orig.group(1) if orig else (img.group(1) if img else ""))
        if not url:
            rows.append({"source": "exhentai", "page_url": page, "status": "no-image-url"})
            continue
        ext = ext_of(url)
        fname = build_name("exhentai", gid, sanitize(title)[:40], ext, ds.raw_dir, used)
        row = {
            "source": "exhentai", "post_id": page.rsplit("/", 1)[-1], "gid": gid,
            "page_url": page, "url": url, "filename": fname, "ext": ext, "bytes": 0,
            "width": 0, "height": 0, "created_at": post_date(info.get("posted")),
            "rating": "explicit" if any(t.startswith("male:") or "sex" in t for t in tags) else "",
            "tags": [t.replace(" ", "_") for t in tags],
        }
        if dry_run:
            row["status"] = "planned"
        else:
            good, size = net.download(s, url, ds.raw_dir / fname, expected=None)
            row["status"] = "ok" if good else "failed"
            row["bytes"] = size
            ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
            time.sleep(1.0)
        rows.append(row)
        if idx % 10 == 0 or idx == len(pages):
            print(f"  [{idx}/{len(pages)}] ok={ok} failed={fail}")
    return {"rows": rows, "kept": len(pages), "dropped": 0, "ok": ok, "failed": fail}
