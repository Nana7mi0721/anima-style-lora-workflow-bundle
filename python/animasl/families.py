"""families 阶段：把「差分家族」显式化，再按族的实际张数重建图桶。

ruzhai 那批 424 张的真实使用里，八成工时耗在插件没覆盖的差分家族整理上，为此写过 5 个
一次性脚本 —— 这里把语义固化成一条命令：

    E:/LoRA_Train/_tmp/fam_split.py      声明家族 + 视觉找跨源同族
    E:/LoRA_Train/_tmp/merge_plan2.py    修正版：合并可传递、去重不传递（贪心留最大的）
    E:/LoRA_Train/_tmp/paw_rebucket.py   按 k 重分为 diff1..diff12
    E:/LoRA_Train/_tmp/regroup.py        按族张数重新分桶

四步：

1. **声明家族**：`_pipeline/raw_posts.jsonl` 里 yande.re 用 `parent_id` 链到根（同一棵
   parent 树 = 一族）；pawchive 一个 `post_id` = 一族；没有元数据的图各自单独成族。
   （`rename` 之后当前名是 0001.jpg，靠 `_pipeline/rename_map.csv` 的 old_name 找回原帖。）
2. **跨源合并**：同一组差分常在 yande.re 与 pawchive 各有一份，声明家族是分开的。
   每张图 256x256 灰度、去均值、L2 归一化成签名，两两相关系数 >= `corr` 且分属不同
   声明家族 -> 并查集合并成一族。
3. **同图去重**：同族内「同分辨率 + pHash 距离 <= `phash_distance` + SSIM >= `ssim`
   + 局部差也要小」判同一张（同一幅画的不同编码/重存），只留体积最大的那份（持平看分辨率）。
   **不传递**：A~B~C 不会把 A、C 也判成一张（否则三张真差分会被压成一张）。apply 时落选者
   连同 .txt 移进 `_excluded/duplicates-family/`。
   为什么还要看局部差：`_ssim_arr` 是**全图** SSIM（全局均值/方差/协方差），只改一小块
   时它几乎不动 —— 实测真实画作上贴 6% 宽的小块，SSIM 还有 0.95~0.986，而 8% 面积以下的
   改动能轻松越过 0.995。所以再加三道局部闸（8x8 分块的**最大**平均差 + 最大像素差 +
   全图平均差），落选一张真差分的代价是丢数据，宁可留着。
4. **按族建桶**：桶名 `diff<k>`，k = 该族去重后实际张数；`repeats = round(base/sqrt(k))`，
   base 取 `makecfg.repeats_for(去重后总张数)`，k=1 时 repeats=base。桶名即 k 的好处：
   一个桶里所有族的张数一样，makecfg 只需要一个 repeats。

报告写两份（**dry-run 也写**：预演也要能看结论）：
    _pipeline/families_report.csv   人看：family, declared_sources, size_before,
                                    size_after, bucket, repeats, members
    _pipeline/families.json         机器读；`rebuild=False` 时也是下次复用的依据
"""

from __future__ import annotations

import json
import math
import shutil
import time
from collections import Counter
from pathlib import Path

from .curate import (_hamming, _ssim_arr, load_meta, load_rename_map,
                     record_excluded, write_csv)

try:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = None
except Exception as exc:  # pragma: no cover
    raise SystemExit("[animasl] families 阶段需要 pillow") from exc

try:
    import numpy as np
except Exception as exc:  # pragma: no cover
    raise SystemExit("[animasl] families 阶段需要 numpy") from exc

N = 256            # 视觉签名的边长（fam_split.py 的口径）
SMALL = 64         # SSIM 用的灰度缩略图（与 curate/dedup 同口径）
HASH = 32          # pHash 的输入边长（与 curate/_fingerprint 同口径）
HASH_BITS = 8
DUP_DIR = "duplicates-family"      # _excluded/ 下的落选者目录

