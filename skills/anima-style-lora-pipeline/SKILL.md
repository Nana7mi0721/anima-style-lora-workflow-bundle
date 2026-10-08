---
name: anima-style-lora-pipeline
description: Anima 风格 LoRA 数据集流水线的总纲：阶段顺序与推进条件、anima_status/anima_stage/anima_job/anima_doctor 四个工具的用法、每阶段该产出什么文件、哪里必须停下来问用户、失败与续跑怎么处理。用户要求"做一个 Anima 风格 LoRA 数据集""跑 anima 流水线""从下载到训练配置走一遍"时使用；单点问题（只下载、只洗标）直接看对应的专项 skill。
---

# SKILL：Anima 风格 LoRA 数据集流水线

目标产物：`<home>/datasets/<name>/` 下一套筛干净的图片 + 合规 caption，外加 `train_configs/<name>/` 的训练配置三件套。

## §1 目录契约

```
<home>/datasets/<name>/
  00_raw/            下载/导入的原始图（只读，不动；**不算图桶**）
  images/            默认图桶：图片 + 同名 .txt caption（0001.png + 0001.txt）★ caption 唯一权威副本
  clean/ watermark/  可选图桶：根下**任何含图的子目录**都自动算一个桶（自己分的 clean/watermark/latest 都行）
  _pipeline/
    manifest.json    阶段状态（每个 stage: pending/preview/done/stale/failed + 时间戳）+ 触发词
    per_image.json   ★ 每张图的只追加状态（旧名、写过的标签、人工删掉的标签、所在桶、看图结果、历史）
    user_deleted.json ★ 人工淘汰账本（谁、为什么、归档到哪、能不能搬回）
    rename_map.csv   新名↔旧名↔post_id↔tags_full 对照表（★ 只追加；预演写 rename_map.preview.csv）
    captions_prev/   写 caption 之前自动留的快照（只留最近 3 份）+ wash_diff.csv（本次 vs 上次的逐张差异）
    dedup_report.csv / screen_report.csv / text_report.csv / wash_report.csv / review_todo.csv / wash_verify.csv
    thumbs/          ≤1536px 缩略图（视觉子代理唯一允许看的输入）
    masks/           文字检测的掩膜与对照图；orig_text/ 是修补前的原图备份
    wash_rules.json  可选，数据集级洗标规则覆盖（minTags/maxTags/prefer/unknown_policy 都可写这里）
  _excluded/         被剔除的图（按 reason 分子目录），永不删
```

### 图桶：`images/` 不是唯一落点

真实使用里出现过 `clean/`（109 张已修水印）+ `watermark/`（33 张未修）、而 `images/` 是空的布局 —— 成品 caption 全在子目录里，工具只数 `images/` 时 `anima_status` 报 `raw=0 images=0 captions=0`，`verify`/`thumbs`/`fix-caption` 对 142 条成品视而不见，`makecfg` 还会把同一张图算两遍。

现在的规则：**根下任何含图的子目录都自动算图桶**（跳过 `_`/`.` 前缀与 `00_raw`/`thumbs`/`masks`/`orig_text`；空目录不算），`images/` 排最前；caption 与图**同目录**；所有阶段默认扫全部桶。只想处理某个子集就传 `workSet:"clean,watermark"`——**给错名字会报错并列出自动发现的桶**（不会静默变成空集）。`makecfg` 另按内容指纹跨桶去重（硬链接/副本只算一次），整桶都是副本时跳过并说明原因。

`anima_status` 会多打一行 `图桶: clean 109图/109caption、watermark 33图/33caption`——**看到这行就不要再去数 `images/` 了**。

`<home>` 默认 `E:/LoRA_Train`，可用插件配置或 `--home` 覆盖。训练配置落在 `<home>/train_configs/<name>/`（`<name>_lora_stage1.toml` + `dataset_<name>.toml` + `train_<name>.bat` + `rationale.md` + `preflight.txt`）。

### 状态文件的所有权（谁写、谁读——真实使用里全靠这个）

一次真实跑批的教训：**技术卡点都顺，成本全花在「谁覆盖谁」上**（72 张图里靠临时脚本救了 3 次场）。所以规矩写死：

