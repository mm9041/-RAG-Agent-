/**
 * 验证侧栏折叠：收起 → 宽度归零 + 出现展开入口 → 展开恢复。
 * 跑在 dev server（5173）上，需要 CDP 浏览器在 9222。
 */
const CDP = process.env.CDP_PORT ?? '9222'
const BASE = process.env.APP_URL ?? 'http://localhost:5173'

const targets = await (await fetch(`http://127.0.0.1:${CDP}/json`)).json()
const page = targets.find((t) => t.type === 'page')
if (!page) {
  console.error('没有可用的 page target')
  process.exit(1)
}

const ws = new WebSocket(page.webSocketDebuggerUrl)
const pending = new Map()
let seq = 0

ws.addEventListener('message', (event) => {
  const msg = JSON.parse(event.data)
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg)
    pending.delete(msg.id)
  }
})

function send(method, params = {}) {
  return new Promise((resolve) => {
    const id = ++seq
    pending.set(id, resolve)
    ws.send(JSON.stringify({ id, method, params }))
  })
}

async function evaluate(expression) {
  const res = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
  return res.result?.result?.value
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms))

async function measure(tag) {
  const snapshot = await evaluate(`(() => {
    const sidebar = document.querySelector('.sidebar')
    const main = document.querySelector('.main')
    const expand = document.querySelector('.mobile-bar .icon-btn')
    const collapse = document.querySelector('[title="收起侧栏"]')
    return JSON.stringify({
      sidebarWidth: sidebar ? Math.round(sidebar.getBoundingClientRect().width) : null,
      sidebarClass: sidebar ? sidebar.className : null,
      mainWidth: main ? Math.round(main.getBoundingClientRect().width) : null,
      expandButton: !!expand,
      collapseButton: !!collapse,
    })
  })()`)
  console.log(`${tag}: ${snapshot}`)
}

await new Promise((resolve) => ws.addEventListener('open', resolve, { once: true }))
await send('Page.enable')
await send('Runtime.enable')
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })

await send('Page.navigate', { url: BASE })
await wait(5000)

await measure('初始    ')

const clicked = await evaluate(`(() => {
  const btn = document.querySelector('[title="收起侧栏"]')
  if (!btn) return 'not-found'
  btn.click()
  return 'clicked'
})()`)
console.log('点击收起:', clicked)
await wait(500)
await measure('收起后  ')

const expanded = await evaluate(`(() => {
  const btn = document.querySelector('.mobile-bar .icon-btn')
  if (!btn) return 'no-expand-button'
  btn.click()
  return 'clicked'
})()`)
console.log('点击展开:', expanded)
await wait(500)
await measure('展开后  ')

ws.close()
process.exit(0)
