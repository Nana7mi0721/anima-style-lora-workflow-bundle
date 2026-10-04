# dsh-anima-style-lora

DSH 插件 + skill + agent 预设：**Anima 风格 LoRA 全流程**（下载 → 导入 → 去重筛选 → 文字检测修补 → 重排编号 → 打标洗标 → 训练配置三件套）。

设计分工：**重活是宿主插件（确定性 Python 工具箱），判断是 skill。**
训练本身不在本流水线范围内（用 `Anima-Standalone-Trainer` 自己跑）。

## 装了什么

| 组件 | 作用 |
|---|---|
| 宿主插件 `dsh-anima-style-lora` | 4 个工具：`anima_status` / `anima_stage` / `anima_job` / `anima_doctor`，插在插件树根层级，对所有 agent 可见 |
| agent 预设 `anima-style-lora`（显示名「Anima风格LoRA全流程」） | 带专属 persona 的 agent 组合；`skill-filesystem` 通过 `customSkillDirs` 挂载本包的 `skills/` |
| 3 个 skill | `anima-style-lora-pipeline`（总纲）/ `-source`（选源下载）/ `-curate`（筛选 + 文字修补 + 视觉复核） |
| Python 工具箱 `python/animasl/` | 确定性实现，可脱离 DSH 直接用 CLI 跑：`python -m animasl.cli --home E:/LoRA_Train <stage>` |

## 安装

安装走 GitHub（与其他自研插件一致），在 DSH 里执行：

```bash
# 在 DSH 里（需要 danger-full-access 或批准）
dsh plugin --profile desktop add github:Nana7mi0721/anima-style-lora-workflow-bundle
```

也可以用桌面端 设置 → 插件 的图形界面安装。安装后新建一个 agent，预设选「Anima风格LoRA全流程」。

源码工作区在 `E:\Study\github program\for dsh\anima style lora workflow bundle`，
只用于修改源码；改动后 commit + push 到 GitHub，再用
`dsh plugin --profile desktop update dsh-anima-style-lora` 更新安装副本。
详见 `E:\Study\github program\for dsh\AGENTS.md`。

### ⚠️ 装完必须重启 DSH

**DSH 只在启动时建立模块解析表**（asar 里的 `installRuntimeInterception` / `ResolutionRouter`：
`if (parent === void 0 || !router.hasInterceptionLayerForUrl(parent)) return native(...)`）。
运行期新装的 bundle 不在表里 → 它的 `import '@deepseek-ai/dsh-tools'` 退回原生解析 →
`ERR_MODULE_NOT_FOUND` → loader 只报一句 `failed to import`、cordis fiber 从未创建。
已实测：**一个只有 6 行的最小对照 bundle 也是同样的报错**，所以这不是本包的问题。

```
dsh: warning: 1 entry did not activate
dsh-anima-style-lora (dsh-anima-style-lora): failed to import
```

看到上面这句就重启桌面端；重启后 `anima_status` / `anima_stage` / `anima_job` / `anima_doctor`
四个工具与预设「Anima风格LoRA全流程」才会出现。**更新已装 bundle 的 JS 之后同理要重启**
（模块代际缓存在 boot 期）。

### 本地自测（不需要 DSH）

```bash
cd "E:/Study/github program/for dsh/anima style lora workflow bundle"
npm test              # 契约 + 四个工具真跑一遍（只读/干跑）
npm run test:live     # 额外跑 text 干跑（慢，要 torch）
npm run test:client   # 设置页浏览器半侧：假 React 渲染 + 断言发出的请求
npm run test:route    # 设置页宿主路由：真起 http server + 真 Python CLI（含信任栅栏）
npm run test:route:doctor  # 上面那条再加一次真 doctor（慢约 1 分钟）
npm run test:all      # check:patch + client + route + smoke
```

`test/smoke.mjs` 把插件装进一个假 ctx（`test/stub/` 用 ESM loader 钩子把
`@deepseek-ai/dsh-tools` 指到替身），校验注册数量、返回值无 `undefined`、
返回值满足自己声明的 `output.schema`、`render()` 有非空正文、disposer 能卸干净。
它抓到过三个真 bug：`require()` 用在 ESM 里导致报告行数恒为 0、`stageOf()` 认不出
无 `--dataset` 的 doctor、`collect()` 把报告数组塞进声明为 string 的字段。

