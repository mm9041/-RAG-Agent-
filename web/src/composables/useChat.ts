/**
 * 聊天状态机。
 *
 * 顺滑的关键在 ``scheduleFlush``：模型 token 到达频率远高于屏幕刷新率
 * （实测一轮回复 30~60 个 chunk），如果每个 chunk 都触发一次响应式更新，
 * Vue 会在同一帧内做几十次 patch，画面反而卡顿。这里把增量攒起来，
 * 每帧只写一次 DOM —— 视觉上仍是逐字出现，但渲染次数被压到 60fps 以内。
 */
import { onUnmounted, ref } from 'vue'

import { streamChat } from '../api/chat'
import { api } from '../api/client'
import type { Message, PhaseMark, Source } from '../types'

const TOOL_PHASES: Record<string, { start: string; end: string }> = {
  rag_summarize: { start: '正在查找相关资料', end: '正在整理检索资料' },
  get_weather: { start: '正在查询天气', end: '正在整理天气信息' },
  get_user_location: { start: '正在确认所在城市', end: '正在准备回复' },
  get_user_id: { start: '正在准备回复', end: '正在准备回复' },
}

function newRequestId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID().replace(/-/g, '')
  }
  return Math.random().toString(36).slice(2) + Date.now().toString(36)
}

