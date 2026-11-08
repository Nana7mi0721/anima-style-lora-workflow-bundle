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

**yande 源的 md5 反查几乎无效**（实测 2/253）：yande 的字节在上传链路上被重编码过，
md5 对不上是常态。对这种来源改走「yande 帖号 → danbooru `source` URL」反查
（`lookup_yande`），仍查不到就跳过 —— 报告按来源分桶给命中率，并提示优先用
families 视觉合并补齐（同族/同一张图先按像素匹配合并，再借同源帖的标签）。

    fetch/import → **enrich** → dedup → screen → text → rename → wash

顺序红线：`text`（文字修补）会重写像素 ⇒ md5 变 ⇒ 查不到。所以要跑在 text 之前；
已经修补过的老数据集，只能拿没修补过的副本（如 `00_raw/`）来查。

写盘语义：**默认 dry-run（只查不写）**，`apply:true` 才落进 raw_posts.jsonl。
dry-run 仍然会发请求 —— 它存在的意义就是"先看命中率再决定要不要跑"，所以
**dry-run 也写报告**（`_pipeline/enrich_report.dryrun.csv`，不覆盖真跑的那份）。
"""

from __future__ import annotations

import csv
import hashlib
import re
import time
from pathlib import Path

from . import curate, net
from .fetch import DANBOORU_API, pixiv_id_from_url, post_date

CHUNK = 1 << 20
SLEEP = 0.35                      # 查询间隔：~3 req/s，远低于 danbooru 认证用户的额度
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
# pixiv 下载器的常见命名：<illust_id>_p<page>-标题.png / <illust_id>_p<page>.jpg
PIXIV_RX = re.compile(r"^(\d+)_p(\d+)", re.I)

# yande 帖页 URL 里的 post id：报告和反查都靠它把行还原成「哪一帖」。
YANDE_POST_RX = re.compile(r"yande\.re/post/show/(\d+)", re.I)
# 分来源统计用固定顺序（yande / danbooru / 其他 / 未知），报告行序才稳定
SOURCE_KEYS = ("yande", "danbooru", "other", "unknown")
SOURCE_LABELS = {"yande": "yande 源", "danbooru": "danbooru 源",
                 "other": "其他 booru", "unknown": "未知来源"}


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


def source_bucket(row: dict) -> str:
    """把一行元数据归到报告分桶：yande / danbooru / other / unknown。

    判据优先看 \`source\` 字段（fetch 写着 yandere / danbooru / pawchive / exhentai），
    再看帖页 URL 的域名，最后认不出来才归 unknown —— 分不清来源就没法解释命中率。
    """
    src = str(row.get("source") or "").strip().lower()
    if "yandere" in src or "yande" in src:
        return "yande"
    if "danbooru" in src:
        return "danbooru"
    for other in ("pawchive", "exhentai", "e-hentai", "pixiv", "gelbooru", "sankaku",
                  "konachan", "safebooru", "zerochan", "twitter", "x.com"):
        if other in src:
            return "other"
    for field in ("page_url", "post_url", "source_url", "url"):
        text = str(row.get(field) or "").lower()
        if "yande.re" in text:
            return "yande"
        if "danbooru.donmai.us" in text:
            return "danbooru"
        if text.startswith("http"):
            return "other"
    return "unknown"


def yande_post_url(row: dict) -> str:
    """把一行 fetch 元数据还原成 yande 帖页 URL；还原不出来返回 ""。

    yandere 的行里 \`post_id\` 就是 yande 帖号，\`page_url\` 也常带着完整 URL；
    两者都没有（手改过的行）就放弃，不猜。
    """
    for field in ("page_url", "post_url"):
        m = YANDE_POST_RX.search(str(row.get(field) or ""))
        if m:
            return f"https://yande.re/post/show/{m.group(1)}"
    pid = str(row.get("post_id") or "").strip()
    if pid.isdigit() and source_bucket(row) == "yande":
        return f"https://yande.re/post/show/{pid}"
    return ""


def _pick_by_url(posts: list[dict], post_url: str) -> dict | None:
    """从 danbooru 的查询结果里挑出**确实**指向这条 yande 帖的那一帖。

    只要有一条的 \`source\` 就是这条 yande URL 就用它；一条都没有时，只有「整个查询
    只返回一帖」才认。多帖时猜错会把别人的标签贴上来 —— 错标签比没标签更糟。
    """
    want = post_url.rstrip("/").lower()
    hits = [p for p in posts if want in str(p.get("source") or "").lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits and len(posts) == 1:
        return posts[0]
    return None


def lookup_yande(s, post_url: str, limit: int = 5) -> tuple[dict | None, str]:
    """yande 帖号 → danbooru 原帖：先 \`search[url]\`，再 \`tags=source:<url>\`。

    为什么要有这条路（ruzhai 复盘 D 段）：yande 源的文件字节在上传链路上几乎都被
    重编码过，\`md5:\` 反查实测只有 2/253 命中。但 yande 与 danbooru 上大量是同一批
    画师的作品，danbooru 帖的 \`source\` 字段里留着 yande 帖页 URL —— 这条线索跟像素
    无关，稳得多，命中就能拿回整条 \`tag_string\`。

    两个查询都打空时返回 \`(None, "yande-none")\`，由调用方记成没查到（不猜）。
    """
    if not post_url:
        return None, ""
    last: Exception | None = None
    for params, how in (({"search[url]": post_url, "limit": limit}, "yande-url"),
                        ({"tags": f"source:{post_url}", "limit": limit}, "yande-source")):
        try:
            data = net.get_json(s, DANBOORU_API, params)
        except Exception as exc:                      # 换下一个查询，别整批断掉
            last = exc
            continue
        if not isinstance(data, list) or not data:
            continue
        post = _pick_by_url(data, post_url)
        if post is not None:
            return post, how
    if last is not None:                              # 两个查询都是网络错：往上抛，别记成没查到
        raise last
    return None, "yande-none"


def source_rates(stats: dict[str, dict[str, int]]
                 ) -> list[tuple[str, str, int, int, float]]:
    """报告用：(key, label, hit, queried, rate) 固定顺序；没查过的来源也会列出来。"""
    out = []
    for key in SOURCE_KEYS:
        st = stats.get(key) or {}
        q = int(st.get("queried") or 0)
        h = int(st.get("hit") or 0)
        out.append((key, SOURCE_LABELS[key], h, q, (h / q) if q else 0.0))
    return out


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
        return {"rows": [], "hit": 0, "miss": 0, "skipped": 0, "queried": 0,
                "written": 0, "by_md5": 0, "by_pixiv": 0, "by_yande": 0,
                "by_source": {k: {"queried": 0, "hit": 0} for k in SOURCE_KEYS},
                "report": ""}

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
    by_md5 = by_pixiv = by_yande = 0
    # 分来源统计：yande 源的 md5 命中率≈0（2/253），混在总命中率里会被彻底掩盖，
    # 所以每张图先定来源桶，查询次数/命中数都记到桶上，报告按桶给命中率。
    stats: dict[str, dict[str, int]] = {k: {"queried": 0, "hit": 0} for k in SOURCE_KEYS}
    for idx, p in enumerate(imgs, 1):
        if limit and queried >= limit:
            print(f"  （--limit {limit} 已用完，剩 {len(imgs) - idx + 1} 张没查）")
            break
        key = lookup_key(p.name, rmap)
        prev = meta.get(key) or {}
        bucket = source_bucket(prev)
        if not force and prev.get("tags"):
            skipped += 1
            report.append({"file": p.name, "key": key, "md5": "", "by": "",
                           "status": "has-tags", "source": bucket,
                           "post_id": prev.get("post_id") or "",
                           "tags_n": len(prev["tags"])})
            continue
        md5 = file_md5(p)
        if not force and prev.get("post_id") and str(prev.get("md5") or "") == md5:
            skipped += 1
            report.append({"file": p.name, "key": key, "md5": md5, "by": "",
                           "status": "same-md5", "source": bucket,
                           "post_id": prev.get("post_id") or "", "tags_n": 0})
            continue
        queried += 1
        stats[bucket]["queried"] += 1
        by, post, pixiv_id = "md5", None, ""
        try:
            post = lookup(s, md5)
            time.sleep(sleep)
            # 第一条支路（yande 源专用）：yande 帖号 → danbooru 的 source URL。
            # yande 的字节几乎都被重编码过，md5 查不到是常态（实测 2/253），这条才是主路。
            if post is None and bucket == "yande":
                yurl = yande_post_url(prev)
                if yurl:
                    queried += 1
                    post, by = lookup_yande(s, yurl)
                    time.sleep(sleep)
            # 第二条支路：文件名里的 pixiv illust id。同一张图被 danbooru 重编码过就永远
            # md5 不中，但 pixiv_id 还在；yande 行还能从 source_url 里取到这个 id。
            if post is None and not no_pixiv:
                info = pixiv_of(key)
                page = info[1] if info else 0
                if info:
                    pixiv_id = info[0]
                elif bucket == "yande":
                    pixiv_id = (str(prev.get("pixiv_id") or "")
                                or pixiv_id_from_url(str(prev.get("source_url") or "")))
                if pixiv_id:
                    queried += 1
                    cands = lookup_pixiv(s, pixiv_id)
                    time.sleep(sleep)
                    post, how = pick_pixiv(cands, key, page, _size(p))
                    by = how
                    if post is not None and bucket == "yande":
                        by = "yande-pixiv"          # yande 源靠作品 id 找回来，单独计数
                    elif post is None and how.endswith("none") and not cands:
                        # 该作品在 danbooru 一帖都没有：还是记成没查到
                        by = "yande-none" if bucket == "yande" else "md5"
        except Exception as exc:                       # 网络/风控失败不该中断整批
            print(f"  ! {p.name}: {type(exc).__name__}: {exc}")
            report.append({"file": p.name, "key": key, "md5": md5, "by": "",
                           "status": f"error:{type(exc).__name__}", "source": bucket,
                           "post_id": "", "tags_n": 0})
            time.sleep(sleep)
            continue
        if post is None:
            miss += 1
            if by.startswith("pixiv"):
                note, status = f"（{by}）", by
            elif by.startswith("yande-"):
                note, status = f"（{bucket} 反查未中）", "no-match"
            else:
                note, status = f"（md5 {md5[:8]}…）", "no-match"
            report.append({"file": p.name, "key": key, "md5": md5, "by": by,
                           "status": status, "source": bucket,
                           "post_id": "", "tags_n": 0})
            print(f"  [{idx}/{len(imgs)}] {p.name}  ✗ danbooru 上没有这张图 {note}")
            continue
        hit += 1
        stats[bucket]["hit"] += 1
        row = post_row(post, key, md5, p.name, by=by, pixiv=pixiv_id)
        row["lookup"]["from_source"] = bucket    # 记下这行是从哪一类来源补出来的
        rows.append(row)
        if by == "md5":
            by_md5 += 1
        elif by.startswith("pixiv") or by == "yande-pixiv":
            by_pixiv += 1
        else:                                    # yande-url / yande-source
            by_yande += 1
        report.append({"file": p.name, "key": key, "md5": md5, "by": by,
                       "status": "match", "source": bucket, "post_id": post.get("id"),
                       "tags_n": len(row["tags"])})
        print(f"  [{idx}/{len(imgs)}] {p.name}  ✓ post {post.get('id')}  "
              f"{len(row['tags'])} 标签  rating={row['rating'] or '-'}  by={by}  "
              f"source={bucket}")

    written = _merge(ds, rows, force=force) if (apply and rows) else 0
    # 报告**永远**写（包括 dry-run）。早先只在 apply 时写，真实使用的反馈是
    # 「dry-run 跑了几分钟，零产出」（g4ku 复盘 5.1 #6）—— 而 dry-run 的全部意义
    # 就是先看命中率再决定要不要 apply，没有报告等于白跑。
    # dry-run 写 enrich_report.dryrun.csv，别覆盖上一次真跑的报告。
    if report:
        path = ds.pipe_dir / ("enrich_report.csv" if apply else "enrich_report.dryrun.csv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["file", "key", "md5", "by", "status",
                                               "source", "post_id", "tags_n"])
            w.writeheader()
            w.writerows(report)
        print(f"[enrich] 报告 -> {path}（{len(report)} 行）")

    print(f"[enrich] 命中 {hit}（md5 {by_md5} / pixiv_id {by_pixiv} / "
          f"yande 帖号 {by_yande}）　查不到 {miss}　"
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
        if by_yande:
            print(f"         其中 {by_yande} 张是 yande 帖号反查 danbooru source URL 命中的")

    # 分来源命中率：总命中率会把「yande 几乎全灭、danbooru 正常」糊成一团，
    # 看不出该换哪条路。这里按桶列清楚，再给一句结论。
    rates = source_rates(stats)
    if any(q for _k, _lbl, _h, q, _r in rates):
        print("[enrich] 分来源命中率：")
        for _k, label, h, q, r in rates:
            if q:
                print(f"         {label}  {h}/{q}  {r:.0%}")
        yd = next((r for r in rates if r[0] == "yande"), None)
        if yd and yd[3]:
            print("         ! yande 源 md5 命中率≈0，优先用 families 视觉合并：yande 的字节"
                  "几乎都被重编码过，md5 必然对不上（实测 2/253）。")
            print("           本次已改走「yande 帖号 → danbooru source URL」反查；仍补不上就别硬凑，"
                  "交给 families 按族合并后用同源帖的标签。")
    return {"rows": rows, "hit": hit, "miss": miss, "skipped": skipped,
            "queried": queried, "written": written, "by_md5": by_md5,
            "by_pixiv": by_pixiv, "by_yande": by_yande,
            "by_source": {k: dict(v) for k, v in stats.items()},
            "report": str(path) if report else ""}