`test/client-smoke.mjs` 用一个极简 React 替身（本机没有 react，它由 web shell
的平台模块表提供）把设置页真的渲染出来，再模拟输入与点击，断言发出去的 HTTP
请求（只写被编辑的键、保存后重读、体检输出上屏）。`test/settings-route.mjs`
用真的 `http` server 加真的 Python CLI 跑通 read/write/probe/doctor，并逐条
验证信任栅栏（非 loopback Host、跨站 Origin、`sec-fetch-site: cross-site`、
超长 body 413）。

### 设置页（图形界面改配置）

设置 → 左侧导航「Anima 风格 LoRA」。它**不是**另存一份配置，而是直接读写
`<home>/.animasl/animasl.config.json`（运行时层），下一次 `anima_*` 调用即生效，
不用重启：

- 分组：`paths` / `runtime` / `dict` / `net` / `screen` / `dedup` / `wash`，
  每个键都标出来源（`bundle` / `defaults` / 本机覆盖），改动过的键可以一键
  「恢复默认」（= 从 runtime 层删掉这个键，回落到 bundle/内置默认）。
- 只读两行：`home`（由 profile 的 `cordis.patch.yml` 行配置决定）与运行时目录。
  路径类字段旁边有「存在 / 不存在」标记，点「跑一次体检」能在页面里看
  `anima_doctor` 的完整输出。
- 通路：宿主半侧 `lib/settings.js` 在 `/anima-lora/api` 上注册 prefix 路由
  （`read` / `write` / `probe` / `doctor`），浏览器半侧 `lib/client.js` 手写、
  只 require 平台 seed 里的 `react`，样式只用宿主的 `--dsw-*` token，
  所以明暗主题都跟着走。写盘是「临时文件 + rename」的原子替换。
- 为什么不用 DSH 自己的设置 RPC：它不给第三方命名空间提供配置读写
  （`dsh-better-sidebar` 的源码注释也是这么写的），第三方插件必须自建路由。

### skills 挂载路径

`cordis.patch.yml` 里预设的 `skill-filesystem` 行用 `customSkillDirs` 挂载本包的 `skills/`。
`customSkillDirs` 只认绝对路径（skill-filesystem 用 `resolve(root)` 解析，相对路径按
host 进程 CWD 展开），所以写的是 profile 安装副本的固定位置：

```yaml
- name: "@deepseek-ai/dsh-skill-filesystem"
  config: { customSkillDirs: ["C:/Users/REISEN/.dsh/profiles/desktop/node_modules/dsh-anima-style-lora/skills"] }
```

安装副本由 pnpm 管理，插件更新后该路径依然有效；源码工作区挪到哪都不影响。
若 DSH 主目录不是 `%USERPROFILE%\.dsh`，才需要改这一行。

## 配置

三级合并，后者覆盖前者：bundle 内 `animasl.config.json` → `<runtime>/animasl.config.json` → 环境变量 `ANIMASL_*`。
`<runtime>` 默认 `<home>/.animasl`（重资源如 ML venv 建议放这里，不要放包内）。

插件的 `config`（在 profile 的 `cordis.patch.yml` 里改）：`home` / `pythonDir` / `python` / `mlPython` / `runtimeDir` / `animaLoraDir` / `timeoutMs` / `longTimeoutMs`。

## 十三个阶段

| stage | 工具参数要点 | 产出 |
|---|---|---|
| `init` | `--trigger @xx` | `_pipeline/manifest.json` |
| `fetch` | `source=yandere\|danbooru\|pawchive\|exhentai`、`tags`/`creator`/`gallery`、`limit`、`cookies` | `00_raw/` + `raw_posts.jsonl` |
| `import` | `src=<目录>`、`move` | 解包/收编进 `00_raw/` |
| `dedup` | `phashDistance`(4)、`ssim`(0.97) | `dedup_report.csv`，`apply` 时移入 `_excluded/duplicates/` |
| `screen` | `minShortSide`(512)、`minBytes`(102400)、`earliest`(2015-01-01) | `screen_report.csv` + `review_queue.csv` |
| `thumbs` | `maxSide`(1536) | `_pipeline/thumbs/`（视觉子代理只能看这个） |
| `text` | `warnRatio`(0.08)、`dropRatio`(0.30)、`patchSmall`、`thresholds`、`dilate`(6) | `masks/`、`text_report.csv`；修补后自动把 `wash` 标 stale |
| `rename` | `start`(1)、`digits`(4) | `images/0001.ext` + `rename_map.csv` |
| `wash` | `trigger`、`rules` | `images/NNNN.txt` + `wash_report.csv` + `review_todo.csv` |
| `review-list` / `apply-review` | `payload` | 看图必答字段的往返 |
| `verify` | `online` | `wash_verify.csv`；§3 形态 + §9 内容闸门（BANNED 残留、否定式、质量词、人数一致性、图-txt 配对） |
| `makecfg` | `kind`(style/character/object/scene/clothing)、`trigger`、`name`、`subdirs`、`dim`、`lr`、`epochs`、`resolution` | 三件套 + `rationale.md` |

