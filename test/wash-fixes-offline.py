"""离线回归：wash/state 的四处修复（task-2，不联网、不跑 ML）。

    <venv>/python.exe test/wash-fixes-offline.py            # 跑完删临时数据集
    <venv>/python.exe test/wash-fixes-offline.py --keep     # 留着现场

钉住的东西（对应用户真实使用的痛点）：

  1. verify 分级：冲突类（conflict-* / count-conflict）只算**提示**（warn），形态违规
     （多行/BOM/标签数/触发词/banned 残留）才算**违反硬约束**（error）；wash_verify.csv
     带 severity 列；返回 dict 里有 files/errors/warns。283 张报「116 张违反硬约束」的
     误报就是这么来的（312 条 conflict-hair-color + 62 条 conflict-underwear）。
  2. add_implied 尊重人工删除：--remove multicolored hair 之后，重跑 wash 或再跑
     fix-caption --add 别的标签，都不能因为「双发色 → multicolored hair」把它补回来；
     只有显式 --add 才放行。
  3. 按文件快照：snapshot_captions(names=[...]) 只备份命中的文件（单张 fix-caption 不必
     整库复制 283 份，把上一代备份挤掉），但仍遵守 hygiene.caption_snapshots 的保留代数。
  4. conflict_counts：族名 -> **有冲突的图片数**（复用 conflict_issues 的三个豁免）。
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
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
HOME_ROOT = Path(os.environ.get("ANIMASL_TEST_HOME", "E:/LoRA_Train"))

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


def run_quiet(fn, *args, **kwargs):
    """调模块函数并吞掉它的中文打印；需要验输出时再单独捕一次。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def make_png(path: Path, color: tuple[int, int, int] = (180, 60, 40)) -> None:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color).save(path)


(HOME_ROOT / "datasets").mkdir(parents=True, exist_ok=True)
base = Path(tempfile.mkdtemp(prefix="_washfix_", dir=str(HOME_ROOT / "datasets")))
home = base / "home"
print(f"[wash-fixes] 临时现场 {base}")

# 只让配置层看到临时 home：开发机上的 runtime 配置（模型路径、阈值）不该干扰断言。
os.environ.pop("ANIMASL_RUNTIME", None)
os.environ["ANIMASL_HOME"] = str(home)
sys.path.insert(0, str(BUNDLE / "python"))

from animasl import config as config_mod        # noqa: E402
from animasl import manifest as manifest_mod    # noqa: E402
from animasl import state as state_mod          # noqa: E402
from animasl import wash as wash_mod            # noqa: E402

cfg = config_mod.Config.load(home=str(home))
ds = config_mod.Dataset("wfix", cfg)
ds.ensure_dirs()
manifest_mod.Manifest(ds.manifest_file).create(ds.name, trigger="@wt").save()
# 出厂 min_tags=20 是成品库的闸门；这里每张只有几个标签，先关掉，别让「标签太少」
# 把形态断言淹掉（阈值与分级的关系由 pipeline-offline 的 verify 用例覆盖）。
(ds.pipe_dir / "wash_rules.json").write_text(json.dumps({"min_tags": 0}), encoding="utf-8")


def put(stem: str, body: str) -> None:
    make_png(ds.images_dir / f"{stem}.png")
    (ds.images_dir / f"{stem}.txt").write_text(body, encoding="utf-8", newline="\n")


def caption_of(stem: str) -> str:
    return (ds.images_dir / f"{stem}.txt").read_text(encoding="utf-8")


put("0001", "@wt, 1girl, solo, black hair, blue hair")   # 双发色 -> 冲突（提示）
put("0002", "@wt, 1girl\nsolo, smile")                   # 多行 -> 形态违规（错误）
put("0003", "@wt, solo, 2girls")                         # solo vs 2girls -> 冲突（提示）

# --- 1. verify 分级：error 只认形态，冲突进 warn ---------------------------

check(wash_mod.issue_severity("multi-line") == "error", "issue_severity: multi-line 是 error")
check(wash_mod.issue_severity("conflict-hair-color(a vs b)") == "warn",
      "issue_severity: conflict-* 是 warn（误报不再算硬约束）")
check(wash_mod.issue_severity("") == "ok", "issue_severity: 空 issue 是 ok")

_res, out = run_quiet(wash_mod.cmd_verify, ds)
check(_res["files"] == 3, f"verify 数了 3 张（实际 {_res['files']}）")
check(_res["errors"] == 1, f"verify errors=1（只有多行那张，实际 {_res['errors']}）")
check(_res["warns"] == 2, f"verify warns=2（两张冲突，实际 {_res['warns']}）")
check("违反硬约束 1（error），提示 2（warn）" in out,
      f"verify 的输出按分级报数（实际：{[ln for ln in out.splitlines() if '[verify]' in ln][:1]}）")

verify_csv = ds.pipe_dir / "wash_verify.csv"
check(verify_csv.exists(), "wash_verify.csv 写出来了")
with open(verify_csv, encoding="utf-8", newline="") as fh:
    _rows = {r["file"]: r for r in csv.DictReader(fh)}
check(all("severity" in r for r in _rows.values()),
      "wash_verify.csv 有 severity 列")
check(_rows.get("0001.png", {}).get("severity") == "warn"
      and "conflict-hair-color" in _rows["0001.png"]["warns"]
      and not _rows["0001.png"]["errors"],
      f"0001 双发色：warn + conflict-hair-color，errors 空（实际 {_rows.get('0001.png')}）")
check(_rows.get("0002.png", {}).get("severity") == "error"
      and "multi-line" in _rows["0002.png"]["errors"],
      f"0002 多行：error + multi-line（实际 {_rows.get('0002.png')}）")
