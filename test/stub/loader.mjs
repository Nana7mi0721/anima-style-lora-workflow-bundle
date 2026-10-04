/**
 * ESM 解析钩子：把裸说明符 @deepseek-ai/dsh-tools 指到本地替身，
 * 这样 lib/ 可以在没有 DSH 的情况下被 import（harness 运行期才有这个包）。
 */
const STUB = new URL('./dsh-tools.mjs', import.meta.url).href

export async function resolve(specifier, context, next) {
  if (specifier === '@deepseek-ai/dsh-tools') return { url: STUB, shortCircuit: true }
  return next(specifier, context)
}