**除了 `init` 之外，所有写盘动作默认 dry-run，必须 `apply: true`。**

**dry-run 不会把阶段标成 done**：没写盘的那一跑在 `manifest.json` 里记成 `preview`，`anima_status` 显示 `▷`，`done=[…]` 里也不会出现它——所以「报告看过了但还没落地」和「已经做完了」不会被混为一谈。`anima_status` 的状态行图例：`✔` 已完成、`▷` 只跑过 dry-run、`↻` 上游改过需重跑、`✘` 失败、`·` 未跑。

**重复 `init` 不会毁掉进度**：数据集已存在时 `init` 只打印提示就返回；`init --force` 是**就地更新**触发词/类型（阶段记录全部保留），只有 `init --reset --yes` 才会丢掉阶段记录从零重建——`--reset` 不带 `--yes` 会先把「当前已完成：init, wash」列出来让你确认。类型（`--kind`）决定 wash 的整类规则，中途改类型是安全的：`--force` 改完从 `wash` 起重跑即可。

## 文字检测 + 修补（koharu 能力的复刻）

koharu 0.83.1 是 GUI-only（CLI 是空的 Cli{}，无 HTTP/MCP），所以直接复用它下载好的两个权重：

- **检测** `mayocream/koharu-layout-rfdetr-seg-2xl-1152`（RF-DETR-Seg-2XL，4 类 text/onomatopoeia/bubble/panel）→ `animasl/ml/textdet.py`
- **修补** `mayocream/lama-manga`（FFCResNetGenerator large，50.9M 参数）→ `animasl/ml/inpaint.py`

实测（RTX 5060 8GB，见 `_probe/textrm_test/`、`_probe/lama_test/`）：

- 掩膜外像素**逐位不变**；分辨率恒定；修复后复检 `verify_ratio` 实测 0.0000。
- 耗时：780×1200 → 10s；5130×7350 → 29s（分块 48 块）；修补本身 1~4s。
- **必须分块**：`max(W,H) > 1440` 时单次缩放到 1152 会把文字彻底丢掉且不报错（5130×7350 单次模式 text=0、分块 text=11）。`--mode auto` 已自动处理。
- 真实效果（人工核对 diff）：半透明水印的"+"、"×"、斜向光条被干净抹除并融入背景；气泡对白/拟声词消失、线稿像素未动。
- **已知局限**：字体式淡水印召回是**部分**的（`学習…無断転載禁止`、平铺的 `sample` 字样在默认阈值下抓不全，其字符得分仅 0.06~0.19）。想更激进地清水印就调低阈值：
  `anima_stage(stage="text", thresholds="text=0.15,onomatopoeia=0.12", patchSmall=true, apply=true)`，代价是误检率上升（修补是**不可逆**的擦除操作）。
- 修补前原图备份在 `_pipeline/orig_text/`；JPEG 以 quality=95、4:4:4 重编码，不引入二次压缩劣化。

## 洗标（零 token 规则引擎）

`animasl/wash.py` 内置了打标指南 §3 格式硬约束、§7 全部规范化规则（59 条别名/废弃对照含 §6.3 口语形、颜色降级、蕴含折叠、否定式拦截、描述词白名单）与 §4 七槽位排序：

`@触发词, [1主体]人数+关系+角色名 → [2外观着装] → [3姿势动作] → [4场景] → [5光影] → [6构图] → [7水印区]`

引擎只对**看图必答字段**（人数/关系/构图/体位细节）产出 `review_todo.csv`，交给视觉子代理（一次一个、一张图、只读缩略图）回填，再走同一个引擎重排。

**判据是「有没有固定视觉对应物」，不是出现频率**（指南 §2.3/§2.4）。据此分三类处理：

