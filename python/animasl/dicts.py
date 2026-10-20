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
import urllib.parse
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
    # 坑：`Path("")` 就是 `Path(".")`，而 `Path(".").exists()` 恒真 ⇒ 没配 tag_dict 的机器
    # 会走到 `_compact_cache(Path("."))` 抛 `ValueError: WindowsPath('.') has an empty name`。
    if str(cfg.get("tag_dict") or "").strip() and tags_json.is_file():
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
    if str(cfg.get("danbooru_general") or "").strip() and gen.is_file():
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

    # 在线拉取过的别名表（`animasl synonyms`）优先级最高：它是 danbooru 的 in-house
    # 别名，比 csv 的 other_names（多数是日文/中文同义写法，不是英文别名）准确得多。
    extra = Path(str(cfg.get("tag_aliases") or ""))
    if extra.exists():
        try:
            rows = json.loads(extra.read_text(encoding="utf-8"))
            for alt, canon in rows.items():
                alt = alt.strip().replace(" ", "_")
                canon = (canon or "").strip().replace(" ", "_")
                if alt and canon and alt != canon:
                    td.alias_to_canonical[alt] = canon
        except Exception:
            pass

    _CACHE["merged"] = td
    return td


def alias_chain(td: TagDict, name: str, limit: int = 8) -> str:
    """跟着别名链走到终点（防止 a→b→c 只跳一步）。返回终点，不为别名则返回原名。"""
    seen: set[str] = set()
    cur = name
    for _ in range(limit):
        nxt = td.canonical(cur)
        if not nxt or nxt == cur or nxt in seen:
            break
        seen.add(cur)
        cur = nxt
    return cur


def resolve(td: TagDict, name: str) -> tuple[str, str]:
    """把用户/模型给的标签归一到 danbooru 真实形态。

    返回 (canonical, why)：
      why = "exact"     本来就在词典里
            "alias"     跟着别名链走到了真实标签（chinese_dress -> qipao）
            "deprecated" 词典里有但 post_count=0（bangs / cap / star 这类）
            "missing"   词典里没有，也没别名可走
    """
    if name in td.meta and td.posts(name) > 0:
        return name, "exact"
    canon = alias_chain(td, name)
    if canon != name and canon in td.meta and td.posts(canon) > 0:
        return canon, "alias"
    if name in td.meta:
        return name, "deprecated"
    return name, "missing"


def lookup_posts(td: TagDict, name: str) -> int:
    return td.posts(name)


def cmd_synonyms(cfg, apply: bool = False, refresh: bool = False, limit: int = 0) -> int:
    """拉 danbooru 的活跃别名表，落盘成 dicts.load() 认得的 tag_aliases。

    为什么非要单独的阶段：本地的 `tags.json`（328k）与 `danbooru_dataset_general.csv`
    都是**规范标签表**，`other_names` 里放的是日文/中文写法（`qipao` 的 other_names
    是「チャイナドレス, 旗袍, Cheongsam」），**不是英文别名**。所以模型写出来的
    `chinese_dress` / `amber_eyes` / `low_angle` / `sofa` 这类 danbooru 认可的历史写法，
    本地词典一条都认不出来 —— g4ku 复盘里「13 个自造词被词典拒收，其中 7 个其实能映射」
    就是这个洞。在线表 41k 条，拉一次存下来，之后纯离线。
    """
    import time

    from . import net

    dest = Path(str(cfg.get("tag_aliases") or ""))
    if not str(dest):
        print("[synonyms] 配置里没有 tag_aliases，先补上路径")
        return 2
    if dest.exists() and not refresh:
        try:
            have = json.loads(dest.read_text(encoding="utf-8"))
        except Exception:
            have = {}
        print(f"[synonyms] 已有 {len(have)} 条：{dest}")
        print("           要重拉加 --refresh")
        return 0

    s = net.danbooru_session(cfg)
    out: dict[str, str] = {}
    page, ok_pages, t0 = 1, 0, time.time()
    while True:
        q = urllib.parse.urlencode({
            "search[status]": "active", "limit": 1000, "page": page,
            "only": "antecedent_name,consequent_name",
        })
        got = net.curl_get_json(s, "https://danbooru.donmai.us/tag_aliases.json?" + q, timeout=90)
        if not got:
            if page == 1:
                print("[synonyms] 一条都没拉到（Cloudflare / 网络），先用本地词典继续；稍后再试")
                return 1
            break
        for row in got:
            alt = (row.get("antecedent_name") or "").strip()
            canon = (row.get("consequent_name") or "").strip()
            if alt and canon and alt != canon:
                out[alt] = canon
        ok_pages += 1
        print(f"  page {page}: +{len(got)}  累计 {len(out)}", flush=True)
        if len(got) < 1000 or (limit and len(out) >= limit):
            break
        page += 1
        time.sleep(0.4)

    if limit:
        out = dict(list(out.items())[:limit])

    # 别名可能指向另一个别名（a→b→c）：压平成一步到位，省得下游反复跟链
    flat = {}
    for alt, canon in out.items():
        cur, seen = canon, set()
        while cur in out and cur not in seen:
            seen.add(cur)
            cur = out[cur]
        flat[alt] = cur

    multi = sum(1 for v in flat.values() if v != out.get(v, v))
    print(f"[synonyms] 拉到 {len(flat)} 条活跃别名（{ok_pages} 页，{time.time()-t0:.0f}s），"
          f"其中 {multi} 条压平了多级别名链")
    for probe in ("chinese_dress", "amber_eyes", "low_angle", "sofa", "hair_clip", "gemstone"):
        if probe in flat:
            print(f"  {probe:16} -> {flat[probe]}")

    if not apply:
        print(f"[synonyms] dry-run：会写 {dest}（{len(flat)} 条）。加 --apply 落盘")
        return 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_text(json.dumps(flat, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    tmp.replace(dest)
    print(f"[synonyms] 已写 {dest}（{len(flat)} 条）。dict-check / wash 下次调用即生效")
    return 0
