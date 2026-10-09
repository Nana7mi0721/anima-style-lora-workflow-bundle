"""Workspace configuration and per-dataset layout.

Resolution order for every setting:  CLI flag > environment > bundle config file
> built-in default.  The bundle config file lives next to this package:
`<bundle>/animasl.config.json`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

BUNDLE_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = BUNDLE_ROOT / "animasl.config.json"

IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
# 这些根目录下的子目录永远不算图桶：00_raw 是原始素材，thumbs/masks 是派生物
BUCKET_SKIP = frozenset({"00_raw", "thumbs", "masks", "orig_text"})


def _has_images(path: Path) -> bool:
    try:
        return any(p.is_file() and p.suffix.lower() in IMG_EXTS for p in path.iterdir())
    except OSError:
        return False


def dir_bytes(path: Path) -> int:
    """目录占用（递归；读不到就当 0）。

    用来在阶段结束时报一句"这个目录现在占多少"：`_pipeline/orig_text/`（修补前原图备份）
    和 `_pipeline/thumbs/`（看图缩略图）是这条流水线里唯二会长到与图片本体同量级的东西，
    用户需要知道它们在哪、能不能删。
    """
    total = 0
    try:
        entries = list(path.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_dir():
                total += dir_bytes(entry)
            elif entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def human_bytes(n: int) -> str:
    """人类可读的字节数（与插件侧 lib/run.js 的 humanSize 同口径）。"""
    if not n:
        return "0B"
    units = ["B", "K", "M", "G", "T"]
    value = float(n)
    i = 0
    while value >= 1024 and i < len(units) - 1:
        value /= 1024
        i += 1
    return f"{round(value)}B" if i == 0 else f"{value:.1f}{units[i]}" if value < 10 else f"{round(value)}{units[i]}"


def runtime_dir() -> Path:
    """Heavy runtime state (ML venv, caches) -- kept out of the installed package.

    Resolution: $ANIMASL_RUNTIME > <home>/.animasl > ~/.animasl
    """
    env = os.environ.get("ANIMASL_RUNTIME")
    if env:
        return Path(env)
    home = os.environ.get("ANIMASL_HOME") or DEFAULTS["home"]
    return Path(home) / ".animasl"


def runtime_config_file() -> Path:
    """The per-user config the settings page writes (`<runtime>/animasl.config.json`)."""
    return runtime_dir() / "animasl.config.json"


# Top-level keys that also honour an `ANIMASL_<KEY>` environment variable.
# Nested tables (screen / dedup / wash) have no environment form -- edit them in
# the runtime config file (the settings page does exactly that).
ENV_KEYS = ("home", "datasets_dir", "configs_dir", "output_dir", "python", "ml_python",
            "models_dir", "anima_lora_dir", "trainer_dir", "koharu_dir", "cookies_file",
            "layout_model", "inpaint_model", "tag_dict")


DEFAULTS: dict[str, Any] = {
    # workspace roots
    "home": "E:/LoRA_Train",
    "datasets_dir": "",          # empty -> <home>/datasets
    "configs_dir": "",           # empty -> <home>/train_configs
    "output_dir": "",            # empty -> <home>/output
    # python / tool paths
    "python": "",                # interpreter used for GPU stages
    "ml_python": "",             # empty -> <runtime>/venv, then <bundle>/python/.venv
    "models_dir": "E:/LoRA_Train/anima_lora/models",
    "anima_lora_dir": "E:/LoRA_Train/anima_lora",
    "trainer_dir": "E:/LoRA_Train/Anima-Standalone-Trainer",
    "koharu_dir": "D:/Program/koharu",
    # tag dictionaries (offline)
    "tag_dict": "E:/LoRA_Train/tags.json",
    "danbooru_general": "E:/LoRA_Train/BooruDatasetTagManagerPlus/Data/danbooru_dataset_general.csv",
    "danbooru_classified": "E:/LoRA_Train/anima_lora/models/danbooru_tags_classified.csv",
    "character_dict": "E:/LoRA_Train/BooruDatasetTagManagerPlus/Data/danbooru_character_tags.csv",
    # network
    # 直连优先；7897 是本机实测可用的 mixed 端口（pawchive CDN 直连会被 reset）
    "proxy_candidates": ["", "http://127.0.0.1:7897", "http://127.0.0.1:7890",
                         "http://127.0.0.1:10809", "http://127.0.0.1:1080"],
    "prefer_proxy_hosts": ["pawchive.pw", "n1.pawchive.pw", "n2.pawchive.pw", "exhentai.org", "e-hentai.org"],
    "cookies_file": "",          # netscape cookies.txt used by gallery-dl style sources
    # curl 兜底（Cloudflare 挑战 requests 时改走 curl）用的 UA；空 = 内置的
    # "animasl/0.1 (+curl)"。千万别填浏览器 UA：那正好会触发 danbooru 的挑战。
    "curl_user_agent": "",
    # 凭据（设置页可填；填了就不必设环境变量。danbooru 走 HTTP basic auth，
    # exhentai 走三个 cookie。只写进 <home>/.animasl/animasl.config.json，不会进仓库）
    "danbooru_login": "",
    "danbooru_api_key": "",
    "exhentai_member_id": "",
    "exhentai_pass_hash": "",
    "exhentai_igneous": "",
    # model weights for the text-removal stage
    "layout_model": "",          # empty -> auto-detect under koharu_dir/store
    "inpaint_model": "",
    # pipeline defaults (all overridable per dataset)
    "screen": {
        "min_short_side": 512,
        "min_bytes": 102400,
        "earliest_date": "2015-01-01",
        "sketch_tags": [
            "sketch", "rough", "lineart", "unfinished", "character sheet",
            "monochrome", "greyscale", "sketchbook", "traditional media",
            "wip", "colorized", "partially colored",
        ],
        "text_warn_ratio": 0.08,
        "text_drop_ratio": 0.30,
    },
    "dedup": {
        "phash_distance": 4,
        "ssim_threshold": 0.995,
    },
    "wash": {
        "min_tags": 20,
        "max_tags": 45,
        # 角色名频率门槛：0 = 不限制（本预设的默认值，按用户拍板取消）。
        # 指南 §8.2 建议设 4（只保留在本数据集出现 >=4 次的角色），要跟随指南就改成 4。
        "character_min_images": 0,
        # 词典 category 整类丢弃（指南 §2.3）：1=artist 3=copyright 5=meta
        "category_drop": [1, 3, 5],
        "category_keep": [],
        "keep_parent_tags": [],
    },
    # 空间卫生：这条流水线里所有"备份/快照"类副产物的保留量。
    # 一次真实使用累积了 6 代 × 5 个训练配置 = 30 个几乎一样的 .bak（其中 25 个是同一个
    # 配置的不同注脚），所以要"默认最小、需要时再放开"：
    #   caption_snapshots  每次写 caption 前留的快照代数（1 = 只留上一版；0 = 不留）
    #   rename_snapshots   每批 rename 的对照表快照份数（权威表 rename_map.csv 不受影响）
    #   makecfg_backups    训练配置的 .bak 代数（1 = 只留上一代；0 = 不留备份）
    #   keep_masks         text 修补后是否保留掩膜（默认不留，失败的那些始终保留）
    "hygiene": {
        "caption_snapshots": 1,
        "rename_snapshots": 3,
        "makecfg_backups": 1,
        "keep_masks": False,
    },
}


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # pragma: no cover - defensive
        raise SystemExit(f"[animasl] cannot parse {path}: {exc}") from exc


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def env_layer() -> dict:
    """The `ANIMASL_*` overrides actually present in this process environment."""
    over: dict[str, Any] = {}
    for key in ENV_KEYS:
        value = os.environ.get("ANIMASL_" + key.upper())
        if value:
            over[key] = value
    return over


def layers(cli: dict | None = None) -> dict:
    """Every configuration layer separately, plus what the merge produces.

    The settings page shows the runtime layer as the editable one and uses this
    to explain where an effective value comes from (default / bundle / runtime /
    environment / CLI). Keys are the raw file contents, so nothing is invented.
    """
    defaults = json.loads(json.dumps(DEFAULTS))
    bundle = _read_json(CONFIG_FILE)
    runtime = _read_json(runtime_config_file())
    env = env_layer()
    data = _deep_merge(defaults, bundle)
    data = _deep_merge(data, runtime)
    data = _deep_merge(data, env)
    data = _deep_merge(data, {k: v for k, v in (cli or {}).items() if v not in (None, "")})
    return {"defaults": defaults, "bundle": bundle, "runtime": runtime, "env": env, "merged": data}


class Config:
    """Merged configuration for one animasl invocation."""

    def __init__(self, cli: dict | None = None) -> None:
        data = dict(DEFAULTS)
        data = _deep_merge(data, _read_json(CONFIG_FILE))
        data = _deep_merge(data, _read_json(runtime_config_file()))
        data = _deep_merge(data, env_layer())
        data = _deep_merge(data, {k: v for k, v in (cli or {}).items() if v not in (None, "")})
        self.data = data

    # -- generic access -------------------------------------------------
    @classmethod
    def load(cls, **cli: Any) -> "Config":
        """Convenience constructor: Config.load(home=...) ignores empty cli values."""
        return cls({k: v for k, v in cli.items() if v not in (None, "")})

    def as_dict(self) -> dict:
        """A detached copy of the merged data (safe to serialise)."""
        return json.loads(json.dumps(self.data))

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def path(self, key: str) -> Path:
        return Path(str(self.data[key]))

    # -- resolved roots -------------------------------------------------
    @property
    def home(self) -> Path:
        return self.path("home")

    @property
    def datasets_dir(self) -> Path:
        return Path(self.data["datasets_dir"]) if self.data["datasets_dir"] else self.home / "datasets"

    @property
    def configs_dir(self) -> Path:
        return Path(self.data["configs_dir"]) if self.data["configs_dir"] else self.home / "train_configs"

    @property
    def output_dir(self) -> Path:
        return Path(self.data["output_dir"]) if self.data["output_dir"] else self.home / "output"

    @property
    def models_dir(self) -> Path:
        return self.path("models_dir")

    @property
    def trainer_dir(self) -> Path:
        return self.path("trainer_dir")

    @property
    def anima_lora_dir(self) -> Path:
        return self.path("anima_lora_dir")

    @property
    def koharu_dir(self) -> Path:
        return self.path("koharu_dir")

    @property
    def tag_dict(self) -> Path:
        return self.path("tag_dict")

    @property
    def danbooru_general(self) -> Path:
        return self.path("danbooru_general")

    @property
    def danbooru_classified(self) -> Path:
        return self.path("danbooru_classified")

    @property
    def character_dict(self) -> Path:
        return self.path("character_dict")

    def dataset(self, name: str) -> "Dataset":
        return Dataset(name, self)


class Dataset:
    """Standard layout of one dataset workspace.

    <datasets_dir>/<name>/
        00_raw/                 downloaded originals, untouched
        images/                 curated + renumbered training images (+ .txt)
        clean/ watermark/ ...   OPTIONAL extra buckets: finished images may live in
                                any root subdir that holds images (see bucket_dirs)
        _pipeline/              every report / manifest produced by the toolchain
        _excluded/              rejected images, kept as evidence
    """

    SUBDIRS = ("00_raw", "images", "_pipeline", "_excluded", "_pipeline/thumbs")

    def __init__(self, name: str, cfg: Config) -> None:
        self.name = name
        self.cfg = cfg
        self.root = cfg.datasets_dir / name
        self.raw_dir = self.root / "00_raw"
        self.images_dir = self.root / "images"
        self.pipe_dir = self.root / "_pipeline"
        self.excluded_dir = self.root / "_excluded"
        self.thumbs_dir = self.pipe_dir / "thumbs"
        self.manifest_file = self.pipe_dir / "manifest.json"

    # -- helpers --------------------------------------------------------
    def exists(self) -> bool:
        return self.manifest_file.exists()

    def ensure_dirs(self) -> None:
        for sub in self.SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def report(self, name: str) -> Path:
        return self.pipe_dir / name

    def require(self) -> "Dataset":
        if not self.exists():
            raise SystemExit(
                f"[animasl] dataset '{self.name}' has no manifest at {self.manifest_file}.\n"
                f"          run:  animasl init {self.name}"
            )
        return self

    def images(self, exts=IMG_EXTS) -> list[Path]:
        if not self.images_dir.exists():
            return []
        return sorted(p for p in self.images_dir.iterdir() if p.is_file() and p.suffix.lower() in exts)

    def raw_images(self, exts=IMG_EXTS) -> list[Path]:
        if not self.raw_dir.exists():
            return []
        return sorted(p for p in self.raw_dir.iterdir() if p.is_file() and p.suffix.lower() in exts)

    # -- 图桶（bucket）：成品图不必都住在 images/ ---------------------------------
    #
    # 真实使用（小叶子miv，146 张）里成品被人工分成了 clean/ 109 + watermark/ 33，
    # images/ 是空的；而工具只认 images/ ⇒ thumbs、verify、fix-caption、status 全部
    # 报 0，makecfg 又按根下子目录扫、把同一批图数了两遍（142 → 284）。
    #
    # 统一口径：**成品图桶 = 数据集根目录下、名字不以 `_`/`.` 开头、含图、且不是
    # 00_raw / thumbs / masks 的子目录**。images/ 只是其中一个（排在最前）。
    # 各阶段用 bucket_dirs() / work_images() 拿工作集，不再各自写死 images/。
    def bucket_dirs(self, work_set=None) -> list[tuple[str, Path]]:
        """本次要处理的图桶 → [(桶名, 目录)]，稳定顺序（images/ 优先，其余按名字）。

        work_set 传了就只用这些桶（名字或相对/绝对路径，逗号分隔的字符串也收），
        传错时报错并列出自动发现的桶，避免静默处理 0 张图。
        """
        if work_set:
            items = work_set if isinstance(work_set, (list, tuple, set)) else str(work_set).split(",")
            out: list[tuple[str, Path]] = []
            for raw in items:
                text = str(raw).strip().strip("/\\")
                if not text:
                    continue
                path = Path(text)
                if not path.is_absolute():
                    path = self.root / text
                if not (path.is_dir() and _has_images(path)):
                    found = "、".join(name for name, _ in self.discover_buckets()) or "（一个都没有）"
                    raise SystemExit(
                        f"[animasl] workSet 里的 '{text}' 不是数据集里的图桶（没有图或不存在）。\n"
                        f"          自动发现的桶：{found}\n"
                        f"          桶 = 数据集根下含图的子目录，如 images / clean / watermark。"
                    )
                out.append((path.name, path))
            return out
        return self.discover_buckets()

    def discover_buckets(self) -> list[tuple[str, Path]]:
        """自动发现图桶（不含 00_raw，因为那是未处理的原始素材）。"""
        if not self.root.exists():
            return []
        found: list[tuple[str, Path]] = []
        for p in sorted(self.root.iterdir(), key=lambda x: x.name):
            if not p.is_dir() or p.name.startswith(("_", ".")) or p.name in BUCKET_SKIP:
                continue
            if _has_images(p):
                found.append((p.name, p))
        # images/ 排最前：它是 rename 的正式落点，报告里先说它更符合直觉
        found.sort(key=lambda item: (item[0] != "images", item[0]))
        return found

    def bucket_of(self, path) -> str:
        """某个文件属于哪个桶（不在桶里就返回空串）。"""
        try:
            rel = Path(path).resolve().relative_to(self.root.resolve())
        except Exception:
            return ""
        name = rel.parts[0] if rel.parts else ""
        return name if name in {n for n, _ in self.discover_buckets()} else ""

    def work_images(self, work_set=None) -> list[Path]:
        """工作集：所有图桶里的图，按桶顺序、桶内按名字。"""
        out: list[Path] = []
        for _, base in self.bucket_dirs(work_set):
            out.extend(sorted(p for p in base.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXTS))
        return out

    def captions(self, work_set=None) -> list[Path]:
        """工作集里已有的 caption 文件（与图同目录）。"""
        return [p.with_suffix(".txt") for p in self.work_images(work_set) if p.with_suffix(".txt").exists()]

    def bucket_counts(self, work_set=None) -> dict[str, int]:
        return {name: len([p for p in base.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXTS])
                for name, base in self.bucket_dirs(work_set)}


def find_python(cfg: Config) -> str:
    """Pick the interpreter used for the core stages (needs requests+pillow)."""
    import shutil
    import sys

    candidates = [
        cfg.get("python", ""),
        os.environ.get("ANIMASL_PYTHON", ""),
        str(runtime_dir() / "venv" / "Scripts" / "python.exe"),
        str(BUNDLE_ROOT / "python" / ".venv" / "Scripts" / "python.exe"),
        str(Path(cfg.get("anima_lora_dir", "")) / ".venv" / "Scripts" / "python.exe"),
        sys.executable,
        shutil.which("python") or "",
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            return cand
    return "python"


def find_ml_python(cfg: Config) -> str:
    """Interpreter that owns torch + rfdetr (the text detect/inpaint stage)."""
    candidates = [
        cfg.get("ml_python", ""),
        str(runtime_dir() / "venv" / "Scripts" / "python.exe"),
        str(BUNDLE_ROOT / "python" / ".venv" / "Scripts" / "python.exe"),
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            return cand
    return find_python(cfg)


def find_model(cfg: Config, which: str) -> Path | None:
    """Locate the koharu rfdetr / lama weights (explicit config wins)."""
    explicit = cfg.get("layout_model" if which == "layout" else "inpaint_model", "")
    if explicit and Path(explicit).exists():
        return Path(explicit)
    root = Path(cfg.get("koharu_dir", "")) / "store" / "hugging-face" / "models"
    pattern = ("*/snapshots/*/model.safetensors" if which == "layout"
               else "*/snapshots/*/lama-manga.safetensors")
    if root.exists():
        hits = sorted(root.glob(pattern))
        if hits:
            return hits[0]
    return None
