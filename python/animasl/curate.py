"""Curation stages: import, dedup, screen, thumbs, rename.

Working directories
    00_raw/     everything that was downloaded or imported
    images/     the curated, renumbered training set (after `rename`)
    _excluded/  rejects, kept as evidence (one subfolder per reason)
"""

from __future__ import annotations

import csv
import fnmatch
import hashlib
import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import config, net, state

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

    # 3) keep one file of each duplicate group
    #    真实使用的坑（g4ku 复盘 5.1 #5）：一组跨源重复里，最大的往往是本地重压缩版，
    #    而**有 booru 标签的**是另一张小的。按「面积 > 体积」选会把标签一起扔掉——
    #    那次 10 组里 9 组丢了 A 源，只能事后手写脚本搬回来救回 6 张。
    #    所以排序第一键是「这文件有没有图源标签」，没标签的大图不能盖掉有标签的小图。
    def has_tags(p: Path) -> bool:
        return bool((meta.get(p.name) or {}).get("tags"))

    def rank(p: Path) -> tuple[int, int, float]:
        m = meta.get(p.name, {})
        return (1 if has_tags(p) else 0,
                int(m.get("width") or 0) * int(m.get("height") or 0), p.stat().st_size)

    rows: list[dict] = []
    dropped: list[Path] = []
    gid = 0
    merge_plan: dict[str, set[str]] = {}          # keep.name -> 要并进去的完整标签池

    def emit(group: list[Path], kind: str) -> None:
        nonlocal gid
        gid += 1
        keep = max(group, key=rank)
        base = set((meta.get(keep.name) or {}).get("tags") or [])
        # 被丢掉那几张的图源标签并到留下来的那张名下，A 源不随文件一起消失
        pool = set(base)
        for p in group:
            pool |= set((meta.get(p.name) or {}).get("tags") or [])
        if pool - base:
            merge_plan[keep.name] = pool
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
                "tags_kept": len(base), "tags_pooled": len(pool),
            })
            dropped.append(p)

    for group in exact_groups:
        emit(group, "exact")
    for group in near_groups:
        emit(group, "near")
    rows.extend(similar)

    write_csv(ds.pipe_dir / "dedup_report.csv", rows)

    # 合并标签**只在 apply 时**做（dry-run 不动 raw_posts.jsonl），
    # 且必须在 move_excluded 之前 —— 文件一旦移走，被剔除那几张的 filename 就对不上行了。
    pooled = 0
    if apply and merge_plan:
        pooled = _pool_tags(ds, merge_plan)
        print(f"[dedup] 合并标签：{len(merge_plan)} 组保留项并入了被剔除项的图源标签"
              f"（{pooled} 条新增 —— 不做这步，跨源重复必然丢 A 源）")
    elif merge_plan:
        print(f"[dedup] （dry-run）有 {len(merge_plan)} 组会合并图源标签，"
              f"加 --apply 才写进 raw_posts.jsonl")

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
            "similar_pairs": len(similar), "pairs_compared": pairs,
            "tag_merges": len(merge_plan), "tags_pooled": pooled}


