/**
 * 宿主半侧设置路由（lib/settings.js）的集成测试：
 * 真的起一个 http server，用真的 Python CLI 跑 read / write / probe / doctor，
 * 并逐条验证信任栅栏与错误分支。
 *
 * 写操作全部落在临时 runtime 目录，不碰用户真实的 <home>/.animasl。
 * 用法：node test/settings-route.mjs [--doctor]
 */
import { createServer, request as httpRequest } from 'node:http'
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createSettingsHandler, ROUTE_PATH } from '../lib/settings.js'

const BUNDLE_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const WITH_DOCTOR = process.argv.includes('--doctor')
const runtimeDir = mkdtempSync(join(tmpdir(), 'asl-route-'))
const configFile = join(runtimeDir, 'animasl.config.json')
const home = process.env.ANIMASL_TEST_HOME || 'E:/LoRA_Train'
const runtime = {
  config: () => ({
    home,
    pythonDir: join(BUNDLE_ROOT, 'python'),
    python: '',
    mlPython: '',
    runtimeDir,
    animaLoraDir: join(home, 'anima_lora'),
    timeoutMs: 600000,
    longTimeoutMs: 2400000,
  }),
}

const failures = []
function check(label, condition, detail = '') {
  if (condition) console.log(`  ok   ${label}`)
  else {
    failures.push(`${label}${detail ? ` — ${detail}` : ''}`)
    console.log(`  FAIL ${label}${detail ? ` — ${detail}` : ''}`)
  }
}

const handler = createSettingsHandler(runtime, { trustedHosts: ['localhost:19387'] })
const server = createServer(handler)
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const port = server.address().port
const url = `http://127.0.0.1:${port}${ROUTE_PATH}`

async function post(method, payload, headers = {}, path = ROUTE_PATH) {
  const target = `http://127.0.0.1:${port}${path}${method ? `/${method}` : ''}`
  const response = await fetch(target, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify(payload || {}),
  })
  let body = null
  try {
    body = await response.json()
  } catch {
    body = null
  }
  return { status: response.status, body }
}

console.log(`[settings-route] runtime=${runtimeDir}`)
console.log('[settings-route] read')
const read = await post('read', {})
check('read 返回 200 且 ok', read.status === 200 && read.body?.ok === true, JSON.stringify(read.body)?.slice(0, 300))
check('read 带路径表', Boolean(read.body?.value?.paths?.runtimeConfig === configFile), JSON.stringify(read.body?.value?.paths))
check('read 跑通 Python 分层', Boolean(read.body?.value?.layers?.merged), read.body?.value?.cliError || '')
check('runtime 层初始为空', JSON.stringify(read.body?.value?.layers?.runtime) === '{}', JSON.stringify(read.body?.value?.layers?.runtime))
check('默认值来自 defaults/bundle 层', Boolean(read.body?.value?.layers?.defaults?.wash))

console.log('[settings-route] write')
const write = await post('write', { set: { 'wash.min_tags': 30, 'screen.min_short_side': 640 }, unset: [] })
check('write 返回 200 且 ok', write.status === 200 && write.body?.ok === true, JSON.stringify(write.body)?.slice(0, 300))
check('write 报告 applied', JSON.stringify(write.body?.value?.applied) === JSON.stringify(['wash.min_tags', 'screen.min_short_side']), JSON.stringify(write.body?.value?.applied))
check('原子写盘产生文件', existsSync(configFile))
const written = JSON.parse(readFileSync(configFile, 'utf8'))
check('文件内容正确', written?.wash?.min_tags === 30 && written?.screen?.min_short_side === 640, JSON.stringify(written))

const readBack = await post('read', {})
check('重读后 runtime 层可见', readBack.body?.value?.layers?.runtime?.wash?.min_tags === 30, JSON.stringify(readBack.body?.value?.layers?.runtime))
check('合并层生效', readBack.body?.value?.layers?.merged?.wash?.min_tags === 30, JSON.stringify(readBack.body?.value?.layers?.merged?.wash))

