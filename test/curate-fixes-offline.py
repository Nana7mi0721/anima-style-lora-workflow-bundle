"""离线回归：图桶改造（rename / text / thumbs / exclude 的 work_set 与 names）。

    python test/curate-fixes-offline.py            # 跑完删临时数据集
    python test/curate-fixes-offline.py --keep     # 留着现场

覆盖的真实摩擦：成品被人工分进 clean/ watermark/ …，images/ 空着；老 rename 只认
00_raw，逼着把 153 张全扁平化再拆回去。这里钉住的断言：
  rename  桶内就地重排、编号跨桶连续且不重叠、文件不跨桶搬、rename_map 带 bucket 列
  rename  work_set 只重排一个桶；编号会撞上别的桶时拒绝而不是静默重号
  text    默认工作集 = 图桶（不是空的 images/）；work_set 只取该桶；00_raw 兜底
  thumbs  names 支持逗号 / 区间 / 通配；prune 永远按全量核对（过滤不误删）
  exclude names="@清单.txt" 逐行读（BOM / # 注释 / 空行 / 行内逗号）；文件不存在报清楚错误
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import shutil
import sys
import tempfile
from pathlib import Path

try:                                   # 控制台可能是 GBK，中文断言输出别乱码
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

KEEP = "--keep" in sys.argv
BUNDLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE / "python"))

from PIL import Image                                     # noqa: E402

from animasl import config as configmod                   # noqa: E402
from animasl import curate, state, text as textmod       # noqa: E402

errors: list[str] = []
passed = 0


def check(ok: bool, message: str) -> None:
    global passed
    if ok:
        passed += 1
        print(f"  ok    {message}")
    else:
        errors.append(message)
        print(f"  FAIL  {message}")


def run(fn, *args, **kwargs):
    """跑一个阶段并吞掉它的日志（断言看返回值/文件；日志单独拿去做关键字检查）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*args, **kwargs)
    return out, buf.getvalue()


def expect_exit(fn, *args, **kwargs) -> str:
    """跑一个应当以 SystemExit 拒绝的阶段，返回它给的错误文案。"""
    try:
        run(fn, *args, **kwargs)
    except SystemExit as exc:
        return str(exc)
    return ""


def png(path: Path, rgb=(120, 60, 30)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), rgb).save(path)


def names_of(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.suffix.lower() in curate.IMG_EXTS)


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


home = Path(tempfile.mkdtemp(prefix="animasl-curate-"))
cfg = configmod.Config.load(home=str(home))
ds = cfg.dataset("demo")
ds.ensure_dirs()
png(ds.root / "clean" / "a.png", (200, 40, 40))
png(ds.root / "clean" / "b.png", (40, 200, 40))
png(ds.root / "watermark" / "y.png", (40, 40, 200))
png(ds.root / "watermark" / "z.png", (200, 200, 40))
before_a = md5(ds.root / "clean" / "a.png")
print(f"[curate-fixes-offline] 临时数据集 {ds.root}")

# ===== rename：桶内就地重排 =================================================
print("\n-- rename：两个图桶、images/ 为空 --")
ret, out = run(curate.cmd_rename, ds)
check(ret["applied"] is False and "预演" in out, "rename 默认 dry-run")
check(not (ds.images_dir / "0001.png").exists()
      and names_of(ds.root / "clean") == ["a.png", "b.png"],
      "预演不动文件、不改名")
preview = ds.pipe_dir / "rename_map.preview.csv"
with open(preview, encoding="utf-8", newline="") as fh:
    prow = list(csv.DictReader(fh))
check(len(prow) == 4 and {r.get("bucket") for r in prow} == {"clean", "watermark"},
      "预演表 4 行、带 bucket 列")
check(not (ds.pipe_dir / "rename_map.csv").exists(), "预演不碰权威表 rename_map.csv")
ranges = {r["bucket"]: (r["first"], r["last"], r["count"]) for r in ret["ranges"]}
check(ranges == {"clean": (1, 2, 2), "watermark": (3, 4, 2)},
      "编号区间按桶给出且跨桶连续（clean 1-2 / watermark 3-4）")

