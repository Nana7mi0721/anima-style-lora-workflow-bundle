"""Dataset manifest: the state machine that records which stage produced what."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

STAGES = [
    "init",
    "fetch",
    "import",
    "dedup",
    "screen",
    "text",
    "rename",
    "wash",
    "review",
    "config",
]

STAGE_DOC = {
    "init": "数据集工作区建立",
    "fetch": "从 booru 站下载原图 + 帖子元数据",
    "import": "外部来源（pawchive/exhentai/本地包）导入",
    "dedup": "精确 + 感知哈希去重",
    "screen": "规则筛选（分辨率/体积/年代/草图标签）",
    "text": "文字检测 + 修补（rfdetr + lama，可回退 koharu）",
    "rename": "重排编号 NNNN + 生成对照表",
    "wash": "规则引擎洗标（零 token）",
    "review": "视觉子代理复核回填（人数/关系/构图/体位）",
    "config": "生成训练配置三件套",
}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Manifest:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    # -- lifecycle ------------------------------------------------------
    def create(self, name: str, **kw: Any) -> "Manifest":
        self.data = {
            "name": name,
            "created": _now(),
            "updated": _now(),
            "trigger": kw.get("trigger") or "",
            "source": {
                "kind": kw.get("source") or "",
                "artist_tag": kw.get("artist_tag") or "",
                "extra": kw.get("extra") or [],
            },
            "stages": {},
            "config": {},
            "notes": [],
        }
        return self

    def save(self) -> None:
        self.data["updated"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    # -- stage bookkeeping ---------------------------------------------
    def set_stage(self, stage: str, status: str, **info: Any) -> None:
        rec = self.data.setdefault("stages", {}).get(stage, {})
        rec.update(info)
        rec["status"] = status
        rec["at"] = _now()
        self.data["stages"][stage] = rec

    def stage(self, stage: str) -> dict:
        return self.data.get("stages", {}).get(stage, {})

    def stage_status(self, stage: str) -> str:
        return self.stage(stage).get("status", "pending")

    def invalidate_from(self, stage: str) -> list[str]:
        """Mark `stage` and every later stage stale; returns the affected list."""
        if stage not in STAGES:
            raise SystemExit(f"[animasl] unknown stage: {stage}")
        hit = STAGES[STAGES.index(stage):]
        for st in hit:
            if st in self.data.get("stages", {}):
                self.data["stages"][st]["status"] = "stale"
        return hit

    # -- convenience ----------------------------------------------------
    @property
    def trigger(self) -> str:
        return self.data.get("trigger", "")

    def cfg(self, key: str, default: Any = None) -> Any:
        return self.data.get("config", {}).get(key, default)

    def set_cfg(self, key: str, value: Any) -> None:
        self.data.setdefault("config", {})[key] = value

    def summary(self) -> str:
        lines = [f"dataset : {self.data.get('name')}",
                 f"trigger : {self.data.get('trigger') or '(未设置)'}",
                 f"source  : {self.data.get('source', {}).get('kind')} "
                 f"{self.data.get('source', {}).get('artist_tag')}".rstrip(),
                 f"updated : {self.data.get('updated')}",
                 "",
                 "stages:"]
        for st in STAGES:
            if st == "init":
                continue
            rec = self.stage(st)
            status = rec.get("status", "pending")
            extra = ""
            for key in ("count", "kept", "dropped", "patched", "files", "images"):
                if key in rec:
                    extra += f" {key}={rec[key]}"
            if rec.get("note"):
                extra += f"  ({rec['note']})"
            lines.append(f"  {st:<8} {status:<8}{extra}")
        return "\n".join(lines)


def load(ds) -> Manifest:
    m = Manifest(ds.manifest_file)
    if not m.data:
        raise SystemExit(f"[animasl] dataset '{ds.name}' is not initialised (no {ds.manifest_file})")
    return m


def ensure(ds) -> Manifest:
    """Load the manifest, creating an empty one when the dataset is new."""
    m = Manifest(ds.manifest_file)
    if not m.data:
        m.create(ds.name)
    return m