| 文件 | 谁写 | 谁读 | 规矩 |
|---|---|---|---|
| `<桶>/<stem>.txt` | `wash` / `apply-review` / `fix-caption` | **所有下游**（`verify`、`makecfg`、你自己） | **caption 的唯一权威副本**，与图同目录。要改 caption 走 `fix-caption`，不要手改文件再重跑 `wash`（重跑会按图源重建） |
| `_pipeline/per_image.json` | `rename` / `wash` / `apply-review` / `fix-caption` / `exclude` | 引擎自己 + 你排查时 | **只追加**：`old_names`/`merged_tags` 取并集，`history` 保留最近 30 次，`bucket` 记它在哪个桶。它是"这张图经历过什么"的账本，不删记录 |
| `_pipeline/rename_map.csv` | `rename` | `wash`（按 `old_name` 回查 booru 标签） | **只追加**，多批次共存；每批另留 `rename_map.<first>-<last>.csv` 快照。**不要读它的 `tags_full` 当标签来源**（那是抓取当时的快照，可能已被清洗） |
| `raw_posts.jsonl` | `fetch` / `import` / `enrich` | `rename`（写进对照表）、`wash`（图源标签 A） | 只追加，按 `(filename, post_id)` 去重合并。**行键是文件名**：`enrich` 补的行必须落在 `rename_map.csv` 的 `old_name` 上，否则 `wash` 静默读不到（它自己按这条选键） |
| `_pipeline/user_deleted.json` | `exclude` / `fix-caption`（`remove`） | `wash`（防复活）、`verify` | 人工淘汰账本：`name`/`old_names`/`archived`/`bucket`/`reason`/`at`，`restored_at` 表示已搬回。**手删图要发 `exclude`，别直接删文件**（否则下一轮只会告诉你"少了几张"） |
| `_pipeline/captions_prev/` + `wash_diff.csv` | `wash` / `apply-review` / `fix-caption` | 人 | 写 caption **前**自动快照（最近 3 份），写完后出逐张差异（新增/删除/清空）。"这次到底改了什么"不用自己 diff |
| `manifest.json` | 每个阶段 | `anima_status`、`wash.resolve_trigger` | 触发词/类型的权威来源 |

**推论**：`wash` 默认**不会复活**你删掉的图源标签（它拿 `per_image.json` 的 `merged_tags` 与当前 caption 比对，差值视为人工删除，再并上 `user_deleted.json` 的名单）。三级开关：默认（差集 + 手删名单）→ `refreshBooru:true`（只忽略手删名单）→ `refreshSource:true`（连手删名单都不看，**会复活**）。这条就是"手工改完 caption 一重跑全回来"那个坑的修法。

## §2 阶段表与 I/O 契约

**17 个阶段**。最后一列是「重跑会覆盖什么」——决定你能不能放心重跑；标 `只追加` 的多跑一次只是浪费，标 `覆盖` 的会抹掉人工修改。