def _pool_tags(ds, merge_plan: dict[str, set[str]]) -> int:
    """把 merge_plan 写回 raw_posts.jsonl：保留项的 `tags` 换成并集。

    这件事必须在文件被移走之前做，否则被剔除那张的 `filename` 就再也对不上了。
    直接改 jsonl 里现存的保留项行；找不到行就新写一条最小行（wash 只读 `tags`）。
    """
    if not merge_plan:
        return 0
    path = ds.pipe_dir / "raw_posts.jsonl"
    rows = net.read_jsonl(path)
    added = 0
    seen: set[str] = set()
    for row in rows:
        name = row.get("filename")
        if name in merge_plan:
            seen.add(name)
            old = set(row.get("tags") or [])
            new = sorted(old | merge_plan[name])
            added += len(set(new) - old)
            row["tags"] = new
    for name, pool in merge_plan.items():
        if name in seen:
            continue
        rows.append({"filename": name, "tags": sorted(pool), "source": "dedup-merge"})
        added += len(pool)
    net.write_jsonl(path, rows)
    return added



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
        if not tags:
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

    # g4ku 复盘 5.1 #10：没有图源标签的数据集上，screen 里的 sketch-tags 判定完全空转，
    # 最后照样打印「保留 N / N」，agent 会以为筛选做过了。这里把「这一跑其实没筛草图」说清楚。
    no_meta = sum(1 for r in rows if "no-booru-metadata" in r["notes"])
    if rows and no_meta == len(rows):
        print(f"[screen] ! {no_meta}/{len(rows)} 张都没有图源标签——"
              f"本次只有分辨率/体积/年代三条规则生效，sketch-tags 完全没参与判定。"
              f"想按草图标签筛，先跑 enrich 补 booru 标签，或直接看 review_queue.csv 人工判。")
    elif no_meta:
        print(f"[screen] {no_meta}/{len(rows)} 张没有图源元数据，"
              f"这些图的 sketch-tags 判定为空（其余 {len(rows) - no_meta} 张正常）")

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
def _name_tokens(names) -> list[str]:
    """`names` 拆成匹配项：逗号分隔；`0001-0012` 是编号区间，其余按 glob 走。"""
    if isinstance(names, (list, tuple, set)):
        raw = [str(n) for n in names]
    else:
        raw = str(names or "").split(",")
    return [t.strip() for t in raw if t.strip()]


def _as_range(token: str) -> tuple[int, int] | None:
    """`0001-0012` -> (1, 12)；不是纯数字区间就返回 None。"""
    left, _, right = token.partition("-")
    if left.isdigit() and right.isdigit():
        return int(left), int(right)
    return None


def _match_name(name: str, tokens: list[str]) -> bool:
    """stem 是否命中任一匹配项（不区分大小写；写全名带扩展名也行）。"""
    stem = Path(name).stem.casefold()
    full = name.casefold()
    for tok in tokens:
        rng = _as_range(tok)
        if rng is not None:
            if stem.isdigit() and rng[0] <= int(stem) <= rng[1]:
                return True
            continue
        pat = Path(tok.replace("\\", "/")).name.casefold()   # 全名 -> 只留文件名
        if fnmatch.fnmatchcase(stem, pat) or fnmatch.fnmatchcase(stem, Path(pat).stem):
            return True
        if fnmatch.fnmatchcase(full, pat):
            return True
    return False


def cmd_thumbs(ds, max_side: int = 1536, max_bytes: int = 2_000_000,
               include_images: bool = False, prune: bool = False,
               force: bool = False, work_set=None, names="") -> dict:
    """生成缩略图（视觉子代理只能看这个）。

    真实使用踩过的坑：thumbs 默认读 00_raw，而 rename 之后 00_raw 已经空了 ⇒
    第二次 thumbs 打印「0 张」，agent 只能自己拿 Pillow 造缩略图。现在：
      * 工作集 = 所有成品图桶（images/、clean/、watermark/ …，见 Dataset.bucket_dirs）；
      * 已存在且比原图新、长边也不超标的缩略图跳过（重跑不再白算几百张）；
      * `names` 只给指定的几张出图（`"0001,0002"`、区间 `"0001-0012"`、glob `"0001*"`，
        按 stem 匹配，带扩展名也行）——复核阶段常常只改了几张，没必要重算整库；
      * --prune 清掉没有对应图的陈旧缩略图（改名后残留的旧编号）。**prune 永远按全量
        工作集核对**，`names` 只决定这一跑写谁、不决定删谁（否则过滤一次就把别的图的
        缩略图清空了）。
    """
    ds.ensure_dirs()
    ds.thumbs_dir.mkdir(parents=True, exist_ok=True)
    all_files = ds.work_images(work_set)
    if not all_files:
        print("[thumbs] 工作集是空的：没有含图的桶（images/ 是空的？）。"
              "先 fetch/import，或 rename 之后再跑 thumbs；成品图放数据集根下任意子目录都行。")
        return {"count": 0, "skipped": 0, "pruned": 0, "dir": str(ds.thumbs_dir)}

    files = all_files
    tokens = _name_tokens(names)
    if tokens:
        files = [f for f in all_files if _match_name(f.name, tokens)]
        print(f"[thumbs] names={names}：命中 {len(files)}/{len(all_files)} 张")
        if not files:
            sample = "、".join(f.stem for f in all_files[:8])
            print(f"[thumbs] 没有图命中 names={names}。工作集里的编号举例：{sample}"
                  "（写法：0001,0002 / 0001-0012 / 0001*）")

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
        # keep 取全量工作集（不是 names 过滤后的）—— prune 只删「没有对应图」的陈旧缩略图，
        # 跟这一跑给没给 names 无关，否则过滤一次就把别的图的缩略图全清空。
        keep = {f.stem for f in all_files}
        for out in sorted(ds.thumbs_dir.glob("*.jpg")):
            if out.stem in keep:
                continue
            try:
                out.unlink()
                pruned += 1
            except Exception:
                pass

    print(f"[thumbs] 新写 {written} 张、跳过 {skipped} 张"
          + (f"（names={names}：{len(files)}/{len(all_files)} 张）" if tokens else "")
          + (f"、清理陈旧 {pruned} 张（按全量 {len(all_files)} 张核对）" if prune else "")
          + f" -> {ds.thumbs_dir}（平均 {total // max(written, 1) // 1024} KB，长边≤{max_side}）")
    used = config.dir_bytes(ds.thumbs_dir)
    if used:
        print(f"         缩略图目录现在占 {config.human_bytes(used)}"
              "（看图子代理用的副本，全部可再生；看图复核结束后可以整个删掉）")
    return {"count": written, "skipped": skipped, "pruned": pruned,
            "dir": str(ds.thumbs_dir), "matched": len(files)}


