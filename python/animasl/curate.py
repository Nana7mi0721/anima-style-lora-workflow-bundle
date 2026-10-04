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

from . import net

IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz"}

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
    """Images live in 00_raw until `rename` has run, then in images/."""
    if prefer_images and ds.images_dir.exists() and any(ds.images_dir.iterdir()):
        return ds.images_dir
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

    staging = ds.pipe_dir / "_import_staging"
    items: list[Path] = []

    def collect(p: Path) -> None:
        if p.is_dir():
            if unpack and p.suffix.lower() in ARCHIVE_EXTS:
                return
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
    print(f"[import] 候选文件 {len(items)}，其中图片 {len(images)}")

    used = {p.name for p in ds.raw_dir.iterdir()} if ds.raw_dir.exists() else set()
    rows: list[dict] = []
    copied = 0
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
        rows.append(entry)

    if rows and not dry_run:
        existing = net.read_jsonl(ds.pipe_dir / "raw_posts.jsonl")
        known = {(r.get("filename"), r.get("origin")) for r in existing}
        merged = existing + [r for r in rows if (r.get("filename"), r.get("origin")) not in known]
        net.write_jsonl(ds.pipe_dir / "raw_posts.jsonl", merged)
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    print(f"[import] 导入 {copied} 张 -> {ds.raw_dir}")
    return {"rows": rows, "count": copied}


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
              apply: bool = False, include_images: bool = False) -> dict:
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
    files = ds.images() if include_images else ds.raw_images()
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
               include_images: bool = False) -> dict:
    ds.ensure_dirs()
    rules = rules or {}
    min_short = int(rules.get("min_short_side", 512))
    min_bytes = int(rules.get("min_bytes", 102400))
    earliest = str(rules.get("earliest_date", "2015-01-01"))
    sketch_tags = {t.lower() for t in rules.get("sketch_tags", sorted(DEFAULT_SKETCH_TAGS))}

    files = ds.images() if include_images else ds.raw_images()
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
               include_images: bool = False) -> dict:
    ds.ensure_dirs()
    ds.thumbs_dir.mkdir(parents=True, exist_ok=True)
    files = ds.images() if include_images else ds.raw_images()
    written = 0
    total = 0
    for f in files:
        out = ds.thumbs_dir / (f.stem + ".jpg")
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
    print(f"[thumbs] {written} 张 -> {ds.thumbs_dir}"
          f"（平均 {total // max(written, 1) // 1024} KB，长边≤{max_side}）")
    return {"count": written, "dir": str(ds.thumbs_dir)}


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

    write_csv(ds.pipe_dir / "rename_map.csv", rows)
    if apply:
        print(f"[rename] {len(rows)} 张已重排为 {start:0{digits}d}…{start + len(rows) - 1:0{digits}d}"
              f" -> {ds.images_dir}")
    else:
        print(f"[rename] 预演：{len(rows)} 张将重排；对照表 {ds.pipe_dir / 'rename_map.csv'}"
              f"（加 --apply 执行）")
    return {"count": len(rows), "map": str(ds.pipe_dir / "rename_map.csv")}


def load_rename_map(ds) -> dict[str, dict]:
    path = ds.pipe_dir / "rename_map.csv"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8", newline="") as fh:
        return {r["new_name"]: r for r in csv.DictReader(fh)}
