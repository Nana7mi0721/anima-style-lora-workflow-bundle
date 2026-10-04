"""animasl command line -- one subcommand per pipeline stage.

    python -m animasl.cli status   --dataset arata
    python -m animasl.cli fetch    --dataset arata --source yandere --tags "asabu202"
    python -m animasl.cli dedup    --dataset arata --apply
    ...

Every stage writes its report into <dataset>/_pipeline/ and records its state in
the dataset manifest, so the agent can always ask `status` what is done.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config as cfgmod
from . import curate, dicts, fetch, makecfg, manifest, net, wash


def _ds(args) -> cfgmod.Dataset:
    cfg = cfgmod.Config.load(home=getattr(args, "home", None))
    return cfg.dataset(args.dataset)


def _apply(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--apply", action="store_true",
                        help="真正写盘 / 移动文件（默认只出报告）")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="anima", description="Anima style LoRA 数据流水线")
    p.add_argument("--home", help="LoRA_Train 根目录（默认读 animasl.config.json）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, help_: str, required: bool = True):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--dataset", required=required, help="数据集名（datasets/<name>）")
        return sp

    sp = add("init", "初始化数据集目录与 manifest")
    sp.add_argument("--trigger", default="", help="触发词，如 @asabu202")
    sp.add_argument("--kind", default="style", choices=sorted(makecfg.KINDS),
                    help="训练目标类型")
    sp.add_argument("--force", action="store_true",
                    help="manifest 已存在时就地更新触发词/类型（阶段记录保留）")
    sp.add_argument("--reset", action="store_true",
                    help="真·重建：丢掉全部阶段记录、从零开始（会二次确认）")
    sp.add_argument("--yes", action="store_true", help="配合 --reset 确认重建")

    sp = add("status", "查看数据集各阶段进度（不给 --dataset 则列出全部）", required=False)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--rebuild", action="store_true", help="重建词典缓存")

    sp = add("fetch", "从 booru / 归档站下载原图与标签")
    sp.add_argument("--source", required=True,
                    choices=["yandere", "danbooru", "pawchive", "exhentai"])
    sp.add_argument("--tags", default="", help="yandere/danbooru：搜索标签（空格分隔）")
    sp.add_argument("--creator", default="", help="pawchive：创作者 id")
    sp.add_argument("--service", default="patreon", help="pawchive：patreon/fanbox/discord")
    sp.add_argument("--gallery", default="", help="exhentai：gid/token")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--cookies", default="", help="cookies.txt（Netscape 格式）")
    sp.add_argument("--title-filter", default="", help="pawchive：标题子串过滤")
    sp.add_argument("--dry-run", action="store_true")

    sp = add("import", "把本地文件/压缩包导入 00_raw")
    sp.add_argument("--src", required=True)
    sp.add_argument("--move", action="store_true")
    sp.add_argument("--no-unpack", action="store_true")
    sp.add_argument("--dry-run", action="store_true")

    sp = add("dedup", "md5 + pHash/SSIM 去重")
    sp.add_argument("--phash-distance", type=int, default=4)
    sp.add_argument("--ssim", type=float, default=0.995)
    sp.add_argument("--include-images", action="store_true")
    _apply(sp)

    sp = add("screen", "低质量 / 过老 / 草图筛选")
    sp.add_argument("--min-short-side", type=int, default=0)
    sp.add_argument("--min-bytes", type=int, default=0)
    sp.add_argument("--earliest", default="", help="早于此日期(YYYY-MM-DD)的剔除")
    sp.add_argument("--include-images", action="store_true")
    _apply(sp)

    sp = add("thumbs", "生成缩略图（视觉子代理只能看这个）")
    sp.add_argument("--max-side", type=int, default=1536)
    sp.add_argument("--include-images", action="store_true")

    sp = add("text", "检测图中文字并修补（rfdetr + lama）")
    sp.add_argument("--warn-ratio", type=float, default=0.0)
    sp.add_argument("--drop-ratio", type=float, default=0.0)
    sp.add_argument("--dilate", type=int, default=6)
    sp.add_argument("--device", default="")
    sp.add_argument("--classes", default="text,onomatopoeia")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--detect-only", action="store_true")
    sp.add_argument("--inpaint-only", action="store_true")
    sp.add_argument("--thresholds", default="",
                    help="检测类别阈值，如 text=0.15,onomatopoeia=0.12（默认 text=0.30,onomatopoeia=0.25）")
    sp.add_argument("--patch-small", action="store_true",
                    help="连水印等小面积文字也修补（默认只有 >8%% 的大段文字才修补）")
    _apply(sp)

    sp = add("rename", "重排编号为 0001…（下载后、洗标前）")
    sp.add_argument("--start", type=int, default=1)
    sp.add_argument("--digits", type=int, default=4)
    _apply(sp)

    sp = add("wash", "洗标：规则引擎规范化 + 七槽位排序")
    sp.add_argument("--trigger", default="")
    sp.add_argument("--no-images", action="store_true", help="处理 00_raw 而不是 images")
    sp.add_argument("--rules", default="", help="额外规则 JSON 文件")
    _apply(sp)

    sp = add("review-list", "列出需要看图补全的图")
    sp = add("apply-review", "把视觉子代理的结果合并回 caption")
    sp.add_argument("--payload", required=True, help="子代理返回的 JSON")
    _apply(sp)

    sp = add("verify", "校验全部 caption 是否违反硬约束")
    sp.add_argument("--online", action="store_true")
    sp.add_argument("--sample", type=int, default=0)

    sp = add("makecfg", "生成训练配置三件套")
    sp.add_argument("--name", default="")
    sp.add_argument("--kind", default="style", choices=sorted(makecfg.KINDS))
    sp.add_argument("--subdirs", default="", help="逗号分隔的子目录，可带 repeats：latest:3,before:1")
    sp.add_argument("--dim", type=int, default=0)
    sp.add_argument("--lr", type=float, default=0.0)
    sp.add_argument("--epochs", type=int, default=0)
    sp.add_argument("--resolution", type=int, default=1280)
    sp.add_argument("--batch", type=int, default=1)
    sp.add_argument("--grad-accum", type=int, default=1)
    sp.add_argument("--dataset-dir", default="")
    sp.add_argument("--trigger", default="")
    sp.add_argument("--force", action="store_true")
    _apply(sp)

    sp = add("doctor", "检查环境：python / torch / rfdetr / 模型 / 代理 / 词典", required=False)
    sp = add("config", "查看配置分层与每个键的来源（设置页读写的就是 runtime 层）", required=False)
    sp.add_argument("--json", action="store_true", help="输出 JSON（设置页用）")
    sub.add_parser("dict", help="词典缓存信息")
    return p


def _list_datasets(cfg: cfgmod.Config) -> int:
    root = Path(cfg.home) / "datasets"
    if not root.exists():
        print(f"[status] 没有数据集目录：{root}")
        return 0
    rows = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(("_", ".")):
            continue          # `_smoke` 这类测试夹具 / 隐藏目录不是数据集，别混进清单
        ds = cfg.dataset(entry.name)
        m = manifest.Manifest(ds.manifest_file)
        stages = m.data.get("stages", {})
        done = [k for k, v in stages.items() if (v or {}).get("status") == "done"]
        rows.append((entry.name, len(ds.raw_images()), len(ds.images()), len(done), done))
    print(f"{'数据集':<22}{'00_raw':>7}{'images':>7}  已完成阶段")
    for name, raw, imgs, _, done in rows:
        print(f"{name:<22}{raw:>7}{imgs:>7}  {','.join(done)}")
    return 0


def _walk_leaves(node, prefix: str = "") -> list[tuple[str, object]]:
    """Flatten a config tree into (dotted key, leaf value) rows."""
    rows: list[tuple[str, object]] = []
    for key, value in (node or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            rows.extend(_walk_leaves(value, path + "."))
        else:
            rows.append((path, value))
    return rows


def _origin_of(path: str, layers: dict) -> str:
    """Which layer supplies a dotted key (later layers win)."""
    def has(layer: dict) -> bool:
        node = layer
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True

    for name in ("env", "runtime", "bundle"):
        if has(layers[name]):
            return name
    return "defaults"


def _config_report(home: str | None, as_json: bool) -> int:
    layers = cfgmod.layers({"home": home} if home else None)
    paths = {
        "home": layers["merged"].get("home"),
        "bundle_config": str(cfgmod.CONFIG_FILE),
        "runtime_config": str(cfgmod.runtime_config_file()),
        "runtime_dir_exists": cfgmod.runtime_config_file().parent.is_dir(),
        "runtime_config_exists": cfgmod.runtime_config_file().is_file(),
    }
    if as_json:
        print(json.dumps({"paths": paths, "layers": layers}, ensure_ascii=False, indent=2))
        return 0
    print(f"[config] home            {paths['home']}")
    print(f"[config] bundle 层       {paths['bundle_config']}")
    print(f"[config] runtime 层      {paths['runtime_config']}"
          f"{'' if paths['runtime_config_exists'] else '（还没有这个文件，设置页保存时才创建）'}")
    if layers["env"]:
        print(f"[config] 环境覆盖        {', '.join(sorted(layers['env']))}")
    print(f"{'键':<34}{'来源':<10}值")
    for key, value in _walk_leaves(layers["merged"]):
        shown = json.dumps(value, ensure_ascii=False)
        if len(shown) > 72:
            shown = shown[:69] + "..."
        print(f"{key:<34}{_origin_of(key, layers):<10}{shown}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.cmd

    if cmd == "dict":
        cfg = cfgmod.Config.load(home=getattr(args, "home", None))
        td = dicts.load(cfg)
        print(f"[dict] {td.size()} 个标签，别名 {len(td.alias_to_canonical)} 条")
        return 0

    if cmd == "doctor":
        _doctor(getattr(args, "home", None))
        return 0

    if cmd == "config":
        return _config_report(getattr(args, "home", None), bool(getattr(args, "json", False)))

    if cmd == "status" and not getattr(args, "dataset", None):
        return _list_datasets(cfgmod.Config.load(home=getattr(args, "home", None)))

    ds = _ds(args)
    ds.ensure_dirs()

    if cmd == "init":
        existing = ds.manifest_file.exists()
        if existing and not (args.force or args.reset):
            print(f"[init] manifest 已存在：{ds.manifest_file}")
            print("       --force 就地更新触发词/类型（阶段记录保留）；--reset 从零重建")
            return 0
        if existing and args.reset:
            # 真·重建会丢阶段记录，所以要求显式二次确认（--reset 已是一次，环境变量兜第二次）
            if not args.yes:
                m_old = manifest.Manifest(ds.manifest_file)
                done = [k for k, v in (m_old.data.get("stages") or {}).items()
                        if (v or {}).get("status") == "done"]
                print(f"[init] --reset 会丢掉全部阶段记录（当前已完成：{', '.join(done) or '无'}）")
                print("       确认请加 --yes，或改用 --force 只更新触发词/类型")
                return 0
            m = manifest.Manifest(ds.manifest_file).create(
                ds.name, trigger=args.trigger, source="manual")
            m.set_cfg("kind", args.kind)
            m.set_stage("init", "done")
            m.save()
            print(f"[init] 已从零重建 -> {ds.manifest_file}\n"
                  f"       触发词 {args.trigger or '(无)'}   类型 {args.kind}")
            return 0
        if existing:                       # --force：就地更新，阶段记录保留
            m = manifest.ensure(ds)
            old_kind = m.cfg("kind")
            old_trigger = m.trigger
            if args.trigger:
                m.data["trigger"] = args.trigger
            m.set_cfg("kind", args.kind)
            m.save()
            print(f"[init] 已就地更新 -> {ds.manifest_file}")
            print(f"       触发词 {old_trigger or '(无)'} -> {args.trigger or '(不变)'}"
                  if args.trigger else f"       触发词 {old_trigger or '(无)'}（不变）")
            print(f"       类型 {old_kind or '(未设)'} -> {args.kind}；阶段记录保留")
            return 0
        m = manifest.Manifest(ds.manifest_file).create(
            ds.name, trigger=args.trigger, source="manual")
        m.set_cfg("kind", args.kind)
        m.set_stage("init", "done")
        m.save()
        print(f"[init] {ds.root}\n       manifest -> {ds.manifest_file}")
        if args.trigger:
            print(f"       触发词 {args.trigger}")
        return 0

    if cmd == "status":
        m = manifest.ensure(ds)
        counts = {
            "00_raw": len(ds.raw_images()),
            "images": len(ds.images()) if ds.images_dir.exists() else 0,
            "captions": len(list(ds.images_dir.glob("*.txt"))) if ds.images_dir.exists() else 0,
        }
        if args.json:
            print(json.dumps({"manifest": m.data, "counts": counts}, ensure_ascii=False, indent=2))
            return 0
        print(f"数据集 {ds.name}  ({ds.root})")
        print(f"  触发词 {m.trigger or '(未设置)'}   类型 {m.cfg('kind', 'style')}")
        marks = {"done": "✔", "running": "…", "failed": "✘", "stale": "↻", "preview": "▷"}
        for stage in manifest.STAGES:
            st = m.stage_status(stage)
            mark = marks.get(st, "·")
            at = m.stage(stage).get("at", "")
            tail = f"   [{st}{' ' + at if at else ''}]" if st in ("preview", "stale", "failed") else ""
            print(f"  {mark} {stage:<8} {manifest.STAGE_DOC.get(stage, '')}{tail}")
        print("  图例：✔ 已完成   ▷ 只跑过 dry-run（没写盘，要 --apply 才算完）"
              "   ↻ 上游改过需重跑   ✘ 失败   · 未跑")
        print(f"  图片：00_raw {counts['00_raw']}，images {counts['images']}，"
              f"caption {counts['captions']}")
        thumbs = sorted(ds.thumbs_dir.glob("*.jpg")) if ds.thumbs_dir.exists() else []
        print(f"  缩略图 {len(thumbs)} 张（{ds.thumbs_dir}）"
              + ("　—— 看图复核只能看这个，别直接读原图" if thumbs else ""))
        for f in sorted(ds.pipe_dir.glob("*.csv")):
            n = sum(1 for _ in f.open(encoding='utf-8'))
            print(f"  报告 {f.name}（{max(0, n - 1)} 行）")
        return 0

    if cmd == "fetch":
        cfg = ds.cfg
        if args.source == "yandere":
            res = fetch.fetch_yandere(cfg, ds, args.tags, limit=args.limit, dry_run=args.dry_run)
        elif args.source == "danbooru":
            res = fetch.fetch_danbooru(cfg, ds, args.tags, limit=args.limit, dry_run=args.dry_run)
        elif args.source == "pawchive":
            res = fetch.fetch_pawchive(cfg, ds, args.creator, service=args.service, limit=args.limit,
                                       dry_run=args.dry_run, cookies_file=args.cookies,
                                       title_filter=args.title_filter)
        else:
            res = fetch.fetch_exhentai(cfg, ds, args.gallery, limit=args.limit,
                                       dry_run=args.dry_run, cookies_file=args.cookies)
        n = fetch.write_posts(ds, (res or {}).get("rows") or [], dry_run=args.dry_run)
        if n:
            print(f"[fetch] 元数据 {n} 条 -> {ds.pipe_dir / 'raw_posts.jsonl'}"
                  f"（rename 的 post_id/tags、wash 的基础标签都读这里）")
        _mark(ds, "fetch", _applied(args))
        return 0

    if cmd == "import":
        curate.cmd_import(ds, args.src, unpack=not args.no_unpack, move=args.move,
                          dry_run=args.dry_run)
        _mark(ds, "import", _applied(args))
        return 0

    if cmd == "dedup":
        curate.cmd_dedup(ds, phash_distance=args.phash_distance, ssim_threshold=args.ssim,
                         apply=args.apply, include_images=args.include_images)
        _mark(ds, "dedup", _applied(args))
        return 0

    if cmd == "screen":
        over = {}
        if args.min_short_side:
            over["min_short_side"] = args.min_short_side
        if args.min_bytes:
            over["min_bytes"] = args.min_bytes
        if args.earliest:
            over["earliest_date"] = args.earliest
        curate.cmd_screen(ds, rules=over, apply=args.apply, include_images=args.include_images)
        _mark(ds, "screen", _applied(args))
        return 0

    if cmd == "thumbs":
        curate.cmd_thumbs(ds, max_side=args.max_side, include_images=args.include_images)
        return 0

    if cmd == "text":
        from . import text as textmod
        over = {}
        if args.warn_ratio:
            over["text_warn_ratio"] = args.warn_ratio
        if args.drop_ratio:
            over["text_drop_ratio"] = args.drop_ratio
        if args.patch_small:
            over["patch_min_ratio"] = 0.0002
        if args.thresholds:
            over["thresholds"] = args.thresholds
        textmod.cmd_text(ds, apply=args.apply, rules_over=over, dilate=args.dilate,
                         device=args.device, classes=args.classes, limit=args.limit,
                         detect_only=args.detect_only, inpaint_only=args.inpaint_only)
        _mark(ds, "text", _applied(args))
        return 0

    if cmd == "rename":
        curate.cmd_rename(ds, start=args.start, digits=args.digits, apply=args.apply)
        _mark(ds, "rename", _applied(args))
        return 0

    if cmd == "wash":
        extra = json.loads(Path(args.rules).read_text(encoding="utf-8")) if args.rules else None
        m = manifest.ensure(ds)
        wash.cmd_wash(ds, apply=args.apply, trigger=args.trigger or m.trigger, rules_over=extra,
                      include_images=not args.no_images)
        _mark(ds, "wash", _applied(args))
        return 0

    if cmd == "review-list":
        wash.cmd_review_list(ds)
        return 0

    if cmd == "apply-review":
        wash.cmd_apply_review(ds, args.payload, apply=args.apply)
        _mark(ds, "review", _applied(args))
        return 0

    if cmd == "verify":
        wash.cmd_verify(ds, online=args.online, sample=args.sample)
        return 0

    if cmd == "makecfg":
        subdirs = [s.strip() for s in args.subdirs.split(",") if s.strip()] or None
        repeats = {}
        if subdirs:
            clean = []
            for s in subdirs:
                if ":" in s:
                    n, r = s.split(":", 1)
                    repeats[n.strip()] = int(r)
                    clean.append(n.strip())
                else:
                    clean.append(s)
            subdirs = clean
        m = manifest.ensure(ds)
        trigger = args.trigger or m.trigger
        makecfg.cmd_makecfg(ds, name=args.name or ds.name, trigger=trigger, kind=args.kind,
                            subdirs=subdirs, dim=args.dim or None, lr=args.lr or None,
                            epochs=args.epochs or None, resolution=args.resolution,
                            batch=args.batch, grad_accum=args.grad_accum,
                            repeats=repeats or None, apply=args.apply,
                            dataset_dir=args.dataset_dir or None, force=args.force)
        _mark(ds, "config", _applied(args))
        return 0

    if cmd == "doctor":
        _doctor(args.home)
        return 0

    return 1


def _applied(args) -> bool:
    """这一跑到底写盘了没有。

    两类子命令的开关语义相反：fetch/import 默认真下载/真导入，用 --dry-run 退出；
    其余阶段默认只出报告，要 --apply 才写盘。dry-run 跑完绝不能把阶段标成 done，
    否则 status 会撒谎，agent 会跳过 apply。
    """
    if hasattr(args, "dry_run"):
        return not args.dry_run
    return bool(getattr(args, "apply", False))


def _mark(ds, stage: str, applied: bool = True, **info) -> None:
    """applied=False 时只记 `preview`（跑过一遍、没写盘），不声称完成。"""
    m = manifest.ensure(ds)
    m.set_stage(stage, "done" if applied else "preview", **info)
    m.save()


def _doctor(home: str | None = None) -> None:
    cfg = cfgmod.Config.load(home=home)
    print(f"animasl {__import__('animasl').__version__}")
    print(f"  home            {cfg.home}")
    print(f"  bundle          {cfgmod.BUNDLE_ROOT}")
    print(f"  runtime         {cfgmod.runtime_dir()}")
    print(f"  数据集用 python  {cfgmod.find_python(cfg)}")
    ml_py = cfgmod.find_ml_python(cfg)
    print(f"  ML python       {ml_py}")
    for key, path in (("trainer", Path(cfg.trainer_dir) / "anima_train_network.py"),
                      ("anima_lora", Path(cfg.anima_lora_dir)),
                      ("koharu", Path(cfg.koharu_dir)),
                      ("tag dict", Path(cfg.tag_dict)),
                      ("danbooru general", Path(cfg.danbooru_general))):
        print(f"  {key:<15} {'OK ' if Path(path).exists() else '缺失'} {path}")
    for which, label in (("layout", "rfdetr 文字检测"), ("inpaint", "lama 修补")):
        model = cfgmod.find_model(cfg, which)
        print(f"  {label:<13} {'OK ' if model else '缺失'} {model or ''}")
    code = ("import torch, rfdetr;"
            "from importlib.metadata import version;"
            "print(torch.__version__, torch.cuda.is_available(), version('rfdetr'))")
    try:
        out = subprocess.run([ml_py, "-c", code], capture_output=True, text=True, timeout=180,
                             encoding="utf-8", errors="replace")
        line = (out.stdout or "").strip().splitlines()
        line = line[-1] if line else (out.stderr or "").strip()[:160]
        print(f"  ML 依赖         {line}")
    except Exception as exc:
        print(f"  ML 依赖         探测失败 {exc}")
    proxies = cfg.get("proxy_candidates") or [""]
    print(f"  代理候选        直连 + {', '.join(proxies[1:])}")
    for host in ("pawchive.pw", "yande.re", "danbooru.donmai.us"):
        url = f"https://{host}/"
        try:
            s = net.session(cfg, url, {})
            r = s.get(url, timeout=20)
            print(f"  {host:<20} HTTP {r.status_code}  via {net.proxy_label()}")
        except Exception as exc:
            print(f"  {host:<20} 不可达  {type(exc).__name__}: {str(exc)[:70]}")
    try:
        td = dicts.load(cfg)
        print(f"  词典            {td.size()} 个标签")
    except Exception as exc:
        print(f"  词典            加载失败 {exc}")

    # 各图源的账号：yande.re 完全不需要；danbooru 只有多标签查询需要；pawchive 公开接口不需要
    # （受限帖要 cookie）；exhentai 必须有登录态。这里只报告"有没有配、配在哪一层"，
    # 不打印密钥本身。取值顺序与运行期一致：设置页写的配置层 > 环境变量。
    login, key = net.danbooru_auth(cfg)
    if login and key:
        src = "设置页" if str(cfg.get("danbooru_login") or "").strip() else "环境变量"
        print(f"  凭据 danbooru    OK  {src}：{login[:2]}*** + API key（多标签查询可用）")
    else:
        print("  凭据 danbooru    未配置：匿名只能单标签且限速极严；多标签要在 danbooru 个人设置页"
              "拿 API key，填进设置页或设 DANBOORU_LOGIN + DANBOORU_API_KEY")

    ck = str(cfg.get("cookies_file") or "")
    jar_domains: dict[str, int] = {}
    if ck and Path(ck).is_file():
        try:
            jar = net.load_cookie_jar(ck)
            for c in jar:
                jar_domains[(c.domain or "(空)")] = jar_domains.get((c.domain or "(空)"), 0) + 1
        except Exception:
            jar_domains = {}

    exh_cfg = [k for k in ("exhentai_member_id", "exhentai_pass_hash", "exhentai_igneous")
               if str(cfg.get(k) or "").strip()]
    exh_env = [k for k in ("EXHENTAI_MEMBER_ID", "EXHENTAI_PASS_HASH", "EXHENTAI_IGNEOUS")
               if os.environ.get(k)]
    if exh_cfg:
        print(f"  凭据 exhentai    OK  设置页：{len(exh_cfg)}/3 项（ipb_member_id / ipb_pass_hash / igneous）")
    elif exh_env:
        print(f"  凭据 exhentai    OK  环境变量：{', '.join(exh_env)}")
    elif any("exhentai" in d for d in jar_domains):
        print(f"  凭据 exhentai    OK  cookies.txt（{sum(v for k, v in jar_domains.items() if 'exhentai' in k)} 条 .exhentai.org）")
    else:
        print("  凭据 exhentai    未配置（exhentai 必须登录态：设置页填三项 / cookies.txt / EXHENTAI_* 环境变量）")

    if ck and Path(ck).is_file():
        st = Path(ck).stat()
        domains = "、".join(f"{d}×{n}" for d, n in sorted(jar_domains.items())) or "解析不到 cookie 行"
        print(f"  凭据 cookies     OK  {ck}（{st.st_size} B，"
              f"{time.strftime('%Y-%m-%d', time.localtime(st.st_mtime))}；{domains}）")
    elif ck:
        print(f"  凭据 cookies     路径不存在 {ck}")
    else:
        print("  凭据 cookies     未配置（pawchive 公开接口不需要；受限帖 / exhentai 需要；"
              "设置页填 cookies_file，或用 fetch 的 cookies= 临时指定 Netscape cookies.txt）")



if __name__ == "__main__":
    raise SystemExit(main())
