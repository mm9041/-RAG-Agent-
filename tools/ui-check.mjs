/**
 * 前端新交互验证：引用点击定位、复制、重新生成、分页接口。
 * 需要 CDP 浏览器在 9222，且服务在 8000（用构建产物，不依赖 vite）。
 */
const CDP = process.env.CDP_PORT ?? '9222'
const BASE = process.env.APP_URL ?? 'http://127.0.0.1:8000'

const targets = await (await fetch(`http://127.0.0.1:${CDP}/json`)).json()
const page = targets.find((t) => t.type === 'page')
if (!page) {
  console.error('没有 page target')
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

const send = (method, params = {}) =>
  new Promise((resolve) => {
    const id = ++seq
    pending.set(id, resolve)
    ws.send(JSON.stringify({ id, method, params }))
  })

async function evaluate(expression) {
  const res = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
  return res.result?.result?.value
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms))

async function waitIdle(maxMs = 90000) {
  const started = Date.now()
  while (Date.now() - started < maxMs) {
    const streaming = await evaluate(`!!document.querySelector('.send-btn--stop')`)
    if (!streaming) return true
    await wait(400)
  }
  return false
}

await new Promise((resolve) => ws.addEventListener('open', resolve, { once: true }))
await send('Page.enable')
await send('Runtime.enable')
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })

await send('Page.navigate', { url: BASE })
await wait(4500)

console.log('=== 1. 发一个会带引用的问题 ===')
await evaluate(`(() => {
  const box = document.querySelector('.composer__input')
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set
  setter.call(box, 'HEPA 滤网多久需要更换一次？')
  box.dispatchEvent(new Event('input', { bubbles: true }))
})()`)
await wait(150)
await evaluate(`document.querySelector('.send-btn:not(:disabled)')?.click()`)
await waitIdle()

const afterSend = await evaluate(`(() => {
  const chips = document.querySelectorAll('.ref-chip')
  const heads = document.querySelectorAll('.sources__head')
  return JSON.stringify({ refChips: chips.length, sourcePanels: heads.length,
    last: [...document.querySelectorAll('.msg')].pop()?.innerText.slice(0, 70) })
})()`)
console.log('发送后:', afterSend)

console.log('\n=== 2. 点击正文 [1] 应展开并高亮对应片段 ===')
const clickRef = await evaluate(`(() => {
  const chip = document.querySelector('.ref-chip')
  if (!chip) return 'no-chip'
  chip.click()
  return chip.textContent
})()`)
console.log('点击引用:', clickRef)
await wait(600)

const afterRef = await evaluate(`(() => {
  const list = document.querySelector('.sources__list')
  const active = document.querySelector('.source--active')
  return JSON.stringify({
    listVisible: list ? getComputedStyle(list).display !== 'none' : false,
    activeCount: active ? 1 : 0,
    activeText: active ? active.innerText.slice(0, 60) : null,
  })
})()`)
console.log('引用定位:', afterRef)

console.log('\n=== 3. 复制按钮 ===')
const clickCopy = await evaluate(`(() => {
  const btn = [...document.querySelectorAll('.icon-btn')].find(b => b.innerText.includes('复制'))
  if (!btn) return 'no-copy-button'
  btn.click()
  return 'clicked'
})()`)
console.log('复制:', clickCopy)
await wait(400)
const copyState = await evaluate(`(() => {
  const btn = [...document.querySelectorAll('.icon-btn')].find(b => b.innerText.includes('已有帮助') || b.innerText.includes('复制') || b.innerText.includes('已复制'))
  return JSON.stringify([...document.querySelectorAll('.icon-btn')].map(b => b.innerText.trim()).filter(t => t.includes('复制')))
})()`)
console.log('复制后按钮文案:', copyState)

console.log('\n=== 4. 重新生成 ===')
const before = await evaluate(`document.querySelectorAll('.msg').length`)
const clickRegen = await evaluate(`(() => {
  const btn = [...document.querySelectorAll('.icon-btn')].find(b => b.innerText.includes('重新生成'))
  if (!btn) return 'no-regenerate-button'
  btn.click()
  return 'clicked'
})()`)
console.log('重新生成:', clickRegen, '| 点击前消息数:', before)
await wait(1500)
await waitIdle()

const afterRegen = await evaluate(`(() => {
  const msgs = [...document.querySelectorAll('.msg')]
  return JSON.stringify({
    messageCount: msgs.length,
    lastRoleIsAssistant: msgs.length ? !msgs[msgs.length-1].innerText.includes('HEPA') : null,
    tail: msgs[msgs.length-1]?.innerText.slice(0, 80),
    stats: document.querySelector('.stats')?.innerText ?? null,
  })
})()`)
console.log('重新生成后:', afterRegen)

ws.close()
process.exit(0)
