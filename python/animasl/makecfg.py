"""Training-config generator -- the three-file set for Anima-Standalone-Trainer.

  train_configs/<name>/<name>_lora_stage1.toml   trainer config
  train_configs/<name>/dataset_<name>.toml       dataset config
  train_configs/<name>/train_<name>.bat          launcher
  train_configs/<name>/rationale.md              why these numbers

The decision tables come from the user's own guides
(`Anima_LoRA_调参指南.md` and the `anima-lora-config` skill):
rank by effective image count, rank<->LR coupling, repeats by data size,
epochs from the target exposure, save interval from the step count.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from . import curate, dicts, state, wash

KINDS = {
    "style": {"name": "画师风格", "rank_band": {16: (8e-5, 1.5e-4), 32: (5e-5, 1e-4),
                                                64: (3e-5, 8e-5)},
              "lr": 5e-5, "lr_high": 8e-5, "exposure": (10, 30)},
    "character": {"name": "人物/角色", "rank_band": {8: (8e-5, 1.5e-4), 16: (8e-5, 1.5e-4),
                                                     32: (5e-5, 1e-4), 64: (3e-5, 8e-5)},
                  "lr": 1e-4, "lr_high": 1e-4, "exposure": (10, 25)},
    "object": {"name": "物体/概念", "rank_band": {16: (8e-5, 1.5e-4), 32: (5e-5, 1e-4)},
               "lr": 1e-4, "lr_high": 1e-4, "exposure": (10, 25)},
    "scene": {"name": "场景", "rank_band": {32: (5e-5, 1e-4), 64: (3e-5, 8e-5)},
              "lr": 5e-5, "lr_high": 8e-5, "exposure": (10, 30)},
    "clothing": {"name": "服装", "rank_band": {16: (8e-5, 1.5e-4), 32: (5e-5, 1e-4)},
                 "lr": 7e-5, "lr_high": 7e-5, "exposure": (10, 25)},
}


def rank_for(kind: str, count: int) -> int:
    if kind in ("style", "scene"):
        if count < 30:
            return 16
        if count < 200:
            return 32
        return 32
    if count < 100:
        return 16
    return 32


def repeats_for(count: int) -> int:
    if count < 20:
        return 6
    if count < 50:
        return 3
    if count < 100:
        return 2
    return 1


def collect_subsets(ds, subdirs: list[str] | None, base: Path | None = None) -> list[dict]:
    """[{name, path, count}] -- only directories that actually exist and hold images."""
    base = base or ds.root
    if subdirs:
        out = []
        for spec in subdirs:
            name = spec.split(":")[0]
            path = base / name
            out.append({"name": name, "path": path,
                        "count": len([p for p in path.iterdir()
                                      if p.suffix.lower() in curate.IMG_EXTS])
                        if path.exists() else 0})
        return out
    out = []
    for p in sorted(base.iterdir()) if base.exists() else []:
        # 00_raw is the download staging area: `rename` moves its files into
        # images/, so counting both would silently double every picture.
        if p.is_dir() and not p.name.startswith(("_", ".")) and p.name not in ("meta", "00_raw"):
            n = len([f for f in p.iterdir() if f.suffix.lower() in curate.IMG_EXTS])
            if n:
                out.append({"name": p.name, "path": p, "count": n})
    return out


def plan(ds, subsets: list[dict], kind: str = "style", trigger: str = "",
         dim: int | None = None, lr: float | None = None, epochs: int | None = None,
         resolution: int = 1280, batch: int = 1, grad_accum: int = 1,
         repeats: dict[str, int] | None = None,
         allow_out_of_band: bool = False) -> dict:
    kind = kind if kind in KINDS else "style"
    k = KINDS[kind]
    total = sum(s["count"] for s in subsets)
    if total == 0:
        raise SystemExit("[makecfg] 没有找到任何图片，先跑 rename/wash")

    rk = dim or rank_for(kind, total)
    # per-subset repeats: differentiated weighting when several folders are used
    reps: dict[str, int] = {}
    if repeats:
        reps = {k2: int(v) for k2, v in repeats.items()}
    elif len(subsets) == 1:
        reps[subsets[0]["name"]] = repeats_for(total)
    else:
        base = repeats_for(total)
        core = next((s["name"] for s in subsets
                     if s["name"].lower() in ("core", "latest", "before")), subsets[-1]["name"])
        for s in subsets:
            reps[s["name"]] = base if s["name"] == core else 1
        # balance: core x repeat close to the largest other product, never below 1
        biggest_other = max([s["count"] for s in subsets if s["name"] != core] or [0])
        core_count = max([s["count"] for s in subsets if s["name"] == core] or [1])
        if biggest_other > core_count * reps[core]:
            reps[core] = min(8, math.ceil(biggest_other / max(core_count, 1)))

    per_epoch = sum(s["count"] * reps.get(s["name"], 1) for s in subsets) / max(batch * grad_accum, 1)
    exposure = sum(k["exposure"]) / 2
    total_steps = exposure * total / max(batch * grad_accum, 1)
    ep = epochs or max(2, int(round(total_steps / max(per_epoch, 1))))
    ep = ep + (ep % 2)          # keep it even so save_every_n_epochs=2 lines up

    save_target = 300 if total_steps < 2000 else 500
    save_every = max(1, int(round(save_target / max(per_epoch, 1))))

    lr_use = lr or (k["lr_high"] if total >= 100 else k["lr"])
    band = k["rank_band"].get(rk)
    warn = ""
    if band:
        lo, hi = band
        if not (lo <= lr_use <= hi):
            # 真实使用里这条被静默收紧成区间下限（5e-05 -> 8e-05），用户根本没看见。
            # 现在改成报错：要么按区间取值，要么显式 --allow-out-of-band 说明你知道自己在干什么。
            if not allow_out_of_band:
                raise SystemExit(
                    f"[makecfg] LR {lr_use:g} 不在 Rank {rk}（{k['name']}）的建议区间 {lo:g}~{hi:g}。\n"
                    f"          直接用区间内的值（如 {lo:g}~{hi:g}，或按类型默认 {k['lr']:g}），\n"
                    f"          或加 --allow-out-of-band 明确表示「我知道，照写」。")
            warn = (f"LR {lr_use:g} 超出 Rank {rk} 的建议区间 {lo:g}~{hi:g}"
                    f"（--allow-out-of-band 已放行，未改动）")
    return {
        "kind": kind, "kind_name": k["name"], "trigger": trigger,
        "count": total, "subsets": subsets, "repeats": reps,
        "network_dim": rk, "network_alpha": rk, "learning_rate": lr_use,
        "max_train_epochs": ep, "steps_per_epoch": round(per_epoch, 1),
        "total_steps": int(per_epoch * ep), "target_exposure": exposure,
        "resolution": resolution, "batch_size": batch, "gradient_accumulation_steps": grad_accum,
        "save_every_n_epochs": save_every, "lr_warn": warn,
    }


def _toml_stage1(p: dict, paths: dict, cfg_dir: Path, name: str) -> str:
    out_name = f"{name}_style_lora" if p["kind"] == "style" else f"{name}_lora"
    return f"""# Anima LoRA stage-1 training config -- generated by animasl