# 「同一张图」的局部差闸门。数值来自实测（真实画作 4320x7680，LANCZOS 缩到 64x64 后比）：
#   同图不同编码（JPEG q90 / q75）：mean 0.02~0.07  max 1.0  blockmax 0.1~0.2  ssim 1.0
#   同图贴一块 6% 宽的小块：       mean 1.6~3.4    max 110~194  blockmax 68~106    ssim 0.95~0.986
# 所以三个闸都留了很大余量；宁可多留一张，也别把真差分当重复删掉。
DUP_MEAN = 2.0        # 全图平均灰度差
DUP_MAX = 24.0        # 最大像素差（重编码在缩略图上几乎不超过 1~3）
DUP_BLOCK = 8.0       # 8x8 分块（每块 = 原图 1/8 宽）里的最大平均差

REPORT_CSV = "families_report.csv"
REPORT_JSON = "families.json"
CSV_FIELDS = ["family", "declared_sources", "size_before", "size_after",
              "bucket", "repeats", "members"]

_DCT_BASIS: dict[int, "np.ndarray"] = {}


# --------------------------------------------------------------------------
# 图像签名：一次解码拿到三样
# --------------------------------------------------------------------------
def _dct_basis(n: int) -> "np.ndarray":
    """DCT-II 的正交基（与 curate/_fingerprint 同一套，只是缓存下来反复用）。"""
    basis = _DCT_BASIS.get(n)
    if basis is None:
        k = np.arange(n)
        basis = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n)) * np.sqrt(2.0 / n)
        basis[0] /= math.sqrt(2.0)
        _DCT_BASIS[n] = basis
    return basis


def _phash(tiny: "np.ndarray") -> int:
    """32x32 灰度 -> 2D DCT -> 左上 8x8 -> 中位数二值。"""
    basis = _dct_basis(tiny.shape[0])
    low = (basis @ tiny.astype(np.float32) @ basis.T)[:HASH_BITS, :HASH_BITS].ravel()
    med = np.median(low[1:])
    out = 0
    for bit in (low > med).astype(np.uint8):
        out = (out << 1) | int(bit)
    return out


def _signature(path: Path):
    """(256 去均值 L2 签名, 64x64 灰度, (宽, 高), pHash)；读不出来给 None。

    三样都从**同一次解码**里拿：分开算等于把图读两遍，500 张时白等十几秒。
    """
    try:
        with Image.open(path) as im:
            gray = ImageOps.exif_transpose(im).convert("L")
            size = (gray.width, gray.height)
            big = np.array(gray.resize((N, N), Image.BILINEAR), dtype=np.float32).ravel()
            small = np.array(gray.resize((SMALL, SMALL), Image.LANCZOS), dtype=np.uint8)
            tiny = np.array(gray.resize((HASH, HASH), Image.LANCZOS), dtype=np.float32)
    except Exception as exc:
        print(f"  ! 读不出 {path.name}：{exc}")
        return None
    big -= big.mean()
    norm = float(np.linalg.norm(big))
    if norm > 0:
        big /= norm
    return big, small, size, _phash(tiny)


