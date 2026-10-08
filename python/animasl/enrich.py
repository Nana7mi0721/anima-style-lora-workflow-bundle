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

    fetch/import → **enrich** → dedup → screen → text → rename → wash

顺序红线：`text`（文字修补）会重写像素 ⇒ md5 变 ⇒ 查不到。所以要跑在 text 之前；
已经修补过的老数据集，只能拿没修补过的副本（如 `00_raw/`）来查。

写盘语义：**默认 dry-run（只查不写）**，`apply:true` 才落进 raw_posts.jsonl。
dry-run 仍然会发请求 —— 它存在的意义就是"先看命中率再决定要不要跑"。
"""

from __future__ import annotations

import csv
import hashlib
import time
from pathlib import Path

from . import curate, net
from .fetch import DANBOORU_API, post_date

CHUNK = 1 << 20
SLEEP = 0.35                      # 查询间隔：~3 req/s，远低于 danbooru 认证用户的额度
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


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


def post_row(post: dict, key: str, md5: str, name: str) -> dict:
    """与 `fetch.py` 的行保持同形，下游（rename / wash / screen）不必知道它从哪来。"""
    tags_all = (post.get("tag_string") or "").split()
    url = post.get("file_url") or post.get("large_file_url") or ""
    return {
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
        "lookup": {"by": "md5", "md5": md5, "file": name},
    }


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
               sleep: float = SLEEP) -> dict:
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
          f"mode={'apply' if apply else 'DRY-RUN（只查不写）'}")
    if not authed:
        print("  ! 未配置 danbooru 凭据：匿名看不到受限内容，命中率会偏低。"
              "设置页填「danbooru 账号 + API key」，或设 DANBOORU_LOGIN / DANBOORU_API_KEY")

    rows: list[dict] = []
    report: list[dict] = []
    hit = miss = skipped = queried = 0
    for idx, p in enumerate(imgs, 1):
        if limit and queried >= limit:
            print(f"  （--limit {limit} 已用完，剩 {len(imgs) - idx + 1} 张没查）")
            break
        key = lookup_key(p.name, rmap)
        prev = meta.get(key) or {}
        if not force and prev.get("tags"):
            skipped += 1
            report.append({"file": p.name, "key": key, "md5": "", "status": "has-tags",
                           "post_id": prev.get("post_id") or "", "tags_n": len(prev["tags"])})
            continue
        md5 = file_md5(p)
        if not force and prev.get("post_id") and str(prev.get("md5") or "") == md5:
            skipped += 1
            report.append({"file": p.name, "key": key, "md5": md5, "status": "same-md5",
                           "post_id": prev.get("post_id") or "", "tags_n": 0})
            continue
        queried += 1
        try:
            post = lookup(s, md5)
        except Exception as exc:                       # 网络/风控失败不该中断整批
            print(f"  ! {p.name}: {type(exc).__name__}: {exc}")
            report.append({"file": p.name, "key": key, "md5": md5,
                           "status": f"error:{type(exc).__name__}", "post_id": "", "tags_n": 0})
            time.sleep(sleep)
            continue
        time.sleep(sleep)
        if post is None:
            miss += 1
            report.append({"file": p.name, "key": key, "md5": md5, "status": "no-match",
                           "post_id": "", "tags_n": 0})
            print(f"  [{idx}/{len(imgs)}] {p.name}  ✗ danbooru 上没有这张图（md5 {md5[:8]}…）")
            continue
        hit += 1
        row = post_row(post, key, md5, p.name)
        rows.append(row)
        report.append({"file": p.name, "key": key, "md5": md5, "status": "match",
                       "post_id": post.get("id"), "tags_n": len(row["tags"])})
        print(f"  [{idx}/{len(imgs)}] {p.name}  ✓ post {post.get('id')}  "
              f"{len(row['tags'])} 标签  rating={row['rating'] or '-'}")

    written = _merge(ds, rows, force=force) if (apply and rows) else 0
    if apply and report:
        path = ds.pipe_dir / "enrich_report.csv"
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["file", "key", "md5", "status", "post_id", "tags_n"])
            w.writeheader()
            w.writerows(report)
        print(f"[enrich] 报告 -> {path}")

    print(f"[enrich] 命中 {hit}　查不到 {miss}　跳过 {skipped}（已有标签或已查过）　"
          f"共查 {queried} 次")
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
    return {"rows": rows, "hit": hit, "miss": miss, "skipped": skipped,
            "queried": queried, "written": written}
