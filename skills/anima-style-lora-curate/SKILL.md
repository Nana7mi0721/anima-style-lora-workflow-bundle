---
name: anima-style-lora-curate
description: Anima 风格 LoRA 训练集的筛选与清理指南：导入素材的 src 红线、md5/pHash/SSIM 三级去重阈值、"低质量/过老/草图/非完整制作"的判定规则与视觉复核协议、图片文字与拟声词的检测（RF-DETR-Seg）与修补（LaMa-manga）流程、koharu GUI 半自动回退、单张改 caption（fix-caption）与查标签是否真实存在（dict-check）、剔除清单的写法。用户要求"筛选数据集/去重/删掉低质量或草图/去掉图里的文字/清理训练集/改某张图的标签/查这个标签有没有"时使用。
---

# SKILL：数据集筛选与清理

## §0 三条原则

1. **只移动，不删除**。所有被剔除的图移到 `_excluded/<reason>/`，并写 `_EXCLUDED_MANIFEST.json`（记录原文件名、来源、命中规则、判定依据）。用户复核后要能一键找回；**人工淘汰走 `exclude` 阶段**（同时记进 `user_deleted.json`，`undo` 能搬回原桶），不要自己 `rm`。
2. **主观项必须给用户看清单再动手**。"古老""草图"是判断而非事实，dry-run 的清单是给用户拍板的，不是给自己看的。
3. **风格 LoRA 要覆盖不要纯净**。同一画师的早期/近期/不同题材都要留（这正是子文件夹 `before/latest/present` 的意义）；剔的是「不是完整作品」和「有文字噪声」，不是「画得一般」。

## §0.5 导入（import）的 src 红线

```
anima_stage(stage:"import", dataset:"X", src:"D:/下载/某画师")      # 默认就导入（不是 apply）
anima_stage(stage:"import", dataset:"X", src:"D:/下载/某画师", dryRun:true)   # 只看清单
```

- **`src` 只能是素材目录或压缩包**。传数据集目录本身、或它的上级目录，会被**直接拒绝**——真实使用里传过一次数据集根，结果 `images/`+`thumbs/`+`masks/` 共 139 个文件被卷进 `00_raw`，只能手工捞回来。已经卷进去的：看 `_excluded/_import_overflow/`，或按文件名把非素材图从 `00_raw` 移走。
- 递归时会跳过**数据集内**的保留目录（`00_raw` / `images` / `thumbs` / `masks` / `_pipeline` / `_excluded`）；数据集**外面**的同名目录不跳（那是正常的素材目录）。
- 非图文件（zip 之外的 mp4/psd 等）不再静默：会打印「跳过非图 N 个」并列前几个扩展名，也不写进 `raw_posts.jsonl`。
- `fetch` 同样是「默认就下载」，预演用 `dryRun:true`；`import`/`fetch` 都**没有 `apply`**。
- **导入的图没有 booru 标签**（`import` 只写「从哪来」这行元数据），而 `wash` 的来源 A 全靠 `raw_posts.jsonl` 里的 `tags`。所以素材是本地图/Pixiv/压缩包时，导入后补一步 `enrich`——按文件 md5 去 danbooru 反查原帖：

```
anima_stage(stage:"enrich", dataset:"X")              # dry-run 只查不写（但**仍会发请求**），先看命中率
anima_stage(stage:"enrich", dataset:"X", apply:true)  # 权威标签补进 raw_posts.jsonl + enrich_report.csv
```

  md5 不中时还会用**文件名里的 pixiv illust id** 再兜一次（`<id>_p<页>` 命名，`noPixiv:true` 可关掉）：只认「原文件名同名同页」或「该作品只有一帖且本图是 p0」，挑不出同一页就不认——多页作品尺寸往往完全一样，硬贴会把 p0 的标签安到 p1 上，**错标签比没标签更糟**。
  **别期待高命中率**：实测 redash 的 72 张图 md5 只中 3 张（全是当初从 danbooru 抓下来的 `.jpg`），43 张查不到；200 张 pixiv 原图里能解析出 id 的 46 张只有 6 张在 danbooru 有帖，其中 3 张还是同作品不同页（被拒）。**只发 pixiv 的画师，A 源天生稀薄，B 源（看图）才是主力**——这不是流程出错。`enrich_report.csv` 的 `by` 列写清每张靠什么命中的。
  **要在 `text` 修补之前跑**（修补改像素 ⇒ md5 变），命中率低先怀疑「修补过 / 裁剪重编码过 / 原图没上传」。反查回来的是权威全集（45~65 条），可能超过 §9 的 45 条上限，`enrich` 会把超限张数报出来，按 §2.2 槽位优先级用 `fix-caption` 往下删。