console.log('[settings-route] unset')
const unset = await post('write', { set: {}, unset: ['wash.min_tags'] })
check('unset 返回 ok', unset.status === 200 && unset.body?.ok === true, JSON.stringify(unset.body)?.slice(0, 200))
check('unset 报告 removed', JSON.stringify(unset.body?.value?.removed) === JSON.stringify(['wash.min_tags']), JSON.stringify(unset.body?.value?.removed))
const afterUnset = JSON.parse(readFileSync(configFile, 'utf8'))
check('键被删掉', afterUnset?.wash?.min_tags === undefined, JSON.stringify(afterUnset))
check('同组其它键保留', afterUnset?.screen?.min_short_side === 640, JSON.stringify(afterUnset))
const afterUnsetAgain = await post('write', { set: {}, unset: ['screen.min_short_side'] })
check('清空后中间对象被剪掉', afterUnsetAgain.body?.ok === true && JSON.stringify(JSON.parse(readFileSync(configFile, 'utf8'))) === '{}', readFileSync(configFile, 'utf8').trim())

console.log('[settings-route] 只读键')
const refused = await post('write', { set: { home: 'E:/elsewhere' }, unset: [] })
check('只读键被拒', refused.body?.ok === false && refused.body?.error?.code === 'read-only-key', JSON.stringify(refused.body))
check('只读键没有落盘', JSON.stringify(JSON.parse(readFileSync(configFile, 'utf8'))) === '{}', readFileSync(configFile, 'utf8').trim())

console.log('[settings-route] probe')
const probe = await post('probe', { paths: [join(BUNDLE_ROOT, 'python'), join(BUNDLE_ROOT, 'does-not-exist')] })
check('probe 返回两个结果', Object.keys(probe.body?.value?.exists || {}).length === 2, JSON.stringify(probe.body?.value))
check('probe 判断正确', probe.body?.value?.exists?.[join(BUNDLE_ROOT, 'python')] === true && probe.body?.value?.exists?.[join(BUNDLE_ROOT, 'does-not-exist')] === false, JSON.stringify(probe.body?.value?.exists))

console.log('[settings-route] 信任栅栏与错误分支')
// fetch() 不允许自定义 Host 头（forbidden header），所以栅栏测试走裸 http
function rawRequest({ method = 'POST', path = `${ROUTE_PATH}/read`, headers = {}, body = '{}', target = port }) {
  return new Promise((resolve, reject) => {
    const req = httpRequest(
      { host: '127.0.0.1', port: target, method, path, headers: { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body), ...headers } },
      (res) => {
        let text = ''
        res.setEncoding('utf8')
        res.on('data', (chunk) => {
          text += chunk
        })
        res.on('end', () => {
          let parsed = null
          try {
            parsed = JSON.parse(text)
          } catch {
            parsed = null
          }
          resolve({ status: res.statusCode, body: parsed })
        })
      },
    )
    req.on('error', reject)
    req.end(body)
  })
}

const badHost = await rawRequest({ headers: { host: 'evil.example' } })
check('非 loopback Host 403', badHost.status === 403 && badHost.body?.error?.code === 'forbidden', JSON.stringify(badHost.body)?.slice(0, 160))
const badHostPort = await rawRequest({ headers: { host: 'evil.example:19387' } })
check('非 loopback Host（带端口）403', badHostPort.status === 403, JSON.stringify(badHostPort.body)?.slice(0, 160))
const localhostAnyPort = await rawRequest({ headers: { host: 'localhost:19388' } })
check('localhost 任意端口放行（本身就是 loopback）', localhostAnyPort.status === 200, JSON.stringify(localhostAnyPort.body)?.slice(0, 160))
const loopbackHost = await rawRequest({ headers: { host: '127.0.0.1:1' } })
check('loopback 任意端口放行', loopbackHost.status === 200 && loopbackHost.body?.ok === true, JSON.stringify(loopbackHost.body)?.slice(0, 160))