ret, out = run(curate.cmd_rename, ds, apply=True)
check(sorted(names_of(ds.root / "clean")) == ["0001.png", "0002.png"]
      and sorted(names_of(ds.root / "watermark")) == ["0003.png", "0004.png"],
      "apply 后各桶内就地重排（a/b -> 0001/0002；y/z -> 0003/0004）")
check(md5(ds.root / "clean" / "0001.png") == before_a, "重排只改名、不改图内容")
check(names_of(ds.images_dir) == [], "文件没跨桶搬（images/ 仍为空）")
with open(ds.pipe_dir / "rename_map.csv", encoding="utf-8", newline="") as fh:
    rows = list(csv.DictReader(fh))
by_new = {r["new_name"]: r for r in rows}
check(len(rows) == 4 and "bucket" in rows[0], "rename_map 4 行 + bucket 列")
check(by_new["0001.png"]["old_name"] == "a.png" and by_new["0001.png"]["bucket"] == "clean",
      "rename_map: clean/a.png -> 0001.png")
check(by_new["0003.png"]["old_name"] == "y.png" and by_new["0003.png"]["bucket"] == "watermark",
      "rename_map: watermark/y.png -> 0003.png")
check("0001" in out and "0003" in out, "日志打印每个桶的编号区间")
st = state.load(ds)
check(st.get("0001.png").get("bucket") == "clean"
      and "a.png" in (st.get("0001.png").get("old_names") or []),
      "per_image.json 记住桶归属与旧名")

ret, out = run(curate.cmd_rename, ds, apply=True)
check(sorted(names_of(ds.root / "clean")) == ["0001.png", "0002.png"]
      and sorted(names_of(ds.root / "watermark")) == ["0003.png", "0004.png"],
      "重跑 rename 编号不变（幂等）")
st = state.load(ds)
check("0001.png" not in (st.get("0001.png").get("old_names") or []),
      "重跑同编号不会把新名当成旧名（old_names 不被污染）")

# ===== rename：workSet 只重排一个桶 + 跨桶撞号拒绝 ===========================
print("\n-- rename：workSet / 撞号保护 --")
png(ds.root / "watermark" / "new.png", (10, 90, 90))
msg = expect_exit(curate.cmd_rename, ds, work_set="watermark")
check("撞上" in msg, "work_set 重排会撞上别桶编号时拒绝（不是静默重号）")
check(sorted(names_of(ds.root / "watermark")) == ["0003.png", "0004.png", "new.png"],
      "拒绝时一个文件都没动")
ret, out = run(curate.cmd_rename, ds, apply=True, work_set="watermark", start=5)
check(sorted(names_of(ds.root / "watermark")) == ["0005.png", "0006.png", "0007.png"]
      and sorted(names_of(ds.root / "clean")) == ["0001.png", "0002.png"],
      "workSet=watermark 只重排该桶（clean 不动），start=5 避开别桶编号")
check(ret["ranges"] == [{"bucket": "watermark", "count": 3, "first": 5, "last": 7}],
      "workSet 只报选中桶的区间")

# ===== text：工作集优先 =====================================================
print("\n-- text：默认工作集 = 图桶 --")
textmod.TEXTDET = home / "missing-textdet.py"        # 离线钩子：没有 ML 脚本就直接返回
textmod.INPAINT = home / "missing-inpaint.py"
ret, out = run(textmod.cmd_text, ds)
check(ret.get("failed") == 5 and "5 张图" in out, "text 默认工作集 = 全部图桶（5 张，不是空的 images/）")
ret, out = run(textmod.cmd_text, ds, work_set="clean")
check(ret.get("failed") == 2 and "2 张图" in out, "text workSet 只取该桶")

ds2 = cfg.dataset("rawonly")
ds2.ensure_dirs()
png(ds2.raw_dir / "r1.png", (90, 90, 90))
png(ds2.raw_dir / "r2.png", (90, 90, 90))
ret, out = run(textmod.cmd_text, ds2)
check(ret.get("failed") == 2 and "2 张图" in out, "没有图桶时 text 退回 00_raw（老行为）")