export function useChat() {
  const messages = ref<Message[]>([])
  const streaming = ref(false)
  const stage = ref('')
  const elapsed = ref(0)
  const stageElapsed = ref(0)
  const firstTokenSeconds = ref<number | null>(null)
  const marks = ref<PhaseMark[]>([])
  const error = ref<string | null>(null)
  const requestId = ref('')

  let startedAt = 0
  let stageAt = 0
  let ticker = 0
  let abortController: AbortController | null = null
  let pendingText = ''
  let rafId = 0
  let scrollHook: (() => void) | null = null

  function setScrollHook(fn: () => void) {
    scrollHook = fn
  }

  function flush() {
    if (!pendingText) return
    const chunk = pendingText
    pendingText = ''
    const last = messages.value[messages.value.length - 1]
    if (last && last.role === 'assistant') {
      last.content += chunk
      if (firstTokenSeconds.value === null) {
        firstTokenSeconds.value = (performance.now() - startedAt) / 1000
      }
    }
    scrollHook?.()
  }

  function scheduleFlush() {
    if (rafId) return
    rafId = requestAnimationFrame(() => {
      rafId = 0
      flush()
    })
  }

  function markPhase(label: string) {
    if (!label) return
    stage.value = label
    stageAt = performance.now()
    marks.value.push({ label, at: (stageAt - startedAt) / 1000 })
  }

  function handle(event: string, data: unknown) {
    const last = messages.value[messages.value.length - 1]
    if (!last || last.role !== 'assistant') return

    switch (event) {
      case 'start': {
        const rid = (data as { request_id?: string } | null)?.request_id
        if (rid) requestId.value = rid
        break
      }
      case 'phase': {
        const label = String(data ?? '')
        if (label && label !== stage.value) markPhase(label)
        break
      }
      case 'content': {
        pendingText += String(data ?? '')
        scheduleFlush()
        break
      }
      case 'tool_start': {
        // 工具调用前的文本是「模型自述」而非答案：旧版直接丢弃，
        // 这里转存到 reasoning，用户点开还能看到它当时在想什么
        flush()
        if (last.content.trim()) {
          last.reasoning = (last.reasoning ? `${last.reasoning}\n` : '') + last.content.trim()
          last.content = ''
        }
        const name = (data as { name?: string } | null)?.name ?? ''
        markPhase(TOOL_PHASES[name]?.start ?? '正在查询相关信息')
        break
      }
      case 'tool_end': {
        const name = (data as { name?: string } | null)?.name ?? ''
        markPhase(TOOL_PHASES[name]?.end ?? '正在准备回复')
        break
      }
      case 'sources': {
        const incoming = (data as Source[] | null) ?? []
        const merged = [...(last.sources ?? [])]
        const seen = new Set(merged.map((s) => s.chunk_id ?? `${s.file}:${s.snippet}`))
        for (const source of incoming) {
          const key = source.chunk_id ?? `${source.file}:${source.snippet}`
          if (!seen.has(key)) {
            seen.add(key)
            merged.push(source)
          }
        }
        last.sources = merged
        break
      }
      case 'final': {
        flush()
        last.content = String(data ?? last.content)
        scrollHook?.()
        break
      }
      case 'error': {
        flush()
        const message = (data as { message?: string } | null)?.message ?? '未知错误'
        last.failed = true
        if (!last.content.trim()) last.content = `本次处理失败：${message}`
        error.value = message
        break
      }
      default:
        break
    }
  }

  function startTicker() {
    stopTicker()
    ticker = window.setInterval(() => {
      const now = performance.now()
      elapsed.value = (now - startedAt) / 1000
      stageElapsed.value = (now - stageAt) / 1000
    }, 100)
  }

  function stopTicker() {
    if (ticker) {
      window.clearInterval(ticker)
      ticker = 0
    }
  }

  async function send(query: string, threadId: string): Promise<boolean> {
    const text = query.trim()
    if (!text || streaming.value) return false

    error.value = null
    messages.value.push({ role: 'user', content: text })
    messages.value.push({ role: 'assistant', content: '', pending: true, sources: [] })

    streaming.value = true
    startedAt = performance.now()
    stageAt = startedAt
    elapsed.value = 0
    stageElapsed.value = 0
    firstTokenSeconds.value = null
    marks.value = []
    stage.value = '正在准备回复'
    requestId.value = ''

    startTicker()
    abortController = new AbortController()

    try {
      await streamChat(
        { query: text, thread_id: threadId, request_id: newRequestId() },
        handle,
        abortController.signal,
      )
    } catch (err) {
      flush()
      const last = messages.value[messages.value.length - 1]
      const aborted = err instanceof Error && err.name === 'AbortError'
      if (last && last.role === 'assistant') {
        if (aborted) {
          if (!last.content.trim()) last.content = '本次处理已停止。'
        } else {
          const message = err instanceof Error ? err.message : String(err)
          last.failed = true
          if (!last.content.trim()) last.content = `本次处理失败：${message}`
          error.value = message
        }
      }
    } finally {
      flush()
      if (rafId) {
        cancelAnimationFrame(rafId)
        rafId = 0
      }
      const last = messages.value[messages.value.length - 1]
      if (last) {
        last.pending = false
        last.liked = false
      }
      streaming.value = false
      stopTicker()
      // 定格总耗时：ticker 已停，这里补最后一次，否则显示会少掉尾部零头
      elapsed.value = (performance.now() - startedAt) / 1000
      abortController = null
      scrollHook?.()
    }
    return true
  }

  async function stop() {
    const rid = requestId.value
    if (rid) {
      // 先送达取消信号，比依赖断连检测更即时；失败也无妨，abort 仍然有效
      api.post('/api/chat/cancel', { request_id: rid }).catch(() => undefined)
    }
    abortController?.abort()
  }

  function setMessages(rows: Message[]) {
    messages.value = rows.map((row) => ({ ...row }))
    error.value = null
  }

  function reset() {
    abortController?.abort()
    stopTicker()
    pendingText = ''
    messages.value = []
    streaming.value = false
    error.value = null
    stage.value = ''
    marks.value = []
    elapsed.value = 0
    stageElapsed.value = 0
    firstTokenSeconds.value = null
  }

  function markLiked(index: number) {
    const target = messages.value[index]
    if (target) target.liked = true
  }

  onUnmounted(() => {
    abortController?.abort()
    stopTicker()
    if (rafId) cancelAnimationFrame(rafId)
  })

  return {
    messages,
    streaming,
    stage,
    elapsed,
    stageElapsed,
    firstTokenSeconds,
    marks,
    error,
    requestId,
    send,
    stop,
    reset,
    setMessages,
    markLiked,
    setScrollHook,
  }
}
