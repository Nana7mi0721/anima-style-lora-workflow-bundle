/**
 * 工具层：把 animasl 工具箱暴露成 4 个宿主工具。
 *
 * anima_status  看某个数据集/全部数据集的进度与产出
 * anima_stage   跑一个流水线阶段（默认 dry-run，写盘要 apply:true）
 * anima_job     续取后台阶段（wait:false 启动的）结果
 * anima_doctor  环境自检（解释器 / 权重 / 词典 / 代理 / 训练器）
 *
 * 返回结构里有 undefined 会被 tool runtime 拒绝，统一过 prune()。
 */

import { defineTool } from '@deepseek-ai/dsh-tools'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { FORCE_STAGES, buildArgs, collect, datasetFacts, lookup, listRuns, reportLines, resolveMlPython, resolvePython, runLines, start } from './run.js'

const STAGES = [
  'init',
  'fetch',
  'import',
  'dedup',
  'screen',
  'thumbs',
  'text',
  'rename',
  'wash',
  'review-list',
  'apply-review',
  'verify',
  'makecfg',
]

const STAGE_DOC = {
  init: '建立数据集骨架目录与 manifest；数据集已存在时不会重建（force=true 就地改触发词/类型并保留阶段记录）',
  fetch: '从 yandere / danbooru / pawchive / exhentai 抓取候选图（--source 必填，dry-run 只列清单）',
  import: '把本地文件夹/压缩包导入 00_raw',
  dedup: 'md5 → pHash → SSIM 三级去重（apply 移入 _excluded/duplicates）',
  screen: '规则筛除低质量/过老/草图（apply 移入 _excluded/<reason>）',
  thumbs: '生成 ≤1536px 缩略图，供视觉子代理看',
  text: '文字/拟声词检测 + LaMa 修补（rfdetr + lama-manga）',
  rename: '按时间重排编号为 0001.ext，写 rename_map.csv',
  wash: '按打标指南规则洗标（整类丢画师/IP/meta；角色名不做频率限制；按修补状态决定水印区），写 NNNN.txt + 报告',
  'review-list': '列出需要看图补标的条目',
  'apply-review': '把视觉子代理的补标结果回填并重洗',
  verify: '校验 caption 合规：§3 形态约束 + §9 内容闸门（BANNED 残留 / 否定式 / 质量词 / 人数一致性）+ 图-txt 配对；--online 再用 danbooru 复核标签是否真实存在',
  makecfg: '生成训练配置三件套（stage1 toml / dataset toml / bat）',
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
      const manifest = path.join(root, entry.name, '_pipeline', 'manifest.json')
      let progress = {}
      let updated = ''
      if (existsSync(manifest)) {
        try {
          const data = JSON.parse(readFileSync(manifest, 'utf8'))
          progress = data.stages ?? {}
          updated = String(data.updated_at ?? '')
        } catch {}
      }
      const facts = datasetFacts({ config: () => ({ home }) }, entry.name)
      rows.push({
        name: entry.name,
        raw: facts.raw,
        images: facts.images,
        captions: facts.captions,
        done: Object.entries(progress)
          .filter(([, value]) => value === 'done' || value?.status === 'done')
          .map(([key]) => key),
        updated,
      })
    }
  } catch {}
  rows.sort((a, b) => (a.updated < b.updated ? 1 : -1))
  return rows
}