| 处理 | 内容 | 依据 |
|---|---|---|
| **整类丢弃**（按词典 category） | `artist`(1) 画师/社团标签（由触发词承载）、`copyright`(3) IP 系列名（"永不添加"）、`meta`(5) 无视觉对应物的常量 | §2.3 |
| **按图像状态决定** | 槽位 7 的 `watermark`/`signature`/`logo`/颜色水印/`speech bubble`/`sound effects`：**去字工具修补过的图不再写**（那片像素已经没了，再写就是幻觉），没修补的照写 | §8.1 决策树 |
| **数据集内频次** | **默认不限制**（`wash.character_min_images` = 0，按用户拍板取消；出现几次都保留）。指南 §8.2 建议设 4（只保留本数据集出现 ≥4 次的角色，理由是低频角色学不会、只会把画风打散成噪声）——要跟随指南把该值改成 4 即可，`wash` 会打印 `[wash] 角色阈值 §8.2：…` | §8.2 |
| **按训练目标（`init --kind`）** | `character` 类型额外整类丢 `character`(4)：角色名由触发词承载（§5.2「角色名 tag 与触发词二选一」、§8.2「角色 LoRA 相反：不写角色名」）；其余类型保留全部角色名（不再看频次） | §5.2 / §8.2 |

- `watermark` 属于**保留**的一类：它「有固定像素对应」，标了模型才把那片像素归因到 `watermark` 而不是画风（§2.4）。社区主流把它当 source noise 删除，本指南的前提是「图去不掉」——去字工具能去掉的图走决策树第一支。
- 散文尾巴默认**不写**（§3.1：实测成品是纯标签串，混排自然语言"可选，非默认"）。来源 caption 里若有散文、而你确实想留，设 `keep_natural_language=true`；保留时按官方形态用 `. `（句点+空格）过渡、最多 3 句、且不复述标签。
- 散文尾巴同样过规则闸门：`ender_lilies_quietus_of_the_knights` 这种 ≥6 词的长名字会被 `parse_caption` 判成"散文"，不拦的话被丢掉的 IP 系列名会从散文路径原样回来（实测踩过）。
- `wash` 可重复跑且**幂等**（来源 C 就是自己上一版输出，逐字节一致）；`_pipeline/wash_rules.json` 或配置文件的 `wash` 段可逐条覆盖规则（tag 清单是**追加**语义，`category_drop` 这类策略开关是**整体替换**语义，设 `[]` 即关闭）。

### 三条不许踩的边界（都在真实数据上踩过）

| 边界 | 规则 | 踩过的坑 |
|---|---|---|
| **别名先于删除** | `wash_tags` 内部先展开 `aliases`，再查 `drop_exact` | 曾经 `topless`/`smiling`/`slender`/`group shot`/`on all fours`/`hair clip`/`hairy arms`/`looking away`/`front·back·side view`/`left side ponytail`/`eyes closed`/`mouth open` 同时躺在 `drop_exact` 和 `aliases` 里，而删除检查在别名之前 ⇒ **别名全是死代码**，§7.2 要求"换成现行形"的标签被静默丢弃（`looking at viewer` 在 63 张真实 caption 里被误杀 58 次） |
| **词典认识就不许被启发式杀** | `drop_regex` 除结构性噪声（否定式、`score_*`/`rating:`、裸年份）外，一律**加词典闸门**：`is_negative()` 只在词典不认识该字符串时才用启发式正则 | `^(very\|really\|extremely)\s+` 杀掉了 `very long hair`（105 万帖）、`^[a-z]+_?style$` 杀掉了 `doggystyle`（"dogg"+"ystyle"）——都是真标签，只在真实数据审计里才暴露 |
| **口语形→现行形，不是丢弃** | §6.3 的自动打标口语形全部进 `aliases`：`eyes closed→closed eyes`、`mouth open→open mouth`、`from front`/`front view→straight-on`、`back view→from behind`、`side view→from side`、`left side ponytail→side ponytail`、`twin tails→twintails` | 这些是"要修的词形"，不是"要删的词"；词典里查无同义形的（`see through` 18 次、`fate` 8 次、`erect nipples` 1 次）**只报告不猜**，见下 |