# dataset: {name}   kind: {p['kind_name']}   images: {p['count']}
# rank {p['network_dim']} / lr {p['learning_rate']:g} / {p['max_train_epochs']} epochs
#   ~{p['steps_per_epoch']} steps/epoch -> ~{p['total_steps']} steps
#   target exposure {p['target_exposure']:g} per image

[model_arguments]
dit_path = "{paths['dit']}"
qwen3_path = "{paths['qwen3']}"
vae_path = "{paths['vae']}"

[dataset_arguments]
dataset_config = "{cfg_dir.as_posix()}/dataset_{name}.toml"
cache_latents_to_disk = true
cache_text_encoder_outputs = true
cache_text_encoder_outputs_to_disk = true

[training_arguments]
output_dir = "{paths['output_dir']}"
output_name = "{out_name}"
save_model_as = "safetensors"
save_precision = "bf16"
max_train_epochs = {p['max_train_epochs']}
save_every_n_epochs = {p['save_every_n_epochs']}
logging_dir = "{cfg_dir.as_posix()}/logs"
log_with = "tensorboard"
learning_rate = {p['learning_rate']:g}
text_encoder_lr = 0
llm_adapter_lr = 0
optimizer_type = "AdamW8bit"
optimizer_args = ["weight_decay=0.01"]
lr_scheduler = "cosine"
lr_warmup_steps = {min(100, max(20, p['total_steps'] // 20))}
max_grad_norm = 1.0
mixed_precision = "bf16"
gradient_checkpointing = true
gradient_accumulation_steps = {p['gradient_accumulation_steps']}
flash_attn = true
max_data_loader_n_workers = 0
persistent_data_loader_workers = false
seed = 42

[anima_arguments]
timestep_sample_method = "logit_normal"
discrete_flow_shift = 3.0

[network_arguments]
network_module = "networks.lora_anima"
network_dim = {p['network_dim']}
network_alpha = {p['network_alpha']}
network_train_unet_only = true
"""


def _toml_dataset(p: dict, cfg_dir: Path, name: str) -> str:
    res = p["resolution"]
    lines = [
        "# dataset config -- generated by animasl",
        f"# {p['count']} images across {len(p['subsets'])} subset(s)",
        "",
        "[general]",
        "enable_bucket = true",
        "bucket_no_upscale = true",
        "min_bucket_reso = 640",
        f"max_bucket_reso = {res}",
        "bucket_reso_steps = 64",
        "shuffle_caption = false",
        "keep_tokens = 1",
        "",
        "[[datasets]]",
        f"resolution = [{res}, {res}]",
        f"batch_size = {p['batch_size']}",
        'caption_extension = ".txt"',
    ]
    for s in p["subsets"]:
        lines += [
            "",
            "[[datasets.subsets]]",
            f'image_dir = "{s["path"].as_posix()}"',
            f'num_repeats = {p["repeats"].get(s["name"], 1)}',
        ]
    return "\n".join(lines) + "\n"


def _bat(p: dict, paths: dict, cfg_dir: Path, name: str) -> str:
    return f"""@echo off
rem generated by animasl -- {name} ({p['kind_name']}, {p['count']} images)
cd /d "%~dp0"
call "{paths['venv_activate']}"
set PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
python "{paths['trainer_py']}" --config_file "{cfg_dir.as_posix()}/{name}_lora_stage1.toml"
pause
"""


def _rationale(p: dict, name: str, cfg_dir: Path) -> str:
    rows = "\n".join(
        f"| {s['name']} | {s['count']} | {p['repeats'].get(s['name'], 1)} | "
        f"{s['count'] * p['repeats'].get(s['name'], 1)} |" for s in p["subsets"])
    warn = f"\n> ⚠️ {p['lr_warn']}\n" if p["lr_warn"] else ""
    return f"""# {name} 训练配置说明（自动生成）

- 目标类型：**{p['kind_name']}**，有效图片 **{p['count']}** 张
- Rank / Alpha：**{p['network_dim']} / {p['network_alpha']}**（按有效图片数选）
- 学习率：**{p['learning_rate']:g}**（AdamW8bit + cosine；已按 Rank–LR 联动表校验）
- 训练长度：**{p['max_train_epochs']} epoch** × {p['steps_per_epoch']} steps/epoch ≈ **{p['total_steps']} steps**
  （目标曝光 {p['target_exposure']:g} 次/图）
- 保存间隔：每 **{p['save_every_n_epochs']}** epoch（≈{p['save_every_n_epochs'] * p['steps_per_epoch']:.0f} steps）
- 分辨率：{p['resolution']}（bucket 640~{p['resolution']}，不上采样），batch {p['batch_size']} × accum {p['gradient_accumulation_steps']}
- 触发词：{p['trigger'] or '（未设置——记得在 caption 里置首）'}

| subset | 图片数 | repeats | 有效样本 |
|---|---:|---:|---:|
{rows}
{warn}
## 生成后的检查清单

- [ ] 每张 caption 都有触发词且在首位（`anima_wash --trigger`）
- [ ] 固有属性（发色/瞳色/标志特征）描述一致
- [ ] 变化因素（服装/姿势/场景/镜头）逐张写出
- [ ] 没有质量词/审美词堆砌
- [ ] 与既有配置相比只改了一个变量（首轮）——调整时按 调参指南 §8 一次一步

生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}
"""


def _toml_kv(text: str) -> dict[str, str]:
    """Flat `key = value` map of a toml file (enough to diff two configs)."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _preflight(ds, p: dict, cfg_dir: Path, name: str, stage1_text: str,
               old_stage1: str = "") -> str:
    """C2：写盘前把「这次配置的依据」落成 preflight.txt —— 图片数、caption 合规率、
    词典外标签、rank/LR/epoch/steps 推导、与上一版配置的差异。

    为什么需要：makecfg 会静默决定 rank/LR/epoch，真实使用里 LR 被悄悄改了而没人看见。
    """
    lines = [f"# makecfg preflight — {name}",
             f"# {time.strftime('%Y-%m-%d %H:%M:%S')}   kind={p['kind']}({p['kind_name']})",
             ""]
    lines.append(f"图片      {p['count']} 张")
    for s in p["subsets"]:
        lines.append(f"          {s['name']:<14} {s['count']:>5} 张 × {p['repeats'].get(s['name'], 1)}")
    health: dict = {}
    unknown: list[str] = []
    zombie: list[str] = []
    try:
        rules = wash.load_rules(ds)
        health = state.caption_health(ds, trigger=p.get("trigger") or "",
                                      min_tags=int(rules.get("min_tags") or 20),
                                      max_tags=int(rules.get("max_tags") or 45))
        lines.append(f"caption   有 txt {health['captions']} / 图 {health['images']}"
                     f"；缺 txt {health['missing_txt']}；空 {health['empty']}"
                     f"；孤立 txt {health['orphan_txt']}")
        lines.append(f"          标签数 min {health['min_tags']} / avg {health['avg_tags']}"
                     f" / max {health['max_tags']}；低于下限 {health['under_min']}"
                     f"；高于上限 {health['over_max']}")
        lines.append(f"          触发词不在首位 {health['trigger_not_first']}"
                     f"；出现两次以上 {health.get('trigger_twice', 0)}")
        td = dicts.load(ds.cfg)
        seen: set[str] = set()
        for f in (ds.images() or ds.raw_images()):
            txt = f.with_suffix(".txt")
            if not txt.exists():
                continue
            for tag in txt.read_text(encoding="utf-8", errors="replace").split(","):
                tag = tag.strip()
                if tag and not tag.startswith("@"):
                    seen.add(tag)
        for tag in sorted(seen):
            key = wash.lookup_key(tag)
            rec = td.meta.get(key)
            if rec is None:
                unknown.append(tag)
            elif int(rec.get("post_count") or 0) <= 0:
                zombie.append(tag)
        lines.append(f"标签       {len(seen)} 个不同；词典里没有 {len(unknown)}"
                     f"；post_count=0（幻觉）{len(zombie)}")
        if unknown:
            lines.append("          词典里没有：" + ", ".join(unknown[:12])
                         + (" …" if len(unknown) > 12 else ""))
        if zombie:
            lines.append("          post_count=0：" + ", ".join(zombie[:12])
                         + (" …" if len(zombie) > 12 else ""))
    except Exception as exc:                      # 体检归体检，别挡住出配置
        lines.append(f"（caption/标签体检跳过：{exc}）")

    lines += ["",
              f"rank      {p['network_dim']} / alpha {p['network_alpha']}"
              f"（由图片数 {p['count']} + kind 决定）",
              f"LR        {p['learning_rate']:g}"
              + (f"  ⚠️ {p['lr_warn']}" if p["lr_warn"] else "（在 Rank 建议区间内）"),
              f"epochs    {p['max_train_epochs']}（目标曝光 {p['target_exposure']:g}/图）"
              f" -> ~{p['steps_per_epoch']} steps/epoch，共 ~{p['total_steps']} steps",
              f"保存      save_every_n_epochs = {p['save_every_n_epochs']}",
              f"分辨率    {p['resolution']}  batch {p['batch_size']}"
              f" × grad-accum {p['gradient_accumulation_steps']}"]
    if health.get("under_min") or health.get("over_max") or unknown or zombie:
        lines += ["", "⚠️ 上面有需要你拍板的项：标签数越界 / 词典里查不到的标签。"
                      "训练前建议先 wash + dict-check 收拾干净。"]

    old_toml = cfg_dir / f"{name}_lora_stage1.toml"
    if old_stage1 or old_toml.exists():
        old = _toml_kv(old_stage1 or old_toml.read_text(encoding="utf-8", errors="replace"))
        new = _toml_kv(stage1_text)
        diff = [f"  {k}: {old.get(k, '(无)')} -> {new[k]}"
                for k in sorted(new) if k in old and old[k] != new[k]]
        lines += ["", "与上一版配置的差异：" + ("（无）" if not diff else "")]
        lines += diff
    return "\n".join(lines) + "\n"


def cmd_makecfg(ds, name: str | None = None, trigger: str = "", kind: str = "style",
                subdirs: list[str] | None = None, dim: int | None = None,
                lr: float | None = None, epochs: int | None = None,
                resolution: int = 1280, batch: int = 1, grad_accum: int = 1,
                repeats: dict[str, int] | None = None, apply: bool = False,
                dataset_dir: str | None = None, force: bool = False,
                allow_out_of_band: bool = False) -> dict:
    name = name or ds.name
    cfg = ds.cfg
    base = Path(dataset_dir) if dataset_dir else ds.root

    subsets = collect_subsets(ds, subdirs, base)
    subsets = [s for s in subsets if s["count"] > 0]
    if not subsets:
        raise SystemExit(f"[makecfg] {base} 下没有找到含图片的子目录")
    missing = [s["name"] for s in subsets if not s["path"].exists()]
    if missing:
        raise SystemExit(f"[makecfg] 子目录不存在: {missing}")

    p = plan(ds, subsets, kind=kind, trigger=trigger, dim=dim, lr=lr, epochs=epochs,
             resolution=resolution, batch=batch, grad_accum=grad_accum, repeats=repeats,
             allow_out_of_band=allow_out_of_band)

    models = Path(cfg.models_dir)
    paths = {
        "dit": (models / "diffusion_models" / "anima-base-v1.0.safetensors").as_posix(),
        "qwen3": (models / "text_encoders" / "qwen_3_06b_base.safetensors").as_posix(),
        "vae": (models / "vae" / "qwen_image_vae.safetensors").as_posix(),
        "output_dir": (Path(cfg.output_dir) / f"{name}_lora").as_posix(),
        "trainer_py": (Path(cfg.trainer_dir) / "anima_train_network.py").as_posix(),
        "venv_activate": (Path(cfg.trainer_dir) / "venv" / "Scripts" / "Activate.bat").as_posix(),
    }
    for key in ("dit", "qwen3", "vae"):
        if not Path(paths[key]).exists():
            print(f"[makecfg] ! 模型缺失 {key}: {paths[key]}")
    if not Path(paths["trainer_py"]).exists():
        raise SystemExit(f"[makecfg] 找不到训练脚本 {paths['trainer_py']}")

    cfg_dir = Path(cfg.configs_dir) / name
    stage1 = _toml_stage1(p, paths, cfg_dir, name)
    dataset_toml = _toml_dataset(p, cfg_dir, name)
    bat = _bat(p, paths, cfg_dir, name)
    rationale = _rationale(p, name, cfg_dir)
    old_stage1 = ""
    old_path = cfg_dir / f"{name}_lora_stage1.toml"
    if old_path.exists():
        old_stage1 = old_path.read_text(encoding="utf-8", errors="replace")
    preflight = _preflight(ds, p, cfg_dir, name, stage1, old_stage1)

    print(f"[makecfg] {name}  {p['kind_name']}  {p['count']} 张  "
          f"rank {p['network_dim']}  lr {p['learning_rate']:g}  "
          f"{p['max_train_epochs']} epoch ≈ {p['total_steps']} steps")
    for s in p["subsets"]:
        print(f"    {s['name']:<12} {s['count']:>5} 张 × {p['repeats'].get(s['name'], 1)}")
    if p["lr_warn"]:
        print(f"    ⚠️ {p['lr_warn']}")

    if not apply:
        print(f"[makecfg] 预演模式（加 --apply 写盘）-> {cfg_dir}")
        print(preflight.rstrip())
        return {"plan": p, "written": [], "preflight": preflight}

    cfg_dir.mkdir(parents=True, exist_ok=True)
    targets = {
        cfg_dir / f"{name}_lora_stage1.toml": stage1,
        cfg_dir / f"dataset_{name}.toml": dataset_toml,
        cfg_dir / f"train_{name}.bat": bat,
        cfg_dir / "rationale.md": rationale,
        cfg_dir / "preflight.txt": preflight,
    }
    for path, text in targets.items():
        if path.exists() and not force:
            backup = path.with_suffix(path.suffix + f".bak{time.strftime('%H%M%S')}")
            path.rename(backup)
            print(f"    (已备份旧文件 -> {backup.name})")
        path.write_text(text, encoding="utf-8", newline="\n")
    print(f"[makecfg] 已写入 {len(targets)} 个文件 -> {cfg_dir}")
    print(f"[makecfg] 依据与差异见 {cfg_dir / 'preflight.txt'}")
    return {"plan": p, "written": [str(t) for t in targets], "preflight": preflight}
