<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError, api } from '../api/client'
import AppSidebar from '../components/AppSidebar.vue'
import MessageItem from '../components/MessageItem.vue'
import StageBar from '../components/StageBar.vue'
import { useAuth } from '../composables/useAuth'
import { useChat } from '../composables/useChat'
import type { HistoryPage, Message, SessionRow, SystemStatus } from '../types'

const route = useRoute()
const router = useRouter()
const { me, load: loadMe, logout, switchUser } = useAuth()
const chat = useChat()
// 注意：必须解构出 error。模板里写 chat.error 拿到的是 ref 对象本身，
// 永远 truthy，会导致"处理失败"常驻显示。
const {
  messages,
  streaming,
  stopping,
  stage,
  elapsed,
  stageElapsed,
  marks,
  firstTokenSeconds,
  error: chatError,
} = chat

const sessions = ref<SessionRow[]>([])
const currentThread = ref('')
const defaultThread = ref('')
const status = ref<SystemStatus | null>(null)
const info = ref<{ indexed_files: number; chunks: number; models: Record<string, unknown> } | null>(null)
const draft = ref('')
const bootError = ref<string | null>(null)
/** 已加载的历史在完整历史中的起始下标；点赞 key 依赖它，不能用页内下标 */
const historyStart = ref(0)
const historyTotal = ref(0)
const hasMore = ref(false)
const loadingEarlier = ref(false)
/**
 * 侧栏是否可见。
 *
 * 桌面端是"折叠"（宽度归零，主区域变宽），移动端是"抽屉"（translateX 移出）。
 * 初始值按视口决定：窄屏先收起，避免一进来就盖住正文。
 */
const sidebarVisible = ref(typeof window === 'undefined' ? true : window.innerWidth > 860)

const scroller = ref<HTMLElement>()
const content = ref<HTMLElement>()
const textarea = ref<HTMLTextAreaElement>()
let statusTimer = 0
let contentObserver: ResizeObserver | null = null
/** 用户是否停在底部。停在底部时新内容自动跟随，往上翻则不打扰。 */
let followBottom = true
/**
 * 最近一次程序滚动的时刻。
 *
 * ``scrollToBottom`` 自己也会触发 scroll 事件，若不区分，初始加载时
 * 会出现：内容很高 → 滚到底 → scroll 事件 → 重新计算 isAtBottom，
 * 若此刻布局尚未稳定则判为"不在底部" → followBottom=false → 此后再不跟随。
 * 这就是会话打开后停在历史中间的原因。
 */
let lastAutoScroll = 0

const EXAMPLES = [
  'HEPA 滤网多久需要更换一次？',
  '机器人不回充电座是什么原因？',
  '扫拖一体机和扫地机器人怎么选？',
]

const displaySessions = computed<SessionRow[]>(() => {
  if (!currentThread.value) return sessions.value
  if (sessions.value.some((s) => s.id === currentThread.value)) return sessions.value
  // 新建但还没发消息的空会话不在后端列表里，补一个占位让它能高亮
  return [{ id: currentThread.value, title: '（新会话，还没开始聊）', rounds: 0, updated: '' }, ...sessions.value]
})

const showWelcome = computed(() => !messages.value.length && !streaming.value)
const canStop = computed(() => streaming.value)

// ---------------------------------------------------------------- 数据加载

async function checkStatus() {
  try {
    status.value = await api.get<SystemStatus>('/api/system/status')
    if (status.value.error) bootError.value = status.value.error
  } catch {
    status.value = null
  }
}

async function loadInfo() {
  try {
    info.value = await api.get('/api/system/info')
  } catch {
    info.value = null
  }
}

async function loadSessions() {
  try {
    const res = await api.get<{ sessions: SessionRow[]; default: string }>('/api/sessions')
    sessions.value = res.sessions
    defaultThread.value = res.default
  } catch {
    sessions.value = []
  }
}

async function loadHistory() {
  if (!currentThread.value) return
  try {
    const res = await api.get<HistoryPage>(
      `/api/sessions/${encodeURIComponent(currentThread.value)}/history?limit=60`,
    )
    chat.setMessages(res.messages)
    historyStart.value = res.start
    historyTotal.value = res.total
    hasMore.value = res.has_more
    await settleScroll()
  } catch (err) {
    if (err instanceof ApiError && err.status === 403) {
      // 会话不属于当前身份：回到默认会话而不是报错
      currentThread.value = defaultThread.value
      syncUrl()
      await loadHistory()
      return
    }
    if (err instanceof ApiError && err.status === 503) return
    chat.setMessages([])
    historyStart.value = 0
    historyTotal.value = 0
    hasMore.value = false
  }
}

