/**
 * anima-style-lora — 设置页的后端：一个自建 HTTP 路由 + 运行时配置文件的读写。
 *
 * 为什么自建路由：DSH 的 settings RPC 只服务宿主自己的命名空间，第三方插件
 * 的配置要落到自己的路由上（dsh-better-sidebar 的注释写明了这一点）。本插件
 * 的宿主半侧因此注入 `webServer`，在 `/anima-lora/api` 上暴露四个方法：
 *
 *   read   -> 路径 + 各配置层（defaults / bundle / runtime / env / merged）
 *   write  -> 把 `set` / `unset` 合并进运行时配置文件（原子写）
 *   probe  -> 批量探测路径是否存在（前端用来提示"这个目录不存在"）
 *   doctor -> 跑一遍 `anima doctor`，把输出原样交给前端展示
 *
 * 写的是 `<runtimeDir>/animasl.config.json` —— Python 侧 `config.layers()` 的
 * runtime 层，下一个 `anima_*` 调用就读到，**不需要重启 DSH**。
 *
 * 安全边界：路由只接受 loopback / 已信任 authority 的请求，并拒绝跨站浏览器
 * 标记（DNS-rebinding 防御）。完整实现抄自 @deepseek-ai/dsh-client-connection
 * 的 api-request-trust.ts + loopback-hostname.ts（BSD-3-Clause；该包不导出这
 * 些 helper，插件也不该依赖它的内部实现）。
 */
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { spawn } from 'node:child_process'
import { resolvePython } from './run.js'

/** 路由前缀：`/anima-lora/api/<method>`。 */
export const ROUTE_PATH = '/anima-lora/api'

/** 请求体上限（配置全是小字段，256 KB 足够）。 */
const MAX_BODY = 256 * 1024

/** 前端不该写的键：home 由插件行配置 + `--home` 参数决定，写了只会误导。 */
const READ_ONLY_KEYS = new Set(['home'])

// ---------------------------------------------------------------- 配置文件

/** 运行时配置文件路径（Python 的 runtime 层）。 */
export function runtimeConfigPath(runtime) {
  return join(runtime.config().runtimeDir, 'animasl.config.json')
}

/** 读运行时配置文件；不存在或坏掉时返回空对象（前端仍应能打开）。 */
export function readRuntimeFile(runtime) {
  try {
    const text = readFileSync(runtimeConfigPath(runtime), 'utf8')
    const parsed = JSON.parse(text)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch {
    return {}
  }
}

/** 原子写：先写临时文件再 rename，避免半截 JSON 被 Python 读到。 */
export function writeRuntimeFile(runtime, data) {
  const file = runtimeConfigPath(runtime)
  mkdirSync(dirname(file), { recursive: true })
  const tmp = `${file}.tmp`
  writeFileSync(tmp, `${JSON.stringify(data, null, 2)}\n`, 'utf8')
  renameSync(tmp, file)
  return file
}

/** 按点分路径写值（`screen.min_short_side`）。 */
function setPath(root, dotted, value) {
  const parts = dotted.split('.').filter(Boolean)
  if (parts.length === 0) return
  let node = root
  for (const part of parts.slice(0, -1)) {
    if (typeof node[part] !== 'object' || node[part] === null || Array.isArray(node[part])) node[part] = {}
    node = node[part]
  }
  node[parts[parts.length - 1]] = value
}

/** 按点分路径删值，并顺手清掉因此变空的中间对象（不留 `"wash": {}`）。 */
function unsetPath(root, dotted) {
  const parts = dotted.split('.').filter(Boolean)
  if (parts.length === 0) return
  const chain = []
  let node = root
  for (const part of parts.slice(0, -1)) {
    if (typeof node[part] !== 'object' || node[part] === null || Array.isArray(node[part])) return
    chain.push([node, part])
    node = node[part]
  }
  delete node[parts[parts.length - 1]]
  for (let i = chain.length - 1; i >= 0; i -= 1) {
    const [parent, key] = chain[i]
    const child = parent[key]
    if (child && typeof child === 'object' && !Array.isArray(child) && Object.keys(child).length === 0) {
      delete parent[key]
    } else {
      break
    }
  }
}

/**
 * 把一次保存合并进当前运行时配置。
 * @param current - 现有运行时配置（会被复制，不原地改）。
 * @param patch - `{ set?: Record<string, unknown>, unset?: string[] }`。
 * @returns `{ next, applied, removed, refused }`；refused 是拒绝写入的只读键。
 */
export function applyPatch(current, patch) {
  const next = JSON.parse(JSON.stringify(current || {}))
  const applied = []
  const removed = []
  const refused = []
  const sets = (patch && patch.set) || {}
  for (const [key, value] of Object.entries(sets)) {
    const top = key.split('.')[0]
    if (READ_ONLY_KEYS.has(top)) {
      refused.push(key)
      continue
    }
    if (value === undefined) continue
    setPath(next, key, value)
    applied.push(key)
  }
  for (const key of (patch && patch.unset) || []) {
    if (typeof key !== 'string' || key.length === 0) continue
    if (READ_ONLY_KEYS.has(key.split('.')[0])) {
      refused.push(key)
      continue
    }
    unsetPath(next, key)
    removed.push(key)
  }
  return { next, applied, removed, refused }
}

// ------------------------------------------------------------------ 子进程

/** 跑 animasl CLI 用的环境（与 stages 一致：PYTHONPATH / ANIMASL_HOME / ANIMASL_RUNTIME）。 */
export function cliEnv(runtime) {
  const config = runtime.config()
  return {
    ...process.env,
    PYTHONPATH: [config.pythonDir, process.env.PYTHONPATH].filter(Boolean).join(
      process.platform === 'win32' ? ';' : ':',
    ),
    PYTHONIOENCODING: 'utf-8',
    PYTHONUNBUFFERED: '1',
    ANIMASL_HOME: config.home,
    ANIMASL_RUNTIME: config.runtimeDir,
  }
}

/**
 * 一次性跑一个 CLI 子命令并收全部输出（不走 job 系统，几秒内返回）。
 * @param runtime - 插件运行时（提供 config()）。
 * @param argv - `animasl.cli` 之后的参数。
 * @param timeoutMs - 超时后杀掉子进程。
 */
export function runCli(runtime, argv, timeoutMs = 120000) {
  const config = runtime.config()
  const python = resolvePython(runtime)
  return new Promise((resolve) => {
    let child
    try {
      child = spawn(python, ['-X', 'utf8', '-m', 'animasl.cli', '--home', config.home, ...argv], {
        cwd: config.pythonDir,
        env: cliEnv(runtime),
        windowsHide: true,
      })
    } catch (error) {
      resolve({ code: -1, stdout: '', stderr: String(error && error.message ? error.message : error) })
      return
    }
    let stdout = ''
    let stderr = ''
    let done = false
    const finish = (result) => {
      if (done) return
      done = true
      clearTimeout(timer)
      resolve(result)
    }
    const timer = setTimeout(() => {
      try {
        child.kill()
      } catch {
        /* 已经退出了 */
      }
      finish({ code: -2, stdout, stderr: `${stderr}\n[settings] 命令超时（${timeoutMs} ms）` })
    }, timeoutMs)
    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString('utf8')
    })
    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString('utf8')
    })
    child.on('error', (error) => finish({ code: -1, stdout, stderr: `${stderr}\n${error.message}` }))
    child.on('close', (code) => finish({ code: code === null ? -1 : code, stdout, stderr }))
  })
}