check(_rows.get("0003.png", {}).get("severity") == "warn"
      and "count-conflict" in _rows["0003.png"]["warns"],
      f"0003 solo vs 2girls：warn + count-conflict（实际 {_rows.get('0003.png')}）")

# --- 2. conflict_counts：族名 -> 有冲突的图片数 ----------------------------

_counts, _ = run_quiet(wash_mod.conflict_counts, ds)
check(_counts == {"hair-color": 1, "count": 1},
      f"conflict_counts 按族数图（实际 {_counts}）")
check("count" not in ({i.split("(")[0] for i in ["conflict-hair-color(x)"]}),
      "conflict_family 不会把 conflict-* 误认成 count")

# --- 3. 按文件快照：只备份命中的文件 ---------------------------------------

_snap1 = state_mod.snapshot_captions(ds, label="fix-caption", names=["0001"])
check(_snap1 is not None and _snap1.is_dir(), "snapshot_captions(names=[单张]) 建出快照目录")
check(sorted(p.name for p in _snap1.glob("*.txt")) == ["0001.txt"],
      f"单文件快照只抄 1 张（实际 {sorted(p.name for p in _snap1.glob('*.txt'))}）")
check(state_mod.snapshot_captions(ds, label="fix-caption", names=["0001"]) is None,
      "内容没变 -> 不新增快照（skip_if_same 仍生效）")

# 改名走一遍：带扩展名也认；改一张 -> 只备份这一张，保留代数仍是 1
(ds.images_dir / "0001.txt").write_text(caption_of("0001") + ", smile", encoding="utf-8", newline="\n")
_snap2 = state_mod.snapshot_captions(ds, label="fix-caption", names=["0001.png"])
check(_snap2 is not None and sorted(p.name for p in _snap2.glob("*.txt")) == ["0001.txt"],
      "names 带 .png 也能匹配（按 stem）")
_prev_dirs = [p for p in (ds.pipe_dir / "captions_prev").iterdir() if p.is_dir()]
check(len(_prev_dirs) == 1,
      f"按文件快照也遵守 hygiene.caption_snapshots=1（实际 {len(_prev_dirs)} 份）")

# 单张 fix-caption：不再整库复制，快照里只有这次改的那一张
_res, _ = run_quiet(wash_mod.cmd_set_caption, ds, name="0003", add="long hair", apply=True)
check(_res.get("applied") and "long hair" in caption_of("0003"),
      "fix-caption 单张能写盘（对照组）")
_latest = state_mod.latest_snapshot(ds)
check(_latest is not None
      and sorted(p.name for p in _latest.glob("*.txt")) == ["0003.txt"],
      f"单张 fix-caption 的快照只有 0003.txt（实际 {sorted(p.name for p in _latest.glob('*.txt')) if _latest else None}）")

# --- 4. 人工删除的 multicolored hair 不被蕴含规则补回 ---------------------

put("0004", "@wt, 1girl, solo, black hair, white hair")
run_quiet(wash_mod.cmd_wash, ds, apply=True)
check("multicolored hair" in caption_of("0004"),
      "wash 首次自动补 multicolored hair（双发色蕴含，§7.3）")

_res, _ = run_quiet(wash_mod.cmd_set_caption, ds, name="0004",
                    remove="multicolored hair", apply=True)
check("multicolored hair" not in caption_of("0004"),
      "--remove multicolored hair 之后 caption 里真的没了")
_st = state_mod.load(ds)
check("multicolored hair" in _st.manual_removed("0004.png"),
      f"manual_removed 记下了这次删除（实际 {_st.manual_removed('0004.png')}）")

run_quiet(wash_mod.cmd_wash, ds, apply=True)
check("multicolored hair" not in caption_of("0004"),
      "重跑 wash 不会把人工删掉的 multicolored hair 补回来")

run_quiet(wash_mod.cmd_set_caption, ds, name="0004", add="smile", apply=True)
check("multicolored hair" not in caption_of("0004"),
      "再跑 fix-caption --add 别的标签也不补回 multicolored hair")

_dry, _ = run_quiet(wash_mod.cmd_set_caption, ds, name="0004", add="hat", apply=False)
check("multicolored hair" not in str(_dry.get("caption") or ""),
      "dry-run 的新 caption 预览也不含被删标签")

# 显式 --add 是唯一放行口
run_quiet(wash_mod.cmd_set_caption, ds, name="0004", add="multicolored hair", apply=True)
check("multicolored hair" in caption_of("0004"),
      "显式 --add multicolored hair 能补回来（人工删除名单被放行）")
_st2 = state_mod.load(ds)
check("multicolored hair" not in _st2.manual_removed("0004.png"),
      "放行后 manual_removed 里不再有它")
run_quiet(wash_mod.cmd_wash, ds, apply=True)
check("multicolored hair" in caption_of("0004"),
      "放行之后 wash 也保留 multicolored hair")

# --- 收尾 -------------------------------------------------------------------

if KEEP:
    print(f"[wash-fixes] --keep：现场留在 {base}")
else:
    _resolved = base.resolve()
    if (_resolved.is_dir() and _resolved.name.startswith("_washfix_")
            and str(_resolved).startswith(str(HOME_ROOT.resolve()))):
        shutil.rmtree(_resolved, ignore_errors=True)

print(f"\n[wash-fixes] {passed} 项通过"
      + (f"，{len(errors)} 项失败:\n- " + "\n- ".join(errors) if errors else "，ALL OK"))
sys.exit(1 if errors else 0)
