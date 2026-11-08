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

import ast
import os
import re
import struct
import time
import zlib
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

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


DEFAULT_TAKE = 2                 # families 模式下每族默认保留张数（族内不足就全留）


def _eval_take(expr: str, n: int) -> int:
    """Evaluate a `take` expression: only n, integers, + - * / // and min()/max().

    No `eval` of arbitrary code -- take comes from the command line, and `eval`
    there would hand the shell a way into the process.
    """
    tree = ast.parse(expr, mode="eval")

    def ev(node: Any) -> int:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return int(node.value)
        if isinstance(node, ast.Name) and node.id == "n":
            return int(n)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            val = ev(node.operand)
            return -val if isinstance(node.op, ast.USub) else val
        if isinstance(node, ast.BinOp):
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.FloorDiv):
                return left // right
            if isinstance(node.op, ast.Div):
                return left // right          # integer semantics: no 2.5 images
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("min", "max") and len(node.args) == 2
                and not node.keywords):
            a, b = ev(node.args[0]), ev(node.args[1])
            return min(a, b) if node.func.id == "min" else max(a, b)
        raise ValueError(f"unsupported take expression node: {type(node).__name__}")

    return ev(tree)


def _parse_take(spec: str, n: int) -> int:
    """`take` spec + family size n -> how many to keep from that family (clamped to 1..n).

    The spec is either an integer ("3") or a small expression over n
    ("max(2,n//2)"); empty or unparsable falls back to DEFAULT_TAKE.  The default
    is not 1 on purpose: keeping only the newest image collapses a whole diff
    sequence ("dressed -> nude -> after") into a single training sample.
    """
    n = int(n)
    if n <= 0:
        return 0
    text = str(spec or "").strip()
    k = DEFAULT_TAKE
    if text:
        try:
            k = int(_eval_take(text, n))
        except Exception:
            k = DEFAULT_TAKE
    return max(1, min(n, k))


def _pick_family(posts: list[dict], take: str = "") -> tuple[list[dict], dict[int, str]]:
    """families=True selection: keep the parent plus k-1 evenly spread children.

    Members are ordered oldest-first (parent, then diffs, then "after" shots), so
    `k` samples spread over the sequence instead of piling up at the newest end.
    """
    pool = {int(p["id"]): p for p in posts}
    roots = _yande_families(pool)
    groups: dict[int, list[int]] = {}
    for pid, root in roots.items():
        groups.setdefault(root, []).append(pid)
    keep: list[dict] = []
    dropped: dict[int, str] = {}
    for root, members in groups.items():
        members.sort(key=lambda i: (pool[i].get("created_at") or 0, i))
        n = len(members)
        k = _parse_take(take, n)
        if k >= n:
            idxs = list(range(n))
        elif k <= 1:
            idxs = [0]                         # parent (oldest) survives
        else:
            idxs = sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})
        chosen = set(idxs)
        keep.extend(pool[members[i]] for i in idxs)
        for pos, pid in enumerate(members):
            if pos not in chosen:
                dropped[pid] = f"family-{root}-took-{k}of{n}"
    keep.sort(key=lambda p: (p.get("created_at") or 0, p["id"]), reverse=True)
    return keep, dropped


def _since_ts(value: Any) -> float:
    """`since` -> unix timestamp (0 = no filter).  Accepts YYYY-MM-DD or a unix stamp."""
    text = str(value or "").strip()
    if not text:
        return 0.0
    if re.fullmatch(r"\d{9,11}", text):
        return float(text)
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if not m:
        return 0.0
    try:
        return time.mktime((int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            0, 0, 0, 0, 0, -1))
    except (OverflowError, ValueError):
        return 0.0


def _yande_filter(posts: Iterable[dict], min_id: int = 0, since: str = "") -> list[dict]:
    """min_id / since filter, applied before family grouping.

    Incremental fetches pass min_id=<last seen id> so old posts never re-enter the
    pool -- family grouping over a partial pool would otherwise pick a "newest"
    member the user already has.
    """
    ts = _since_ts(since)
    floor = int(min_id or 0)
    out: list[dict] = []
    for post in posts:
        try:
            pid = int(post.get("id") or 0)
        except (TypeError, ValueError):
            pid = 0
        if floor and pid < floor:
            continue
        if ts and float(post.get("created_at") or 0) < ts:
            continue
        out.append(post)
    return out


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