// -------------------------------------------------------------- 路由与栅栏

/** 规范化 authority：写了端口就带端口，否则只有 hostname。 */
function parseAuthority(authority) {
  try {
    return new URL(`http://${authority}`)
  } catch {
    return undefined
  }
}

/** 是否 loopback：localhost / [::1] / 127.x.x.x。 */
export function isLoopbackHostname(hostname) {
  if (hostname === 'localhost' || hostname === '[::1]') return true
  const parts = hostname.split('.')
  return parts.length === 4 && parts[0] === '127' && parts.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255)
}

function canonicalAuthority(entry, entryUrl) {
  const port = entryUrl.port !== '' ? entryUrl.port : new URL(`https://${entry}`).port
  return port === '' ? entryUrl.hostname : `${entryUrl.hostname}:${port}`
}

function isTrustedAuthority(hostUrl, trustedHosts) {
  return (trustedHosts || []).some((entry) => {
    const entryUrl = parseAuthority(entry)
    if (entryUrl === undefined) return false
    return canonicalAuthority(entry, entryUrl) === entryUrl.hostname
      ? entryUrl.hostname === hostUrl.hostname
      : entryUrl.host === hostUrl.host
  })
}

/**
 * 浏览器信任栅栏：Host 必须是 loopback 或已信任 authority，跨站标记一律拒。
 * 无 Origin 也可以（Host 栅栏已经绑定过 authority）。
 */
export function isTrustedApiRequest(request, trustedHosts) {
  const header = (name) => {
    const value = request && request.headers ? request.headers[name] : undefined
    return typeof value === 'string' ? value : undefined
  }
  const host = header('host')
  if (!host) return false
  const hostUrl = parseAuthority(host)
  if (hostUrl === undefined) return false
  if (!isLoopbackHostname(hostUrl.hostname) && !isTrustedAuthority(hostUrl, trustedHosts)) return false
  if (header('sec-fetch-site') === 'cross-site') return false
  const origin = header('origin')
  if (origin === undefined) return true
  try {
    return new URL(origin).hostname === hostUrl.hostname
  } catch {
    return false
  }
}

/** 读 JSON 请求体（带上限；坏 JSON 返回 null；超限返回 oversize 标记）。 */
function readBody(req) {
  return new Promise((resolve) => {
    let size = 0
    let done = false
    const chunks = []
    req.on('data', (chunk) => {
      if (done) return
      size += chunk.length
      if (size > MAX_BODY) {
        done = true
        // 不 destroy：先把剩余请求体抽干再回 413，否则客户端会拿到 ECONNRESET 而不是响应
        req.resume()
        resolve({ oversize: true })
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      const text = Buffer.concat(chunks).toString('utf8').trim()
      if (text.length === 0) {
        resolve({})
        return
      }
      try {
        const parsed = JSON.parse(text)
        resolve(parsed && typeof parsed === 'object' ? parsed : null)
      } catch {
        resolve(null)
      }
    })
    req.on('error', () => resolve(null))
  })
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body),
  })
  res.end(body)
}

