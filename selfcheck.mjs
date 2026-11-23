/**
 * 插件端到端自检（不碰真实 DSH_HOME，也不碰真在跑的服务）
 *   node selfcheck.mjs
 * 干什么：
 *   1. 把 DSH_HOME 指到一个临时目录
 *   2. mock 一个最小 ctx 调 apply()
 *   3. 验证：persona 装好了 / 两个服务真的在听 / 状态对象对得上
 *   4. 收摊，杀子进程、删临时目录
 */
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import net from 'node:net'

const TMP = mkdtempSync(join(tmpdir(), 'whale-loli-check-'))
process.env.DSH_HOME = TMP
// 自检要测「服务真的能起来」这条路径 —— 平时默认是 autoStart=false（不起服务）
process.env.WHALE_AUTOSTART = '1'

const VITALS_PORT = 18781
const BRAINVIZ_PORT = 18765

const fail = []
const ok = (name, cond, extra = '') => {
  if (cond) console.log('  ✅ ' + name)
  else { console.log('  ❌ ' + name + (extra ? ' —— ' + extra : '')); fail.push(name) }
}

const portOpen = (port) => new Promise((resolve) => {
  let settled = false
  const done = (v) => { if (!settled) { settled = true; try { s.destroy() } catch {} ; resolve(v) } }
  const s = net.connect({ host: '127.0.0.1', port })
  s.setTimeout(800)
  s.once('connect', () => done(true))
  s.once('timeout', () => done(false))
  s.once('error', () => done(false))
})

console.log('临时 DSH_HOME:', TMP)

// 先确认测试端口是空的
for (const p of [VITALS_PORT, BRAINVIZ_PORT]) {
  if (await portOpen(p)) {
    console.log(`  ! 端口 ${p} 已被占用 —— 这个自检要求它空着，先停掉占用的东西`)
    process.exit(2)
  }
}

const mod = await import('./index.js')
console.log('\n[1] 模块导出')
ok('导出 name', mod.name === 'whale-loli', String(mod.name))
ok('导出 apply', typeof mod.apply === 'function')
ok('导出 inject 是数组', Array.isArray(mod.inject))

const effects = []
const ctx = {
  effect: (fn, label) => { effects.push({ fn, label }) },
  inject: () => { /* 测试里不挂 webServer */ },
}

console.log('\n[2] 调 apply()')
mod.apply(ctx, { vitalsPort: VITALS_PORT, brainvizPort: BRAINVIZ_PORT, roleName: '小测试', ownerNick: '测试主人' })

// 等它把服务拉起来（最多 25 秒）
const t0 = Date.now()
let vOk = false, bOk = false
while (Date.now() - t0 < 25_000) {
  vOk = await portOpen(VITALS_PORT)
  bOk = await portOpen(BRAINVIZ_PORT)
  if (vOk && bOk) break
  await new Promise((r) => setTimeout(r, 800))
}

console.log('\n[3] 结果')
const personaPath = join(TMP, 'douyin-duty', 'persona.md')
ok('persona.md 装好了', existsSync(personaPath), personaPath)
if (existsSync(personaPath)) {
  const txt = readFileSync(personaPath, 'utf8')
  ok('角色名替换了', txt.includes('# 小测试') && !txt.includes('# 小瑜'), txt.slice(0, 60))
  ok('主人昵称替换了', txt.includes('测试主人') && !txt.includes('{{OWNER_NICK}}'))
}
ok(`生命状态在 ${VITALS_PORT} 上听`, vOk)
ok(`记忆星图在 ${BRAINVIZ_PORT} 上听`, bOk)
// ctx.effect 是在服务起来之后才注册的，等它一下
const te = Date.now()
while (effects.length === 0 && Date.now() - te < 6000) await new Promise((r) => setTimeout(r, 300))
ok('注册了清理 effect', effects.length >= 1, String(effects.length))

// 再调一次 apply —— 应该「已存在就跳过」，不覆盖
console.log('\n[4] 二次调用（人设不能覆盖）')
const before = existsSync(personaPath) ? readFileSync(personaPath, 'utf8') : ''
mod.apply(ctx, { vitalsPort: VITALS_PORT, brainvizPort: BRAINVIZ_PORT, roleName: '不该出现', ownerNick: '不该出现' })
await new Promise((r) => setTimeout(r, 2500))
const after = existsSync(personaPath) ? readFileSync(personaPath, 'utf8') : ''
ok('人设没被第二次覆盖', before === after && !after.includes('不该出现'))

// 收摊
console.log('\n[5] 收摊')
for (const e of effects) { try { const cleanup = e.fn(); if (typeof cleanup === 'function') cleanup() } catch { /* ignore */ } }
await new Promise((r) => setTimeout(r, 1200))

console.log('\n' + '='.repeat(50))
if (fail.length) {
  console.log(`✗ ${fail.length} 项没过：`)
  for (const f of fail) console.log('   - ' + f)
} else {
  console.log('✓ 全部通过')
}
try { rmSync(TMP, { recursive: true, force: true }) } catch { /* 删不掉就算了 */ }
process.exit(fail.length ? 1 : 0)
