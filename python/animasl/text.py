"""Text detection + inpainting stage.

Two small GPU scripts do the heavy lifting (they live next to this file so the
bundle stays self-contained):

    ml/textdet.py   RF-DETR-Seg (mayocream koharu-layout-rfdetr-seg-2xl-1152)
                    -> 8-bit mask at the ORIGINAL resolution (255 = patch me)
    ml/inpaint.py   LaMa-manga -> repaint the masked pixels in place

They run under the ML interpreter (``ml_python`` setting -> ``<runtime>/venv`` ->
``<bundle>/python/.venv`` -> the dataset interpreter), never under the toolchain
interpreter, because only that one has torch+rfdetr.

Policy (all thresholds live in ``animasl.config.json`` so they are tunable):

    ratio <= text_warn_ratio                  keep
    text_warn_ratio < ratio <= text_drop_ratio patch (detect_only: report only)
    ratio >  text_drop_ratio                  drop   -> _excluded/text/

Patching necessarily invalidates every caption written before it: auto-taggers
describe the pixels they saw, and those pixels are gone.  So a successful patch
marks the `wash` stage stale.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from . import config as cfgmod
from . import manifest
from .curate import move_excluded, probe_image, write_csv

ML_DIR = Path(__file__).resolve().parent / "ml"
TEXTDET = ML_DIR / "textdet.py"
INPAINT = ML_DIR / "inpaint.py"

DEFAULT_THRESHOLDS = "text=0.30,onomatopoeia=0.25,bubble=0.5,panel=0.5"


def _run(py: str, script: Path, args: list[str], timeout: int = 1800) -> dict:
    """Run one ML script and return the JSON object it printed on stdout."""
    cmd = [py, "-X", "utf8", str(script)] + args
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        raise RuntimeError(f"{script.name} exit {proc.returncode}: " + " | ".join(tail))
    payload = None
    for line in reversed((proc.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except Exception:
                continue
    if payload is None:
        tail = (proc.stderr or "").strip().splitlines()[-4:]
        raise RuntimeError(f"{script.name} produced no JSON: " + " | ".join(tail))
    payload["_seconds"] = round(time.time() - t0, 2)
    return payload


def _run_safe(py: str, script: Path, args: list[str], timeout: int = 1800) -> tuple[dict | None, str]:
    try:
        return _run(py, script, args, timeout=timeout), ""
    except Exception as exc:
        return None, str(exc)[:300]


def cmd_text(ds, apply: bool = False, rules_over: dict | None = None, dilate: int = 6,
             device: str = "", classes: str = "text,onomatopoeia", limit: int = 0,
             detect_only: bool = False, inpaint_only: bool = False) -> dict:
    ds.ensure_dirs()
    cfg = ds.cfg
    rules = dict(rules_over or {})
    warn = float(rules.get("text_warn_ratio", cfg.get("screen", {}).get("text_warn_ratio", 0.08)))
    drop = float(rules.get("text_drop_ratio", cfg.get("screen", {}).get("text_drop_ratio", 0.30)))
    # Patching is gated at `warn` by default: a 1 % watermark is handled by the
    # caption (tag `watermark`), while a 10 % text block physically ruins the
    # training image.  --patch-small lowers the gate so every detection is
    # repainted (and the rest of the run is proportionally slower).
    patch_min = float(rules.get("patch_min_ratio", warn))
    thresholds = str(rules.get("thresholds", cfg.get("text_thresholds", DEFAULT_THRESHOLDS)))

    py = cfgmod.find_ml_python(cfg)
    layout_ckpt = cfgmod.find_model(cfg, "layout")
    inpaint_ckpt = cfgmod.find_model(cfg, "inpaint")
    cache_dir = cfgmod.runtime_dir() / "ml_cache"
    masks_dir = ds.pipe_dir / "masks"
    backup_dir = ds.pipe_dir / "orig_text"
    masks_dir.mkdir(parents=True, exist_ok=True)

    files = ds.images() or ds.raw_images()
    if limit:
        files = files[:limit]
    if not files:
        print("[text] 没有图片（images/ 与 00_raw/ 都为空）")
        return {"count": 0}

    print(f"[text] {len(files)} 张图  占比 >{drop:.0%} 剔除 / >{patch_min:.2%} 修补 / "
          f"classes={classes} dilate={dilate} {'（仅检测）' if detect_only else ''}")
    if not TEXTDET.exists() or not INPAINT.exists():
        print(f"[text] ! 缺少 ML 脚本：{TEXTDET} / {INPAINT}")
        return {"count": 0, "failed": len(files)}

    rows: list[dict] = []
    drops: list[tuple[Path, str, dict]] = []
    patched = 0
    failed = 0
    small = 0  # 检出文字但低于修补阈值、只留 note 的张数
    t_start = time.time()
    from .curate import load_meta
    meta = load_meta(ds)

    for idx, f in enumerate(files, 1):
        info = probe_image(f)
        m = meta.get(f.name, {})
        row = {"file": f.name, "width": info["width"], "height": info["height"],
               "detections": 0, "text_area_ratio": "", "decision": "keep", "patched": 0,
               "verify_ratio": "", "seconds": "", "notes": ""}
        mask_path = masks_dir / f"{f.stem}.mask.png"

        if inpaint_only and mask_path.exists():
            det, err = None, ""
            ratio = None
        else:
            args = ["--image", str(f), "--out-dir", str(masks_dir), "--thresholds", thresholds,
                    "--classes", classes, "--dilate", str(dilate)]
            if device:
                args += ["--device", device]
            if layout_ckpt:
                args += ["--ckpt", str(layout_ckpt), "--cache-dir", str(cache_dir)]
            det, err = _run_safe(py, TEXTDET, args)
            ratio = None

        if det is not None:
            ratio = float(det.get("text_area_ratio") or 0.0)
            row["detections"] = len(det.get("detections") or [])
            row["text_area_ratio"] = f"{ratio:.4f}"
            row["seconds"] = det.get("_seconds", "")
            mask_path = Path(det.get("mask_path") or mask_path)
        elif err:
            row["notes"] = "detect-failed:" + err[:160]
            failed += 1
            rows.append(row)
            print(f"[text] {idx}/{len(files)} {f.name} 检测失败：{err[:90]}")
            continue

        if ratio is None:
            row["notes"] = "mask-only"

        decision = "keep"
        if ratio is not None and ratio > 0:
            if ratio > drop:
                decision = "drop"
            elif ratio >= patch_min:
                decision = "patch"
            else:
                row["notes"] = (row["notes"] + "|small-text(未达修补阈值)").strip("|")
                small += 1
        elif ratio is not None and ratio > drop:
            decision = "drop"
        row["decision"] = decision

        # ---- act -------------------------------------------------------
        if apply and decision == "drop":
            drops.append((f, "text", {"post_id": m.get("post_id", ""), "source": m.get("source", ""),
                                      "rule": f"text_area_ratio>{drop}",
                                      "detail": f"{ratio:.4f} dets={row['detections']}",
                                      "decided_by": "rule"}))
            rows.append(row)
            continue

        if apply and decision == "patch" and not detect_only:
            if not mask_path.exists():
                row["notes"] = (row["notes"] + "|no-mask").strip("|")
            else:
                backup_dir.mkdir(parents=True, exist_ok=True)
                bak = backup_dir / f.name
                if not bak.exists():
                    shutil.copy2(f, bak)
                tmp = f.with_name(f.stem + ".patched" + f.suffix)
                args = ["--image", str(f), "--mask", str(mask_path), "--out", str(tmp)]
                if device:
                    args += ["--device", device]
                if inpaint_ckpt:
                    args += ["--checkpoint", str(inpaint_ckpt)]
                res, err = _run_safe(py, INPAINT, args)
                if res is None:
                    row["notes"] = (row["notes"] + "|inpaint-failed:" + err[:120]).strip("|")
                    failed += 1
                else:
                    tmp.replace(f)
                    patched += 1
                    row["patched"] = 1
                    # verify: re-detect on the patched pixels
                    vargs = ["--image", str(f), "--out-dir", str(masks_dir / "verify"),
                             "--thresholds", thresholds, "--classes", classes, "--dilate", str(dilate)]
                    if device:
                        vargs += ["--device", device]
                    if layout_ckpt:
                        vargs += ["--ckpt", str(layout_ckpt), "--cache-dir", str(cache_dir)]
                    ver, verr = _run_safe(py, TEXTDET, vargs)
                    if ver is not None:
                        row["verify_ratio"] = f"{float(ver.get('text_area_ratio') or 0):.4f}"
                        if float(ver.get("text_area_ratio") or 0) > warn:
                            row["notes"] = (row["notes"] + "|residual-text").strip("|")
                    elif verr:
                        row["notes"] = (row["notes"] + "|verify-failed").strip("|")
        elif decision == "patch" and not apply:
            row["notes"] = (row["notes"] + "|dry-run").strip("|")

        rows.append(row)
        if idx % 10 == 0 or idx == len(files):
            print(f"[text] {idx}/{len(files)}  {(time.time() - t_start) / 60:.1f} min  "
                  f"修补 {patched}  剔除 {len(drops)}  失败 {failed}")

    write_csv(ds.pipe_dir / "text_report.csv", rows)
    if apply and drops:
        n = move_excluded(ds, drops)
        print(f"[text] 剔除 {n} 张（文字占比 > {drop:.0%}）-> {ds.excluded_dir / 'text'}/")

    if patched:
        mf = manifest.ensure(ds)
        if mf.stage_status("wash") == "done":
            mf.invalidate_from("wash")
            mf.save()
            print("[text] 已修补图片 -> wash/review/config 标记为 stale（caption 描述的是修补前的图）")

    kept = len(rows) - len(drops)
    print(f"[text] 完成：{len(rows)} 张，keep {kept - patched}，patch {patched}，"
          f"drop {len(drops)}，小字保留 {small}，失败 {failed}；报告 {ds.pipe_dir / 'text_report.csv'}"
          f"{'' if apply else '（dry-run：加 --apply 才写盘）'}")
    return {"count": len(rows), "patched": patched, "dropped": len(drops), "failed": failed,
            "small": small, "kept": kept}
