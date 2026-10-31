<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { ApiError, api } from '../api/client'

interface KnowledgeFile {
  name: string
  size: number
  version: string
  versions: number
}

interface Overview {
  files: number
  bytes: number
  likes: number
  feedback: number
  indexed_files: number
  chunks: number
  models: { chat: string; embedding: string; rerank: string; rerank_enabled: boolean }
  system: { ready: boolean; stage: string; boot_seconds: number | null; error: string | null }
}

type Tab = 'overview' | 'knowledge' | 'feedback'

const router = useRouter()

const admin = ref(false)
const enabled = ref(false)
const checked = ref(false)
const tab = ref<Tab>('overview')

const password = ref('')
const loginError = ref('')
const loggingIn = ref(false)

const overview = ref<Overview | null>(null)
const files = ref<KnowledgeFile[]>([])
const rows = ref<Record<string, unknown>[]>([])
const notice = ref<{ kind: 'ok' | 'danger' | 'info'; text: string } | null>(null)

const models = ref({ chat: '', embedding: '', rerank: '' })
const confirmRebuild = ref(false)
const savingModels = ref(false)

const uploadFile = ref<File | null>(null)
const preview = ref<{ name: string; text: string; warnings: string[]; exists: boolean; size: number } | null>(null)
const modelName = ref('')
const scope = ref('')
const replace = ref(false)
const publishing = ref(false)
const previewing = ref(false)
const pendingDelete = ref<string | null>(null)
const openVersions = ref<string | null>(null)
const versionList = ref<string[]>([])

const modelChanged = computed(
  () => overview.value !== null && models.value.embedding.trim() !== overview.value.models.embedding,
)

function flash(kind: 'ok' | 'danger' | 'info', text: string) {
  notice.value = { kind, text }
  window.setTimeout(() => {
    if (notice.value?.text === text) notice.value = null
  }, 5000)
}

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

// ---------------------------------------------------------------- 鉴权

async function checkAdmin() {
  try {
    const res = await api.get<{ admin: boolean; enabled: boolean }>('/api/admin/me')
    admin.value = res.admin
    enabled.value = res.enabled
  } catch {
    admin.value = false
  } finally {
    checked.value = true
  }
  if (admin.value) await loadAll()
}

async function loginAdmin() {
  if (!password.value || loggingIn.value) return
  loggingIn.value = true
  loginError.value = ''
  try {
    await api.post('/api/admin/login', { password: password.value })
    password.value = ''
    await checkAdmin()
  } catch (err) {
    loginError.value = err instanceof ApiError ? err.message : String(err)
  } finally {
    loggingIn.value = false
  }
}

async function logoutAdmin() {
  try {
    await api.post('/api/admin/logout')
  } catch {
    /* 已失效也照样退出 */
  }
  admin.value = false
  overview.value = null
}

// ---------------------------------------------------------------- 数据

async function loadOverview() {
  try {
    const data = await api.get<Overview>('/api/admin/overview')
    overview.value = data
    models.value = { ...data.models }
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) admin.value = false
  }
}

async function loadKnowledge() {
  try {
    const res = await api.get<{ files: KnowledgeFile[] }>('/api/admin/knowledge')
    files.value = res.files
  } catch {
    files.value = []
  }
}

async function loadFeedback() {
  try {
    const res = await api.get<{ rows: Record<string, unknown>[] }>('/api/admin/feedback')
    rows.value = res.rows
  } catch {
    rows.value = []
  }
}

async function loadAll() {
  await Promise.all([loadOverview(), loadKnowledge(), loadFeedback()])
}

function switchTab(next: Tab) {
  tab.value = next
  if (next === 'overview') loadOverview()
  if (next === 'knowledge') loadKnowledge()
  if (next === 'feedback') loadFeedback()
}

// ---------------------------------------------------------------- 模型配置

async function saveModels() {
  if (savingModels.value) return
  if (modelChanged.value && !confirmRebuild.value) {
    flash('danger', '更换嵌入模型需要重新生成整个知识库，请先勾选确认。')
    return
  }
  savingModels.value = true
  try {
    const res = await api.put<{ message: string }>('/api/admin/models', {
      chat: models.value.chat,
      embedding: models.value.embedding,
      rerank: models.value.rerank,
      confirm_rebuild: confirmRebuild.value,
    })
    flash('ok', res.message)
    confirmRebuild.value = false
    await loadOverview()
  } catch (err) {
    flash('danger', err instanceof ApiError ? err.message : String(err))
  } finally {
    savingModels.value = false
  }
}

