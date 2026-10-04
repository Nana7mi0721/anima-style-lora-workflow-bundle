/**
 * 本地冒烟：不经过 DSH 运行期，把插件装进一个假 ctx，校验四个工具的契约，
 * 并真跑一遍只读/干跑的阶段。
 *
 *   node --import ./test/stub/register.mjs test/smoke.mjs           # 契约 + 快阶段
 *   node --import ./test/stub/register.mjs test/smoke.mjs --live    # 额外跑 text 干跑（慢）
 *
 * 校验点（对应过去踩过的坑）：
 *   1. 注册数量与命名唯一；
 *   2. 返回值里不能有 undefined（tool runtime 会拒收 "value" must be a lossless JSON object）；
 *   3. 返回值必须满足自己声明的 output.schema（additionalProperties:false + 类型）；
 *   4. render() 必须给出非空 text；
 *   5. apply 的 disposer 能干净卸载。
 */
import { existsSync } from 'node:fs'
import path from 'node:path'
import { apply, name } from '../lib/index.js'

const live = process.argv.includes('--live')
const home = process.env.ANIMASL_TEST_HOME ?? 'E:/LoRA_Train'
const testDataset = process.env.ANIMASL_TEST_DATASET ?? '_smoke'
const hasDataset = existsSync(path.join(home, 'datasets', testDataset))
const defs = new Map()
const logs = []
const errors = []

