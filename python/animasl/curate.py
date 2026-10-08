"""Curation stages: import, dedup, screen, thumbs, rename.

Working directories
    00_raw/     everything that was downloaded or imported
    images/     the curated, renumbered training set (after `rename`)
    _excluded/  rejects, kept as evidence (one subfolder per reason)
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import net, state

IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz"}

# 数据集自己的保留目录：import 不会递归进去（它们是流水线的产物，不是素材）
RESERVED_DIRS = {"00_raw", "images", "_pipeline", "_excluded", "thumbs", "masks"}

try:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = None
except Exception as exc:  # pragma: no cover
    raise SystemExit("[animasl] pillow is required for the curation stages") from exc

try:
    import numpy as np
except Exception as exc:  # pragma: no cover
    raise SystemExit("[animasl] numpy is required for the curation stages") from exc


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def working_dir(ds, manifest=None, prefer_images: bool = True) -> Path:
    """成品图所在的主要目录；还没有成品桶时回落到 00_raw（rename 之前）。

    成品图可以住在 images/、clean/、watermark/ 任意一个桶里（见 Dataset.bucket_dirs），
    这里只回答「主要的那个在哪」；要枚举全部图请用 `ds.work_images()`。
    """
    buckets = ds.bucket_dirs() if prefer_images else []
    if buckets:
        return buckets[0][1]
    return ds.raw_dir


def load_meta(ds) -> dict[str, dict]:
    """filename -> post row, from raw_posts.jsonl."""
    rows = net.read_jsonl(ds.pipe_dir / "raw_posts.jsonl")
    return {r.get("filename"): r for r in rows if r.get("filename")}


def probe_image(path: Path) -> dict:
    info = {"width": 0, "height": 0, "mode": "", "bytes": path.stat().st_size if path.exists() else 0,
            "error": ""}
    try:
        with Image.open(path) as im:
            info["width"], info["height"] = im.size
            info["mode"] = im.mode
            im.verify()
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# exclusion evidence (every reject must be traceable and reversible)
# --------------------------------------------------------------------------
def record_excluded(ds, entries: list[dict]) -> Path | None:
    """Append exclusion evidence to <dataset>/_excluded/_EXCLUDED_MANIFEST.json.

    Each entry: {original, new_name, post_id, source, reason, rule, detail, decided_by}
    decided_by is one of rule / vision / user.
    """
    if not entries:
        return None
    path = ds.excluded_dir / "_EXCLUDED_MANIFEST.json"
    data: dict = {"dataset": ds.name, "entries": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("entries", [])
        except Exception:
            pass
    known = {(e.get("original"), e.get("reason")) for e in data["entries"]}
    for e in entries:
        key = (e.get("original"), e.get("reason"))
        if key in known:
            continue
        known.add(key)
        data["entries"].append(e)
    data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["count"] = len(data["entries"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def move_excluded(ds, files: list[tuple[Path, str, dict]]) -> int:
    """Move [(path, reason, extra)] into _excluded/<reason>/ and record evidence."""
    entries = []
    for path, reason, extra in files:
        dest_dir = ds.excluded_dir / reason
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        try:
            shutil.move(str(path), str(dest))
        except Exception as exc:  # pragma: no cover
            print(f"[exclude] 移动失败 {path.name}: {exc}")
            continue
        side = path.with_suffix(".txt")
        if side.exists():
            try:
                shutil.move(str(side), str(dest_dir / side.name))
            except Exception:
                pass
        entries.append({"original": path.name, "new_name": "", "post_id": extra.get("post_id", ""),
                        "source": extra.get("source", ""), "reason": reason,
                        "rule": extra.get("rule", ""), "detail": extra.get("detail", ""),
                        "decided_by": extra.get("decided_by", "rule")})
    record_excluded(ds, entries)
    return len(entries)


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = fields or list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------
def _find_extractor() -> list[str] | None:
    for cand in (["7z", "x"], ["7za", "x"], ["C:/Program Files/7-Zip/7z.exe", "x"],
                 ["C:/Program Files/WinRAR/WinRAR.exe", "x"], ["unrar", "x"]):
        exe = cand[0]
        if os.path.sep in exe or (len(exe) > 2 and exe[1] == ":"):
            if Path(exe).exists():
                return cand
        elif shutil.which(exe):
            return cand
    return None


def unpack_archive(path: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".zip":
        import zipfile
        with zipfile.ZipFile(path) as zf:
            zf.extractall(dest)
        return sum(1 for _ in dest.rglob("*"))
    ex = _find_extractor()
    if not ex:
        raise SystemExit(f"[import] 无法解包 {path.name}：未找到 7z / WinRAR / unrar")
    subprocess.run([*ex, str(path), f"-o{dest}", "-y"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return sum(1 for _ in dest.rglob("*"))


def cmd_import(ds, src: str, unpack: bool = True, move: bool = False,
               recurse: bool = True, dry_run: bool = False) -> dict:
    src_path = Path(src)
    if not src_path.exists():
        raise SystemExit(f"[import] 路径不存在: {src_path}")
    ds.ensure_dirs()

    # src 红线（真实使用踩过）：src 传成数据集根目录，rglob 会把 images/、thumbs/、
    # masks/ 全部当成"新图"再导一份进 00_raw —— 200 张里有 139 张是这么来的。
    root = ds.root.resolve()
    src_res = src_path.resolve()
    if src_res == root or root.is_relative_to(src_res):
        raise SystemExit(
            f"[import] 拒绝执行：src 是数据集目录本身或它的上级\n"
            f"         src   = {src_res}\n"
            f"         数据集 = {root}\n"
            f"         import 只收「素材目录 / 压缩包」，请指向图片所在的源目录，\n"
            f"         例如 <数据集>/00_raw 之外的一个新目录；数据集内的 images/、\n"
            f"         thumbs/ 由 rename/thumbs 阶段维护，不需要再导入。"
        )
    if src_res.is_relative_to(root):
        # 数据集目录内的路径：保留目录与成品图桶一律拒绝（它们是流水线自己的产物），
        # 别的子目录放行但要说清楚 —— **含图的根子目录会自动成为图桶**，导完就多一个桶。
        buckets = {name for name, _ in ds.bucket_dirs()}
        if src_res.name in RESERVED_DIRS or src_res.name in buckets:
            raise SystemExit(
                f"[import] 拒绝执行：src 是数据集里的保留目录/图桶（{src_res.name}）\n"
                f"         import 只收外部素材；{src_res.name}/ 是流水线自己的产物。"
            )
        print(f"[import] ⚠ src 在数据集目录内（{src_res.relative_to(root)}）——"
              f"导入后这个目录只要还含图就会被当成图桶（work set）。建议把素材移出去再导入。")

    staging = ds.pipe_dir / "_import_staging"
    items: list[Path] = []

    def collect(p: Path) -> None:
        if p.is_dir():
            if unpack and p.suffix.lower() in ARCHIVE_EXTS:
                return
            # 数据集自己的保留目录不再递归（只对数据集内的路径生效，
            # 别把外部素材目录里恰好叫 images/ 的文件夹也跳过）
            try:
                if p.resolve().is_relative_to(root):
                    if p.name in RESERVED_DIRS or p.name.startswith(("_", ".")):
                        return
            except Exception:
                pass
            it = p.rglob("*") if recurse else p.glob("*")
            for child in it:
                if child.is_file():
                    items.append(child)
        else:
            items.append(p)

    collect(src_path)

    # unpack archives into staging first
    archives = [p for p in items if p.suffix.lower() in ARCHIVE_EXTS]
    for arc in archives:
        target = staging / arc.stem
        print(f"  unpack {arc.name} -> {target}")
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)
            unpack_archive(arc, target)
            for child in target.rglob("*"):
                if child.is_file():
                    items.append(child)

    images = [p for p in items if p.suffix.lower() in IMG_EXTS]
    # 同名 .txt 是 caption 侧车（人工或别的打标工具写的），必须跟着图一起搬：
    # 上次真实使用里 import 只搬了图，144 条 caption 是手工补搬的。
    img_stems = {p.stem for p in images}
    sidecars = [p for p in items
                if p.suffix.lower() == ".txt" and p.stem in img_stems
                and p.with_suffix("") not in images]
    side_set = set(sidecars)
    others = [p for p in items if p not in side_set and p.suffix.lower() not in IMG_EXTS]
    print(f"[import] 候选文件 {len(items)}，其中图片 {len(images)}，"
          f"同名 txt 侧车 {len(sidecars)}，非图 {len(others)}")
    if others:
        # 早先这里是个静默黑洞：zip/mp4/txt 既不进 00_raw 也不进任何报告，
        # 于是"我明明导入了 30 个文件"没法核对。现在逐个列出来。
        by_ext: dict[str, int] = {}
        for p in others:
            by_ext[p.suffix.lower() or "(无后缀)"] = by_ext.get(p.suffix.lower() or "(无后缀)", 0) + 1
        print("         非图文件不计入 00_raw（解包后的图会收）："
              + "，".join(f"{ext}×{n}" for ext, n in sorted(by_ext.items(), key=lambda kv: -kv[1])))
        for p in others[:5]:
            print(f"         · {p.name}")
        if len(others) > 5:
            print(f"         · … 另有 {len(others) - 5} 个")

    used = {p.name for p in ds.raw_dir.iterdir()} if ds.raw_dir.exists() else set()
    rows: list[dict] = [{"source": "import", "origin": str(p), "filename": p.name,
                         "ext": p.suffix.lower(), "bytes": p.stat().st_size,
                         "status": "skipped-non-image"} for p in sorted(others)]
    copied = 0
    side_copied = 0
    for p in sorted(images):
        if p.is_relative_to(ds.raw_dir) if hasattr(p, "is_relative_to") else str(p).startswith(str(ds.raw_dir)):
            continue
        name = p.name
        n = 1
        while name in used:
            n += 1
            name = f"{p.stem}__{n}{p.suffix}"
        used.add(name)
        entry = {"source": "import", "origin": str(p), "filename": name,
                 "ext": p.suffix.lower(), "bytes": p.stat().st_size, "status": "planned"}
        if not dry_run:
            dest = ds.raw_dir / name
            if move:
                shutil.move(str(p), str(dest))
            else:
                shutil.copy2(p, dest)
            info = probe_image(dest)
            entry.update({"status": "ok" if not info["error"] else "unreadable",
                          "width": info["width"], "height": info["height"],
                          "created_at": time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime))})
            copied += 1
            side = p.with_suffix(".txt")
            if side.exists():
                try:
                    shutil.copy2(side, dest.with_suffix(".txt"))
                    side_copied += 1
                    entry["sidecar"] = "txt"
                except Exception as exc:
                    print(f"  ! 侧车搬运失败 {side.name}: {exc}")
        rows.append(entry)

    if rows and not dry_run:
        existing = net.read_jsonl(ds.pipe_dir / "raw_posts.jsonl")
        known = {(r.get("filename"), r.get("origin")) for r in existing}
        merged = existing + [r for r in rows
                             if r.get("status") != "skipped-non-image"
                             and (r.get("filename"), r.get("origin")) not in known]
        net.write_jsonl(ds.pipe_dir / "raw_posts.jsonl", merged)
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    print(f"[import] 导入 {copied} 张 -> {ds.raw_dir}"
          + (f"（含同名 txt 侧车 {side_copied} 条）" if side_copied else ""))
    if sidecars and not dry_run and side_copied < len(sidecars):
        print(f"         注意：{len(sidecars) - side_copied} 条侧车没搬过来（同名图片没被导入？）")
    if others:
        print(f"         跳过非图 {len(others)} 个（已在报告里逐条列出）")
    return {"rows": rows, "count": copied, "sidecars": side_copied, "skipped": len(others)}


# --------------------------------------------------------------------------
# dedup
# --------------------------------------------------------------------------
def _fingerprint(path: Path, size: int = 32, hash_size: int = 8):
    """One decode -> (pHash, 64x64 grayscale uint8, aspect ratio).

    Decoding each image once and keeping a tiny grayscale copy lets the dedup
    pass do *pairwise* SSIM cheaply instead of trusting a transitive pHash
    clustering.
    """
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("L")
            aspect = im.width / max(1, im.height)
            small = np.asarray(im.resize((64, 64), Image.LANCZOS), dtype=np.uint8)
            arr = np.asarray(im.resize((size, size), Image.LANCZOS), dtype=np.float32)
    except Exception:
        return None
    n = size
    k = np.arange(n)
    basis = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n)) * np.sqrt(2.0 / n)
    basis[0] /= math.sqrt(2.0)
    dct = basis @ arr @ basis.T
    low = dct[:hash_size, :hash_size].flatten()
    med = np.median(low[1:])
    bits = (low > med).astype(np.uint8)
    out = 0
    for b in bits:
        out = (out << 1) | int(b)
    return out, small, aspect


def _phash(path: Path, size: int = 32, hash_size: int = 8) -> int | None:
    """Perceptual hash: 32x32 grayscale -> 2D DCT -> top-left 8x8 -> median bits."""
    fp = _fingerprint(path, size=size, hash_size=hash_size)
    return None if fp is None else fp[0]


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _ssim_arr(a: np.ndarray, b: np.ndarray) -> float:
    """Global SSIM over two equally shaped grayscale arrays (0..255)."""
    a = a.astype(np.float64, copy=False)
    b = b.astype(np.float64, copy=False)
    mu_a, mu_b = a.mean(), b.mean()
    va, vb = a.var(), b.var()
    cov = ((a - mu_a) * (b - mu_b)).mean()
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    return float(((2 * mu_a * mu_b + c1) * (2 * cov + c2)) /
                 ((mu_a ** 2 + mu_b ** 2 + c1) * (va + vb + c2)))


def _ssim(path_a: Path, path_b: Path, size: int = 64) -> float:
    try:
        def load(p: Path):
            with Image.open(p) as im:
                return np.asarray(ImageOps.exif_transpose(im).convert("L")
                                  .resize((size, size), Image.LANCZOS), dtype=np.uint8)
        return _ssim_arr(load(path_a), load(path_b))
    except Exception:
        return 1.0


def cmd_dedup(ds, phash_distance: int = 4, ssim_threshold: float = 0.995,
              ssim_review: float = 0.97, aspect_tolerance: float = 0.02,
              apply: bool = False, include_images: bool = False,
              work_set=None) -> dict:
    """De-duplicate 00_raw.

    Three tiers, in increasing aggressiveness:

    1. **exact** - identical md5, dropped unconditionally.
    2. **near** - pHash distance <= `phash_distance` *and* SSIM >= `ssim_threshold`:
       the same picture resized / re-encoded / re-saved, dropped.
    3. **similar** - SSIM in [`ssim_review`, `ssim_threshold`): the report lists
       them and *nothing is moved*.  Artists routinely post several drawings of
       one scene (session variants); for a style LoRA those are legitimate data,
       so the decision belongs to the user, not to this rule.

    SSIM - not the pHash clustering - decides the grouping, because a transitive
    pHash union happily chains three genuinely different drawings of one scene
    into a single "duplicate" group.
    """
    ds.ensure_dirs()
    files = ds.work_images(work_set) if include_images else ds.raw_images()
    if not files:
        print(f"[dedup] 没有可处理的图片（{ds.raw_dir}）")
        return {"groups": 0, "dropped": 0}
    print(f"[dedup] {len(files)} 张图  md5 精确 → pHash≤{phash_distance} 候选 → "
          f"SSIM≥{ssim_threshold} 判同一张（{ssim_review}~{ssim_threshold} 只报告）")
    meta = load_meta(ds)

    # 1) exact duplicates by md5 of the file content
    by_md5: dict[str, list[Path]] = {}
    for i, f in enumerate(files, 1):
        h = hashlib.md5(f.read_bytes()).hexdigest()
        by_md5.setdefault(h, []).append(f)
        if i % 200 == 0:
            print(f"  md5 {i}/{len(files)}")

    exact_groups = [v for v in by_md5.values() if len(v) > 1]
    winners = [v[0] for v in by_md5.values()]

    # 2) one decode per winner -> phash + 64x64 grayscale + aspect
    fps: dict[Path, tuple] = {}
    for i, f in enumerate(winners, 1):
        fp = _fingerprint(f)
        if fp is not None:
            fps[f] = fp
        if i % 100 == 0:
            print(f"  fingerprint {i}/{len(winners)}")

    keys = list(fps)
    parent = {k: k for k in keys}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    similar: list[dict] = []
    pairs = 0
    for i in range(len(keys)):
        pa = keys[i]
        ha, sa, aa = fps[pa]
        for j in range(i + 1, len(keys)):
            pb = keys[j]
            hb, sb, ab = fps[pb]
            if abs(aa - ab) > aspect_tolerance * max(aa, ab):
                continue
            dist = _hamming(ha, hb)
            if dist > phash_distance:
                continue
            pairs += 1
            score = _ssim_arr(sa, sb)
            if score >= ssim_threshold:
                union(pa, pb)
            elif score >= ssim_review:
                similar.append({
                    "group": "", "kind": "similar", "decision": "review",
                    "keep": max(pa, pb, key=lambda p: p.stat().st_size).name,
                    "file": min(pa, pb, key=lambda p: p.stat().st_size).name,
                    "size": min(pa.stat().st_size, pb.stat().st_size),
                    "kept_size": max(pa.stat().st_size, pb.stat().st_size),
                    "ssim": f"{score:.4f}", "phash_distance": dist,
                    "post_id": meta.get(pa.name, {}).get("post_id", ""),
                })

    groups: dict[Path, list[Path]] = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)
    near_groups = [sorted(v) for v in groups.values() if len(v) > 1]

    # 3) keep the largest/latest file of each duplicate group
    def rank(p: Path) -> tuple[int, float]:
        m = meta.get(p.name, {})
        return (int(m.get("width") or 0) * int(m.get("height") or 0), p.stat().st_size)

    rows: list[dict] = []
    dropped: list[Path] = []
    gid = 0

    def emit(group: list[Path], kind: str) -> None:
        nonlocal gid
        gid += 1
        keep = max(group, key=rank)
        for p in group:
            if p is keep:
                continue
            score = f"{_ssim_arr(fps[keep][1], fps[p][1]):.4f}" if kind == "near" else ""
            rows.append({
                "group": gid, "kind": kind, "decision": "drop",
                "keep": keep.name, "file": p.name, "size": p.stat().st_size,
                "kept_size": keep.stat().st_size, "ssim": score,
                "phash_distance": _hamming(fps[keep][0], fps[p][0]) if kind == "near" else 0,
                "post_id": meta.get(p.name, {}).get("post_id", ""),
            })
            dropped.append(p)

    for group in exact_groups:
        emit(group, "exact")
    for group in near_groups:
        emit(group, "near")
    rows.extend(similar)

    write_csv(ds.pipe_dir / "dedup_report.csv", rows)

    if apply and dropped:
        move_excluded(ds, [(p, "duplicates",
                            {"post_id": meta.get(p.name, {}).get("post_id", ""),
                             "source": meta.get(p.name, {}).get("source", ""),
                             "rule": "md5/phash+ssim",
                             "detail": f"ssim>={ssim_threshold}",
                             "decided_by": "rule"}) for p in dropped])
        print(f"[dedup] 已剔除 {len(dropped)} 张 -> {ds.excluded_dir / 'duplicates'}"
              f"（证据 _EXCLUDED_MANIFEST.json）")
    else:
        print(f"[dedup] 报告 {ds.pipe_dir / 'dedup_report.csv'}"
              f"（精确重复组 {len(exact_groups)}，同图组 {len(near_groups)}，"
              f"相似待定 {len(similar)} 对，候选比较 {pairs} 对；"
              f"{'没有需要剔除的' if apply else '未执行删除，加 --apply 才会移动文件'}）")
    return {"groups": len(exact_groups) + len(near_groups), "dropped": len(dropped),
            "exact_groups": len(exact_groups), "near_groups": len(near_groups),
            "similar_pairs": len(similar), "pairs_compared": pairs}



# --------------------------------------------------------------------------
DEFAULT_SKETCH_TAGS = {
    "sketch", "rough", "lineart", "unfinished", "character sheet", "monochrome",
    "greyscale", "sketchbook", "traditional media", "wip", "colorized",
    "partially colored", "sketch page", "reference sheet", "chibi",
}


def cmd_screen(ds, rules: dict | None = None, apply: bool = False,
               include_images: bool = False, work_set=None) -> dict:
    ds.ensure_dirs()
    rules = rules or {}
    min_short = int(rules.get("min_short_side", 512))
    min_bytes = int(rules.get("min_bytes", 102400))
    earliest = str(rules.get("earliest_date", "2015-01-01"))
    sketch_tags = {t.lower() for t in rules.get("sketch_tags", sorted(DEFAULT_SKETCH_TAGS))}

    files = ds.work_images(work_set) if include_images else ds.raw_images()
    meta = load_meta(ds)
    print(f"[screen] {len(files)} 张图  min_short_side={min_short} min_bytes={min_bytes} "
          f"earliest={earliest}")

    rows: list[dict] = []
    drops: list[tuple[Path, str]] = []
    review: list[dict] = []

    for f in files:
        m = meta.get(f.name, {})
        info = probe_image(f)
        w = int(m.get("width") or info["width"] or 0)
        h = int(m.get("height") or info["height"] or 0)
        size = info["bytes"]
        reasons: list[str] = []
        notes: list[str] = []

        if info["error"]:
            reasons.append("unreadable")
            notes.append(info["error"])
        short = min(w, h) if w and h else 0
        if short and short < min_short:
            reasons.append("low-res")
        if size and size < min_bytes:
            reasons.append("tiny-file")
        date = str(m.get("created_at") or "")
        if date and earliest and date < earliest:
            reasons.append("too-old")
        tags = {t.lower().replace("_", " ") for t in (m.get("tags") or [])}
        hit = sorted(tags & sketch_tags)
        if hit:
            notes.append("sketch-tags:" + "|".join(hit))
            review.append({"file": f.name, "why": "sketch-tags", "detail": ",".join(hit),
                           "width": w, "height": h, "date": date})
        if not m:
            notes.append("no-booru-metadata")

        row = {"file": f.name, "decision": "drop" if reasons else "keep",
               "reasons": "|".join(reasons), "width": w, "height": h, "bytes": size,
               "date": date, "rating": m.get("rating", ""), "post_id": m.get("post_id", ""),
               "tags": " ".join(sorted(tags))[:400], "notes": "|".join(notes)}
        rows.append(row)
        if reasons:
            drops.append((f, reasons[0],
                          {"post_id": m.get("post_id", ""), "source": m.get("source", ""),
                           "rule": "|".join(reasons), "detail": f"{w}x{h} {size}B {date}",
                           "decided_by": "rule"}))

    write_csv(ds.pipe_dir / "screen_report.csv", rows)
    write_csv(ds.pipe_dir / "review_queue.csv", review)

    if apply and drops:
        n = move_excluded(ds, drops)
        print(f"[screen] 移除 {n} 张 -> {ds.excluded_dir}/<reason>/ "
              f"（证据 {ds.excluded_dir / '_EXCLUDED_MANIFEST.json'}）")
    kept = len(files) - len(drops)
    print(f"[screen] 保留 {kept} / {len(files)}；报告 {ds.pipe_dir / 'screen_report.csv'}"
          f"；待视觉复核 {len(review)} 张 -> review_queue.csv")
    return {"count": len(files), "kept": kept, "dropped": len(drops), "review": len(review)}


# --------------------------------------------------------------------------
# thumbs (for the vision subagents -- never let them read 30MB originals)
# --------------------------------------------------------------------------
def cmd_thumbs(ds, max_side: int = 1536, max_bytes: int = 2_000_000,
               include_images: bool = False, prune: bool = False,
               force: bool = False, work_set=None) -> dict:
    """生成缩略图（视觉子代理只能看这个）。

    真实使用踩过的坑：thumbs 默认读 00_raw，而 rename 之后 00_raw 已经空了 ⇒
    第二次 thumbs 打印「0 张」，agent 只能自己拿 Pillow 造缩略图。现在：
      * 工作集 = 所有成品图桶（images/、clean/、watermark/ …，见 Dataset.bucket_dirs）；
      * 已存在且比原图新、长边也不超标的缩略图跳过（重跑不再白算几百张）；
      * --prune 清掉没有对应图的陈旧缩略图（改名后残留的旧编号）。
    """
    ds.ensure_dirs()
    ds.thumbs_dir.mkdir(parents=True, exist_ok=True)
    files = ds.work_images(work_set)
    if not files:
        print("[thumbs] 工作集是空的：没有含图的桶（images/ 是空的？）。"
              "先 fetch/import，或 rename 之后再跑 thumbs；成品图放数据集根下任意子目录都行。")
        return {"count": 0, "skipped": 0, "pruned": 0, "dir": str(ds.thumbs_dir)}

    written = 0
    skipped = 0
    total = 0
    for f in files:
        out = ds.thumbs_dir / (f.stem + ".jpg")
        if out.exists() and not force:
            try:
                fresh = out.stat().st_mtime >= f.stat().st_mtime
                with Image.open(out) as old:
                    fits = max(old.size) <= max_side
                if fresh and fits:
                    skipped += 1
                    continue
            except Exception:
                pass
        try:
            with Image.open(f) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((max_side, max_side), Image.LANCZOS)
                quality = 88
                while True:
                    im.save(out, "JPEG", quality=quality, optimize=True)
                    if out.stat().st_size <= max_bytes or quality <= 45:
                        break
                    quality -= 8
            written += 1
            total += out.stat().st_size
        except Exception as exc:
            print(f"  ! thumb failed {f.name}: {exc}")

    pruned = 0
    if prune:
        keep = {f.stem for f in files}
        for out in sorted(ds.thumbs_dir.glob("*.jpg")):
            if out.stem in keep:
                continue
            try:
                out.unlink()
                pruned += 1
            except Exception:
                pass

    print(f"[thumbs] 新写 {written} 张、跳过 {skipped} 张"
          + (f"、清理陈旧 {pruned} 张" if prune else "")
          + f" -> {ds.thumbs_dir}（平均 {total // max(written, 1) // 1024} KB，长边≤{max_side}）")
    return {"count": written, "skipped": skipped, "pruned": pruned, "dir": str(ds.thumbs_dir)}


# --------------------------------------------------------------------------
# exclude: 人工淘汰（带清单与回滚）
# --------------------------------------------------------------------------
def cmd_exclude(ds, names="", reason: str = "user", apply: bool = False,
                undo: bool = False, missing: bool = False, work_set=None,
                note: str = "") -> dict:
    """人工淘汰：把图 + 同名 txt 移进 `_excluded/<reason>/`，并记进用户手删清单。

    为什么需要它（真实使用复盘）：上一次跑完，用户手删了 4 张图，工具下一轮只报
    「少了 4 张」，既不知道删的是谁，也没留找回路径；复核时只能去翻文件系统。

      * `names` 里每张图 → 移动到 `_excluded/<reason>/`，`_pipeline/user_deleted.json`
        只追加一条（记当前文件名、所有旧名、归档路径、原因、时间）；
      * `missing: true` 是**事后补账**：把 per_image.json / rename_map.csv 里知道、
        但工作集里已经不在的图补进清单（archived 为空，只留证据不搬文件）；
      * `undo: true` → 按清单里的归档路径搬回**原来的桶**（entries 里记了 bucket），并标记
        restored_at；不给 `names` 就恢复清单里所有还归档着的条目。

    默认 dry-run，`apply: true` 才动文件。淘汰永远只移动不删除。
    """
    ds.ensure_dirs()
    st = state.load(ds)
    want = names if isinstance(names, (list, tuple, set)) else str(names or "").split(",")
    want = [str(n).strip() for n in want if str(n).strip()]
    rows: list[dict] = []

    if undo:
        data = state.load_user_deleted(ds)
        if not want:
            # 不给名字 = 把清单里所有还归档着的都搬回来（"我反悔了"是最常见的用法）
            want = [str(e.get("name")) for e in data["entries"]
                    if str(e.get("archived") or "") and not e.get("restored_at")]
            if not want:
                print("[exclude] 清单里没有可恢复的条目（用 names 指定，或先 missing:true 补账）")
                return {"rows": [], "restored": 0, "applied": bool(apply)}
        for name in want:
            key = st.find_by_old_name(name) or name
            entry = next((e for e in data["entries"]
                          if e.get("name") == key
                          or Path(str(e.get("name"))).stem == Path(name).stem), None)
            if entry is None:
                rows.append({"file": name, "action": "undo", "status": "不在清单里"})
                continue
            src = Path(str(entry.get("archived") or ""))
            # 恢复目标必须回到它原来所在的桶（桶布局下 images/ 常常是空的）
            bucket = (str(entry.get("bucket") or "")
                      or str(st.get(str(entry["name"])).get("bucket") or "")
                      or "images")
            dest = ds.root / bucket / str(entry["name"])
            if not src.is_file():
                rows.append({"file": entry["name"], "action": "undo",
                             "status": f"归档文件不在（{src}）—— 需要手工找回"})
                continue
            if apply:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
                side = src.with_suffix(".txt")
                if side.exists():
                    shutil.move(str(side), str(dest.with_suffix(".txt")))
                state.clear_user_deleted(ds, str(entry["name"]))
                st.record(str(entry["name"]), history={"what": "restored-by-user"})
                rows.append({"file": entry["name"], "action": "undo", "status": f"已恢复到 {dest}"})
            else:
                rows.append({"file": entry["name"], "action": "undo",
                             "status": f"会从 {src} 恢复到 {dest}"})
        if apply:
            st.save()
        for row in rows:
            print(f"  {row['file']}  {row['status']}")
        print(f"[exclude] undo：{sum(1 for r in rows if r['status'].startswith('已恢复'))} 张已恢复"
              + ("" if apply else "（dry-run：加 apply:true 才动文件）"))
        return {"rows": rows, "restored": sum(1 for r in rows if r["status"].startswith("已恢复")),
                "applied": bool(apply)}

    if missing:
        present = {p.name for p in ds.work_images(work_set)}
        known: dict[str, list[str]] = {}
        for name in st.names():
            known[name] = [str(o) for o in (st.get(name).get("old_names") or [])]
        for name, row in load_rename_map(ds).items():
            known.setdefault(name, [])
            old = str(row.get("old_name") or "")
            if old and old not in known[name]:
                known[name].append(old)
        for e in state.load_user_deleted(ds)["entries"]:
            known.setdefault(str(e.get("name") or ""), list(e.get("old_names") or []))
        gone = [n for n in sorted(known) if n and n not in present]
        for name in gone:
            olds = known.get(name) or []
            if apply:
                state.note_user_deleted(ds, name, archived="", reason="user", old_names=olds,
                                        note=note or "事后补账：文件已不在工作集里")
                st.record(name, user_deleted_at=state.now(),
                          history={"what": "user-deleted-recorded"})
            rows.append({"file": name, "action": "missing", "status": "不在工作集里",
                         "old_names": "; ".join(olds)})
        if apply:
            st.save()
        for row in rows[:20]:
            print(f"  {row['file']}  旧名：{row.get('old_names') or '（无记录）'}")
        if len(rows) > 20:
            print(f"  … 还有 {len(rows) - 20} 张")
        print(f"[exclude] 事后补账：状态里知道 {len(known)} 张，其中 {len(gone)} 张已不在工作集"
              + ("" if apply else "（dry-run：加 apply:true 才写清单）"))
        return {"rows": rows, "missing": len(gone), "applied": bool(apply)}

    if not want:
        data = state.load_user_deleted(ds)
        live = [e for e in data["entries"] if not e.get("restored_at")]
        print(f"[exclude] 手删清单里有 {len(live)} 条：")
        for e in live[:15]:
            print(f"   {e.get('name')}　归档 {e.get('archived') or '（只留证据）'}"
                  f"　原因 {e.get('reason') or '-'}　{e.get('at')}")
        if not live:
            print("   （空。要淘汰某几张：exclude name=\"0007,0012\" apply=true；"
                  "要把历史上手删的补进清单：exclude missing=true apply=true）")
        return {"entries": live, "count": len(live)}

    moves: list[tuple[Path, str, dict]] = []
    for name in want:
        cur = state.resolve_name(ds, name)
        hit, bucket = state.locate(ds, cur)
        if hit is None:
            rows.append({"file": name, "action": "exclude", "status": "找不到这张图"})
            continue
        dest = (ds.excluded_dir / (reason or "user")) / hit.name
        if apply:
            moves.append((hit, reason or "user",
                          {"rule": "user-exclude", "detail": note, "decided_by": "user"}))
            state.note_user_deleted(ds, hit.name, archived=str(dest), reason=reason or "user",
                                    old_names=(st.get(hit.name).get("old_names") or []), note=note,
                                    bucket=bucket)
            st.record(hit.name, bucket=bucket, user_deleted_at=state.now(),
                      history={"what": "user-excluded", "reason": reason or "user"})
            rows.append({"file": hit.name, "action": "exclude", "status": f"已移到 {dest}"})
        else:
            rows.append({"file": hit.name, "action": "exclude",
                         "status": f"会从 {bucket or '?'} 移到 {dest}"})
    moved = move_excluded(ds, moves) if moves else 0
    if apply:
        st.save()
    for row in rows:
        print(f"  {row['file']}  {row['status']}")
    print(f"[exclude] {moved} 张已淘汰（共处理 {len(rows)} 张）"
          + ("" if apply else "（dry-run：加 apply:true 才动文件）"))
    return {"rows": rows, "moved": moved, "applied": bool(apply)}


# --------------------------------------------------------------------------
# rename
# --------------------------------------------------------------------------
def cmd_rename(ds, start: int = 1, digits: int = 4, apply: bool = False,
               move: bool = True) -> dict:
    ds.ensure_dirs()
    files = ds.raw_images()
    if not files:
        files = ds.images()
        if files:
            raise SystemExit("[rename] 00_raw 为空，而 images/ 已有内容——数据集似乎已重排过")
        raise SystemExit("[rename] 00_raw 为空，先下载或导入图片")
    meta = load_meta(ds)

    # keep the booru ordering (newest first) when metadata is available
    def sort_key(p: Path):
        m = meta.get(p.name, {})
        return (str(m.get("created_at") or "0000-00-00"), p.name)

    files = sorted(files, key=sort_key)
    rows: list[dict] = []
    sidecars = 0
    for idx, f in enumerate(files):
        n = start + idx
        new_name = f"{n:0{digits}d}{f.suffix.lower()}"
        m = meta.get(f.name, {})
        rows.append({
            "new_name": new_name, "old_name": f.name, "post_id": m.get("post_id", ""),
            "source": m.get("source", ""), "created_at": m.get("created_at", ""),
            "rating": m.get("rating", ""), "width": m.get("width", ""), "height": m.get("height", ""),
            "bytes": f.stat().st_size, "url": m.get("url", ""),
            # danbooru/yande.re 的原形是**下划线** tag（多词标签 `hakurei_reimu`），CSV 里只能存
            # 字符串，所以统一写成下划线形：空格形会在下游 `.split()` 时被切成两个词。
            "tags_full": " ".join(str(t).replace(" ", "_") for t in (m.get("tags") or [])),
            "category": m.get("category", ""),
        })
        if apply:
            dest = ds.images_dir / new_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if move:
                shutil.move(str(f), str(dest))
            else:
                shutil.copy2(f, dest)
            # 原图旁边的 .txt（早期手工补的 / import 带进来的）跟着搬，别留在 00_raw 里成孤儿
            side = f.with_suffix(".txt")
            if side.exists():
                target = dest.with_suffix(".txt")
                if not target.exists():
                    try:
                        shutil.move(str(side), str(target)) if move else shutil.copy2(side, target)
                        sidecars += 1
                    except Exception:
                        pass

    total_rows = len(rows)
    first, last = start, start + total_rows - 1
    if not apply:
        # 预演不碰权威对照表（load_rename_map 读的就是它，别让预演污染下游的标签来源）
        preview = ds.pipe_dir / "rename_map.preview.csv"
        write_csv(preview, rows)
        print(f"[rename] 预演：{total_rows} 张将重排为 {first:0{digits}d}…{last:0{digits}d}"
              f"；对照表 {preview}（加 --apply 执行；权威表 rename_map.csv 不动）")
        return {"count": total_rows, "map": str(preview), "applied": False}

    # 只追加（真实使用踩过）：早先这里直接覆盖 rename_map.csv，第二批 rename
    # （--start 27）一跑，第一批 26 行的 post_id/tags_full 就没了。
    map_path = ds.pipe_dir / "rename_map.csv"
    merged: list[dict] = []
    seen_old: set[str] = set()
    if map_path.exists():
        with open(map_path, encoding="utf-8", newline="") as fh:
            for old_row in csv.DictReader(fh):
                key = old_row.get("old_name", "")
                if key and key in {r["old_name"] for r in rows}:
                    continue          # 同一张图重排过：以本次为准
                if key and key in seen_old:
                    continue
                seen_old.add(key)
                merged.append(old_row)
    merged.extend(rows)
    write_csv(map_path, merged)
    batch_path = ds.pipe_dir / f"rename_map.{first:0{digits}d}-{last:0{digits}d}.csv"
    write_csv(batch_path, rows)

    st = state.load(ds)
    for row in rows:
        st.link_rename(
            row["new_name"], row["old_name"],
            post_id=row["post_id"], source=row["source"], created_at=row["created_at"],
            width=row["width"], height=row["height"], url=row["url"],
            origin="A+booru" if row["tags_full"] else "A=none",
            history={"what": "rename", "from": row["old_name"]},
        )
    st.add_batch(first, last, total_rows, batch_path.name)
    st.save()

    print(f"[rename] {total_rows} 张已重排为 {first:0{digits}d}…{last:0{digits}d} -> {ds.images_dir}")
    print(f"         对照表 {map_path}（累计 {len(merged)} 行，只追加）　本批快照 {batch_path.name}")
    if sidecars:
        print(f"         随图搬运的 .txt 边车：{sidecars} 个")
    print(f"         状态 {st.path.name}（per_image.json：编号/来源/post_id/旧名的唯一事实来源）")
    return {"count": total_rows, "map": str(map_path), "batch": str(batch_path),
            "rows": len(merged), "state": str(st.path), "applied": True}


def load_rename_map(ds) -> dict[str, dict]:
    """new_name -> 对照行。

    表是**只追加**的（每个批次都往里加，见 cmd_rename），同一 new_name 出现多次时
    以最后一行（最新批次）为准。
    """
    path = ds.pipe_dir / "rename_map.csv"
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            key = row.get("new_name") or ""
            if key:
                out[key] = row
    return out


def load_rename_by_old(ds) -> dict[str, dict]:
    """old_name -> 对照行（反查：wash 需要从当前编号找回下载时的文件名与 post_id）。"""
    out: dict[str, dict] = {}
    for row in load_rename_map(ds).values():
        old = row.get("old_name") or ""
        if old:
            out[old] = row
    return out
