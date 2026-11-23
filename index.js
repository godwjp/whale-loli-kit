/**
 * whale-loli-kit —— DSH 插件（host 半端）
 * ============================================================================
 * 这个插件**只做三件事**，不碰模型路由、不碰任何会话、不改别人的配置：
 *
 *   1. 首次启动时把 `assets/persona.md` 装进 `<DSH_HOME>/douyin-duty/`
 *      （已经有 persona.md 就跳过 —— 绝不覆盖别人已经调好的人设）
 *   2. 起两个本地只读服务：
 *        生命状态 127.0.0.1:8781（assets/vitals.py）
 *        记忆星图 127.0.0.1:8765（assets/brainviz.py）
 *      **端口已经被占就跳过**（比如你自己已经手动起过一份）
 *   3. 注册 `/api/whale/status`（有 webServer 才注册），给设置页/脚本查状态
 *
 * 设计纪律：
 *   · 零 npm 依赖，只用 Node 内置
 *   · **每一个可能抛错的地方都包了 try/catch** —— 这个插件挂了绝不能拖死 DSH
 *   · 子进程一律 `windowsHide: true`（不然 Windows 上会弹黑窗口）
 *   · 不下载任何东西、不联网
 * ============================================================================
 */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { homedir } from 'node:os'
import net from 'node:net'

const HERE = dirname(fileURLToPath(import.meta.url))
const ASSETS = join(HERE, 'assets')

export const name = 'whale-loli'
export const inject = []
export const optional = ['webServer']

/**
 * 配置项：照 DSH 插件的规矩，**导出 Config 才收得到 cordis.patch.yml 里的 config**。
 * schemastery 是宿主的可选 peer —— 拿不到就 `Config = undefined`，
 * 插件照样跑（apply 里对 config 做了全套兜底，缺什么用什么默认）。
 */
let z = null
try {
  const m = await import('@deepseek-ai/schemastery')
  z = m?.default ?? m
} catch { z = null }

export const Config = (z && typeof z.object === 'function')
  ? z.object({
    enabled: z.boolean().default(true),
    vitalsPort: z.number().default(8781),
    brainvizPort: z.number().default(8765),
    pythonPath: z.string().default(''),
    roleName: z.string().default('小瑜'),
    ownerNick: z.string().default(''),
  })
  : undefined

const log = (m) => { try { console.log('[whale-loli] ' + m) } catch { /* 日志写不出不影响 */ } }

/** 插件自己的状态（给 /api/whale/status 用） */
const state = {
  persona: null,
  vitals: { running: false, port: 8781, note: '' },
  brainviz: { running: false, port: 8765, note: '' },
  python: '',
  errors: [],
}

function dshHome() {
  return process.env.DSH_HOME || join(homedir(), '.dsh')
}

/** 端口有没有人在听（连一下就知道了） */
function portOpen(port, timeout = 700) {
  return new Promise((resolve) => {
    let settled = false
    const done = (v) => { if (!settled) { settled = true; try { sock.destroy() } catch { /* ignore */ } resolve(v) } }
    const sock = net.connect({ host: '127.0.0.1', port })
    sock.setTimeout(timeout)
    sock.once('connect', () => done(true))
    sock.once('timeout', () => done(false))
    sock.once('error', () => done(false))
  })
}

/** 找一个能用的 Python（优先 config.pythonPath） */
function findPython(preferred) {
  const cands = []
  if (preferred && String(preferred).trim()) cands.push({ cmd: String(preferred).trim(), pre: [] })
  cands.push({ cmd: 'python', pre: [] })
  cands.push({ cmd: 'py', pre: ['-3'] })
  for (const c of cands) {
    try {
      const r = spawnSync(c.cmd, [...c.pre, '--version'], { windowsHide: true, timeout: 6000, encoding: 'utf8' })
      if (r && r.status === 0) return c
    } catch { /* 试下一个 */ }
  }
  return null
}

