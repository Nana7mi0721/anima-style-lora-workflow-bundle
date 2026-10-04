"""The wash (洗标) rule engine -- deterministic, zero-token caption normalisation.

Implements the rules of `Anima_style_LoRA_打标指南.md`:

  §3   hard constraints   single line, lowercase, spaces (no underscores except
                          score_*), ", " separator, no leading comma, 20-45 tags,
                          trigger word first and unique
  §7.2 alias / deprecated table
  §7.3 colour downgrade + multicolored hair
  §7.4 implication folding (drop a parent tag when a child is present)
  §7.5 zero tolerance for negative forms (`no panties` / `no bra` are real tags)
  §7.6 descriptor whitelist (lighting / composition words that are not booru tags)
  §7.7 hallucinated tags (post_count == 0) are dropped
  §4   seven-slot ordering

Everything here runs offline against the local danbooru dictionaries; the only
optional network step is `verify --online`.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from . import curate, dicts, manifest as manifest_mod, net

# --------------------------------------------------------------------------
# default rules -- mirrors the guide's §7.2 / §7.6 tables
# --------------------------------------------------------------------------
NEGATION_RX = r"^(no|without|not|never)\b"

DEFAULT_RULES: dict = {
    "trigger": "",
    # 指南 §2.3「该删的」+ §2.4「该保留的」：判据不是出现频率，而是「有没有固定视觉对应物」。
    # ⇒ 水印/签名/logo **不在这里禁**（有像素对应），改由 watermark_tags + 修补状态决定（见 cmd_wash）。
    "drop_exact": [
        # quality / meta 常量（无视觉对应物）
        "best quality", "high quality", "masterpiece", "amazing quality", "very aesthetic",
        "absurdres", "highres", "hi res", "lowres", "commentary", "commentary request",
        "translated", "translation request", "bad id", "bad source", "third party edit",
        "uncensored", "photoshop", "digital version", "digital media", "artist revision",
        "detexted", "wallpaper", "url",
        # 文字族：danbooru 已废弃整族，改由 speech bubble / sound effects / logo 承载（§2.3）
        "text", "japanese text", "english text", "chinese text", "korean text",
        # booru bookkeeping（描述的是帖子本身，不是画面）
        "tagme", "tag me", "artist request", "character request", "copyright request",
        "bad pixiv id", "bad twitter id", "bad link",
        "resized", "downscaled", "sample", "image sample", "jpeg artifacts",
        "check commentary", "english commentary",
        # 评级词：**按用户拍板保留**（`safe`/`sensitive`/`questionable`/`explicit`/`nsfw`/`general`，
        # 含 `rating safe` 这类空格形）。指南 §4.3 的官方段 [quality/meta/year/safety] 是「一般不写」，
        # 但用户既有数据集整族都带评级词，保持形态一致优先。冒号形 `rating:safe` 与 `score_*`
        # 仍在 drop_regex 里当结构性噪声丢掉（不是标签形态）。
        # 自证词 / 模型自造词（§2.3 meta）
        "detailed", "beautiful", "anime style", "anime", "illustration", "art", "artwork",
        "digital art", "picture", "image", "visible",
        # 已废弃且无别名（§7.2「删除」）
        "areola", "areolae", "bangs",
        #
        # ⚠️ 下面这些**故意不在**名单里，改动前先读指南：
        #   `looking at viewer` / `open mouth` / `closed mouth` / `solo focus`
        #     —— 指南 §11.1 的成品示例里就有它们（表情/视线是 §4.2 归槽位 2 的「变化因素」）
        #   `topless` → `breasts out`、`slender` → `skinny`、`smiling` → `smile`、
        #   `group shot` → `group picture`、`on all fours` → `all fours`、
        #   `hair clip` → `hairclip`、`hairy arms` → `arm hair`、`looking away` → `looking to the side`、
        #   `front/back/side view` → `straight-on`/`from behind`/`from side`、
        #   `left side ponytail` → `side ponytail`、`eyes closed` → `closed eyes`、
        #   `mouth open` → `open mouth`、`from front` → `straight-on`
        #     —— §7.2/§7.6/§7.7 要求**换成现行形**，不是丢弃；它们都在 aliases 里。
        #     曾经它们同时躺在 drop_exact 里，而 drop_exact 检查在别名之前 ⇒ 别名成了死代码，
        #     真实的"变化因素"被静默丢掉（63 张真实 caption 审计：`looking at viewer` 命中 58 次）。
    ],
    "drop_regex": [
        NEGATION_RX,   # §7.5
        r"^(rating:\s*|score_\d|score_)",
        r"^\d{4}$",                       # bare year tags
        r"^(19|20)\d{2}$",
        r"\bnot\s+\w+",                   # "tongue not out"
        r"visible$",                      # "X visible"
        r"^slightly\s",                   # "slightly X"
        r"^twin tails$",
        r"^(very|really|extremely)\s+",
        r"^[a-z]+_?style$",               # "anime_style"
        r"^\([a-z]+\)$",                  # 模板占位符残留 "(series)" / "(artist)"
        r"^[a-z]$",                       # 单字母碎片；词典认识的 `v`/`w` 不受影响
    ],
    "aliases": {
        "pantsu": "panties", "seifuku": "serafuku", "megane": "glasses", "naked": "nude",
        "topless": "breasts out", "ass grab": "grabbing another's ass",
        "ass_grab": "grabbing another's ass", "breast grab": "grabbing another's breast",
        "breast_grab": "grabbing another's breast", "breast hold": "grabbing own breast",
        "breast_hold": "grabbing own breast", "tan lines": "tanlines", "tan_lines": "tanlines",
        "bunny ears": "rabbit ears", "bunny_ears": "rabbit ears", "swimsuits": "swimsuit",
        "bikini top": "bikini top only", "bikini_top": "bikini top only",
        "semen": "cum",
        "looking away": "looking to the side", "cowgirl": "cowgirl position",
        "smiling": "smile", "slender": "skinny", "group shot": "group picture",
        "on all fours": "all fours", "hair clip": "hairclip", "hairy arms": "arm hair",
        "reverse cowgirl": "reverse cowgirl position", "tights": "pantyhose", "sofa": "couch",
        "indoor background": "indoors", "outdoor background": "outdoors",
        "low angle": "from below", "high angle": "from above", "thigh-highs": "thighhighs",
        "thigh highs": "thighhighs", "hand on face": "hand on own face",
        "hand on cheek": "hand on own cheek", "nopan": "no panties",
        "shimapan": "striped panties", "twin tails": "twintails", "twintail": "twintails",
        "barefeet": "barefoot",
        # §7.2 表里其余条目 + §6.3 的"口语形"→现行形（逐条核过词典：目标标签都真实存在）
        "eyes closed": "closed eyes", "mouth open": "open mouth",
        "front view": "straight-on", "back view": "from behind", "side view": "from side",
        "from front": "straight-on", "left side ponytail": "side ponytail",
        "bikini pull": ["clothes pull", "one breast out"],   # §7.2：废弃后拆成两个现行标签
        # §7.7「不存在的标签」：词典没有这些写法，但同义现行标签存在（逐个核过词典）
        "bunny girl": "playboy bunny", "gluteal fold": "gluteal sulcus", "aftersex": "after sex",
        # 站点拼写差异（yande.re 的标签拼法比 danbooru 松，实测遇到）
        "chinadress": "china dress", "garter": "garter belt", "nekomimi": "cat ears",
        "thighhigh": "thighhighs",
    },
    "alias_extra": [],          # [{from:..., to:...}] per-dataset additions
    "drop_parent_when_child": True,
    "keep_parent_tags": ["solo", "1girl", "1boy", "breasts", "hair", "clothes"],
    "color_downgrade": {
        "light blue": "blue", "pale blue": "blue", "sky blue": "blue", "dark blue": "blue",
        "navy blue": "blue", "silver": "white", "platinum": "white", "snow white": "white",
        "golden": "yellow", "gold": "yellow", "amber": "yellow", "blonde": "blonde",
        "ash": "grey", "gray": "grey", "charcoal": "black", "jet black": "black",
        "crimson": "red", "scarlet": "red", "maroon": "red", "rose": "pink",
        "magenta": "pink", "violet": "purple", "lavender": "purple", "lilac": "purple",
        "emerald": "green", "mint": "green", "olive": "green", "teal": "blue",
        "turquoise": "blue", "cyan": "blue", "aqua": "blue", "cream": "white",
        "beige": "brown", "tan": "brown", "chocolate": "brown", "chestnut": "brown",
        "auburn": "brown", "ginger": "orange", "rainbow": "multicolored",
    },
    "hair_colors": ["black", "blonde", "blue", "brown", "green", "grey", "orange", "pink",
                    "purple", "red", "white", "multicolored", "two-tone", "gradient"],
    "eye_colors": ["black", "blue", "brown", "green", "grey", "orange", "pink", "purple",
                   "red", "white", "yellow", "multicolored", "heterochromia", "two-tone"],
    # 评级词：**用户拍板保留**（指南 §4.3 的官方段 [quality/meta/year/safety]「一般不写」，
    # 但用户既有数据集整族带评级词，形态一致优先）。它们不是 danbooru 的 tag 而是 rating 值，
    # 所以词典查不到 —— 单独一张表，既免于被记成 unknown，也让 classify() 把它们排到
    # **触发词之后、主体之前**（就是官方段的位置），而不是掉进槽位 0。
    "rating_tags": ["safe", "sensitive", "questionable", "explicit", "nsfw", "general",
                    "rating safe", "rating questionable", "rating explicit"],
    "descriptor_whitelist": [
        "warm tone", "cool tone", "soft lighting", "soft light", "bright lighting",
        "natural lighting", "warm lighting", "dim lighting", "cinematic lighting",
        "soft shadows", "side lighting", "strong light", "strong shadows",
        "hard shadow pattern", "low contrast", "cool-warm mixed", "bright indoor light",
        "sunlight from left", "sunlight through blinds", "soft body highlight",
        "diagonal composition", "multi-figure composition", "eye level",
        "foreground focus on hips",
        "blue watermark", "red watermark",
    ],
    "framing_tags": [
        "upper body", "lower body", "full body", "cowboy shot", "close-up", "portrait",
        "from above", "from below", "from behind", "from side", "straight-on",
        "wide shot", "dutch angle", "profile", "head out of frame", "feet out of frame",
        "foreground", "depth of field", "blurry background", "backlighting", "silhouette",
    ],
    # 水印区（槽位 7，指南 §8.1/§11.1）：**有固定像素对应**的一族。是否写由图像状态决定——
    # 去字工具把水印修补掉了就不再写（决策树第一支「能在图像层去掉就去掉，比标出来更好」）。
    "watermark_tags": [
        "watermark", "sample watermark", "blue watermark", "red watermark", "color watermark",
        "signature", "logo", "artist logo", "artist name", "web address",
        "twitter username", "pixiv username", "patreon username", "skeb username",
        "fanbox username", "speech bubble", "sound effects", "text focus",
    ],
    "people_tags": ["1girl", "2girls", "3girls", "4girls", "5girls", "6+girls", "1boy", "2boys",
                    "3boys", "4boys", "5boys", "6+boys", "solo", "multiple girls", "multiple boys",
                    "male focus", "female focus", "everyone", "hetero", "yuri", "yaoi", "group",
                    "no humans", "1other", "2others", "3others", "multiple others", "other focus"],
    # 指南 §8.3：关系标签同归槽位 1（人数 + 关系 + 角色名）。这些词按 danbooru 的 l1 会掉进
    # 槽位 3/6（`solo focus` 曾掉进 6、`group sex` 掉进 3、`bisexual` 无 l1 掉进 0），
    # 所以单独成表、在槽位判定最前面命中。
    "relation_tags": ["solo focus", "faceless male", "bisexual", "group sex", "threesome",
                      "ffm threesome", "mmf threesome", "orgy", "multiple views"],
    "sex_tags": [
        "sex", "vaginal", "anal", "oral", "fellatio", "cunnilingus", "paizuri", "handjob",
        "masturbation", "penetration", "cum", "cum in pussy", "cum on body", "nipples",
        "pussy", "penis", "anus", "ass", "testicles", "pubic hair", "nude", "bottomless",
        "topless", "breasts out", "pussy juice", "saliva", "ahegao", "orgasm",
        "missionary", "doggystyle", "cowgirl position", "reverse cowgirl position",
        "spread legs", "on back", "all fours", "standing sex", "suspended congress",
        "mating press", "prone bone", "against wall", "sex from behind", "fellatio",
        # §8.4 的性描写词汇里 l1=身体/表情、会被误判成槽位 2 的（cumdrip 实测掉进 2）
        "cumdrip", "cum on breasts", "cum on ass", "cum on clothes", "cum on face",
        "squirting", "clitoris", "clitoral stimulation", "large insertion",
        "imminent penetration", "after sex", "after fellatio", "after vaginal",
    ],
    # Markers that an actual sex act is happening.  Nudity alone (`nipples`,
    # `nude`, `bottomless`) is NOT one: a clothed shirt-lift pin-up does not need
    # a vision pass for "position", and flagging it would burn a subagent run.
    "act_tags": [
        "sex", "vaginal", "anal", "penetration", "fellatio", "cunnilingus", "paizuri",
        "handjob", "masturbation", "sex from behind", "standing sex", "mating press",
        "prone bone", "suspended congress", "missionary", "doggystyle",
        "cowgirl position", "reverse cowgirl position", "cum in pussy", "pussy juice",
        "ahegao", "penis", "testicles",
    ],
    "position_known_tags": [
        "missionary", "doggystyle", "cowgirl position", "reverse cowgirl position",
        "mating press", "prone bone", "suspended congress", "standing sex",
        "sex from behind", "spread legs", "on back", "all fours",
    ],
    "slot_overrides": {},          # tag -> slot, highest priority
    # 词典 category 整类丢弃（指南 §2.3）：1=artist（由触发词承载）、3=copyright（IP 系列名
    # 「永不添加」）、5=meta（无视觉对应物的常量）。实测 watermark / signature / logo /
    # speech bubble / sound effects / censored / mosaic censoring 在 danbooru 口径下都是
    # general(0)，不会被整类丢弃误伤；category_keep 是逃生门，命中者不参与整类丢弃。
    "category_drop": [1, 3, 5],
    "category_keep": [
        "watermark", "sample watermark", "blue watermark", "red watermark", "signature",
        "logo", "artist logo", "artist name", "web address", "twitter username",
        "pixiv username", "patreon username", "skeb username", "fanbox username",
        "speech bubble", "sound effects", "censored", "mosaic censoring", "bar censor",
        "traditional media", "sketch", "monochrome", "lineart",
        # `original`：danbooru 把它归在 copyright 类，但它不是 IP 系列名，而是"原创非二创"这一
        # 事实陈述（§2.3 禁的是 `touhou`/`blue_archive` 这类系列名）。用户自己的 63 张 arata
        # caption 里 52 张都写了它 ⇒ 丢掉等于替用户改口径。要跟随 §2.3 的严格读法，
        # 把它从这里删掉即可（或在 wash_rules.json 里设 category_keep 整体覆盖）。
        "original",
    ],
    # 角色名频率门槛。指南 §8.2 建议 4（低频角色学不会，只会把画风打散成噪声；阈值演变
    # 10 → 5 → 4），但**本预设按用户拍板取消**：0 = 不限制，出现几次都保留。
    # 想跟随指南就把它改成 4（wash 会打印「[wash] 角色阈值 §8.2：…」确认已生效）。
    "character_min_images": 0,
    "unknown_policy": "report",    # report | drop | keep
    "min_tags": 20,
    "max_tags": 45,
    # 指南 §3.1：实测成品是纯标签串，混排自然语言「可选，非默认」。要保留来源 caption 里
    # 的散文尾巴（空间布局/光照方向/材质氛围）就在这里改 true。
    "keep_natural_language": False,
}


# danbooru tag category -> 丢弃理由（指南 §2.3）
_CAT_WHY = {1: "artist-tag(由触发词承载)", 3: "copyright-ip(永不添加)", 5: "meta-constant"}
# 这几条是「策略开关」而不是「标签清单」：覆盖时整体替换，这样设成 [] 能真的关掉
_REPLACE_KEYS = {"category_drop"}


def _merge_rules(rules: dict, over: dict) -> None:
    """Overlay one rule layer. Tag lists are **additive** (a dataset can only add bans),
    except the policy keys in _REPLACE_KEYS where "set it to [] to turn the rule off"
    has to work."""
    for k, v in over.items():
        if k in _REPLACE_KEYS:
            # 防御性深拷贝：v 是 Config 里那个**活的对象**，直接接管后再 append 会把这个
            # 数据集的内存配置改掉（下一个数据集/下一次 load_rules 就会看到被污染的值）
            rules[k] = json.loads(json.dumps(v))
        elif isinstance(v, dict) and isinstance(rules.get(k), dict):
            rules[k].update(v)
        elif isinstance(v, list) and isinstance(rules.get(k), list):
            rules[k] = rules[k] + [x for x in v if x not in rules[k]]
        else:
            rules[k] = v


def load_rules(ds=None, extra: dict | None = None) -> dict:
    """DEFAULT_RULES → 配置文件的 wash 段（bundle 默认 + runtime 覆盖）→ 数据集 wash_rules.json
    → CLI 覆盖。数据集的 wash 段曾经是**死配置**（定义了但没人读），现在真的接上了。
    最后追加训练目标（manifest 的 kind）带来的差异——指南 §5 的规则是按类型分的。"""
    rules = json.loads(json.dumps(DEFAULT_RULES))
    if ds is not None:
        _merge_rules(rules, ds.cfg.get("wash") or {})
        path = ds.pipe_dir / "wash_rules.json"
        if Path(path).exists():
            _merge_rules(rules, json.loads(Path(path).read_text(encoding="utf-8")))
        try:
            rules["kind"] = manifest_mod.ensure(ds).cfg("kind", "style") or "style"
        except Exception:                       # manifest 读不动就按 style 走，别让洗标挂掉
            rules["kind"] = "style"
    if extra:
        rules.update(extra)
    _apply_kind_rules(rules)
    return rules


# 类型差异（指南 §5 / §8.2）：默认（style/scene/object/clothing）保留数据集内高频角色名，
# 但**角色 LoRA 相反——身份由触发词承载，角色名 tag 整类不写**（§5.2「角色名 tag 与触发词
# 二选一」、§8.2「角色 LoRA 相反：不写角色名」）。
_KIND_DROP_CATS = {"character": [4]}
_KIND_NOTE = {
    "character": "角色 LoRA：角色名 tag 由触发词承载，整类剔除（§5.2 / §8.2）；固有特征"
                 "（发色/瞳色）剪不剪是逐数据集决策，判据是出现比例 ≥0.9",
    "style": "画师风格 LoRA：角色 tag 默认保留（本预设不看频次，§8.2 的 ≥4 阈值已按用户拍板取消），"
             "去留交用户裁决；不要描述风格本身（§5.3）",
    "clothing": "服装 LoRA：角色/服装/姿势/环境四层必须解耦（§5.4）",
    "scene": "场景 LoRA：构图多样（远/中/近景、横/竖）是第一要求（§5.5）",
    "object": "物体/概念 LoRA：写朝向与状态（open/closed/folded/worn/in use），§5.5",
}


def _apply_kind_rules(rules: dict) -> None:
    kind = rules.get("kind") or "style"
    if kind in _KIND_DROP_CATS:
        rules["category_drop"] = list(rules.get("category_drop") or [])
        for cat in _KIND_DROP_CATS[kind]:
            if cat not in rules["category_drop"]:
                rules["category_drop"].append(cat)
    if kind == "character":
        # 身份进触发词后，角色名不再逐张写（频率阈值本来就无关了）
        _CAT_WHY[4] = "character-name(由触发词承载)"
        rules["character_min_images"] = 0
    else:
        _CAT_WHY.pop(4, None)                   # 别把 character 的措辞留给下一个数据集
    rules["kind_note"] = _KIND_NOTE.get(kind, "")


# --------------------------------------------------------------------------
# token level transforms
# --------------------------------------------------------------------------
_WS = re.compile(r"\s+")


def normalize_tag(tag: str) -> str:
    t = tag.strip().strip('"').strip("'").strip(".,;:")
    t = t.replace("_", " ")
    t = _WS.sub(" ", t)
    t = t.lower()
    t = re.sub(r"^\d+\s*(girl|boy)s?$", lambda m: m.group(0).replace(" ", ""), t)
    return t


def lookup_key(tag: str) -> str:
    """空格形 → danbooru 词典键（下划线形），并剥掉转义括号的反斜杠。

    用户的真实 caption 用 danbooru 的转义写法（`hikari \\(blue archive\\)`），
    而词典键是 `hikari_(blue_archive)`；不剥反斜杠就会被误判成"词典里没有"。
    输出侧保留用户的写法（§3 约束 11「一个数据集内形态统一」），只在这里归一查询用。
    """
    return tag.replace("\\", "").replace(" ", "_")


# 这几条正则描述的是「结构性噪声」，不是启发式猜测：即便词典认识这个字符串也要拦。
# 其余 drop_regex 只对**词典不认识的**字符串生效 —— 否则 `very long hair`（真标签，105 万帖）
# 会被 `^(very|really|extremely)\s+` 杀掉、`doggystyle` 会被 `^[a-z]+_?style$` 杀掉。
_BAN_ALWAYS = frozenset({
    NEGATION_RX,
    r"^(rating:\s*|score_\d|score_)",
    r"^\d{4}$",
    r"^(19|20)\d{2}$",
})


def is_negative(tag: str, rules: dict, td=None) -> bool:
    """Negation filter (guide 7.5) + the guide's prose/noise regexes.

    `no shoes`(125797 posts) / `no humans`(235767) / `no mouth` / `no headwear`
    are real, heavily used danbooru tags, so a leading negation is only treated
    as a hallucinated negative (`no cum`, `no penetration`, `tongue not out`)
    when the dictionary does not know the string.

    同理：`very long hair` / `very short hair` / `doggystyle` 都真实存在，
    所以启发式正则（`^(very|...)\\s+`、`^[a-z]+_?style$`、`visible$`、`^slightly\\s`）
    只在词典不认识该字符串时才生效。
    """
    known = td is not None and lookup_key(tag) in td
    for rx in rules["drop_regex"]:
        if not re.search(rx, tag):
            continue
        if known and rx not in _BAN_ALWAYS:
            continue
        if known and rx == NEGATION_RX:
            continue
        return True
    return False


def downgrade_color(tag: str, rules: dict) -> str:
    """silver hair -> white hair / light blue eyes -> blue eyes."""
    for suffix, allowed in (("hair", rules["hair_colors"]), ("eyes", rules["eye_colors"])):
        if tag.endswith(" " + suffix) or tag == suffix:
            head = tag[: -len(suffix)].strip()
            if not head:
                return tag
            if head in allowed:
                return tag
            parts = head.split()
            for i in range(len(parts)):
                cand = " ".join(parts[i:])
                if cand in rules["color_downgrade"]:
                    return f"{rules['color_downgrade'][cand]} {suffix}"
            if len(parts) > 1 and parts[-1] in allowed:
                return f"{parts[-1]} {suffix}"
    return tag


def apply_implication(tags: list[str], td, rules: dict) -> tuple[list[str], list[str]]:
    """§7.4 -- drop a parent tag when a more specific child is present."""
    if not rules.get("drop_parent_when_child"):
        return tags, []
    present = set(tags)
    dropped: list[str] = []
    keep = set(rules.get("keep_parent_tags") or [])
    out = []
    for t in tags:
        parent_children = td.children.get(lookup_key(t)) or set()
        # a tag is a parent if some present tag has it as its dict parent
        is_parent = any(td.parent(lookup_key(x)) == lookup_key(t) for x in present)
        if is_parent and t not in keep and parent_children:
            dropped.append(t)
            continue
        out.append(t)
    return out, dropped


def fold_pairs(tags: list[str], rules: dict) -> list[str]:
    """A-priori folding pairs that are not in the dictionary parent graph."""
    pairs = [
        ({"cat ears", "dog ears", "fox ears", "rabbit ears", "animal ears"}, "animal ears"),
        ({"school swimsuit", "swimsuit", "bikini"}, "swimsuit"),
        ({"lace-trimmed bra", "sports bra", "bra"}, "bra"),
        ({"thighhighs", "pantyhose", "black thighhighs", "white thighhighs"}, "thighhighs"),
    ]
    out = []
    for t in tags:
        drop = False
        for group, parent in pairs:
            if t == parent and (group - {parent}) & set(tags):
                drop = True
                break
        if not drop:
            out.append(t)
    return out


def add_implied(tags: list[str], rules: dict) -> list[str]:
    """§7.3 -- more than one hair colour implies multicolored hair."""
    hair = {t.split(" hair")[0] for t in tags if t.endswith(" hair") and
            t.split(" hair")[0] in rules["hair_colors"]}
    if len(hair) >= 2 and "multicolored hair" not in tags:
        tags.append("multicolored hair")
    return tags


# --------------------------------------------------------------------------
# slot classification (§4.1)
# --------------------------------------------------------------------------
SLOT_NAMES = {1: "subject", 2: "appearance", 3: "pose", 4: "scene", 5: "lighting",
              6: "framing", 7: "watermark", 0: "other"}

_L1_SLOT = {
    "服装": 2, "头发": 2, "眼睛": 2, "身体": 2, "饰品": 2, "表情": 2, "cosplay": 2,
    "动作": 3, "背景": 4, "物品": 4, "食物": 4, "动物": 4, "构图": 6,
    "画风": 5, "画师": 7, "人数": 1, "作品": 0,
}


# 第 2 槽位的"表情 / 视线"关键词（§4.2）。必须排在词典 l1 之前，见 classify 里的注释。
_FACE_SLOT2 = (
    "looking", "gaze", "eye contact", "staring", "glaring", "wink", "rolling eyes",
    "expression", "smile", "smirk", "grin", "frown", "pout", "blush", "embarrassed",
    "surprised", "shocked", "angry", "annoyed", "sad", "crying", "tears", "teary",
    "open mouth", "closed mouth", "parted lips", "clenched teeth", "tongue out",
    "drooling", "sweat", "nose blush", "eyebrows", "eyelashes", "makeup", "lipstick",
)

# 第 5 槽位的"光影色调"词尾（§4.1）。用词尾而非子串匹配，避免 `light blue hair` 被误判。
_LIGHT_SLOT5 = (
    "light", "lights", "lighting", "backlighting", "backlight", "shadow", "shadows",
    "tone", "contrast", "highlight", "highlights", "glow", "rays", "day", "night",
    "dark", "darkness", "sunset", "sunrise", "dusk", "dawn", "twilight", "moonlight",
)


def classify(tag: str, td, rules: dict) -> int:
    if tag in (rules.get("slot_overrides") or {}):
        return int(rules["slot_overrides"][tag])
    if tag in (rules.get("rating_tags") or ()):
        return 9                               # 官方段 [quality/meta/year/safety] 的位置（触发词之后）
    if tag in rules["people_tags"]:
        return 1
    if tag in (rules.get("relation_tags") or ()):
        return 1                               # §8.3 关系标签同归槽位 1（须在 framing/sex 之前）
    if tag in rules["framing_tags"]:
        return 6
    if tag in rules["watermark_tags"]:
        return 7
    if tag in rules["descriptor_whitelist"]:
        return 5 if any(k in tag for k in ("light", "tone", "shadow", "contrast", "highlight",
                                           "sunlight", "warm", "cool")) else 6
    if tag in rules["sex_tags"]:
        return 3
    # §4.2 的第 2 槽位含**表情与视线**（"旧规则曾把视线放槽位 3…已更正为归槽位 2"）。
    # 这一条必须排在词典 l1 之前：danbooru 把 `looking at viewer` 归在"构图"组（l1=构图），
    # 照 l1 走会把它送进槽位 6；照旧关键词表走会送进槽位 3。两者都与指南冲突。
    if any(k in tag for k in _FACE_SLOT2):
        return 2
    # §4.1 槽位 5 = 光影色调；词典把 `sunlight` 这类归在"背景"组，照 l1 走会掉进槽位 4。
    # 用**词尾**判定（不能用子串：`light blue hair` 里也有 "light"，那是槽位 2 的发色）。
    if tag.endswith(_LIGHT_SLOT5):
        return 5
    if td is not None:
        l1 = td.l1(lookup_key(tag))
        if l1 in _L1_SLOT:
            return _L1_SLOT[l1]
        cat = (td.meta.get(lookup_key(tag)) or {}).get("category")
        if cat == 4:
            return 1
        if cat == 3:
            # 默认 category_drop 已经把 IP 系列名整类丢掉（§2.3「永不添加」）；这里只兜住
            # 用户手动关掉该规则的情况：模型卡的段序是 [character] [series]，贴着人数标签放。
            return 1
    if tag.endswith((" hair", " eyes")):
        return 2
    if any(k in tag for k in ("pose", "standing", "sitting", "lying", "kneeling", "crouching",
                              "walking", "running", "jumping", "arms", "hands", "legs",
                              "head tilt", "holding", "spread", "on back",
                              "all fours", "leaning", "hugging", "kissing")):
        return 3
    if any(k in tag for k in ("light", "shadow", "tone", "contrast", "backlight", "sunlight")):
        return 5
    if any(k in tag for k in ("background", "indoors", "outdoors", "sky", "room", "floor",
                              "wall", "window", "chair", "bed", "tree", "water", "city")):
        return 4
    return 0


# --------------------------------------------------------------------------
# caption parsing / assembly
# --------------------------------------------------------------------------
def parse_caption(text: str) -> tuple[list[str], str]:
    """Split a caption into (tag-ish segments, natural language tail)."""
    if not text:
        return [], ""
    text = text.replace("\ufeff", "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"^[\s,]+", "", text)
    parts = [p.strip() for p in text.split(",")]
    tags: list[str] = []
    prose: list[str] = []
    for part in parts:
        if not part:
            continue
        words = part.split()
        looks_like_prose = len(words) >= 6 or (len(words) >= 4 and not re.match(
            r"^[a-z0-9'\- ]+$", part))
        (prose if looks_like_prose else tags).append(part)
    nlp = ". ".join(p.rstrip(".") for p in prose).strip()
    if nlp and not nlp.endswith("."):
        nlp += "."
    return tags, nlp


def wash_tags(raw_tags: list[str], rules: dict, td, trigger: str = "",
              report: dict | None = None, drop_extra: set[str] | None = None,
              drop_chars: dict[str, int] | None = None) -> list[str]:
    rep = report if report is not None else {}
    dropped: list[dict] = rep.setdefault("dropped", [])
    unknown: list[str] = rep.setdefault("unknown", [])
    seen: set[str] = set()
    out: list[str] = []
    trig_cmp = trigger.strip().lstrip("@").lower() if trigger else ""

    def emit(raw: str, allow_alias: bool = True) -> None:
        """处理单个标签。别名**先于** drop_exact 展开——否则 `topless`→`breasts out`、
        `smiling`→`smile` 这类"换现行形"的规则永远走不到（标签会先被当 banned 丢掉）。"""
        tag = normalize_tag(raw)
        if not tag:
            return
        if allow_alias:
            for extra in rules.get("alias_extra") or []:
                if tag == normalize_tag(extra.get("from", "")):
                    tag = normalize_tag(extra.get("to", ""))
            target = rules["aliases"].get(tag)
            if isinstance(target, (list, tuple)):      # §7.2 `bikini pull` 拆成两个现行标签
                for one in target:
                    emit(one, allow_alias=False)
                return
            if target:
                tag = target
        if tag in rules["drop_exact"] or is_negative(tag, rules, td):
            dropped.append({"tag": tag, "why": "banned"})
            return
        tag = downgrade_color(tag, rules)
        if td is not None:
            # 水印区标签：图已被去字工具修补过 ⇒ 那片像素没了，再写就是幻觉（§8.1 决策树）
            if drop_extra and tag in drop_extra:
                dropped.append({"tag": tag, "why": "watermark-removed-by-inpaint"})
                return
            if tag not in rules.get("category_keep", ()):
                cat = (td.meta.get(lookup_key(tag)) or {}).get("category")
                if cat in rules.get("category_drop", ()):
                    dropped.append({"tag": tag, "why": _CAT_WHY.get(cat, f"category-{cat}")})
                    return
                if cat == 4 and drop_chars and tag in drop_chars:
                    n = drop_chars[tag]
                    dropped.append({"tag": tag, "why": f"low-freq-character({n}<{rules.get('character_min_images', 0)})"})
                    return
        if td is not None and lookup_key(tag) not in td \
                and tag not in rules["descriptor_whitelist"] \
                and tag not in (rules.get("rating_tags") or ()):
            if trig_cmp and tag.strip().lstrip("@").lower() == trig_cmp:
                return            # 触发词不是标签，别记成"词典里没有"
            unknown.append(tag)
            if rules.get("unknown_policy") == "drop":
                dropped.append({"tag": tag, "why": "not-a-real-tag"})
                return
        if tag in seen:
            dropped.append({"tag": tag, "why": "duplicate"})
            return
        seen.add(tag)
        out.append(tag)

    for raw in raw_tags:
        emit(raw)

    out = fold_pairs(out, rules)
    out, implied_dropped = apply_implication(out, td, rules) if td is not None else (out, [])
    for t in implied_dropped:
        dropped.append({"tag": t, "why": "implied-by-child"})
    out = add_implied(out, rules)

    seen = set()
    deduped = []
    for t in out:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    out = deduped

    if trigger:
        trig = trigger if trigger.startswith("@") else "@" + trigger
        out = [t for t in out if t.lstrip("@") != trig.lstrip("@")]
        out.insert(0, trig)

    # seven-slot ordering, stable inside a slot
    order: dict[int, list[str]] = {}
    offset = 1 if trigger else 0
    for i, t in enumerate(out):
        if trigger and i == 0:
            continue
        slot = classify(t, td, rules)
        order.setdefault(slot, []).append(t)
    ordered: list[str] = [out[0]] if trigger else []
    # 槽位 0 = classify 认不出来的词（词典无此条、关键词表也没命中）。故意排在**槽位 3 之后、
    # 槽位 4 之前**：它既不是人数/外观也不是背景/镜头，夹在中间最不干扰前三个高权重槽位
    # 的相邻关系；这个顺序是刻意的，不是漏排（改它会动全数据集的 token 序）。
    # 槽位 9 = 评级词（用户拍板保留），排在**触发词之后、槽位 1 之前**——即官方段
    # [quality/meta/year/safety] 的位置，与 §4.3 的段序一致。
    for slot in (9, 1, 2, 3, 0, 4, 5, 6, 7):
        ordered.extend(order.get(slot, []))

    rep["unknown"] = sorted(set(unknown))
    rep["tag_count"] = len([t for t in ordered if not (trigger and t == ordered[0])])
    return ordered


def _prose_key(s: str) -> str:
    return _WS.sub(" ", s.strip().strip(".").lower().replace("_", " ")).strip()


def filter_prose(nlp: str, rules: dict, td, dropped: list[dict] | None = None) -> str:
    """散文尾巴也要过规则闸门。

    为什么必须做：`parse_caption` 把 >=6 词的片段判成散文，而 danbooru 上正是长名字最多
    —— `ender_lilies_quietus_of_the_knights`（copyright）、`someartist_(handle)`（artist）
    都会走散文路径。来源 C 就是上一版 caption 时，被 category_drop 丢掉的 IP 系列名会从
    散文路径原样回来（实测 0004.png 就这么漏出过一个 IP 名 + 多余句号）。
    """
    if not nlp:
        return ""
    gone = {_prose_key(str(d.get("tag") or "")) for d in (dropped or [])}
    keep: list[str] = []
    for part in nlp.rstrip(".").split(". "):
        p = part.strip()
        if not p:
            continue
        key = _prose_key(p)
        if key in gone or key in rules["drop_exact"] or is_negative(key, rules, td):
            continue
        if td is not None and key not in rules.get("category_keep", ()):
            rec = td.meta.get(lookup_key(key)) or {}
            if rec.get("category") in rules.get("category_drop", ()):
                continue
        keep.append(p)
    return (". ".join(keep) + ".") if keep else ""


def assemble(tags: list[str], nlp: str, rules: dict) -> str:
    """标签串 + 可选散文尾巴。

    指南 §3.1：实测成品是**纯标签串**，混排自然语言「可选，非默认」；真要写时标签段与散文
    之间用 **`. `**（句点+空格）过渡（不是 `, `），散文 1~3 句且**不得复述标签**。
    """
    line = ", ".join(t for t in tags if t)
    if nlp and rules.get("keep_natural_language", False):
        # 洗标可以重跑，而重跑时"上一版 caption"就是来源 C：多词标签会被 parse_caption 判成
        # 散文，于是同一个东西既当标签又当句子尾巴出现一次。
        have = {_prose_key(t) for t in tags}
        keep = [p.strip() for p in nlp.rstrip(".").split(". ")
                if p.strip() and _prose_key(p) not in have][:3]
        if keep:
            tail = ". ".join(p.rstrip(".") for p in keep).rstrip(".") + "."
            line = f"{line}. {tail}" if line else tail
    line = re.sub(r"\s+", " ", line).strip()
    line = line.replace(" ,", ",")
    return line


def validate(line: str, rules: dict, tag_count: int, td=None) -> list[str]:
    issues: list[str] = []
    if "\n" in line:
        issues.append("multi-line")
    if line != line.lower():
        issues.append("uppercase")
    if line.startswith(","):
        issues.append("leading-comma")
    if "  " in line:
        issues.append("double-space")
    if re.search(r"\w_\w", line.replace("score_", "")):
        issues.append("underscore-form")
    if tag_count < int(rules.get("min_tags", 20)):
        issues.append(f"too-few-tags({tag_count})")
    if tag_count > int(rules.get("max_tags", 45)):
        issues.append(f"too-many-tags({tag_count})")
    for part in (t.strip() for t in line.split(",")):
        if part and is_negative(part, rules, td):
            issues.append(f"banned-tag({part})")
            break
    return issues


# 人数一致性（§9「人数一致性冲突 0」）：`solo` 与多人数/`solo focus` 互斥
_SOLO = "solo"
_MULTI_COUNT = ("2girls", "3girls", "4girls", "5girls", "6+girls", "multiple girls",
                "1boy", "2boys", "3boys", "multiple boys", "solo focus")


def audit_caption(text: str, rules: dict, td=None, patched: bool = False) -> list[str]:
    """§9 离线闸门的**残留检查**：这张 caption 里是否还有"规则本该丢掉"的东西。

    与 `validate` 的分工：`validate` 查形态（单行/大小写/分隔符/标签数），这里查内容——
    规则引擎跑一遍，凡是会被丢的标签就是残留。用途是给"人工编辑过"或"别的打标工具写的"
    caption 兜底：`wash` 跑完再手改，画师/IP/质量词就可能悄悄回来（指南 §9 明确要求
    「BANNED 令牌零残留」「否定式 token 零残留」「质量词 0、文字族 0」）。"""
    issues: list[str] = []
    raw = text
    if raw.startswith("\ufeff"):
        issues.append("bom")
    if raw.endswith("\n") or raw.endswith("\r"):
        issues.append("trailing-newline")
    body = raw.strip()
    if body.endswith((".", "!", "?", ";", ":")):
        issues.append("trailing-punctuation")
    tags, _ = parse_caption(body)
    names = [normalize_tag(t) for t in tags]
    hard = {"drop_exact", "aliases", "category_drop"}
    residue: list[str] = []
    for t in names:
        if t.startswith("@"):
            continue
        if t in rules.get("category_keep", ()) or t in rules.get("descriptor_whitelist", ()):
            continue
        if t in rules.get("watermark_tags", ()) and not patched:
            continue                       # 未修补的图，水印区标签是**要求写**的（§8.1）
        if t in rules.get("drop_exact", ()):
            residue.append(t)
            continue
        al = rules.get("aliases", {}).get(t)
        if al is not None and al != t:     # 自映射别名不算残留（曾经表里混进过一条）
            residue.append(f"{t}->" + "+".join(al) if isinstance(al, list) else f"{t}->{al}")
            continue
        if td is not None:
            cat = (td.meta.get(lookup_key(t)) or {}).get("category")
            if cat in rules.get("category_drop", ()):
                residue.append(f"{t}({_CAT_WHY.get(cat, f'category-{cat}')})")
                continue
        if is_negative(t, rules, td):
            residue.append(f"{t}(negation)")
    if residue:
        issues.append("banned-residue(" + "|".join(residue[:4]) +
                      (f"|+{len(residue) - 4}" if len(residue) > 4 else "") + ")")
    if _SOLO in names and any(t in names for t in _MULTI_COUNT):
        clash = [t for t in names if t in _MULTI_COUNT]
        issues.append("count-conflict(solo vs " + ", ".join(clash) + ")")
    return issues


# --------------------------------------------------------------------------
# stage entry points
# --------------------------------------------------------------------------
def _source_tags(ds, name: str, rmap: dict, meta: dict) -> tuple[list[str], str, str]:
    """Return (tags from source A, existing caption text from source C, origin label).

    来源 A 有两条路，优先走**列表**那条：`raw_posts.jsonl` 的 `tags` 是 list，标签边界无歧义；
    `rename_map.csv` 只能存字符串（下划线形，见 `curate.cmd_rename`），万一遇到空格形的老 CSV，
    `.split()` 会把多词标签切成两个词。CSV 用 `old_name` 回查 raw_posts（重排后文件名已变）。
    """
    row = rmap.get(name) or {}
    old_name = str(row.get("old_name") or name)
    tags_a: list[str] = [str(t) for t in (meta.get(old_name, {}).get("tags") or [])]
    if not tags_a:
        tags_a = (row.get("tags_full") or "").split()
    caption_file = None
    for base in (ds.images_dir, ds.raw_dir):
        cand = base / (Path(name).stem + ".txt")
        if cand.exists():
            caption_file = cand
            break
    text_c = caption_file.read_text(encoding="utf-8", errors="replace") if caption_file else ""
    origin = "A+booru" if tags_a else "A=none"
    return tags_a, text_c, origin


def _patched_images(ds, rmap: dict) -> set[str]:
    """返回「大段文字/水印已被去字工具修补掉」的图片名（当前文件名）。

    指南 §8.1 决策树第一支：**能在图像层去掉就去掉** —— 那片像素已经没了，caption 里再写
    `watermark`/`signature`/`logo` 就是幻觉（§11.2 检查清单「每个标签都在画面里真实存在」）。
    `text` 阶段跑在 `rename` 之前，所以报告里是原文件名，要经 rename_map 映射回当前名。
    """
    out: set[str] = set()
    path = ds.pipe_dir / "text_report.csv"
    if not path.exists():
        return out
    old2new = {str(r.get("old_name") or ""): str(r.get("new_name") or "") for r in rmap.values()}
    for r in csv.DictReader(open(path, encoding="utf-8", newline="")):
        if str(r.get("decision") or "").strip() != "patch":
            continue
        old = str(r.get("file") or "").strip()
        if old:
            out.add(old)                      # 没跑过 rename 的数据集直接按名字对
            if old2new.get(old):
                out.add(old2new[old])
    return out


def cmd_wash(ds, apply: bool = False, trigger: str = "", rules_over: dict | None = None,
             include_images: bool = True) -> dict:
    rules = load_rules(ds, rules_over)
    trigger = trigger or rules.get("trigger") or ""
    if not trigger:
        print("[wash] ! 未指定触发词（--trigger 或 manifest.trigger），将不插入触发词")
    td = dicts.load(ds.cfg)
    print(f"[wash] 词典 {td.size()} 个标签；触发词 {trigger or '(无)'}")
    _kind = rules.get("kind") or "style"
    _cats = "".join(f" {c}={_CAT_WHY.get(c, c)}" for c in rules.get("category_drop", ()))
    print(f"[wash] 目标类型 {_kind}；整类丢弃的词典 category：{_cats or ' （无）'}")
    if _kind == "character":
        print("[wash] 角色 LoRA：角色名 tag 由触发词承载 -> 整类剔除（指南 §5.2 / §8.2）")
    files = ds.images() if include_images else ds.raw_images()
    if not files:
        raise SystemExit("[wash] 没有图片")
    rmap = curate.load_rename_map(ds)
    meta = curate.load_meta(ds)

    # ---- pass 1：来源标签只读一次，顺带统计角色频次（§8.2）与水印修补状态（§8.1）
    sources: dict[str, tuple[list[str], str, str]] = {}
    char_count: dict[str, int] = {}
    char_min = int(rules.get("character_min_images") or 0)
    for f in files:
        tags_a, text_c, origin = _source_tags(ds, f.name, rmap, meta)
        sources[f.name] = (tags_a, text_c, origin)
        if char_min:
            tags_c, _ = parse_caption(text_c)
            for t in list(tags_a) + list(tags_c):
                t = normalize_tag(t)
                if (td.meta.get(lookup_key(t)) or {}).get("category") == 4:
                    char_count[t] = char_count.get(t, 0) + 1
    drop_chars = {t: n for t, n in char_count.items() if n < char_min}
    if char_min:
        print(f"[wash] 角色阈值 §8.2：本数据集出现 <{char_min} 次的角色剔除"
              f"（{len(drop_chars)}/{len(char_count)} 个角色）")
        if drop_chars:
            print("  " + "; ".join(f"{t}×{n}" for t, n in
                                   sorted(drop_chars.items(), key=lambda kv: (kv[1], kv[0]))[:8]))
    else:
        print("[wash] 角色名**不做频率限制**（character_min_images=0）：出现几次都保留"
              "（指南 §8.2 建议 ≥4，本预设按用户拍板取消；要恢复就把它设回 4）")
    patched = _patched_images(ds, rmap)
    if patched:
        print(f"[wash] 去字工具修补过 {len(patched)} 张 -> 这些图不再写 watermark/signature/logo"
              f"（§8.1：能在图像层去掉就去掉，比标出来更好）")
    wm_drop = set(rules["watermark_tags"]) if patched else None

    rows: list[dict] = []
    review: list[dict] = []
    no_source = 0
    prose_dropped = 0
    emptied: list[str] = []            # caption 洗完只剩触发词的张数（来源标签全被规则丢光）
    emptied_why: dict[str, int] = {}
    for f in files:
        tags_a, text_c, origin = sources[f.name]
        if not tags_a and not text_c.strip():
            no_source += 1
        tags_c, nlp_c = parse_caption(text_c)
        rep: dict = {}
        merged = wash_tags(list(tags_a) + list(tags_c), rules, td, trigger, rep,
                           drop_extra=wm_drop if f.name in patched else None,
                           drop_chars=drop_chars)
        nlp_c = filter_prose(nlp_c, rules, td, rep.get("dropped"))
        if nlp_c and not rules.get("keep_natural_language", False):
            prose_dropped += 1
        line = assemble(merged, nlp_c, rules)
        issues = validate(line, rules, rep.get("tag_count", 0), td)
        if not [t for t in merged if not t.startswith("@")]:
            emptied.append(f.name)
            for d in rep.get("dropped", []):
                k = str(d.get("why"))
                emptied_why[k] = emptied_why.get(k, 0) + 1

        out_path = f.with_suffix(".txt")
        if apply:
            out_path.write_text(line, encoding="utf-8", newline="\n")

        rows.append({
            "file": f.name, "origin": origin, "tags_in": len(tags_a) + len(tags_c),
            "tags_out": rep.get("tag_count", 0), "dropped": len(rep.get("dropped", [])),
            "drop_why": "|".join(sorted({str(d.get("why")) for d in rep.get("dropped", [])}))[:160],
            "patched": "yes" if f.name in patched else "",
            "unknown": "|".join(rep.get("unknown", [])[:8]),
            "issues": "|".join(issues), "caption": line[:600],
        })
        need = _review_fields(merged, rules)
        if need:
            review.append({"file": f.name, "thumb": str(ds.thumbs_dir / (f.stem + ".jpg")),
                           "missing": ",".join(need), "caption": line[:400]})

    curate.write_csv(ds.pipe_dir / "wash_report.csv", rows)
    curate.write_csv(ds.pipe_dir / "review_todo.csv", review)
    bad = [r for r in rows if r["issues"]]
    print(f"[wash] {len(rows)} 张 -> captions {'已写入' if apply else '未写入(加 --apply)'}"
          f"；有问题 {len(bad)} 张；待看图补全 {len(review)} 张")
    if bad[:5]:
        print("  例：" + "; ".join(f"{r['file']}:{r['issues']}" for r in bad[:5]))
    if prose_dropped:
        print(f"[wash] {prose_dropped}/{len(rows)} 张 caption 里有散文尾巴，按 §3.1（实测成品是纯标签串）"
              f"未写入；要保留就在 cfg.wash 或 _pipeline/wash_rules.json 里设 keep_natural_language=true")
    if no_source:
        print(f"[wash] ! {no_source}/{len(rows)} 张既没有来源标签也没有既有 caption，成品只剩触发词。"
              f"\n        来源标签读的是 _pipeline/raw_posts.jsonl（fetch 写入）经 rename_map.csv 映射，"
              f"先确认这两个文件非空；本地图请用 .txt sidecar 或 anima-lora-auto-caption 打标后再洗。")
    if emptied:
        why_top = "、".join(f"{w}×{n}" for w, n in sorted(emptied_why.items(),
                                                          key=lambda kv: -kv[1])[:3])
        print(f"[wash] ! {len(emptied)}/{len(rows)} 张 caption 洗完只剩下触发词：来源标签**有**数据，"
              f"但被规则整类丢掉了（主要原因：{why_top}）。"
              f"\n        这就是「画师标签由触发词承载 / IP 系列名永不添加」的必然后果——"
              f"单画师图源（yande.re 一类）的 booru 标签往往只有画师+角色几个，洗完就空了。"
              f"\n        这批图必须补内容：跑 anima-lora-auto-caption 自动打标，或按 review-list 走看图补全，"
              f"再重跑 wash（wash 幂等，可反复跑）。")
    return {"count": len(rows), "issues": len(bad), "review": len(review),
            "no_source": no_source, "prose_dropped": prose_dropped,
            "empty_captions": len(emptied)}


def _review_fields(tags: list[str], rules: dict) -> list[str]:
    """Fields the guide says a vision pass must fill (§4.4)."""
    need: list[str] = []
    if not (set(tags) & set(rules["people_tags"])):
        need.append("people")
    if not (set(tags) & set(rules["framing_tags"])):
        need.append("composition")
    if (set(tags) & set(rules["act_tags"])) and not (set(tags) & set(rules["position_known_tags"])):
        need.append("position")
    return need


def cmd_review_list(ds) -> dict:
    path = ds.pipe_dir / "review_todo.csv"
    if not path.exists():
        # 这是「还没到这一步」而不是失败：wash 没跑过（或没有需要看图的条目）时
        # 不该让 agent 以为流水线炸了，给 0 条 + 下一步指引即可。
        print("[review] 还没有 review_todo.csv —— 先跑 wash 阶段（wash 会按规则列出需要看图的条目）")
        return {"count": 0, "note": "review_todo.csv 不存在，先跑 wash"} 
    rows = list(csv.DictReader(open(path, encoding="utf-8", newline="")))
    print(f"[review] {len(rows)} 张需要看图补全；缩略图在 {ds.thumbs_dir}")
    for r in rows[:20]:
        print(f"  {r['file']:<16} 缺: {r['missing']}")
    return {"count": len(rows)}


def cmd_apply_review(ds, payload: str | Path, apply: bool = True) -> dict:
    """Merge vision-subagent answers back through the same rule engine."""
    rules = load_rules(ds)
    trigger = rules.get("trigger") or ""
    td = dicts.load(ds.cfg)
    data = None
    src = Path(payload)
    if src.exists():
        data = json.loads(src.read_text(encoding="utf-8"))
    elif src.suffix.lower() == ".json" or any(sep in str(payload) for sep in ("/", "\\")):
        raise SystemExit(f"[review] 找不到补标文件：{payload}（写好后用 --payload 指过来）")
    if data is None:
        try:
            data = json.loads(str(payload))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"[review] --payload 既不是存在的文件、也不是合法 JSON：{exc}")
    if isinstance(data, dict):
        data = data.get("images") or data.get("results") or []
    updated = 0
    for item in data:
        name = item.get("file") or item.get("image") or ""
        stem = Path(name).stem
        cand = [f for f in ds.images() if f.stem == stem] + \
               [f for f in ds.raw_images() if f.stem == stem]
        if not cand:
            print(f"  ! {name} 找不到对应图片")
            continue
        txt = cand[0].with_suffix(".txt")
        old = txt.read_text(encoding="utf-8", errors="replace") if txt.exists() else ""
        tags, nlp = parse_caption(old)
        add = []
        for key in ("people", "relationship", "composition", "position", "appearance", "scene"):
            val = item.get(key)
            if not val:
                continue
            add.extend(val if isinstance(val, list) else [val])
        rep: dict = {}
        merged = wash_tags(tags + add, rules, td, trigger, rep)
        line = assemble(merged, nlp or item.get("nlp", ""), rules)
        if apply:
            txt.write_text(line, encoding="utf-8", newline="\n")
        updated += 1
    print(f"[review] 已合并 {updated} 张的看图结果")
    return {"updated": updated}


def cmd_verify(ds, online: bool = False, sample: int = 0) -> dict:
    """Audit every caption against the hard constraints (§3) and the rules."""
    rules = load_rules(ds)
    td = dicts.load(ds.cfg)
    files = ds.images() or ds.raw_images()
    rmap = curate.load_rename_map(ds) if files else {}
    patched = _patched_images(ds, rmap)
    rows: list[dict] = []
    unknown_all: dict[str, int] = {}
    residue_all: dict[str, int] = {}
    for f in files:
        txt = f.with_suffix(".txt")
        text = txt.read_text(encoding="utf-8", errors="replace") if txt.exists() else ""
        tags, nlp = parse_caption(text)
        issues = validate(text.rstrip("\r\n"), rules,
                          len([t for t in tags if not t.startswith("@")]), td)
        if not text:
            issues.append("missing-caption")
        if text:
            extra = audit_caption(text, rules, td, patched=f.name in patched)
            for e in extra:
                if e.startswith("banned-residue("):
                    for item in e[len("banned-residue("):-1].split("|"):
                        if not item.startswith("+"):
                            residue_all[item] = residue_all.get(item, 0) + 1
            issues += extra
        for t in tags:
            tt = normalize_tag(t)
            if td is not None and lookup_key(tt) not in td and \
                    tt not in rules["descriptor_whitelist"] and not tt.startswith("@"):
                unknown_all[tt] = unknown_all.get(tt, 0) + 1
        rows.append({"file": f.name, "tags": len(tags), "issues": "|".join(issues)})
    curate.write_csv(ds.pipe_dir / "wash_verify.csv", rows)
    bad = [r for r in rows if r["issues"]]
    print(f"[verify] {len(rows)} 个 caption，{len(bad)} 个违反硬约束")
    if residue_all:
        top = sorted(residue_all.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
        print(f"[verify] 规则本该丢掉的残留 {len(residue_all)} 种（画师/IP/meta/质量词/否定式/"
              f"该换现行形的别名）：" + ", ".join(f"{k}×{v}" for k, v in top))
        print("         -> 再跑一次 wash 清掉；若是有意保留，加进 wash_rules.json 的对应白名单")
    if unknown_all:
        top = sorted(unknown_all.items(), key=lambda kv: -kv[1])[:15]
        print(f"[verify] 词典中不存在的标签 {len(unknown_all)} 种，出现最多的："
              + ", ".join(f"{k}({v})" for k, v in top))
    if files:
        stems = {f.stem for f in files}
        orphans = [p.name for p in ds.images_dir.glob("*.txt") if p.stem not in stems]
        if orphans:
            print(f"[verify] ! {len(orphans)} 个 .txt 没有对应图片（图-txt 配对不完整）："
                  + ", ".join(sorted(orphans)[:6]))
    if online:
        print("[verify] --online：将用 danbooru search[name_comma] 批量复核（每批 120）")
        _online_verify(ds, sorted(unknown_all))
    return {"count": len(rows), "issues": len(bad), "unknown_kinds": len(unknown_all),
            "residue_kinds": len(residue_all)}


def _online_verify(ds, tags: list[str], batch: int = 120) -> dict:
    s = net.danbooru_session(ds.cfg)
    confirmed: list[str] = []
    for i in range(0, len(tags), batch):
        chunk = tags[i:i + batch]
        query = " ".join(lookup_key(t) for t in chunk)
        try:
            data = net.get_json(s, "https://danbooru.donmai.us/tags.json",
                                {"search[name_comma]": query, "limit": batch})
        except Exception as exc:
            print(f"  ! online verify batch {i // batch} failed: {exc}")
            continue
        found = {d["name"].replace("_", " ") for d in data}
        confirmed.extend(sorted(set(chunk) & found))
    print(f"[verify] 在线确认 {len(confirmed)}/{len(tags)} 个标签真实存在")
    return {"confirmed": confirmed}
