/**
 * anima-style-lora — 浏览器半侧：在「设置」里加一页「Anima 风格 LoRA」。
 *
 * 手写、不走打包器（与 godot-bridge 的 client/client.js 同样做法）：
 *   · 由 `window.__ModuleLoader__.load({ id: <包名>, factory })` 惰性注册；
 *   · `id` 必须等于包名；
 *   · 只能 require 平台 seed 里的模块（这里只用 react）；
 *   · 不加载任何 Harness 客户端包，控件与样式全部自己写，颜色只用宿主的
 *     `--dsw-alias-*` token（CSS 抄自 dsh-client-ui-primitives 的
 *     settings-form/*.module.css 与 dsh-better-sidebar 的分组卡配方）。
 *
 * 数据通路：本插件的宿主半侧在 `/anima-lora/api` 上自建了路由
 * （DSH 的 settings RPC 不服务第三方命名空间），四个方法 read / write /
 * probe / doctor。写入落在 `<runtimeDir>/animasl.config.json`，不需要重启。
 */
window.__ModuleLoader__.load({
  id: 'dsh-anima-style-lora',
  factory(require) {
    const React = require('react')
    const h = React.createElement
    const NS = 'animaStyleLora'
    const API = '/anima-lora/api'

    // ------------------------------------------------------------ 文案
    const zh = {
      settingsNav: 'Anima 风格 LoRA',
      intro: '这里改的是本机运行时配置，保存后立刻对 anima_* 工具生效，不需要重启。',
      fileLabel: '写入文件',
      loading: '正在读取配置…',
      loadFailed: '读不到配置：',
      retry: '重试',
      save: '保存',
      saving: '保存中…',
      reset: '恢复默认',
      resetPending: '将恢复默认',
      saved: '已保存',
      savedWithRefused: '已保存；以下键被忽略：',
      failed: '保存失败：',
      override: '本机配置',
      fromBundle: 'bundle 配置',
      fromDefaults: '内置默认',
      envOverride: '环境变量',
      exists: '存在',
      missing: '不存在',
      doctor: '跑一次体检',
      doctoring: '体检中…',
      doctorTitle: '体检输出',
      homeHint: '由插件行配置决定（profile 的 cordis.patch.yml），这里只读。',
      runtimeDirHint: '插件运行时目录（config.home 下的 .animasl）。',
      pythonHint: '留空则按 运行时 venv → 包内 venv → anima_lora venv → PATH 依次查找。',
      listHint: '一行一项，保存时去掉空行。',
      numberListHint: '用逗号或空格分隔。',
      emptyValue: '（空 = 用默认）',
      readOnly: '只读',
      none: '（无）',
      unsetHint: '清空并保存 = 删掉本机覆盖，回到默认。',
      noOverride: '未在本机配置',
      groupPaths: '路径',
      groupRuntime: '运行时与工具链',
      groupDict: '词典与模型',
      groupCreds: '凭据（图源账号）',
      groupNet: '网络',
      groupScreen: '筛选（screen）',
      groupDedup: '去重（dedup）',
      groupWash: '洗标（wash）',
      groupHygiene: '空间与备份（hygiene）',
      hintDatasets: '默认 <home>/datasets。',
      hintConfigs: '训练配置三件套的落点，默认 <home>/train_configs。',
      hintOutput: '默认 <home>/output。',
      hintAnimaLora: 'anima_lora 训练器目录（其 .venv 会被复用）。',
      hintTrainer: 'Anima-Standalone-Trainer 目录。',
      hintKoharu: 'koharu 安装目录（模型缓存来源）。',
      hintModels: 'LoRA 输出/模型目录。',
      hintTagDict: 'tags.json（danbooru 全量标签词典，约 38 万条）。',
      hintDanbooruGeneral: 'BooruDatasetTagManagerPlus 的 danbooru_dataset_general.csv。',
      hintDanbooruClassified: 'danbooru_dataset_classified.csv（可选）。',
      hintCharacterDict: '角色别名词典（可选）。',
      hintLayoutModel: '文字/拟声词/气泡检测模型；留空用内置默认。',
      hintInpaintModel: 'LaMa 修补模型；留空用内置默认。',
      hintProxy: '按顺序探测，空串=直连（一行一个）。',
      hintPreferProxy: '这些主机优先走代理。',
      hintCookies: 'cookies.txt（exhentai / pawchive 受限帖）。',
      reveal: '显示',
      conceal: '隐藏',
      hintDanbooruLogin: 'danbooru 用户名；留空则读环境变量 DANBOORU_LOGIN。',
      hintDanbooruKey: 'danbooru API key（My Account → API Key）；留空则读 DANBOORU_API_KEY。有它才能一次查 200 个帖。',
      hintExhentaiMember: 'exhentai 的 ipb_member_id（浏览器 cookie 或 cookies.txt 二选一）。',
      hintExhentaiPass: 'exhentai 的 ipb_pass_hash。',
      hintExhentaiIgneous: 'exhentai 的 igneous（登录后才有；过期就重新登一次）。',
      hintCurlUa: 'curl 兜底用的 UA，留空 = animasl/0.1 (+curl)。**别填浏览器 UA**——danbooru 的 Cloudflare 正好拿它拦。',
      hintCredNote: '凭据只存在你本机的 <runtimeDir>/animasl.config.json（不进 git）；也可以继续用下面的 cookies.txt。',
      hintShortSide: '短边小于该值的图被筛掉。',
      hintMinBytes: '体积小于该值的图被筛掉。',
      hintEarliest: '早于该日期的图被筛掉（YYYY-MM-DD）。',
      hintWarnRatio: '文字面积占比超过它就送修补（0~1）。',
      hintDropRatio: '超过它就整张丢弃（0~1）。',
      hintSketchTags: '命中这些标签的图按草图筛掉。',
      hintPhash: 'pHash 汉明距离阈值（越小越严）。',
      hintSsim: 'SSIM 阈值，≥ 它判定为同一张图（0.9~1）。',
      hintMinTags: '少于该标签数只告警。',
      hintMaxTags: '多于该标签数告警（指南建议 25~40）。',
      hintCharMin: '角色名 tag 的最低出现次数；0 = 不限制（本预设默认，按用户拍板）。',
      hintCatDrop: '整类丢弃的词典 category：1=画师 3=版权 5=meta（4=角色，仅角色 LoRA）。',
      hintCatKeep: '整类保留的 category（会覆盖丢弃表）。',
      hintKeepParent: '保留的父标签（一般不填）。',
      hintCaptionSnapshots: 'wash 写盘前保留几代 caption 快照（0 = 不留；内容与上一版完全相同就不新建）。',
      hintRenameSnapshots: '每批 rename 的对照表快照份数（权威表 rename_map.csv 是累计的，不受影响）。',
      hintMakecfgBackups: '训练配置保留几代 .bak<时分> 备份（一次 apply 写的四件套算同一代，preflight.txt 是报告不进备份；0 = 不留）。',
      hintKeepMasks: 'text 修补后保留掩膜（默认清掉本次成功用过的；失败/未修补的始终保留）。',
      hintHygieneNote: '这几个默认值刻意压到最小：反复跑批不该往磁盘堆副本。要看历史就临时调大，用完调回来。',
      booleanHint: '只能是 true / false',
      booleanOn: '已开启',
      booleanOff: '已关闭',
    }
    const en = {
      settingsNav: 'Anima style LoRA',
      intro: 'These are this machine\u2019s runtime settings. Saving applies to the anima_* tools immediately \u2014 no restart.',
      fileLabel: 'Writes to',
      loading: 'Loading configuration\u2026',
      loadFailed: 'Cannot read configuration: ',
      retry: 'Retry',
      save: 'Save',
      saving: 'Saving\u2026',
      reset: 'Reset',
      resetPending: 'will reset',
      saved: 'Saved',
      savedWithRefused: 'Saved; ignored keys: ',
      failed: 'Save failed: ',
      override: 'local',
      fromBundle: 'bundle',
      fromDefaults: 'default',
      envOverride: 'env',
      exists: 'exists',
      missing: 'missing',
      doctor: 'Run doctor',
      doctoring: 'Running\u2026',
      doctorTitle: 'Doctor output',
      homeHint: 'Owned by the plugin row in the profile cordis.patch.yml; read-only here.',
      runtimeDirHint: 'Plugin runtime directory (`.animasl` under config.home).',
      pythonHint: 'Empty = discover runtime venv \u2192 bundle venv \u2192 anima_lora venv \u2192 PATH.',
      listHint: 'One entry per line.',
      numberListHint: 'Separate with commas or spaces.',
      emptyValue: '(empty = default)',
      readOnly: 'read-only',
      none: '(none)',
      unsetHint: 'Save an empty value to drop the local override.',
      noOverride: 'not set locally',
      groupPaths: 'Paths',
      groupRuntime: 'Runtime and toolchain',
      groupDict: 'Dictionaries and models',
      groupCreds: 'Credentials (image sources)',
      groupNet: 'Network',
      groupScreen: 'Screening (screen)',
      groupDedup: 'Deduplication (dedup)',
      groupWash: 'Tag washing (wash)',
      groupHygiene: 'Disk usage & backups (hygiene)',
      hintDatasets: 'Defaults to <home>/datasets.',
      hintConfigs: 'Where the training config trio is written; defaults to <home>/train_configs.',
      hintOutput: 'Defaults to <home>/output.',
      hintAnimaLora: 'anima_lora trainer directory (its .venv is reused).',
      hintTrainer: 'Anima-Standalone-Trainer directory.',
      hintKoharu: 'koharu install directory (model cache source).',
      hintModels: 'LoRA output / model directory.',
      hintTagDict: 'tags.json (full danbooru tag dictionary, ~380k entries).',
      hintDanbooruGeneral: 'BooruDatasetTagManagerPlus danbooru_dataset_general.csv.',
      hintDanbooruClassified: 'danbooru_dataset_classified.csv (optional).',
      hintCharacterDict: 'Character alias dictionary (optional).',
      hintLayoutModel: 'Text / onomatopoeia / bubble detector; empty = built-in default.',
      hintInpaintModel: 'LaMa inpainting model; empty = built-in default.',
      hintProxy: 'Probed in order; an empty entry means direct (one per line).',
      hintPreferProxy: 'Hosts that prefer the proxy.',
      hintCookies: 'cookies.txt (exhentai / restricted pawchive posts).',
      reveal: 'Show',
      conceal: 'Hide',
      hintDanbooruLogin: 'danbooru user name; empty falls back to the DANBOORU_LOGIN env var.',
      hintDanbooruKey: 'danbooru API key (My Account \u2192 API Key); empty falls back to DANBOORU_API_KEY. Required to list 200 posts per call.',
      hintExhentaiMember: 'exhentai ipb_member_id (either this or cookies.txt).',
      hintExhentaiPass: 'exhentai ipb_pass_hash.',
      hintExhentaiIgneous: 'exhentai igneous (only present when logged in; re-login if it expired).',
      hintCurlUa: 'User agent for the curl fallback; empty = animasl/0.1 (+curl). Do NOT put a browser UA here \u2014 that is exactly what danbooru\u2019s Cloudflare blocks.',
      hintCredNote: 'Credentials live only in your local <runtimeDir>/animasl.config.json (never committed); cookies.txt below still works.',
      hintShortSide: 'Images with a shorter side below this are screened out.',
      hintMinBytes: 'Images smaller than this are screened out.',
      hintEarliest: 'Images older than this are screened out (YYYY-MM-DD).',
      hintWarnRatio: 'Text area above this goes to inpainting (0~1).',
      hintDropRatio: 'Above this the whole image is dropped (0~1).',
      hintSketchTags: 'Images matching these tags are treated as sketches.',
      hintPhash: 'pHash Hamming distance threshold (lower = stricter).',
      hintSsim: 'SSIM threshold; at or above it images count as the same (0.9~1).',
      hintMinTags: 'Warn below this tag count.',
      hintMaxTags: 'Warn above this tag count (guide suggests 25~40).',
      hintCharMin: 'Minimum occurrences for character tags; 0 = unlimited (this preset\u2019s default).',
      hintCatDrop: 'Dictionary categories dropped wholesale: 1=artist 3=copyright 5=meta (4=character for character LoRAs).',
      hintCatKeep: 'Categories kept wholesale (overrides the drop list).',
      hintKeepParent: 'Parent tags to keep (normally empty).',
      hintCaptionSnapshots: 'Caption snapshot generations kept before wash writes (0 = none; identical content creates no new snapshot).',
      hintRenameSnapshots: 'Rename-map snapshots kept per batch (the cumulative rename_map.csv is unaffected).',
      hintMakecfgBackups: 'Generations of .bak<HHMMSS> training-config backups to keep (the four files one apply writes count as one generation; preflight.txt is a report and is never backed up; 0 = none).',
      hintKeepMasks: 'Keep masks after text inpainting (the ones used successfully are removed by default; failed/unpatched masks are always kept).',
      hintHygieneNote: 'These defaults are deliberately minimal: re-running the pipeline should not pile up copies on disk. Raise them temporarily when you want history, then set them back.',
      booleanHint: 'true or false only',
      booleanOn: 'on',
      booleanOff: 'off',
    }
    const isEnglish = String((typeof navigator !== 'undefined' && navigator.language) || 'zh')
      .toLowerCase()
      .startsWith('en')
    const t = (key) => {
      const table = isEnglish ? en : zh
      return table[key] !== undefined ? table[key] : key
    }

    // ------------------------------------------------------------ 样式
    // 分组卡/行/输入/按钮的尺寸与色值抄自宿主设置面板（settings-form/*.module.css
    // 与 dsh-better-sidebar 的分组卡配方），只保留 --dsw-alias-* token 引用。
    const CSS = `
.asl-section{display:flex;flex-direction:column;gap:16px;width:100%;max-width:760px}
.asl-intro{margin:0;padding:0 2px;font-size:13px;line-height:20px;color:var(--dsw-alias-label-tertiary)}
.asl-file{margin:0;padding:0 2px;font-size:12px;line-height:18px;color:var(--dsw-alias-label-tertiary)}
.asl-file code{font-family:var(--ds-font-family-code,ui-monospace,monospace);background:var(--dsw-alias-bg-layer-2);border:1px solid var(--dsw-alias-border-l1);border-radius:6px;padding:1px 6px;color:var(--dsw-alias-label-secondary)}
.asl-group{display:flex;flex-direction:column;gap:8px;box-sizing:border-box;flex:none;padding:20px;border:1px solid var(--dsw-alias-border-l2);border-radius:16px;background:var(--dsw-alias-bg-layer-3)}
.asl-group-head{display:flex;align-items:baseline;gap:7px;padding:0 2px 6px;font-size:13px;font-weight:600;line-height:20px;color:var(--dsw-alias-label-primary)}
.asl-row{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 2px;border-bottom:1px solid var(--dsw-alias-border-l2)}
.asl-row:last-child{border-bottom:none}
.asl-row-text{display:flex;flex-direction:column;gap:4px;min-width:0;flex:1}
.asl-row-head{display:flex;align-items:center;gap:8px}
.asl-label{font-size:14px;line-height:22px;color:var(--dsw-alias-label-primary)}
.asl-badge{font-size:11px;line-height:16px;font-weight:500;padding:1px 7px;border-radius:999px;background:var(--dsw-alias-accent-soft,var(--dsw-alias-bg-layer-2));color:var(--dsw-alias-label-secondary);white-space:nowrap}
.asl-desc{margin:0;font-size:12px;line-height:18px;color:var(--dsw-alias-label-tertiary)}
.asl-invalid{margin:0;font-size:12px;line-height:17px;color:var(--dsw-alias-state-error-primary)}
.asl-control{display:flex;flex:none;align-items:center;gap:6px}
.asl-input{height:34px;box-sizing:border-box;padding:0 12px;border:0.5px solid var(--dsw-alias-border-l4);border-radius:var(--dsw-radius-md,8px);background:var(--dsw-alias-bg-layer-1);font:inherit;font-size:13px;color:var(--dsw-alias-label-primary);width:280px}
.asl-input.asl-number{width:110px}
.asl-input.asl-secret{width:280px;font-family:var(--ds-font-family-code,ui-monospace,monospace)}
.asl-eye{appearance:none;flex:none;border:1px solid var(--dsw-alias-border-l2);border-radius:var(--dsw-radius-sm,6px);padding:4px 8px;font:inherit;font-size:11px;line-height:16px;cursor:pointer;background:var(--dsw-alias-bg-layer-1);color:var(--dsw-alias-label-secondary);white-space:nowrap}
.asl-eye:hover{color:var(--dsw-alias-label-primary)}
.asl-eye:disabled{opacity:.4;cursor:default}
.asl-bool{display:flex;align-items:center;gap:7px;cursor:pointer;font-size:13px;line-height:20px;color:var(--dsw-alias-label-secondary)}
.asl-check{width:16px;height:16px;flex:none;margin:0;accent-color:var(--dsw-alias-state-business-primary);cursor:pointer}
.asl-bool-text{white-space:nowrap}
.asl-input:focus{outline:none;border-color:var(--dsw-alias-state-business-primary)}
.asl-input:disabled{opacity:.55}
.asl-input[aria-invalid=true]{border-color:var(--dsw-alias-state-error-primary)}
.asl-textarea{box-sizing:border-box;width:280px;min-height:74px;padding:6px 10px;border:0.5px solid var(--dsw-alias-border-l4);border-radius:var(--dsw-radius-md,8px);background:var(--dsw-alias-bg-layer-1);font-family:var(--ds-font-family-code,ui-monospace,monospace);font-size:12px;line-height:1.6;color:var(--dsw-alias-label-primary);resize:vertical}
.asl-reset{appearance:none;border:none;background:none;padding:0;font:inherit;font-size:12px;line-height:18px;color:var(--dsw-alias-label-secondary);cursor:pointer}
.asl-reset:hover{color:var(--dsw-alias-label-primary)}
.asl-reset:disabled{opacity:.4;cursor:default}
.asl-static{font-size:13px;line-height:20px;color:var(--dsw-alias-label-secondary);font-family:var(--ds-font-family-code,ui-monospace,monospace);word-break:break-all;text-align:right;max-width:420px}
.asl-footer{display:flex;align-items:center;gap:8px;padding-top:4px}
.asl-save{appearance:none;border:1px solid transparent;border-radius:var(--dsw-radius-md,8px);padding:5px 14px;font:inherit;font-size:13px;line-height:1.5;cursor:pointer;background:var(--dsw-alias-label-primary);color:var(--dsw-alias-bg-layer-3)}
.asl-save:disabled{opacity:.4;cursor:default}
.asl-ghost{appearance:none;border:1px solid var(--dsw-alias-border-l2);border-radius:var(--dsw-radius-md,8px);padding:5px 14px;font:inherit;font-size:13px;line-height:1.5;cursor:pointer;background:var(--dsw-alias-bg-layer-1);color:var(--dsw-alias-label-primary)}
.asl-ghost:disabled{opacity:.4;cursor:default}
.asl-message{flex:1;min-width:0;margin:0;font-size:12px;line-height:1.5;color:var(--dsw-alias-label-secondary)}
.asl-message.asl-error{color:var(--dsw-alias-state-error-primary)}
.asl-pre{margin:0;padding:12px;border:1px solid var(--dsw-alias-border-l1);border-radius:12px;background:var(--dsw-alias-bg-layer-2);font-family:var(--ds-font-family-code,ui-monospace,monospace);font-size:12px;line-height:1.6;color:var(--dsw-alias-label-secondary);white-space:pre-wrap;word-break:break-all;max-height:420px;overflow:auto}
.asl-state{margin:0;padding:0 2px;font-size:13px;line-height:20px;color:var(--dsw-alias-label-tertiary)}
@media (prefers-reduced-motion:reduce){.asl-input,.asl-textarea{transition:none}}
`

    // ------------------------------------------------------------ 字段表
    // type: text | number | list | numberList；readonly 的行只展示不写。
    const GROUPS = [
      {
        id: 'paths',
        titleKey: 'groupPaths',
        fields: [
          { key: 'home', type: 'readonly', label: 'home', hintKey: 'homeHint', fromPaths: 'home' },
          { key: 'datasets_dir', type: 'text', probe: true, hintKey: 'hintDatasets' },
          { key: 'configs_dir', type: 'text', probe: true, hintKey: 'hintConfigs' },
          { key: 'output_dir', type: 'text', probe: true, hintKey: 'hintOutput' },
        ],
      },
      {
        id: 'runtime',
        titleKey: 'groupRuntime',
        fields: [
          { key: 'runtimeDir', type: 'readonly', label: 'runtime', hintKey: 'runtimeDirHint', fromPaths: 'runtimeDir' },
          { key: 'python', type: 'text', hintKey: 'pythonHint' },
          { key: 'ml_python', type: 'text', hintKey: 'pythonHint' },
          { key: 'anima_lora_dir', type: 'text', probe: true, hintKey: 'hintAnimaLora' },
          { key: 'trainer_dir', type: 'text', probe: true, hintKey: 'hintTrainer' },
          { key: 'koharu_dir', type: 'text', probe: true, hintKey: 'hintKoharu' },
          { key: 'models_dir', type: 'text', probe: true, hintKey: 'hintModels' },
        ],
      },
      {
        id: 'dict',
        titleKey: 'groupDict',
        fields: [
          { key: 'tag_dict', type: 'text', probe: true, hintKey: 'hintTagDict' },
          { key: 'danbooru_general', type: 'text', probe: true, hintKey: 'hintDanbooruGeneral' },
          { key: 'danbooru_classified', type: 'text', probe: true, hintKey: 'hintDanbooruClassified' },
          { key: 'character_dict', type: 'text', probe: true, hintKey: 'hintCharacterDict' },
          { key: 'layout_model', type: 'text', probe: true, hintKey: 'hintLayoutModel' },
          { key: 'inpaint_model', type: 'text', probe: true, hintKey: 'hintInpaintModel' },
        ],
      },
      {
        id: 'creds',
        titleKey: 'groupCreds',
        noteKey: 'hintCredNote',
        fields: [
          { key: 'danbooru_login', type: 'text', hintKey: 'hintDanbooruLogin' },
          { key: 'danbooru_api_key', type: 'secret', hintKey: 'hintDanbooruKey' },
          { key: 'exhentai_member_id', type: 'text', hintKey: 'hintExhentaiMember' },
          { key: 'exhentai_pass_hash', type: 'secret', hintKey: 'hintExhentaiPass' },
          { key: 'exhentai_igneous', type: 'secret', hintKey: 'hintExhentaiIgneous' },
        ],
      },
      {
        id: 'net',
        titleKey: 'groupNet',
        fields: [
          { key: 'proxy_candidates', type: 'list', hintKey: 'hintProxy' },
          { key: 'prefer_proxy_hosts', type: 'list', hintKey: 'hintPreferProxy' },
          { key: 'cookies_file', type: 'text', probe: true, hintKey: 'hintCookies' },
          { key: 'curl_user_agent', type: 'text', hintKey: 'hintCurlUa' },
        ],
      },
      {
        id: 'screen',
        titleKey: 'groupScreen',
        fields: [
          { key: 'screen.min_short_side', type: 'number', min: 64, max: 8192, step: 1, hintKey: 'hintShortSide' },
          { key: 'screen.min_bytes', type: 'number', min: 0, step: 1024, hintKey: 'hintMinBytes' },
          { key: 'screen.earliest_date', type: 'text', placeholder: 'YYYY-MM-DD', hintKey: 'hintEarliest' },
          { key: 'screen.text_warn_ratio', type: 'number', min: 0, max: 1, step: 0.01, hintKey: 'hintWarnRatio' },
          { key: 'screen.text_drop_ratio', type: 'number', min: 0, max: 1, step: 0.01, hintKey: 'hintDropRatio' },
          { key: 'screen.sketch_tags', type: 'list', hintKey: 'hintSketchTags' },
        ],
      },
      {
        id: 'dedup',
        titleKey: 'groupDedup',
        fields: [
          { key: 'dedup.phash_distance', type: 'number', min: 0, max: 32, step: 1, hintKey: 'hintPhash' },
          { key: 'dedup.ssim_threshold', type: 'number', min: 0.5, max: 1, step: 0.001, hintKey: 'hintSsim' },
        ],
      },
      {
        id: 'wash',
        titleKey: 'groupWash',
        fields: [
          { key: 'wash.min_tags', type: 'number', min: 0, max: 200, step: 1, hintKey: 'hintMinTags' },
          { key: 'wash.max_tags', type: 'number', min: 1, max: 400, step: 1, hintKey: 'hintMaxTags' },
          { key: 'wash.character_min_images', type: 'number', min: 0, max: 100, step: 1, hintKey: 'hintCharMin' },
          { key: 'wash.category_drop', type: 'numberList', hintKey: 'hintCatDrop' },
          { key: 'wash.category_keep', type: 'numberList', hintKey: 'hintCatKeep' },
          { key: 'wash.keep_parent_tags', type: 'list', hintKey: 'hintKeepParent' },
        ],
      },
      {
        id: 'hygiene',
        titleKey: 'groupHygiene',
        noteKey: 'hintHygieneNote',
        fields: [
          { key: 'hygiene.caption_snapshots', type: 'number', min: 0, max: 20, step: 1, hintKey: 'hintCaptionSnapshots' },
          { key: 'hygiene.rename_snapshots', type: 'number', min: 0, max: 50, step: 1, hintKey: 'hintRenameSnapshots' },
          { key: 'hygiene.makecfg_backups', type: 'number', min: 0, max: 20, step: 1, hintKey: 'hintMakecfgBackups' },
          { key: 'hygiene.keep_masks', type: 'boolean', hintKey: 'hintKeepMasks' },
        ],
      },
    ]

    const ALL_FIELDS = GROUPS.reduce((acc, group) => acc.concat(group.fields), [])
    const FIELD_BY_KEY = ALL_FIELDS.reduce((acc, field) => {
      acc[field.key] = field
      return acc
    }, {})

    // ------------------------------------------------------------ 小工具
    function getPath(root, dotted) {
      if (root === null || typeof root !== 'object') return undefined
      let node = root
      for (const part of dotted.split('.')) {
        if (node === null || typeof node !== 'object') return undefined
        node = node[part]
      }
      return node
    }

    function hasPath(root, dotted) {
      if (root === null || typeof root !== 'object') return false
      let node = root
      for (const part of dotted.split('.')) {
        if (node === null || typeof node !== 'object' || !Object.prototype.hasOwnProperty.call(node, part)) return false
        node = node[part]
      }
      return true
    }

    function toText(value) {
      if (value === undefined || value === null) return ''
      if (Array.isArray(value)) return value.map((item) => String(item)).join('\n')
      return String(value)
    }

    /** 输入框文本 → 配置值（空串 = 清除覆盖）。 */
    function parseValue(field, text) {
      if (field.type === 'boolean') {
        const flag = text.trim().toLowerCase()
        if (flag === '') return { empty: true }
        if (flag === 'true') return { value: true }
        if (flag === 'false') return { value: false }
        return { error: t('booleanHint') }
      }
      if (field.type === 'number') {
        const trimmed = text.trim()
        if (trimmed === '') return { empty: true }
        const number = Number(trimmed)
        if (!Number.isFinite(number)) return { error: t('numberListHint') }
        if (field.min !== undefined && number < field.min) return { error: `≥ ${field.min}` }
        if (field.max !== undefined && number > field.max) return { error: `≤ ${field.max}` }
        return { value: number }
      }
      if (field.type === 'list') {
        const items = text
          .split('\n')
          .map((line) => line.trim())
          .filter((line) => line.length > 0)
        return { value: items }
      }
      if (field.type === 'numberList') {
        const items = text
          .split(/[\s,]+/)
          .map((part) => part.trim())
          .filter((part) => part.length > 0)
          .map((part) => Number(part))
        if (items.some((item) => !Number.isFinite(item))) return { error: t('numberListHint') }
        return { value: items }
      }
      const trimmed = text.trim()
      if (trimmed === '') return { empty: true }
      return { value: trimmed }
    }

    async function api(method, payload) {
      const response = await fetch(`${API}/${method}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload || {}),
      })
      let body
      try {
        body = await response.json()
      } catch {
        throw new Error(`HTTP ${response.status}`)
      }
      if (!body || body.ok !== true) {
        throw new Error((body && body.error && body.error.message) || `HTTP ${response.status}`)
      }
      return body.value
    }

    // ------------------------------------------------------------ 组件
    function StaticRow(props) {
      const { field, value } = props
      return h(
        'div',
        { className: 'asl-row' },
        h(
          'div',
          { className: 'asl-row-text' },
          h(
            'div',
            { className: 'asl-row-head' },
            h('span', { className: 'asl-label' }, field.label || field.key),
            h('span', { className: 'asl-badge' }, t('readOnly')),
          ),
          h('p', { className: 'asl-desc' }, t(field.hintKey)),
        ),
        h('div', { className: 'asl-control' }, h('span', { className: 'asl-static' }, value || t('none'))),
      )
    }

    function Field(props) {
      const { field, effective, override, dirty, onDirty, onReset, probeState, busy } = props
      const initial = dirty !== undefined ? dirty : toText(effective)
      const [text, setText] = React.useState(initial)
      const [touched, setTouched] = React.useState(false)
      const [revealed, setRevealed] = React.useState(false)
      const parsed = parseValue(field, text)
      const invalid = parsed.error !== undefined
      const multiline = field.type === 'list' || field.type === 'numberList'
      const secret = field.type === 'secret'
      const boolean = field.type === 'boolean'
      const origin = override ? t('override') : effective !== undefined ? t('fromDefaults') : t('noOverride')

      const commit = (next) => {
        setText(next)
        setTouched(true)
        onDirty(next)
      }

      return h(
        'div',
        { className: 'asl-row' },
        h(
          'div',
          { className: 'asl-row-text' },
          h(
            'div',
            { className: 'asl-row-head' },
            h('span', { className: 'asl-label' }, field.label || field.key),
            h('span', { className: 'asl-badge' }, origin),
            probeState !== undefined
              ? h(
                  'span',
                  { className: 'asl-badge' },
                  probeState ? t('exists') : t('missing'),
                )
              : null,
          ),
          h('p', { className: 'asl-desc' }, t(field.hintKey)),
          h('p', { className: 'asl-desc' }, field.key),
          invalid ? h('p', { className: 'asl-invalid' }, parsed.error) : null,
        ),
        h(
          'div',
          { className: 'asl-control' },
          boolean
            ? h(
                'label',
                { className: 'asl-bool' },
                h('input', {
                  type: 'checkbox',
                  className: 'asl-check',
                  checked: text.trim().toLowerCase() === 'true',
                  disabled: busy,
                  onChange: (event) => commit(event.target.checked ? 'true' : 'false'),
                }),
                h('span', { className: 'asl-bool-text' }, t(text.trim().toLowerCase() === 'true' ? 'booleanOn' : 'booleanOff')),
              )
            : multiline
            ? h('textarea', {
                className: 'asl-textarea',
                value: text,
                spellCheck: false,
                disabled: busy,
                'aria-invalid': invalid,
                placeholder: t(field.type === 'numberList' ? 'numberListHint' : 'listHint'),
                onInput: (event) => commit(event.target.value),
              })
            : h('input', {
                className: `asl-input${field.type === 'number' ? ' asl-number' : ''}${secret ? ' asl-secret' : ''}`,
                type: field.type === 'number' ? 'number' : secret && !revealed ? 'password' : 'text',
                value: text,
                min: field.min,
                max: field.max,
                step: field.step,
                spellCheck: false,
                autoComplete: 'off',
                disabled: busy,
                'aria-invalid': invalid,
                placeholder: field.placeholder || t('emptyValue'),
                onInput: (event) => commit(event.target.value),
              }),
          secret
            ? h(
                'button',
                {
                  type: 'button',
                  className: 'asl-eye',
                  disabled: busy,
                  onClick: () => setRevealed(!revealed),
                },
                t(revealed ? 'conceal' : 'reveal'),
              )
            : null,
          h(
            'button',
            {
              type: 'button',
              className: 'asl-reset',
              disabled: busy || !override,
              title: t('unsetHint'),
              onClick: () => {
                setTouched(false)
                setText('')
                onReset()
              },
            },
            touched && dirty === undefined ? t('resetPending') : t('reset'),
          ),
        ),
      )
    }

    function SettingsPage() {
      const [state, setState] = React.useState({ status: 'loading' })
      const [edits, setEdits] = React.useState({})
      const [unsets, setUnsets] = React.useState([])
      const [busy, setBusy] = React.useState(false)
      const [outcome, setOutcome] = React.useState(null)
      const [probe, setProbe] = React.useState({})
      const [doctor, setDoctor] = React.useState(null)

      const load = React.useCallback(async () => {
        setState({ status: 'loading' })
        try {
          const value = await api('read')
          setState({ status: 'ready', value })
          setEdits({})
          setUnsets([])
          const paths = ALL_FIELDS.filter((field) => field.probe)
            .map((field) => getPath((value.layers && value.layers.merged) || {}, field.key))
            .filter((item) => typeof item === 'string' && item.length > 0)
          if (paths.length > 0) {
            try {
              const probed = await api('probe', { paths })
              setProbe(probed.exists || {})
            } catch {
              setProbe({})
            }
          }
        } catch (error) {
          setState({ status: 'failed', error: String(error && error.message ? error.message : error) })
        }
      }, [])

      React.useEffect(() => {
        load()
      }, [load])

      if (state.status === 'loading') {
        return h('div', { className: 'asl-section' }, h('p', { className: 'asl-state' }, t('loading')))
      }
      if (state.status === 'failed') {
        return h(
          'div',
          { className: 'asl-section' },
          h('p', { className: 'asl-message asl-error' }, `${t('loadFailed')}${state.error}`),
          h(
            'div',
            { className: 'asl-footer' },
            h('button', { type: 'button', className: 'asl-ghost', onClick: load }, t('retry')),
          ),
        )
      }

      const value = state.value || {}
      const layers = value.layers || {}
      const merged = layers.merged || {}
      const runtime = layers.runtime || {}
      const envKeys = layers.env || {}
      const dirty = Object.keys(edits).length > 0 || unsets.length > 0

      const markDirty = (key, text) => {
        setOutcome(null)
        setEdits((previous) => {
          const next = { ...previous }
          next[key] = text
          return next
        })
        setUnsets((previous) => previous.filter((item) => item !== key))
      }

      const markReset = (key) => {
        setOutcome(null)
        setEdits((previous) => {
          const next = { ...previous }
          delete next[key]
          return next
        })
        setUnsets((previous) => (previous.includes(key) ? previous : previous.concat(key)))
      }

      const save = async () => {
        const set = {}
        const problems = []
        for (const [key, text] of Object.entries(edits)) {
          const field = FIELD_BY_KEY[key]
          if (!field) continue
          const parsed = parseValue(field, text)
          if (parsed.error !== undefined) {
            problems.push(`${key}: ${parsed.error}`)
            continue
          }
          if (parsed.empty) {
            if (!unsets.includes(key)) unsets.push(key)
            continue
          }
          set[key] = parsed.value
        }
        if (problems.length > 0) {
          setOutcome({ kind: 'failed', message: problems.join('；') })
          return
        }
        setBusy(true)
        setOutcome(null)
        try {
          const result = await api('write', { set, unset: unsets })
          const refused = (result && result.refused) || []
          const applied = (result && result.applied) || []
          const removed = (result && result.removed) || []
          setOutcome({
            kind: refused.length > 0 ? 'failed' : 'saved',
            message:
              refused.length > 0
                ? `${t('savedWithRefused')}${refused.join(', ')}`
                : `${t('saved')}：+${applied.length} / -${removed.length}`,
          })
          await load()
        } catch (error) {
          setOutcome({ kind: 'failed', message: String(error && error.message ? error.message : error) })
        } finally {
          setBusy(false)
        }
      }

      const runDoctor = async () => {
        setBusy(true)
        setDoctor({ status: 'running' })
        try {
          const result = await api('doctor')
          setDoctor({ status: 'done', output: result.output, exitCode: result.exitCode })
        } catch (error) {
          setDoctor({ status: 'done', output: String(error && error.message ? error.message : error), exitCode: -1 })
        } finally {
          setBusy(false)
        }
      }

      const rows = []
      rows.push(h('p', { className: 'asl-intro', key: 'intro' }, t('intro')))
      if (value.paths && value.paths.runtimeConfig) {
        rows.push(
          h(
            'p',
            { className: 'asl-file', key: 'file' },
            `${t('fileLabel')}：`,
            h('code', null, value.paths.runtimeConfig),
            value.paths.runtimeConfigExists ? '' : `（${t('missing')}）`,
          ),
        )
      }
      if (value.cliError) {
        rows.push(h('p', { className: 'asl-message asl-error', key: 'clierror' }, value.cliError))
      }

      for (const group of GROUPS) {
        const children = [
          h(
            'div',
            { className: 'asl-group-head', key: 'head' },
            h('span', null, t(group.titleKey)),
          ),
        ]
        if (group.noteKey) {
          children.push(h('p', { className: 'asl-desc', key: 'note' }, t(group.noteKey)))
        }
        for (const field of group.fields) {
          if (field.type === 'readonly') {
            const from = value.paths ? value.paths[field.fromPaths] : undefined
            children.push(h(StaticRow, { key: field.key, field, value: from }))
            continue
          }
          const effective = getPath(merged, field.key)
          const override = hasPath(runtime, field.key) || hasPath(envKeys, field.key)
          const text = getPath(merged, field.key)
          const probeState = field.probe && typeof text === 'string' && text.length > 0 ? probe[text] : undefined
          children.push(
            h(Field, {
              key: field.key,
              field,
              effective,
              override,
              dirty: edits[field.key],
              onDirty: (next) => markDirty(field.key, next),
              onReset: () => markReset(field.key),
              probeState,
              busy,
            }),
          )
        }
        rows.push(h('div', { className: 'asl-group', key: group.id }, children))
      }

      rows.push(
        h(
          'div',
          { className: 'asl-footer', key: 'footer' },
          h(
            'button',
            { type: 'button', className: 'asl-save', disabled: busy || !dirty, onClick: save },
            busy ? t('saving') : t('save'),
          ),
          h(
            'button',
            { type: 'button', className: 'asl-ghost', disabled: busy, onClick: runDoctor },
            busy ? t('doctoring') : t('doctor'),
          ),
          outcome
            ? h(
                'p',
                { className: `asl-message${outcome.kind === 'failed' ? ' asl-error' : ''}` },
                outcome.message,
              )
            : h('p', { className: 'asl-message' }, dirty ? `${Object.keys(edits).length} / ${unsets.length}` : ''),
        ),
      )

      if (doctor) {
        rows.push(
          h(
            'div',
            { className: 'asl-group', key: 'doctor' },
            h(
              'div',
              { className: 'asl-group-head' },
              h('span', null, t('doctorTitle')),
              doctor.status === 'done' ? h('span', { className: 'asl-badge' }, `exit ${doctor.exitCode}`) : null,
            ),
            h('pre', { className: 'asl-pre' }, doctor.status === 'running' ? t('doctoring') : doctor.output || ''),
          ),
        )
      }

      return h('div', { className: 'asl-section' }, rows)
    }

    // ------------------------------------------------------------ 注册
    return {
      // 只依赖 slots（设置页槽位）。locale 是可选增强：取不到就用内置中英文案。
      inject: ['slots'],
      apply(ctx) {
        // 文案：优先注册进宿主的 locale 服务；API 形态在不同版本有差异，
        // 注册失败也不影响渲染（组件用自己的 t()）。
        // 注意：locale 没有出现在 inject 里，cordis 4 的 ctx 代理对未声明服务
        // 的任何属性访问都会抛 "cannot get property ... without inject" ——
        // 连 `if (!ctx.locale)` 这种判空都活不过去，所以探测必须整体包 try/catch。
        ctx.effect(() => {
          try {
            if (!ctx.locale || typeof ctx.locale.register !== 'function') return () => {}
            return ctx.locale.register(NS, { zh, en })
          } catch {
            return () => {}
          }
        }, 'anima-style-lora: dictionaries')

        ctx.effect(() => {
          const style = document.createElement('style')
          style.setAttribute('data-plugin', 'dsh-anima-style-lora')
          style.textContent = CSS
          document.head.appendChild(style)
          return () => {
            style.remove()
          }
        }, 'anima-style-lora: settings styles')

        ctx.slots.inject('settings.section', () =>
          ctx.slots.register(
            {
              name: 'settings.section',
              id: 'anima-style-lora',
              order: 100,
              label: () => t('settingsNav'),
            },
            SettingsPage,
          ),
        )
      },
    }
  },
})
