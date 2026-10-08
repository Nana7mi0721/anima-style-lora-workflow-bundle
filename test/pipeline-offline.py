"""离线端到端回归：把真实使用里踩过的坑一条条钉住（不联网、不跑 ML）。

    <ml venv>/python.exe test/pipeline-offline.py            # 跑完删临时数据集
    <ml venv>/python.exe test/pipeline-offline.py --keep     # 留着现场

为什么要它：`E:\\LoRA_Train\\datasets\\redash` 那次真实使用里，代价最大的不是技术卡点，
而是流程自己的状态与参数契约问题。这些都能在几秒内离线复现，所以每条坑配一个断言：

  import  src 传数据集根 -> 必须拒绝（否则 images/thumbs 被卷进 00_raw）
  import  非图文件 -> 不再静默黑洞，报告里要有 skipped-non-image
  enrich  没有图片 -> 给指引不联网；补标行必须写在 rename_map 的 old_name 上
  enrich  pixiv 兜底 -> 只认同名同页；多页作品尺寸还一样时拒绝猜（错标签比没标签糟）
  thumbs  rename 之后 -> 必须用 images/（旧实现读 00_raw 打印 0 张）
  rename  跑第二批 -> rename_map.csv 只追加 + 留本批快照
  wash    手工删掉的图源标签 -> 默认不复活；--refresh-source 才整体重洗
  fix-caption -> 补/删/整条替换，删除项记入人工名单，重洗不复活
  verify  触发词位置 + caption 健康度要一起报
  dict-check -> 编造的标签必须被点出来（对照 post_count=0 与词典缺失）
  makecfg LR 超区间 -> 报错而不是静默收紧；preflight.txt 记依据与差异
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:                                   # 控制台可能是 GBK，中文断言输出别乱码
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

KEEP = "--keep" in sys.argv
BUNDLE = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
HOME_ROOT = BUNDLE.parents[1] / "datasets" if False else Path(os.environ.get("ANIMASL_TEST_HOME", "E:/LoRA_Train"))

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


def run(*argv: str, expect: int = 0) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BUNDLE / "python")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("ANIMASL_HOME", None)
    env.pop("ANIMASL_RUNTIME", None)
    proc = subprocess.run(
        [PYTHON, "-X", "utf8", "-m", "animasl.cli", "--home", str(home), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=str(BUNDLE / "python"),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != expect:
        print(f"      (exit {proc.returncode}, expected {expect})\n{out[-1500:]}")
    return proc.returncode, out


def make_png(path: Path, size: tuple[int, int] = (64, 48), color: tuple[int, int, int] = (200, 30, 30)) -> None:
    from PIL import Image
    Image.new("RGB", size, color).save(path)


base = Path(tempfile.mkdtemp(prefix="_pipeoff_", dir=str(HOME_ROOT / "datasets")))
home = base / "home"
ds_dir = home / "datasets" / "t"
src = base / "src"
(ds_dir).mkdir(parents=True, exist_ok=True)
src.mkdir(parents=True, exist_ok=True)
print(f"[pipeline-offline] 临时现场 {base}")

# 素材：3 张图 + 1 个非图文件（mp4 要落进 skipped-non-image 统计）
for i, name in enumerate(["a.png", "b.png", "c.png"]):
    make_png(src / name, color=(30 * i + 40, 60, 200 - 20 * i))
(src / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)

# --- init / import ----------------------------------------------------------

code, out = run("init", "--dataset", "t", "--trigger", "@tstyle", "--kind", "style")
check(code == 0 and (ds_dir / "_pipeline" / "manifest.json").exists(), "init 建出 manifest")
manifest = json.loads((ds_dir / "_pipeline" / "manifest.json").read_text(encoding="utf-8"))
check(manifest.get("trigger") == "@tstyle", "manifest 记下触发词 @tstyle")

# --- enrich（离线只验契约：空集给指引 + 补标键必须是 rename_map 的 old_name）--

code, out = run("enrich", "--dataset", "t", "--apply")
check(code == 0 and "没有图片" in out, "enrich 空集给指引、退出 0（不联网、不炸）")

sys.path.insert(0, str(BUNDLE / "python"))
from animasl import enrich as _enrich          # noqa: E402
check(_enrich.lookup_key("0007.png", {"0007.png": {"old_name": "pixiv_a.png"}}) == "pixiv_a.png",
      "enrich 补标键取 rename_map 的 old_name（否则 wash 静默读不到）")
check(_enrich.lookup_key("pixiv_a.png", {}) == "pixiv_a.png",
      "enrich 还没 rename 时就用当前文件名")

# pixiv 兜底只认确定同页：md5 查不到时会用文件名里的 illust id 再查一次，
# 但多页作品（尺寸还都一样）绝不能按尺寸硬贴 —— 错标签比没标签更糟。
check(_enrich.pixiv_of("145960069_p2-标题.png") == ("145960069", 2), "从 pixiv 文件名解析 illust id 与页号")
check(_enrich.pixiv_of("145960069_p0.png") == ("145960069", 0), "p0 也要认（单帖作品的合法兜底）")
check(_enrich.pixiv_of("redash_a.png") is None, "普通文件名不给 pixiv id，不去瞎查")


class _P:
    """pick_pixiv 只用到 source 与 id，不需要真 Session。"""
    def __init__(self, pid, source=""):
        self.pid, self.source = pid, source

    def get(self, k):
        return {"id": self.pid, "source": self.source}.get(k)


_amb = [_P(12267680, "img/2026/09/20/12/02/06/149878653_p0.png"), _P(12267681)]
_ok, how = _enrich.pick_pixiv([_P(1, "https://i.pximg.net/img/2026/01/01/x/149878653_p1.png?foo=1")],
                              "149878653_p1.png", 1, None)
check(how == "pixiv-source", "原文件名与我们的 old_name 同名同页 -> 认（source 带域名和 query 也要剥干净）")
_ok, how = _enrich.pick_pixiv(_amb, "149878653_p1.png", 1, None)
check(_ok is None and how == "pixiv-ambiguous",
      "多帖候选 -> 拒绝（redash 实测：3 张不同页尺寸全一样，按尺寸贴就串页）")
_ok, how = _enrich.pick_pixiv([_P(9)], "149878653_p2.png", 2, None)
check(_ok is None, "单帖但不是 p0 -> 拒绝（那帖可能是别的页）")

code, out = run("import", "--dataset", "t", "--src", str(src))
raw = sorted((ds_dir / "00_raw").glob("*"))
check(code == 0 and len([p for p in raw if p.suffix == ".png"]) == 3, "import 收进 3 张图")
check("clip.mp4" in out and "非图" in out, "import 报告跳过非图片（不再静默黑洞）")

code, out = run("import", "--dataset", "t", "--src", str(ds_dir), expect=1)
check("数据集目录本身" in out or "拒绝执行" in out, "import 拒绝数据集根（src 红线）")

# --- rename（两批：只追加） --------------------------------------------------

code, out = run("rename", "--dataset", "t", "--start", "1", "--apply")
images = sorted(p for p in (ds_dir / "images").glob("*") if p.suffix == ".png")
check(code == 0 and len(images) == 3, "rename 重排出 3 张")
rmap = ds_dir / "_pipeline" / "rename_map.csv"
rows1 = rmap.read_text(encoding="utf-8").strip().splitlines()
check(len(rows1) == 4, f"rename_map.csv = 表头 + 3 行（实际 {len(rows1)} 行）")

state_file = ds_dir / "_pipeline" / "per_image.json"
check(state_file.exists(), "per_image.json 由插件原生写出（不必自己写脚本）")
state = json.loads(state_file.read_text(encoding="utf-8"))
check(len(state.get("images") or {}) == 3, "per_image.json 有 3 条记录")

(code, out) = run("thumbs", "--dataset", "t")
thumbs = list((ds_dir / "_pipeline" / "thumbs").glob("*.jpg"))
check(code == 0 and len(thumbs) == 3, f"thumbs 在 rename 之后仍出图（实际 {len(thumbs)} 张）")
code, out = run("thumbs", "--dataset", "t")
check("跳过" in out and "3" in out, "thumbs 第二次跳过已是最新的")

# 第二批：再导入 2 张并重排，rename_map 不能被覆盖
for name in ["d.png", "e.png"]:
    make_png(src / name, size=(80, 60), color=(10, 160, 90))
run("import", "--dataset", "t", "--src", str(src / "d.png"))
run("import", "--dataset", "t", "--src", str(src / "e.png"))
code, out = run("rename", "--dataset", "t", "--start", "4", "--apply")
rows2 = rmap.read_text(encoding="utf-8").strip().splitlines()
check(len(rows2) == 6, f"再跑一批 rename_map.csv 追加到 6 行（实际 {len(rows2)} 行）")
check(any(p.name.startswith("rename_map.0004") for p in (ds_dir / "_pipeline").glob("rename_map.*.csv")),
      "留了本批快照 rename_map.<first>-<last>.csv")

# --- wash：图源标签 + 防复活 -------------------------------------------------

# 冒充一次 booru 抓取：给 0001 一条带 tags 的元数据（wash 的来源 A）
posts = ds_dir / "_pipeline" / "raw_posts.jsonl"
rows = [r.split(",") for r in rmap.read_text(encoding="utf-8").strip().splitlines()]
header = rows[0]
first_row = next(r for r in rows[1:] if r[header.index("new_name")] == images[0].name)
first_old = first_row[header.index("old_name")]
meta = {"filename": first_old, "old_name": first_old, "post_id": "123", "source": "danbooru",
        "tags": ["1girl", "solo", "long_hair", "blue_eyes"], "page_url": "https://example.invalid/123"}
with posts.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(meta, ensure_ascii=False) + "\n")

code, out = run("wash", "--dataset", "t", "--trigger", "@tstyle", "--apply")
txt = ds_dir / "images" / "0001.txt"
body = txt.read_text(encoding="utf-8") if txt.exists() else ""
check(code == 0 and body.startswith("@tstyle"), "wash 把触发词写在首位")
check("long hair" in body and "blue eyes" in body, "wash 并入了图源标签（并规范成空格形）")

# 人工删掉一个图源标签，重洗不该复活它
txt.write_text(body.replace("long hair, ", "").replace(", long hair", ""), encoding="utf-8", newline="\n")
run("wash", "--dataset", "t", "--trigger", "@tstyle", "--apply")
body2 = txt.read_text(encoding="utf-8")
check("long hair" not in body2, "重跑 wash 不复活人工删掉的标签（守住了）")

run("wash", "--dataset", "t", "--trigger", "@tstyle", "--refresh-source", "--apply")
body3 = txt.read_text(encoding="utf-8")
check("long hair" in body3, "--refresh-source 才按图源整体重洗")

# --- fix-caption：增量修一张图 ------------------------------------------------

code, out = run("fix-caption", "--dataset", "t", "--name", "0001", "--remove", "blue eyes", "--apply")
check(code == 0 and "blue eyes" not in txt.read_text(encoding="utf-8"), "fix-caption --remove 生效")
run("wash", "--dataset", "t", "--trigger", "@tstyle", "--apply")
check("blue eyes" not in txt.read_text(encoding="utf-8"), "fix-caption 删掉的标签重洗也不复活")

code, out = run("fix-caption", "--dataset", "t", "--name", "0001", "--add", "smile", "--apply")
check(code == 0 and "smile" in txt.read_text(encoding="utf-8"), "fix-caption --add 生效")

code, out = run("fix-caption", "--dataset", "t", "--name", "0001", "--set", "@tstyle, 1girl, solo", "--apply")
check(txt.read_text(encoding="utf-8").strip() == "@tstyle, 1girl, solo", "fix-caption --set 整条替换")

code, out = run("fix-caption", "--dataset", "t", "--name", "9999", "--add", "x", "--apply", expect=1)
check("找不到" in out or "没有" in out, "fix-caption 对不存在的图报错而不是静默")

# --- 图桶 + 冲突族 + 快照/差异 + exclude（小叶子miv 复盘那批修复）-----------

import sys as _sys                                    # noqa: E402
_sys.path.insert(0, str(BUNDLE / "python"))
from animasl import config as _config, state as _state, makecfg as _makecfg, wash as _wash  # noqa: E402

check(_wash.normalize_tag(":|") == ":|" and _wash.normalize_tag("@_@") == "@_@"
      and _wash.normalize_tag("o_o") == "o_o" and _wash.normalize_tag(">_<") == ">_<",
      "标点/表情标签不被洗坏（:| @_@ o_o >_<）")
check(_wash.normalize_tag("smile,") == "smile" and _wash.normalize_tag("seifuku") == "seifuku",
      "普通标签照旧去尾标点、下划线归一成空格")
check("conflict-hair-color" in " ".join(_wash.conflict_issues(["pink hair", "purple hair"])),
      "发色冲突被检出")
check("conflict-underwear" in " ".join(_wash.conflict_issues(["panties", "no panties"])),
      "下着冲突被检出")
check(_wash.conflict_issues(["1girl", "1boy"]) == [], "1girl + 1boy 不算冲突")
check("count-conflict" in " ".join(_wash.conflict_issues(["solo", "1boy"])), "solo 与多人互斥被检出")

bk = home / "datasets" / "bk"
bk.mkdir(parents=True, exist_ok=True)
run("init", "--dataset", "bk", "--trigger", "@bk", "--kind", "style")
(bk / "images").mkdir(exist_ok=True)                  # 空的 images/ 不该被当成图桶
for bucket, name, color, body in [("clean", "0001.png", (10, 90, 160), "@bk, 1girl, solo, smile"),
                                  ("watermark", "0002.png", (200, 40, 40), "@bk, 1boy, watermark, signature")]:
    d = bk / bucket
    d.mkdir(exist_ok=True)
    make_png(d / name, color=color)
    (d / name[:-4]).with_suffix(".txt").write_text(body, encoding="utf-8", newline="\n")
# latest/ 做成 clean 的硬链接副本：复盘痛点 5（makecfg 曾把 142 张算成 284 张）
(bk / "latest").mkdir(exist_ok=True)
hardlinked = True
try:
    os.link(bk / "clean" / "0001.png", bk / "latest" / "0001.png")
    os.link(bk / "clean" / "0001.txt", bk / "latest" / "0001.txt")
except OSError:
    hardlinked = False

cfg = _config.Config.load(home=str(home))   # 别吃默认 home，否则 Dataset 指向别的数据根
bds = _config.Dataset("bk", cfg)
labels = [label for label, _ in bds.bucket_dirs()]
check(labels == ["clean", "latest", "watermark"], f"图桶自动发现（实际 {labels}）")
check("images" not in labels, "空 images/ 不算图桶")
check(len(bds.work_images()) == 3, f"work_images 收齐三个桶里的图（实际 {len(bds.work_images())}）")

code, out = run("wash", "--dataset", "bk", "--apply")
check("clean" in out and "watermark" in out, "wash 报出处理的图桶")
check("标签来源" in out and "A=none" in out, "wash 体检标签来源（A=none 要显眼）")
check("enrich" in out or "fetch" in out, "A=none 占多数时给可执行修法（enrich / fetch）")
c1 = bk / "clean" / "0001.txt"
check(c1.read_text(encoding="utf-8").startswith("@bk"), "桶里的 caption 被就地重写（不再只认 images/）")
check(len(list((bk / "_pipeline" / "captions_prev").glob("*"))) >= 1, "wash 写盘前留了 caption 快照")

# 人工改 caption（删掉 smile）后重跑：不该复活，且要出本次 vs 上次的差异表
c1.write_text("@bk, 1girl, solo", encoding="utf-8", newline="\n")
run("wash", "--dataset", "bk", "--apply")
body = c1.read_text(encoding="utf-8")
check("smile" not in body, "桶布局下防复活照旧生效")
diff_file = bk / "_pipeline" / "wash_diff.csv"
check(diff_file.exists(), "wash 写盘后产出 wash_diff.csv（本次 vs 上次）")
if diff_file.exists():
    check("0001" in diff_file.read_text(encoding="utf-8"), "wash_diff.csv 里记着被改的那张")

subs = _makecfg.collect_subsets(bds, None)
total = sum(s["count"] for s in subs)
check(total == 2, f"makecfg 跨桶按内容指纹去重（硬链接不算两次，实际 {total}）")
if hardlinked:
    check("latest" not in [s["name"] for s in subs], "整桶都是副本的桶被跳过并说明")

code, out = run("exclude", "--dataset", "bk", "--names", "0002", "--reason", "user", "--apply")
check(code == 0 and (bk / "_excluded" / "user" / "0002.png").exists(), "exclude 把图移进 _excluded/<reason>")
check(any("0002" in n for n in _state.user_deleted_names(bds)), "exclude 记进 user_deleted.json")
code, out = run("exclude", "--dataset", "bk", "--undo", "--apply")
check(code == 0 and (bk / "watermark" / "0002.png").exists(), "exclude --undo 按账本搬回来")
(bk / "clean" / "0001.png").unlink()
(bk / "latest" / "0001.png").unlink()
code, out = run("exclude", "--dataset", "bk", "--missing", "--apply")
check(code == 0 and any("0001" in n for n in _state.user_deleted_names(bds)),
      "exclude --missing 把「账本里有、图桶里没有」的记成人工删除")

# --- verify / dict-check ----------------------------------------------------

code, out = run("verify", "--dataset", "t")
check(code == 0 and "caption" in out, "verify 跑通")
check("健康度" in out or "缺 txt" in out or "低于下限" in out or "标签数" in out,
      "verify 顺带报 caption 健康度（图-txt 配对/标签数）")
check("触发词" in out, "verify 检查触发词位置")

code, out = run("dict-check", "--dataset", "t")
check(code == 0 and "存在" in out and "词典里没有" in out, "dict-check 分类报告数据集里的标签")
code, out = run("dict-check", "--tags", "1girl,zzz_not_a_real_tag")
check("1girl" in out and "zzz_not_a_real_tag" in out, "dict-check --tags 不依赖数据集")
check("词典里没有" in out, "dict-check 点出编造的标签")

# --- makecfg：LR 不再静默收紧 -------------------------------------------------

trainer = Path(os.environ.get("ANIMASL_TEST_TRAINER", "E:/LoRA_Train/Anima-Standalone-Trainer"))
if (trainer / "anima_train_network.py").exists():
    code, out = run("makecfg", "--dataset", "t", "--dim", "16", "--lr", "5e-05", "--apply", expect=1)
    check("建议区间" in out and "--allow-out-of-band" in out, "makecfg 对超区间 LR 报错（不再静默收紧）")
    code, out = run("makecfg", "--dataset", "t", "--dim", "16", "--lr", "5e-05",
                    "--allow-out-of-band", "--apply")
    cfg_dir = home / "train_configs" / "t"
    pre = cfg_dir / "preflight.txt"
    check(code == 0 and pre.exists(), "makecfg --allow-out-of-band 照写并产出 preflight.txt")
    if pre.exists():
        text = pre.read_text(encoding="utf-8")
        check("LR" in text and "rank" in text, "preflight 写清 rank/LR 的推导")
        check("caption" in text and "词典" in text, "preflight 带 caption 合规率与词典外标签")
        check("与上一版配置的差异" not in text, "首版 preflight 不写差异段（没有上一版）")
        code, out = run("makecfg", "--dataset", "t", "--dim", "32", "--force", "--apply")
        text2 = pre.read_text(encoding="utf-8") if pre.exists() else ""
        check(code == 0 and "network_dim" in text2, "第二次 makecfg 的 preflight 列出与上一版的差异")
else:
    print("  skip  makecfg（找不到训练器 anima_train_network.py）")

# --- 收尾 -------------------------------------------------------------------

if KEEP:
    print(f"[pipeline-offline] --keep：现场留在 {base}")
else:
    resolved = base.resolve()
    if resolved.is_dir() and resolved.name.startswith("_pipeoff_") and str(resolved).startswith(str(HOME_ROOT.resolve())):
        shutil.rmtree(resolved, ignore_errors=True)

print(f"\n[pipeline-offline] {passed} 项通过"
      + (f"，{len(errors)} 项失败:\n- " + "\n- ".join(errors) if errors else "，ALL OK"))
sys.exit(1 if errors else 0)
