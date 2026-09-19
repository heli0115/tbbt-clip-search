/**
 * 离线验证前端样式：Tailwind 的自定义主题类到底有没有生成。
 *
 * 为什么需要这个脚本
 * ------------------
 * 本项目的 agent 会话里**跑不了 Vite**（sandbox 拦 node 的 child_process，
 * esbuild 报 `spawn EPERM`，见 AGENTS.md 第 9 节），而 Tailwind 的
 * `@theme` 自定义色一旦名字写错，构建**不会报错** —— 只是样式静默消失，
 * 只能靠人眼看页面才发现。所以这里绕开 Vite，直接调 Tailwind 自己的
 * 编译 API（`@tailwindcss/node`，它是 `@tailwindcss/vite` 的同一份内核）：
 *
 *   1. 编译 frontend/src/index.css
 *   2. 把前端源码里出现的类名当作 candidates 传进去
 *   3. 检查关键类是否真的产出了 CSS 规则
 *
 * ⚠️ 候选类名的提取方式必须与 Tailwind 一致：**全文扫描字符串字面量**。
 * 不能把 JSX 的 `${...}` 三元表达式整段删掉 —— 那样只会收集到一个分支，
 * 漏掉另一个（本脚本第一版正是这么错的，漏了 `hover:bg-panel-hover`）。
 *
 * 用法（项目根目录）：
 *     node tools/verify_tailwind.mjs
 *
 * 注意：它只能证明「类名与主题变量成立」，不能替代真机构建 / 肉眼验收。
 */
import { readFileSync, readdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const FRONTEND = fileURLToPath(new URL('../frontend/', import.meta.url))

/** `@tailwindcss/node` 装在 frontend 下，脚本在 tools/ 里，得显式解析。 */
const requireFromFrontend = createRequire(join(FRONTEND, 'package.json'))
const { compile } = await import(
  pathToFileURL(requireFromFrontend.resolve('@tailwindcss/node')).href
)

/** 这些类名任何一个缺失都说明写法有问题（主题色、v4 渐变名、行内截断…）。 */
const REQUIRED = [
  'bg-ink',
  'bg-panel',
  'hover:bg-panel-hover',
  'bg-brand',
  'hover:bg-brand-hover',
  'text-brand',
  'text-muted',
  'border-line',
  'ring-line',
  'ring-brand',
  'bg-linear-to-b',
  'line-clamp-2',
  'aspect-video',
]

function collectSources(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) return collectSources(path)
    return /\.(tsx|ts|html)$/.test(entry.name) ? [path] : []
  })
}

function collectCandidates(files) {
  const found = new Set()
  for (const file of files) {
    const text = readFileSync(file, 'utf8')
    for (const match of text.matchAll(/'([^'\n]*)'|"([^"\n]*)"|`([^`]*)`/g)) {
      const raw = match[1] ?? match[2] ?? match[3] ?? ''
      for (const token of raw.split(/[\s'"`$?()!{},;=|&<>]+/)) {
        if (token) found.add(token)
      }
    }
  }
  return [...found]
}

/** 选择器里 `:`、`/`、`.` 会被转义（`.hover\:bg-panel-hover`），这里照做。 */
function hasRule(css, name) {
  return css.includes(`.${name.replace(/([/:.])/g, '\\$1')}`)
}

const indexCss = readFileSync(join(FRONTEND, 'src/index.css'), 'utf8')
const candidates = collectCandidates([
  ...collectSources(join(FRONTEND, 'src')),
  join(FRONTEND, 'index.html'),
])

const compiler = await compile(indexCss, {
  base: FRONTEND,
  onDependency: () => {},
})
const output = compiler.build(candidates)

const missing = REQUIRED.filter((name) => !hasRule(output, name))
const colors = [...new Set(output.match(/--color-[a-z-]+:/g) ?? [])].map((item) =>
  item.replace(/^--color-|:$/g, ''),
)

console.log(
  `候选类名 ${candidates.length} 个 → 产出 CSS ${(output.length / 1024).toFixed(1)} KB`,
)
console.log(`主题色：${colors.join(' / ')}`)

if (missing.length) {
  console.log(`❌ 缺少规则：${missing.join(', ')}`)
  process.exitCode = 1
} else {
  console.log(`✅ 关键类全部生成（${REQUIRED.length} 项）`)
}