function check(ok, message) {
  if (!ok) errors.push(message)
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${message}`)
}

// ---- 假 ctx ----------------------------------------------------------------

const sctx = {
  effectCalls: [],
  tools: {
    register(def) {
      if (defs.has(def.name)) throw new Error('duplicate tool name: ' + def.name)
      defs.set(def.name, def)
      return () => defs.delete(def.name)
    },
  },
  effect(fn) {
    const cleanup = fn()
    sctx.effectCalls.push(cleanup)
    return cleanup
  },
}

// webServer 半侧的假 ctx：只记录注册了什么路由
const wctx = {
  routes: [],
  webServer: {
    register(route) {
      if (wctx.routes.some((item) => item.kind === route.kind && item.path === route.path)) {
        throw new Error(`duplicate ${route.kind} route "${route.path}"`)
      }
      wctx.routes.push(route)
      return () => {
        wctx.routes = wctx.routes.filter((item) => item !== route)
      }
    },
  },
  effect(fn) {
    return fn()
  },
}

const ctx = {
  inject(keys, fn) {
    const known = ['tools', 'webServer']
    for (const key of keys) {
      if (!known.includes(key)) throw new Error('unexpected inject: ' + keys.join(','))
    }
    if (keys.includes('tools')) fn(sctx)
    if (keys.includes('webServer')) fn(wctx)
  },
  get() {
    return undefined
  },
  logger: { info: (message) => logs.push(message) },
}

apply(ctx, { home, animaLoraDir: path.join(home, 'anima_lora') })

// ---- 契约 ------------------------------------------------------------------

const EXPECTED = ['anima_status', 'anima_stage', 'anima_job', 'anima_doctor']
check(defs.size === EXPECTED.length, `registered ${defs.size} tools (expect ${EXPECTED.length})`)
for (const tool of EXPECTED) check(defs.has(tool), `tool ${tool} present`)
check(
  logs.some((line) => line.includes('registered 4 tools')),
  `apply logged the tool registration: ${logs.join(' | ') || '(none)'}`,
)

// 设置路由：恰好一条 prefix 路由，路径与浏览器半侧一致
check(wctx.routes.length === 1, `registered ${wctx.routes.length} web route (expect 1)`)
check(wctx.routes[0]?.kind === 'prefix' && wctx.routes[0]?.path === '/anima-lora/api', `route path: ${wctx.routes[0]?.kind} ${wctx.routes[0]?.path}`)
check(typeof wctx.routes[0]?.handler === 'function', 'route handler is a function')

function scanUndefined(value, where) {
  if (value === undefined) return [`${where}: undefined`]
  if (Array.isArray(value)) return value.flatMap((item, i) => scanUndefined(item, `${where}[${i}]`))
  if (value && typeof value === 'object') {
    return Object.entries(value).flatMap(([key, item]) => scanUndefined(item, `${where}.${key}`))
  }
  return []
}

function scanSchema(value, schema, where) {
  if (value === undefined) return [`${where}: undefined`]
  if (value === null || schema === undefined) return []
  const type = schema.type
  const bad = (message) => [`${where}: ${message}`]
  if (type === 'string') return typeof value === 'string' ? [] : bad(`expect string, got ${typeof value}`)
  if (type === 'number') return typeof value === 'number' ? [] : bad(`expect number, got ${typeof value}`)
  if (type === 'boolean') return typeof value === 'boolean' ? [] : bad(`expect boolean, got ${typeof value}`)
  if (type === 'object') {
    if (typeof value !== 'object' || Array.isArray(value)) return bad('expect object')
    const props = schema.properties ?? {}
    const found = []
    for (const key of Object.keys(value)) {
      if (schema.additionalProperties === false && !(key in props)) found.push(`${where}.${key}: not in schema`)
      if (key in props) found.push(...scanSchema(value[key], props[key], `${where}.${key}`))
    }
    for (const key of schema.required ?? []) {
      if (!(key in value)) found.push(`${where}.${key}: missing required`)
    }
    return found
  }
  return []
}

async function run(tool, args, label) {
  const def = defs.get(tool)
  let value
  try {
    value = await def.execute(args, { signal: undefined })
  } catch (error) {
    check(false, `${label} threw: ${error.message}`)
    return undefined
  }
  const problems = [...scanUndefined(value, 'value'), ...scanSchema(value, def.output.schema, 'value')]
  check(problems.length === 0, `${label} value matches schema${problems.length ? ' -> ' + problems.join('; ') : ''}`)
  let rendered
  try {
    rendered = def.output.render(args, value)
  } catch (error) {
    check(false, `${label} render threw: ${error.message}`)
    return value
  }
  const text = Array.isArray(rendered) && rendered[0] && typeof rendered[0].text === 'string' ? rendered[0].text : ''
  check(rendered.length > 0 && text.length > 0, `${label} render text ${text.length} chars`)
  if (text) {
    const keep = value && value.ok === false ? 18 : 4
    console.log(text.split('\n').slice(0, keep).map((line) => '      | ' + line).join('\n'))
  }
  return value
}

// ---- 真跑（只读 / 干跑） ----------------------------------------------------

await run('anima_status', {}, 'status(all)')
await run('anima_doctor', {}, 'doctor')
await run('anima_job', {}, 'job(list)')
if (hasDataset) {
  await run('anima_status', { dataset: testDataset }, `status(${testDataset})`)
  await run('anima_stage', { stage: 'screen', dataset: testDataset }, 'stage(screen dry-run)')
  await run('anima_stage', { stage: 'review-list', dataset: testDataset, force: true }, 'stage(review-list, force 应被忽略)')
  await run('anima_stage', { stage: 'makecfg', dataset: testDataset, kind: 'style', trigger: '@smoketest' }, 'stage(makecfg dry-run)')
  if (live) {
    await run('anima_stage', { stage: 'text', dataset: testDataset, device: 'cuda' }, 'stage(text dry-run)')
  }
} else {
  console.log(`skip  dataset cases (没有 ${path.join(home, 'datasets', testDataset)}；用 ANIMASL_TEST_DATASET 指一个已有数据集)`)
}

// 负例：缺 dataset 必须抛
let threw = ''
try {
  await defs.get('anima_stage').execute({ stage: 'screen' }, {})
} catch (error) {
  threw = error.message
}
check(threw === 'dataset is required', `stage without dataset throws (${threw || 'no error'})`)

// ---- 卸载：effect 的清理函数必须能摘掉全部工具 --------------------------------

const dispose = sctx.effectCalls?.[0]
if (typeof dispose === 'function') {
  dispose()
  check(defs.size === 0, `disposer removed every tool (${defs.size} left)`)
} else {
  check(false, 'effect cleanup was not captured')
}

console.log(`\n${errors.length === 0 ? 'ALL OK' : errors.length + ' FAILURES'}`)
if (errors.length) {
  for (const error of errors) console.log(' - ' + error)
  process.exitCode = 1
}
