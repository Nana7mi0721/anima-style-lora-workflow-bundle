---
name: anima-style-lora-source
description: Anima 风格 LoRA 训练集的下载来源指南：yande.re / danbooru / pawchive.pw / exhentai 四个源的 API 用法与标签语义、认证方式（设置页「凭据」分组的 danbooru 用户名+API key / exhentai 三项，或 cookies.txt）、danbooru 的 Cloudflare 403 与 curl 兜底、必须走代理的域名、父子投稿去重、原图选择、文件名标签不完整等坑，以及按画师类型选源的判断。用户要求"下载数据集/抓某画师的图/从 danbooru 或 yande.re 下载/补充素材"时使用。
---

# SKILL：训练集下载来源

## §0 先判断该用哪个源

| 画师类型 | 首选 | 理由 |
|---|---|---|
| 在 danbooru/yande.re 有 tag 的常见画师 | **yande.re**（图更大更全，无需认证）→ 不够再 danbooru | 两站标签体系同源，yande.re 原图多为 PNG |
| 只在 danbooru 有、yande.re 收录少 | **danbooru** | 需要 API key 才能多标签查询 |
| Patreon / Fanbox / Fantia / Discord 画师（不进 booru） | **pawchive.pw** | kemono 风格聚合站，公开 API |
| 同人本 / CG 集 / 游戏立绘 | **exhentai** | 整本下载，但需登录态 |
| 多个源混用 | yande.re + danbooru 打底，pawchive 补差分 | 注意跨源去重（dedup 阶段会抓 md5/phash） |

**跨源混用是常态**，但要在 `wash` 阶段把「同一张图不同来源的标签」合并——`rename_map.csv` 里保留了每张图的 `source` 与 `tags_full`。

## §1 统一调用方式

```
anima_stage(stage:"fetch", dataset:"X", source:"yandere", tags:"<画师tag>", limit:250, dryRun:true)   # 只看清单
anima_stage(stage:"fetch", dataset:"X", source:"yandere", tags:"<画师tag>", limit:250)               # 真的下载（默认行为）
```

**`fetch` 没有 `apply`**：默认就是下载，预演用 `dryRun:true`（`import` 同理）。传了 `apply` 会在执行前被拒，错误里会列出该阶段支持的参数。长阶段（250 张原图）用 `detach:true` 拿 runId 再 `anima_job` 轮询。

产出行落在 `00_raw/`，元数据落 `_pipeline/raw_posts.jsonl`（每行一条候选：source/post_id/page_url/url/filename/ext/bytes/width/height/created_at/rating/tags[]/parent_id/md5/status）。**这批 `tags[]` 就是后面 wash 阶段的 booru 骨架**，丢了就得重新抓（`rename_map.csv` 里也有 `tags_full` 快照，但那是抓取当时的样子，别当权威来源）。

## §2 yande.re

- 接口：`GET https://yande.re/post.json?tags=<tag>&limit=100&page=N`，无需认证。
- 标签：画师用 `artist_name` 的**下划线形式**（`asabu202`、`miyase_mahiro`）。可多标签（空格分隔），但收窄到只剩目标画师就够。
- **父子投稿必须按 `created_at` 去重，不能用 id 排序**——子帖 id 可能小于父帖。工具的 `_yande_families` 已做并查集家族归并，保留最新一幅。
- 原图优先 PNG（画师投稿常见 30~50MB/张）；250 张 ≈ 8GB ≈ 45 分钟，务必 detach 跑。
- 代理：直连常可用；被 reset 时走 `127.0.0.1:7897`（旧的 7890 未必在监听）。工具的 proxy 候选表是 `[""（直连）, 7897, 7890, 10809, 1080]`，会自动探测。
- **坑（多次踩过）**：yande.re 的**文件名里的标签不完整**（实测 250 张里 146 张文件名的 tag 少于 API 返回，43 张连画师 tag 都没有）。**洗标只能用 `raw_posts.jsonl`/`rename_map.csv` 里的 `tags_full`，绝对不要用文件名解析标签。**
- **坑（决定后续工序）**：yande.re 的 `tags_full` **语义稀疏** —— 按画师抓下来的帖子，标签常常只有 `<画师>`＋偶尔一个角色名。洗标会按指南把这些**整类丢掉**（画师标签由触发词承载、IP 系列名永不添加）⇒ **caption 洗完可能只剩触发词**（实测 10 张里 3 张洗空，`too-few-tags(0)`）。所以 yande.re 抓完**必须**再接一道补内容工序：`anima-lora-auto-caption`（自动打标）或看图补全，然后重跑 `wash`（幂等）。yande.re 的价值在「按画师精确找图 + 原图质量」，不在标签；标签基准要靠 danbooru 或自动打标补。
- 想知道某张图洗完还剩多少，看 `_pipeline/wash_report.csv` 的 `tags_out` / `drop_why` / `unknown` 三列；`wash` 收尾的两个 `!` 报警（无来源标签 / 洗完只剩触发词）就是这条链的体检报告。
- **写 `raw_posts.jsonl` 时标签必须是 danbooru 下划线原形**（`hakurei_reimu`，不是 `hakurei reimu`）：下游 `rename` 会把它们空格拼进 CSV，`wash` 再 `.split()` 还原 —— 空格形多词标签会被切成两个词，而且**不报错**，只是 caption 里平白多出 `hakurei`、`reimu` 两条词典查不到的垃圾。（工具自己的 fetcher 已经是这个格式；手写补充元数据时注意。）

