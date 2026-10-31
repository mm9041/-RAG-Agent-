/**
 * 用 CDP 检查页面真实状态并可选交互。
 *
 * 为什么不用 --screenshot：它拿不到 scrollTop 这类运行时值，
 * 无法判断"停在历史中间"到底是渲染问题还是本来就到底了。
 *
 * 用法：
 *   node tools/probe.mjs                    # 只检查首屏状态
 *   node tools/probe.mjs --send "你好"       # 检查后发送一条消息并观察流式
 */
const CDP = process.env.CDP_PORT ?? '9222'
const BASE = process.env.APP_URL ?? 'http://127.0.0.1:8000'

const sendArg = process.argv.indexOf('--send')
const messageToSend = sendArg > 0 ? process.argv[sendArg + 1] : null

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
  const res = await send('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true,
  })
  return res.result?.result?.value
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms))

await new Promise((resolve) => ws.addEventListener('open', resolve, { once: true }))
await send('Page.enable')
await send('Runtime.enable')
// CDP 控制的页面默认视口很小（实测 320px 高），不设置的话布局探测会失真
await send('Emulation.setDeviceMetricsOverride', {
  width: 1440,
  height: 900,
  deviceScaleFactor: 1,
  mobile: false,
})

await send('Page.navigate', { url: BASE })
await wait(4500)

const snapshot = await evaluate(`(() => {
  const el = document.querySelector('.main__scroll')
  const msgs = [...document.querySelectorAll('.msg')]
  const h = (sel) => {
    const node = document.querySelector(sel)
    return node ? Math.round(node.getBoundingClientRect().height) : null
  }
  return JSON.stringify({
    scroller: !!el,
    scrollTop: el ? Math.round(el.scrollTop) : null,
    scrollHeight: el ? Math.round(el.scrollHeight) : null,
    clientHeight: el ? Math.round(el.clientHeight) : null,
    distanceFromBottom: el ? Math.round(el.scrollHeight - el.scrollTop - el.clientHeight) : null,
    messageCount: msgs.length,
    layout: {
      innerHeight: window.innerHeight,
      html: h('html'), body: h('body'), app: h('#app'),
      shell: h('.app'), main: h('.main'), scroll: h('.main__scroll'),
      inner: h('.main__inner'), composer: h('.composer'), sidebar: h('.sidebar'),
    },
    lastMessage: msgs.length ? msgs[msgs.length - 1].innerText.slice(0, 80) : null,
    hintRow: document.querySelector('.composer__hint')?.innerText ?? null,
  })
})()`)

console.log('—— 首屏 ——')
console.log(snapshot)

if (messageToSend) {
  const typed = await evaluate(`(() => {
    const box = document.querySelector('.composer__input')
    if (!box) return 'no-textarea'
    box.focus()
    return 'ok'
  })()`)
  console.log('输入框:', typed)

  // 直接设值再派发 input 事件，Vue 的 v-model 才能收到
  await evaluate(`(() => {
    const box = document.querySelector('.composer__input')
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set
    setter.call(box, ${JSON.stringify(messageToSend)})
    box.dispatchEvent(new Event('input', { bubbles: true }))
  })()`)
  await wait(200)

  const startedAt = Date.now()
  await evaluate(`document.querySelector('.send-btn:not(:disabled)')?.click()`)

  let firstToken = null
  let lastLen = 0
  let stableTicks = 0

  for (let i = 0; i < 120; i += 1) {
    await wait(200)
    const state = await evaluate(`(() => {
      const bubble = document.querySelector('.msg:last-of-type .msg__text, .msg:last-of-type .md-body')
      const stage = document.querySelector('.stage')
      const stop = document.querySelector('.send-btn--stop')
      const msgs = document.querySelectorAll('.msg')
      const last = msgs[msgs.length - 1]
      return JSON.stringify({
        stage: stage ? stage.innerText.replace(/\\n/g, ' | ') : null,
        streaming: !!stop,
        textLength: last ? last.innerText.length : 0,
      })
    })()`)
    const parsed = JSON.parse(state ?? '{}')
    const elapsed = ((Date.now() - startedAt) / 1000).toFixed(1)

    if (firstToken === null && parsed.textLength > 0) {
      firstToken = elapsed
      console.log(`首字: ${firstToken}s`)
    }
    if (parsed.textLength === lastLen) stableTicks += 1
    else stableTicks = 0
    lastLen = parsed.textLength

    if (i % 5 === 0) console.log(`  [${elapsed}s] stage=${parsed.stage} len=${parsed.textLength}`)

    if (!parsed.streaming && stableTicks >= 2 && i > 2) {
      console.log(`完成: ${elapsed}s, 最终长度 ${parsed.textLength}`)
      break
    }
  }

  const final = await evaluate(`(() => {
    const el = document.querySelector('.main__scroll')
    const msgs = [...document.querySelectorAll('.msg')]
    return JSON.stringify({
      distanceFromBottom: el ? Math.round(el.scrollHeight - el.scrollTop - el.clientHeight) : null,
      messageCount: msgs.length,
      answer: msgs.length ? msgs[msgs.length - 1].innerText.slice(0, 160) : null,
      stats: document.querySelector('.stats')?.innerText ?? null,
    })
  })()`)
  console.log('—— 完成后 ——')
  console.log(final)
}

ws.close()
process.exit(0)