PIXIV_URL_RX = re.compile(
    r"pixiv\.net/(?:[a-z]{2}/)?(?:artworks?/|i/|member_illust\.php\?illust_id=)(\d+)", re.I)


def pixiv_id_from_url(url: str) -> str:
    """Extract a pixiv illust id out of any pixiv URL ('' when there is none).

    yande.re (and booru posts in general) keep the original pixiv link in their
    `source` field; enrich reuses that id to find the danbooru mirror when the
    md5 lookup comes up empty.
    """
    m = PIXIV_URL_RX.search(str(url or ""))
    return m.group(1) if m else ""


def fetch_yandere(cfg, ds, tags: str, limit: int, dry_run: bool = False,
                  families: bool = False, take: str = "",
                  min_id: int = 0, since: str = "") -> dict:
    """yande.re fetch.

    families=False (default) keeps only the newest member of every family (the
    old behaviour).  families=True keeps the parent plus a few children: each
    family contributes `take` images spread evenly over the sequence
    (`take` is an integer or an expression such as "max(2,n//2)", default 2).
    min_id / since filter the posts before grouping, so incremental fetches only
    ever see new posts.
    """
    s = net.session(cfg, YANDE_API, {"tags": "rating:safe", "limit": 1}, referer="https://yande.re/")
    print(f"[fetch] yande.re  tags={tags!r}  limit={limit}  proxy={net.proxy_label()}"
          + (f"  families take={take or f'默认{DEFAULT_TAKE}张/族'}" if families else ""))
    want = int(limit * RAW_POOL_SLACK) + 20
    if families:
        want *= 2                # whole families are kept, so the window must be wider
    pool = _yande_pages(s, tags, want)
    print(f"  raw candidates: {len(pool)}")
    if min_id or since:
        before = len(pool)
        pool = {p["id"]: p for p in _yande_filter(pool.values(), min_id, since)}
        print(f"  after min_id={min_id or '-'} since={since or '-'}: {len(pool)} "
              f"(filtered out {before - len(pool)})")
    _yande_expand(s, pool)
    if families:
        keep, dropped = _pick_family(list(pool.values()), take)
    else:
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
            # enrich 用：yande 帖的出典（多半是 pixiv 链接）与从中抽出的作品 id
            "source_url": str(post.get("source") or ""),
            "pixiv_id": pixiv_id_from_url(post.get("source") or ""),
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
    from_source = 0
    for idx, post in enumerate(posts, 1):
        url = post.get("file_url") or post.get("large_file_url") or ""
        # 新帖常见 `file_url` 为 null（未通过审核的 banned/受限帖，或 API 只给部分字段）。
        # 这时回源到 `source`（pixiv 原图直链）还能救回来一张图 —— 真实使用里
        # g4ku 复盘 5.1 #9 就是这么丢了整批新帖。pixiv 直链**必须带 Referer**，
        # 否则 403（这就是 net.download 之外的专用路径）。
        src_url = str(post.get("source") or "")
        pixiv_src = src_url if src_url.startswith("http") and "pixiv" in src_url else ""
        if not url and not pixiv_src:
            rows.append({"source": "danbooru", "post_id": post.get("id"), "status": "no-file-url"})
            continue
        tag_string = post.get("tag_string") or ""
        tags_all = tag_string.split()
        fetch_url = url or pixiv_src                 # 没有 file_url 就回源 pixiv 直链
        ext = "." + (post.get("file_ext") or ext_of(fetch_url).lstrip("."))
        if ext in (".", ""):
            ext = ".jpg"
        fname = build_name("danbooru", post["id"], "_".join(tags_all[:6]), ext, ds.raw_dir, used)
        row = {
            "source": "danbooru",
            "post_id": post["id"],
            "page_url": f"https://danbooru.donmai.us/posts/{post['id']}",
            "url": fetch_url,
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
        if not url:
            row["from_source"] = True
            from_source += 1
        if dry_run:
            row["status"] = "planned"
        else:
            if url:
                good, size = net.download(s, url, ds.raw_dir / fname)
            else:
                # pixiv 直链：不带 Referer 一律 403；用同一 session 起一次带头的请求
                good, size = net.download_with_referer(
                    s, fetch_url, ds.raw_dir / fname, referer="https://www.pixiv.net/")
            row["status"] = "ok" if good else "failed"
            row["bytes"] = size or row["bytes"]
            ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
            time.sleep(0.4)
        rows.append(row)
        if idx % 25 == 0 or idx == len(posts):
            print(f"  [{idx}/{len(posts)}] ok={ok} failed={fail}"
                  + (f"  (回源 {from_source})" if from_source else ""))
    if from_source:
        print(f"  {from_source} 张没有 file_url，已回源到 source（pixiv 直链带 Referer）")
    return {"rows": rows, "kept": len(posts), "dropped": 0, "ok": ok, "failed": fail,
            "from_source": from_source}


# --------------------------------------------------------------------------
# pawchive.pw  (kemono-style public API: Patreon / Fanbox / Discord archives)
# --------------------------------------------------------------------------
PAW_API = "https://pawchive.pw/api/v1"
PAW_REFERER = "https://pawchive.pw/"
# 帖子附件 ZIP 走 file.pawchive.pw —— 与图片 CDN n{node}.pawchive.pw **不同主机**。
# 实测（ruzhai 复盘 D 段）：n1 / n3..n10 TLS 握手失败、n2 404，`--tlsv1.2 -k` 也无效，
# 唯一还能通的通道就是这条主机，而且必须用 Range 局部读，整包下载既慢又容易断。
PAW_FILE_HOST = "https://file.pawchive.pw/data"
# UA 必须自报家门：浏览器 UA 在 CDN 上一律 403（同段实测）。user_agent 可覆盖。
PAW_UA = ("anima-style-lora/0.1 (+https://github.com/Nana7mi0721/"
          "anima-style-lora-workflow-bundle)")
PAW_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif")
ZIP_TAIL = 64 * 1024              # EOCD 在文件末尾，只读最后 64KB


def _zip_name_fix(name: str) -> str:
    """zip 条目名还原：GBK 字节被当 cp437 解出的乱码 -> 真名。

    附件 ZIP 是 Windows 中文环境打的：条目名是 GBK 字节，但 central directory
    里没有 UTF-8 标志位，解析器按 cp437 一解码就成了 `┐┬╟` 这类乱码。这里把字符串
    按 cp437 编回字节，再依次按 gbk / gb18030 / utf-8 试解；都不成说明它本来就是
    正常名字（ASCII 名字、或带 UTF-8 标志位的），原样返回。
    """
    text = str(name or "")
    if not text:
        return text
    try:
        raw = text.encode("cp437")
    except UnicodeEncodeError:
        return text                      # 已经是真 unicode 名，没有乱码可还原
    for codec in ("gbk", "gb18030", "utf-8"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return text


def _resp_bytes(resp) -> bytes:
    """requests.Response -> bytes（也让测试里的 fake response 能直接喂进来）。"""
    content = getattr(resp, "content", None)
    if content is None:
        raw = getattr(resp, "raw", None)
        content = raw.read() if raw is not None else b""
    return content if isinstance(content, bytes) else bytes(content or b"")


def _range_bytes(s, url: str, start: int, end: int, retries: int = 4,
                 sleep: float = 1.5) -> bytes:
    """HTTP Range 读闭区间 [start, end]（服务器忽略 Range 时整包读再切片）。

    ZIP 的目录和数据全靠这一条：只取需要的那几十 KB，附件多大都不影响。
    """
    size = end - start + 1
    if size <= 0:
        return b""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = s.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=180)
            code = int(getattr(resp, "status_code", 200))
            data = _resp_bytes(resp)
            if code == 206 and data:
                return data
            if code == 200:
                return data[start:end + 1]
            raise IOError(f"HTTP {code}（{len(data)} 字节）")
        except Exception as exc:          # noqa: BLE001 - 重试见下
            last = exc
            if attempt < retries:
                time.sleep(sleep * attempt)
    raise IOError(f"Range {start}-{end} 读 {url} 失败: {last}")


def _head_size(s, url: str, retries: int = 3, sleep: float = 1.5) -> int:
    """zip 总长：先 HEAD 的 Content-Length；HEAD 被拒就 Range: bytes=0-0 看 Content-Range。"""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            head = getattr(s, "head", None)
            if head is not None:
                resp = head(url, timeout=90)
                if int(getattr(resp, "status_code", 200)) < 400:
                    hdrs = getattr(resp, "headers", None) or {}
                    length = hdrs.get("Content-Length") or hdrs.get("content-length")
                    if length:
                        return int(length)
            resp = s.get(url, headers={"Range": "bytes=0-0"}, timeout=90)
            hdrs = getattr(resp, "headers", None) or {}
            crange = str(hdrs.get("Content-Range") or hdrs.get("content-range") or "")
            m = re.search(r"/\s*(\d+)\s*$", crange)
            if m:
                return int(m.group(1))
            length = hdrs.get("Content-Length") or hdrs.get("content-length")
            if length:
                return int(length)
            raise IOError("响应里没有长度信息")
        except Exception as exc:          # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(sleep * attempt)
    raise IOError(f"取 {url} 体积失败: {last}")


def _zip_central_dir(s, url: str, size: int) -> tuple[list[dict], int]:
    """只读文件尾部的 central directory，返回 (entries, n_total)。

    entries: [{'name','raw','flags','method','crc','csize','usize','lho'}]
    """
    if size <= 0:
        return [], 0
    tail = _range_bytes(s, url, max(0, size - ZIP_TAIL), size - 1)
    i = tail.rfind(b"PK\x05\x06")
    if i < 0:
        raise IOError("zip 尾部没有 EOCD（不是 zip，或文件被截断）")
    if i + 22 > len(tail):
        raise IOError("EOCD 被截断")
    (_sig, _disk, _cd_disk, _n_here, n_total, cd_size, cd_off,
     _clen) = struct.unpack("<IHHHHIIH", tail[i:i + 22])
    # zip64：字段顶到 0xFFFF / 0xFFFFFFFF 时真值在 zip64 EOCD 里
    if n_total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
        if i >= 20 and tail[i - 20:i - 16] == b"PK\x06\x07":
            (_dsk, eocd64_off, _ndsk) = struct.unpack("<IQI", tail[i - 16:i])
            rec = _range_bytes(s, url, int(eocd64_off), int(eocd64_off) + 55)
            if rec[:4] == b"PK\x06\x06" and len(rec) >= 56:
                (_s6, _sz6, _vm, _vn, _d6, _cd6, _n_here6, n_total,
                 cd_size, cd_off) = struct.unpack("<IQHHIIQQQQ", rec[:56])
    total = int(n_total)
    if total <= 0 or cd_size <= 0:
        return [], max(total, 0)
    cd = _range_bytes(s, url, int(cd_off), int(cd_off) + int(cd_size) - 1)
    entries: list[dict] = []
    pos = 0
    while pos + 46 <= len(cd) and cd[pos:pos + 4] == b"PK\x01\x02":
        (_sig, _vmade, _vneed, flags, method, _mtime, _mdate, crc, csize, usize,
         nlen, elen, clen, _dstart, _iattr, _eattr,
         lho) = struct.unpack("<IHHHHHHIIIHHHHHII", cd[pos:pos + 46])
        raw_name = cd[pos + 46:pos + 46 + nlen]
        if len(raw_name) < nlen:
            break
        if flags & 0x800:
            name = raw_name.decode("utf-8", "replace")
        else:
            name = _zip_name_fix(raw_name.decode("cp437", "replace"))
        entries.append({"name": name, "raw": raw_name, "flags": flags,
                        "method": int(method), "crc": int(crc),
                        "csize": int(csize), "usize": int(usize), "lho": int(lho)})
        pos += 46 + nlen + elen + clen
    return entries, total


def _zip_extract(s, url: str, ent: dict) -> bytes:
    """按 central directory 的偏移取 local header + 压缩数据并解压（deflate/stored）。

    只发两次 Range：30 字节 local header（拿 nlen/elen），再读数据本体。
    """
    lh = _range_bytes(s, url, ent["lho"], ent["lho"] + 29)
    if lh[:4] != b"PK\x03\x04" or len(lh) < 30:
        raise IOError("local header 不对（偏移错位或文件已变）")
    nlen, elen = struct.unpack("<HH", lh[26:30])
    start = ent["lho"] + 30 + nlen + elen
    raw = _range_bytes(s, url, start, start + ent["csize"] - 1) if ent["csize"] else b""
    if ent["method"] == 0:
        data = raw
    elif ent["method"] == 8:
        data = zlib.decompress(raw, -15)
    else:
        raise IOError(f"不支持的压缩方式 {ent['method']}（只认 stored/deflate）")
    if ent["usize"] and len(data) != ent["usize"]:
        raise IOError(f"解压长度不符 {len(data)} != {ent['usize']}")
    if ent["crc"] and (zlib.crc32(data) & 0xFFFFFFFF) != ent["crc"]:
        raise IOError("CRC 校验失败")
    return data


def _zip_index(entries: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """basename -> entry；再加一层「最后一个空格之后」的兜底索引。

    乱码被 _zip_name_fix 修好后仍可能与 API 名字差一点（空格、全半角括号），
    但 `名字 12-1.jpg` 这种「空格后缀」两边通常一致（paw_dl3.py 的做法）。
    """
    by_base: dict[str, dict] = {}
    by_tail: dict[str, dict] = {}
    for ent in entries:
        base = os.path.basename(ent["name"])
        by_base.setdefault(base, ent)
        by_tail.setdefault(base.rsplit(" ", 1)[-1], ent)
    return by_base, by_tail


def _paw_zip_url(att: dict) -> str:
    """附件 ZIP 的下载地址（file.pawchive.pw；`?f=` 是它要求的原始文件名）。"""
    url = f"{PAW_FILE_HOST}{att.get('path') or ''}"
    name = str(att.get("name") or "")
    return f"{url}?f={quote(name)}" if name else url


def _paw_row(post: dict, item: dict) -> dict:
    """与其它源同形的元数据行（文件名/扩展名由调用方的 plan 决定）。"""
    return {
        "source": "pawchive",
        "post_id": post["id"],
        "creator": post.get("user"),
        "service": post.get("service"),
        "page_url": (f"https://pawchive.pw/{post.get('service')}/user/"
                     f"{post.get('user')}/post/{post['id']}"),
        "url": item["url"],
        "title": post.get("title") or "",
        "filename": item["filename"],
        "ext": item["ext"],
        "bytes": 0,
        "width": 0,
        "height": 0,
        "created_at": post_date(post.get("published")),
        "rating": "",
        "tags": [],
    }


def _paw_fail_row(post: dict, item: dict, reason: str) -> dict:
    row = _paw_row(post, item)
    row["status"] = "failed"
    row["reason"] = reason
    row["archive"] = {"mode": "zip"}
    return row


def _paw_http_post(s, ds, post: dict, plan: list[dict]) -> list[dict]:
    """直连 `n{node}.pawchive.pw`（老路径；有些机器上这个 CDN 已经全挂）。"""
    rows: list[dict] = []
    for item in plan:
        good, size = net.download(s, item["url"], ds.raw_dir / item["filename"])
        row = _paw_row(post, item)
        row["status"] = "ok" if good else "failed"
        row["bytes"] = size
        row["archive"] = {"mode": "http"}
        if not good:
            row["reason"] = "cdn-failed"
        rows.append(row)
        time.sleep(0.6)
    return rows


def _paw_zip_post(s, ds, post: dict, plan: list[dict]) -> list[dict]:
    """ZIP 通道：Range 局部读帖子附件 ZIP，只取 plan 点名的图。

    一个 ZIP 的目录只读一次；每张图两次 Range（local header + 压缩数据），
    整包永远不落盘。条目名先过 _zip_name_fix，再按 basename / 空格后缀匹配。
    """
    zips = [a for a in (post.get("attachments") or [])
            if str(a.get("name") or "").lower().endswith(".zip") and a.get("path")]
    if not zips:
        print(f"    ! 帖 {post['id']}: 没有 ZIP 附件")
        return [_paw_fail_row(post, item, "no-zip") for item in plan]
    att = zips[0]
    zip_url = _paw_zip_url(att)
    try:
        size = _head_size(s, zip_url)
        entries, total = _zip_central_dir(s, zip_url, size)
    except Exception as exc:              # noqa: BLE001 - 一帖失败不该拖垮整批
        print(f"    ! 帖 {post['id']}: ZIP 目录读取失败 {type(exc).__name__}: {exc}")
        return [_paw_fail_row(post, item, "zip-unreadable") for item in plan]
    by_base, by_tail = _zip_index(entries)
    print(f"    · 帖 {post['id']}: ZIP {size / 1e6:.1f}MB / {total} 个条目，取 {len(plan)} 张")
    rows: list[dict] = []
    for item in plan:
        base = os.path.basename(item["name"])
        ent = by_base.get(base) or by_tail.get(base.rsplit(" ", 1)[-1])
        if not ent:
            rows.append(_paw_fail_row(post, item, "not-in-zip"))
            continue
        try:
            data = _zip_extract(s, zip_url, ent)
        except Exception as exc:          # noqa: BLE001
            print(f"      ! {base}: 抽取失败 {type(exc).__name__}: {exc}")
            rows.append(_paw_fail_row(post, item, "zip-extract-failed"))
            continue
        dest = ds.raw_dir / item["filename"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        row = _paw_row(post, item)
        row["status"] = "ok"
        row["bytes"] = len(data)
        row["archive"] = {"mode": "zip",
                          "zip": os.path.basename(str(att.get("name") or "")),
                          "entry": ent["name"], "crc": ent["crc"]}
        rows.append(row)
        time.sleep(0.2)
    return rows


def pawchive_session(cfg, cookies_file: str = "", user_agent: str = "") -> Any:
    """pawchive 的 session：**必须用可识别的非浏览器 UA**（浏览器 UA 会被 CDN 403）。

    `file.pawchive.pw` 与 API 都按 UA 放行；`user_agent` 参数可覆盖默认值。
    """
    s = net.session(cfg, PAW_API + "/posts?o=0", referer=PAW_REFERER)
    s.headers["User-Agent"] = str(user_agent or "").strip() or PAW_UA
    if cookies_file:
        jar = net.load_cookie_jar(cookies_file)
        s.cookies.update(jar)
        print(f"  cookies: {len(jar)} 条 from {cookies_file}")
    return s


def fetch_pawchive(cfg, ds, creator: str, service: str = "patreon",
                   limit: int = 0, dry_run: bool = False, cookies_file: str = "",
                   user_agent: str = "", title_filter: str = "",
                   include_cover: bool = False, archive_mode: str = "auto") -> dict:
    """`creator` = numeric creator id (the /user/<id> part of a pawchive URL).

    **`file` 是封面，不是作品**（g4ku 复盘 5.1 #4：一次抓取 39 张 `cover.jpeg` 混进数据集，
    既没内容也没标签，只能事后手工剔）。现在默认跳过 `file`，只抓 `attachments`
    （真正的作品图）。确实要封面就传 `include_cover=True`。

    pawchive 的帖子**没有标签**（它不是 booru），所以 `tags` 只能是空列表 ——
    这个源的打标 100% 依赖后续 enrich（md5/pixiv_id 反查）与看图。

    取图通道 `archive_mode`（ruzhai 复盘 D 段：CDN 在某些机器上 SSL 全挂）：
      auto  先直连 `n{node}.pawchive.pw`；该帖一张都没拿到 → 回退帖子附件 ZIP
      zip   强制 ZIP：HTTP Range 局部读 central directory，只取需要的那几张
      http  只直连，不回退
    结果里 `archives` 记录每帖实际走的通道，报告能看出哪些帖是 ZIP 救回来的。
    """
    mode = str(archive_mode or "auto").strip().lower()
    if mode not in ("auto", "zip", "http"):
        print(f"  ! archive_mode={archive_mode!r} 不认识，按 auto 处理")
        mode = "auto"
    s = pawchive_session(cfg, cookies_file, user_agent)
    print(f"[fetch] pawchive  service={service}  creator={creator}  limit={limit or 'all'}  "
          f"archive_mode={mode}  proxy={net.proxy_label()}")
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
    covers_skipped = 0
    archives: dict[str, dict] = {}
    zip_posts = 0
    for idx, post in enumerate(posts, 1):
        files: list[dict] = []
        if post.get("file"):
            if include_cover:
                files.append(post["file"])
            else:
                covers_skipped += 1
        files.extend(post.get("attachments") or [])
        plan: list[dict] = []
        for fobj in files:
            path = fobj.get("path") or ""
            if not path:
                continue
            name = fobj.get("name") or Path(path).name
            ext = os.path.splitext(name)[1].lower() or ext_of(path)
            if ext not in PAW_IMG_EXTS:
                continue                  # 视频 / psd / zip 都不进数据集
            node = fobj.get("node") or 1
            plan.append({
                "name": name,
                "path": path,
                "node": node,
                "ext": ext,
                "url": f"https://n{node}.pawchive.pw/data{path}",
                "filename": build_name("pawchive", post["id"],
                                       sanitize(Path(name).stem)[:40], ext,
                                       ds.raw_dir, used),
            })
        if not plan:
            continue
        pid = str(post["id"])
        got: list[dict] = []
        used_mode = "zip" if mode == "zip" else "http"
        if dry_run:
            for item in plan:
                row = _paw_row(post, item)
                row["status"] = "planned"
                row["archive"] = {"mode": used_mode, "planned": True}
                got.append(row)
        elif mode == "http":
            got = _paw_http_post(s, ds, post, plan)
        elif mode == "zip":
            got = _paw_zip_post(s, ds, post, plan)
        else:                             # auto：先直连，整帖全失败才回退 ZIP
            got = _paw_http_post(s, ds, post, plan)
            if not any(r.get("status") == "ok" for r in got):
                zrows = _paw_zip_post(s, ds, post, plan)
                if any(r.get("status") == "ok" for r in zrows):
                    got = zrows
                    used_mode = "zip"
                else:
                    print(f"    ! 帖 {pid}: 直连与 ZIP 都没拿到图")
        good = sum(1 for r in got if r.get("status") in ("ok", "planned"))
        bad = sum(1 for r in got if r.get("status") == "failed")
        ok += good
        fail += bad
        if used_mode == "zip":
            zip_posts += 1
        archives[pid] = {"mode": used_mode, "files": good, "failed": bad}
        rows.extend(got)
        if idx % 10 == 0 or idx == len(posts):
            print(f"  [{idx}/{len(posts)}] files={len(rows)} ok={ok} failed={fail}"
                  + (f"  zip_posts={zip_posts}" if zip_posts else ""))
    if covers_skipped:
        print(f"  cover 跳过 {covers_skipped} 张（封面不是作品；要抓就传 includeCover:true）")
    if zip_posts:
        print(f"  ZIP 通道：{zip_posts} 帖（直连 CDN 拿不到图，改用帖子附件 ZIP + Range 局部读）")
    if rows and not any(r.get("tags") for r in rows):
        print("  ! pawchive 帖子没有标签（它不是 booru）——这个源的打标要靠 enrich + 看图")
    return {"rows": rows, "kept": len(posts), "dropped": 0, "ok": ok, "failed": fail,
            "covers_skipped": covers_skipped, "archives": archives,
            "archive_mode": mode, "zip_posts": zip_posts}


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