// ---------------------------------------------------------------- 知识文件

async function onPickFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  uploadFile.value = file
  preview.value = null
  if (!file) return

  previewing.value = true
  const form = new FormData()
  form.append('file', file)
  try {
    const res = await api.upload<{ name: string; text: string; warnings: string[]; exists: boolean; size: number }>(
      '/api/admin/knowledge/preview',
      form,
    )
    preview.value = res
    replace.value = res.exists
  } catch (err) {
    flash('danger', err instanceof ApiError ? err.message : String(err))
    uploadFile.value = null
  } finally {
    previewing.value = false
    input.value = ''
  }
}

async function publishFile() {
  if (!uploadFile.value || publishing.value) return
  publishing.value = true
  const form = new FormData()
  form.append('file', uploadFile.value)
  form.append('model', modelName.value)
  form.append('scope', scope.value)
  form.append('replace', String(replace.value))
  try {
    const res = await api.upload<{ message: string }>('/api/admin/knowledge/publish', form)
    flash('ok', res.message)
    uploadFile.value = null
    preview.value = null
    modelName.value = ''
    scope.value = ''
    replace.value = false
    await Promise.all([loadKnowledge(), loadOverview()])
  } catch (err) {
    flash('danger', err instanceof ApiError ? err.message : String(err))
  } finally {
    publishing.value = false
  }
}

async function deleteFile(name: string) {
  pendingDelete.value = null
  try {
    const res = await api.del<{ message: string }>(`/api/admin/knowledge/${encodeURIComponent(name)}`)
    flash('ok', res.message)
    await Promise.all([loadKnowledge(), loadOverview()])
  } catch (err) {
    flash('danger', err instanceof ApiError ? err.message : String(err))
  }
}

async function toggleVersions(name: string) {
  if (openVersions.value === name) {
    openVersions.value = null
    versionList.value = []
    return
  }
  openVersions.value = name
  try {
    const res = await api.get<{ versions: string[] }>(`/api/admin/knowledge/versions/${encodeURIComponent(name)}`)
    versionList.value = res.versions
  } catch {
    versionList.value = []
  }
}

function versionHref(name: string, version: string): string {
  return `/api/admin/knowledge/versions/${encodeURIComponent(name)}/${encodeURIComponent(version)}`
}

onMounted(checkAdmin)
</script>