## §3 danbooru

- 接口：`GET https://danbooru.donmai.us/posts.json?tags=<tag>&limit=200&page=N`。
- 认证：**设置页 →「Anima 风格 LoRA」→「凭据」分组**填用户名 + API key（写进 `<home>/.animasl/animasl.config.json`）；也可以继续用环境变量 `DANBOORU_LOGIN` + `DANBOORU_API_KEY`。优先级：**设置页 > 环境变量**。**匿名只能查单标签且限速极严**，多标签查询必须带认证。
- 标签：同样下划线形式；`artist_name` 之外常用 `order:score` 之类过滤，但不要用它筛掉低分图（风格 LoRA 要的是风格覆盖，不是人气）。
- 页面 URL 拼 `https://danbooru.donmai.us/posts/<id>`，写进 `rename_map.csv` 的 `post_id`，方便人工回查。
- 免费账号有每日上限；limit 一次 200，工具已做 429/Retry-After 退避。
- **Cloudflare 与 curl 兜底（别误判成"凭据没配"）**：danbooru 的拦截规则 ≈「Chrome UA + 非浏览器 TLS 指纹 = 机器人」，`requests` 直发**一律 403** `<title>Just a moment...</title>`。工具在识别到挑战页后会自动改用 **curl 子进程**重放（代理 / `-u login:key` / Referer / 按主机过滤的 cookie 都跟着转），curl 侧用自报家门的 UA（`curl_user_agent`，默认 `animasl/0.1 (+curl)`）。⇒ **千万别把浏览器 UA 填进 `curl_user_agent`**，那反而必被拦。`anima_doctor` 里那行 `danbooru.donmai.us HTTP 403` 是挑战页，不代表 API 不可用。直连 danbooru 是连不上的（GFW `ConnectTimeout`），它和 `cdn.donmai.us` 都在 `prefer_proxy_hosts` 里；若某次日志显示 `proxy=直连` 并 `ConnectTimeout`，**先重跑一次**（一次性抖动）。

## §4 pawchive.pw

**这是 kemono 风格的聚合站（Patreon / Fanbox / Fantia / Discord 归档），公开 JSON API 不需要过 Cloudflare 盾。**

- 接口：`GET https://pawchive.pw/api/v1/posts?o=0`（50 条/页）、`GET /api/v1/creators`、`GET /api/v1/{service}/user/{creator_id}?q=&o=`、`GET /api/v1/{service}/user/{creator_id}/profile`、`POST /api/v1/{service}/user/{creator_id}/post/{post_id}`。
- 调用：`anima_stage(stage:"fetch", dataset:"X", source:"pawchive", creator:"<creator_id>", service:"patreon", limit:200, titleFilter:"<可选子串>")`。
- 帖子结构：`{id, user, service, title, substring, published, file:{name,node,path}, attachments:[…], has_full}`；`path` 形如 `/64/06/<sha>.png`。
- **原图 URL = `https://n{node}.pawchive.pw/data{path}`**，例如 node=2 → `n2.pawchive.pw`。缩略图是 `https://img.pawchive.pw/thumbnail/data{path}`（**别下缩略图**）。
- **必须走代理**：`n*.pawchive.pw` 直连会被 `ConnectionResetError(10054)` 重置，实测经 `http://127.0.0.1:7897` 才拿到完整 PNG。`prefer_proxy_hosts` 已包含 pawchive 域。
- 请求头：`Referer: https://pawchive.pw/` + 浏览器 UA。主域若要 `cf_clearance`，用 `cookies:"<cookies.txt>"` 传 Netscape 格式 cookie（该站与 UA 绑定，工具会统一用固定 UA）。
- 跳过 `.psd` / `.zip` / 视频（工具内建）；差分图会被当独立帖子下载，交给 dedup 处理。
- creator_id 怎么找：站内搜索画师名 → URL 里 `/patreon/user/<id>`。也可以先 `web_search` 拿名字再让用户确认。

