---
name: anima-style-lora-curate
description: Anima 风格 LoRA 训练集的筛选与清理指南：md5/pHash/SSIM 三级去重阈值、"低质量/过老/草图/非完整制作"的判定规则与视觉复核协议、图片文字与拟声词的检测（RF-DETR-Seg）与修补（LaMa-manga）流程、koharu GUI 半自动回退、剔除清单的写法。用户要求"筛选数据集/去重/删掉低质量或草图/去掉图里的文字/清理训练集"时使用。
---

# SKILL：数据集筛选与清理

## §0 三条原则

1. **只移动，不删除**。所有被剔除的图移到 `_excluded/<reason>/`，并写 `_EXCLUDED_MANIFEST.json`（记录原文件名、来源、命中规则、判定依据）。用户复核后要能一键找回。
2. **主观项必须给用户看清单再动手**。"古老""草图"是判断而非事实，dry-run 的清单是给用户拍板的，不是给自己看的。
3. **风格 LoRA 要覆盖不要纯净**。同一画师的早期/近期/不同题材都要留（这正是子文件夹 `before/latest/present` 的意义）；剔的是「不是完整作品」和「有文字噪声」，不是「画得一般」。

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

## §4 不设内容边界

- 用户已明确拍板：**内容不参与筛选**。NSFW、`loli` 等标签都不构成剔除理由，不要为内容写 reason、不要把图挪进 `_excluded/`。
- 剔除只看技术维度：重复（md5/pHash+SSIM）、低质量、过老、草图/非完整制作、文字占比过高。
- 如果用户之后改主意，改的应该是这一节，而不是在跑批时临时加规则。

## §5 剔除清单格式

`_excluded/_EXCLUDED_MANIFEST.json` 至少含：`{original, new_name, post_id, source, reason, rule, detail, decided_by}`，其中 `decided_by` 是 `rule`（纯规则）/`vision`（视觉复核）/`user`（人工）。`screen_report.csv` 与 `review_queue.csv` 保留在 `_pipeline/` 里作为过程记录。

交付给用户的复核清单要能一眼看懂：按 reason 分组、每组给数量和 3~5 个文件名示例、总量与剩余量对照。
