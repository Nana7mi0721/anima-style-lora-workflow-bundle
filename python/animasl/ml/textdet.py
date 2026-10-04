#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Manga / illustration text + bubble detector -> full-resolution inpaint mask.

Wraps the fine-tuned ``RFDETRSeg2XLarge`` checkpoint that ships with the koharu
app (4 classes: ``text``, ``onomatopoeia``, ``bubble``, ``panel``) and turns its
segmentation output into an 8-bit single-channel PNG at the ORIGINAL image
resolution:

    255 = "please inpaint / remove this"      0 = keep

Self-contained: runnable as a plain script, no imports from the surrounding
package.

    python textdet.py --image page.png --out-dir out/ \\
        --thresholds text=0.25,onomatopoeia=0.2,bubble=0.5,panel=0.5 \\
        --device cuda --classes text,onomatopoeia --dilate 6

Prints exactly one JSON object on stdout; all progress/diagnostics go to stderr.

Inference strategy (--mode)
---------------------------
The network has a fixed 1152 input.  Feeding it a whole 5000-8000 px dataset
page means a 6x downscale, and measurement on real images shows that destroys
small text completely::

    wm_01.jpg 5130x7350   single-pass: text=0  ono=0  bub=0
                          tiled:       text=11 ono=17 bub=7

So ``--mode auto`` (the default) runs the model as a native-resolution sliding
window whenever the image is bigger than one tile, which keeps glyphs at their
true pixel size and yields the exact-resolution masks we want anyway.
``--mode single`` reproduces the plain downscale-to-1152 behaviour.

Why the checkpoint needs a conversion step
------------------------------------------
The koharu artefact is a bare ``model.safetensors`` (tensors + metadata), while
``rfdetr``'s loader expects a pickled dict ``{"model": state_dict, "args": {...}}``
built by its training stack.  ``_ensure_torch_checkpoint`` rebuilds that envelope
once and caches it next to this package.

The checkpoint is a *fine-tune*: it changes the published ``RFDETRSeg2XLarge``
defaults (resolution 768 -> 1152, num_select 300 -> 160, num_classes 90 -> 4).
All three are overriding constructor arguments below, and the remaining
architecture knobs (patch_size 12, dec_layers 6, num_queries 300, group_detr 13,
num_windows 2) already match the published config exactly.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
from PIL import Image, ImageFile

# A 30-50 MB / 6000 px PNG is a normal dataset page; PIL's decompression-bomb
# guard and its strict truncation check both get in the way of that.
ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

CLASSES = ("text", "onomatopoeia", "bubble", "panel")
DEFAULT_THRESHOLDS = {"text": 0.25, "onomatopoeia": 0.2, "bubble": 0.5, "panel": 0.5}
DEFAULT_CLASSES = ("text", "onomatopoeia")

ARCH = "RFDETRSeg2XLarge"

#: Where the koharu app keeps this snapshot (used when --ckpt is not given).
KNOWN_CKPT_DIRS = (
    r"D:\Program\koharu\store\hugging-face\models"
    r"\mayocream--koharu-layout-rfdetr-seg-2xl-1152\snapshots"
    r"\aed55fdb8ca953c6bec33cf6ed6dd52a9b72bfa2",
)

#: Cache for the converted torch checkpoint: <python>/models/
CACHE_DIR = Path(__file__).resolve().parents[2] / "models"


def _log(msg: str) -> None:
    """Progress/diagnostics -> stderr, so stdout stays pure JSON."""
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# argument parsing helpers
# --------------------------------------------------------------------------- #


def parse_thresholds(spec: str) -> dict:
    """``"text=0.25,bubble=0.5"`` -> ``{"text": 0.25, "bubble": 0.5}``."""
    out = dict(DEFAULT_THRESHOLDS)
    if not spec:
        return out
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"bad --thresholds entry {chunk!r}; expected name=value")
        name, _, raw = chunk.partition("=")
        name = name.strip()
        if name not in CLASSES:
            raise ValueError(f"unknown class {name!r} in --thresholds; known: {list(CLASSES)}")
        out[name] = float(raw)
    return out