/** 向上加载更早的一页，并补偿滚动位置，避免视图跳到顶部。 */
async function loadEarlier() {
  if (!hasMore.value || loadingEarlier.value) return
  const box = scroller.value
  const beforeHeight = box?.scrollHeight ?? 0
  loadingEarlier.value = true
  try {
    const res = await api.get<HistoryPage>(
      `/api/sessions/${encodeURIComponent(currentThread.value)}/history?limit=60&before=${historyStart.value}`,
    )
    chat.prependMessages(res.messages)
    historyStart.value = res.start
    hasMore.value = res.has_more
    await nextTick()
    if (box) box.scrollTop += box.scrollHeight - beforeHeight
  } catch {
    /* 加载失败保持原样，用户可再点一次 */
  } finally {
    loadingEarlier.value = false
  }
}

/**
 * 滚到底。
 *
 * 早先按"定时器补滚两次"处理，实测仍会停在历史中部：markdown 渲染、
 * 字体替换、引用面板展开都会继续改高度，一次 scrollTop 会被顶回去。
 * 改为 ResizeObserver 持续监听内容高度，只要用户停在底部就跟着走。
 */
function isAtBottom(el: HTMLElement, threshold = 90): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < threshold
}

function scrollToBottom(force = false) {
  const el = scroller.value
  if (!el) return
  // 用 followBottom 而不是实时 isAtBottom 判断：流式追加时内容已经变高，
  // 此刻 isAtBottom 必然是 false，会导致"永远不跟随"。
  if (!force && !followBottom) return
  lastAutoScroll = performance.now()
  el.scrollTop = el.scrollHeight
  followBottom = true
}

function onScroll() {
  // 自己触发的滚动不算"用户往上翻"
  if (performance.now() - lastAutoScroll < 150) return
  const el = scroller.value
  if (!el) return
  followBottom = isAtBottom(el)
}

async function settleScroll() {
  await nextTick()
  scrollToBottom(true)
  requestAnimationFrame(() => scrollToBottom(true))
}

function stopPolling() {
  if (statusTimer) {
    window.clearInterval(statusTimer)
    statusTimer = 0
  }
}

function startPolling() {
  stopPolling()
  statusTimer = window.setInterval(async () => {
    await checkStatus()
    if (status.value?.ready) {
      stopPolling()
      await loadSessions()
      await loadHistory()
    } else if (status.value?.error) {
      stopPolling()
    }
  }, 1000)
}

async function retryBoot() {
  bootError.value = null
  await checkStatus()
  try {
    const res = await api.post<SystemStatus & { ok: boolean; error?: string }>('/api/system/retry')
    if (res.booting) {
      // 已有初始化在进行：请求被立即接受但不会另起一次，恢复轮询等它完成
      startPolling()
    } else if (!res.ok) {
      bootError.value = res.error ?? '初始化失败'
    }
  } catch (err) {
    bootError.value = err instanceof Error ? err.message : String(err)
  }
  await checkStatus()
  if (status.value?.ready) {
    await loadSessions()
    currentThread.value = currentThread.value || defaultThread.value
    await loadHistory()
  }
}

// ---------------------------------------------------------------- 交互

function syncUrl() {
  if (!currentThread.value) return
  if (route.query.chat === currentThread.value) return
  router.replace({ query: { ...route.query, chat: currentThread.value } })
}

