/**
 * 测试替身：本地跑冒烟时不经过 DSH，defineTool 只做恒等返回。
 * （真正运行时由 harness 提供 @deepseek-ai/dsh-tools。）
 */
export function defineTool(definition) {
  return definition
}

export default { defineTool }