<template>
  <div class="admin">
    <div class="admin__nav">
      <div class="sidebar__head" style="padding: 4px 4px 16px">
        <div class="brand-dot">智</div>
        <div class="brand-name">管理控制台</div>
      </div>

      <button class="nav-item" :class="{ 'nav-item--active': tab === 'overview' }" @click="switchTab('overview')">
        <span class="nav-item__title">概览与模型</span>
      </button>
      <button class="nav-item" :class="{ 'nav-item--active': tab === 'knowledge' }" @click="switchTab('knowledge')">
        <span class="nav-item__title">知识文件</span>
      </button>
      <button class="nav-item" :class="{ 'nav-item--active': tab === 'feedback' }" @click="switchTab('feedback')">
        <span class="nav-item__title">反馈与点赞</span>
      </button>

      <div style="margin-top: 18px; display: flex; flex-direction: column; gap: 6px">
        <RouterLink to="/" class="side-btn side-btn--ghost">← 返回用户端</RouterLink>
        <button v-if="admin" class="side-btn side-btn--ghost" @click="logoutAdmin">退出管理员</button>
      </div>
    </div>

    <div class="admin__body">
      <div class="admin__inner">
        <div v-if="!checked" class="empty"><span class="spin" /> 正在检查登录状态</div>

        <!-- 未登录 -->
        <div v-else-if="!admin" class="admin__gate">
          <h1 class="admin__title">管理员登录</h1>
          <p class="admin__sub">管理知识资料、查看运行配置与用户反馈</p>
          <div v-if="!enabled" class="notice notice--warn">
            未设置 <code>ADMIN_PASSWORD</code>，管理员功能未启用。
          </div>
          <form v-else @submit.prevent="loginAdmin">
            <div class="field">
              <label class="field__label" for="admin-pwd">管理员密码</label>
              <input
                id="admin-pwd"
                v-model="password"
                class="input"
                type="password"
                autocomplete="current-password"
                :disabled="loggingIn"
              />
            </div>
            <div v-if="loginError" class="notice notice--danger">{{ loginError }}</div>
            <button class="btn btn--primary" type="submit" :disabled="loggingIn">
              <span v-if="loggingIn" class="spin" />
              {{ loggingIn ? '正在验证' : '登录' }}
            </button>
          </form>
          <p class="admin__hint">令牌有效期 8 小时，改密码后需重新登录。</p>
        </div>

        <template v-else>
          <div v-if="notice" class="notice" :class="`notice--${notice.kind}`">{{ notice.text }}</div>

          <!-- 概览 -->
          <template v-if="tab === 'overview'">
            <h1 class="admin__title">管理概览</h1>
            <p class="admin__sub">管理知识资料、查看运行配置与用户反馈</p>

            <div v-if="overview" class="metrics">
              <div class="metric">
                <div class="metric__value">{{ overview.files }}</div>
                <div class="metric__label">知识文件</div>
              </div>
              <div class="metric">
                <div class="metric__value">{{ humanSize(overview.bytes) }}</div>
                <div class="metric__label">资料体积</div>
              </div>
              <div class="metric">
                <div class="metric__value">{{ overview.chunks }}</div>
                <div class="metric__label">索引片段</div>
              </div>
              <div class="metric">
                <div class="metric__value">{{ overview.likes }}</div>
                <div class="metric__label">用户点赞</div>
              </div>
              <div class="metric">
                <div class="metric__value">{{ overview.feedback }}</div>
                <div class="metric__label">反馈总数</div>
              </div>
            </div>

            <div v-if="overview" class="panel">
              <div class="panel__head">
                <h2>运行状态</h2>
              </div>
              <div class="kv">
                <span>服务状态</span>
                <span :style="{ color: overview.system.ready ? 'var(--ok)' : 'var(--warn)' }">
                  {{ overview.system.ready ? '就绪' : overview.system.stage }}
                </span>
              </div>
              <div class="kv">
                <span>启动耗时</span>
                <span>{{ overview.system.boot_seconds ?? '—' }} 秒</span>
              </div>
              <div class="kv">
                <span>重排</span>
                <span>{{ overview.models.rerank_enabled ? '已启用' : '未启用' }}</span>
              </div>
            </div>

            <div class="panel">
              <div class="panel__head">
                <h2>模型配置</h2>
              </div>
              <div class="field">
                <label class="field__label" for="m-chat">聊天模型</label>
                <input id="m-chat" v-model="models.chat" class="input" />
              </div>
              <div class="field">
                <label class="field__label" for="m-embed">嵌入模型</label>
                <input id="m-embed" v-model="models.embedding" class="input" />
              </div>
              <div class="field">
                <label class="field__label" for="m-rerank">重排模型</label>
                <input id="m-rerank" v-model="models.rerank" class="input" />
              </div>
              <label class="check">
                <input v-model="confirmRebuild" type="checkbox" :disabled="!modelChanged" />
                我确认更换嵌入模型时重新生成知识库向量
              </label>
              <p class="admin__hint">
                聊天 / 重排模型保存后用于下一次问答，无需重启；更换嵌入模型会重建全部向量，失败时自动恢复原配置。
              </p>
              <button class="btn btn--primary" :disabled="savingModels" @click="saveModels">
                <span v-if="savingModels" class="spin" />
                {{ savingModels ? '正在应用' : '保存并应用模型' }}
              </button>
            </div>
          </template>

          <!-- 知识文件 -->
          <template v-else-if="tab === 'knowledge'">
            <h1 class="admin__title">知识库管理</h1>
            <p class="admin__sub">上传、替换或移除资料，发布后立即重建索引</p>

            <div class="panel">
              <div class="panel__head">
                <h2>已发布资料</h2>
              </div>
              <div v-if="!files.length" class="empty">还没有知识文件</div>
              <div v-for="file in files" :key="file.name" class="file-row">
                <div class="file-row__main">
                  <div class="file-row__name">{{ file.name }}</div>
                  <div class="file-row__meta">
                    {{ humanSize(file.size) }} · 版本 {{ file.version }} · {{ file.versions }} 个历史版本
                  </div>
                </div>
                <button class="icon-btn" @click="toggleVersions(file.name)">
                  {{ openVersions === file.name ? '收起版本' : '历史版本' }}
                </button>
                <button class="icon-btn" @click="pendingDelete = file.name">删除</button>
              </div>

              <div v-if="openVersions" class="versions">
                <div v-if="!versionList.length" class="empty" style="padding: 12px 0">该文件暂无历史备份版本</div>
                <a
                  v-for="version in versionList"
                  :key="version"
                  class="version-link"
                  :href="versionHref(openVersions, version)"
                >
                  ↓ {{ version }}
                </a>
              </div>

              <div v-if="pendingDelete" class="notice notice--danger" style="margin-top: 10px">
                确认删除「{{ pendingDelete }}」并更新索引？该操作会保留一份备份，但当前版本不可恢复。
                <div style="display: flex; gap: 8px; margin-top: 8px">
                  <button class="btn btn--danger" @click="deleteFile(pendingDelete)">确认删除</button>
                  <button class="btn" @click="pendingDelete = null">取消</button>
                </div>
              </div>
            </div>

            <div class="panel">
              <div class="panel__head">
                <h2>发布新资料</h2>
              </div>
              <div class="field">
                <label class="field__label">选择文件（UTF-8 TXT 或文本 PDF，最多 20MB）</label>
                <input class="input" type="file" accept=".txt,.pdf" :disabled="previewing" @change="onPickFile" />
              </div>

              <div v-if="previewing" class="empty" style="padding: 12px 0"><span class="spin" /> 正在解析文件</div>

              <template v-if="preview">
                <div v-if="preview.exists" class="notice notice--warn">
                  已存在同名文件，发布将覆盖它（旧版本会备份到历史版本）。
                </div>
                <div v-for="warning in preview.warnings" :key="warning" class="notice notice--warn">
                  {{ warning }}
                </div>
                <div class="field">
                  <label class="field__label">资料适用品牌 / 型号</label>
                  <input v-model="modelName" class="input" placeholder="例如：X20 Pro / 全系" />
                </div>
                <div class="field">
                  <label class="field__label">适用条件与来源</label>
                  <input v-model="scope" class="input" placeholder="例如：说明书 v3、HEPA 滤网" />
                </div>
                <div class="field">
                  <label class="field__label">文本预览（前 5000 字）</label>
                  <pre class="preview">{{ preview.text }}</pre>
                </div>
                <label v-if="preview.exists" class="check">
                  <input v-model="replace" type="checkbox" /> 确认替换同名文件
                </label>
                <button class="btn btn--primary" :disabled="publishing" @click="publishFile">
                  <span v-if="publishing" class="spin" />
                  {{ publishing ? '正在发布并重建索引' : '发布并更新索引' }}
                </button>
              </template>
            </div>
          </template>

          <!-- 反馈 -->
          <template v-else>
            <h1 class="admin__title">反馈与点赞</h1>
            <p class="admin__sub">用户对回答的反馈，内容已在入库前脱敏</p>

            <div class="metrics">
              <div class="metric">
                <div class="metric__value">{{ rows.filter((r) => r.rating === '有帮助').length }}</div>
                <div class="metric__label">点赞</div>
              </div>
              <div class="metric">
                <div class="metric__value">{{ rows.length }}</div>
                <div class="metric__label">反馈总数</div>
              </div>
            </div>

            <div class="panel">
              <div v-if="!rows.length" class="empty">还没有收到用户反馈</div>
              <table v-else class="table">
                <thead>
                  <tr>
                    <th style="width: 44%">问题</th>
                    <th>反馈</th>
                    <th>时间</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, index) in rows" :key="index">
                    <td>{{ row.question }}</td>
                    <td>{{ row.rating }}</td>
                    <td class="muted">{{ String(row.updated ?? '').slice(0, 19).replace('T', ' ') }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </template>
        </template>
      </div>
    </div>
  </div>
</template>