| 阶段 | 工具调用 | 输入 | 产出 | 重跑会覆盖什么 |
|---|---|---|---|---|
| init | `anima_stage(stage:"init", dataset:"X")` | — | 目录骨架 + manifest | 不重建已存在的 manifest（`force:true` 就地改触发词/类型并保留阶段记录；`reset:true`+`yes:true` 才推倒重来） |
| fetch | `anima_stage(stage:"fetch", dataset:"X", source:"yandere", tags:"artist_name", limit:250)` | 图源 | `00_raw/*` + raw_posts.jsonl | 只追加：同名文件跳过，元数据按 `(filename, post_id)` 去重合并。**默认就下载**，`dryRun:true` 只列清单 |
| import | `anima_stage(stage:"import", dataset:"X", src:"D:/下载/xxx")` | 素材目录/压缩包 | `00_raw/*`（zip/rar/7z 自动解） | 只追加（重名加 `__n`）。**`src` 不能是数据集目录本身或它的上级**，会被拒绝；非图文件会报"跳过 N 个"，不进 `00_raw` |
| enrich | `anima_stage(stage:"enrich", dataset:"X")` → `apply:true` | `images/`（没重排就是 `00_raw/`） | raw_posts.jsonl + enrich_report.csv（含 `by` 列） | 只追加（按 `(filename, post_id)` 合并；`force:true` 才重查已有标签的图）。**默认 dry-run 只查不写**，但**仍会发请求**——就是给你看命中率。**必须在 `text` 修补之前跑**（改过像素 md5 就对不上）；md5 不中时用文件名里的 pixiv id 兜一次（只认同名同页/单帖 p0，`noPixiv:true` 可关） |
| dedup | `anima_stage(stage:"dedup", dataset:"X")` → `apply:true` | `00_raw/`（`includeImages:true` 连图桶） | dedup_report.csv | 覆盖报告；apply 时把重复图**移动**到 `_excluded/duplicates/` |
| screen | `stage:"screen"` → 视觉复核 → `apply:true` | `00_raw/`（`includeImages:true` 连图桶） | screen_report.csv + review_queue.csv | 覆盖报告；apply 时移入 `_excluded/<reason>/` |
| thumbs | `anima_stage(stage:"thumbs", dataset:"X")` | 图桶（没有才用 `00_raw/`） | `_pipeline/thumbs/*.jpg` | 覆盖已有缩略图（**不吃 apply**，写盘是默认行为）；已是最新的跳过，`prune:true` 清掉没有对应图的陈旧缩略图 |
| text | `anima_stage(stage:"text", dataset:"X")` → `apply:true` | `00_raw/`（`includeImages:true` 处理 `images/`） | 修补后的图 + text_report.csv + `masks/` | **⚠️ 修补不可逆**：apply 就地改写图片（修补前备份在 `_pipeline/orig_text/`），并让 `wash` 及之后全部失效 |
| rename | `anima_stage(stage:"rename", dataset:"X", apply:true)` | `00_raw/`（已重排过会报错） | `images/0001.ext` + rename_map.csv + per_image.json | `rename_map.csv` **只追加**（按 `old_name` 去重，本次为准）+ 本批快照；预演只写 `rename_map.preview.csv`，不碰权威表；同名 `.txt` 边车跟着搬 |
| wash | `anima_stage(stage:"wash", dataset:"X", trigger:"@xxx")` → `apply:true` | 图桶里的 `*.txt` + raw_posts.jsonl（经 rename_map 的 `old_name` 回查） | 每个桶里的 `NNNN.txt` + wash_report.csv + review_todo.csv + wash_diff.csv + captions_prev/ | 覆盖 caption（**幂等**：同输入重跑结果一致）。默认不复活人工删掉的图源标签（`refreshBooru` / `refreshSource` 逐级放宽）；写盘前自动快照、写盘后出本次 vs 上次的差异 |
| review-list | `anima_stage(stage:"review-list", dataset:"X")` | wash_report.csv | review_todo.csv | 覆盖清单 |
| apply-review | `anima_stage(stage:"apply-review", dataset:"X", payload:"...json", apply:true)` | 视觉子代理的补标 JSON | 回填后的 caption | 覆盖这些图的 caption；触发词从 manifest 取（不会丢）；`item.remove` 里的标签进人工删除名单 |
| fix-caption | `anima_stage(stage:"fix-caption", dataset:"X", name:"0007", add:"…", apply:true)` | `images/<stem>.txt` | 改好的 caption | **只动你点名的那一张**：`add` 补标签 / `remove` 删标签（并记进人工删除名单）/ `set` 整条替换，都要再过一遍洗标 + 形态校验 |
| exclude | `anima_stage(stage:"exclude", dataset:"X", names:"0007,0012", reason:"user", apply:true)` | 图桶里的图 | 图+同名 txt 移到 `_excluded/<reason>/` + user_deleted.json | **淘汰专用**：`undo:true` 按账本搬回**原来的桶**、`missing:true` 把"账本里有、现在不在图桶里"的记成人工删除（事后补账）、无参数则列清单。别直接删文件 |
| verify | `anima_stage(stage:"verify", dataset:"X")` / `online:true` | 图桶里的 `*.txt` | wash_verify.csv | 覆盖报告（**只读，不写 caption**，也没有 apply）。查形态 + 内容闸门 + **冲突族** + **水印区与修补状态一致性**；`sample:N` 等距抽查；`trigger:"@yyy"` 可覆盖 |
| dict-check | `anima_stage(stage:"dict-check", dataset:"X")` / `tags:"a, b"` | caption 或显式标签 | 终端输出（不写盘） | — |
| makecfg | `anima_stage(stage:"makecfg", dataset:"X", kind:"style", trigger:"@xxx", subdirs:"before,latest,present")` | 图桶 + manifest | `train_configs/X/` 四件套 + **preflight.txt** | 覆盖已有配置（旧文件先改名成 `.bak<时分秒>` 备份；`force:true` 则不备份）。**LR 超出该 rank 的建议区间直接报错**，除非 `allowOutOfBand:true`；自动发现图桶并按内容指纹跨桶去重 |

