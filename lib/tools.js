/**
 * 工具层：把 animasl 工具箱暴露成 4 个宿主工具。
 *
 * anima_status  看某个数据集/全部数据集的进度与产出（含 caption 健康度）
 * anima_stage   跑一个流水线阶段（默认 dry-run，写盘要 apply:true）
 * anima_job     续取后台阶段（detach:true / wait:false 启动的）结果
 * anima_doctor  环境自检（解释器 / 权重 / 词典 / 代理 / 凭据）
 *
 * 参数矩阵在 run.js 的 STAGE_PARAMS 里，**执行前**校验：阶段不支持的参数直接报错并列出
 * 该阶段支持的参数，而不是把参数丢掉再报 `unrecognized arguments`。
 * 返回结构里有 undefined 会被 tool runtime 拒绝，统一过 prune()。
 */

import { defineTool } from '@deepseek-ai/dsh-tools'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { RUN_OUTPUT_PROPERTIES, STAGE_PARAMS, buildArgs, collect, datasetFacts, lookup, listRuns, reportLines, resolveMlPython, resolvePython, runLines, stageParamError, start } from './run.js'

const STAGES = [
  'init',
  'fetch',
  'import',
  'enrich',
  'dedup',
  'screen',
  'thumbs',
  'text',
  'rename',
  'wash',
  'review-list',
  'apply-review',
  'fix-caption',
  'verify',
  'dict-check',
  'makecfg',
]

const STAGE_DOC = {
  init: '建立数据集骨架目录与 manifest；数据集已存在时不会重建（force=true 就地改触发词/类型并保留阶段记录）。**不接受 apply** —— 写盘是它的默认行为',
  fetch: '从 yandere / danbooru / pawchive / exhentai 抓取候选图（source 必填；默认就下载，dryRun:true 只列清单）',
  import: '把本地文件夹/压缩包导入 00_raw。**src 只能是素材目录/压缩包，给数据集根会被拒绝**（否则 images/ 和 thumbs/ 会被卷进来）',
  enrich: '按文件 md5 去 danbooru 反查原帖，把权威标签补进 raw_posts.jsonl（wash 的「来源 A」）。md5 查不到时会用文件名里的 pixiv_id 再兜一次（`<id>_p<页>` 命名的图；只认同名同页或作品仅一帖且本图是 p0，不猜）。**默认 dry-run 只查不写**，apply:true 才落盘。要在 text 修补之前跑 —— 改过像素 md5 就对不上了',
  dedup: 'md5 → pHash → SSIM 三级去重（apply 移入 _excluded/duplicates）',
  screen: '规则筛除低质量/过老/草图（apply 移入 _excluded/<reason>）',
  thumbs: '生成 ≤1536px 缩略图供视觉子代理看；有 images/ 就用它（重排后仍可用），已是最新的会跳过。**不接受 apply**',
  text: '文字/拟声词检测 + LaMa 修补（rfdetr + lama-manga）',
  rename: '按时间重排编号为 0001.ext；rename_map.csv 只追加（另留本批快照），dryRun 写 preview 表',
  wash: '按打标指南规则洗标（整类丢画师/IP/meta；角色名不做频率限制；按修补状态决定水印区），写 NNNN.txt + 报告。默认**不复活**人工删掉的图源标签（要整体重洗用 refreshSource:true）',
  'review-list': '列出需要看图补标的条目',
  'apply-review': '把视觉子代理的补标结果回填并重洗（触发词取 manifest.trigger，不再丢）',
  'fix-caption': '增量修一张图的 caption：add 补标签 / remove 删标签（记入人工删除名单，重跑 wash 不复活）/ set 整条替换。不带 apply 只看新旧对照',
  verify: '校验 caption 合规：§3 形态约束 + §9 内容闸门（BANNED 残留 / 否定式 / 质量词 / 人数一致性）+ 触发词位置 + 图-txt 配对 + 标签数区间；online:true 再用 danbooru 复核标签是否真实存在',
  'dict-check': '查一批标签在 danbooru 词典里的真实存在性与 category/post_count；不存在的给形近候选，能被别名表归一的也会说明。**补标签之前先查这里**，别凭印象编造',
  makecfg: '生成训练配置（stage1 toml / dataset toml / train bat / rationale.md）+ preflight.txt 体检单（依据与上一版的差异）。LR 超出该 rank 的建议区间会报错，除非 allowOutOfBand:true',
}

