/**
 * 交互回归：停止生成、点赞、引用展开。
 *
 * 与 probe.mjs 的区别：那个只读状态，这个会真的点按钮、发请求。
 * 仍然连的是同一个 CDP 端口，所以需要先启动带 --remote-debugging-port 的浏览器。
 */
const CDP = process.env.CDP_PORT ?? '9222'
const BASE = process.env.APP_URL ?? 'http://127.0.0.1:8000'

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

async function typeAndSend(text) {
  await evaluate(`(() => {
    const box = document.querySelector('.composer__input')
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set
    setter.call(box, ${JSON.stringify(text)})
    box.dispatchEvent(new Event('input', { bubbles: true }))
  })()`)
  await wait(150)
  await evaluate(`document.querySelector('.send-btn:not(:disabled)')?.click()`)
}

await new Promise((resolve) => ws.addEventListener('open', resolve, { once: true }))
await send('Page.enable')
await send('Runtime.enable')
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })

await send('Page.navigate', { url: BASE })
await wait(4000)

// ---------------------------------------------------------------- 停止生成
console.log('=== 1. 停止生成 ===')
await typeAndSend('HEPA 滤网多久需要更换一次？')
await wait(1200)

const stopClicked = await evaluate(`(() => {
  const btn = document.querySelector('.send-btn--stop')
  if (!btn) return 'no-stop-button'
  btn.click()
  return 'clicked'
})()`)
console.log('停止按钮:', stopClicked)

// 等到流结束（最多 20 秒）
let stopped = false
for (let i = 0; i < 100; i += 1) {
  await wait(200)
  const state = await evaluate(`(() => {
    const msgs = [...document.querySelectorAll('.msg')]
    const last = msgs[msgs.length - 1]
    return JSON.stringify({
      streaming: !!document.querySelector('.send-btn--stop'),
      inputDisabled: document.querySelector('.composer__input')?.disabled ?? null,
      tail: last ? last.innerText.slice(-70) : null,
    })
  })()`)
  const parsed = JSON.parse(state ?? '{}')
  if (!parsed.streaming) {
    console.log('已停止:', state)
    stopped = true
    break
  }
}
if (!stopped) console.log('停止后仍在流式（可能处于协作式停止的等待中）')

// ---------------------------------------------------------------- 点赞
console.log('\n=== 2. 点赞 ===')
const likeResult = await evaluate(`(() => {
  const buttons = [...document.querySelectorAll('.icon-btn')].filter(b => b.innerText.includes('有帮助'))
  if (!buttons.length) return 'no-like-button'
  const last = buttons[buttons.length - 1]
  last.click()
  return 'clicked'
})()`)
console.log('点赞按钮:', likeResult)
await wait(1200)

const likedState = await evaluate(`(() => {
  const buttons = [...document.querySelectorAll('.icon-btn')].filter(b => b.innerText.includes('点赞') || b.innerText.includes('有帮助'))
  return JSON.stringify(buttons.slice(-3).map(b => ({ text: b.innerText.trim(), disabled: b.disabled })))
})()`)
console.log('点赞后状态:', likedState)

// ---------------------------------------------------------------- 引用
console.log('\n=== 3. 引用面板 ===')
const sourcesResult = await evaluate(`(() => {
  const heads = [...document.querySelectorAll('.sources__head')]
  if (!heads.length) return JSON.stringify({ count: 0 })
  const last = heads[heads.length - 1]
  last.click()
  return JSON.stringify({ count: heads.length, label: last.innerText.trim() })
})()`)
console.log('引用头:', sourcesResult)
await wait(500)

const sourceItems = await evaluate(`(() => {
  const items = [...document.querySelectorAll('.source')]
  return JSON.stringify({
    visible: items.length,
    first: items.length ? items[0].innerText.slice(0, 90) : null,
  })
})()`)
console.log('展开后:', sourceItems)

// ---------------------------------------------------------------- 会话切换
console.log('\n=== 4. 会话列表 ===')
const sessions = await evaluate(`(() => {
  const items = [...document.querySelectorAll('.sidebar .nav-item')]
  return JSON.stringify({
    count: items.length,
    first3: items.slice(0, 3).map(i => i.innerText.replace(/\\n/g, ' ')),
    active: document.querySelector('.nav-item--active')?.innerText.replace(/\\n/g, ' ') ?? null,
  })
})()`)
console.log('会话:', sessions)

ws.close()
process.exit(0)