顺序不是死板的：`text` 必须在 `rename` 前（rename 后文件名与 raw_posts.jsonl 的对应关系会断，text 报告就不好回溯）；`wash` 必须在 `rename` 后（caption 文件名跟新编号走）；`thumbs` 可以在 `screen` 前先跑一遍用于人工看；**`enrich` 要在 `text` 之前**（修补改像素 ⇒ md5 变了对不上原帖；local 图源没有 booru 标签时它是唯一的 A 源）。

**`enrich` 命中率低是常态，不是故障**：实测 redash 的 72 张图 md5 只中 3 张，200 张 pixiv 原图里 46 张能解析出 illust id 的只有 6 张在 danbooru 有帖。只发 pixiv 的画师在 booru 上就是没有 ⇒ **A 源稀薄时把力气放到 B 源（看图）**，别反复重跑 `enrich`。命中的那几张是白捡的权威标签，照收。

**dry-run 不算完成**：没写盘的那一跑在 manifest 里记成 `preview`，`anima_status` 里显示 `▷`，`done=[…]` 里也不会出现它。看到 `▷` 就表示「报告看过了、还没落地」，下一步是带 `apply:true` 重跑同一阶段。`↻ stale` 表示上游改过（例如 `text` 修补了图），这条链上的洗标/配置都要重跑。

### 参数矩阵：哪些阶段不吃 apply（真实使用里靠试错才发现）

传了该阶段不支持的参数，**在执行前就会被拒**（不会静默忽略），错误里会列出该阶段支持的参数。记住四条例外：

| 阶段 | 例外 |
|---|---|
| `init` / `thumbs` | **不吃 `apply`** —— 写盘就是它们的默认行为 |
| `fetch` / `import` | 也是默认写盘，但用 **`dryRun:true`** 预演（不是 `apply`） |
| `verify` / `review-list` / `dict-check` | **只读**，没有 `apply`（`verify` 可以 `trigger` 覆盖，`dict-check` 可以只给 `tags` 不给 dataset） |
| `force` | 只在 `init`、`enrich`、`makecfg` 上存在 |
| `enrich` 的 `noPixiv` | 关掉「md5 不中就用文件名里的 pixiv id 再兜一次」这条支路（默认开着） |
| `workSet` | `dedup` / `screen` / `thumbs` / `wash` / `verify` / `exclude` 都能限定只看某些图桶（`"clean,watermark"`）；不传就是全部桶 |
| `exclude` | 默认 dry-run，要 `apply:true`；没有 `dryRun`（反过来也一样） |

`fix-caption` 的 `add`/`remove`/`set` 三个参数至少要给一个；`set` 是整条替换，给了它 `add`/`remove` 会被忽略。

`wash` 的常用旋钮（都在参数矩阵里，传错会报错）：`minTags`/`maxTags`（标签数上下限，写进 rules）、`prefer:"A"|"C"`（同族冲突留哪边）、`dropMarks`（整类丢水印/签名/logo 族）、`refreshBooru` / `refreshSource`（防复活逐级放宽）、`trigger`（不传就从 manifest 取）。

## §2.5 与已有 skill 的分工（不要重复造）

打标与调参这两块的**知识**已经沉淀在用户自己的 skill 里，本 bundle 只提供**机械执行**：

| 事情 | 归谁 |
|---|---|
| caption 形态规范化（单行/小写/`", "` 分隔/20~45 标签/七槽位排序/触发词置首/别名归一/否定式剔除） | 本 bundle 的 `wash` 阶段（规则引擎，可复现） |
| 「这张图该打什么标签」的语义判断、三源优先级、什么该写什么不该写 | `anima-style-lora-caption` skill |
| 批量自动打标（anima_lora 的 caption-autotag，走 GPU daemon） | `anima-lora-auto-caption` skill |
| 三件套文件的生成（字段、默认值、文件命名、rationale.md） | 本 bundle 的 `makecfg` 阶段 |
| rank / LR / epochs / repeats / 分辨率怎么定、第二轮怎么调 | `anima-lora-config` skill |