/** 每个阶段支持的参数名（供描述里的"参数矩阵"一节） */
function paramTable() {
  return Object.entries(STAGE_PARAMS)
    .map(([stage, keys]) => `  ${stage}: ${keys.length ? keys.join(', ') : '(无)'}`)
    .join('\n')
}

function prune(value) {
  if (Array.isArray(value)) return value.map(prune)
  if (value && typeof value === 'object') {
    const out = {}
    for (const [key, item] of Object.entries(value)) {
      if (item === undefined) continue
      const cleaned = prune(item)
      if (cleaned === undefined) continue
      out[key] = cleaned
    }
    return out
  }
  return value
}

function listing(home) {
  const root = path.join(home, 'datasets')
  const rows = []
  try {
    for (const entry of readdirSync(root, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue
      if (entry.name.startsWith('_') || entry.name.startsWith('.')) continue
      const manifest = path.join(root, entry.name, '_pipeline', 'manifest.json')
      let progress = {}
      let updated = ''
      let trigger = ''
      if (existsSync(manifest)) {
        try {
          const data = JSON.parse(readFileSync(manifest, 'utf8'))
          progress = data.stages ?? {}
          updated = String(data.updated_at ?? '')
          trigger = String(data.trigger ?? '')
        } catch {}
      }
      const facts = datasetFacts({ config: () => ({ home }) }, entry.name)
      const health = captionHealth(facts, trigger)
      rows.push({
        name: entry.name,
        raw: facts.raw,
        images: facts.images,
        captions: facts.captions,
        done: Object.entries(progress)
          .filter(([, value]) => value === 'done' || value?.status === 'done')
          .map(([key]) => key),
        updated,
        health,
      })
    }
  } catch {}
  rows.sort((a, b) => (a.updated < b.updated ? 1 : -1))
  return rows
}

/**
 * caption 健康度（A5）：真实使用里 agent 只能看到 images=N captions=N，
 * 标签数越界、触发词位置、孤立 txt 这些都得自己写脚本统计。
 */
function captionHealth(facts, trigger = '', minTags = 20, maxTags = 45) {
  const dir = path.join(facts.root, 'images')
  const out = {
    images: 0, captions: 0, missingTxt: 0, orphanTxt: 0, empty: 0,
    minTags: 0, maxTags: 0, avgTags: 0, underMin: 0, overMax: 0,
    triggerMissing: 0, triggerNotFirst: 0, examples: [],
  }
  let files = []
  try {
    files = readdirSync(dir, { withFileTypes: true })
      .filter((e) => e.isFile())
      .map((e) => e.name)
  } catch {
    return out
  }
  const imgExt = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'])
  const stems = new Set()
  for (const name of files) {
    if (imgExt.has(path.extname(name).toLowerCase())) {
      stems.add(path.basename(name, path.extname(name)))
    }
  }
  out.images = stems.size
  const counts = []
  for (const stem of [...stems].sort()) {
    const txt = path.join(dir, `${stem}.txt`)
    if (!existsSync(txt)) {
      out.missingTxt += 1
      if (out.examples.length < 5) out.examples.push(`${stem}: 缺 txt`)
      continue
    }
    out.captions += 1
    let text = ''
    try {
      text = readFileSync(txt, 'utf8')
    } catch {}
    const body = text.trim()
    if (!body) {
      out.empty += 1
      if (out.examples.length < 5) out.examples.push(`${stem}: 空 caption`)
      continue
    }
    const tags = body.split(',').map((t) => t.trim()).filter(Boolean)
    counts.push(tags.length)
    if (tags.length < minTags) {
      out.underMin += 1
      if (out.examples.length < 5) out.examples.push(`${stem}: 只有 ${tags.length} 个标签`)
    }
    if (tags.length > maxTags) out.overMax += 1
    if (trigger) {
      if (!body.includes(trigger)) out.triggerMissing += 1
      else if (!body.startsWith(trigger)) out.triggerNotFirst += 1
    }
  }
  for (const name of files) {
    if (path.extname(name).toLowerCase() !== '.txt') continue
    const stem = path.basename(name, '.txt')
    if (!stems.has(stem)) out.orphanTxt += 1
  }
  if (counts.length) {
    counts.sort((a, b) => a - b)
    out.minTags = counts[0]
    out.maxTags = counts[counts.length - 1]
    out.avgTags = Math.round((counts.reduce((a, b) => a + b, 0) / counts.length) * 10) / 10
  }
  return out
}

function healthLine(health, trigger = '') {
  const bits = [
    `tags ${health.minTags}/${health.avgTags}/${health.maxTags} (min/avg/max)`,
    `缺 txt ${health.missingTxt}`,
    `孤立 txt ${health.orphanTxt}`,
    `空 caption ${health.empty}`,
    `<20 标签 ${health.underMin}`,
    `>45 标签 ${health.overMax}`,
  ]
  if (trigger) bits.push(`无触发词 ${health.triggerMissing}`, `触发词不在首位 ${health.triggerNotFirst}`)
  if (health.examples.length) bits.push(`例: ${health.examples.join('; ')}`)
  return bits.join('  ')
}

function describe(args) {
  const stage = args.stage
  const bits = [`stage=${stage}`]
  for (const key of ['source', 'tags', 'creator', 'gallery', 'src', 'kind', 'trigger', 'name', 'add', 'remove']) {
    if (args[key]) bits.push(`${key}=${args[key]}`)
  }
  bits.push(args.apply ? 'apply' : 'dry-run')
  return bits.join(' ')
}

export function registerAnimaTools(sctx, options) {
  const disposers = []
  const register = (definition) => {
    disposers.push(sctx.tools.register(definition))
  }
  const runtime = { config: options.resolve }

  register(
    defineTool({
      name: 'anima_status',
      description:
        '查看 anima 风格 LoRA 数据集流水线状态。不传 dataset 时列出 home/datasets 下所有数据集及其阶段进度与图片数；传 dataset 时给出该数据集的 00_raw/images/captions/thumbs 数量、caption 健康度（标签数 min/avg/max、<20 与 >45 的张数、缺 txt、孤立 txt、触发词位置）、已完成的阶段、_pipeline 下的报告文件（含行数），以及最近的阶段运行记录。任何阶段开始前先用它确认现状。',
      parameters: {
        dataset: { type: 'string', description: '数据集目录名（home/datasets 下的子目录），省略则列出全部' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            home: { type: 'string' },
            dataset: { type: 'string' },
            raw: { type: 'number' },
            images: { type: 'number' },
            captions: { type: 'number' },
            thumbs: { type: 'number' },
            health: { type: 'string' },
            stages: { type: 'string' },
            reports: { type: 'string' },
            datasets: { type: 'string' },
            runs: { type: 'string' },
          },
        },
        render(args, value) {
          const lines = []
          if (value.datasets) {
            lines.push(`datasets under ${value.home}:`)
            lines.push(value.datasets)
          }
          if (value.dataset) {
            lines.push(
              `${value.dataset}: raw=${value.raw} images=${value.images} captions=${value.captions} thumbs=${value.thumbs}`,
            )
            if (value.health) lines.push(`caption 健康度: ${value.health}`)
            if (value.stages) lines.push(`stages: ${value.stages}`)
            if (value.reports) {
              lines.push('reports:')
              lines.push(value.reports)
            }
          }
          if (value.runs) {
            lines.push('recent runs:')
            lines.push(value.runs)
          }
          return [{ type: 'text', text: lines.join('\n') }]
        },
      },
      async execute(args) {
        const config = options.resolve()
        const out = { home: config.home }
        if (!args.dataset) {
          const rows = listing(config.home)
          out.datasets = rows
            .map((row) => `${row.name}  raw=${row.raw} images=${row.images} captions=${row.captions}  done=[${row.done.join(',')}]`)
            .join('\n')
        } else {
          const facts = datasetFacts(runtime, args.dataset)
          out.dataset = args.dataset
          out.raw = facts.raw
          out.images = facts.images
          out.captions = facts.captions
          out.thumbs = facts.thumbs
          const manifest = path.join(facts.root, '_pipeline', 'manifest.json')
          let trigger = ''
          if (existsSync(manifest)) {
            try {
              const data = JSON.parse(readFileSync(manifest, 'utf8'))
              trigger = String(data.trigger ?? '')
              out.stages = Object.entries(data.stages ?? {})
                .map(([key, value]) => `${key}:${value?.status ?? value}`)
                .join(' ')
            } catch {}
          }
          out.health = healthLine(captionHealth(facts, trigger), trigger)
          out.reports = reportLines(facts.reports).join('\n')
        }
        const runs = listRuns()
        if (runs.length > 0) {
          out.runs = runLines(runs).join('\n')
        }
        return prune(out)
      },
    }),
  )

  register(
    defineTool({
      name: 'anima_stage',
      description:
        `跑 anima 风格 LoRA 数据集流水线的一个阶段（${STAGES.join('/')}）。` +
        Object.entries(STAGE_DOC)
          .map(([key, value]) => `\n- ${key}: ${value}`)
          .join('') +
        '\n\n每个阶段支持的参数（传了列表外的参数会在执行前直接报错，不会被静默忽略）：\n' +
        paramTable() +
        '\n默认 dry-run，只有 apply:true 才写盘/移动/删除（删除一律是移动到 _excluded，不真删）。' +
        '注意 init / thumbs 写盘是默认行为（不接受 apply）；fetch / import 用 dryRun:true 预演。' +
        'fetch/text 这类长阶段可 detach:true 先拿 runId，再用 anima_job 续取。',
      parameters: {
        stage: { type: 'string', description: '阶段名', enum: STAGES },
        dataset: { type: 'string', description: '数据集目录名（home/datasets 下的子目录）' },
        source: { type: 'string', description: 'fetch 的来源', enum: ['yandere', 'danbooru', 'pawchive', 'exhentai'] },
        tags: { type: 'string', description: 'fetch 的标签/画师名（yandere/danbooru）；dict-check 时是要查的标签（**只按逗号切**，多词标签照 caption 的空格形整条写，如 `amiya (arknights)`）' },
        creator: { type: 'string', description: 'pawchive 的 creator id' },
        service: { type: 'string', description: 'pawchive 的 service（patreon/fanbox/discord…）' },
        gallery: { type: 'string', description: 'exhentai 的画廊 URL 或 gid/token' },
        cookies: { type: 'string', description: 'cookies.txt 路径（exhentai / pawchive 登录态）' },
        titleFilter: { type: 'string', description: 'pawchive 标题过滤子串' },
        src: { type: 'string', description: 'import 的源目录/压缩包路径（不能是数据集根或其父目录）' },
        kind: { type: 'string', description: 'makecfg 的 LoRA 类型；init 时记进 manifest', enum: ['style', 'character', 'object', 'scene', 'clothing'] },
        trigger: { type: 'string', description: '触发词（init/wash/apply-review/fix-caption/verify/makecfg），原样写进 caption 首位，如 @aratax；省略则用 init 时记进 manifest 的触发词' },
        name: { type: 'string', description: 'makecfg 的配置名（默认取数据集名）；fix-caption 是要修的那张图（0001 / 0001.jpg / 旧文件名都行）' },
        subdirs: { type: 'string', description: 'makecfg 的子目录列表，逗号分隔（如 before,latest,present）' },
        dim: { type: 'number', description: 'makecfg 的 network_dim（省略按图片数自动）' },
        lr: { type: 'number', description: 'makecfg 的学习率（省略按类型自动；超出该 rank 建议区间会报错）' },
        epochs: { type: 'number', description: 'makecfg 的 epoch 数（省略按曝光量自动）' },
        resolution: { type: 'number', description: 'makecfg 的 resolution，默认 1280（8GB 可退 1024）' },
        batch: { type: 'number', description: 'makecfg 的 batch size，默认 1' },
        gradAccum: { type: 'number', description: 'makecfg 的 gradient_accumulation_steps，默认 1' },
        datasetDir: { type: 'string', description: 'makecfg 的数据集目录（默认数据集根本身）' },
        limit: { type: 'number', description: 'fetch / enrich 最大张数；dict-check 最多列多少个"存在"的标签（只是显示条数，不影响检查范围）' },
        phashDistance: { type: 'number', description: 'dedup 的 pHash 汉明距离阈值，默认 4' },
        ssim: { type: 'number', description: 'dedup 判定「同一张图」的 SSIM 阈值，默认 0.995（0.97~0.995 只列入报告不动文件）'},
        minShortSide: { type: 'number', description: 'screen 短边下限，默认 512' },
        minBytes: { type: 'number', description: 'screen 体积下限，默认 102400' },
        earliest: { type: 'string', description: 'screen 最早日期，默认 2015-01-01' },
        maxSide: { type: 'number', description: 'thumbs 长边上限，默认 1536' },
        prune: { type: 'boolean', description: 'thumbs 删掉没有对应原图的陈旧缩略图' },
        warnRatio: { type: 'number', description: 'text 送修补的文字面积占比阈值，默认 0.08' },
        dropRatio: { type: 'number', description: 'text 判弃的文字面积占比阈值，默认 0.30' },
        dilate: { type: 'number', description: 'text 掩膜膨胀像素，默认 6' },
        classes: { type: 'string', description: 'text 检测的类别子集，如 text,onomatopoeia' },
        device: { type: 'string', description: 'text 的 torch 设备，如 cuda / cpu' },
        patchSmall: { type: 'boolean', description: 'text 连水印等小面积文字一起修补（默认只修补 >8% 的大段文字）' },
        thresholds: { type: 'string', description: "text 检测阈值，如 'text=0.15,onomatopoeia=0.12'；调低可抓到淡水印但会误检" },
        detectOnly: { type: 'boolean', description: 'text 只检测不修补（先看报告再决定）' },
        inpaintOnly: { type: 'boolean', description: 'text 只按已有掩膜修补' },
        rules: { type: 'string', description: 'wash 的额外规则 JSON 路径' },
        refreshSource: { type: 'boolean', description: 'wash 按图源标签整体重洗（默认不复活人工删掉的标签）' },
        noImages: { type: 'boolean', description: 'wash 处理 00_raw 而不是 images' },
        noPixiv: { type: 'boolean', description: 'enrich 的 md5 查不到时不再用文件名里的 pixiv_id 兜底（默认会兜）' },
        payload: { type: 'string', description: 'apply-review 的补标 JSON 文件路径' },
        add: { type: 'string', description: 'fix-caption 要补的标签，逗号分隔（照样过 wash 规则）' },
        remove: { type: 'string', description: 'fix-caption 要删的标签，逗号分隔（记入人工删除名单，重跑 wash 不复活）' },
        set: { type: 'string', description: 'fix-caption 整条替换成这段 caption（与 add/remove 二选一）' },
        start: { type: 'number', description: 'rename 起始编号，默认 1' },
        digits: { type: 'number', description: 'rename 编号位数，默认 4' },
        online: { type: 'boolean', description: 'verify 是否走 danbooru 在线校验' },
        sample: { type: 'number', description: 'verify 抽查张数（默认全部）' },
        apply: { type: 'boolean', description: '真正写盘（默认 false = dry-run）' },
        dryRun: { type: 'boolean', description: 'fetch / import 的反向开关：true = 只预演不下载/不导入' },
        move: { type: 'boolean', description: 'rename / import 使用移动而非复制' },
        noUnpack: { type: 'boolean', description: 'import 不解压压缩包' },
        force: { type: 'boolean', description: 'init 就地更新触发词/类型并保留阶段记录；makecfg 不备份直接覆盖已有配置。其他阶段传它会在执行前报错' },
        reset: { type: 'boolean', description: 'init 从零重建 manifest（会丢阶段记录，需同时 yes:true）' },
        yes: { type: 'boolean', description: 'init --reset 的二次确认' },
        allowOutOfBand: { type: 'boolean', description: 'makecfg 明知 LR 超出该 rank 建议区间也照写（默认报错让你确认）' },
        includeImages: { type: 'boolean', description: 'dedup/screen/thumbs 也处理 images/ 下已重排的图' },
        detach: { type: 'boolean', description: '不等结果，立刻返回 runId（长阶段用）' },
        waitMs: { type: 'number', description: '等待上限毫秒，默认取插件 timeoutMs' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: RUN_OUTPUT_PROPERTIES,
        },
        render(args, value) {
          const head = `${value.stage} on ${value.dataset ?? '(none)'}: ${value.ok ? 'ok' : value.running ? 'running' : 'failed'} (${value.seconds}s, exit=${value.exitCode})`
          const lines = [head]
          if (value.error) lines.push(`error: ${value.error}`)
          if (value.notes) lines.push(value.notes)
          if (value.reports) lines.push(`reports: ${value.reports}`)
          lines.push('--- log tail ---')
          lines.push(String(value.log ?? '').split('\n').slice(-25).join('\n'))
          return [{ type: 'text', text: lines.join('\n') }]
        },
      },
      async execute(args) {
        const config = options.resolve()
        if (!args.dataset && args.stage !== 'dict-check') throw new Error('dataset is required')
        const paramError = stageParamError(args.stage, args)
        if (paramError) throw new Error(paramError)
        const argv = buildArgs(runtime, args)
        const long = args.stage === 'fetch' || args.stage === 'text' || args.stage === 'import'
        const timeoutMs = args.waitMs ?? (long ? config.longTimeoutMs : config.timeoutMs)
        const entry = start(runtime, argv, {
          timeoutMs,
          python: args.stage === 'text' ? resolveMlPython(runtime) : resolvePython(runtime),
        })
        if (args.detach === true) {
          return prune({
            runId: entry.runId,
            stage: entry.stage,
            dataset: entry.dataset,
            ok: false,
            running: true,
            exitCode: -1,
            seconds: 0,
            command: `[detached] ${entry.command}`,
            log: `started: ${describe(args)}`,
          })
        }
        const result = await collect(runtime, entry, { waitMs: timeoutMs })
        return prune({ ...result, log: result.log.join('\n') })
      },
    }),
  )

  register(
    defineTool({
      name: 'anima_job',
      description:
        '续取一个后台阶段运行（anima_stage 用 detach:true 启动后拿到的 runId）的结果。不传 runId 时列出最近的运行。仍在跑就返回 running:true 和当前日志尾部，可稍后再调。',
      parameters: {
        runId: { type: 'string', description: 'anima_stage detach 返回的 runId' },
        waitMs: { type: 'number', description: '等待上限毫秒，默认 60000' },
        tail: { type: 'number', description: '返回日志尾部行数，默认 60' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: RUN_OUTPUT_PROPERTIES,
        },
        render(args, value) {
          if (value.runs) return [{ type: 'text', text: value.runs }]
          const head = `${value.runId} ${value.stage} ${value.dataset ?? ''}: ${value.running ? 'running' : value.ok ? 'ok' : 'failed'} (${value.seconds}s)`
          const lines = [head]
          if (value.error) lines.push(`error: ${value.error}`)
          if (value.notes) lines.push(value.notes)
          lines.push(String(value.log ?? '').split('\n').slice(-30).join('\n'))
          return [{ type: 'text', text: lines.join('\n') }]
        },
      },
      async execute(args) {
        if (!args.runId) {
          const runs = listRuns()
          return {
            runs:
              runs.length === 0
                ? '(no runs in this session)'
                : runLines(runs).join('\n'),
          }
        }
        const entry = lookup(args.runId)
        if (!entry) throw new Error(`unknown runId: ${args.runId}`)
        const result = await collect(runtime, entry, { waitMs: args.waitMs ?? 60000, tail: args.tail ?? 60 })
        return prune({ ...result, log: result.log.join('\n') })
      },
    }),
  )

  register(
    defineTool({
      name: 'anima_doctor',
      description:
        'anima 流水线环境自检：工具链解释器与依赖、ML 解释器（torch+rfdetr）、训练器、anima_lora、koharu 权重、四份词典、curl 兜底、各图源连通性与代理、以及各图源的凭据配没配（danbooru API key / exhentai / cookies.txt；只报有无，不打印密钥）。proxies:true 时额外跑「HTTP 客户端 × 代理候选」实测矩阵（每个候选 ~10s，用于定位「requests 走不通但 curl 能通」这类问题）。任何阶段报错或换机器先跑它。',
      parameters: {
        proxies: { type: 'boolean', description: '额外跑代理实测矩阵（慢，排障时再用）' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            ok: { type: 'boolean' },
            report: { type: 'string' },
          },
        },
        render(args, value) {
          return [{ type: 'text', text: value.report ?? '' }]
        },
      },
      async execute(args) {
        const config = options.resolve()
        const argv = ['-X', 'utf8', '-m', 'animasl.cli', '--home', config.home, 'doctor']
        if (args.proxies === true) argv.push('--proxies')
        const entry = start(runtime, argv, { timeoutMs: 300000 })
        const result = await collect(runtime, entry, { waitMs: 300000, tail: 200 })
        return { ok: result.ok, report: result.log.join('\n') }
      },
    }),
  )

  return disposers
}
