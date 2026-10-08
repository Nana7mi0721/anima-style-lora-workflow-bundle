/**
 * 参数矩阵回归（纯离线、毫秒级）。
 *
 * 为什么要它：真实使用里模型三次靠试错才发现 init / thumbs 不吃 apply、verify 不吃 trigger ——
 * 报错是子进程 argparse 抛的 "unrecognized arguments: --apply"，长阶段还得先白等。
 * 现在改成执行前校验，这个测试钉住三件事：
 *   1. stageParamError 对「阶段不支持的参数」报错，并列出该阶段支持的参数；
 *   2. 合法组合（含每个阶段的全部声明参数）不被误报；
 *   3. 工具声明的 parameters 与 STAGE_PARAMS 一致 —— 否则模型看的描述和实际校验会对不上。
 *
 *   node --import ./test/stub/register.mjs test/params.mjs
 */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { RUN_OUTPUT_PROPERTIES, STAGE_PARAMS, datasetFacts, stageParamError } from '../lib/run.js'
import { registerAnimaTools } from '../lib/tools.js'

const errors = []
function check(ok, message) {
  if (!ok) errors.push(message)
  console.log(`${ok ? 'ok  ' : 'fail'}  ${message}`)
}

// ---- 1. 该报错的组合 --------------------------------------------------------

const bad = [
  ['init', { stage: 'init', dataset: 'd', apply: true }, 'apply', 'dryRun'],
  ['thumbs', { stage: 'thumbs', dataset: 'd', apply: true }, 'apply', 'dryRun'],
  ['verify', { stage: 'verify', dataset: 'd', apply: true }, 'apply', '只读不写'],
  ['wash', { stage: 'wash', dataset: 'd', force: true }, 'force', 'init / makecfg'],
  ['rename', { stage: 'rename', dataset: 'd', dryRun: true }, 'dryRun', 'fetch / import'],
  ['screen', { stage: 'screen', dataset: 'd', online: true }, 'online', 'screen 支持'],
]
for (const [label, args, needle, hint] of bad) {
  const err = stageParamError(args.stage, args)
  check(err.includes('不支持参数') && err.includes(needle), `${label}: 拒绝 ${needle}`)
  check(err.includes(hint) || err.includes(args.stage + ' 支持'), `${label}: 报错里带可执行提示`)
}

// ---- 2. 合法组合不被误报 ----------------------------------------------------

for (const [stage, keys] of Object.entries(STAGE_PARAMS)) {
  const args = { stage, dataset: 'd' }
  for (const key of keys) {
    args[key] = key === 'apply' || key === 'dryRun' || key === 'move' ? true
      : key === 'limit' || key === 'sample' ? 3
        : key === 'src' || key === 'payload' || key === 'name' ? 'x'
          : 'value'
  }
  if (keys.includes('apply')) args.apply = true
  args.waitMs = 1000
  args.detach = true
  const err = stageParamError(stage, args)
  check(err === '', `${stage}: 全量合法参数通过（${keys.join(',') || '无专属参数'}）`)
}

check(stageParamError('init', { stage: 'init', dataset: 'd', apply: false }) === '',
  'apply:false 视同没传（不误报）')
check(stageParamError('dict-check', { stage: 'dict-check', tags: '1girl' }) === '',
  'dict-check 可以不带 dataset')

// ---- 3. 工具声明 vs STAGE_PARAMS -------------------------------------------

const defs = new Map()
const sctx = {
  tools: { register: (def) => { defs.set(def.name, def); return () => defs.delete(def.name) } },
  effect: (fn) => fn(),
}
registerAnimaTools(sctx, { resolve: () => ({ home: 'E:/LoRA_Train' }) })

const stageTool = defs.get('anima_stage')
check(Boolean(stageTool), 'anima_stage 注册成功')
const declared = new Set(Object.keys(stageTool.parameters))
for (const [stage, keys] of Object.entries(STAGE_PARAMS)) {
  const missing = keys.filter((key) => !declared.has(key))
  check(missing.length === 0, `anima_stage 声明了 ${stage} 用到的参数${missing.length ? '（缺 ' + missing.join(',') + '）' : ''}`)
}
const declaredStages = new Set(stageTool.parameters.stage.enum)
for (const stage of Object.keys(STAGE_PARAMS)) {
  check(declaredStages.has(stage), `stage enum 含 ${stage}`)
}