即：**「写什么」问 caption skill，「能不能开跑」问 config skill，「文件长什么样、放哪里、跑没跑过」问本 bundle 的工具。**

### wash 会按指南整类丢标签——这是设计，不是丢数据

`wash` 的判据是「有没有固定视觉对应物」（指南 §2.3/§2.4），所以**画师标签（词典 category=1）、IP 系列名（3）、meta 常量（5）会被整类剔除**：画师由触发词承载，IP 系列名「永不添加」。看到 `wash_report.csv` 的 `drop_why` 列里出现 `artist-tag(由触发词承载)` / `copyright-ip(永不添加)` / `meta-constant` 都是**预期行为**，不要去把它们加回来。

另外两条会看图像状态与数据集统计：

- **`watermark-removed-by-inpaint`**：这张图已被 `text` 阶段修补过 ⇒ 水印像素没了 ⇒ 槽位 7 的 `watermark`/`signature`/`logo` 不再写（指南 §8.1 决策树：能在图像层去掉就去掉）。没修补过的图照写——`watermark` 是**保留**类，不是 source noise。
- **`low-freq-character(n<4)`**：**默认不会出现**——本预设已按用户拍板取消角色名频率门槛（`wash.character_min_images = 0`，出现几次都保留）。指南 §8.2 建议设 4，若某天把配置改回 4 又看到这条理由，先确认是不是 `parse_caption`/来源标签把角色名拆开了，而不是直接调阈值。
- **`character-name(由触发词承载)`**：只在数据集类型是 `character`（`init --kind character`）时出现——角色 LoRA 的身份由触发词承载，角色名 tag 整类不写（§5.2/§8.2）。`wash` 收尾会打印「目标类型 x；整类丢弃的词典 category」一行，看到它就别奇怪角色名为什么没了。
- 散文尾巴默认不写（§3.1 纯标签串是实测成品形态），`wash` 会在结尾报「N 张 caption 里有散文尾巴未写入」。

### 四个容易误判成 bug 的行为（都在真实数据上踩过）

1. **别名先于删除**：`topless`/`smiling`/`slender`/`group shot`/`on all fours`/`hair clip`/`hairy arms`/`looking away`/`front·back·side view`/`eyes closed`/`mouth open`/`left side ponytail` 是 §7.2/§6.3 要求**换成现行形**的词，不是要删的词。引擎内部先展开别名再查删除表；如果你看到 `eyes closed` 变成 `closed eyes`、`topless` 变成 `breasts out`，那是对的。
2. **词典闸门**：除结构性噪声（否定式、`score_*`/`rating:`、裸年份、模板占位符 `(series)`、单字母碎片）外，启发式正则**只对词典不认识的字符串生效**。所以 `very long hair`、`doggystyle`（曾分别被 `^(very|…)\s+` 和 `^[a-z]+_?style$` 误杀）这类真标签会被保住——这不是漏拦。
3. **`original` 在 `category_keep` 里**：它虽是 danbooru 的 copyright 类，但不是 IP 系列名，而是"原创非二创"的事实陈述。用户既有 caption 里大量出现，所以保留；要跟随 §2.3 的严格读法就从 `category_keep` 删掉它。
4. **评级词保留**（`safe`/`sensitive`/`questionable`/`explicit`/`nsfw`/`general`）：指南 §4.3 说官方段 `[quality/meta/year/safety]`「一般不写」，但**用户已拍板保留**，所以它们不会被丢，还会被排到**触发词之后、主体之前**（官方段位置），也不进 `unknown`。别把 `sensitive`/`nsfw` 当"漏洗"或自行删掉。

**洗完只剩触发词怎么办**：`wash` 结尾会点名报警（附丢弃理由计数）。这是"画师标签由触发词承载 + IP 永不添加"的必然后果——单画师图源（yande.re 一类）的 booru 标签本来就只有画师+角色几个。正确处置是**补内容**：`anima-lora-auto-caption` 自动打标或看图补全，再重跑 `wash`（幂等），**不是**把画师/IP 标签加回来。

## §3 推进纪律

