/**
 * SSE 客户端。
 *
 * 不能用原生 EventSource：它只支持 GET，而这里要 POST 一段 JSON。
 * 所以用 fetch + ReadableStream 手动解析 SSE 帧。
 *
 * 帧格式（与服务端 server/sse.py 对应）：
 *   event: <name>\n
 *   data: {"d": <payload>}\n
 *   \n
 */
import { ApiError } from './client'

export interface ChatPayload {
  query: string
  thread_id: string
  request_id: string
}

export type SseEvent = (event: string, data: unknown) => void

export async function streamChat(
  payload: ChatPayload,
  onEvent: SseEvent,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    credentials: 'same-origin',
    signal,
  })

  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body?.detail ?? detail
    } catch {
      /* ignore */
    }
    throw new ApiError(String(detail), res.status)
  }
  if (!res.body) throw new ApiError('当前浏览器不支持流式响应', 0)

  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        emitFrame(frame, onEvent)
        boundary = buffer.indexOf('\n\n')
      }
    }
    if (buffer.trim()) emitFrame(buffer, onEvent)
  } finally {
    try {
      reader.releaseLock()
    } catch {
      /* abort 后 lock 已释放 */
    }
  }
}

function emitFrame(frame: string, onEvent: SseEvent): void {
  let event = 'message'
  const dataLines: string[] = []

  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
  }
  if (!dataLines.length) return

  let parsed: { d?: unknown }
  try {
    parsed = JSON.parse(dataLines.join('\n'))
  } catch {
    return
  }
  onEvent(event, parsed.d)
}
