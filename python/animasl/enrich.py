"""按文件 md5 去 danbooru 反查原帖，把权威标签补进 raw_posts.jsonl。

为什么需要这一阶段（打标指南 §6「标签来源与三源优先级」）：

    A = booru 权威标签（**事实基准**，有 A 时不可被推翻）
    B = 看图（真相）
    C = 自动打标初稿（**待校验的输入，不是成品**）

`fetch` 从 booru 下载时顺手把每帖的 `tag_string` 写进 `_pipeline/raw_posts.jsonl`
的 `tags[]`，wash 读它当骨架。但 `import` 进来的本地图 / pixiv 图 / 用户自有图
**一个标签都没有**，A 源直接断链：caption 要么只剩触发词，要么全靠视觉子代理
（C 源）猜 —— 而 C 源的已知坑是幻觉、人数覆盖只有 25%、服装漏标约 1/3。

danbooru 支持 `md5:<hex>` 搜索。只要手上还有原始字节，就能把帖子找回来：
这张图在 danbooru 上有人传过 ⇒ 命中 ⇒ 拿回整条 `tag_string`（几十条），A 源补齐。

**md5 查不到时还会用 pixiv_id 兜一次**（除非 `--no-pixiv`）：pixiv 下载的文件名通常
是 `<illust_id>_p<page>`，而 danbooru 的 `pixiv_id` 字段可搜。这条支路救的是
「同一张图被 danbooru 重编码过 ⇒ md5 永远不中、但作品还在」的情况。多页作品的多个
候选尺寸常常完全一样，所以只认两种确定情形（见 `pick_pixiv`）：帖子的 `source`
原文件名与我们的 old_name 同名同页，或该作品只有一帖且我们是 p0 —— 其余不猜，
错标签比没标签更糟。

    fetch/import → **enrich** → dedup → screen → text → rename → wash

顺序红线：`text`（文字修补）会重写像素 ⇒ md5 变 ⇒ 查不到。所以要跑在 text 之前；
已经修补过的老数据集，只能拿没修补过的副本（如 `00_raw/`）来查。

写盘语义：**默认 dry-run（只查不写）**，`apply:true` 才落进 raw_posts.jsonl。
dry-run 仍然会发请求 —— 它存在的意义就是"先看命中率再决定要不要跑"。
"""

from __future__ import annotations

import csv
import hashlib
import re
import time
from pathlib import Path

from . import curate, net
from .fetch import DANBOORU_API, post_date

CHUNK = 1 << 20
SLEEP = 0.35                      # 查询间隔：~3 req/s，远低于 danbooru 认证用户的额度
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
# pixiv 下载器的常见命名：<illust_id>_p<page>-标题.png / <illust_id>_p<page>.jpg
PIXIV_RX = re.compile(r"^(\d+)_p(\d+)", re.I)


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def pixiv_of(name: str) -> tuple[str, int] | None:
    """从文件名解析 (illust_id, page)。`145960069_p2-标题.png` -> ('145960069', 2)。"""
    m = PIXIV_RX.match(Path(name).name)
    return (m.group(1), int(m.group(2))) if m else None


def _src_base(post: dict) -> str:
    """帖子 `source` 的原文件名（danbooru 常把上传者给的文件名原样存在这里）。

    实测：`149878653_p0.png` 这类同名同页，是比"尺寸相同"可靠得多的判据 ——
    同一作品的多页在 danbooru 上尺寸往往完全一样，只有文件名能区分是哪一页。
    """
    src = str(post.get("source") or "").split("?")[0].rstrip("/")
    return src.rsplit("/", 1)[-1].lower()