# --------------------------------------------------------------------------
# 声明家族
# --------------------------------------------------------------------------
def _pid(value):
    """post_id 在 jsonl 里是 int、在 rename_map.csv 里是字符串，统一成可比较的键。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return int(text) if text.isdigit() else text


def _parent_map(meta: dict[str, dict]) -> dict:
    """post_id -> parent_id（只含下载过的帖子；父帖没下载就当它是根）。"""
    out = {}
    for row in meta.values():
        pid, parent = _pid(row.get("post_id")), _pid(row.get("parent_id"))
        if pid is not None and parent is not None:
            out[pid] = parent
    return out


def _yande_root(pid, parents: dict):
    """parent 链走到根；链断了或成环就以当前节点为根（同 fam_split.py）。"""
    seen = set()
    cur = pid
    while cur in parents and cur not in seen:
        seen.add(cur)
        cur = parents[cur]
    return cur


def _declared(label: str, row: dict, parents: dict) -> str:
    """声明家族的键。

    yande.re 同一条 parent 树 = 一族；pawchive（以及其它带 post_id 的源，比如 danbooru
    的多页帖）一个帖子 = 一族；没有元数据的图各自单独成族，键就是文件名。
    """
    src = str(row.get("source") or "").lower()
    pid = _pid(row.get("post_id"))
    if pid is None:
        return f"single:{label}"
    if src.startswith("yande"):
        return f"y:{_yande_root(pid, parents)}"
    return f"{src or 'post'}:{pid}"


def _unique_labels(files: list[Path]) -> list[str]:
    """报告里的身份：同名文件（images/a.jpg 与 clean/a.jpg）会把两行塌成一行，加桶名前缀。"""
    counts = Counter(p.name for p in files)
    return [f"{p.parent.name}/{p.name}" if counts[p.name] > 1 else p.name for p in files]


# --------------------------------------------------------------------------
# 并查集 + 文件搬运
# --------------------------------------------------------------------------
class _UF:
    """并查集（与 fetch/_yande_families、curate/dedup 同一套写法）。"""

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _move_into(src: Path, dest_dir: Path) -> Path:
    """把图连同同名 .txt 搬进目标目录；同名冲突加 -2/-3 后缀，绝不覆盖。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        stem, ext = src.stem, src.suffix
        i = 2
        while dest.exists():
            dest = dest_dir / f"{stem}-{i}{ext}"
            i += 1
        print(f"  ! 同名冲突：{src.parent.name}/{src.name} -> {dest_dir.name}/{dest.name}")
    shutil.move(str(src), str(dest))
    side = src.with_suffix(".txt")
    if side.exists():
        try:
            shutil.move(str(side), str(dest.with_suffix(".txt")))
        except Exception as exc:
            print(f"  ! 侧车搬运失败 {side.name}: {exc}")
    return dest


def _prune_empty_diff(ds) -> int:
    """apply 之后清掉空掉的 diff<k> 目录（regroup.py 也这么收尾）。"""
    removed = 0
    for entry in sorted(ds.root.iterdir()):
        if not entry.is_dir() or not entry.name.startswith("diff"):
            continue
        if not entry.name[4:].isdigit():
            continue
        try:
            if not any(entry.iterdir()):
                entry.rmdir()
                removed += 1
        except OSError:
            continue
    return removed


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _base_repeats(count: int) -> int:
    """makecfg 的 repeats 阶梯（延迟导入：makecfg 会连带拉起 wash/dicts，只在真要算时加载）。"""
    from .makecfg import repeats_for
    return repeats_for(count)

