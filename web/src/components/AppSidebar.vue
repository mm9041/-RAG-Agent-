<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

import type { AuthMe, SessionRow, SystemStatus } from '../types'

const props = defineProps<{
  me: AuthMe
  sessions: SessionRow[]
  currentThread: string
  streaming: boolean
  status: SystemStatus | null
  info: { indexed_files: number; chunks: number; models: Record<string, unknown> } | null
}>()

const emit = defineEmits<{
  switchUser: [userId: string]
  select: [threadId: string]
  newSession: []
  deleteSession: [threadId: string]
  clearOthers: []
  logout: []
  close: []
}>()

const pendingDelete = ref<string | null>(null)
const pendingClear = ref(false)

// 接口最多回 50 个会话，但一次性全渲染会把侧栏撑得很长；
// 先显示最近 20 个，剩下的点「显示更多」再放出来。
const PAGE = 20
const visibleCount = ref(PAGE)
const visibleSessions = computed(() => props.sessions.slice(0, visibleCount.value))
const hiddenCount = computed(() => Math.max(0, props.sessions.length - visibleCount.value))

function showMore() {
  visibleCount.value += PAGE
}

// 刻意**不**做"当前会话在折叠区外就自动展开"：默认会话 user-1001 本身很旧，
// 排在列表后段，一加载就会把折叠区撑到 40 多条，等于没折叠。
// 代价是深链接打开很旧的会话时侧栏看不到高亮，点一次「显示更多」即可。

watch(
  () => props.currentThread,
  () => {
    pendingDelete.value = null
    pendingClear.value = false
  },
)

// 两种确认共用一个浮窗，所以打开一个必须先关掉另一个
function askDelete(threadId: string) {
  pendingClear.value = false
  pendingDelete.value = threadId
}

function askClear() {
  pendingDelete.value = null
  pendingClear.value = true
}

function closeConfirm() {
  pendingDelete.value = null
  pendingClear.value = false
}

function confirmDelete(threadId: string) {
  emit('deleteSession', threadId)
  closeConfirm()
}

function confirmClear() {
  emit('clearOthers')
  closeConfirm()
}

// 浮窗盖住了侧栏，键盘用户需要一个不用鼠标就能退出的方式
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && (pendingDelete.value || pendingClear.value)) closeConfirm()
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <aside class="sidebar">
    <div class="sidebar__head">
      <div class="brand-dot">智</div>
      <div class="brand-name">智扫通</div>
      <button class="icon-btn" style="margin-left: auto" title="收起侧栏" @click="emit('close')">
        <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6">
          <path d="M10 3L5 8l5 5" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </button>
    </div>

    <div class="sidebar__body">
      <div class="side-label">知识库</div>
      <template v-if="info">
        <div class="kb-line"><span>已索引</span><span>{{ info.indexed_files }} 文件 / {{ info.chunks }} 片段</span></div>
        <div class="kb-line"><span>聊天模型</span><span>{{ info.models.chat }}</span></div>
        <div class="kb-line"><span>嵌入模型</span><span>{{ info.models.embedding }}</span></div>
        <div class="kb-line"><span>重排模型</span><span>{{ info.models.rerank || '未配置' }}</span></div>
      </template>
      <div v-else class="kb-line"><span>状态</span><span>{{ status?.stage || '读取中' }}</span></div>

      <div class="side-label">登录身份（由会话注入，模型无法更改）</div>
      <select
        class="side-select"
        :value="me.user_id ?? ''"
        :disabled="streaming"
        @change="emit('switchUser', ($event.target as HTMLSelectElement).value)"
      >
        <option v-for="user in me.users" :key="user" :value="user">{{ user }}</option>
      </select>
      <div v-if="me.demo_mode" class="demo-tip">演示模式 · 未设置 APP_PASSWORD</div>

      <template v-if="me.admin_enabled">
        <div class="side-label">管理</div>
        <RouterLink to="/admin" class="nav-item">
          <span class="nav-item__title">管理控制台</span>
          <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.6">
            <path d="M6 3l5 5-5 5" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </RouterLink>
      </template>

      <div class="side-label">历史会话</div>
      <button class="side-btn side-btn--ghost" :disabled="streaming" @click="emit('newSession')">
        ＋ 开始新会话
      </button>

      <div style="margin-top: 4px">
        <button
          v-for="session in visibleSessions"
          :key="session.id"
          class="nav-item"
          :class="{ 'nav-item--active': session.id === currentThread }"
          :disabled="streaming"
          @click="emit('select', session.id)"
        >
          <span class="nav-item__title">{{ session.title }}</span>
          <span class="nav-item__meta">{{ session.rounds }} 轮</span>
          <span
            class="nav-item__meta"
            role="button"
            tabindex="0"
            title="删除该会话"
            style="padding: 0 3px"
            @click.stop="askDelete(session.id)"
            @keydown.enter.stop="askDelete(session.id)"
            >×</span
          >
        </button>
        <div v-if="!sessions.length" class="empty" style="padding: 16px 0">还没有历史会话</div>
        <button
          v-if="hiddenCount > 0"
          class="side-btn side-btn--ghost"
          style="margin-top: 2px"
          @click="showMore"
        >
          显示更多（还有 {{ hiddenCount }} 个）
        </button>
      </div>

      <div v-if="sessions.length > 1" style="margin-top: 8px">
        <button class="side-btn side-btn--ghost" @click="askClear">清理其他会话</button>
      </div>
    </div>

    <div v-if="me.password_required" class="sidebar__foot">
      <button class="side-btn side-btn--ghost" @click="emit('logout')">退出登录</button>
    </div>

    <!-- 确认浮窗：绝对定位在侧栏内，垂直居中；盖住侧栏而不是挤动列表 -->
    <div
      v-if="pendingDelete || pendingClear"
      class="side-confirm"
      role="presentation"
      @click.self="closeConfirm"
    >
      <div class="side-confirm__card" role="dialog" aria-modal="true" aria-labelledby="side-confirm-text">
        <p class="side-confirm__text" id="side-confirm-text">
          {{ pendingDelete ? '确认删除该会话？该操作不可恢复。' : '确认清理除当前会话外的全部会话？' }}
        </p>
        <div class="side-confirm__actions">
          <button class="btn side-confirm__btn" @click="closeConfirm">取消</button>
          <button
            v-if="pendingDelete"
            class="btn btn--danger side-confirm__btn"
            @click="confirmDelete(pendingDelete)"
          >
            确认删除
          </button>
          <button v-else class="btn btn--danger side-confirm__btn" @click="confirmClear">确认清理</button>
        </div>
      </div>
    </div>
  </aside>
</template>
