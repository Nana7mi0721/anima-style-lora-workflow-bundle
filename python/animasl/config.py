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

    def images(self, exts=(".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")) -> list[Path]:
        if not self.images_dir.exists():
            return []
        return sorted(p for p in self.images_dir.iterdir() if p.is_file() and p.suffix.lower() in exts)

    def raw_images(self, exts=(".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")) -> list[Path]:
        if not self.raw_dir.exists():
            return []
        return sorted(p for p in self.raw_dir.iterdir() if p.is_file() and p.suffix.lower() in exts)


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