# --------------------------------------------------------------------------
# 计算：声明家族 -> 跨源合并 -> 同族去重
# --------------------------------------------------------------------------
def _rel(ds, path: Path) -> str:
    """数据集内的相对路径（正斜杠）；报告与 families.json 里的身份。"""
    try:
        return path.resolve().relative_to(ds.root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _row_for(path: Path, meta: dict, renamed: dict) -> dict:
    """一张图的图源元数据：先按下载时的文件名查，再按 rename 之后的编号查。"""
    return meta.get(path.name) or renamed.get(path.name) or {}


def _sign_table(files: list[Path]) -> list:
    out = []
    for i, path in enumerate(files, 1):
        out.append(_signature(path))
        if i % 50 == 0 or i == len(files):
            print(f"  签名 {i}/{len(files)}")
    return out


def _vision_pairs(declared: list[str], sigs: list, corr: float) -> list:
    """相关系数 >= corr 且**分属不同声明家族**的图对（跨源合并的唯一依据）。

    500 张以内 O(n^2) 就是 12 万对，用矩阵乘一次算完；分块只是为了压住峰值内存
    （每张 256x256 的签名是 65536 维，全量 stack 起来 500 张就是 130MB）。
    """
    idx = [i for i, s in enumerate(sigs) if s is not None]
    if len(idx) < 2:
        return []
    mat = np.empty((len(idx), N * N), dtype=np.float32)
    for pos, i in enumerate(idx):
        mat[pos] = sigs[i][0]
    pairs: list[tuple[int, int, float]] = []
    step = 64
    for start in range(0, len(idx), step):
        block = mat[start:start + step] @ mat[start:].T
        np.clip(block, -1.0, 1.0, out=block)
        for r in range(block.shape[0]):
            i = idx[start + r]
            row = block[r]
            for jj in np.nonzero(row >= corr)[0]:
                pos = start + int(jj)
                if pos <= start + r:
                    continue
                j = idx[pos]
                if declared[i] == declared[j]:
                    continue
                pairs.append((i, j, float(row[jj])))
    return pairs


def _local_diff(a: "np.ndarray", b: "np.ndarray") -> tuple:
    """两张 64x64 灰度的局部差：(全图平均差, 最大像素差, 8x8 分块里最大的块平均差)。"""
    d = np.abs(a.astype(np.float32) - b.astype(np.float32))
    blocks = d.reshape(8, SMALL // 8, 8, SMALL // 8).mean(axis=(1, 3))
    return float(d.mean()), float(d.max()), float(blocks.max())


def _same_image(a, b, phash_distance: int, ssim: float) -> dict | None:
    """两张图是不是同一张（同一幅画的不同编码/重存）。是就给证据，不是给 None。

    四道闸：pHash 粗筛 -> SSIM -> 三道局部差。局部差是必须的：_ssim_arr 是全图
    SSIM，只改一小块时全局均值/方差几乎不动，越过得很容易（实测 6% 宽的改动还有
    0.95~0.986，更小的改动直接 > 0.995）。把真差分删掉是丢数据，宁可留着。
    """
    dist = _hamming(a[3], b[3])
    if dist > phash_distance:
        return None
    score = _ssim_arr(a[1], b[1])
    if score < ssim:
        return None
    mean, maxd, blockmax = _local_diff(a[1], b[1])
    if mean > DUP_MEAN or maxd > DUP_MAX or blockmax > DUP_BLOCK:
        return None
    return {"ssim": score, "phash": dist, "mean": mean, "max": maxd, "blockmax": blockmax}


def _dedup(files: list[Path], sigs: list, members: list[int],
           phash_distance: int, ssim: float):
    """同族内贪心去重：按 (体积, 面积) 从大到小，只和**已保留**的比，命中就丢自己。

    「不传递」是关键（merge_plan2.py 踩出来的）：A~B、B~C 但 A≁C 时若按并查集合并，
    三张真差分会被压成一张；同一幅画的不同编码/重存才会两张都近似到 0.995。
    """
    def rank(i: int) -> tuple:
        sig = sigs[i]
        w, h = sig[2] if sig else (0, 0)
        return (-_size_of(files[i]), -(w * h), files[i].name)

    kept: list[int] = []
    kept_by_size: dict[tuple, list[int]] = {}
    dropped: dict[int, tuple] = {}
    for i in sorted(members, key=rank):
        sig = sigs[i]
        hit = None
        if sig is not None:
            for k in kept_by_size.get(sig[2], ()):
                sk = sigs[k]
                if sk is None:
                    continue
                evidence = _same_image(sig, sk, phash_distance, ssim)
                if evidence is not None:
                    hit = (k, evidence)
                    break
        if hit is None:
            kept.append(i)
            if sig is not None:
                kept_by_size.setdefault(sig[2], []).append(i)
        else:
            dropped[i] = hit
    return kept, dropped


def _walk(ds, files: list[Path], labels: list[str],
          phash_distance: int, ssim: float, corr: float) -> list[dict]:
    """算一遍：声明家族 + 跨源合并 + 同族去重。只读图，不动文件。"""
    meta = load_meta(ds)
    renamed = load_rename_map(ds)
    parents = _parent_map(meta)
    declared = [_declared(labels[i], _row_for(path, meta, renamed), parents)
                for i, path in enumerate(files)]

    sigs = _sign_table(files)
    pairs = _vision_pairs(declared, sigs, corr)
    uf = _UF(len(files))
    first: dict[str, int] = {}
    for i, key in enumerate(declared):       # 同一声明家族先连成一片
        if key in first:
            uf.union(first[key], i)
        else:
            first[key] = i
    for i, j, _score in pairs:               # 再让跨源的近邻把两片接起来
        uf.union(i, j)
    valid = sum(1 for s in sigs if s is not None)
    print(f"  签名有效 {valid}/{len(files)}  声明家族 {len(set(declared))} 个  "
          f"跨声明近邻 {len(pairs)} 对（corr>={corr}）")

    groups: dict[int, list[int]] = {}
    for i in range(len(files)):
        groups.setdefault(uf.find(i), []).append(i)

    plans: list[dict] = []
    for members in groups.values():
        kept, dropped = _dedup(files, sigs, members, phash_distance, ssim)
        where = {i: labels[i] for i in members}
        plans.append({
            "declared": sorted({declared[i] for i in members}),
            "members": sorted(members),
            "kept": sorted(kept),
            "dropped": {i: {"of": where[k], **evidence}
                        for i, (k, evidence) in dropped.items()},
            "merged_by": [(where[i], where[j], score) for i, j, score in pairs
                          if i in where],
        })
    return plans


# --------------------------------------------------------------------------
# 复用：families.json 与当前图集合/参数完全对得上就不再算一遍
# --------------------------------------------------------------------------
def _load_store(ds) -> dict | None:
    path = ds.pipe_dir / REPORT_JSON
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  ! {REPORT_JSON} 读不出来，按首次跑重算：{exc}")
        return None


def _ws_label(work_set) -> str:
    if not work_set:
        return ""
    items = work_set if isinstance(work_set, (list, tuple, set)) else str(work_set).split(",")
    return ",".join(str(x).strip().strip("/\\") for x in items if str(x).strip())


def _same_params(have: dict, want: dict) -> bool:
    for key in ("corr", "ssim"):
        try:
            if abs(float(have[key]) - float(want[key])) > 1e-9:
                return False
        except (KeyError, TypeError, ValueError):
            return False
    for key in ("phash_distance", "signature", "work_set"):
        if have.get(key) != want.get(key):
            return False
    return True


def _from_store(ds, store: dict, files: list[Path], params: dict) -> list[dict] | None:
    """复用上次的家族划分；对不上就返回 None（= 老实重算）。

    只在「图集合完全一致 + 参数一致」时复用。被搬进 _excluded 的重复图在复用时
    已经不在工作集里，直接跳过（所以第二次跑 duplicates 报 0 是实话）。
    """
    if not isinstance(store, dict) or not _same_params(store.get("params") or {}, params):
        return None
    want = store.get("image_set")
    if not isinstance(want, list):
        return None
    rels = [_rel(ds, p) for p in files]
    if sorted(want) != sorted(rels):
        return None
    by_path = {rel: i for i, rel in enumerate(rels)}

    plans: list[dict] = []
    for fam in store.get("families") or []:
        kept: list[int] = []
        dropped: dict[int, dict] = {}
        for m in fam.get("members") or []:
            i = by_path.get(m.get("path"))
            if m.get("role") == "dup":
                if i is None:
                    continue                      # 已经在 _excluded 里了
                dropped[i] = {"of": m.get("of") or "", "ssim": float(m.get("ssim") or 0.0),
                              "phash": int(m.get("phash") or 0),
                              "blockmax": float(m.get("blockmax") or 0.0)}
            elif i is None:
                return None                       # 记录与磁盘对不上，重算
            else:
                kept.append(i)
        if not kept:
            return None
        plans.append({
            "declared": list(fam.get("declared") or []),
            "members": sorted(kept + list(dropped)),
            "kept": sorted(kept),
            "dropped": dropped,
            "merged_by": [(e.get("a"), e.get("b"), float(e.get("corr") or 0.0))
                          for e in fam.get("merged_by") or []],
        })
    return plans or None


# --------------------------------------------------------------------------
# 落盘：建桶 / repeats / 两份报告
# --------------------------------------------------------------------------
def _family_key(declared: list[str], used: set) -> str:
    """家族在报告里的键：声明键里最小的那个；多声明合并时标 +N，重名再补 #n。"""
    base = declared[0] if declared else "single"
    if len(declared) > 1:
        base = f"{base}+{len(declared) - 1}"
    key = base
    n = 2
    while key in used:
        key = f"{base}#{n}"
        n += 1
    used.add(key)
    return key


def _finish(ds, files: list[Path], labels: list[str], plans: list[dict],
            params: dict, apply: bool) -> dict:
    """分桶 + repeats + 写报告（+ apply 时真的搬文件）。"""
    total_before = len(files)
    total_kept = sum(len(p["kept"]) for p in plans)
    dups = sum(len(p["dropped"]) for p in plans)
    base = _base_repeats(total_kept) if total_kept else 0

    used: set = set()
    orders = sorted(plans, key=lambda p: (-len(p["kept"]), (p["declared"] or [""])[0]))
    for plan in orders:
        k = len(plan["kept"])
        plan["bucket"] = f"diff{k}"
        # round(base/sqrt(k))：k 越大单族能贡献的重复越少；max(1,...) 兜住
        # base=1 且 k>=5 时 round 给 0 的情况（训练里 repeats=0 等于把这组丢了）
        plan["repeats"] = max(1, int(round(base / math.sqrt(k)))) if k else 0
        plan["key"] = _family_key(plan["declared"], used)

    moved = moved_dup = 0
    if apply:
        dup_root = ds.excluded_dir / DUP_DIR
        evidence = []
        for plan in orders:
            for i in sorted(plan["dropped"]):
                src = files[i]
                if not src.exists():
                    continue                       # 上次已经搬过
                info = plan["dropped"][i]
                dest = _move_into(src, dup_root)
                evidence.append({
                    "original": src.name, "new_name": dest.name, "post_id": "",
                    "source": "", "reason": DUP_DIR, "rule": "same-image",
                    "detail": f"SSIM {info['ssim']:.4f} pHash {info['phash']} "
                              f"块差 {info.get('blockmax', 0.0):.1f} -> 保留 {info['of']}",
                    "decided_by": "rule",
                })
                files[i] = dest
                moved_dup += 1
        for plan in orders:
            dest_dir = ds.root / plan["bucket"]
            for i in plan["kept"]:
                src = files[i]
                if not src.exists():
                    continue
                if src.parent.resolve() == dest_dir.resolve():
                    continue
                files[i] = _move_into(src, dest_dir)
                moved += 1
        record_excluded(ds, evidence)
        gone = _prune_empty_diff(ds)
        if gone:
            print(f"  清掉 {gone} 个空掉的 diff<k> 目录")

    rows: list[dict] = []
    fam_json: list[dict] = []
    by_bucket: dict[str, list[dict]] = {}
    for plan in orders:
        keep_set = set(plan["kept"])
        members: list[dict] = []
        summary: list[str] = []
        for i in plan["members"]:
            entry = {"path": _rel(ds, files[i]), "label": labels[i],
                     "role": "keep" if i in keep_set else "dup"}
            if i in plan["dropped"]:
                info = plan["dropped"][i]
                entry.update({"of": info["of"], "ssim": round(float(info["ssim"]), 4),
                              "phash": int(info["phash"]),
                              "blockmax": round(float(info.get("blockmax", 0.0)), 1)})
                summary.append(f"{labels[i]}(dup->{info['of']})")
            else:
                summary.append(labels[i])
            members.append(entry)
        merged = sorted(plan["merged_by"], key=lambda t: -t[2])
        fam_json.append({
            "key": plan["key"], "declared": plan["declared"],
            "bucket": plan["bucket"], "repeats": plan["repeats"],
            "size_before": len(plan["members"]), "size_after": len(plan["kept"]),
            "merged_pairs": len(merged),
            "merged_by": [{"a": a, "b": b, "corr": round(c, 4)}
                          for a, b, c in merged[:20]],
            "members": members,
        })
        rows.append({"family": plan["key"],
                     "declared_sources": ";".join(plan["declared"]),
                     "size_before": len(plan["members"]),
                     "size_after": len(plan["kept"]),
                     "bucket": plan["bucket"],
                     "repeats": plan["repeats"],
                     "members": ";".join(summary)})
        by_bucket.setdefault(plan["bucket"], []).append(plan)

    def bucket_no(name: str) -> int:
        return int(name[4:]) if name[4:].isdigit() else 0

    image_set = sorted(_rel(ds, files[i]) for plan in orders for i in plan["kept"])
    store = {
        "dataset": ds.name,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "params": params,
        "images_before": total_before,
        "images_after": total_kept,
        "base_repeats": base,
        "totals": {"families": len(orders), "duplicates": dups},
        "buckets": {name: {"families": len(items), "repeats": items[0]["repeats"]}
                    for name, items in sorted(by_bucket.items(),
                                               key=lambda kv: bucket_no(kv[0]))},
        "image_set": image_set,
        "families": fam_json,
    }
    ds.ensure_dirs()
    write_csv(ds.pipe_dir / REPORT_CSV, rows, CSV_FIELDS)
    (ds.pipe_dir / REPORT_JSON).write_text(
        json.dumps(store, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print(f"[families] {len(orders)} 族  {total_before} 张 -> {total_kept} 张"
          f"（同图去重 {dups} 张）  base repeats {base}")
    for name, items in sorted(by_bucket.items(), key=lambda kv: bucket_no(kv[0])):
        print(f"  {name}  {len(items)} 族 x{len(items[0]['kept'])} 张  "
              f"repeats={items[0]['repeats']}")
    print(f"  报告 {ds.pipe_dir / REPORT_CSV}")
    print(f"       {ds.pipe_dir / REPORT_JSON}")
    if apply:
        print(f"  已搬：{moved} 张进 diff<k>，{moved_dup} 张进 _excluded/{DUP_DIR}/")
    else:
        print("  （dry-run：只写了报告，没动任何文件；加 apply 才真正分桶）")

    return {"families": len(orders), "images_before": total_before,
            "images_after": total_kept, "duplicates": dups,
            "buckets": {name: len(items) for name, items in by_bucket.items()},
            "repeats": {name: items[0]["repeats"] for name, items in by_bucket.items()},
            "applied": bool(apply)}


# --------------------------------------------------------------------------
# 命令入口
# --------------------------------------------------------------------------
def cmd_families(ds, apply: bool = False, work_set=None, phash_distance: int = 6,
                 ssim: float = 0.995, corr: float = 0.85,
                 rebuild: bool = False) -> dict:
    """把差分家族显式化，并按族的实际张数重分图桶（见模块 docstring 的四步）。

    返回 {"families", "images_before", "images_after", "duplicates",
          "buckets": {桶名: 家族数}, "repeats": {桶名: repeats}, "applied"}；
    buckets/repeats 按桶聚合，一个桶里所有族张数相同、repeats 也相同。
    """
    ds.ensure_dirs()
    files = ds.work_images(work_set)
    if not files and not work_set:
        files = ds.raw_images()
        if files:
            print("[families] 还没有成品图桶，先用 00_raw/ 当工作集")
    files = list(files)
    if not files:
        print(f"[families] 没有可处理的图片（{ds.root}）")
        return {"families": 0, "images_before": 0, "images_after": 0, "duplicates": 0,
                "buckets": {}, "repeats": {}, "applied": False}

    print(f"[families] {len(files)} 张图  签名 {N}x{N}（去均值 + L2）  "
          f"跨源合并 corr>={corr}  同图 SSIM>={ssim} + pHash<={phash_distance} "
          f"+ 块差<={DUP_BLOCK:g}")
    params = {"corr": float(corr), "ssim": float(ssim),
              "phash_distance": int(phash_distance), "signature": N,
              "work_set": _ws_label(work_set)}
    labels = _unique_labels(files)

    plans = None if rebuild else _from_store(ds, _load_store(ds) or {}, files, params)
    if plans:
        print(f"[families] 图集合与 {REPORT_JSON} 一致，直接复用上次的家族划分"
              f"（rebuild=True 可强制重算）")
    else:
        if rebuild:
            print("[families] rebuild：忽略旧记录，重新算")
        plans = _walk(ds, files, labels, phash_distance, ssim, corr)
    return _finish(ds, files, labels, plans, params, apply)