1. **每个阶段前**：`anima_status(dataset:"X")` 看数量与报告。若阶段已 done 且输入没变，不要重跑；输入变了（例如又 fetch 了一批）先 `anima_stage(stage:"init", ...)` 让 manifest 把后续阶段标失效，或直接重跑该阶段。
2. **写盘前必须 dry-run**：不带 `apply` 跑一次，把清单读给用户（尤其 `dedup`/`screen` 的剔除清单、`fetch` 的下载清单）。用户点头后再 `apply:true`。
3. **长阶段用 detach**：`fetch`（250 张原图 30~50MB/张）和 `text` 会跑很久，用 `anima_stage(..., detach:true)` 拿 runId，再 `anima_job(runId:"...")` 轮询；不要用 shell 后台跑（会被沙箱杀掉或丢日志）。
4. **阶段之间不要跨**：不要一边 fetch 一边 rename；`rename_map.csv` 是后续所有阶段的对照基准。
5. 任何异常先 `anima_doctor`，再决定是否重跑。
6. **要改 caption 就用 `fix-caption`**，不要手工改 `images/*.txt` 后重跑 `wash`——`wash` 是整条重建的（默认虽不复活你删掉的标签，但你新加的东西它也不认识）。`fix-caption` 只动你点名的那一张，且改动会进 `per_image.json` 的账本。
7. **补标签前先 `dict-check`**：`anima_stage(stage:"dict-check", dataset:"X")` 会把你 caption 里**词典查不到**和 **post_count=0（danbooru 上不存在 ⇒ 幻觉标签，指南 §7.7 禁止写）**的标签逐条列出来。真实使用里出现过模型凭印象编标签、最后靠 verify 才发现的情况——这一步能提前拦住。
8. **`anima_status` 现在会报 caption 健康度**（`tags min/avg/max`、缺 txt、孤立 txt、空 caption、<20 标签、>45 标签、触发词缺失/不在首位）**和图桶行**。低于下限的图多半是"洗完只剩触发词"，处置见上文；`图桶:` 那行告诉你数据实际躺在哪些目录（别默认是 `images/`）。
9. **淘汰图用 `exclude`，不要直接删文件**：`exclude names:"0007" reason:"user" apply:true` 会把图+同名 txt 移进 `_excluded/`、记进 `user_deleted.json`（`wash` 因此不会复活它的标签）。删错了 `undo:true` 按账本搬回**原来的桶**；上一次跑批里手删的、工具不知道的，用 `missing:true` 补账。
10. **每次写 caption 后看一眼 `wash_diff.csv`**：它是"本次 vs 上次"的逐张差异（新增/删除/清空），终端也会打印 `本次改动 N 张`。看到"清空"或大面积"删除"就先停下来问用户，别接着往下跑。
11. **`wash` 报 `A=none` 占一半以上时，先补 A 源再谈洗标**：那条告警意味着这次 wash 只做了"格式化 + 压父标签"，没有 booru 权威标签可依据。修法是 `enrich`（md5 反查）或 `fetch --source danbooru --tags <画师名>`，**都要在 `text` 修补之前做**。

## §4 必须停下来问用户的点

- 触发词定什么（`@xxx`，默认建议画师名/风格名，小写、无空格）。
- 数据集要分成几个子文件夹（`before/latest/present` 这类），以及每个的语义——这决定 `makecfg` 的 repeats 与 caption 变化因素。
- `dedup`/`screen` 的剔除清单（尤其"古老图片""草图"这两类主观项）。
- 看图补标的结果里出现与 booru 标签冲突的条目。
- **人数/关系（§8.3）**：`verify` 报 `count-conflict(solo vs 2girls)`，或一整批 caption 都没有人数标签时——人数只能看图（实测 52% 是多角色图而 booru 标签一个都没写），**不要用启发式兜底**；先按 `anima-style-lora-curate` 的「人数与关系必须看图」协议补，再重跑 `wash`+`verify`。
- rank / LR / epochs 的最终取值（`makecfg` 会给推荐值 + rationale.md，等用户确认或改写）。

## §5 训练阶段不在本流水线内

本流水线到「生成训练配置三件套」为止。开跑训练、看 loss、采样验证是下一步的事：
`cd /d E:\LoRA_Train\train_configs\<name>` 然后跑 `train_<name>.bat`，或走 anima_lora daemon。要动参数回 `anima-lora-config` skill。