# --------------------------------------------------------------------------
# exclude: 人工淘汰（带清单与回滚）
# --------------------------------------------------------------------------
def _names_from_file(ds, spec: str) -> list[str]:
    """`@清单.txt` -> 逐行读名字（# 注释、空行、BOM 都能扛）。"""
    spec = spec.strip().strip('"').strip("'")
    if not spec:
        raise SystemExit('[exclude] names="@…" 后面要跟清单文件路径，'
                         '例如 names="@E:/tmp/list.txt"')
    raw = Path(spec).expanduser()
    if raw.is_absolute():
        tried = [raw]
    else:
        # 相对路径先按当前工作目录找（命令行里敲的最自然），找不到再按数据集根找
        tried = [Path.cwd() / spec, ds.root / spec]
    hit = next((p for p in tried if p.is_file()), None)
    if hit is None:
        raise SystemExit(
            "[exclude] 清单文件不存在：" + spec + "\n"
            "         试过：" + "、".join(str(p) for p in tried) + "\n"
            '         写法：names="@E:/tmp/list.txt"（一行一个名字，也可以逗号分隔；# 开头是注释）')
    names: list[str] = []
    for line in hit.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.extend(t.strip() for t in line.split(",") if t.strip())
    if not names:
        raise SystemExit(f"[exclude] 清单文件 {hit} 里没有有效名字（{hit.stat().st_size} 字节；"
                         "# 开头是注释、空行会被跳过）")
    print(f"[exclude] 从清单文件读了 {len(names)} 个名字：{hit}")
    return names


def _expand_names(ds, names) -> list[str]:
    """names 可以是 list、`0001,0002`，或 `@清单文件`（见 _names_from_file）。"""
    if isinstance(names, (list, tuple, set)):
        return [str(n).strip() for n in names if str(n).strip()]
    text = str(names or "").strip()
    if text.startswith("@"):
        return _names_from_file(ds, text[1:])
    return [n.strip() for n in text.split(",") if n.strip()]


