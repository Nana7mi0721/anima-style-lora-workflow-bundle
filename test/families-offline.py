"""families 阶段的离线自测：合成图造临时数据集，验证四步语义与落盘行为。

覆盖：
  * 声明家族：yande 的 parent 树 / pawchive 的单个帖子 / 没有元数据的孤儿
  * 跨源视觉合并：两个声明家族里各有一张「同一幅画」，corr >= 0.85 应合成一族
  * 同图去重：同族内同一幅画的 PNG / JPEG 两种编码，保留体积最大的那份
  * 按族建桶：diff<k>，repeats = round(base/sqrt(k))
  * dry-run 不动文件、报告照写；apply 之后文件真的换目录、.txt 跟着走
  * rebuild=False 时图集合没变就复用上一次的划分

用法：  python test/families-offline.py
临时数据集落在 ANIMASL_TEST_HOME（默认 E:/LoRA_Train）的 datasets/ 下，跑完删除；
ANIMASL_TEST_KEEP=1 保留现场。
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

try:                                   # 控制台可能是 GBK，中文断言输出别乱码
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BUNDLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE / "python"))

from animasl import config, net  # noqa: E402
from animasl.curate import _ssim_arr  # noqa: E402
from animasl.families import _local_diff, _signature, cmd_families  # noqa: E402

HOME_ROOT = Path(os.environ.get("ANIMASL_TEST_HOME", "E:/LoRA_Train"))
KEEP = os.environ.get("ANIMASL_TEST_KEEP") == "1"

checks = 0
errors: list[str] = []


def check(ok: bool, msg: str) -> None:
    global checks
    checks += 1
    print(("  ok   " if ok else "  FAIL ") + msg)
    if not ok:
        errors.append(msg)


# --------------------------------------------------------------------------
# 合成图：低频噪声底图 + 局部换块（= 同一幅画的差分），JPEG 重存后 SSIM 仍近 1
# --------------------------------------------------------------------------
def smooth(seed: int, blur: int = 9) -> np.ndarray:
    rng = np.random.default_rng(seed)
    big = rng.normal(128.0, 30.0, (256 + blur, 256 + blur))
    out = np.zeros((256, 256))
    for dy in range(blur):
        for dx in range(blur):
            out += big[dy:dy + 256, dx:dx + 256]
    return np.clip(out / blur / blur, 0, 255).astype(np.uint8)


def patch(base: np.ndarray, block: tuple[int, int, int, int], seed: int) -> np.ndarray:
    """把一小块换成另一片同分布噪声：视觉上还是那张画，但不是同一张图。"""
    arr = base.copy()
    x, y, w, h = block
    arr[y:y + h, x:x + w] = smooth(seed)[y:y + h, x:x + w]
    return arr


def build(ds) -> list[str]:
    """造 7 张图：yande 族 3 张 + pawchive 族 3 张（其中一张与 yande 族同画）+ 孤儿。"""
    ds.ensure_dirs()
    images = ds.images_dir
    base_a, base_b = smooth(11), smooth(22)
    files = {
        "a1.png": patch(base_a, (0, 0, 48, 48), 1),
        "a2.png": patch(base_a, (100, 0, 48, 48), 2),
        "a3.png": patch(base_a, (200, 0, 48, 48), 3),
        "b1.png": base_b,
        "b3.png": patch(base_a, (0, 200, 48, 48), 4),
        "o1.png": smooth(33),
    }
    for name, arr in files.items():
        Image.fromarray(arr).save(images / name)
    # b1 的「另一种编码」：同一份像素存成 JPEG，体积更小 -> 去重时落选
    Image.fromarray(base_b).save(images / "b2.jpg", quality=98)
    # 每张图都配一个 .txt：验证搬桶时 caption 跟着走
    for name in list(files) + ["b2.jpg"]:
        (images / Path(name).with_suffix(".txt")).write_text("1girl, solo, style", encoding="utf-8")

    rows = [
        {"source": "yandere", "post_id": 100, "filename": "a1.png", "parent_id": None,
         "tags": ["1girl"]},
        {"source": "yandere", "post_id": 101, "filename": "a2.png", "parent_id": 100},
        {"source": "yandere", "post_id": 102, "filename": "a3.png", "parent_id": 101},
        {"source": "pawchive", "post_id": 900, "filename": "b1.png"},
        {"source": "pawchive", "post_id": 900, "filename": "b2.jpg"},
        {"source": "pawchive", "post_id": 900, "filename": "b3.png"},
        # o1.png 故意没有元数据 -> 自成一族
    ]
    net.write_jsonl(ds.pipe_dir / "raw_posts.jsonl", rows)
    return sorted(p.name for p in images.iterdir() if p.is_file())


def names(path: Path) -> list[str]:
    return sorted(p.name for p in path.iterdir() if p.is_file()) if path.exists() else []


def scene(seed: int = 3, size: int = 512) -> Image.Image:
    """有低频结构的合成画：渐变 + 三个圆。只改一小块时**全图 SSIM 几乎不动**，
    这正好用来钉住「SSIM 过了也不能当同一张」的那道局部差闸。"""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    base = (110 + 60 * np.sin(xx / 47.0) * np.cos(yy / 61.0)
            + 40 * np.sin((xx + yy) / 23.0))
    base = base + rng.normal(0, 6, (size, size))
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(img)
    for cx, cy, r in [(140, 160, 70), (330, 300, 90), (420, 120, 50)]:
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=40)
    return img


def guard_case() -> None:
    """SSIM >= 0.995 但局部被改过一块：必须当成两张（否则真差分会被删）。"""
    print("\n[局部差闸门]")
    root = HOME_ROOT if (HOME_ROOT / "datasets").is_dir() else Path(tempfile.mkdtemp(
        prefix="_famhome_"))
    base = Path(tempfile.mkdtemp(prefix="_famguard_", dir=root / "datasets"))
    ds = config.Config.load(home=str(root)).dataset(base.name)
    try:
        ds.ensure_dirs()
        img = scene()
        img.save(ds.images_dir / "g1.png")
        edited = img.copy()
        ImageDraw.Draw(edited).rectangle([200, 220, 216, 236], fill=0)   # 16x16 黑块
        edited.save(ds.images_dir / "g2.png")
        (ds.images_dir / "g1.txt").write_text("1girl, solo", encoding="utf-8")
        net.write_jsonl(ds.pipe_dir / "raw_posts.jsonl", [
            {"source": "yandere", "post_id": 700, "filename": "g1.png"},
            {"source": "pawchive", "post_id": 800, "filename": "g2.png"},
        ])

        sig1, sig2 = _signature(ds.images_dir / "g1.png"), _signature(ds.images_dir / "g2.png")
        score = _ssim_arr(sig1[1], sig2[1])
        _mean, _maxd, block = _local_diff(sig1[1], sig2[1])
        check(score >= 0.995, f"这个用例的全图 SSIM 确实 >= 0.995（{score:.5f}）")
        check(block > 8.0, f"但 8x8 分块差很大（{block:.1f} > 8）")

        res = cmd_families(ds)
        check(res["families"] == 1, f"跨源 corr 该把它们并成一族（{res['families']}）")
        check(res["duplicates"] == 0, f"局部改过的不能判同图（duplicates={res['duplicates']}）")
        check(res["images_after"] == 2, f"两张都留着（{res['images_after']}）")
        check(res["buckets"] == {"diff2": 1}, f"桶 diff2（{res['buckets']}）")
        check(res["repeats"] == {"diff2": 4},
              f"repeats：round(6/sqrt(2))=4（{res['repeats']}）")

        cmd_families(ds, apply=True)
        check(names(ds.root / "diff2") == ["g1.png", "g1.txt", "g2.png"],
              f"apply 后两张都进 diff2（{names(ds.root / 'diff2')}）")
        check(not (ds.excluded_dir / "duplicates-family").exists(),
              "没有图被当重复删掉")
    finally:
        if KEEP:
            print(f"[families-offline] 保留现场 {ds.root}")
        else:
            shutil.rmtree(ds.root, ignore_errors=True)


def main() -> int:
    root = HOME_ROOT
    if not (root / "datasets").is_dir():
        root = Path(tempfile.mkdtemp(prefix="_famhome_"))
        (root / "datasets").mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="_famoff_", dir=root / "datasets"))
    ds = config.Config.load(home=str(root)).dataset(base.name)
    print(f"[families-offline] 临时数据集 {ds.root}")
    try:
        before = build(ds)
        check(len(before) == 14, f"造图：7 张图 + 7 个 .txt（实际 {len(before)} 个文件）")

        # -- 1) dry-run ----------------------------------------------------
        print("\n[dry-run]")
        res = cmd_families(ds)
        check(res["applied"] is False, "dry-run 的 applied 为 False")
        check(res["images_before"] == 7, f"images_before == 7（{res['images_before']}）")
        check(res["families"] == 2, f"家族数 == 2（{res['families']}）")
        check(res["duplicates"] == 1, f"同图去重 == 1（{res['duplicates']}）")
        check(res["images_after"] == 6, f"images_after == 6（{res['images_after']}）")
        check(res["buckets"] == {"diff5": 1, "diff1": 1},
              f"桶 == diff5 x1 / diff1 x1（{res['buckets']}）")
        check(res["repeats"] == {"diff5": 3, "diff1": 6},
              f"repeats：round(6/sqrt(5))=3、k=1 时 6（{res['repeats']}）")
        check(names(ds.images_dir) == before, "dry-run 没动 images/ 里的任何文件")
        check(not any(p.name.startswith("diff") for p in ds.root.iterdir()
                      if p.is_dir()), "dry-run 没建 diff<k> 桶")

        report = ds.pipe_dir / "families_report.csv"
        store_path = ds.pipe_dir / "families.json"
        check(report.exists(), "dry-run 也写了 families_report.csv")
        check(store_path.exists(), "dry-run 也写了 families.json")
        with open(report, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        check(len(rows) == 2, f"报告 2 行（{len(rows)}）")
        check(list(rows[0]) == ["family", "declared_sources", "size_before", "size_after",
                                "bucket", "repeats", "members"],
              f"报告列名（{list(rows[0])}）")
        store = json.loads(store_path.read_text(encoding="utf-8"))
        check(store["totals"] == {"families": 2, "duplicates": 1},
              f"families.json totals（{store['totals']}）")
        check(len(store["image_set"]) == 6, f"image_set 记 6 张保留图（{len(store['image_set'])}）")

        merged = [f for f in store["families"] if f["size_before"] == 6]
        check(len(merged) == 1, "跨源合并后的家族有 6 个成员")
        fam = merged[0]
        check(fam["declared"] == ["pawchive:900", "y:100"],
              f"合并依据记下两个声明源（{fam['declared']}）")
        check(fam["merged_pairs"] >= 1 and fam["merged_by"],
              f"families.json 记了跨源合并证据 {fam['merged_pairs']} 对")
        check(fam["bucket"] == "diff5" and fam["repeats"] == 3,
              f"合并族 -> diff5 repeats 3（{fam['bucket']}/{fam['repeats']}）")
        dup = [m for m in fam["members"] if m["role"] == "dup"]
        check(len(dup) == 1 and dup[0]["path"] == "images/b2.jpg" and dup[0]["of"] == "b1.png",
              f"落选的是 b2.jpg（保留了体积更大的 b1.png）：{dup}")
        check(dup and dup[0]["ssim"] >= 0.995, f"落选者 SSIM 证据（{dup and dup[0]['ssim']}）")

        lone = [f for f in store["families"] if f["size_before"] == 1]
        check(len(lone) == 1 and lone[0]["declared"] == ["single:o1.png"],
              f"孤儿自成一族（{lone and lone[0]['declared']}）")
        check(lone and lone[0]["bucket"] == "diff1" and lone[0]["repeats"] == 6,
              "孤儿 -> diff1、repeats 取 base 6")

        # -- 2) apply ------------------------------------------------------
        print("\n[apply]")
        res2 = cmd_families(ds, apply=True)
        check(res2["applied"] is True, "apply 的 applied 为 True")
        check(res2["families"] == 2 and res2["duplicates"] == 1, "apply 与 dry-run 结论一致")
        check(names(ds.root / "diff5") == sorted(
            ["a1.png", "a1.txt", "a2.png", "a2.txt", "a3.png", "a3.txt",
             "b1.png", "b1.txt", "b3.png", "b3.txt"]),
              f"diff5/ 里 5 张图 + 5 个 .txt（{names(ds.root / 'diff5')}）")
        check(names(ds.root / "diff1") == ["o1.png", "o1.txt"],
              f"diff1/ 里孤儿 + .txt（{names(ds.root / 'diff1')}）")
        check(names(ds.excluded_dir / "duplicates-family") == ["b2.jpg", "b2.txt"],
              f"落选者连同 .txt 进了 _excluded/duplicates-family/"
              f"（{names(ds.excluded_dir / 'duplicates-family')}）")
        check(names(ds.images_dir) == [], "images/ 已经搬空")
        manifest = ds.excluded_dir / "_EXCLUDED_MANIFEST.json"
        check(manifest.exists(), "落选者记进了 _excluded/_EXCLUDED_MANIFEST.json")

        # -- 3) 复用（图集合没变 + 默认 rebuild=False） ---------------------
        print("\n[reuse]")
        res3 = cmd_families(ds)
        check(res3["families"] == 2 and res3["images_before"] == 6,
              f"复用：家族 2、工作集 6 张（{res3['families']}/{res3['images_before']}）")
        check(res3["duplicates"] == 0, "复用时被搬走的重复图不再计入（0）")
        check(res3["buckets"] == {"diff5": 1, "diff1": 1}, f"复用的桶划分（{res3['buckets']}）")
        check(names(ds.root / "diff5") == sorted(
            ["a1.png", "a1.txt", "a2.png", "a2.txt", "a3.png", "a3.txt",
             "b1.png", "b1.txt", "b3.png", "b3.txt"]),
              "复用不会重复搬文件")

        # -- 4) 幂等 apply + rebuild 强制重算 -------------------------------
        print("\n[apply 幂等 / rebuild]")
        res4 = cmd_families(ds, apply=True)
        check(res4["images_after"] == 6 and names(ds.excluded_dir / "duplicates-family")
              == ["b2.jpg", "b2.txt"], "重复 apply 不炸、文件数不变")
        res5 = cmd_families(ds, rebuild=True)
        check(res5["families"] == 2 and res5["images_after"] == 6,
              f"rebuild=True 重算仍是 2 族 6 张（{res5['families']}/{res5['images_after']}）")
    finally:
        if KEEP:
            print(f"\n[familes-offline] 保留现场 {ds.root}")
        else:
            shutil.rmtree(ds.root, ignore_errors=True)

    guard_case()

    print(f"\n[families-offline] {checks - len(errors)}/{checks} 项通过")
    if errors:
        print("[families-offline] 失败项：")
        for e in errors:
            print("  - " + e)
        return 1
    print("[families-offline] ALL OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