def parse_classes(spec: str) -> list:
    """``"text,onomatopoeia"`` -> ``["text", "onomatopoeia"]`` (order preserved)."""
    names = [c.strip() for c in (spec or "").split(",") if c.strip()]
    if not names:
        raise ValueError("--classes resolved to an empty list")
    for n in names:
        if n not in CLASSES:
            raise ValueError(f"unknown class {n!r}; known: {list(CLASSES)}")
    return names


def resolve_ckpt(arg: str | None) -> Path:
    """Locate ``model.safetensors`` from --ckpt, $ANIMASL_RFDETR_CKPT, or defaults."""
    cands = []
    if arg:
        cands.append(Path(arg))
    env = os.environ.get("ANIMASL_RFDETR_CKPT")
    if env:
        cands.append(Path(env))
    cands.extend(Path(p) for p in KNOWN_CKPT_DIRS)

    for c in cands:
        if c.is_dir():
            st = c / "model.safetensors"
            if st.is_file():
                return st
        elif c.is_file():
            return c
    raise FileNotFoundError(
        "could not locate the RF-DETR checkpoint; pass --ckpt <model.safetensors|dir>. "
        f"Tried: {[str(c) for c in cands]}"
    )


# --------------------------------------------------------------------------- #
# checkpoint conversion (safetensors -> rfdetr's {"model","args"} envelope)
# --------------------------------------------------------------------------- #


def _ensure_torch_checkpoint(st_path: Path, cache_dir: Path) -> Path:
    """Convert the bare safetensors checkpoint into the dict rfdetr's loader wants.

    ``rfdetr.models.weights.load_pretrain_weights`` does ``torch.load()`` and reads
    ``checkpoint["model"]`` (+ optional ``checkpoint["args"]``), so a raw
    safetensors file cannot be passed straight through.  The conversion is cached
    and only redone when the safetensors file is newer.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / (st_path.stem + ".rfdetr.pt")

    try:
        if out.is_file() and out.stat().st_mtime >= st_path.stat().st_mtime and out.stat().st_size > 0:
            _log(f"[ckpt] using cached torch checkpoint {out}")
            return out
    except OSError:
        pass

    import torch
    from safetensors import safe_open

    _log(f"[ckpt] converting {st_path.name} -> {out.name} (one-off, ~10 s)")
    t0 = time.time()
    state, meta = {}, {}
    with safe_open(str(st_path), framework="pt") as f:
        meta = dict(f.metadata() or {})
        for k in f.keys():
            state[k] = f.get_tensor(k)

    # Class head carries num_classes+1 rows (background/no-object slot).
    if "class_embed.bias" not in state:
        raise RuntimeError("checkpoint has no 'class_embed.bias'; not an RF-DETR detection head")
    n_rows = int(state["class_embed.bias"].shape[0])
    num_classes = max(1, n_rows - 1)

    # num_queries * group_detr == rows of refpoint_embed.weight (300 * 13 == 3900).
    group_detr = 13
    total_q = int(state["refpoint_embed.weight"].shape[0]) if "refpoint_embed.weight" in state else 300 * 13
    num_queries = total_q // group_detr if total_q % group_detr == 0 else total_q

    class_names = None
    raw = meta.get("class_names")
    if raw:
        try:
            class_names = json.loads(raw)
        except Exception:
            class_names = None

    args = {
        "num_classes": num_classes,
        "num_queries": num_queries,
        "group_detr": group_detr,
        "resolution": int(meta.get("resolution", 1152)),
        "num_select": int(meta.get("num_select", 160)),
        "class_names": class_names,
        "format": meta.get("format", "pt"),
        # Let rfdetr's from_checkpoint()-style inference recognise the variant.
        "model_name": ARCH,
        "pretrain_weights": "rf-detr-seg-xxlarge.pt",
    }
    _log(
        f"[ckpt] meta={{{', '.join(f'{k}={meta[k]!r}' for k in sorted(meta))}}} "
        f"-> num_classes={num_classes} num_queries={num_queries} group_detr={group_detr} "
        f"resolution={args['resolution']} num_select={args['num_select']}"
    )

    tmp = out.with_suffix(out.suffix + ".tmp")
    torch.save({"model": state, "args": args}, tmp)
    os.replace(tmp, out)
    _log(f"[ckpt] wrote {out} in {time.time() - t0:.1f}s")
    return out


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #


def load_model(ckpt_pt: Path, device: str):
    """Build RFDETRSeg2XLarge with the fine-tune's overrides and load the weights."""
    import torch
    from rfdetr import RFDETRSeg2XLarge

    logging.getLogger("rfdetr").setLevel(logging.ERROR)

    _log(f"[model] constructing {ARCH} (device={device}) from {ckpt_pt.name}")
    t0 = time.time()
    model = RFDETRSeg2XLarge(
        pretrain_weights=str(ckpt_pt),
        num_classes=4,
        resolution=1152,
        num_select=160,
        device=device,
    )
    # predict() defers .to(device); do it now so the first call isn't paying for it.
    inner = getattr(model, "model", None)
    if inner is not None and getattr(inner, "model", None) is not None:
        inner.model = inner.model.to(torch.device(device))
        inner.model.eval()
    _log(f"[model] ready in {time.time() - t0:.1f}s")
    return model