def _size(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def pick_pixiv(posts: list[dict], our_name: str, page: int,
               dims: tuple[int, int] | None = None) -> tuple[dict | None, str]:
    """从 `pixiv_id:` 的候选里挑出**同一页**那一条，挑不出就不猜。

    多页作品在 danbooru 上是一页一帖、尺寸常常一模一样，所以只按尺寸匹配会把
    p0 的标签贴到 p3 上（错标签比没标签更糟）。判据按可靠度排序：
      ① 帖子 `source` 的原文件名 == 我们的 old_name（同页，确定）
      ② 该作品在 danbooru 只有 1 帖、我们这张是 p0、且尺寸一致（同页，高置信）
    其余一律返回 None，由调用方记成 `pixiv-ambiguous` / `pixiv-none`。
    """
    base = Path(our_name).name.lower()
    for post in posts:
        if _src_base(post) == base:
            return post, "pixiv-source"
    if len(posts) == 1 and page == 0:
        post = posts[0]
        if dims is None or (post.get("image_width"), post.get("image_height")) == dims:
            return post, "pixiv-single"
    return None, ("pixiv-ambiguous" if len(posts) > 1 else "pixiv-none")


def lookup_pixiv(s, pixiv_id: str, limit: int = 50) -> list[dict]:
    """`pixiv_id:<id>` 查该作品在 danbooru 的全部帖（多页作品会有多帖）。"""
    data = net.get_json(s, DANBOORU_API, {"tags": f"pixiv_id:{pixiv_id}", "limit": limit})
    return data if isinstance(data, list) else []


def lookup_key(name: str, rmap: dict) -> str:
    """该把补标行写在 `raw_posts.jsonl` 的哪个键上。

    wash 的 `_source_tags()` 是这么找回帖子的：当前文件名 → `rename_map.csv` 的
    `old_name` → `raw_posts.jsonl[old_name]`。所以补标行必须写在**同一个键**上：
    还没 rename 时就是当前文件名；已经 rename 过就得写回 `old_name`，
    否则 wash 永远读不到（而且不会报错，只会静默退化）。
    """
    row = rmap.get(name) or {}
    return str(row.get("old_name") or name)


def lookup(s, md5: str) -> dict | None:
    """`md5:<hex>` 精确反查；命中返回帖子 dict，没有返回 None。"""
    data = net.get_json(s, DANBOORU_API, {"tags": f"md5:{md5}", "limit": 1})
    if not isinstance(data, list) or not data:
        return None
    return data[0]


def post_row(post: dict, key: str, md5: str, name: str, by: str = "md5",
             pixiv: str = "") -> dict:
    """与 `fetch.py` 的行保持同形，下游（rename / wash / screen）不必知道它从哪来。"""
    tags_all = (post.get("tag_string") or "").split()
    url = post.get("file_url") or post.get("large_file_url") or ""
    row = {
        "source": "danbooru-md5",
        "post_id": post.get("id"),
        "page_url": f"https://danbooru.donmai.us/posts/{post.get('id')}",
        "url": url,
        "filename": key,
        "ext": "." + (post.get("file_ext") or Path(name).suffix.lstrip(".")),
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
        "md5": post.get("md5") or md5,
        "status": "ok",
        "lookup": {"by": by, "md5": md5, "file": name},
    }
    if pixiv:
        row["lookup"]["pixiv_id"] = pixiv
    return row


def _merge(ds, rows: list[dict], force: bool = False) -> int:
    """按 (filename, post_id) 并入 raw_posts.jsonl；force 时替换同键旧行。"""
    path = ds.pipe_dir / "raw_posts.jsonl"
    existing = net.read_jsonl(path)
    if force:
        keys = {(r.get("filename"), r.get("post_id")) for r in rows}
        existing = [r for r in existing if (r.get("filename"), r.get("post_id")) not in keys]
    known = {(r.get("filename"), r.get("post_id")) for r in existing}
    fresh = [r for r in rows if (r.get("filename"), r.get("post_id")) not in known]
    net.write_jsonl(path, existing + fresh)
    return len(fresh)


def cmd_enrich(ds, limit: int = 0, apply: bool = False, force: bool = False,
               sleep: float = SLEEP, no_pixiv: bool = False) -> dict:
    root = curate.working_dir(ds)
    imgs = sorted(p for p in root.iterdir()
                  if p.is_file() and p.suffix.lower() in IMG_EXT) if root.exists() else []
    if not imgs:
        print(f"[enrich] {root.name}/ 里没有图片：先 fetch 或 import")
        return {"rows": [], "hit": 0, "miss": 0, "skipped": 0, "queried": 0, "written": 0}

    rmap = curate.load_rename_map(ds)
    meta = curate.load_meta(ds)
    s = net.danbooru_session(ds.cfg)
    authed = bool(getattr(s, "auth", None))
    print(f"[enrich] {len(imgs)} 张（工作集 {root.name}/）  "
          f"auth={'yes' if authed else 'ANONYMOUS'}  proxy={net.proxy_label()}  "
          f"mode={'apply' if apply else 'DRY-RUN（只查不写）'}"
          f"{'' if no_pixiv else '  fallback=pixiv_id'}")
    if not authed:
        print("  ! 未配置 danbooru 凭据：匿名看不到受限内容，命中率会偏低。"
              "设置页填「danbooru 账号 + API key」，或设 DANBOORU_LOGIN / DANBOORU_API_KEY")

    rows: list[dict] = []
    report: list[dict] = []
    hit = miss = skipped = queried = 0
    by_md5 = by_pixiv = 0
    for idx, p in enumerate(imgs, 1):
        if limit and queried >= limit:
            print(f"  （--limit {limit} 已用完，剩 {len(imgs) - idx + 1} 张没查）")
            break
        key = lookup_key(p.name, rmap)
        prev = meta.get(key) or {}
        if not force and prev.get("tags"):
            skipped += 1
            report.append({"file": p.name, "key": key, "md5": "", "by": "",
                           "status": "has-tags", "post_id": prev.get("post_id") or "",
                           "tags_n": len(prev["tags"])})
            continue
        md5 = file_md5(p)
        if not force and prev.get("post_id") and str(prev.get("md5") or "") == md5:
            skipped += 1
            report.append({"file": p.name, "key": key, "md5": md5, "by": "",
                           "status": "same-md5", "post_id": prev.get("post_id") or "",
                           "tags_n": 0})
            continue
        queried += 1
        by, post, pixiv_id = "md5", None, ""
        try:
            post = lookup(s, md5)
            time.sleep(sleep)
            # md5 查不到时，用文件名里的 pixiv illust id 再试一次。命中率差别很大：
            # 同一张图被 danbooru 重编码过就永远 md5 不中，但 pixiv_id 还在。
            if post is None and not no_pixiv:
                info = pixiv_of(key)
                if info:
                    pixiv_id = info[0]
                    queried += 1
                    cands = lookup_pixiv(s, pixiv_id)
                    time.sleep(sleep)
                    post, how = pick_pixiv(cands, key, info[1], _size(p))
                    by = how
                    if post is None and how.endswith("none") and not cands:
                        by = "md5"          # 该作品在 danbooru 一帖都没有：还是记成没查到
        except Exception as exc:                       # 网络/风控失败不该中断整批
            print(f"  ! {p.name}: {type(exc).__name__}: {exc}")
            report.append({"file": p.name, "key": key, "md5": md5, "by": "",
                           "status": f"error:{type(exc).__name__}", "post_id": "", "tags_n": 0})
            time.sleep(sleep)
            continue
        if post is None:
            miss += 1
            note = f"（{by}）" if by.startswith("pixiv") else f"（md5 {md5[:8]}…）"
            report.append({"file": p.name, "key": key, "md5": md5, "by": by,
                           "status": by if by.startswith("pixiv") else "no-match",
                           "post_id": "", "tags_n": 0})
            print(f"  [{idx}/{len(imgs)}] {p.name}  ✗ danbooru 上没有这张图 {note}")
            continue
        hit += 1
        row = post_row(post, key, md5, p.name, by=by, pixiv=pixiv_id)
        rows.append(row)
        if by == "md5":
            by_md5 += 1
        else:
            by_pixiv += 1
        report.append({"file": p.name, "key": key, "md5": md5, "by": by,
                       "status": "match", "post_id": post.get("id"),
                       "tags_n": len(row["tags"])})
        print(f"  [{idx}/{len(imgs)}] {p.name}  ✓ post {post.get('id')}  "
              f"{len(row['tags'])} 标签  rating={row['rating'] or '-'}  by={by}")

    written = _merge(ds, rows, force=force) if (apply and rows) else 0
    if apply and report:
        path = ds.pipe_dir / "enrich_report.csv"
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["file", "key", "md5", "by", "status",
                                               "post_id", "tags_n"])
            w.writeheader()
            w.writerows(report)
        print(f"[enrich] 报告 -> {path}")

    print(f"[enrich] 命中 {hit}（md5 {by_md5} / pixiv_id {by_pixiv}）　查不到 {miss}　"
          f"跳过 {skipped}（已有标签或已查过）　共查 {queried} 次")
    if hit:
        if apply:
            print(f"         写入 {written} 条元数据 -> {ds.pipe_dir / 'raw_posts.jsonl'}")
            print("         下一步 wash：这些标签作为「来源 A」并进 caption"
                  "（画师 / IP / meta 类仍按打标指南规则丢弃）")
        else:
            print("         DRY-RUN：没有写盘。确认命中率后加 apply:true 才落进 raw_posts.jsonl")
        # 权威全集往往比 caption 预算更宽：实测一张 danbooru 原帖 45~63 条，
        # wash 丢完画师/IP/meta 还剩四十多条，会顶到 §9 的 20~45 上限。
        dense = [len(r["tags"]) for r in rows]
        over = sum(1 for n in dense if n > 45)
        if dense:
            print(f"         原帖标签数 平均 {sum(dense) / len(dense):.0f} 条"
                  f"（最多 {max(dense)}）")
        if over:
            print(f"         注：{over} 张超过 §9 的 45 条上限。wash 先按规则丢类，"
                  "剩下的按指南 §2.2 从低优先级槽位（构图/背景/光影）用 fix-caption 往下删")
    if hit + miss:
        rate = hit / (hit + miss)
        print(f"         命中率 {rate:.0%}")
        if rate < 0.5 and miss:
            print("         ! 命中率偏低，常见原因：①已跑过 text 修补（像素被改，md5 必然对不上）"
                  "②图被裁剪或重编码过 ③这些图本来就没上传到 danbooru")
            if no_pixiv:
                print("         本次带了 --no-pixiv：pixiv_id 兜底那条路没走"
                      "（文件名形如 <illust_id>_p<page> 时它能救回重编码过的图）")
        if by_pixiv:
            print(f"         其中 {by_pixiv} 张是 pixiv_id 兜回来的（md5 不中但原帖还在）")
    return {"rows": rows, "hit": hit, "miss": miss, "skipped": skipped,
            "queried": queried, "written": written, "by_md5": by_md5, "by_pixiv": by_pixiv}