/** 装人设：已经有就跳过，绝不覆盖 */
function installPersona(roleName, ownerNick) {
  try {
    const duty = join(dshHome(), 'douyin-duty')
    mkdirSync(duty, { recursive: true })
    const target = join(duty, 'persona.md')
    if (existsSync(target)) {
      return { ok: true, skipped: true, path: target, note: '已经有 persona.md，没动它' }
    }
    const src = join(ASSETS, 'persona.md')
    if (!existsSync(src)) return { ok: false, error: 'assets/persona.md 不见了' }
    let txt = readFileSync(src, 'utf8')
    txt = txt.replace(/^# 小瑜/m, '# ' + (String(roleName || '').trim() || '小瑜'))
    txt = txt.split('{{OWNER_NICK}}').join(String(ownerNick || '').trim() || '主人')
    writeFileSync(target, txt, 'utf8')
    return { ok: true, skipped: false, path: target }
  } catch (e) {
    return { ok: false, error: String(e?.message ?? e) }
  }
}

/** 起一个 python 服务；端口已占就跳过 */
async function startService(label, py, script, port, extraEnv = {}) {
  const info = { running: false, port, note: '' }
  try {
    if (await portOpen(port)) {
      info.note = `端口 ${port} 已经有人在听 —— 跳过（多半是你自己已经起过一份）`
      return info
    }
    if (!py) {
      info.note = '没找到 Python —— 装个 Python 3.9+ 并加进 PATH'
      return info
    }
    if (!existsSync(script)) {
      info.note = `${script} 不见了`
      return info
    }
    const child = spawn(py.cmd, [...py.pre, script, ...(label === 'vitals' ? ['--serve'] : [])], {
      cwd: dirname(script),
      env: { ...process.env, ...extraEnv },
      windowsHide: true,
      /**
       * 🔴 2026-11-23 实测：`stdio: 'ignore'` 在 Windows 上会让 `py.exe` 启动器
       *    **起不来**（进程创建了但脚本没执行，连 log 都没写）。换成 pipe 就正常。
       *    ⇒ 这里用 pipe 并**把输出收下来**（最近 40 行），既排障又不弹窗口。
       */
      stdio: process.env.WHALE_LOLI_DEBUG ? 'inherit' : ['ignore', 'pipe', 'pipe'],
      detached: false,
    })
    // 必须消费输出，否则管道满了会把子进程堵死
    const tail = []
    const eat = (buf) => {
      try {
        const s = String(buf)
        for (const ln of s.split('\n')) if (ln.trim()) tail.push(ln.trim())
        while (tail.length > 40) tail.shift()
      } catch { /* ignore */ }
    }
    child.stdout?.on('data', eat)
    child.stderr?.on('data', eat)
    info.tail = tail
    child.on('error', (e) => { info.note = 'spawn 失败：' + String(e?.message ?? e) })
    child.on('exit', (code) => { info.running = false; info.note = `退出（code=${code}）` })
    // 🔴 别只等 1.5 秒 —— vitals.py 起来要读 panel、算初始状态，实测要 2~3 秒。
    //    这里轮询到 8 秒，谁先到算谁。
    const deadline = Date.now() + 8000
    while (Date.now() < deadline) {
      if (await portOpen(port, 500)) { info.running = true; break }
      await new Promise((r) => setTimeout(r, 500))
    }
    info.note = info.running ? 'ok' : '起来了但端口没在听（看 assets 目录下的 log）'
    info._child = child
    return info
  } catch (e) {
    info.note = '起不来：' + String(e?.message ?? e)
    return info
  }
}

function sendJson(res, code, obj) {
  try {
    const body = JSON.stringify(obj, null, 1)
    res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' })
    res.end(body)
  } catch { /* 连接断了就算了 */ }
}

export function apply(ctx, config = {}) {
  const cfg = config || {}
  if (cfg.enabled === false) { log('配置里 enabled=false，什么都不做'); return }

  // 🔴 兜底顺序：config（patch 里配的）→ 环境变量 → 默认值。
  //    实测本机的 profile node_modules 里没有 @deepseek-ai/schemastery，
  //    ⇒ Config 导出会是 undefined ⇒ DSH 可能不把 patch 里的 config 传进来。
  //    所以环境变量这一层必须有，不然想换端口就没辙了。
  const num = (a, b, d) => {
    const v = Number(a) || Number(b) || d
    return Number.isFinite(v) && v > 0 ? v : d
  }
  const vitalsPort = num(cfg.vitalsPort, process.env.WHALE_VITALS_PORT, 8781)
  const brainvizPort = num(cfg.brainvizPort, process.env.WHALE_BRAINVIZ_PORT, 8765)
  const roleName = String(cfg.roleName || process.env.WHALE_ROLE_NAME || '').trim()
  const ownerNick = String(cfg.ownerNick || process.env.WHALE_OWNER_NICK || '').trim()
  const pythonPath = String(cfg.pythonPath || process.env.WHALE_PYTHON || '').trim()
  /**
   * 🔴🔴 2026-11-23 实测踩到的坑：**默认不自动起服务**。
   *
   *   重启 DSH 时，用户自己的生命状态服务可能还没起来（它靠看门狗/自启拉），
   *   而本插件在 profile 加载阶段就跑 —— 于是它先 bind 了 8781，
   *   用的是自己目录下的空 `state.json`。**等于把用户的"身体"换成了一份新的。**
   *   （那次运气好：它 bind 完又检测到端口被占，自己退出了。顺序反过来就真会抢。）
   *
   *   ⇒ 默认 `autoStart: false` —— 只装人设，不起服务。
   *     想让它自己起：cordis.patch.yml 里把 autoStart 改成 true，
   *     或者干脆手动 `python assets/vitals/vitals.py --serve`。
   */
  const autoStart = cfg.autoStart === true || process.env.WHALE_AUTOSTART === '1'

  // 全部丢到异步里跑 —— 绝不能拖慢 DSH 启动
  void (async () => {
    try {
      // ① 人设
      const p = installPersona(roleName, ownerNick)
      state.persona = p
      if (p.ok && p.skipped) log('人设：已存在，没动（' + p.path + '）')
      else if (p.ok) log('人设：装好了 → ' + p.path)
      else { log('人设：没装成（' + p.error + '）'); state.errors.push('persona: ' + p.error) }

      // ② Python
      const py = findPython(pythonPath)
      state.python = py ? [py.cmd, ...py.pre].join(' ') : ''
      if (!py) log('没找到 Python —— 生命状态和记忆星图会跳过')
      else log('Python: ' + state.python)

      // ③ 两个服务（默认 autoStart=false ⇒ 不起，见上面那段注释）
      if (!autoStart) {
        state.vitals.note = 'autoStart=false（默认不起服务；要起就手动跑 vitals.py 或改配置）'
        state.brainviz.note = state.vitals.note
        log('本地服务：autoStart=false，没起（这是默认行为，不是出错）')
      } else {
        const v = await startService('vitals', py, join(ASSETS, 'vitals', 'vitals.py'), vitalsPort, { VITALS_PORT: String(vitalsPort) })
        state.vitals = { running: v.running, port: vitalsPort, note: v.note }
        log(`生命状态：${v.note}`)

        const b = await startService('brainviz', py, join(ASSETS, 'brainviz', 'server.py'), brainvizPort, { BRAINVIZ_PORT: String(brainvizPort) })
        state.brainviz = { running: b.running, port: brainvizPort, note: b.note }
        log(`记忆星图：${b.note}`)

        // ④ 退出时收摊（只在真的由本插件起的子进程才杀）
        try {
          ctx.effect(() => () => {
            for (const c of [v?._child, b?._child]) {
              try { if (c && c.exitCode === null) c.kill() } catch { /* ignore */ }
            }
          }, 'whale-loli: 收本地服务')
        } catch { /* 宿主没有 effect 就算了 */ }
      }
    } catch (e) {
      log('初始化出错（不影响 DSH）：' + String(e?.message ?? e))
      state.errors.push(String(e?.message ?? e))
    }
  })()

  // ⑤ 状态接口（有 webServer 才挂）
  try {
    ctx.inject(['webServer'], (webCtx) => {
      try {
        webCtx.effect(() => webCtx.get('webServer').register({
          kind: 'prefix',
          path: '/api/whale',
          handler: async (req, res) => {
            try {
              const url = new URL(req.url, 'http://127.0.0.1')
              if (url.pathname.replace(/\/+$/, '') === '/api/whale/status') {
                return sendJson(res, 200, {
                  ok: true,
                  plugin: 'whale-loli-kit',
                  persona: state.persona,
                  python: state.python,
                  vitals: { running: state.vitals.running, port: state.vitals.port, url: `http://127.0.0.1:${state.vitals.port}/` },
                  brainviz: { running: state.brainviz.running, port: state.brainviz.port, url: `http://127.0.0.1:${state.brainviz.port}/` },
                  errors: state.errors,
                })
              }
              return sendJson(res, 404, { ok: false, error: `unknown ${req.method} ${url.pathname}` })
            } catch (e) {
              return sendJson(res, 500, { ok: false, error: String(e?.message ?? e) })
            }
          },
        }), 'whale-loli: /api/whale 路由')
        log('状态接口就绪：/api/whale/status')
      } catch (e) { log('HTTP 路由没挂上（不影响别的）：' + String(e?.message ?? e)) }
    })
  } catch { /* 没有 webServer 服务，跳过 */ }
}