# --------------------------------------------------------------------------- #
# image i/o
# --------------------------------------------------------------------------- #


def load_image_rgb(path: Path) -> Image.Image:
    """Open any PNG/JPEG the datasets throw at us and return an RGB image.

    Tolerates CMYK, palette, greyscale, 16-bit and truncated files.
    """
    with Image.open(path) as im:
        im.load()  # force the decode so a broken file fails here, not later
        mode = im.mode

        if mode in ("I;16", "I;16B", "I;16L", "I;16N", "I", "F"):
            arr = np.asarray(im)
            if arr.dtype == np.uint16 or (arr.size and float(arr.max()) > 255.0):
                mx = float(arr.max()) or 1.0
                arr = (arr.astype(np.float32) * (255.0 / mx)).clip(0, 255).astype(np.uint8)
            else:
                arr = arr.astype(np.uint8)
            return Image.fromarray(arr, mode="L").convert("RGB")

        if mode != "RGB":
            im = im.convert("RGB")
        else:
            im = im.copy()
    return im


# --------------------------------------------------------------------------- #
# mask assembly
# --------------------------------------------------------------------------- #


def _resize_bool(mask_u8: np.ndarray, w: int, h: int) -> np.ndarray:
    """Bilinear up/downscale of a 0/255 mask, re-thresholded at 50 %."""
    if mask_u8.shape == (h, w):
        return mask_u8 >= 128
    resized = Image.fromarray(mask_u8, mode="L").resize((w, h), Image.BILINEAR)
    return np.asarray(resized) >= 128


def dilate_mask(mask_u8: np.ndarray, radius: int) -> np.ndarray:
    """Grow the mask by *radius* px so glyph anti-aliasing/halo is covered."""
    if radius <= 0:
        return mask_u8
    try:
        import cv2

        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
        return cv2.dilate(mask_u8, k, iterations=1)
    except Exception:  # pragma: no cover - cv2 is present in our venv
        from PIL import ImageFilter

        size = 2 * radius + 1
        if size < 3:
            size = 3
        im = Image.fromarray(mask_u8, mode="L").filter(ImageFilter.MaxFilter(size))
        return np.asarray(im)


