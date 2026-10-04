---
name: anima-style-lora-pipeline
description: Anima 风格 LoRA 数据集流水线的总纲：阶段顺序与推进条件、anima_status/anima_stage/anima_job/anima_doctor 四个工具的用法、每阶段该产出什么文件、哪里必须停下来问用户、失败与续跑怎么处理。用户要求"做一个 Anima 风格 LoRA 数据集""跑 anima 流水线""从下载到训练配置走一遍"时使用；单点问题（只下载、只洗标）直接看对应的专项 skill。
---

# SKILL：Anima 风格 LoRA 数据集流水线

目标产物：`<home>/datasets/<name>/` 下一套筛干净的图片 + 合规 caption，外加 `train_configs/<name>/` 的训练配置三件套。

## §1 目录契约

```
<home>/datasets/<name>/
  00_raw/            下载/导入的原始图（只读，不动）
  images/            进入流水线的图 + 同名 .txt caption（0001.png + 0001.txt）
  _pipeline/
    manifest.json    阶段状态（每个 stage: pending/preview/done/stale/failed + 时间戳）
    rename_map.csv   新名↔旧名↔post_id↔tags_full 对照表（洗标的事实来源）
    dedup_report.csv / screen_report.csv / wash_report.csv / review_todo.csv / wash_verify.csv
    thumbs/          ≤1536px 缩略图（视觉子代理唯一允许看的输入）
    wash_rules.json  可选，数据集级洗标规则覆盖
  _excluded/         被剔除的图（duplicates/<reason>），永不删
```

`<home>` 默认 `E:/LoRA_Train`，可用插件配置或 `--home` 覆盖。

## §2 阶段表

| 阶段 | 工具调用 | 产出 | apply 后状态 |
|---|---|---|---|
| init | `anima_stage(stage:"init", dataset:"X")` | 目录骨架 + manifest | done |
| fetch | `anima_stage(stage:"fetch", dataset:"X", source:"yandere", tags:"artist_name", limit:250)` | `00_raw/*` + raw_posts.jsonl | done |
| import | `anima_stage(stage:"import", dataset:"X", src:"D:/下载/xxx")` | `00_raw/*`（zip/rar/7z 自动解） | done |
| dedup | `anima_stage(stage:"dedup", dataset:"X")` → `apply:true` | dedup_report.csv | done |
| screen | `anima_stage(stage:"screen", dataset:"X")` → 视觉复核 → `apply:true` | screen_report.csv + review_queue.csv | done |
| thumbs | `anima_stage(stage:"thumbs", dataset:"X")` | `_pipeline/thumbs/*.jpg` | done |
| text | `anima_stage(stage:"text", dataset:"X")` → `apply:true` | 修补后的图 + text_report.csv | done |
| rename | `anima_stage(stage:"rename", dataset:"X", apply:true)` | `images/0001.ext` + rename_map.csv | done |
| wash | `anima_stage(stage:"wash", dataset:"X", trigger:"@xxx")` → `apply:true` | `images/NNNN.txt` + wash_report.csv | done |
| review-list | `anima_stage(stage:"review-list", dataset:"X")` | review_todo.csv（看图清单） | — |
| apply-review | `anima_stage(stage:"apply-review", dataset:"X", payload:"...json", apply:true)` | 回填后的 caption | done |
| verify | `anima_stage(stage:"verify", dataset:"X")` / `online:true` | wash_verify.csv | done |
| makecfg | `anima_stage(stage:"makecfg", dataset:"X", kind:"style", trigger:"@xxx", subdirs:"before,latest,present")` | `train_configs/X/` 三件套 + rationale.md | done |

顺序不是死板的：`text` 必须在 `rename` 前（rename 后文件名与 raw_posts.jsonl 的对应关系会断，text 报告就不好回溯）；`wash` 必须在 `rename` 后（caption 文件名跟新编号走）；`thumb` 可以在 `screen` 前先跑一遍用于人工看。

**dry-run 不算完成**：没写盘的那一跑在 manifest 里记成 `preview`，`anima_status` 里显示 `▷`，`done=[…]` 里也不会出现它。看到 `▷` 就表示「报告看过了、还没落地」，下一步是带 `apply:true` 重跑同一阶段。`↻ stale` 表示上游改过（例如 `text` 修补了图），这条链上的洗标/配置都要重跑。

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
| `anima_doctor` 说找不到 ML 解释器 | 文字检测阶段需要 torch+rfdetr 的 venv；先用 `anima_stage(stage:"text", ...)` 的报错确认，装到 `<home>/.animasl/venv` 或 `anima_lora/.venv` |
| fetch 报代理错误 / ConnectionReset | 该源需要代理，改 `proxy_candidates`（bundle 的 `animasl.config.json` 或 `<home>/.animasl/animasl.config.json`） |
| caption 数 ≠ 图片数 | wash 只对 `images/` 下的图写 caption；缺的那些多半是 wash 判弃或看图待补，看 wash_report.csv |
| 图片很小/被拉伸 | 检查 screen 的 min_short_side 与 makecfg 的 bucket_no_upscale；resize 不创造信息 |
| 训练 OOM | 不在本流程内，但先确认 resolution：8GB 建议 1024 |
