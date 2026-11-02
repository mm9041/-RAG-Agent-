// 验证删除确认浮窗是否居中在侧栏内，以及 Esc 能否关闭。
// 用法：先启动带 --remote-debugging-port=9222 的 Edge，再 node tools/side-confirm-check.mjs
import { writeFileSync } from 'node:fs'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const targets = await (await fetch('http://127.0.0.1:9222/json')).json()
const page = targets.find((t) => t.type === 'page')
if (!page) throw new Error('没有可用的页面目标')

const ws = new WebSocket(page.webSocketDebuggerUrl)
let seq = 0
const pending = new Map()
ws.addEventListener('message', (e) => {
  const msg = JSON.parse(e.data)
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
await new Promise((r) => ws.addEventListener('open', r, { once: true }))

await send('Runtime.enable')
await send('Page.enable')
// 默认窗口只有 800px 宽，会落进移动端断点（侧栏变成收起的抽屉），
// 量出来的位置对但截图看不见东西。固定成桌面视口。
await send('Emulation.setDeviceMetricsOverride', {
  width: 1440,
  height: 900,
  deviceScaleFactor: 1,
  mobile: false,
})

const evaluate = async (expression) => {
  const res = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
  if (res.result?.exceptionDetails) throw new Error(JSON.stringify(res.result.exceptionDetails))
  return res.result?.result?.value
}

await send('Page.navigate', { url: 'http://127.0.0.1:8000/' })
for (let i = 0; i < 60; i++) {
  await sleep(250)
  const ready = await evaluate(`!!document.querySelector('.sidebar .nav-item')`)
  if (ready) break
}
await sleep(400)

console.log('--- 打开前 ---')
console.log('会话条数:', await evaluate(`document.querySelectorAll('.sidebar .nav-item').length`))
console.log(
  '旧内联块是否消失:',
  await evaluate(`document.querySelectorAll('.sidebar__body .notice--danger').length === 0`),
)

await evaluate(`document.querySelector('.nav-item__meta[title="删除该会话"]').click()`)
await sleep(300)

const metrics = await evaluate(`(() => {
  const side = document.querySelector('.sidebar').getBoundingClientRect()
  const card = document.querySelector('.side-confirm__card')
  if (!card) return { found: false }
  const c = card.getBoundingClientRect()
  const body = document.querySelector('.sidebar__body')
  return {
    found: true,
    sidebar: { x: Math.round(side.x), y: Math.round(side.y), w: Math.round(side.width), h: Math.round(side.height) },
    card: { x: Math.round(c.x), y: Math.round(c.y), w: Math.round(c.width), h: Math.round(c.height) },
    dxCenter: Math.round((c.x + c.width / 2) - (side.x + side.width / 2)),
    dyCenter: Math.round((c.y + c.height / 2) - (side.y + side.height / 2)),
    scrollTop: body ? body.scrollTop : null,
    text: document.querySelector('.side-confirm__text').textContent.trim(),
  }
})()`)

console.log('--- 浮窗位置 ---')
console.log(JSON.stringify(metrics, null, 2))

const shot = await send('Page.captureScreenshot', { format: 'png' })
writeFileSync('tools/side-confirm.png', Buffer.from(shot.result.data, 'base64'))
console.log('截图: tools/side-confirm.png')

// Esc 关闭
await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
await sleep(250)
console.log('Esc 后浮窗还在:', await evaluate(`!!document.querySelector('.side-confirm')`))

// 点遮罩关闭
await evaluate(`document.querySelector('.nav-item__meta[title="删除该会话"]').click()`)
await sleep(200)
console.log('再次打开:', await evaluate(`!!document.querySelector('.side-confirm')`))
await evaluate(`document.querySelector('.side-confirm').click()`)
await sleep(200)
console.log('点遮罩后:', await evaluate(`!!document.querySelector('.side-confirm')`))

ws.close()
process.exit(0)
