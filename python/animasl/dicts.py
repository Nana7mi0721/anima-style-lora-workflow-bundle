"""Offline tag dictionaries.

Sources already on disk:
  * tags.json                         328k danbooru tags {category, cn_name, name, post_count}
                                      -> existence check + post_count (underscore form)
  * danbooru_dataset_general.csv      106k general tags with other_names / parent_tag /
                                      category_l1 (Chinese taxonomy) / post_count
  * danbooru_tags_classified.csv      114k tags with a two level Korean taxonomy (anima_lora)
  * danbooru_character_tags.csv       character tags with copyright

A compact TSV cache is written next to each source so later runs load in ~0.3s
instead of ~8s.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

_CACHE: dict[str, "TagDict"] = {}


class TagDict:
    def __init__(self) -> None:
        self.meta: dict[str, dict] = {}       # name -> {category, post_count, l1, l2, parent}
        self.alias_to_canonical: dict[str, str] = {}
        self.children: dict[str, set[str]] = {}

    def __contains__(self, name: str) -> bool:
        return name in self.meta

    def posts(self, name: str) -> int:
        rec = self.meta.get(name)
        return int(rec.get("post_count") or 0) if rec else 0

    def l1(self, name: str) -> str:
        return (self.meta.get(name) or {}).get("l1", "")

    def parent(self, name: str) -> str:
        return (self.meta.get(name) or {}).get("parent", "")

    def canonical(self, name: str) -> str | None:
        return self.alias_to_canonical.get(name)

    def size(self) -> int:
        return len(self.meta)


def _compact_cache(src: Path) -> Path:
    return src.with_suffix(src.suffix + ".animasl.tsv")


def _load_compact(cache: Path) -> TagDict:
    td = TagDict()
    with open(cache, encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        idx = {k: i for i, k in enumerate(header)}
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < len(header):
                parts += [""] * (len(header) - len(parts))
            name = parts[idx["name"]]
            td.meta[name] = {
                "category": int(parts[idx["category"]] or 0),
                "post_count": int(parts[idx["post_count"]] or 0),
                "l1": parts[idx["l1"]],
                "l2": parts[idx["l2"]],
                "parent": parts[idx["parent"]],
            }
    return td


def _write_compact(cache: Path, td: TagDict) -> None:
    with open(cache, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("name\tcategory\tpost_count\tl1\tl2\tparent\n")
        for name, rec in td.meta.items():
            fh.write("\t".join([
                name, str(rec.get("category", 0)), str(rec.get("post_count", 0)),
                rec.get("l1", ""), rec.get("l2", ""), rec.get("parent", ""),
            ]) + "\n")


def load(cfg, rebuild: bool = False) -> TagDict:
    """Merged dictionary: tags.json (existence/post_count) + general csv (parent/l1)."""
    if "merged" in _CACHE and not rebuild:
        return _CACHE["merged"]
    td = TagDict()

    tags_json = Path(str(cfg.get("tag_dict") or ""))
    if tags_json.exists():
        cache = _compact_cache(tags_json)
        if cache.exists() and not rebuild and cache.stat().st_mtime >= tags_json.stat().st_mtime:
            td = _load_compact(cache)
        else:
            data = json.loads(tags_json.read_text(encoding="utf-8"))
            for rec in data:
                name = (rec.get("name") or "").strip()
                if not name:
                    continue
                td.meta[name] = {
                    "category": int(rec.get("category") or 0),
                    "post_count": int(rec.get("post_count") or 0),
                    "l1": "", "l2": "", "parent": "",
                }
            try:
                _write_compact(cache, td)
            except Exception:
                pass

    gen = Path(str(cfg.get("danbooru_general") or ""))
    if gen.exists():
        with open(gen, encoding="utf-8-sig", errors="replace", newline="") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("tag") or "").strip()
                if not name:
                    continue
                rec = td.meta.setdefault(name, {"category": 0, "post_count": 0,
                                                "l1": "", "l2": "", "parent": ""})
                rec["l1"] = (row.get("category_l1") or "").strip()
                rec["l2"] = (row.get("category_l2") or "").strip()
                parent = (row.get("parent_tag") or "").strip()
                if parent:
                    rec["parent"] = parent
                    td.children.setdefault(parent, set()).add(name)
                pc = (row.get("post_count") or "").strip()
                if pc.isdigit() and int(pc) > int(rec.get("post_count") or 0):
                    rec["post_count"] = int(pc)
                names = (row.get("other_names") or "").strip()
                if names:
                    for alt in names.split(","):
                        alt = alt.strip().replace(" ", "_")
                        if alt and alt not in td.alias_to_canonical:
                            td.alias_to_canonical[alt] = name

    _CACHE["merged"] = td
    return td


def lookup_posts(td: TagDict, name: str) -> int:
    return td.posts(name)