## §5 exhentai

- 需要登录态：**设置页「凭据」分组**填 `ipb_member_id` / `ipb_pass_hash` / `igneous`（优先级最高），或环境变量 `EXHENTAI_MEMBER_ID` / `EXHENTAI_PASS_HASH` / `EXHENTAI_IGNEOUS`，或 `cookies:"<cookies.txt>"`。
- 一个 `cookies.txt` 里可以同时放 exhentai 与 pawchive 的 cookie：工具按 **domain 列**区分（`TRUE` = 域级、补前导点；`FALSE` = Host-Only），不会串站。
- 调用：`anima_stage(stage:"fetch", dataset:"X", source:"exhentai", gallery:"https://exhentai.org/g/<gid>/<token>/", limit:200)`。
- 流程：`POST https://exhentai.org/api.php`（`{"method":"gdata","gidlist":[[gid,token]],"namespace":1}`）取元数据 → 翻 `/g/<gid>/<token>/?p=N` 收集 `/s/<pagekey>/<gid>-<n>` → 逐页解析 `<img id="img" src>` 拿原图。
- **注意**：这条链路只做到「可调用」，没有在真实 cookie 下端到端验证过。第一次用先 `limit:5` 跑一遍，确认拿到的是原图分辨率而不是 `-thumb`。失败就直接让用户给 cookies.txt，别硬猜。
- 同人本整本都是同一风格的连续页，**图画文字（拟声词、对白）比例高**，`text` 阶段的检测修补要认真做；纯对白页可以考虑直接剔除。

## §6 代理配置

配置优先级：命令行 > 环境变量 `ANIMASL_*` > `<home>/.animasl/animasl.config.json`（**设置页写的就是这一层**）> bundle 的 `animasl.config.json`。

```json
{
  "proxy_candidates": ["", "http://127.0.0.1:7897", "http://127.0.0.1:7890", "socks5://127.0.0.1:10809", "socks5://127.0.0.1:1080"],
  "prefer_proxy_hosts": ["pawchive.pw", "exhentai.org", "e-hentai.org", "n2.pawchive.pw", "danbooru.donmai.us", "cdn.donmai.us"],
  "curl_user_agent": ""
}
```

- 探测顺序：候选表**直连优先**；`prefer_proxy_hosts` 命中的主机把带协议的候选提前、直连降为最后一档。命中判据是 `status_code < 500`（所以 Cloudflare 的 403 也算"这个出口通"）。
- 实测本机只有 `http://127.0.0.1:7897` 可用：直连 danbooru `ConnectTimeout`、`7890` `ProxyError`、`socks5://…` `InvalidSchema`（没装 pysocks）。
- `curl_user_agent`：curl 兜底用的 UA，**留空/自报家门就行，别填浏览器 UA**（见 §3）。
- 注意：本机 shell 里的 `HTTPS_PROXY`（曾是 `127.0.0.1:1590`）是**坏的**，外网一律 `CONNECT 502`。工具的 session 一律 `trust_env=False`，只看上面的候选表。用户换代理端口就在**设置页「网络」分组**改，别改代码。

## §7 下载后第一件事

`anima_status(dataset:"X")` 看 `raw` 数量与 `00_raw` 体积，然后立刻跑 `import`/`dedup`——先把重复和坏图清掉再谈别的。跨源混抓一定会带进大量重复（同图在 yande.re 与 danbooru 各一份，pawchive 的差分图）。
