<script setup lang="ts">
import { ref, watch } from 'vue'

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

watch(
  () => props.currentThread,
  () => {
    pendingDelete.value = null
    pendingClear.value = false
  },
)

function confirmDelete(threadId: string) {
  emit('deleteSession', threadId)
  pendingDelete.value = null
}

function confirmClear() {
  emit('clearOthers')
  pendingClear.value = false
}
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
          v-for="session in sessions"
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
            @click.stop="pendingDelete = session.id"
            @keydown.enter.stop="pendingDelete = session.id"
            >×</span
          >
        </button>
        <div v-if="!sessions.length" class="empty" style="padding: 16px 0">还没有历史会话</div>
      </div>

      <div v-if="pendingDelete" class="notice notice--danger" style="margin: 8px 4px 0; font-size: 12px">
        确认删除该会话？该操作不可恢复。
        <div style="display: flex; gap: 6px; margin-top: 6px">
          <button class="btn btn--danger" style="padding: 4px 10px" @click="confirmDelete(pendingDelete)">确认删除</button>
          <button class="btn" style="padding: 4px 10px" @click="pendingDelete = null">取消</button>
        </div>
      </div>

      <div v-if="sessions.length > 1" style="margin-top: 8px">
        <button v-if="!pendingClear" class="side-btn side-btn--ghost" @click="pendingClear = true">
          清理其他会话
        </button>
        <div v-else class="notice notice--danger" style="margin: 4px 4px 0; font-size: 12px">
          确认清理除当前会话外的全部会话？
          <div style="display: flex; gap: 6px; margin-top: 6px">
            <button class="btn btn--danger" style="padding: 4px 10px" @click="confirmClear">确认清理</button>
            <button class="btn" style="padding: 4px 10px" @click="pendingClear = false">取消</button>
          </div>
        </div>
      </div>
    </div>

    <div v-if="me.password_required" class="sidebar__foot">
      <button class="side-btn side-btn--ghost" @click="emit('logout')">退出登录</button>
    </div>
  </aside>
</template>