## §1 去重（dedup）

```
anima_stage(stage:"dedup", dataset:"X")                    # dry-run，出 dedup_report.csv
anima_stage(stage:"dedup", dataset:"X", apply:true)        # 移入 _excluded/duplicates/
```

三级判据，逐级从严：

| 级别 | 判据 | 默认阈值 | 说明 |
|---|---|---|---|
| 1 | 文件 md5 相同 | — | 同图重复下载（跨源常见） |
| 2 | pHash 汉明距离 | ≤ 4 | 缩放/重压/加水印的同一张图 |
| 3 | SSIM 复核 | ≥ 0.97 | 对 pHash 命中的候选再算一次，避免不同图撞 pHash |

保留策略：**分辨率×文件体积最大者**（同图里最大那张保留原样，其余剔除）。分组用并查集，一组保留一张。

跨源重复要从 `raw_posts.jsonl` 的 `source` 看；如果用户想让某源优先（例如 yande.re 的 PNG 比 danbooru 的 JPG 好），在报告里指出冲突组，让用户定。

## §2 规则筛选（screen）

```
anima_stage(stage:"screen", dataset:"X")             # dry-run：screen_report.csv + review_queue.csv
anima_stage(stage:"screen", dataset:"X", apply:true) # 移入 _excluded/<reason>/
```

默认规则（都在 `animasl.config.json` 的 `screen` 段里，可调）：

| 规则 | 默认值 | 命中后 |
|---|---|---|
| `min_short_side` | 512 px | 剔除（太小，学不到线条） |
| `min_bytes` | 100 KB | 剔除（多为低质量重压） |
| `earliest_date` | 2015-01-01 | 剔除（画风与训练目标差太远；**要先和用户确认**，老画师的老图可能是风格核心） |
| `sketch_tags` | sketch / rough / lineart / unfinished / character sheet / monochrome / greyscale / sketchbook / traditional media / wip / colorized / partially colored / sketch page / reference sheet / chibi | 进 `review_queue.csv`，**交视觉复核后**再剔 |

注意：`monochrome`/`greyscale` 在黑白线稿画师身上是正常产出，不是缺陷——命中 `sketch_tags` 的图**一律先复核**，不要直接 apply。

### 视觉复核协议（硬约束）

- **主代理永不读图**。看图必须：`anima_stage(stage:"thumbs", dataset:"X")` 生成 ≤1536px 缩略图 → **一次一个子代理、一个子代理只看一张图**，用 Read 自己的视觉读 `_pipeline/thumbs/*.jpg`。
- 原图 30~50MB 会被供应商**静默拒绝**（不是报错，是直接不返回内容），所以必须先缩略图。
- 提问必须是**结构化槽位填空**，不能开放式描述。草图复核用这几个字段：
  ```
  1) 完成度：完成作品 / 草图线稿 / 局部未完成
  2) 画面主体：单人 / 多人 / 无人物（纯风景或纯物件）
  3) 是否含大段文字：无 / 少量签名水印 / 大段对白或拟声词
  4) 是否含角色设定表（多视角同一角色）
  5) 一句话依据（≤20 字）
  ```
- 判定"草图线稿"要能说出依据（大量未上色、辅助线可见、比例未修正）；说不出来就保留。

#### 人数与关系必须看图（指南 §8.3）

实测某 250 张数据集里 **131 张（52%）是多角色图**，而原始 booru 标签**一个大人数标签都没有** ⇒ **一律推 `1girl` 会把一半以上的图判错**，且**启发式不自动兜底**（视觉回报才是权威）。所以 `review-list` 会为没有人数标签的 caption 生成一条复核项，问的就是槽位 1：

```
1) 人数：单人 / 2girls / 3girls+ / 无人物（纯风景）
2) 性别构成：1girl / 1boy / 2girls+1boy / 只露局部（只见手臂或阴茎 → faceless male）
3) 关系：solo / solo focus / hetero / yuri / multiple girls / group sex
4) 一句话依据（≤20 字）
```