function describe(args) {
  const stage = args.stage
  const bits = [`stage=${stage}`]
  for (const key of ['source', 'tags', 'creator', 'gallery', 'src', 'kind', 'trigger']) {
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
        '查看 anima 风格 LoRA 数据集流水线状态。不传 dataset 时列出 home/datasets 下所有数据集及其阶段进度与图片数；传 dataset 时给出该数据集的 00_raw/images/captions/thumbs 数量、已完成的阶段、_pipeline 下的报告文件（含行数），以及最近的阶段运行记录。任何阶段开始前先用它确认现状。',
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
          if (existsSync(manifest)) {
            try {
              const data = JSON.parse(readFileSync(manifest, 'utf8'))
              out.stages = Object.entries(data.stages ?? {})
                .map(([key, value]) => `${key}:${value?.status ?? value}`)
                .join(' ')
            } catch {}
          }
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
        '\n默认 dry-run，只有 apply:true 才写盘/移动/删除（删除一律是移动到 _excluded，不真删）。' +
        'fetch/text 这类长阶段可 wait:false 先拿 runId，再用 anima_job 续取。',
      parameters: {
        stage: { type: 'string', description: '阶段名', enum: STAGES },
        dataset: { type: 'string', description: '数据集目录名（home/datasets 下的子目录）' },
        source: { type: 'string', description: 'fetch 的来源', enum: ['yandere', 'danbooru', 'pawchive', 'exhentai'] },
        tags: { type: 'string', description: 'fetch 的标签/画师名（yandere/danbooru）' },
        creator: { type: 'string', description: 'pawchive 的 creator id' },
        service: { type: 'string', description: 'pawchive 的 service（patreon/fanbox/discord…）' },
        gallery: { type: 'string', description: 'exhentai 的画廊 URL 或 gid/token' },
        cookies: { type: 'string', description: 'cookies.txt 路径（exhentai / pawchive 登录态）' },
        titleFilter: { type: 'string', description: 'pawchive 标题过滤子串' },
        src: { type: 'string', description: 'import 的源目录/压缩包路径' },
        kind: { type: 'string', description: 'makecfg 的 LoRA 类型', enum: ['style', 'character', 'object', 'scene', 'clothing'] },
        trigger: { type: 'string', description: '触发词（wash/makecfg），原样写进 caption 首位，如 @aratax；省略则用 init 时记进 manifest 的触发词' },
        name: { type: 'string', description: 'makecfg 的配置名（默认取数据集名）' },
        subdirs: { type: 'string', description: 'makecfg 的子目录列表，逗号分隔（如 before,latest,present）' },
        dim: { type: 'number', description: 'makecfg 的 network_dim（省略按图片数自动）' },
        lr: { type: 'number', description: 'makecfg 的学习率（省略按类型自动）' },
        epochs: { type: 'number', description: 'makecfg 的 epoch 数（省略按曝光量自动）' },
        resolution: { type: 'number', description: 'makecfg 的 resolution，默认 1280（8GB 可退 1024）' },
        limit: { type: 'number', description: 'fetch 最大张数' },
        phashDistance: { type: 'number', description: 'dedup 的 pHash 汉明距离阈值，默认 4' },
        ssim: { type: 'number', description: 'dedup 判定「同一张图」的 SSIM 阈值，默认 0.995（0.97~0.995 只列入报告不动文件）'},
        minShortSide: { type: 'number', description: 'screen 短边下限，默认 512' },
        minBytes: { type: 'number', description: 'screen 体积下限，默认 102400' },
        earliest: { type: 'string', description: 'screen 最早日期，默认 2015-01-01' },
        maxSide: { type: 'number', description: 'thumbs 长边上限，默认 1536' },
        warnRatio: { type: 'number', description: 'text 送修补的文字面积占比阈值，默认 0.08' },
        dropRatio: { type: 'number', description: 'text 判弃的文字面积占比阈值，默认 0.30' },
        dilate: { type: 'number', description: 'text 掩膜膨胀像素，默认 6' },
        classes: { type: 'string', description: 'text 检测的类别子集，如 text,onomatopoeia' },
        device: { type: 'string', description: 'text 的 torch 设备，如 cuda / cpu' },
        patchSmall: { type: 'boolean', description: 'text 连水印等小面积文字一起修补（默认只修补 >8% 的大段文字）' },
        thresholds: { type: 'string', description: "text 检测阈值，如 'text=0.15,onomatopoeia=0.12'；调低可抓到淡水印但会误检" },
        rules: { type: 'string', description: 'wash 的额外规则 JSON 路径' },
        payload: { type: 'string', description: 'apply-review 的补标 JSON 文件路径' },
        start: { type: 'number', description: 'rename 起始编号，默认 1' },
        digits: { type: 'number', description: 'rename 编号位数，默认 4' },
        online: { type: 'boolean', description: 'verify 是否走 danbooru 在线校验' },
        apply: { type: 'boolean', description: '真正写盘（默认 false = dry-run）' },
        move: { type: 'boolean', description: 'rename/import 使用移动而非复制' },
        force: { type: 'boolean', description: '忽略已有产出重新计算（只有 init / makecfg 阶段支持；其他阶段会被忽略并在日志首行提示）。init 上它表示"就地更新触发词/类型、保留阶段记录"，不重建 manifest' },
        includeImages: { type: 'boolean', description: 'dedup/screen 也处理 images/ 下已导入的图' },
        detach: { type: 'boolean', description: '不等结果，立刻返回 runId（长阶段用）' },
        waitMs: { type: 'number', description: '等待上限毫秒，默认取插件 timeoutMs' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            runId: { type: 'string' },
            stage: { type: 'string' },
            dataset: { type: 'string' },
            ok: { type: 'boolean' },
            running: { type: 'boolean' },
            timedOut: { type: 'boolean' },
            exitCode: { type: 'number' },
            seconds: { type: 'number' },
            command: { type: 'string' },
            log: { type: 'string' },
            error: { type: 'string' },
            images: { type: 'number' },
            captions: { type: 'number' },
            raw: { type: 'number' },
            reports: { type: 'string' },
          },
        },
        render(args, value) {
          const head = `${value.stage} on ${value.dataset ?? '(none)'}: ${value.ok ? 'ok' : value.running ? 'running' : 'failed'} (${value.seconds}s, exit=${value.exitCode})`
          const lines = [head]
          if (value.error) lines.push(`error: ${value.error}`)
          if (value.reports) lines.push(`reports: ${value.reports}`)
          lines.push('--- log tail ---')
          lines.push(String(value.log ?? '').split('\n').slice(-25).join('\n'))
          return [{ type: 'text', text: lines.join('\n') }]
        },
      },
      async execute(args) {
        const config = options.resolve()
        if (!args.dataset) throw new Error('dataset is required')
        const argv = buildArgs(runtime, args)
        const ignoredForce = args.force === true && !FORCE_STAGES.has(args.stage)
        const forceNote = ignoredForce
          ? `note: 阶段 ${args.stage} 不支持 force，已忽略（只有 ${[...FORCE_STAGES].join(' / ')} 支持）\n`
          : ''
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
            log: `${forceNote}started: ${describe(args)}`,
          })
        }
        const result = await collect(runtime, entry, { waitMs: timeoutMs })
        return prune({ ...result, log: forceNote + result.log.join('\n') })
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
          properties: {
            runId: { type: 'string' },
            stage: { type: 'string' },
            dataset: { type: 'string' },
            ok: { type: 'boolean' },
            running: { type: 'boolean' },
            timedOut: { type: 'boolean' },
            exitCode: { type: 'number' },
            seconds: { type: 'number' },
            log: { type: 'string' },
            error: { type: 'string' },
            runs: { type: 'string' },
          },
        },
        render(args, value) {
          if (value.runs) return [{ type: 'text', text: value.runs }]
          const head = `${value.runId} ${value.stage} ${value.dataset ?? ''}: ${value.running ? 'running' : value.ok ? 'ok' : 'failed'} (${value.seconds}s)`
          return [{ type: 'text', text: `${head}\n${String(value.log ?? '').split('\n').slice(-30).join('\n')}` }]
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
        'anima 流水线环境自检：工具链解释器与依赖、ML 解释器（torch+rfdetr）、训练器、anima_lora、koharu 权重、四份词典、代理连通性。任何阶段报错或换机器先跑它。',
      parameters: {},
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
      async execute() {
        const config = options.resolve()
        const argv = ['-X', 'utf8', '-m', 'animasl.cli', '--home', config.home, 'doctor']
        const entry = start(runtime, argv, { timeoutMs: 180000 })
        const result = await collect(runtime, entry, { waitMs: 180000, tail: 200 })
        return { ok: result.ok, report: result.log.join('\n') }
      },
    }),
  )

  return disposers
}