- **`original` 是 `category_keep` 里的例外**：danbooru 把它归在 copyright 类，但它不是 IP 系列名而是"原创非二创"的事实陈述；用户自己的 63 张 caption 里 52 张写了它。要跟随 §2.3 的严格读法，把它从 `category_keep` 里删掉即可。
- **评级词保留**（`safe`/`sensitive`/`questionable`/`explicit`/`nsfw`/`general` + `rating safe` 这类空格形）：指南 §4.3 的官方段 `[quality/meta/year/safety]`「一般不写」，但用户既有数据集整族带评级词，**用户已明确拍板保留**，所以洗标不动它们。它们在 `classify()` 里走独立的 `rating_tags` 表 ⇒ 排在**触发词之后、槽位 1 之前**（就是官方段的位置），并且不会因为「词典查不到」被记进 `unknown`。要恢复 §4.3 的读法，在 `_pipeline/wash_rules.json` 的 `drop_exact_extra` 里列出它们即可。冒号形 `rating:safe` 与 `score_*` 仍按结构性噪声丢弃（不是标签形态，`wash.py` 的 `drop_regex`）。
- **槽位 2 = 外观着装 + 表情 + 视线，槽位 5 = 光影色调**（§4.1/§4.2，指南明确"旧规则曾把视线放槽位 3，已更正为归槽位 2"）：`looking *` 在 danbooru 里归"构图"组（l1=构图）、`sunlight` 归"背景"组，照 l1 走会分别掉进槽位 6 / 槽位 4 ⇒ `classify()` 里把表情/视线与光影词尾的判定**排在 l1 之前**（光影用**词尾**匹配，避免 `light blue hair` 被误判）。
- **关系标签归槽位 1**（§8.3「关系标签同归槽位 1」）：`solo focus`/`faceless male`/`bisexual`/`group sex`/`threesome`/`ffm threesome`/`orgy`/`multiple views` 单列成 `relation_tags`，判定排在 `framing_tags`/`sex_tags`/l1 之前——照 l1 走 `solo focus` 会掉进槽位 6、`group sex` 掉进 3、`bisexual`（词典无此词）掉进 0 排到触发词前后。同理把 §8.4 里 l1=身体/表情的性描写词（`cumdrip`/`cum on breasts`/`squirting`/`clitoris`/`large insertion`/`after sex` 等）补进 `sex_tags` 钉在槽位 3。
- 词典里没有、但同义现行形存在的写法走别名：`bunny girl→playboy bunny`、`gluteal fold→gluteal sulcus`、`aftersex→after sex`。查无同义形的留在 `wash_report.csv` 的 `unknown` 列里报给用户（`verify` 也会列出来），要压制就在 `_pipeline/wash_rules.json` 里加 `alias_extra`。
- **实测效果**（`datasets/arata/latest/` 的 63 张真实 caption，评级词保留之后重测）：标签数中位 **49 → 43**（区间 27~62）；丢弃项只有四类可解释的 —— `implied-by-child` 335（§7.4 折叠）、`banned` 19（占位符 `(series)` 8、单字母碎片 `o`/`t` 8、`artist revision` 1 等）、`duplicate` 19、`copyright-ip` 13（`blue archive`/`fate (series)`/`indie virtual youtuber`）。unknown 收敛到 3 种（`see through` 18、`fate` 8、`erect nipples` 1）。

### 标签来源链（断了不会报错，只会让 caption 只剩触发词）

```
fetch  →  _pipeline/raw_posts.jsonl      每个下载文件的 post_id / tags / created_at / 宽高
rename →  _pipeline/rename_map.csv       new_name → old_name + post 元数据 + tags_full
wash   →  caption 的来源 A（booru 标签）+ 来源 C（同名 .txt sidecar）
```

- `screen` 从 `raw_posts.jsonl` 读 `created_at` 判"过老"，`dedup` 读宽高决定保留哪张，`rename` 按 `created_at` 排序编号。
- **标签一律用 danbooru 下划线原形**（`hakurei_reimu`、`long_hair`），不是空格形：`raw_posts.jsonl` 的 `tags` 是 list（边界无歧义，`wash` 优先读它）；`rename_map.csv` 只能存字符串，`rename` 会写 `tags_full` = 下划线形空格拼接。历史上这里有个坑——CSV 里存空格形多词标签，下游 `.split()` 会把它切成 `hakurei` + `reimu` 两个词，且**不会报错**。
- 本地 `import` 进来的图只有"从哪来"这一行元数据（`source=import`），**没有 booru 标签**：要么自带 `.txt`，要么先用 `anima-lora-auto-caption` 打标再洗。
- `wash` 有**两个点名报警器**：① "既无来源标签也无既有 caption"（来源链断了）；② "caption 洗完只剩触发词"（来源标签**有**数据，但被整类规则丢光——单画师图源的典型症状，附丢弃理由计数）。`verify` 的 `too-few-tags` 是第三道闸。