三条易错：① `solo` 与 `solo focus` **互斥**——画面确实只有一人写 `solo`，多人但镜头聚焦一人写 `solo focus`（`verify` 会报 `count-conflict`）；② 有 `sex` 却无 `penis` 时**仍算男性在场**（男性只露衣袖/未入镜），单看标签会误判成 solo；③ **分镜 ≠ 人数**：同一角色的多视图/三面图按**角色个数**算人数，多视图写 `multiple views`，不要把分镜数写成 `3girls`。

## §3 文字检测与修补（text）

目标：去掉图里的**大段文字/对白/拟声词**，同时保留签名与画面本身。

```
anima_stage(stage:"text", dataset:"X")                        # dry-run：只检测，出面积占比报告
anima_stage(stage:"text", dataset:"X", apply:true)             # 检测 + LaMa 修补，覆盖原图
```

管线：**RF-DETR-Seg（mayocream koharu-layout-rfdetr-seg-2xl-1152）检测 → 掩膜膨胀 → LaMa-manga 修补**。模型是 koharu 自带的权重（`<koharu_dir>/store/hugging-face/models/`），插件直接复用，不需要启动 koharu。

- 检测类别：`text` / `onomatopoeia` / `bubble` / `panel`；推荐阈值 text 0.25、onomatopoeia 0.2、bubble 0.5、panel 0.5。
- **默认只把 text + onomatopoeia 计入修补掩膜**；`panel`（分格线）与 `bubble`（对白框）要看情况——整页漫画才需要，单张插画修补掉分格线会毁构图。
- 面积占比决策（可调）：
  - `> 8%`（`text_warn_ratio`）→ 送修补；
  - `> 30%`（`text_drop_ratio`）→ 打印警告，交用户决定是否剔除（通常是漫画页/设定表）；
  - 修补后要复检一次（修补区域是否留下糊斑）。
- **修补后 caption 必须重洗**：自动打标工具描述的是修补前的原图，文字相关的 tag/描述全部作废。流程上 `text` 排在 `rename`/`wash` 之前就是为了这个。

### koharu 回退（半自动）

koharu 0.83.1 是 **GUI-only**（CLI 只有 `-h/-V`，旧的 `--port 7331 --headless` 启动方式在该版本已失效，没有 HTTP API / MCP）。所以：

- 自动管线失败（模型加载不了、显存不够、结果明显不可用）时，回退：让用户手动打开 `D:\Program\koharu\koharu.exe`，在 GUI 里对失败的那批图跑「检测 + 修补」，导出结果。
- skill 负责：**准备输入**（把待处理图/掩膜清单整理好，给出明确的操作步骤）、**回收校验**（对返回值重跑检测，确认文字面积占比已降到阈值下、分辨率与原图一致）。
- 不要试图脚本化 koharu：它的资产存在内容寻址的 `.khrproj` 里，没有对外接口。

## §3.5 改 caption 与查标签（fix-caption / dict-check）

看图补标之后、或者你觉得某个标签写错了，**不要手工编辑 `images/*.txt`**——`wash` 是整条重建的，你的改动会被冲掉（真实使用里为此刻意绕开插件、自己写了 `_trim2.py`/`_add_vision.py` 三个脚本）。

用 `fix-caption` 点名单张图增量修：

```
anima_stage(stage:"fix-caption", dataset:"X", name:"0007", remove:"solo")                      # dry-run 看新旧对照
anima_stage(stage:"fix-caption", dataset:"X", name:"0007", add:"multiple views", apply:true)   # 补一个标签
anima_stage(stage:"fix-caption", dataset:"X", name:"0007", set:"@artist, 1girl, ...", apply:true)  # 整条替换
```

- 三个参数至少给一个；`set` 优先（给了它 `add`/`remove` 会被忽略）。
- 改完仍会过一遍洗标 + 形态校验（§9 闸门）；`remove` 掉的标签会记进 `per_image.json` 的人工删除名单，**之后重跑 `wash` 也不会自己回来**。
- 图名可以用 `0007`、`0007.jpg`、或重排前的旧文件名（会按 `per_image.json` 的 `old_names` 找）。

**补标签前先查词典**（这条是"模型编造标签"的根治手段）：

```
anima_stage(stage:"dict-check", dataset:"X")                 # 检查现有 caption 里所有标签
anima_stage(stage:"dict-check", tags:"kimono, obi, wide sleeves")   # 查你打算写的几个词（不用 dataset）
```