def _tile_origins(total: int, tile: int, overlap: int) -> list:
    """Start offsets covering ``total`` px with the given tile/overlap."""
    if total <= tile:
        return [0]
    stride = max(1, tile - overlap)
    pos, out = 0, []
    while True:
        pos = min(pos, max(0, total - tile))
        if not out or out[-1] != pos:
            out.append(pos)
        if pos + tile >= total:
            break
        pos += stride
    return out


def _iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    ub = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    denom = ua + ub - inter
    return inter / denom if denom > 0 else 0.0


def _nms(records: list, iou_thr: float = 0.6) -> list:
    """Drop duplicate boxes that overlapping tiles reported twice (per class)."""
    keep = []
    for lab in {r["label"] for r in records}:
        idx = [i for i, r in enumerate(records) if r["label"] == lab]
        idx.sort(key=lambda i: -records[i]["score"])
        while idx:
            a = idx.pop(0)
            keep.append(a)
            idx = [i for i in idx if _iou(records[a]["box"], records[i]["box"]) < iou_thr]
    return [records[i] for i in sorted(keep)]


def _infer_crop(model, crop, gate: float):
    """One forward pass. Returns (xyxy, confidence, labels, masks-or-None)."""
    import torch

    with torch.no_grad():
        dets = model.predict(crop, threshold=gate, include_source_image=False)
    xyxy = np.asarray(getattr(dets, "xyxy", np.zeros((0, 4))), dtype=np.float64).reshape(-1, 4)
    conf = np.asarray(getattr(dets, "confidence", np.zeros((0,))), dtype=np.float64).reshape(-1)
    names = None
    for src in (getattr(dets, "data", None), getattr(dets, "metadata", None)):
        if isinstance(src, dict) and "class_name" in src:
            try:
                names = [str(x) for x in src["class_name"]]
                break
            except Exception:
                names = None
    labels = []
    for i in range(len(xyxy)):
        if names is not None and i < len(names):
            labels.append(names[i])
        else:
            cid = np.asarray(getattr(dets, "class_id", np.zeros((0,))), dtype=np.int64).reshape(-1)
            c = int(cid[i]) if i < len(cid) else 0
            labels.append(CLASSES[c] if 0 <= c < len(CLASSES) else f"class_{c}")
    masks = getattr(dets, "mask", None)
    masks = None if masks is None else np.asarray(masks)
    if masks is not None and masks.ndim == 4:  # (N, 1, H, W)
        masks = masks[:, 0]
    return xyxy, conf, labels, masks


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="textdet.py",
        description="Detect manga text/bubbles and write a full-resolution inpaint mask.",
    )
    p.add_argument("--image", required=True, help="input image (PNG/JPEG, any size)")
    p.add_argument("--out-dir", required=True, help="directory for <stem>.mask.png")
    p.add_argument(
        "--thresholds",
        default=",".join(f"{k}={v}" for k, v in DEFAULT_THRESHOLDS.items()),
        help="per-class score thresholds, e.g. text=0.25,onomatopoeia=0.2,bubble=0.5,panel=0.5",
    )
    p.add_argument(
        "--classes",
        default=",".join(DEFAULT_CLASSES),
        help="classes to include in the mask (default: text,onomatopoeia)",
    )
    p.add_argument("--dilate", type=int, default=6, help="mask dilation radius in px (default 6)")
    p.add_argument("--device", default=None, choices=["cuda", "cpu"], help="default: cuda if available")
    p.add_argument("--ckpt", default=None, help="model.safetensors file or its directory")
    p.add_argument("--cache-dir", default=None, help="where the converted .pt is cached")
    p.add_argument("--infer-size", type=int, default=1152, help="max side fed to the network (single mode)")
    p.add_argument(
        "--mode",
        default="auto",
        choices=["auto", "single", "tile"],
        help="auto (default): tile when the image exceeds one tile, else single. "
        "single: downscale the whole image to --infer-size.",
    )
    p.add_argument("--tile", type=int, default=1152, help="tile size for tiled mode (default 1152)")
    p.add_argument("--overlap", type=int, default=192, help="tile overlap in px (default 192)")
    p.add_argument("--json-only", action="store_true", help="suppress progress output on stderr")
    p.add_argument("--save-overlay", action="store_true", help="also write <stem>.overlay.png")
    p.add_argument("--optimize", action="store_true", help="call model.optimize_for_inference() first")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    global _log
    if args.json_only:
        _log = lambda *_a, **_k: None  # noqa: E731

    t_start = time.time()

    import torch

    thresholds = parse_thresholds(args.thresholds)
    want_classes = parse_classes(args.classes)

    device = args.device
    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda" and not torch.cuda.is_available():
        _log("[warn] cuda requested but unavailable; falling back to cpu")
        device = "cpu"

    img_path = Path(args.image)
    if not img_path.is_file():
        raise FileNotFoundError(f"no such image: {img_path}")

    # ---- image ---------------------------------------------------------- #
    t0 = time.time()
    img = load_image_rgb(img_path)
    full_w, full_h = img.size
    if full_w == 0 or full_h == 0:
        raise ValueError(f"image has zero extent: {img_path}")
    _log(f"[img] {img_path.name} {full_w}x{full_h} mode={img.mode} loaded in {time.time() - t0:.2f}s")

    # ---- model ---------------------------------------------------------- #
    st_path = resolve_ckpt(args.ckpt)
    cache_dir = Path(args.cache_dir) if args.cache_dir else CACHE_DIR
    ckpt_pt = _ensure_torch_checkpoint(st_path, cache_dir)
    model = load_model(ckpt_pt, device)

    if args.optimize:
        try:
            model.optimize_for_inference()
            _log("[model] optimize_for_inference() done")
        except Exception as exc:  # non-fatal
            _log(f"[model] optimize_for_inference() failed, continuing: {exc}")

    # predict() applies one threshold to every class, so run at the loosest
    # threshold we care about and apply the per-class cut afterwards.
    gate = min(thresholds[c] for c in want_classes)
    _log(f"[run] classes={want_classes} thresholds={ {c: thresholds[c] for c in want_classes} } gate={gate}")

    tile = max(256, int(args.tile))
    mode = args.mode
    if mode == "auto":
        # Tiling only pays off once a single pass would have to discard real
        # resolution.  At <=1.25x the tile size a plain downscale actually
        # measures *better*, because the model squares every input up and a
        # non-square crop therefore gets aspect-distorted (975x1200 scored
        # 4 detections as one downscaled pass vs 1 as two tiles).
        mode = "tile" if max(full_w, full_h) > tile * 1.25 else "single"
    _log(f"[run] mode={mode} tile={tile} overlap={args.overlap if mode == 'tile' else 0}")

    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()

    union = np.zeros((full_h, full_w), dtype=bool)
    records: list = []
    raw_total = 0

    def _absorb(xyxy, conf, labels, masks, ox, oy, cw, ch, inv):
        """Filter one forward pass, record global boxes, composite its masks."""
        nonlocal raw_total
        raw_total += len(xyxy)
        for i in range(len(xyxy)):
            lab = labels[i]
            if lab not in want_classes or conf[i] < thresholds.get(lab, 0.5):
                continue
            x1 = float(np.clip(xyxy[i][0] * inv + ox, 0, full_w))
            y1 = float(np.clip(xyxy[i][1] * inv + oy, 0, full_h))
            x2 = float(np.clip(xyxy[i][2] * inv + ox, 0, full_w))
            y2 = float(np.clip(xyxy[i][3] * inv + oy, 0, full_h))
            records.append(
                {
                    "label": lab,
                    "score": round(float(conf[i]), 4),
                    "box": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                    "area_ratio": round(max(0.0, x2 - x1) * max(0.0, y2 - y1) / float(full_w * full_h), 6),
                }
            )
            if masks is not None and i < masks.shape[0]:
                mm = masks[i]
                mm = (mm.astype(np.uint8) * 255) if mm.dtype == bool else mm.astype(np.uint8)
                union[oy:oy + ch, ox:ox + cw] |= _resize_bool(mm, cw, ch)
            else:
                # Detection-only checkpoint: fall back to the filled box.
                a, b = int(np.floor(x1)), int(np.floor(y1))
                c, d = int(np.ceil(x2)), int(np.ceil(y2))
                if c > a and d > b:
                    union[b:d, a:c] = True

    t0 = time.time()

    if mode == "single":
        scale = 1.0
        if max(full_w, full_h) > args.infer_size:
            scale = args.infer_size / float(max(full_w, full_h))
            small = img.resize((max(1, round(full_w * scale)), max(1, round(full_h * scale))), Image.LANCZOS)
        else:
            small = img
        _log(f"[img] single pass {full_w}x{full_h} -> {small.size[0]}x{small.size[1]}")
        xyxy, conf, labels, masks = _infer_crop(model, small, gate)
        _absorb(xyxy, conf, labels, masks, 0, 0, full_w, full_h, (1.0 / scale) if scale != 1.0 else 1.0)
    else:
        xs = _tile_origins(full_w, tile, int(args.overlap))
        ys = _tile_origins(full_h, tile, int(args.overlap))
        _log(f"[img] tiled pass {len(xs)}x{len(ys)} = {len(xs) * len(ys)} tiles of {tile}px")
        for oy in ys:
            for ox in xs:
                cw, ch = min(tile, full_w - ox), min(tile, full_h - oy)
                if cw < 32 or ch < 32:
                    continue
                crop = img.crop((ox, oy, ox + cw, oy + ch))
                xyxy, conf, labels, masks = _infer_crop(model, crop, gate)
                _absorb(xyxy, conf, labels, masks, ox, oy, cw, ch, 1.0)

    infer_s = time.time() - t0
    n_raw = len(records)
    records = _nms(records)
    _log(
        f"[run] {raw_total} raw -> {n_raw} above threshold -> {len(records)} after cross-tile NMS "
        f"({infer_s:.2f}s)"
    )

    # ---- mask ----------------------------------------------------------- #
    t0 = time.time()
    mask_u8 = (union.astype(np.uint8)) * 255
    mask_u8 = dilate_mask(mask_u8, int(args.dilate))
    text_area_ratio = float((mask_u8 > 0).mean()) if mask_u8.size else 0.0
    _log(f"[mask] assembled+dilated in {time.time() - t0:.2f}s coverage={text_area_ratio:.4f}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mask_path = out_dir / f"{img_path.stem}.mask.png"
    Image.fromarray(mask_u8, mode="L").save(mask_path)

    if args.save_overlay:
        base = np.asarray(img.convert("RGB")).copy()
        sel = mask_u8 > 0
        base[sel] = (base[sel] * 0.6 + np.array([255, 0, 0]) * 0.4).astype(np.uint8)
        Image.fromarray(base).save(out_dir / f"{img_path.stem}.overlay.png")

    seconds = time.time() - t_start
    result = {
        "image": str(args.image),
        "width": full_w,
        "height": full_h,
        "classes_used": want_classes,
        "detections": records,
        "text_area_ratio": round(text_area_ratio, 6),
        "mask_path": str(mask_path),
        "seconds": round(seconds, 3),
        "device": device,
    }

    if device == "cuda":
        try:
            _log(
                f"[gpu] peak VRAM {torch.cuda.max_memory_allocated() / 2**20:.0f} MiB alloc / "
                f"{torch.cuda.max_memory_reserved() / 2**20:.0f} MiB reserved"
            )
        except Exception:
            pass
    _log(f"[done] total {seconds:.2f}s")

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
