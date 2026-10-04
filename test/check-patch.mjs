// cordis.patch.yml 里引用的每个模块名都必须真实存在，否则预设会静默变成
// 「加载失败」：registry 的 catch 只把错误记进 record.broken 并 warn 一行，
// GUI 上只看到一个红色徽标 —— 真实原因（ERR_MODULE_NOT_FOUND）不落到任何日志文件。
//
// 这就是本脚本存在的原因：把 asar 里的包名清单抽出来，逐个核对。
// 用法： node test/check-patch.mjs [--asar <path>]
// 找不到 asar 时只打印 SKIP 并以 0 退出（不阻塞不在这台机器上的开发）。

import { readFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const bundleRoot = join(here, '..');

const ASAR_CANDIDATES = [
  process.env.DSH_APP_ASAR,
  'D:/Program/deepseek harness desktop/resources/app.asar',
  'C:/Program Files/DeepSeek Harness/resources/app.asar',
  '/Applications/DeepSeek Harness.app/Contents/Resources/app.asar',
].filter(Boolean);

function findAsar(argv) {
  const i = argv.indexOf('--asar');
  if (i >= 0 && argv[i + 1]) return argv[i + 1];
  return ASAR_CANDIDATES.find((p) => existsSync(p));
}

/** asar 头：uint32@0=4，uint32@4=header pickle 大小，uint32@12=JSON 字符串长度，JSON 从 16 开始。 */
function readAsarPackageNames(path) {
  const fd = readFileSync(path, { flag: 'r' });
  const strlen = fd.readUInt32LE(12);
  const header = JSON.parse(fd.subarray(16, 16 + strlen).toString('utf8'));
  const names = new Set();
  const walk = (node, prefix) => {
    for (const [name, entry] of Object.entries(node ?? {})) {
      const p = `${prefix}/${name}`;
      if (entry.files) walk(entry.files, p);
      else if (p.endsWith('package.json')) {
        const m = /\/node_modules\/((?:@[^/]+\/)?[^/]+)\/package\.json$/.exec(p);
        if (m) names.add(m[1]);
      }
    }
  };
  walk(header.files, '');
  return names;
}

/** 只挑像模块名的：`@scope/name`、`dsh-*`、`cordis:*`。预设的显示名（中文）跳过。 */
function referencedNames(yaml) {
  const out = new Set();
  for (const line of yaml.split(/\r?\n/)) {
    const m = /^\s*-?\s*name:\s*["']?([^\s"']+)["']?\s*$/.exec(line);
    if (!m) continue;
    const value = m[1];
    if (value.startsWith('@') && value.includes('/')) out.add(value);
    else if (/^[a-z][a-z0-9-]*$/.test(value) && (value.startsWith('dsh-') || value.startsWith('cordis')))
      out.add(value);
  }
  return out;
}

const asar = findAsar(process.argv.slice(2));
if (!asar) {
  console.log('[check-patch] SKIP：没找到 app.asar（用 --asar <path> 或 DSH_APP_ASAR 指定）');
  process.exit(0);
}

const available = readAsarPackageNames(asar);
const referenced = referencedNames(readFileSync(join(bundleRoot, 'cordis.patch.yml'), 'utf8'));
// bundle 自己（被 link: 装进 profile）与 cordis 内置分组不是 asar 里的包。
const local = new Set(['dsh-anima-style-lora']);

const missing = [...referenced].filter(
  (name) => !local.has(name) && !name.startsWith('cordis:') && !available.has(name),
);

console.log(`[check-patch] asar: ${asar}`);
console.log(`[check-patch] 引用模块 ${referenced.size} 个，asar 可用包 ${available.size} 个`);
for (const name of [...referenced].sort()) {
  const mark = local.has(name) || name.startsWith('cordis:') ? '·' : available.has(name) ? '✓' : '✗';
  console.log(`  ${mark} ${name}`);
}
if (missing.length > 0) {
  console.error(`\n[check-patch] ❌ 以下模块在 asar 里不存在，预设会「加载失败」：`);
  for (const name of missing) console.error(`  - ${name}`);
  process.exit(1);
}
console.log('\n[check-patch] OK：所有引用模块都存在');