## §6 常见故障

| 现象 | 处理 |
|---|---|
| `anima_doctor` 的「ML 依赖」失败 | 文字检测/修补阶段要 torch+rfdetr，**bundle 里不带 venv**。放一个 `import torch, rfdetr` 都过的解释器到 `<home>/.animasl/venv`（doctor 会打印具体命令），或把设置项 `ml_python` 指过去。装在 bundle 目录里重装即丢 |
| fetch 报代理错误 / ConnectionReset | 该源需要代理，在**设置页「网络」分组**改 `proxy_candidates`（或 bundle 的 `animasl.config.json`）；`prefer_proxy_hosts` 里的主机会优先走代理 |
| danbooru 报 403 / `Just a moment...` / 只有单标签能查 | ① 多标签查询要凭据：设置页「凭据」分组填用户名 + API key；② 403 挑战页由 **curl 兜底**自动绕过（`requests` 直发 403 属正常）；③ 若日志显示 `proxy=直连` + `ConnectTimeout`，重跑一次 |
| exhentai 报「需要 cookies.txt 或 EXHENTAI_*」 | 设置页「凭据」分组填 `ipb_member_id`/`ipb_pass_hash`/`igneous`，或 `cookies:"<cookies.txt>"`；`igneous` 过期就重新登录一次再抄 |
| caption 数 ≠ 图片数 | 先看 `anima_status` 的**图桶行**确认数据在哪个目录（不是只有 `images/`）；再查 wash 判弃或看图待补，看 wash_report.csv |
| 工具"看不见"我的图/caption（`images=0` 却有几百个 txt） | 数据在 `clean/`、`watermark/` 这类子目录里 —— 现在自动识别为图桶；仍看不到就 `anima_status` 看 `图桶:` 行、`anima_stage(..., workSet:"clean")` 显式指定 |
| `wash` 打印 `A=none` 占多数 | 这批图没有 booru 权威标签（本地导入且 `enrich` 没命中是常态）。先 `enrich` 或 `fetch --source danbooru --tags <画师名>`，再 wash；别把画师/IP 标签手工加回去 |
| `verify` 报 `conflict-hair-color(...)` / `count-conflict(...)` | 同一族出现多个取值（发色/瞳色/下着/人数）。以 booru 标签为准就用 `wash prefer:"A"`，以看图结果为准用 `prefer:"C"`；若本来是两个角色，那是人数标签缺失，先按 §8.3 看图补人数 |
| caption 里出现 `|`、`@ @` 这种碎片 | 旧版把 `:|`（14,582 帖）当标点洗坏了 —— 现在纯符号/表情标签原样保留。仍出现就 `fix-caption` 修那一条 |
| `makecfg` 报"图片数比预期多一倍" | 旧版把子目录和 `images/` 里的同一张图算两遍；现在按内容指纹跨桶去重，硬链接/副本只算一次 |
| 图片很小/被拉伸 | 检查 screen 的 min_short_side 与 makecfg 的 bucket_no_upscale；resize 不创造信息 |
| 训练 OOM | 不在本流程内，但先确认 resolution：8GB 建议 1024 |

### 这些坑已经修好了（看到旧现象先更新装机副本）

上面这些行为里有几条是**真实使用掉进去后修的**。如果你的工具还表现出旧行为，多半是装机副本没更新（`plugin_manager install_bundle` 重装 `dsh-anima-style-lora`）：