`verify` 是**交付前的验收闸门**，按指南 §9 分两层：**形态层**（单行、全小写、无下划线形、` , ` 分隔、无前导逗号、无重复 token、标签数 20~45、触发词唯一且置首、BOM/尾换行/尾标点）与**内容层**（BANNED 令牌零残留＝画师/IP/meta/质量词/文字族/否定式/该换现行形的别名、人数一致性 = `solo` 与 `2girls`/`1boy`/`solo focus` 互斥、图-txt 配对完整含孤儿 `.txt`）。内容层是给"人工编辑过、或别的打标工具写的"caption 兜底的：`wash` 跑完再手改，画师/IP/质量词就会悄悄回来。`--online` 再用 danbooru `search[name_comma]` 复核"词典里没有"的标签是否真实存在（每批 120）。实测在用户的 63 张既有 caption 上查出 23 种残留（`explicit`×30、`sensitive`×17、`nsfw`×15、`blue archive`×9、`(series)`×8、`pantsu→panties`/`garter→garter belt`/`swimsuits→swimsuit` 等别名形）。

## 训练配置三件套

`makecfg` 依据用户自己的《Anima_LoRA_调参指南》决策表：Rank–LR 联动（风格 100+ 张 → dim 32 / lr 5e-5~8e-5）、曝光量算 steps、差异化 repeat、显存档。

生成物严格不含 Anima 无效参数（`noise_offset`、`min_snr_gamma`），`shuffle_caption` 恒 false（`cache_text_encoder_outputs=true` 时 trainer 强制要求），`network_module` 恒 `networks.lora_anima`。默认 `resolution=[1280,1280]`（与既有 11 个可跑配置一致；8GB OOM 时退 1024）。

## 目录契约

```
datasets/<name>/
  00_raw/            下载/导入的原始文件
  images/            重排编号后的工作集（图片 + 同名 .txt caption）
  _pipeline/         manifest.json、*_report.csv、masks/、thumbs/、orig_text/、rename_map.csv
  _excluded/         剔除物（按 reason 分子目录）+ _EXCLUDED_MANIFEST.json 证据链
```

**只移动不删除。** 每条剔除都记 `_EXCLUDED_MANIFEST.json`：`{original, reason, rule, detail, decided_by}`。

## 环境要求

- DSH ≥ 0.2.0-rc.1（`@deepseek-ai/dsh-tools` peer）
- Python 3.13 venv，装 `torch(cu13x) + torchvision + rfdetr==1.7.0 + pillow + numpy`（`python/.venv`，或用 `mlPython` 指过去）
- 词典：`tags.json` + `danbooru_dataset_general.csv`（合并后 383,196 条）
- 下载：`DANBOORU_LOGIN`/`DANBOORU_API_KEY`；pawchive/exhentai 需要 cookie，且 **pawchive CDN 必须走代理**（直连被 reset）

跑 `anima_doctor` 可以一次性体检上述每一项（含下面这张表的"凭据配没配"）。

### 各图源需要什么账号

| 图源 | 要账号吗 | 怎么配 | 不配会怎样 |
|---|---|---|---|
| **yande.re** | **不需要** | 什么都不用 | —（公开 `post.json`，代理只在被 reset 时才用） |
| **danbooru** | 单标签不用，**多标签要用** | `DANBOORU_LOGIN` + `DANBOORU_API_KEY`（用户名 + 个人设置页的 API key） | 匿名只能查**单标签**且限速极严。`anima_doctor` 里那行 `danbooru.donmai.us HTTP 403` 是首页拒绝匿名 UA，**不代表 API 不可用** |
| **pawchive.pw** | 公开接口不用 | 受限帖才需要：`cookies` 参数或 `cookies_file` 配置（Netscape `cookies.txt`） | 公开帖照抓；受限帖拿不到。**CDN 域名必须走代理**（直连被 reset） |
| **exhentai** | **必须** | `EXHENTAI_MEMBER_ID` / `EXHENTAI_PASS_HASH` / `EXHENTAI_IGNEOUS`，或 `cookies` 指向 `cookies.txt` | 直接报 `[fetch] exhentai 需要 cookies.txt 或 EXHENTAI_* 环境变量`。这条链路只做到"可调用"，第一次用先 `limit:5` 验证拿到的是原图而不是 `-thumb` |
| **gelbooru** | — | **未实现**（当前只支持上面四个源） | `source` 传 `gelbooru` 会报未知来源 |

凭据一律走环境变量或 `cookies.txt`，**不写进配置文件、不进仓库**；`anima_doctor` 只报告"配没配"，不打印密钥内容。