function resize() {
  const el = textarea.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 168)}px`
}

async function send(text?: string) {
  const query = (text ?? draft.value).trim()
  if (!query || streaming.value) return
  draft.value = ''
  await nextTick()
  resize()
  scrollToBottom(true)
  await chat.send(query, currentThread.value)
  syncUrl()
  await loadSessions()
}

async function stop() {
  await chat.stop()
}

async function selectThread(id: string) {
  if (streaming.value || id === currentThread.value) return
  currentThread.value = id
  collapseOnMobile()
  chat.reset()
  syncUrl()
  await loadHistory()
}

async function newSession() {
  if (streaming.value) return
  try {
    const res = await api.post<{ thread_id: string }>('/api/sessions')
    currentThread.value = res.thread_id
    chat.reset()
    syncUrl()
    collapseOnMobile()
    await nextTick()
    textarea.value?.focus()
  } catch {
    /* 新建失败时保持当前会话不变 */
  }
}

/** 只在窄屏收起侧栏：桌面端切换会话时侧栏闪一下反而干扰。 */
function collapseOnMobile() {
  if (window.innerWidth <= 860) sidebarVisible.value = false
}

async function deleteSession(id: string) {
  try {
    await api.del(`/api/sessions/${encodeURIComponent(id)}`)
  } catch {
    return
  }
  await loadSessions()
  if (id === currentThread.value) await newSession()
}

async function clearOthers() {
  try {
    await api.post('/api/sessions/clear-others', { current: currentThread.value })
  } catch {
    return
  }
  await loadSessions()
}

async function onSwitchUser(userId: string) {
  if (streaming.value) return
  await switchUser(userId)
  chat.reset()
  await loadSessions()
  currentThread.value = defaultThread.value
  syncUrl()
  await loadHistory()
}

async function onLogout() {
  await logout()
  router.push('/')
}

function findQuestion(index: number): string {
  for (let i = index - 1; i >= 0; i -= 1) {
    if (messages.value[i]?.role === 'user') return messages.value[i].content
  }
  return ''
}

async function onLike(payload: { index: number; answer: string }) {
  try {
    await api.post('/api/feedback/like', {
      thread_id: currentThread.value,
      // 服务端按完整历史计下标，这里必须加上已加载部分的起点
      index: historyStart.value + payload.index,
      question: findQuestion(payload.index),
      answer: payload.answer,
      sources: messages.value[payload.index]?.sources ?? [],
    })
    chat.markLiked(payload.index)
  } catch {
    /* 点赞失败不打断阅读 */
  }
}

/** 重新生成：先回退上一轮（连提问一起删），再把同一个问题重发一遍。 */
async function regenerate(payload: { index: number }) {
  if (streaming.value) return
  const question = findQuestion(payload.index)
  if (!question) return
  try {
    await api.post(`/api/sessions/${encodeURIComponent(currentThread.value)}/truncate`)
  } catch {
    return
  }
  chat.reset()
  await loadHistory()
  await chat.send(question, currentThread.value)
  await loadSessions()
}

function isLastAssistant(index: number): boolean {
  return index === messages.value.length - 1 && messages.value[index]?.role === 'assistant'
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    send()
  }
}

// ---------------------------------------------------------------- 生命周期

onMounted(async () => {
  contentObserver = new ResizeObserver(() => {
    // 内容变高时，若用户本来就在底部，就跟到底
    if (followBottom) scrollToBottom(true)
  })
  await loadMe()
  await loadInfo()
  await checkStatus()
  await loadSessions()

  const fromUrl = typeof route.query.chat === 'string' ? route.query.chat : ''
  currentThread.value = fromUrl || defaultThread.value
  chat.setScrollHook(() => scrollToBottom())

  if (status.value?.ready) {
    await loadHistory()
    syncUrl()
  } else if (!status.value?.error) {
    startPolling()
  }
})

watch(content, (el, previous) => {
  if (!contentObserver) return
  if (previous) contentObserver.unobserve(previous)
  if (el) contentObserver.observe(el)
})

onUnmounted(() => {
  stopPolling()
  contentObserver?.disconnect()
  contentObserver = null
})
</script>

<template>
  <div class="app">
    <AppSidebar
      :class="{ 'sidebar--open': sidebarVisible, 'sidebar--collapsed': !sidebarVisible }"
      :inert="!sidebarVisible"
      :me="me ?? { authenticated: false, user_id: null, demo_mode: true, password_required: false, users: [], admin_enabled: false }"
      :sessions="displaySessions"
      :current-thread="currentThread"
      :streaming="streaming"
      :status="status"
      :info="info"
      @switch-user="onSwitchUser"
      @select="selectThread"
      @new-session="newSession"
      @delete-session="deleteSession"
      @clear-others="clearOthers"
      @logout="onLogout"
      @close="sidebarVisible = false"
    />

    <div class="main">
      <!-- 侧栏收起后出现的展开入口，桌面端和移动端共用 -->
      <div v-if="!sidebarVisible" class="mobile-bar">
        <button class="icon-btn" @click="sidebarVisible = true">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" width="14" height="14">
            <path d="M2 4h12M2 8h12M2 12h12" stroke-linecap="round" />
          </svg>
          展开会话
        </button>
        <span class="mobile-bar__title">智扫通</span>
      </div>

      <!-- 初始化未完成 -->
      <div v-if="!status?.ready" class="boot">
        <div class="boot__card">
          <template v-if="bootError || status?.error">
            <div class="notice notice--danger" style="margin: 0 0 12px">
              <strong>客服初始化失败</strong>
              <div style="margin-top: 4px; word-break: break-all">{{ bootError || status?.error }}</div>
            </div>
            <p class="boot__hint">请检查模型配置、网络和知识文件。修复后可直接重试，已发布的知识库会保留。</p>
            <button class="btn btn--primary" @click="retryBoot">重新初始化</button>
          </template>
          <template v-else>
            <div style="display: flex; align-items: center; gap: 10px">
              <span class="spin" />
              <span style="font-size: 13.5px">{{ status?.stage || '正在初始化客服组件' }}</span>
            </div>
            <p class="boot__hint">
              服务启动时已在后台预加载，通常几秒内完成。旧版 Streamlit 需要等到第一个页面会话才开始加载。
            </p>
          </template>
        </div>
      </div>

      <template v-else>
        <div ref="scroller" class="main__scroll" @scroll.passive="onScroll">
          <div ref="content" class="main__inner">
            <div v-if="hasMore" class="load-earlier">
              <button class="btn" :disabled="loadingEarlier" @click="loadEarlier">
                <span v-if="loadingEarlier" class="spin" />
                {{ loadingEarlier ? '正在加载' : `加载更早的消息（共 ${historyTotal} 条）` }}
              </button>
            </div>

            <div v-if="me?.demo_mode" class="notice notice--warn">
              未设置 <code>APP_PASSWORD</code>，当前任何人可访问，仅供本地演示使用。
            </div>

            <div v-if="showWelcome" class="welcome">
              <h1>你好，我是智扫通</h1>
              <p>扫地 / 扫拖机器人的专业客服。可以问我选购建议、使用方式、故障排查和维护保养。</p>
              <div class="welcome__examples">
                <button v-for="example in EXAMPLES" :key="example" class="chip" @click="send(example)">
                  {{ example }}
                </button>
              </div>
            </div>

            <MessageItem
              v-for="(message, index) in messages"
              :key="index"
              :message="message"
              :index="index"
              :thread-id="currentThread"
              :question="findQuestion(index)"
              :can-regenerate="!streaming && isLastAssistant(index) && !message.incomplete"
              @like="onLike"
              @regenerate="regenerate"
            />

            <StageBar
              v-if="streaming"
              :stage="stage"
              :elapsed="elapsed"
              :stage-elapsed="stageElapsed"
              :marks="marks"
              :stopping="stopping"
            />

            <div v-else-if="elapsed > 0" class="stats">
              本次回复用时 {{ elapsed.toFixed(1) }} 秒
              <template v-if="firstTokenSeconds !== null"> · 开始出字 {{ firstTokenSeconds.toFixed(1) }} 秒</template>
            </div>
          </div>
        </div>

        <div class="composer">
          <div class="composer__inner">
            <div class="composer__box">
              <textarea
                ref="textarea"
                v-model="draft"
                class="composer__input"
                rows="1"
                :placeholder="streaming ? '正在回复，可点击右侧停止' : '请输入您的问题'"
                :disabled="streaming"
                @keydown="onKeydown"
                @input="resize"
              />
              <button v-if="canStop" class="send-btn send-btn--stop" title="停止生成" @click="stop">
                <svg viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="2" /></svg>
              </button>
              <button v-else class="send-btn" title="发送" :disabled="!draft.trim()" @click="send()">
                <svg viewBox="0 0 24 24"><path d="M4 12l16-8-6 8 6 8z" /></svg>
              </button>
            </div>
            <div class="composer__hint">
              <span>Enter 发送，Shift + Enter 换行</span>
              <span v-if="chatError">处理失败，可重试或换个问法</span>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.boot {
  flex: 1;
  display: grid;
  place-items: center;
  padding: 24px;
}

.boot__card {
  width: 100%;
  max-width: 460px;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  padding: 22px;
  background: #fff;
}

.boot__hint {
  font-size: 12px;
  color: var(--text-3);
  margin: 12px 0 0;
  line-height: 1.6;
}

.welcome {
  padding: 48px 0 24px;
}

.welcome h1 {
  font-size: 24px;
  font-weight: 600;
  margin: 0 0 8px;
}

.welcome p {
  color: var(--text-2);
  margin: 0 0 22px;
  font-size: 13.5px;
}

.welcome__examples {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.chip {
  padding: 8px 14px;
  border: 1px solid var(--line);
  border-radius: 999px;
  font-size: 13px;
  color: var(--text-2);
  background: #fff;
  transition: border-color 0.12s, color 0.12s, background 0.12s;
}

.chip:hover {
  border-color: var(--brand);
  color: var(--brand-dark);
  background: var(--brand-soft);
}

.stats {
  font-size: 11.5px;
  color: var(--text-3);
  padding: 0 0 18px 42px;
}

.load-earlier {
  display: flex;
  justify-content: center;
  padding: 4px 0 18px;
}

.mobile-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--line-soft);
  flex: 0 0 auto;
}

.mobile-bar__title {
  font-size: 13px;
  font-weight: 500;
}
</style>
