"""数据集级状态：每张图一条、只追加，caption 的唯一基准是 images/<stem>.txt。

为什么要有这个模块（来自一次真实使用的复盘）
------------------------------------------------
跑完一次 72 张图的完整流程，技术卡点（去重 / 筛除 / 文字检测）全顺，成本全花在
「状态文件的所有权」上：

  * rename_map.csv 是一次性快照，第二次 rename 整文件覆盖 ⇒ 第一批 26 行的元数据没了；
  * images/*.txt 既是 wash 的输出、又是人工/视觉补标的输入，重跑 wash 会把人工
    删掉的标签从图源标签里重新并回来；
  * 图片、编号、来源、post_id、修补状态、视觉台账散在 5 个文件里，谁都能写，
    没有单一事实来源 ⇒ 72 张图里靠外部脚本救了 3 次场。

这里给出两句话的契约：

  1. `_pipeline/per_image.json` 是「这张图的一切已知事实」的唯一事实来源，
     **只追加不删除**：改名只往 old_names 里追加，跑过的阶段往 history 里追加。
  2. caption 的唯一权威副本是 `images/<stem>.txt`。下游一律读它，
     **不要读 rename_map.csv 里的 tags_full**（那是下载时的原始标签，不是成品）。

其余模块通过 State 读写这些事实：curate.rename 记编号与来源，wash 记「并入了哪些
图源标签」「哪几个标签是人删掉的」，apply-review / fix-caption 记人工改动。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STATE_NAME = "per_image.json"
VERSION = 1
HISTORY_CAP = 30
IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(v) for v in value if str(v)]


def _union(old, new) -> list[str]:
    """保序并集：老的在前，新的追加在后面（只追加语义）。"""
    out = _as_list(old)
    seen = set(out)
    for item in _as_list(new):
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def state_file(ds) -> Path:
    return ds.pipe_dir / STATE_NAME


def caption_path(ds, name: str) -> Path:
    """caption 的唯一权威路径。"""
    return ds.images_dir / (Path(name).stem + ".txt")


def locate(ds, name: str) -> tuple[Path | None, str]:
    """在 images/ 与 00_raw/ 里找这张图（先 images/）。返回 (路径, "images"|"00_raw"|"")。"""
    stem = Path(name).stem
    for base, where in ((ds.images_dir, "images"), (ds.raw_dir, "00_raw")):
        if not base.exists():
            continue
        direct = base / name
        if direct.is_file():
            return direct, where
        for p in base.iterdir():
            if p.is_file() and p.stem == stem and p.suffix.lower() in IMG_EXTS:
                return p, where
    return None, ""


def resolve_name(ds, name: str) -> str:
    """把 0001 / 0001.jpg / 旧文件名 都解析成当前文件名（解析不出来就原样返回）。"""
    stem = Path(name).stem
    hit, _ = locate(ds, name)
    if hit is not None:
        return hit.name
    st = load(ds)
    if stem in st.images:
        return stem
    for cur, item in st.images.items():
        if stem == Path(cur).stem or stem in _as_list(item.get("old_names")):
            return cur
    for cur, item in st.images.items():
        for old in _as_list(item.get("old_names")):
            if Path(old).stem == stem:
                return cur
    return name


class State:
    """`_pipeline/per_image.json` 的读写封装。"""

    def __init__(self, ds, data: dict | None = None) -> None:
        self.ds = ds
        self.path = state_file(ds)
        data = data or {}
        self.data = {
            "version": VERSION,
            "dataset": data.get("dataset") or ds.name,
            "created_at": data.get("created_at") or _now(),
            "updated_at": data.get("updated_at") or "",
            "images": data.get("images") or {},
            "batches": data.get("batches") or [],
        }

    # -- 读写 ----------------------------------------------------------
    @classmethod
    def load(cls, ds) -> "State":
        path = state_file(ds)
        if not path.exists():
            return cls(ds)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            # 状态文件坏了不能挡流程：留个 .broken 副本，从空状态继续
            try:
                path.replace(path.with_suffix(".json.broken"))
            except Exception:
                pass
            return cls(ds)
        if not isinstance(data, dict):
            return cls(ds)
        return cls(ds, data)

    def save(self) -> Path:
        self.data["updated_at"] = _now()
        self.data["version"] = VERSION
        self.ds.pipe_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.ds.pipe_dir), prefix=".per_image.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text + "\n")
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
        return self.path

    # -- 查询 ----------------------------------------------------------
    @property
    def images(self) -> dict:
        return self.data["images"]

    def get(self, name: str) -> dict:
        return self.images.get(name) or {}

    def has(self, name: str) -> bool:
        return name in self.images

    def names(self) -> list[str]:
        return sorted(self.images)

    def find_by_old_name(self, old: str) -> str:
        if old in self.images:
            return old
        stem = Path(old).stem
        for cur, item in self.images.items():
            for candidate in _as_list(item.get("old_names")):
                if candidate == old or Path(candidate).stem == stem:
                    return cur
        return ""

    def merged_tags(self, name: str) -> list[str]:
        """上一次 wash 从图源标签里并进来的标签（用来识别「人删掉的」）。"""
        return _as_list(self.get(name).get("merged_tags"))

    def manual_removed(self, name: str) -> list[str]:
        return _as_list(self.get(name).get("manual_removed"))

    def caption(self, name: str) -> str:
        """权威 caption：优先读 images/<stem>.txt，读不到回落到状态里记的副本。"""
        path = caption_path(self.ds, name)
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except Exception:
                pass
        return str(self.get(name).get("caption") or "")

    # -- 写入 ----------------------------------------------------------
    def record(self, name: str, **fields) -> dict:
        """把已知事实并进这条记录。只追加：old_names / merged_tags / manual_removed
        取并集，history 追加，标量字段覆盖（None 与空串不覆盖）。"""
        item = self.images.setdefault(
            name,
            {"name": name, "stem": Path(name).stem, "first_seen": _now()},
        )
        history = fields.pop("history", None)
        for key, value in fields.items():
            if value is None:
                continue
            if key in ("old_names", "merged_tags", "manual_removed"):
                item[key] = _union(item.get(key), value)
            elif key == "vision" and isinstance(value, dict):
                merged = dict(item.get("vision") or {})
                merged.update(value)
                item["vision"] = merged
            elif isinstance(value, str) and value == "" and key not in ("caption",):
                continue
            else:
                item[key] = value
        item["name"] = name
        item["stem"] = Path(name).stem
        item["updated_at"] = _now()
        if history:
            rows = item.get("history") or []
            rows.append({"at": item["updated_at"], **history})
            item["history"] = rows[-HISTORY_CAP:]
        return item

    def note_manual_removed(self, name: str, tags) -> list[str]:
        """记住「这几个标签是人为删掉的」，重跑 wash 时不再从图源标签复活。"""
        clean = [t for t in _as_list(tags) if t]
        if not clean:
            return self.manual_removed(name)
        self.record(name, manual_removed=clean, history={"what": "manual-remove", "tags": clean})
        return self.manual_removed(name)

    def clear_manual_removed(self, name: str, tags=None) -> list[str]:
        """把标签从「人为删除」名单里放出来（下次 wash 可以重新并入）。"""
        item = self.get(name)
        keep = set(_as_list(item.get("manual_removed")))
        drop = set(_as_list(tags)) if tags else keep
        item["manual_removed"] = sorted(keep - drop)
        self.record(name, history={"what": "manual-remove-cleared", "tags": sorted(drop)})
        return item["manual_removed"]

    def link_rename(self, new_name: str, old_name: str, **fields) -> dict:
        """改名：新名字建一条、把旧名字并进 old_names，同时把旧记录的事实搬过来。"""
        old = self.images.pop(old_name, None)
        item = self.record(new_name, old_names=[old_name], **fields)
        if old:
            for key, value in old.items():
                if key in ("name", "stem", "first_seen", "updated_at", "history"):
                    continue
                if key not in item or item.get(key) in (None, "", [], {}):
                    item[key] = value
                elif key in ("old_names", "merged_tags", "manual_removed"):
                    item[key] = _union(item.get(key), value)
            item["history"] = ((old.get("history") or []) + (item.get("history") or []))[-HISTORY_CAP:]
        return item

    def add_batch(self, start: int, end: int, count: int, report: str) -> dict:
        row = {"start": start, "end": end, "count": count, "report": report, "at": _now()}
        self.data["batches"] = (self.data["batches"] or []) + [row]
        return row

    def caption_stats(self) -> dict:
        """状态里记的 caption 概况（不读盘）。"""
        rows = [self.get(n) for n in self.images]
        counts = [int(r.get("tag_count") or 0) for r in rows]
        return {
            "tracked": len(rows),
            "with_caption": sum(1 for r in rows if r.get("caption")),
            "min_tags": min(counts) if counts else 0,
            "max_tags": max(counts) if counts else 0,
            "avg_tags": round(sum(counts) / len(counts), 1) if counts else 0.0,
        }


def load(ds) -> State:
    return State.load(ds)


def sync_images(ds, st: State | None = None) -> dict:
    """把 images/ 里的实际文件与状态对齐：新图建记录、消失的图只记一笔（不删记录）。"""
    st = st or State.load(ds)
    present = {p.name for p in ds.images()} if ds.images_dir.exists() else set()
    added = []
    for name in sorted(present):
        if not st.has(name):
            st.record(name, first_seen=_now(), history={"what": "adopted"})
            added.append(name)
    missing = [n for n in st.names() if n not in present]
    if missing:
        for name in missing:
            item = st.get(name)
            if item.get("missing_at"):
                continue
            st.record(name, missing_at=_now(), history={"what": "missing-from-images"})
    return {"added": added, "missing": missing, "present": len(present)}


_LEADING_TRIGGER = re.compile(r"^@\S+")


def caption_health(ds, trigger: str = "", min_tags: int = 20, max_tags: int = 45) -> dict:
    """caption 健康度（A5）：形态问题 + 触发词位置 + 有图无 txt / 有 txt 无图。"""
    imgs = {p.stem: p.name for p in ds.images()} if ds.images_dir.exists() else {}
    txts = {p.stem: p for p in sorted(ds.images_dir.glob("*.txt"))} if ds.images_dir.exists() else {}
    missing_txt = sorted(name for stem, name in imgs.items() if stem not in txts)
    orphan_txt = sorted(p.name for stem, p in txts.items() if stem not in imgs)
    counts: list[int] = []
    under: list[str] = []
    over: list[str] = []
    bad_trigger: list[str] = []
    empty: list[str] = []
    for stem, path in txts.items():
        if stem not in imgs:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        body = text.strip()
        if not body:
            empty.append(path.name)
            continue
        tags = [t.strip() for t in body.split(",") if t.strip()]
        counts.append(len(tags))
        if len(tags) < min_tags:
            under.append(path.name)
        elif len(tags) > max_tags:
            over.append(path.name)
        if trigger:
            if not body.startswith(trigger) or body.count(trigger) != 1:
                bad_trigger.append(path.name)
    return {
        "images": len(imgs),
        "captions": len(txts),
        "missing_txt": missing_txt,
        "orphan_txt": orphan_txt,
        "empty": empty,
        "min_tags": min(counts) if counts else 0,
        "max_tags": max(counts) if counts else 0,
        "avg_tags": round(sum(counts) / len(counts), 1) if counts else 0.0,
        "under_min": under,
        "over_max": over,
        "trigger_not_first": bad_trigger,
    }


def health_lines(ds, trigger: str = "", min_tags: int = 20, max_tags: int = 45) -> list[str]:
    """caption 健康度 → 给人看的几行。"""
    h = caption_health(ds, trigger=trigger, min_tags=min_tags, max_tags=max_tags)
    out = [
        f"  caption 健康度  图 {h['images']} 张 / txt {h['captions']} 个"
        f"　标签数 {h['min_tags']}~{h['max_tags']}（均 {h['avg_tags']}）",
        f"                  低于 {min_tags}：{len(h['under_min'])}"
        f"　高于 {max_tags}：{len(h['over_max'])}"
        f"　空 caption：{len(h['empty'])}"
        + (f"　触发词不在首位/重复：{len(h['trigger_not_first'])}" if trigger else ""),
    ]
    if h["missing_txt"]:
        out.append(f"  ⚠ 有图无 txt（{len(h['missing_txt'])}）：{', '.join(h['missing_txt'][:8])}"
                   + (" …" if len(h["missing_txt"]) > 8 else ""))
    if h["orphan_txt"]:
        out.append(f"  ⚠ 有 txt 无图（{len(h['orphan_txt'])}）：{', '.join(h['orphan_txt'][:8])}"
                   + (" …" if len(h["orphan_txt"]) > 8 else ""))
    if h["under_min"]:
        out.append(f"  ⚠ 标签过少：{', '.join(h['under_min'][:8])}" + (" …" if len(h["under_min"]) > 8 else ""))
    if h["over_max"]:
        out.append(f"  ⚠ 标签过多：{', '.join(h['over_max'][:8])}" + (" …" if len(h["over_max"]) > 8 else ""))
    if h["empty"]:
        out.append(f"  ⚠ 空 caption：{', '.join(h['empty'][:8])}" + (" …" if len(h["empty"]) > 8 else ""))
    if h["trigger_not_first"]:
        out.append(f"  ⚠ 触发词问题：{', '.join(h['trigger_not_first'][:8])}"
                   + (" …" if len(h["trigger_not_first"]) > 8 else ""))
    return out