// anima_stage / anima_job 必须共用同一份输出 schema（早先 anima_job 少声明 5 个键，
// 宿主校验直接判 invalid output，后台任务结果永远取不回来）
const jobSchema = defs.get('anima_job').output.schema.properties
const stageSchema = stageTool.output.schema.properties
for (const key of Object.keys(RUN_OUTPUT_PROPERTIES)) {
  check(Object.keys(stageSchema).includes(key), `anima_stage 输出声明 ${key}`)
  check(Object.keys(jobSchema).includes(key), `anima_job 输出声明 ${key}`)
}
check(Object.keys(jobSchema).length === Object.keys(stageSchema).length,
  'anima_job 与 anima_stage 输出 schema 完全一致')

// ---- 4. 图桶扫描 + status 渲染（复盘痛点 4/5）-------------------------------
// 上次真实使用把 142 条成品 caption 放在 clean/ 与 watermark/ 里，images/ 是空的，
// 于是 anima_status 只报 raw=0 images=0 captions=0，工具对整批成品视而不见。

const tmpHome = mkdtempSync(join(tmpdir(), 'anima-params-'))
const dsRoot = join(tmpHome, 'datasets', 't')
for (const dir of ['clean', 'watermark', 'images', '00_raw', '_pipeline']) {
  mkdirSync(join(dsRoot, dir), { recursive: true })
}
writeFileSync(join(dsRoot, 'clean', '0001.png'), 'png')
writeFileSync(join(dsRoot, 'clean', '0001.txt'), '@t, 1girl, solo')
writeFileSync(join(dsRoot, 'watermark', '0002.jpg'), 'jpg')
writeFileSync(join(dsRoot, 'watermark', '0002.txt'), '@t, 1boy, watermark')
writeFileSync(join(dsRoot, '00_raw', '149878653_p0.png'), 'png')
writeFileSync(join(dsRoot, '_pipeline', 'wash_report.csv'), 'file,origin\n0001.png,A=booru\n0002.jpg,A=none\n')

const facts = datasetFacts({ config: () => ({ home: tmpHome }) }, 't')
check(facts.buckets.map((b) => b.name).join(',') === 'clean,watermark',
  `datasetFacts 发现图桶（实际 ${facts.buckets.map((b) => b.name).join(',') || '无'}）`)
check(facts.bucketImages === 2 && facts.bucketCaptions === 2,
  `datasetFacts 数桶里的图与 caption（${facts.bucketImages} 图 / ${facts.bucketCaptions} caption）`)
check(facts.images === 0 && facts.raw === 1, '空 images/ 与 00_raw 仍各按各的算')
check(facts.reports.some((r) => r.name === 'wash_report.csv' && r.rows === 2), '报告行数照旧数得对')

const defs2 = new Map()
registerAnimaTools({
  tools: { register: (def) => { defs2.set(def.name, def); return () => defs2.delete(def.name) } },
  effect: (fn) => fn(),
}, { resolve: () => ({ home: tmpHome }) })
const statusTool = defs2.get('anima_status')
const statusValue = await statusTool.execute({ dataset: 't' }, { signal: undefined })
const statusText = statusTool.output.render({ dataset: 't' }, statusValue)
  .map((part) => part.text).join('\n')
check(/图桶: clean 1图\/1caption/.test(statusText), `anima_status 渲染出图桶行（${JSON.stringify(statusText.split('\n')[1] ?? '')}）`)
check(/caption 健康度/.test(statusText), 'anima_status 仍带 caption 健康度')
rmSync(tmpHome, { recursive: true, force: true })

console.log(errors.length === 0
  ? '\n[params] ALL OK'
  : `\n[params] ${errors.length} 项失败:\n- ${errors.join('\n- ')}`)
process.exit(errors.length === 0 ? 0 : 1)
