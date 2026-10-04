#!/usr/bin/env python
"""Text / watermark removal for anime illustrations with a Big-LaMa checkpoint.

Standalone CLI (no imports from the rest of the package beyond the sibling
``lama_arch`` module):

    python inpaint.py --image in.png --mask m.png --out out.png [--device cuda|cpu]
                      [--pad-to 8] [--dilate 0]

* ``--mask`` is an 8-bit grayscale PNG at the same resolution as the image;
  every non-zero pixel marks a region to erase.  A mask of a different size is
  rejected (``--allow-mask-resize`` scales it with NEAREST instead).
* The image is written back at the original resolution, RGB, same container
  format as the input.
* One JSON object is printed on stdout.

Default checkpoint:
``D:\\Program\\koharu\\store\\hugging-face\\models\\mayocream--lama-manga\\
snapshots\\f91c85b26913b3e83f9877867b4c336da3675238\\lama-manga.safetensors``
(fp32, 989 tensors, ``FFCResNetGenerator`` 18-block "large arch" LaMa, loaded
with ``strict=True``).

Execution modes (reported in the JSON as ``mode``):

``full``      one forward pass over the whole image.
``tiled``     the image is walked cell by cell (only cells that own mask pixels
              are processed) with ``--margin`` px of surrounding context; each
              cell is padded to a multiple of ``--pad-to``.  Output is still
              full resolution.
``tiled+downscale``
              as ``tiled``, but a single crop would not fit in memory, so that
              crop is inpainted at a reduced scale and the result is resized
              back.  Only masked pixels ever come from the model, everything
              else is copied from the source image.

Adapted from the LaMa reference implementation (advimman/lama, Apache-2.0) and
the inference convention used by IOPaint / manga-image-translator: input RGB in
[0, 1], mask in {0, 1}, ``img * (1 - mask)`` concatenated with the mask, fp32.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import torch
from PIL import Image

try:  # normal package import
    from .lama_arch import build_lama_manga
except ImportError:  # running the file directly / as a script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from lama_arch import build_lama_manga

DEFAULT_CHECKPOINT = (
    r"D:\Program\koharu\store\hugging-face\models\mayocream--lama-manga"
    r"\snapshots\f91c85b26913b3e83f9877867b4c336da3675238\lama-manga.safetensors"
)

# Cell/margin defaults.  LaMa is trained on random crops of a few hundred px, so a
# ~1k crop with a few hundred px of context is well inside its comfort zone; the
# FFC global branch also sees the whole crop, which is what we want for texture.
DEFAULT_CELL = 1024
DEFAULT_MARGIN = 256
DEFAULT_MAX_RUN = 2048


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
def _log(msg: str) -> None:
    print(f"[inpaint] {msg}", file=sys.stderr, flush=True)


def load_image(path: str) -> Image.Image:
    img = Image.open(path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def load_mask(path: str, size: tuple[int, int], allow_resize: bool = False) -> np.ndarray:
    """Return a bool array (H, W), True = erase. Non-zero pixels are the erased region."""
    m = Image.open(path)
    if m.size != size:
        if not allow_resize:
            # A silently resized mask erases the wrong pixels (and a small mask scaled up
            # can end up covering the whole image), so refuse unless asked explicitly.
            raise SystemExit(
                f"mask {m.size[0]}x{m.size[1]} does not match image {size[0]}x{size[1]}; "
                "the mask must be at the same resolution as the image "
                "(pass --allow-mask-resize to scale it with NEAREST anyway)"
            )
        _log(f"mask {m.size} != image {size}; resizing mask with NEAREST (--allow-mask-resize)")
        m = m.resize(size, Image.NEAREST)
    if m.mode not in ("L", "1", "I", "F"):
        m = m.convert("L")
    arr = np.asarray(m)
    if arr.ndim == 3:  # e.g. RGBA mask
        arr = arr[..., :3].max(axis=2)
    return arr > 0


def dilate_mask(mask: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return mask
    import cv2

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    return cv2.dilate(mask.astype(np.uint8), k, iterations=1).astype(bool)


def _reflect_pad_to(x: torch.Tensor, multiple: int) -> tuple[torch.Tensor, int, int]:
    """Pad the last two dims up to a multiple of ``multiple`` with reflection."""
    h, w = x.shape[-2:]
    ph = (multiple - h % multiple) % multiple
    pw = (multiple - w % multiple) % multiple
    if ph or pw:
        # reflection needs pad < dim; fall back to replicate for tiny crops
        mode = "reflect" if (ph < h and pw < w) else "replicate"
        x = torch.nn.functional.pad(x, (0, pw, 0, ph), mode=mode)
    return x, ph, pw


def _grid_cells(mask: np.ndarray, cell: int):
    """Fallback splitter: non-overlapping `cell` x `cell` cells owning >=1 mask pixel."""
    h, w = mask.shape
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return
    cy0, cy1 = int(ys.min()), int(ys.max()) + 1
    cx0, cx1 = int(xs.min()), int(xs.max()) + 1
    for y0 in range(cy0 // cell * cell, cy1, cell):
        for x0 in range(cx0 // cell * cell, cx1, cell):
            y1, x1 = min(y0 + cell, h), min(x0 + cell, w)
            if mask[y0:y1, x0:x1].any():
                yield y0, y1, x0, x1


def _regions_for_mask(mask: np.ndarray, limit: int, cell: int = 1024):
    """Split the mask into non-overlapping rectangles that each fit one model run.

    Connected components are used first: one text block / watermark normally becomes one
    pass, which keeps a whole erased region inside a single prediction (no seams inside a
    hole) and keeps the number of passes low. Regions wider/taller than `limit` are halved
    recursively. Each yielded rectangle covers its mask pixels exactly once, so passes
    never overwrite each other's output.
    """
    h, w = mask.shape
    limit = max(64, int(limit))
    boxes = []
    try:
        import cv2

        n, labels = cv2.connectedComponents(mask.astype(np.uint8), connectivity=8)
        for i in range(1, n):
            ys, xs = (labels == i).nonzero()
            boxes.append((int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1))
        del labels
    except Exception:  # pragma: no cover - OpenCV missing/unavailable
        boxes = list(_grid_cells(mask, cell))
    if not boxes:
        return
    out, stack = [], list(boxes)
    while stack:
        y0, y1, x0, x1 = stack.pop()
        if (y1 - y0) <= limit and (x1 - x0) <= limit:
            out.append((y0, y1, x0, x1))
            continue
        if (y1 - y0) >= (x1 - x0):
            my = (y0 + y1) // 2
            stack += [(y0, my, x0, x1), (my, y1, x0, x1)]
        else:
            mx = (x0 + x1) // 2
            stack += [(y0, y1, x0, mx), (y0, y1, mx, x1)]
    for box in sorted(out):
        yield box


# --------------------------------------------------------------------------------------
# inference
# --------------------------------------------------------------------------------------
class LamaInpainter:
    def __init__(self, checkpoint: str, device: str, pad_to: int = 8):
        self.device = torch.device(device)
        self.pad_to = max(1, int(pad_to))
        t0 = time.time()
        self.model = build_lama_manga()
        try:
            from safetensors.torch import load_file
        except ImportError as exc:  # pragma: no cover
            raise SystemExit("the 'safetensors' package is required to read the checkpoint") from exc
        state = load_file(checkpoint, device="cpu")
        # strict=True is deliberate: the architecture in lama_arch.py reproduces every
        # key of this checkpoint, so any mismatch is a bug rather than a version drift.
        self.model.load_state_dict(state, strict=True)
        self.model.eval().to(self.device)
        self.load_seconds = time.time() - t0
        del state
        _log(f"loaded {checkpoint} in {self.load_seconds:.2f}s (strict=True, {len(self.model.state_dict())} tensors)")

    @torch.inference_mode()
    def _forward_crop(self, img: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """img: (h,w,3) uint8; mask: (h,w) bool -> (h,w,3) uint8 at the same size."""
        h, w = mask.shape
        img_t = torch.from_numpy(np.ascontiguousarray(img)).permute(2, 0, 1).float().unsqueeze(0) / 255.0
        mask_t = torch.from_numpy(np.ascontiguousarray(mask)).float().unsqueeze(0).unsqueeze(0)
        img_t, mask_t = img_t.to(self.device), mask_t.to(self.device)
        img_t, ph, pw = _reflect_pad_to(img_t, self.pad_to)
        mask_t, _, _ = _reflect_pad_to(mask_t, self.pad_to)

        out = self.model(img_t, mask_t)
        out = out[0, :, : h, : w].clamp(0, 1)
        arr = (out.permute(1, 2, 0).float().cpu().numpy() * 255.0).round().astype(np.uint8)
        del img_t, mask_t, out
        return arr

    def _forward_downscaled(self, img: np.ndarray, mask: np.ndarray, max_run: int) -> np.ndarray:
        """Inpaint a crop that is too large, by running the model on a smaller copy."""
        import cv2

        h, w = mask.shape
        scale = max_run / float(max(h, w))
        nh, nw = max(self.pad_to, int(round(h * scale))), max(self.pad_to, int(round(w * scale)))
        small_img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
        small_mask = cv2.resize(mask.astype(np.uint8), (nw, nh), interpolation=cv2.INTER_NEAREST) > 0
        pred = self._forward_crop(small_img, small_mask)
        return cv2.resize(pred, (w, h), interpolation=cv2.INTER_LANCZOS4)

    def inpaint(self, image: np.ndarray, mask: np.ndarray, tile: int = DEFAULT_CELL,
                margin: int = DEFAULT_MARGIN, max_run: int = DEFAULT_MAX_RUN,
                full_limit_px: int = 4_000_000) -> tuple[np.ndarray, str, dict]:
        """Return (full-resolution result, mode, stats)."""
        h, w = mask.shape
        if not mask.any():
            return image.copy(), "full", {"passes": 0, "note": "empty mask"}

        stats = {"passes": 0, "downscaled_passes": 0, "load_seconds": round(self.load_seconds, 3)}

        if h * w <= full_limit_px and max(h, w) <= max_run:
            try:
                pred = self._forward_crop(image, mask)
                stats["passes"] = 1
                return _blend(image, pred, mask), "full", stats
            except torch.cuda.OutOfMemoryError:
                _log("full-resolution pass ran out of VRAM -> falling back to tiled mode")
                torch.cuda.empty_cache()
            except RuntimeError as exc:
                if "out of memory" not in str(exc).lower():
                    raise
                _log(f"full-resolution pass failed ({exc.__class__.__name__}) -> tiled mode")
                torch.cuda.empty_cache()

        # ---- tiled ----------------------------------------------------------------
        # A single crop is at most `max_run` px per side, so a region may be at most
        # max_run - 2*margin before it has to be split; larger regions are halved.
        out = image.copy()
        mode = "tiled"
        limit = max_run - 2 * margin
        if limit < 256:
            limit = max(256, max_run // 2)
        for (y0, y1, x0, x1) in _regions_for_mask(mask, limit, cell=tile):
            cy0, cy1 = max(0, y0 - margin), min(h, y1 + margin)
            cx0, cx1 = max(0, x0 - margin), min(w, x1 + margin)
            crop_img = image[cy0:cy1, cx0:cx1]
            crop_mask = mask[cy0:cy1, cx0:cx1]
            if max(crop_img.shape[:2]) > max_run:
                pred = self._forward_downscaled(crop_img, crop_mask, max_run)
                mode = "tiled+downscale"
                stats["downscaled_passes"] += 1
            else:
                pred = self._forward_crop(crop_img, crop_mask)
            stats["passes"] += 1
            own = mask[y0:y1, x0:x1]
            sub = pred[y0 - cy0: y1 - cy0, x0 - cx0: x1 - cx0]
            out[y0:y1, x0:x1][own] = sub[own]
        return out, mode, stats


def _blend(image: np.ndarray, pred: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Take the model output inside the mask, the original pixels outside it."""
    out = image.copy()
    out[mask] = pred[mask]
    return out


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Erase masked regions (text, watermarks) from an illustration using a Big-LaMa checkpoint.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--image", required=True, help="input image")
    p.add_argument("--mask", required=True, help="8-bit grayscale mask, non-zero = erase")
    p.add_argument("--out", required=True, help="output image path")
    p.add_argument("--checkpoint", default=DEFAULT_CHECKPOINT, help="safetensors LaMa checkpoint")
    p.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu",
                   choices=["cuda", "cpu"], help="inference device")
    p.add_argument("--pad-to", type=int, default=8, help="pad each forward pass to a multiple of N")
    p.add_argument("--allow-mask-resize", action="store_true",
                   help="scale a mask whose resolution differs from the image (NEAREST)")
    p.add_argument("--dilate", type=int, default=0, help="grow the mask by N px before inpainting")
    p.add_argument("--tile", type=int, default=DEFAULT_CELL,
                   help="tile mode: grid cell size used to split the mask when OpenCV is unavailable")
    p.add_argument("--margin", type=int, default=DEFAULT_MARGIN,
                   help="tile mode: context pixels kept around each masked region")
    p.add_argument("--max-run", type=int, default=DEFAULT_MAX_RUN,
                   help="largest side length of a single forward pass")
    p.add_argument("--full-limit", type=int, default=4_000_000,
                   help="max image pixels for a single full-resolution pass")
    p.add_argument("--force-tiled", action="store_true", help="skip the full-resolution attempt")
    p.add_argument("--quality", type=int, default=95,
                   help="JPEG quality when re-encoding (PNG is always lossless)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    t_start = time.time()

    if args.device == "cuda" and not torch.cuda.is_available():
        raise SystemExit("--device cuda requested but CUDA is not available")

    src = load_image(args.image)
    mask = load_mask(args.mask, src.size, allow_resize=args.allow_mask_resize)
    mask = dilate_mask(mask, args.dilate)
    # np.asarray() on a PIL image yields a read-only view; torch.from_numpy() warns about
    # that, so take an explicit writable copy here.
    image = np.array(src, dtype=np.uint8, copy=True)
    mask = np.array(mask, dtype=bool, copy=True)

    full_limit = args.full_limit
    if args.device == "cpu":
        full_limit = min(full_limit, 1_200_000)  # CPU passes are slow; tile earlier
    if args.force_tiled:
        full_limit = 0

    if args.device == "cuda":
        torch.cuda.reset_peak_memory_stats()

    model = LamaInpainter(args.checkpoint, args.device, pad_to=args.pad_to)
    result, mode, stats = model.inpaint(
        image, mask,
        tile=args.tile, margin=args.margin, max_run=args.max_run, full_limit_px=full_limit,
    )

    peak_mb = None
    if args.device == "cuda":
        peak_mb = round(torch.cuda.max_memory_allocated() / (1024 * 1024), 1)

    out_img = Image.fromarray(result, mode="RGB")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    # Re-encoding must not quietly degrade the training image: PNG stays
    # lossless, JPEG is written at high quality with 4:4:4 chroma instead of
    # PIL's default quality=75 / 4:2:0.
    if os.path.splitext(args.out)[1].lower() in (".jpg", ".jpeg"):
        out_img.save(args.out, quality=int(args.quality), subsampling=0, optimize=True)
    else:
        out_img.save(args.out)

    payload = {
        "image": os.path.abspath(args.image),
        "mask": os.path.abspath(args.mask),
        "out": os.path.abspath(args.out),
        "width": int(src.size[0]),
        "height": int(src.size[1]),
        "device": args.device,
        "seconds": round(time.time() - t_start, 3),
        "mode": mode,
        "mask_pixels": int(mask.sum()),
        "mask_ratio": round(float(mask.mean()), 5),
        "pad_to": args.pad_to,
        "dilate": args.dilate,
        "peak_vram_mb": peak_mb,
        "passes": stats.get("passes", 0),
        "downscaled_passes": stats.get("downscaled_passes", 0),
    }
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
