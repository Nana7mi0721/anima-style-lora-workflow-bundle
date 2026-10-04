/**
 * dsh-anima-style-lora —— Anima 风格 LoRA 制作流水线的宿主插件。
 *
 * 干的是「重活」：下载、导入、去重、筛选、文字检测与修补、重排编号、
 * 洗标规则引擎、训练配置三件套。判断类的工作（看图补标、风格取舍）
 * 交给同一 bundle 里的 skills 与视觉子代理。
 *
 * 同时提供一个设置页：宿主半侧在这里注册 `/anima-lora/api` 路由（读写本机
 * 运行时配置），浏览器半侧在 `lib/client.js` 往「设置」里加一页。工具仍然是
 * 主入口，设置页只是为了不用手改 JSON。
 */

import path from 'node:path'
import { PACKAGE_ROOT } from './run.js'
import { registerAnimaTools } from './tools.js'
import { createSettingsHandler, ROUTE_PATH } from './settings.js'

export const name = 'anima-style-lora'

// 注意：这里刻意不导出 Schemastery 的 Config。
// bundle 的模块解析走不到 profile 的 node_modules，静态 import
// '@deepseek-ai/schemastery' 会让整个模块图实例化失败（loader 报
// "failed to import"、fiber 从未创建）。默认值全部由下面的 resolve() 补齐，
// 配置从 cordis.patch.yml 的行 config 注入。

export function apply(ctx, config) {
  config = config ?? {}
  const resolve = () => {
    const home = config.home || 'E:\\LoRA_Train'
    return {
      home,
      pythonDir: config.pythonDir || path.join(PACKAGE_ROOT, 'python'),
      python: config.python || '',
      mlPython: config.mlPython || '',
      runtimeDir: config.runtimeDir || path.join(home, '.animasl'),
      animaLoraDir: config.animaLoraDir || path.join(home, 'anima_lora'),
      timeoutMs: Number(config.timeoutMs) || 600000,
      longTimeoutMs: Number(config.longTimeoutMs) || 2400000,
    }
  }

  ctx.inject(['tools'], (sctx) => {
    sctx.effect(() => {
      const disposers = registerAnimaTools(sctx, { resolve })
      ctx.logger?.info?.(`anima-style-lora: registered ${disposers.length} tools (home=${resolve().home})`)
      return () => {
        for (const dispose of disposers) {
          try {
            dispose()
          } catch {}
        }
      }
    }, 'anima-style-lora tools')
  })

  // 设置页后端：DSH 的 settings RPC 不服务第三方命名空间，所以自建路由。
  // 信任栅栏的 trustedHosts 在请求时现取（webRuntime 不一定存在，取不到就
  // 只放行 loopback —— 那正是桌面端的常态）。
  const trustedHosts = () => {
    try {
      const runtime = ctx.webRuntime ?? (typeof ctx.get === 'function' ? ctx.get('webRuntime') : undefined)
      const hosts = runtime && runtime.trustedHosts
      return Array.isArray(hosts) ? hosts : []
    } catch {
      return []
    }
  }

  ctx.inject(['webServer'], (sctx) => {
    sctx.effect(() => {
      const handler = createSettingsHandler({ config: resolve }, { trustedHosts })
      const dispose = sctx.webServer.register({ kind: 'prefix', path: ROUTE_PATH, handler })
      ctx.logger?.info?.(`anima-style-lora: settings API at ${ROUTE_PATH}`)
      return () => {
        try {
          dispose()
        } catch {}
      }
    }, 'anima-style-lora settings route')
  })
}