| 旧现象 | 现在的行为 |
|---|---|
| `anima_job` 报 `Error: tool "anima_job" returned invalid output: "value.command" is not a declared property` | 已修：`anima_stage` 与 `anima_job` 共用同一份返回 schema，detach 的后台结果能取回来了 |
| `init`/`thumbs` 传 `apply` 报 `unrecognized arguments: --apply`；`verify` 传 `trigger` 报错 | 已修：参数矩阵**在执行前**校验，错误里直接列出该阶段支持的参数（`init`/`thumbs` 不吃 apply，`fetch`/`import` 用 `dryRun`） |
| `rename` 跑第二批后 `rename_map.csv` 只剩新的一批，旧元数据丢了 | 已修：对照表**只追加**，每批另留 `rename_map.<a>-<b>.csv`；预演写 `rename_map.preview.csv` 不污染权威表 |
| `thumbs` 在 `rename` 之后打印「0 张」（只读 `00_raw`） | 已修：有 `images/` 就用 `images/`，已是最新的跳过，`prune:true` 清陈旧缩略图 |
| `apply-review` 之后触发词没了 | 已修：触发词从 manifest 取（`wash.resolve_trigger`），补标不再把它洗掉 |
| 手工改完 caption 重跑 `wash`，删掉的标签全回来了 | 已修：`wash` 默认不复活人工删掉的图源标签（比对 `per_image.json` 的 `merged_tags`）；整体重洗用 `refreshSource:true` |
| `import` 传数据集根目录，把 `images/`+`thumbs/` 全卷进 `00_raw` | 已修：`src` 是数据集目录本身或它的上级会被**拒绝**；数据集内的保留目录（`images`/`thumbs`/`masks`/`_pipeline`/`00_raw`/`_excluded`）不递归；非图文件报数不静默 |
| `makecfg` 把我给的 `lr 5e-05` 静默收紧成 `8e-05` | 已修：LR 超出该 rank 的建议区间**直接报错**（列出区间），要么用区间内的值，要么 `allowOutOfBand:true` 明确放行（此时**不改值**，只警告） |
| `anima_status` 只报图片数和 caption 数 | 现在还有一行 caption 健康度（标签数 min/avg/max、缺/孤立 txt、<20、>45、触发词位置 + 示例）+ 每个数据集一行（`_`/`.` 前缀目录自动跳过） |
| `anima_doctor` 180s 超时 | 现在允许 300s；排障时用 `proxies:true` 让它跑一遍「代理 × 客户端」实测矩阵（`requests` vs `curl`，含 socks5 缺依赖的说明） |
| 142 条成品 caption 放在 `clean/`+`watermark/` 里，`anima_status` 却报 `raw=0 images=0 captions=0`（工具对整批成品视而不见） | 已修：**根下任何含图的子目录都算图桶**，所有阶段默认扫全部桶，caption 与图同目录；`anima_status` 多打一行 `图桶: clean 109图/109caption、watermark 33图/33caption` |
| `wash` 全程 `A=none` 却报 ok，洗出来只是"格式化 + 压父标签" | 已修：wash 每次打印 `标签来源：A+booru N、A=none M`，A=none 占一半以上时**醒目告警**并给两条修法（`enrich` / `fetch --source danbooru --tags <画师名>`） |
| 合并只有集合层：同族冲突（`pink hair` vs `purple hair`）靠顺序定胜负 | 已修：**冲突族检测**（发色/瞳色/发长/胸围/下着/人数）进 `validate` 与 `verify`；`prefer:"A"\|"C"` 指定留哪边，裁掉的记进报告 |
| 发现标签数超上限，只能手改 `wash_rules.json` | 已修：`minTags`/`maxTags` 直接是 `wash` 的参数 |
| `refreshSource` 全有/全无（要么不复活、要么全复活） | 已修：三级 —— 默认（差集+手删名单）/ `refreshBooru:true`（只忽略手删名单）/ `refreshSource:true`（全放开） |
| `:|` 被洗成 `|`、`@_@` 被洗成 `@ @` | 已修：纯符号/表情标签白名单（`:|` `>_<` `o_o` `0_0` `@_@` `...` `♪` …）原样保留，词典里都有 |
| 水印族标签要一张张 `fix-caption` 删 | 已修：`dropMarks:true` 一次丢掉 `mark_tags` 整类（给已经清过水印的桶用） |
| 洗完看不到"这次改了什么"（得自己 diff） | 已修：写 caption 前自动快照 `captions_prev/`（留 3 份）、写完后出 `wash_diff.csv` 并打印 `本次改动 N 张（新增/删除/清空）` |
| 用户手删了 4 张图，工具下一轮只说"少了 4 张"，不知道是谁、也没找回路径 | 已修：`exclude` 阶段（移进 `_excluded/` + 记 `user_deleted.json`，`undo` 能按账本搬回原桶，`missing:true` 给历史手删补账） |
| `makecfg` 把 142 张算成 284 张（`images/` 与子目录双计） | 已修：按内容指纹跨桶去重；整桶都是副本时跳过并说明"想同时训练两个版本就改文件名，别用硬链接" |