def cmd_exclude(ds, names="", reason: str = "user", apply: bool = False,
                undo: bool = False, missing: bool = False, work_set=None,
                note: str = "") -> dict:
    """人工淘汰：把图 + 同名 txt 移进 `_excluded/<reason>/`，并记进用户手删清单。

    为什么需要它（真实使用复盘）：上一次跑完，用户手删了 4 张图，工具下一轮只报
    「少了 4 张」，既不知道删的是谁，也没留找回路径；复核时只能去翻文件系统。

      * `names` 里每张图 → 移动到 `_excluded/<reason>/`，`_pipeline/user_deleted.json`
        只追加一条（记当前文件名、所有旧名、归档路径、原因、时间）；
      * `names` 也可以写 `"@E:/tmp/list.txt"`：从文件逐行读名字（# 注释、空行、BOM 都处理），
        批量淘汰几十张时不用在命令行里拼一长串；
      * `missing: true` 是**事后补账**：把 per_image.json / rename_map.csv 里知道、
        但工作集里已经不在的图补进清单（archived 为空，只留证据不搬文件）；
      * `undo: true` → 按清单里的归档路径搬回**原来的桶**（entries 里记了 bucket），并标记
        restored_at；不给 `names` 就恢复清单里所有还归档着的条目。

    默认 dry-run，`apply: true` 才动文件。淘汰永远只移动不删除。
    """
    ds.ensure_dirs()
    st = state.load(ds)
    want = _expand_names(ds, names)   # "@清单.txt" 也在这里展开
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
def _is_batch_map(name: str) -> bool:
    """`rename_map.0001-0146.csv` 这类批次快照（权威表 rename_map.csv 不算）。"""
    if not (name.startswith("rename_map.") and name.endswith(".csv")):
        return False
    body = name[len("rename_map."):-len(".csv")]
    left, _, right = body.partition("-")
    return bool(left and right and left.isdigit() and right.isdigit())


def _prune_batch_maps(ds, keep: int | None = None) -> int:
    """每批 rename 留一份快照，只保留最近 keep 份（默认 `hygiene.rename_snapshots` = 3）。

    累计的权威表 `rename_map.csv` 一直在（下游读的是它），批次快照只是"这一批搬了谁"的
    证据——反复重排几十次就会堆几十个几乎一样的 CSV，没必要。
    """
    if keep is None:
        keep = state.hygiene(ds, "rename_snapshots", 3)
    olds = [p for p in ds.pipe_dir.iterdir()
            if p.is_file() and _is_batch_map(p.name)]
    if len(olds) <= keep:
        return 0
    olds.sort(key=lambda p: (p.stat().st_mtime, p.name))
    # keep=0 要全清 —— 注意 olds[:-0] 是空表（切片陷阱），必须单独判
    victims = olds if keep <= 0 else olds[:-keep]
    removed = 0
    for old in victims:
        try:
            old.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _rename_order(meta: dict, files: list[Path]) -> list[Path]:
    """重排顺序：有图源元数据的按 created_at（再按文件名），没有的按文件名。

    桶布局下大多数图已经是 `0001.png` 这种编号名，`meta` 里查不到（raw_posts 记的是下载
    时的文件名）⇒ 退化成按名字排，编号原样保留；只有 `clean/a.png` 这类还没改过名的图才会
    按图源时间排序。
    """
    def key(p: Path):
        m = meta.get(p.name, {})
        return (str(m.get("created_at") or "0000-00-00"), p.name)

    return sorted(files, key=key)


def _rename_row(f: Path, new_name: str, meta: dict, bucket: str) -> dict:
    """对照表的一行。`bucket` 是这一跑结束时它所在的桶（老表没这列照样能读）。"""
    m = meta.get(f.name, {})
    return {
        "new_name": new_name, "old_name": f.name, "post_id": m.get("post_id", ""),
        "source": m.get("source", ""), "created_at": m.get("created_at", ""),
        "rating": m.get("rating", ""), "width": m.get("width", ""), "height": m.get("height", ""),
        "bytes": f.stat().st_size, "url": m.get("url", ""),
        # danbooru/yande.re 的原形是**下划线** tag（多词标签 `hakurei_reimu`），CSV 里只能存
        # 字符串，所以统一写成下划线形：空格形会在下游 `.split()` 时被切成两个词。
        "tags_full": " ".join(str(t).replace(" ", "_") for t in (m.get("tags") or [])),
        "category": m.get("category", ""), "bucket": bucket,
    }