输出分四类：`✓ 存在(名字/类别/出现帖数)`、`↪ 别名可归一`（词典里没有这个写法，但洗标引擎的别名表会把它换成现行形，照写没问题）、`! post_count=0`（danbooru 上**没有这张图** ⇒ 按指南 §7.7 属幻觉标签，**禁止写**）、`! 词典里没有`（拼错或已不是现行形，会给形近候选；多词标签被拆开时会直说"整条不存在，但每个词单独存在"）。从 `--dataset` 收集时每个问题标签还会带上"出现在几张图里"，最普遍的问题排在最前。

实测过的那三条（用户问过）：`see through` → 归一成 **`transparent`**（danbooru 把 see-through 并进了它，词典里只剩 `see-through_hat` 这类派生）；`fate` → 归一成 **`fate (series)`**，随后被"IP 系列名永不添加"整类丢掉（正解：IP 根本不该写）；`erect nipples` → **不存在这种标签**，归一成 `nipples` 保留可见特征。**注意 `--tags` 只按逗号切**，多词标签请照 caption 的空格形整条写（`amiya (arknights)` 而不是 `amiya`+`(arknights)`）。

## §3.6 图桶与人工淘汰（workSet / exclude）

**图桶**：数据集根下**任何含图的子目录**都算一个数据桶（`images/` 只是默认那个）。真实使用里出现过 `clean/`（已修水印 109 张）+ `watermark/`（未修 33 张）、而 `images/` 是空的布局——那时工具只认 `images/`，142 条成品 caption 全部"不存在"。现在所有阶段默认扫全部桶、caption 与图**同目录**：

```
anima_stage(stage:"screen", dataset:"X", includeImages:true, workSet:"clean,watermark")   # 只看这两个桶
anima_stage(stage:"verify", dataset:"X", workSet:"clean")
```

- 桶名给错会**报错并列出自动发现的桶**（不会静默处理 0 张）。
- `anima_status` 会打一行 `图桶: clean 109图/109caption、watermark 33图/33caption` —— 判断"工具到底看见了什么"先看这行。
- `makecfg` 会自动发现桶并按内容指纹去重：硬链接/副本只算一次，整桶都是副本时跳过并提示"想同时训练两个版本就改文件名"。

**人工淘汰**：想删掉某几张图（不是规则命中，而是你自己判断），**不要直接删文件**——用 `exclude`：

```
anima_stage(stage:"exclude", dataset:"X")                                   # 列出手删清单
anima_stage(stage:"exclude", dataset:"X", names:"0007,0012", reason:"user", apply:true)
anima_stage(stage:"exclude", dataset:"X", undo:true, apply:true)            # 反悔：按账本搬回**原来的桶**
anima_stage(stage:"exclude", dataset:"X", missing:true, apply:true)         # 给"上次直接删掉的"补账
```

- 图 + 同名 `.txt` 一起移进 `_excluded/<reason>/`，并记进 `_pipeline/user_deleted.json`（`wash` 因此不会复活它带的标签）。
- 上一次会话里已经手删、工具不知道的图，用 `missing:true` 补一条记录（`archived` 空，只留证据）——**复盘时才知道删的是谁**。
- `undo` 不带 `names` 就是"把清单里所有还归档着的搬回来"。

## §4 不设内容边界

- 用户已明确拍板：**内容不参与筛选**。NSFW、`loli` 等标签都不构成剔除理由，不要为内容写 reason、不要把图挪进 `_excluded/`。
- 剔除只看技术维度：重复（md5/pHash+SSIM）、低质量、过老、草图/非完整制作、文字占比过高。
- 如果用户之后改主意，改的应该是这一节，而不是在跑批时临时加规则。

## §5 剔除清单格式

`_excluded/_EXCLUDED_MANIFEST.json` 至少含：`{original, new_name, post_id, source, reason, rule, detail, decided_by}`，其中 `decided_by` 是 `rule`（纯规则）/`vision`（视觉复核）/`user`（人工）。`screen_report.csv` 与 `review_queue.csv` 保留在 `_pipeline/` 里作为过程记录。

交付给用户的复核清单要能一眼看懂：按 reason 分组、每组给数量和 3~5 个文件名示例、总量与剩余量对照。
