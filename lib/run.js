/**
 * 子进程执行层：定位 python、拼命令行、跑 animasl.cli、回收报告。
 *
 * 为什么单独一层：工具定义只关心「跑哪个 stage、回什么结构」，
 * 解释器发现（runtime venv / 包内 venv / anima_lora venv / PATH）、
 * 环境变量（PYTHONPATH、UTF-8、ANIMASL_*）、日志截断与运行登记都在这里。
 */

import { spawn } from 'node:child_process'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

/** 包根目录（lib/ 的上一层）——python/ 与 skills/ 都在这里。 */
export const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

/**
 * 支持 --force 的阶段（与 python/animasl/cli.py 里的子命令一一对应）。
 * 其余阶段传 force 只会被静默忽略，并在 anima_stage 的返回里给一行提示。
 */
export const FORCE_STAGES = new Set(['init', 'makecfg'])

/** 运行登记表：runId -> 运行状态。wait:false 的调用靠它续取结果。 */
const RUNS = new Map()
let runSeq = 0

function firstExisting(candidates) {
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.length > 0 && existsSync(candidate)) return candidate
  }
  return ''
}

/** 工具链解释器：只需要 requests + pillow + numpy。 */
export function resolvePython(runtime) {
  const config = runtime.config()
  return (
    firstExisting([
      config.python,
      path.join(config.runtimeDir, 'venv', 'Scripts', 'python.exe'),
      path.join(config.pythonDir, '.venv', 'Scripts', 'python.exe'),
      path.join(config.animaLoraDir, '.venv', 'Scripts', 'python.exe'),
      'python',
    ]) || 'python'
  )
}

/** ML 解释器：需要 torch + rfdetr，只有文字检测/修补阶段用。 */
export function resolveMlPython(runtime) {
  const config = runtime.config()
  return (
    firstExisting([
      config.mlPython,
      path.join(config.runtimeDir, 'venv', 'Scripts', 'python.exe'),
      path.join(config.pythonDir, '.venv', 'Scripts', 'python.exe'),
    ]) || resolvePython(runtime)
  )
}

/** 把 stage 参数翻译成 animasl.cli 的 argv。 */
export function buildArgs(runtime, args) {
  const config = runtime.config()
  const stage = String(args.stage ?? '')
  const argv = ['-X', 'utf8', '-m', 'animasl.cli', '--home', config.home, stage, '--dataset', String(args.dataset ?? '')]

  const flag = (name, value) => {
    if (value !== undefined && value !== null && value !== '') argv.push(name, String(value))
  }
  const toggle = (name, value) => {
    if (value === true) argv.push(name)
  }

  flag('--source', args.source)
  flag('--tags', args.tags)
  flag('--creator', args.creator)
  flag('--service', args.service)
  flag('--gallery', args.gallery)
  flag('--cookies', args.cookies)
  flag('--title-filter', args.titleFilter)
  flag('--src', args.src)
  flag('--trigger', args.trigger)
  flag('--kind', args.kind)
  flag('--subdirs', args.subdirs)
  flag('--name', args.name)
  flag('--rules', args.rules)
  flag('--payload', args.payload)
  flag('--dataset-dir', args.datasetDir)
  flag('--earliest', args.earliest)
  flag('--classes', args.classes)
  flag('--device', args.device)
  flag('--phash-distance', args.phashDistance)
  flag('--ssim', args.ssim)
  flag('--min-short-side', args.minShortSide)
  flag('--min-bytes', args.minBytes)
  flag('--max-side', args.maxSide)
  flag('--warn-ratio', args.warnRatio)
  flag('--drop-ratio', args.dropRatio)
  flag('--dilate', args.dilate)
  flag('--limit', args.limit)
  flag('--dim', args.dim)
  flag('--lr', args.lr)
  flag('--epochs', args.epochs)
  flag('--resolution', args.resolution)
  flag('--batch', args.batch)
  flag('--grad-accum', args.gradAccum)
  flag('--start', args.start)
  flag('--digits', args.digits)
  flag('--thresholds', args.thresholds)

  toggle('--apply', args.apply)
  toggle('--dry-run', args.dryRun)
  toggle('--move', args.move)
  // --force 只存在于部分子命令；无条件下发会让 argparse 报
  // "unrecognized arguments: --force"，所以按阶段白名单转发。
  if (args.force === true && FORCE_STAGES.has(args.stage)) argv.push('--force')
  toggle('--online', args.online)
  toggle('--include-images', args.includeImages)
  toggle('--no-unpack', args.noUnpack)
  toggle('--detect-only', args.detectOnly)
  toggle('--inpaint-only', args.inpaintOnly)
  toggle('--patch-small', args.patchSmall)
  toggle('--save-overlay', args.saveOverlay)
  return argv
}