def _staging_path(src: Path, i: int) -> Path:
    """桶内两段式改名的中转名（不撞已有文件，崩溃留下的也能让开）。"""
    n = 0
    while True:
        tail = f"-{n}" if n else ""
        cand = src.with_name(f"{src.stem}.__renaming{i}{tail}{src.suffix}")
        if not cand.exists():
            return cand
        n += 1


def _rename_in_place(pairs: list[tuple[Path, Path]]) -> int:
    """桶内就地改名（两段式），返回跟着搬的 .txt 边车数。

    为什么两段：桶里 0002/0003 要变成 0001/0002 时，单段直接 `0003 -> 0002` 会撞上还没搬走
    的 0002（Windows 上 rename 到已存在文件直接失败，POSIX 上静默覆盖 = 丢图）。先把这次要
    改的图全部挪到中转名，再落到目标名，任何顺序都不互相踩。中途崩了留下的
    `0001.__renaming0.png` 还是图，下次 rename 会把它重新编号收回来。
    """
    todo = [(src, dest) for src, dest in pairs if src != dest]
    if not todo:
        return 0
    staged: list[tuple[Path, Path, Path | None, Path | None]] = []
    for i, (src, dest) in enumerate(todo):
        tmp = _staging_path(src, i)
        src.rename(tmp)
        side = src.with_suffix(".txt")
        side_tmp = side_dest = None
        if side.exists():
            side_tmp = tmp.with_suffix(".txt")
            side.rename(side_tmp)
            side_dest = dest.with_suffix(".txt")
        staged.append((tmp, dest, side_tmp, side_dest))
    sidecars = 0
    for tmp, dest, side_tmp, side_dest in staged:
        try:
            tmp.rename(dest)
        except OSError as exc:
            print(f"  ! 改名失败 {tmp.name} -> {dest.name}：{exc}"
                  "（中转名留在桶里，下次 rename 会重新编号）")
            continue
        if side_tmp is not None and side_tmp.exists():
            if side_dest.exists():
                print(f"  ! {side_dest.name} 已存在，{side_tmp.name} 留在桶里（不覆盖）")
            else:
                side_tmp.rename(side_dest)
                sidecars += 1
    return sidecars


def _print_rename_ranges(ranges: list[dict], digits: int, in_buckets: bool) -> None:
    """打印每个桶的编号区间（桶布局下「我这几张去哪儿了」是最高频的疑问）。"""
    total = sum(r["count"] for r in ranges)
    if in_buckets:
        print(f"[rename] {len(ranges)} 个图桶、{total} 张，桶内就地重排"
              "（编号跨桶连续，但文件不跨桶搬）：")
    else:
        print(f"[rename] {total} 张 -> images/，编号区间：")
    for r in ranges:
        print(f"  {r['bucket']:<10} {r['first']:0{digits}d}…{r['last']:0{digits}d}"
              f"（{r['count']} 张）")