// trustedHosts 分支：用非 loopback 主机名另起一个 handler
const namedServer = createServer(createSettingsHandler(runtime, { trustedHosts: ['dsh.local:19387'] }))
await new Promise((resolve) => namedServer.listen(0, '127.0.0.1', resolve))
const namedPort = namedServer.address().port
const namedOk = await rawRequest({ headers: { host: 'dsh.local:19387' }, target: namedPort })
check('trustedHosts 命中（同名同端口）放行', namedOk.status === 200 && namedOk.body?.ok === true, JSON.stringify(namedOk.body)?.slice(0, 160))
const namedWrongPort = await rawRequest({ headers: { host: 'dsh.local:19388' }, target: namedPort })
check('trustedHosts 端口不符 403', namedWrongPort.status === 403, JSON.stringify(namedWrongPort.body)?.slice(0, 160))
const namedWrongName = await rawRequest({ headers: { host: 'other.local:19387' }, target: namedPort })
check('trustedHosts 名字不符 403', namedWrongName.status === 403, JSON.stringify(namedWrongName.body)?.slice(0, 160))
namedServer.close()

const crossSite = await rawRequest({ headers: { host: '127.0.0.1', 'sec-fetch-site': 'cross-site' } })
check('cross-site 403', crossSite.status === 403, JSON.stringify(crossSite.body)?.slice(0, 160))
const badOrigin = await rawRequest({ headers: { host: '127.0.0.1', origin: 'http://evil.example' } })
check('跨站 Origin 403', badOrigin.status === 403, JSON.stringify(badOrigin.body)?.slice(0, 160))
const okOrigin = await rawRequest({ headers: { host: `127.0.0.1:${port}`, origin: `http://127.0.0.1:${port}` } })
check('同源 Origin 放行', okOrigin.status === 200 && okOrigin.body?.ok === true, JSON.stringify(okOrigin.body)?.slice(0, 160))
const originNoPort = await rawRequest({ headers: { host: `127.0.0.1:${port}`, origin: 'http://127.0.0.1' } })
check('Origin 不带端口也放行（Edge 行为）', originNoPort.status === 200 && originNoPort.body?.ok === true, JSON.stringify(originNoPort.body)?.slice(0, 160))
const nullOrigin = await rawRequest({ headers: { host: '127.0.0.1', origin: 'null' } })
check('Origin: null 403', nullOrigin.status === 403, JSON.stringify(nullOrigin.body)?.slice(0, 160))

const getResponse = await rawRequest({ method: 'GET', headers: { host: '127.0.0.1' }, body: '' })
check('GET 405', getResponse.status === 405, `${getResponse.status}`)
const missing = await post('', {}, {}, '/anima-lora/api')
check('空方法名 404', missing.status === 404 && missing.body?.error?.code === 'not-found', JSON.stringify(missing.body))
const unknown = await post('nope', {})
check('未知方法 404', unknown.status === 404, JSON.stringify(unknown.body))
const badJson = await post('read', null, { 'content-type': 'application/json' })
check('空 body 也能处理', badJson.status === 200, `${badJson.status}`)
const brokenJson = await rawRequest({ body: '{not json' })
check('坏 JSON 400', brokenJson.status === 400, `${brokenJson.status}`)
const oversize = await rawRequest({ body: `{"x":"${'a'.repeat(300 * 1024)}"}` })
check('超长 body 413', oversize.status === 413 && oversize.body?.error?.code === 'too-large', `${oversize.status} ${JSON.stringify(oversize.body)?.slice(0, 120)}`)

if (WITH_DOCTOR) {
  console.log('[settings-route] doctor（跑真的 animasl doctor，约 1 分钟）')
  const doctor = await post('doctor', {})
  check('doctor 返回 0', doctor.body?.value?.exitCode === 0, JSON.stringify(doctor.body)?.slice(0, 400))
  check('doctor 输出含关键项', /trainer|koharu|tag dict/.test(doctor.body?.value?.output || ''), (doctor.body?.value?.output || '').slice(0, 200))
} else {
  console.log('[settings-route] 跳过 doctor（加 --doctor 才跑）')
}

server.close()
rmSync(runtimeDir, { recursive: true, force: true })

console.log('')
if (failures.length === 0) console.log('[settings-route] ALL OK')
else {
  console.log(`[settings-route] ${failures.length} 个失败：`)
  for (const item of failures) console.log(`  - ${item}`)
  process.exitCode = 1
}