const ok = (value) => ({ ok: true, value })
const fail = (code, message) => ({ ok: false, error: { code, message } })

// ------------------------------------------------------------------- 各方法

/** read：路径 + 各配置层。Python 读不到时降级成"只有路径 + 运行时文件"。 */
async function methodRead(runtime) {
  const config = runtime.config()
  const runtimeFile = runtimeConfigPath(runtime)
  const bundleFile = join(config.pythonDir, '..', 'animasl.config.json')
  const paths = {
    home: config.home,
    runtimeDir: config.runtimeDir,
    runtimeConfig: runtimeFile,
    runtimeConfigExists: existsSync(runtimeFile),
    bundleConfig: bundleFile,
    bundleRoot: join(config.pythonDir, '..'),
    python: resolvePython(runtime),
  }
  const result = await runCli(runtime, ['config', '--json'], 60000)
  if (result.code !== 0) {
    return {
      paths,
      runtime: readRuntimeFile(runtime),
      layers: null,
      cliError: (result.stderr || result.stdout || `exit ${result.code}`).trim().split('\n').slice(-4).join('\n'),
    }
  }
  try {
    const parsed = JSON.parse(result.stdout)
    return { paths, runtime: parsed.layers.runtime || {}, layers: parsed.layers, cliError: null }
  } catch (error) {
    return {
      paths,
      runtime: readRuntimeFile(runtime),
      layers: null,
      cliError: `无法解析 config --json 的输出：${error.message}`,
    }
  }
}

/** write：把补丁合并进运行时配置文件。 */
function methodWrite(runtime, payload) {
  const current = readRuntimeFile(runtime)
  const { next, applied, removed, refused } = applyPatch(current, payload)
  if (applied.length === 0 && removed.length === 0 && refused.length > 0) {
    return fail('read-only-key', `这些键由插件行配置决定，不能在这里改：${refused.join(', ')}`)
  }
  const file = writeRuntimeFile(runtime, next)
  return ok({ file, applied, removed, refused, runtime: next })
}

/** probe：批量探测路径是否存在（前端用来标"这个路径不存在"）。 */
function methodProbe(payload) {
  const paths = Array.isArray(payload && payload.paths) ? payload.paths.slice(0, 64) : []
  const exists = {}
  for (const path of paths) {
    if (typeof path !== 'string' || path.length === 0) continue
    exists[path] = existsSync(path)
  }
  return ok({ exists })
}

/** doctor：跑体检，把输出交给前端（可以是几十秒的活，前端自己转圈）。 */
async function methodDoctor(runtime) {
  const result = await runCli(runtime, ['doctor'], 180000)
  return ok({ exitCode: result.code, output: `${result.stdout}${result.stderr}`.trim() })
}

/**
 * 造路由处理器。`runtime` 传的是插件那个 { config() } 对象。
 * @param runtime - 插件运行时。
 * @param options - `{ trustedHosts?: () => string[] }`，默认从 webRuntime 取。
 */
export function createSettingsHandler(runtime, options = {}) {
  return async function handle(req, res) {
    const trusted = typeof options.trustedHosts === 'function' ? options.trustedHosts() : options.trustedHosts
    if (!isTrustedApiRequest(req, trusted || [])) {
      sendJson(res, 403, fail('forbidden', '请求不是来自本机的 DSH 页面。'))
      return
    }
    const url = new URL(req.url || '/', 'http://localhost')
    const method = url.pathname.slice(ROUTE_PATH.length).replace(/^\/+/, '').replace(/\/+$/, '')
    if (method === '') {
      sendJson(res, 404, fail('not-found', `缺少方法名：${ROUTE_PATH}/<read|write|probe|doctor>`))
      return
    }
    if (req.method !== 'POST') {
      sendJson(res, 405, fail('method-not-allowed', '设置接口只接受 POST。'))
      return
    }
    const payload = await readBody(req)
    if (payload === null) {
      sendJson(res, 400, fail('bad-request', '请求体不是合法 JSON。'))
      return
    }
    if (payload.oversize === true) {
      sendJson(res, 413, fail('too-large', `请求体超过 ${Math.floor(MAX_BODY / 1024)} KB。`))
      return
    }
    try {
      let answer
      if (method === 'read') answer = ok(await methodRead(runtime))
      else if (method === 'write') answer = methodWrite(runtime, payload)
      else if (method === 'probe') answer = methodProbe(payload)
      else if (method === 'doctor') answer = await methodDoctor(runtime)
      else {
        sendJson(res, 404, fail('not-found', `未知方法：${method}`))
        return
      }
      sendJson(res, answer.ok ? 200 : 400, answer)
    } catch (error) {
      sendJson(res, 500, fail('internal', String(error && error.message ? error.message : error)))
    }
  }
}
