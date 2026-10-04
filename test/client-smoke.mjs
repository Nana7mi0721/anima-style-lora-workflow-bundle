/**
 * 浏览器半侧（lib/client.js）的离线冒烟测试。
 *
 * 本机没有 react（客户端 bundle 的 react 由 web shell 的平台模块表提供），
 * 所以这里用一个只实现本插件用到的那几个 hook 的极简 React 替身，把设置页
 * 真的渲染出来，再模拟输入/点击，断言发出去的 HTTP 请求。
 *
 * 用法：node test/client-smoke.mjs
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const BUNDLE_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const failures = []
function check(label, condition, detail = '') {
  if (condition) {
    console.log(`  ok   ${label}`)
  } else {
    failures.push(`${label}${detail ? ` — ${detail}` : ''}`)
    console.log(`  FAIL ${label}${detail ? ` — ${detail}` : ''}`)
  }
}

// ------------------------------------------------------------------ 极简 React
function createLiteReact() {
  const store = new Map()
  const effects = []
  let current = null
  let dirty = false
  const slot = (path) => {
    if (!store.has(path)) store.set(path, { hooks: [], cursor: 0 })
    return store.get(path)
  }
  const React = {
    createElement(type, props, ...children) {
      const flat = []
      const push = (value) => {
        if (Array.isArray(value)) value.forEach(push)
        else if (value !== null && value !== undefined && typeof value !== 'boolean') flat.push(value)
      }
      children.forEach(push)
      return { type, props: props || {}, children: flat }
    },
    useState(initial) {
      const s = current
      const index = s.cursor++
      if (!(index in s.hooks)) s.hooks[index] = typeof initial === 'function' ? initial() : initial
      const set = (next) => {
        s.hooks[index] = typeof next === 'function' ? next(s.hooks[index]) : next
        dirty = true
      }
      return [s.hooks[index], set]
    },
    useCallback(fn) {
      const s = current
      const index = s.cursor++
      if (!(index in s.hooks)) s.hooks[index] = fn
      return s.hooks[index]
    },
    useEffect(fn) {
      const s = current
      const index = s.cursor++
      if (!(index in s.hooks)) {
        s.hooks[index] = true
        effects.push(fn)
      }
    },
  }
  function tree(node, path) {
    if (node === null || node === undefined || typeof node === 'boolean') return null
    if (Array.isArray(node)) return node.map((child, index) => tree(child, `${path}[${index}]`))
    if (typeof node === 'string' || typeof node === 'number') return { text: String(node) }
    if (typeof node.type === 'function') {
      const key = `${path}/${node.type.name || 'anon'}`
      const saved = current
      current = slot(key)
      current.cursor = 0
      let out
      try {
        out = node.type(node.props)
      } finally {
        current = saved
      }
      return tree(out, key)
    }
    return {
      type: node.type,
      props: node.props,
      children: node.children.map((child, index) => tree(child, `${path}/${index}`)),
    }
  }
  return {
    React,
    render(Component) {
      let guard = 0
      do {
        dirty = false
        current = slot('root')
        current.cursor = 0
        var view = tree(React.createElement(Component, {}), 'root')
        while (effects.length > 0) {
          const fn = effects.shift()
          const cleanup = fn()
          void cleanup
          if (dirty) break
        }
        guard += 1
      } while ((dirty || effects.length > 0) && guard < 40)
      return view
    },
  }
}

function collect(node, predicate, out = []) {
  if (!node) return out
  if (node.props && predicate(node)) out.push(node)
  for (const child of node.children || []) collect(child, predicate, out)
  return out
}
const byClass = (node, className) =>
  collect(node, (item) => String(item.props.className || '').includes(className))
const allText = (node) => {
  if (!node) return ''
  if (node.text !== undefined) return node.text
  return (node.children || []).map(allText).join('')
}

// ------------------------------------------------------------------ 假环境
const calls = []
const RESPONSES = {
  read: {
    paths: {
      home: 'E:/LoRA_Train',
      runtimeDir: 'E:/LoRA_Train/.animasl',
      runtimeConfig: 'E:/LoRA_Train/.animasl/animasl.config.json',
      runtimeConfigExists: false,
      bundleConfig: 'E:/bundle/animasl.config.json',
      bundleRoot: 'E:/bundle',
      python: 'E:/bundle/python/.venv/Scripts/python.exe',
    },
    runtime: {},
    cliError: null,
    layers: {
      defaults: { home: 'E:/LoRA_Train', wash: { min_tags: 20, max_tags: 45 } },
      bundle: { home: 'E:/LoRA_Train' },
      runtime: {},
      env: {},
      merged: {
        home: 'E:/LoRA_Train',
        datasets_dir: 'E:/LoRA_Train/datasets',
        tag_dict: 'E:/LoRA_Train/tags.json',
        proxy_candidates: ['', 'http://127.0.0.1:7897'],
        'screen.min_short_side': 512,
        'wash.min_tags': 20,
        'wash.category_drop': [1, 3, 5],
      },
    },
  },
  write: { file: 'E:/LoRA_Train/.animasl/animasl.config.json', applied: ['wash.min_tags'], removed: [], refused: [], runtime: {} },
  probe: { exists: { 'E:/LoRA_Train/tags.json': true, 'E:/LoRA_Train/datasets': false } },
  doctor: { exitCode: 0, output: '[doctor] OK' },
}

let loaded = null
globalThis.window = {
  __ModuleLoader__: {
    load(definition) {
      loaded = definition
    },
  },
}
// Node 24 把 navigator 定义成只读 getter，只能 defineProperty
Object.defineProperty(globalThis, 'navigator', { value: { language: 'zh-CN' }, configurable: true })
globalThis.document = {
  head: { appendChild() {} },
  createElement: () => ({ setAttribute() {}, remove() {}, textContent: '' }),
}
globalThis.fetch = async (url, options) => {
  const method = String(url).split('/').pop()
  calls.push({ method, payload: JSON.parse((options && options.body) || '{}') })
  const value = RESPONSES[method]
  if (!value) return { status: 404, json: async () => ({ ok: false, error: { message: 'not found' } }) }
  return { status: 200, json: async () => ({ ok: true, value }) }
}

// ------------------------------------------------------------------ 跑测试
console.log('[client-smoke] 加载 lib/client.js')
const source = readFileSync(join(BUNDLE_ROOT, 'lib', 'client.js'), 'utf8')
// eslint-disable-next-line no-new-func
new Function(source)()
check('bundle 注册到 __ModuleLoader__', Boolean(loaded))
check('id 等于包名', loaded && loaded.id === 'dsh-anima-style-lora', loaded && loaded.id)

const lite = createLiteReact()
const required = []
const mod = loaded.factory((specifier) => {
  required.push(specifier)
  if (specifier === 'react') return lite.React
  throw new Error(`不允许 require：${specifier}`)
})
check('只 require 平台 seed 的模块', required.length === 1 && required[0] === 'react', required.join(', '))
check('导出 apply()', mod && typeof mod.apply === 'function')
check('inject 含 slots', Array.isArray(mod.inject) && mod.inject.includes('slots'), JSON.stringify(mod.inject))

const registrations = []
const effects = []
const ctx = {
  effect(fn, label) {
    effects.push({ fn, label })
    return () => {}
  },
  slots: {
    inject(name, callback) {
      registrations.push({ name })
      return callback()
    },
    register(definition, component) {
      registrations[registrations.length - 1] = { name: definition.name, definition, component }
      return () => {}
    },
  },
}
mod.apply(ctx)
check('注册了 settings.section', registrations.some((item) => item.name === 'settings.section'))
const entry = registrations.find((item) => item.name === 'settings.section')
check('槽位 id/order/label 齐备', Boolean(entry && entry.definition.id === 'anima-style-lora' && entry.definition.order === 100 && typeof entry.definition.label() === 'string'))
check('注册了样式与字典两个 effect', effects.length >= 2, `${effects.length}`)
for (const effect of effects) effect.fn()

console.log('[client-smoke] 渲染设置页')
let view = lite.render(entry.component)
await new Promise((resolve) => setTimeout(resolve, 0))
// 初次渲染会 useState(loading) → effect 里 load() → 再渲染
let guard = 0
while (allText(view).includes('正在读取配置') && guard < 20) {
  await new Promise((resolve) => setTimeout(resolve, 0))
  view = lite.render(entry.component)
  guard += 1
}

check('首个请求是 read', calls.length > 0 && calls[0].method === 'read', JSON.stringify(calls[0] || null))
check('渲染出分组卡', byClass(view, 'asl-group').length >= 6, `${byClass(view, 'asl-group').length}`)
check('渲染出输入框', byClass(view, 'asl-input').length >= 10, `${byClass(view, 'asl-input').length}`)
check('渲染出只读行（home / runtime）', allText(view).includes('E:/LoRA_Train/.animasl'))
check('路径探查请求发出', calls.some((call) => call.method === 'probe'))
check('标出存在的词典', allText(view).includes('存在'))
check('写入文件路径展示出来', allText(view).includes('animasl.config.json'))

console.log('[client-smoke] 编辑并保存')
const washField = collect(view, (item) => {
  const text = allText(item)
  return String(item.props.className || '').includes('asl-row') && text.includes('wash.min_tags')
})[0]
check('找到 wash.min_tags 行', Boolean(washField), washField ? '' : '未渲染出该行')
const input = collect(washField, (item) => item.type === 'input')[0]
check('该行有输入框', Boolean(input))
input.props.onInput({ target: { value: '30' } })
const saveButton = collect(view, (item) => item.type === 'button' && String(item.props.className).includes('asl-save'))[0]
// 编辑后需要重新渲染才能拿到可点的保存按钮（disabled 由 dirty 决定）
view = lite.render(entry.component)
const saveAfter = collect(view, (item) => item.type === 'button' && String(item.props.className).includes('asl-save'))[0]
check('保存按钮出现', Boolean(saveAfter))
check('有未保存改动时按钮可用', saveAfter && saveAfter.props.disabled === false, JSON.stringify(saveAfter && saveAfter.props.disabled))
void saveButton
await saveAfter.props.onClick()
const writeCall = calls.filter((call) => call.method === 'write').pop()
check('发出 write 请求', Boolean(writeCall))
check('只写了被编辑的键', writeCall && JSON.stringify(writeCall.payload.set) === JSON.stringify({ 'wash.min_tags': 30 }), JSON.stringify(writeCall && writeCall.payload))
check('保存后重新读配置', calls.filter((call) => call.method === 'read').length >= 2, `${calls.filter((call) => call.method === 'read').length}`)

console.log('[client-smoke] 恢复默认与体检')
view = lite.render(entry.component)
const taggedRow = collect(view, (item) => {
  const text = allText(item)
  return String(item.props.className || '').includes('asl-row') && text.includes('wash.category_drop')
})[0]
const resetButton = collect(taggedRow, (item) => item.type === 'button' && String(item.props.className).includes('asl-reset'))[0]
check('找到 wash.category_drop 的恢复按钮', Boolean(resetButton))
void resetButton
const doctorButton = collect(view, (item) => item.type === 'button' && String(item.props.className).includes('asl-ghost'))[0]
check('找到体检按钮', Boolean(doctorButton))
await doctorButton.props.onClick()
check('发出 doctor 请求', calls.some((call) => call.method === 'doctor'))
view = lite.render(entry.component)
check('展示体检输出', allText(view).includes('[doctor] OK'))

console.log('[client-smoke] 凭据分组')
const findRow = (root, needle) =>
  collect(root, (item) => {
    const text = allText(item)
    return String(item.props.className || '').includes('asl-row') && text.includes(needle)
  })[0]
view = lite.render(entry.component)
const keyRow = findRow(view, 'danbooru_api_key')
check('渲染出凭据分组（danbooru_api_key 行）', Boolean(keyRow), keyRow ? '' : '未渲染出该行')
const keyInput = collect(keyRow, (item) => item.type === 'input')[0]
check('密钥默认是密码框', Boolean(keyInput) && keyInput.props.type === 'password', JSON.stringify(keyInput && keyInput.props.type))
const eye = collect(keyRow, (item) => item.type === 'button' && String(item.props.className).includes('asl-eye'))[0]
check('有显示/隐藏开关', Boolean(eye))
eye.props.onClick()
view = lite.render(entry.component)
const keyInput2 = collect(findRow(view, 'danbooru_api_key'), (item) => item.type === 'input')[0]
check('点开后变成明文框', Boolean(keyInput2) && keyInput2.props.type === 'text', JSON.stringify(keyInput2 && keyInput2.props.type))
keyInput2.props.onInput({ target: { value: 'kq-test-key' } })
view = lite.render(entry.component)
const credSave = collect(view, (item) => item.type === 'button' && String(item.props.className).includes('asl-save'))[0]
await credSave.props.onClick()
const credWrite = calls.filter((call) => call.method === 'write').pop()
check(
  '凭据走同一条 write 通路',
  credWrite && JSON.stringify(credWrite.payload.set) === JSON.stringify({ danbooru_api_key: 'kq-test-key' }),
  JSON.stringify(credWrite && credWrite.payload),
)

console.log('')
if (failures.length === 0) {
  console.log('[client-smoke] ALL OK')
} else {
  console.log(`[client-smoke] ${failures.length} 个失败：`)
  for (const item of failures) console.log(`  - ${item}`)
  process.exitCode = 1
}