def cmd_rename(ds, start: int = 1, digits: int = 4, apply: bool = False,
               move: bool = True, work_set=None) -> dict:
    """重排编号：`00_raw/` -> `images/`，或**成品图桶内就地重排**。

    真实使用踩过的坑：成品被人工分进 clean/ watermark/ diff1… 之后 images/ 是空的，老的
    rename 只认 00_raw，于是 153 张只能先全扁平化、再手工拆回桶里。现在按数据形态自动选路：

      * `00_raw/` 还有待整理的图 -> 老路径：按图源时间编号，搬进 `images/`（move:false 为复制）；
      * `00_raw/` 空了（成品都在桶里）-> **桶内就地重排**：按桶分组、桶内按图源时间排序，
        文件**不跨桶搬**（桶归属是用户的分组语义），只把一个桶里的编号重排干净；
      * 显式给 `workSet` -> 只重排这几个桶（此时 00_raw/ 不动：显式意图优先）。

    编号为什么跨桶连续、而不是每个桶都从 `start` 重来：`_pipeline/thumbs`、`masks`、
    `orig_text` 全是**按 stem 扁平命名**的（`0001.jpg` / `0001.mask.png`），跨桶重号会让
    两张不同图的缩略图/掩膜/修补前备份互相踩，`text --inpaint-only` 与 `text-rollback`
    就会张冠李戴。所以每个桶各占一段、段与段不重叠：clean 0001…0109、watermark 0110…0142。

    `_pipeline/rename_map.csv` 仍然只追加、记 `old_name -> new_name`，另加 `bucket` 列
    （老表没这列照样读；同名旧行按桶区分，clean/a.png 与 watermark/a.png 不会互相顶掉）。
    """
    ds.ensure_dirs()
    meta = load_meta(ds)

    # ---- 这一跑处理「00_raw 待整理」还是「成品图桶」 --------------------------
    buckets: list[tuple[str, Path]] = []
    raw: list[Path] = []
    if work_set:
        buckets = ds.bucket_dirs(work_set)   # 给错名字在这里就报错并列出自动发现的桶
        leftover = ds.raw_images()
        if leftover:
            print(f"[rename] 指定了 workSet={work_set}：只重排这些桶，"
                  f"00_raw/ 里还有 {len(leftover)} 张这次不动")
    else:
        raw = ds.raw_images()
        strays = ds.stray_images()
        if not raw:
            buckets = ds.discover_buckets()
            if not buckets and strays:
                # 根目录散图 = 整个数据集对工具隐形（mignon 复盘最大坑）。这里当作
                # 待整理素材并入 images/：rename 本来就是「把散图重排成 0001…」的入口，
                # 边搬边编号，sidecar .txt 也跟着走。--apply 才真搬，dry-run 只预演。
                raw = strays
                print(f"[rename] 根目录发现 {len(strays)} 张散图（不在任何图桶里）——"
                      f"当作待整理素材并入 images/{'（move）' if move else '（复制）'}")
            elif buckets and strays:
                print(f"[rename] ⚠️ 根目录另有 {len(strays)} 张散图不属于任何图桶，本次不处理；"
                      "先 mv 进某个图桶（或 00_raw/），或对整个数据集重跑 rename（不传 workSet）")
    if not raw and not buckets:
        raise SystemExit("[rename] 00_raw 为空，也没有含图的桶（images/、clean/、watermark/ …），"
                         "根目录也没有散图——先下载或导入图片")

    rows: list[dict] = []
    ranges: list[dict] = []
    plan: list[tuple[Path, Path]] = []

    if raw:
        files = _rename_order(meta, raw)
        for idx, f in enumerate(files):
            n = start + idx
            new_name = f"{n:0{digits}d}{f.suffix.lower()}"
            rows.append(_rename_row(f, new_name, meta, "images"))
            plan.append((f, ds.images_dir / new_name))
        ranges.append({"bucket": "images", "count": len(files), "first": start,
                       "last": start + len(files) - 1})
    else:
        n = start
        for name, base in buckets:
            files = _rename_order(meta, [p for p in base.iterdir()
                                         if p.is_file() and p.suffix.lower() in IMG_EXTS])
            if not files:
                continue
            first = n
            for f in files:
                new_name = f"{n:0{digits}d}{f.suffix.lower()}"
                rows.append(_rename_row(f, new_name, meta, name))
                plan.append((f, base / new_name))
                n += 1
            ranges.append({"bucket": name, "count": len(files), "first": first, "last": n - 1})

    if not raw and work_set:
        # 只重排部分桶时，编号可能撞上没参与这次重排的桶（thumbs/masks/orig_text 按 stem 平铺）
        selected = {name for name, _ in buckets}
        other_stems = {p.stem for name, base in ds.discover_buckets() if name not in selected
                       for p in base.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXTS}
        clash = sorted({dest.stem for src, dest in plan if src != dest} & other_stems)
        if clash:
            raise SystemExit(
                "[rename] 编号会撞上没参与这次重排的桶：" + ", ".join(clash[:8])
                + (" …" if len(clash) > 8 else "") + "\n"
                "         缩略图/掩膜/修补备份是按 stem 扁平存的，跨桶重号会张冠李戴。\n"
                "         换一个 --start 避开这些编号，或对整个数据集重排（不传 workSet）。")

    total_rows = len(rows)
    first, last = start, start + total_rows - 1
    if not apply:
        # 预演不碰权威对照表（load_rename_map 读的就是它，别让预演污染下游的标签来源）
        preview = ds.pipe_dir / "rename_map.preview.csv"
        write_csv(preview, rows)
        _print_rename_ranges(ranges, digits, in_buckets=not raw)
        print(f"[rename] 预演：{total_rows} 张将重排；对照表 {preview}"
              "（加 --apply 执行；权威表 rename_map.csv 不动）")
        return {"count": total_rows, "map": str(preview), "applied": False, "ranges": ranges}

    sidecars = 0
    if raw:
        for src, dest in plan:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if move:
                shutil.move(str(src), str(dest))
            else:
                shutil.copy2(src, dest)
            # 原图旁边的 .txt（早期手工补的 / import 带进来的）跟着搬，别留在 00_raw 里成孤儿
            side = src.with_suffix(".txt")
            if side.exists():
                target = dest.with_suffix(".txt")
                if not target.exists():
                    try:
                        if move:
                            shutil.move(str(side), str(target))
                        else:
                            shutil.copy2(side, target)
                        sidecars += 1
                    except Exception:
                        pass
    else:
        if not move:
            print("[rename] 桶内重排是就地改名（move 只对 00_raw -> images 那批有意义，这里仍就地）")
        sidecars = _rename_in_place(plan)

    # 只追加（真实使用踩过）：早先这里直接覆盖 rename_map.csv，第二批 rename
    # （--start 27）一跑，第一批 26 行的 post_id/tags_full 就没了。
    map_path = ds.pipe_dir / "rename_map.csv"
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    new_olds = {(r["bucket"], r["old_name"]) for r in rows}
    new_old_names = {r["old_name"] for r in rows}
    if map_path.exists():
        with open(map_path, encoding="utf-8", newline="") as fh:
            for old_row in csv.DictReader(fh):
                old = old_row.get("old_name", "")
                if not old:
                    continue
                b = str(old_row.get("bucket") or "").strip()
                # 同一张图重排过：以本次为准。老表没有 bucket 列时按旧名认；
                # 有桶列时按 (桶, 旧名) 认，免得 clean/a.png 与 watermark/a.png 互相顶掉。
                if (b, old) in new_olds if b else old in new_old_names:
                    continue
                if (b, old) in seen:
                    continue
                seen.add((b, old))
                merged.append(old_row)
    merged.extend(rows)
    write_csv(map_path, merged)
    batch_path = ds.pipe_dir / f"rename_map.{first:0{digits}d}-{last:0{digits}d}.csv"
    write_csv(batch_path, rows)
    pruned = _prune_batch_maps(ds)
    if pruned:
        print(f"[rename] 清理了 {pruned} 份过老的批次快照（只留最近 "
              f"{state.hygiene(ds, 'rename_snapshots', 3)} 份；"
              f"权威表 {map_path.name} 是累计的，不受影响）")

    st = state.load(ds)
    for row in rows:
        facts = dict(post_id=row["post_id"], source=row["source"],
                     created_at=row["created_at"], width=row["width"],
                     height=row["height"], url=row["url"], bucket=row["bucket"],
                     origin="A+booru" if row["tags_full"] else "A=none")
        if row["old_name"] == row["new_name"]:
            # 编号没变（重跑同一批）：别把新名字当旧名塞进 old_names
            st.record(row["new_name"], history={"what": "rename", "note": "编号未变"}, **facts)
        else:
            st.link_rename(row["new_name"], row["old_name"],
                           history={"what": "rename", "from": row["old_name"]}, **facts)
    st.add_batch(first, last, total_rows, batch_path.name)
    st.save()

    _print_rename_ranges(ranges, digits, in_buckets=not raw)
    print(f"         对照表 {map_path}（累计 {len(merged)} 行，只追加）　本批快照 {batch_path.name}")
    if sidecars:
        print(f"         随图搬运的 .txt 边车：{sidecars} 个")
    print(f"         状态 {st.path.name}（per_image.json：编号/来源/post_id/旧名的唯一事实来源）")
    return {"count": total_rows, "map": str(map_path), "batch": str(batch_path),
            "rows": len(merged), "state": str(st.path), "applied": True, "ranges": ranges}


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