/** 采集某个数据集的现状（不跑任何 stage）。 */
export function datasetFacts(runtime, dataset) {
  const config = runtime.config()
  const root = path.join(config.home, 'datasets', dataset)
  const count = (dir, filter) => {
    try {
      return readdirSync(dir).filter(filter).length
    } catch {
      return 0
    }
  }
  const isImage = (name) => /\.(png|jpe?g|webp|gif|bmp|avif)$/i.test(name)
  const reports = []
  const pipe = path.join(root, '_pipeline')
  try {
    for (const entry of readdirSync(pipe)) {
      if (!entry.endsWith('.csv')) continue
      const full = path.join(pipe, entry)
      const stat = statSync(full)
      let rows = 0
      try {
        rows = stat.size === 0 ? 0 : Math.max(0, countLines(full) - 1)
      } catch {
        rows = 0
      }
      reports.push({ name: entry, rows, modified: stat.mtime.toISOString().slice(0, 16).replace('T', ' ') })
    }
  } catch {}
  reports.sort((a, b) => (a.modified < b.modified ? 1 : -1))
  return {
    root,
    raw: count(path.join(root, '00_raw'), isImage),
    images: count(path.join(root, 'images'), isImage),
    captions: count(path.join(root, 'images'), (n) => n.endsWith('.txt')),
    thumbs: count(path.join(pipe, 'thumbs'), (n) => n.endsWith('.jpg')),
    reports: reports.slice(0, 10),
  }
}

function countLines(file) {
  // 小文件才数行；报告最多几万行，用同步读一次就够（只有 status 会走这里）
  const text = readFileSync(file, 'utf8')
  let lines = 0
  for (let i = 0; i < text.length; i += 1) if (text[i] === '\n') lines += 1
  return lines
}

/**
 * 跑一条 animasl.cli 命令。
 * @param {any} runtime - { config }
 * @param {string[]} argv - 传给 python 的参数
 * @param {{timeoutMs?: number, label?: string}} options
 * @returns {Promise<any>} 运行登记条目
 */
