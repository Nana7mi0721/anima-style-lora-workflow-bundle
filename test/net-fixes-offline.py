"""网络源修复的离线自测（D 段）：不联网，全部用 fake session / 内存 ZIP。

覆盖：
  * _zip_name_fix：GBK 字节被当 cp437 解出的乱码还原
  * Range 读 ZIP：只读尾部 central directory + 每个条目的 local header/数据，
    用 fake session 按 Range 切片返回，验证解压内容与 CRC
  * archive_mode：auto（直连全失败 -> 回退 ZIP）/ http（不回退）/ zip（强制）
  * yande 差分语义：_eval_take / _parse_take / _yande_filter / _pick_family
  * enrich：source_bucket / yande_post_url / _pick_by_url / lookup_yande /
    source_rates，以及 cmd_enrich 的 yande 反查落报告（dry-run）

用法：  python test/net-fixes-offline.py
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BUNDLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE / "python"))

from animasl import config, enrich, fetch  # noqa: E402

checks = 0
errors: list[str] = []


def check(ok: bool, msg: str) -> None:
    global checks
    checks += 1
    print(("  ok   " if ok else "  FAIL ") + msg)
    if not ok:
        errors.append(msg)


# --------------------------------------------------------------------------
# fake HTTP：Range 切片 + HEAD 长度；download 永远失败（模拟 CDN 全挂）
# --------------------------------------------------------------------------
class Resp:
    def __init__(self, status: int, content: bytes = b"", headers: dict | None = None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}


class FakeZipSession:
    def __init__(self, blob: bytes, url_key: str = "bundle.zip"):
        self.blob = blob
        self.url_key = url_key
        self.headers: dict[str, str] = {}
        self.requests = 0

    def head(self, url, timeout=90):
        return Resp(200, headers={"Content-Length": str(len(self.blob))})

    def get(self, url, headers=None, timeout=180):
        if self.url_key not in str(url):
            raise IOError(f"404 {url}")
        self.requests += 1
        rng = str((headers or {}).get("Range") or "")
        if not rng.startswith("bytes="):
            return Resp(200, self.blob)
        a, _, b = rng[6:].partition("-")
        start = int(a or 0)
        end = int(b) if b else len(self.blob) - 1
        end = min(end, len(self.blob) - 1)
        return Resp(206, self.blob[start:end + 1],
                    {"Content-Range": f"bytes {start}-{end}/{len(self.blob)}"})


class FakeNet:
    def __init__(self, posts: list[dict]):
        self.posts = posts
        self.downloads: list[str] = []

    def session(self, *a, **k):
        raise AssertionError("fetch 测试应直接 patch pawchive_session")

    def proxy_label(self):
        return ""

    def get_json(self, s, url, params=None):
        offset = int((params or {}).get("o") or 0)
        return self.posts[offset:] if offset == 0 else []

    def download(self, s, url, dest):
        self.downloads.append(url)
        return False, 0


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(zipfile.ZipInfo("12-1.jpg"), b"JPEGDATA" * 200)   # stored
        zf.writestr("notes.txt", b"hello " * 500, compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def main() -> int:
    print("[net-fixes-offline] 网络源修复（pawchive ZIP / yande 差分 / enrich 反查）")

    # --- 1. GBK 文件名还原 -------------------------------------------------
    gbk = "示例 12-1.jpg".encode("gbk")
    mojibake = gbk.decode("cp437")
    check(fetch._zip_name_fix(mojibake) == "示例 12-1.jpg",
          "GBK 字节被当 cp437 解出的乱码能还原（_zip_name_fix）")
    check(fetch._zip_name_fix("plain-ascii.jpg") == "plain-ascii.jpg",
          "ASCII 名字原样返回")
    check(fetch._zip_name_fix("正常中文.jpg") == "正常中文.jpg",
          "本来就是真 unicode 的名字原样返回")

    # --- 2. Range 局部读 ZIP ---------------------------------------------
    blob = make_zip()
    s = FakeZipSession(blob)
    url = "https://file.pawchive.pw/data/x/bundle.zip?f=bundle.zip"
    entries, total = fetch._zip_central_dir(s, url, len(blob))
    names = [e["name"] for e in entries]
    check(total == 2 and names == ["12-1.jpg", "notes.txt"],
          f"central directory 只读文件尾就解析出来（{names}）")
    check(s.requests <= 3, f"目录解析只发了 {s.requests} 次 Range（不整包下载）")
    by_base, by_tail = fetch._zip_index(entries)
    ent = by_base["12-1.jpg"]
    data = fetch._zip_extract(s, url, ent)
    check(data == b"JPEGDATA" * 200, "按 Range 取 local header + 数据并解压（stored）")
    ent2 = by_base["notes.txt"]
    check(fetch._zip_extract(s, url, ent2) == b"hello " * 500,
          "deflate 条目解压 + CRC 校验通过")
    check(by_tail.get("12-1.jpg") is ent, "_zip_index 的空格后缀兜底索引可用")

    # --- 3. archive_mode：auto / zip / http --------------------------------
    root = Path(tempfile.mkdtemp(prefix="_netfix_home_"))
    try:
        cfg = config.Config.load(home=str(root))
        ds = cfg.dataset("nettest")
        ds.ensure_dirs()
        post = {
            "id": 7,
            "user": 5,
            "service": "patreon",
            "published": "2024-01-02T03:04:05",
            "title": "t",
            "file": {"name": "cover.jpeg", "path": "/data/cover.jpeg", "node": 1},
            "attachments": [
                {"name": "12-1.jpg", "path": "/data/12-1.jpg", "node": 1},
                {"name": "bundle.zip", "path": "/data/bundle.zip", "node": 9},
            ],
        }
        net_fake = FakeNet([post])
        old_net = fetch.net
        old_session = fetch.pawchive_session
        fetch.net = net_fake
        fetch.pawchive_session = lambda cfg, cookies_file="", user_agent="": s
        try:
            res = fetch.fetch_pawchive(cfg, ds, "5", service="patreon", limit=1,
                                       archive_mode="auto")
            check(res["archives"]["7"]["mode"] == "zip" and res["zip_posts"] == 1,
                  f"auto：直连全失败后回退 ZIP（{res['archives']['7']}）")
            check(res["ok"] == 1 and res["covers_skipped"] == 1,
                  "auto：ZIP 救回 1 张、封面被跳过（include_cover 默认关）")
            check(any(p.suffix == ".jpg" for p in ds.raw_dir.iterdir()),
                  "ZIP 里的图真的落盘到 00_raw/")
            res2 = fetch.fetch_pawchive(cfg, ds, "5", service="patreon", limit=1,
                                        archive_mode="http")
            check(res2["archives"]["7"]["mode"] == "http" and res2["ok"] == 0,
                  "http：只直连、不回退 ZIP")
            ds.ensure_dirs()
            res3 = fetch.fetch_pawchive(cfg, ds, "5", service="patreon", limit=1,
                                        archive_mode="zip")
            check(res3["archives"]["7"]["mode"] == "zip" and res3["ok"] == 1,
                  "zip：强制走 ZIP")
        finally:
            fetch.net = old_net
            fetch.pawchive_session = old_session

        # --- 4. yande 差分语义 ---------------------------------------------
        check(fetch._parse_take("max(2,n//2)", 5) == 2
              and fetch._parse_take("max(2,n//2)", 9) == 4
              and fetch._parse_take("3", 9) == 3
              and fetch._parse_take("", 9) == 2
              and fetch._parse_take("垃圾", 9) == 2,
              "take 表达式解析（max(2,n//2) / 整数 / 默认 2 / 无法解析退回 2）")
        check(fetch._parse_take("99", 3) == 3, "take 被族内张数钳住（不会越界）")
        posts = [
            {"id": 10, "created_at": 100, "parent_id": None},
            {"id": 11, "created_at": 200, "parent_id": 10},
            {"id": 12, "created_at": 300, "parent_id": 11},
            {"id": 13, "created_at": 400, "parent_id": None},
        ]
        filtered = fetch._yande_filter(posts, min_id=11, since="1970-01-01")
        check([p["id"] for p in filtered] == [11, 12, 13],
              "min_id / since 在分组前过滤（增量抓取不会把旧帖带回来）")
        keep_all, dropped = fetch._pick_family(posts, take="2")
        check(len(keep_all) == 3 and len(dropped) == 1,
              f"families 模式每族按 take 取样（3 张的族取 2、单张族留 1，共 {len(keep_all)} 张）")
        check({p["id"] for p in keep_all} == {10, 12, 13},
              "取样含 parent 与隔张 child，不是只留最新一张")
        keep1, _ = fetch._pick_family(posts, take="1")
        check({p["id"] for p in keep1} == {10, 13},
              "take=1 时留 parent（最老那张），不是最新")
        old_keep, _ = fetch._yande_keep({p["id"]: p for p in posts})
        check({p["id"] for p in old_keep} == {12, 13}, "families=false 仍是老行为（每族最新一张）")

        # --- 5. enrich：来源分桶 + yande 帖号反查 ---------------------------
        check(enrich.source_bucket({"source": "yandere"}) == "yande"
              and enrich.source_bucket({"page_url": "https://danbooru.donmai.us/posts/1"}) == "danbooru"
              and enrich.source_bucket({"source": "pawchive"}) == "other"
              and enrich.source_bucket({}) == "unknown",
              "source_bucket 按 source 字段 / 帖页域名分桶")
        row = {"source": "yandere", "post_id": 12345}
        check(enrich.yande_post_url(row) == "https://yande.re/post/show/12345",
              "yande 行能还原帖页 URL")
        url1 = "https://yande.re/post/show/12345"
        check(enrich._pick_by_url([{"id": 1, "source": url1}], url1) is not None,
              "_pick_by_url 认 source 完全匹配")
        check(enrich._pick_by_url([{"id": 1, "source": "x"}, {"id": 2, "source": "y"}], url1) is None,
              "多帖且无 source 匹配时拒绝猜（错标签比没标签更糟）")
        check(enrich._pick_by_url([{"id": 3, "source": ""}], url1)["id"] == 3,
              "查询只返回一帖时接受它")

        calls = []
        payloads = [[], [{"id": 99, "source": url1, "tag_string": "1girl solo"}]]
        old_get_json = enrich.net.get_json
        enrich.net.get_json = lambda s, url, params=None: (calls.append(params), payloads.pop(0))[1]
        try:
            post2, how = enrich.lookup_yande(object(), url1)
        finally:
            enrich.net.get_json = old_get_json
        check(post2 and post2["id"] == 99 and how == "yande-source",
              f"lookup_yande 第二条查询 tags=source:<url> 命中（{how}）")
        check(len(calls) == 2, "lookup_yande 先 search[url] 再 source:<url>")
        rates = enrich.source_rates({"yande": {"queried": 10, "hit": 0},
                                     "danbooru": {"queried": 5, "hit": 1}})
        check(rates[0][0] == "yande" and rates[0][4] == 0
              and any(r[0] == "danbooru" and r[4] > 0 for r in rates)
              and all(len(r) == 5 for r in rates),
              "source_rates 固定顺序、没查过的来源也列出来，按来源给命中率")

        # --- 6. cmd_enrich：yande 反查命中后写进报告 ------------------------
        ds2 = cfg.dataset("enrichtest")
        ds2.ensure_dirs()
        img = ds2.images_dir / "0001.jpg"
        from PIL import Image
        Image.new("RGB", (8, 8), (10, 20, 30)).save(img)
        (ds2.images_dir / "0001.txt").write_text("1girl", encoding="utf-8")
        (ds2.pipe_dir / "raw_posts.jsonl").write_text(json.dumps(
            {"source": "yandere", "post_id": 12345, "filename": "0001.jpg"}) + "\n",
            encoding="utf-8")
        old_lookup, old_yande, old_dan = enrich.lookup, enrich.lookup_yande, enrich.net.danbooru_session
        enrich.net.danbooru_session = lambda cfg: object()
        enrich.lookup = lambda s, md5: None
        enrich.lookup_yande = lambda s, post_url, limit=5: (
            {"id": 99, "source": post_url, "tag_string": "1girl solo",
             "file_ext": "jpg", "created_at": "2024-01-01", "rating": "s"}, "yande-url")
        try:
            out = enrich.cmd_enrich(ds2, apply=False, sleep=0)
        finally:
            enrich.lookup, enrich.lookup_yande = old_lookup, old_yande
            enrich.net.danbooru_session = old_dan
        rep = ds2.pipe_dir / "enrich_report.dryrun.csv"
        text = rep.read_text(encoding="utf-8") if rep.exists() else ""
        check(out["by_yande"] == 1 and out["hit"] == 1,
              f"cmd_enrich 走 yande 帖号反查并命中（by_yande={out['by_yande']}）")
        check("yande" in text and "match" in text,
              "dry-run 报告按来源记录 yande 命中（enrich_report.dryrun.csv）")
        check(not (ds2.pipe_dir / "raw_posts.jsonl").read_text(encoding="utf-8").count("danbooru-md5"),
              "dry-run 不写 raw_posts.jsonl")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n[net-fixes-offline] {checks} 项" + ("通过，ALL OK" if not errors else f"中 {len(errors)} 项失败"))
    for e in errors:
        print("  - " + e)
    return 0 if not errors else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(2)