bak = ds.pipe_dir / "orig_text"
png(bak / "0001.png", (30, 30, 30))
png(bak / "0005.png", (60, 60, 60))
ret, out = run(textmod.cmd_text_rollback, ds)
check(ret["count"] == 2, "text-rollback 能对上图桶里的图（不是只看 images/）")

# ===== thumbs：names 三种写法 + prune 全量语义 ==============================
print("\n-- thumbs：names 过滤 --")


def thumb_stems() -> list[str]:
    return sorted(p.stem for p in ds.thumbs_dir.glob("*.jpg"))


def clear_thumbs() -> None:
    for p in ds.thumbs_dir.glob("*.jpg"):
        p.unlink()


ret, out = run(curate.cmd_thumbs, ds)
check(ret["count"] == 5 and thumb_stems() == ["0001", "0002", "0005", "0006", "0007"],
      "thumbs 默认对全量图桶出图（5 张）")
clear_thumbs()
ret, out = run(curate.cmd_thumbs, ds, names="0001,0005")
check(ret["matched"] == 2 and ret["count"] == 2 and thumb_stems() == ["0001", "0005"],
      "names 逗号：只出命中那两张")
ret, out = run(curate.cmd_thumbs, ds, names="0001-0002")
check(ret["matched"] == 2 and thumb_stems() == ["0001", "0002", "0005"],
      "names 区间 0001-0012：0001/0002 命中")
ret, out = run(curate.cmd_thumbs, ds, names="000*")
check(ret["matched"] == 5 and "命中 5/5" in out, "names 通配 000*：5 张全命中")
ret, out = run(curate.cmd_thumbs, ds, names="0001.png", force=True)
check(ret["matched"] == 1, "names 允许带扩展名（按 stem 匹配）")
ret, out = run(curate.cmd_thumbs, ds, names="9999")
check(ret["matched"] == 0 and ret["count"] == 0 and "没有图命中" in out,
      "names 全落空时给提示、不报错")
run(curate.cmd_thumbs, ds, force=True)
(ds.thumbs_dir / "9999.jpg").write_bytes((ds.thumbs_dir / "0001.jpg").read_bytes())
ret, out = run(curate.cmd_thumbs, ds, names="0001", prune=True)
check(ret["pruned"] == 1 and "9999" not in thumb_stems(), "prune 清掉没有对应图的陈旧缩略图")
check("0002" in thumb_stems(), "prune 按全量核对：names 过滤不会误删别的图的缩略图")

# ===== exclude：@清单文件 ==================================================
print("\n-- exclude：names=@清单文件 --")
kill = home / "kill-list.txt"
kill.write_text("\ufeff# 手工淘汰（BOM + 注释 + 空行都要扛）\n\n0001\n0002.png,0005\n",
                encoding="utf-8")
ret, out = run(curate.cmd_exclude, ds, names="@" + str(kill), apply=True)
check(ret["moved"] == 3, "exclude @file：BOM/# 注释/空行/行内逗号都认（3 张）")
check(all((ds.excluded_dir / "user" / n).exists() for n in ("0001.png", "0002.png", "0005.png")),
      "淘汰的图进 _excluded/user/")
check(not (ds.root / "clean" / "0001.png").exists(), "清理后两个桶里都没有残留")
entries = {e.get("name"): e for e in state.load_user_deleted(ds)["entries"]}
check(entries.get("0001.png", {}).get("bucket") == "clean",
      "user_deleted.json 记住了桶归属（undo 能搬回原桶）")
missing_msg = expect_exit(curate.cmd_exclude, ds, names="@" + str(home / "nope-list.txt"))
check("清单文件不存在" in missing_msg and "nope-list.txt" in missing_msg,
      "清单文件不存在时明确报错（列出试过的路径）")

# ===== 收尾 ================================================================
print("\n" + ("[curate-fixes-offline] " + str(passed) + " 项通过，ALL OK" if not errors
             else f"[curate-fixes-offline] {passed} 项通过，{len(errors)} 项失败:"))
for item in errors:
    print(f"- {item}")
if KEEP:
    print(f"  现场保留在 {home}")
else:
    shutil.rmtree(home, ignore_errors=True)
sys.exit(0 if not errors else 1)