export function start(runtime, argv, options = {}) {
  const config = runtime.config()
  const python = options.python ?? resolvePython(runtime)
  const runId = `anima-${++runSeq}`
  const started = Date.now()
  const entry = {
    runId,
    argv,
    command: [python, ...argv].join(' '),
    running: true,
    ok: undefined,
    exitCode: undefined,
    signal: undefined,
    seconds: 0,
    log: [],
    error: '',
    dataset: datasetOf(argv),
    stage: stageOf(argv),
    startedAt: new Date().toISOString(),
  }
  RUNS.set(runId, entry)

  const env = {
    ...process.env,
    PYTHONPATH: [config.pythonDir, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
    PYTHONIOENCODING: 'utf-8',
    PYTHONUNBUFFERED: '1',
    ANIMASL_HOME: config.home,
    ANIMASL_RUNTIME: config.runtimeDir,
  }
  const child = spawn(python, argv, { cwd: config.pythonDir, env, windowsHide: true })
  entry.pid = child.pid

  const push = (chunk) => {
    const text = chunk.toString('utf8')
    for (const line of text.split(/\r?\n/)) {
      if (line.trim().length > 0) entry.log.push(line)
    }
    if (entry.log.length > 2000) entry.log.splice(0, entry.log.length - 2000)
  }
  child.stdout.on('data', push)
  child.stderr.on('data', push)

  entry.done = new Promise((resolve) => {
    const finish = (code, signal) => {
      entry.running = false
      entry.settled = true
      entry.exitCode = code
      entry.signal = signal
      entry.seconds = Math.round((Date.now() - started) / 100) / 10
      entry.ok = code === 0
      resolve(entry)
    }
    child.on('error', (err) => {
      entry.error = String(err?.message ?? err)
      finish(-1, undefined)
    })
    child.on('close', (code, signal) => finish(code, signal))
  })

  const timeoutMs = Number(options.timeoutMs ?? config.timeoutMs)
  if (timeoutMs > 0) {
    entry.timer = setTimeout(() => {
      if (!entry.running) return
      entry.timedOut = true
      try {
        child.kill()
      } catch {}
    }, timeoutMs)
    entry.done.then(() => clearTimeout(entry.timer))
  }
  return entry
}

/** 等一条运行结束并按结构回收。 */
export async function collect(runtime, entry, options = {}) {
  if (entry.running) {
    await Promise.race([
      entry.done,
      new Promise((resolve) => setTimeout(resolve, Number(options.waitMs ?? runtime.config().timeoutMs))),
    ])
  }
  const tail = Number(options.tail ?? 60)
  const facts = entry.dataset ? datasetFacts(runtime, entry.dataset) : undefined
  return {
    runId: entry.runId,
    stage: entry.stage,
    dataset: entry.dataset,
    ok: entry.ok === true,
    running: entry.running === true,
    timedOut: entry.timedOut === true,
    exitCode: entry.exitCode,
    seconds: entry.seconds,
    command: entry.command,
    log: entry.log.slice(-tail),
    error: entry.error,
    ...(facts === undefined ? {} : { images: facts.images, captions: facts.captions, raw: facts.raw, reports: reportLines(facts.reports).join('\n') }),
  }
}

export function lookup(runId) {
  return RUNS.get(runId)
}

export function listRuns() {
  return [...RUNS.values()].slice(-10).map((entry) => ({
    runId: entry.runId,
    stage: entry.stage,
    dataset: entry.dataset,
    running: entry.running,
    ok: entry.ok,
    seconds: entry.seconds,
    startedAt: entry.startedAt,
  }))
}

/** 运行记录 → 一行一条文本（工具返回值里不能出现 undefined 字样）。 */
export function runLines(rows) {
  return rows.map(
    (row) =>
      `${row.runId} ${row.stage ?? '(none)'} ${row.dataset ?? ''} running=${row.running === true} ok=${row.ok === true} ${row.seconds}s`,
  )
}

/** 报告文件 → 一行一条文本。 */
export function reportLines(rows) {
  return rows.map((row) => `${row.name}  rows=${row.rows}  ${row.modified}`)
}

/**
 * 从 argv 里认出子命令名。argv 形如
 *   ['-X','utf8','-m','animasl.cli','--home',HOME, STAGE, '--dataset', DS, ...]
 * doctor / status 这类没有 --dataset 的命令也要认出来（早先按 '--dataset'
 * 前一个 token 取，导致 doctor 的 stage 变成 undefined）。
 */
function stageOf(argv) {
  const start = argv.indexOf('animasl.cli')
  let i = start >= 0 ? start + 1 : 0
  while (i < argv.length) {
    const token = argv[i]
    if (token === '--home') {
      i += 2
      continue
    }
    if (token.startsWith('-')) {
      i += 1
      continue
    }
    return token
  }
  return undefined
}

function datasetOf(argv) {
  const index = argv.indexOf('--dataset')
  return index >= 0 ? argv[index + 1] : undefined
}
