/**
 * 管理端渲染截图。会先用临时密码登录（仅测试实例），再逐个 tab 截图。
 * 需要 CDP 浏览器在 9222。
 */
import { writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

const CDP = process.env.CDP_PORT ?? '9222'
const BASE = process.env.APP_URL ?? 'http://127.0.0.1:8010'
const PASSWORD = process.env.ADMIN_PWD ?? 'test-admin-123'

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

async function shoot(name) {
  const res = await send('Page.captureScreenshot', { format: 'png' })
  const data = res.result?.data
  if (!data) {
    console.log(`${name}: 截图失败`)
    return
  }
  const path = join(tmpdir(), name)
  await writeFile(path, Buffer.from(data, 'base64'))
  console.log(`${name}: ${path}`)
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms))

await new Promise((resolve) => ws.addEventListener('open', resolve, { once: true }))
await send('Page.enable')
await send('Runtime.enable')
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })

await send('Page.navigate', { url: `${BASE}/admin` })
await wait(2500)

const loggedIn = await evaluate(`fetch('/api/admin/login', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ password: ${JSON.stringify(PASSWORD)} }),
  credentials: 'same-origin',
}).then(r => r.status)`)

if (loggedIn !== 200) {
  console.log('登录失败，状态码:', loggedIn)
  await shoot('zs-admin-login.png')
  ws.close()
  process.exit(1)
}

await send('Page.navigate', { url: `${BASE}/admin` })
await wait(2600)
await shoot('zs-admin-overview.png')

await evaluate(`[...document.querySelectorAll('.nav-item')].find(b => b.innerText.includes('知识文件'))?.click()`)
await wait(1400)
await shoot('zs-admin-knowledge.png')

await evaluate(`[...document.querySelectorAll('.nav-item')].find(b => b.innerText.includes('反馈'))?.click()`)
await wait(1200)
await shoot('zs-admin-feedback.png')

const metrics = await evaluate(`(() => {
  const cards = [...document.querySelectorAll('.metric')].map(m => m.innerText.replace(/\\n/g, ' '))
  const tableRows = document.querySelectorAll('.table tbody tr').length
  return JSON.stringify({ cards, tableRows, tab: document.querySelector('.nav-item--active')?.innerText.trim() })
})()`)
console.log('反馈页指标:', metrics)

ws.close()
process.exit(0)
