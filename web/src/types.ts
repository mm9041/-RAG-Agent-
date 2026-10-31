export interface Source {
  chunk_id?: string
  file: string
  snippet: string
  distance?: number
  page?: number | null
  ref?: number | string
  kb_version?: string
}

export interface Message {
  role: 'user' | 'assistant'
  content: string
  sources?: Source[]
  incomplete?: boolean
  liked?: boolean
  /** 仍在流式输出中：此时按纯文本渲染，结束后再转 markdown */
  pending?: boolean
  /** 工具调用前的模型自述，旧版直接丢弃，这里保留可展开查看 */
  reasoning?: string
  failed?: boolean
}

export interface SessionRow {
  id: string
  title: string
  rounds: number
  updated: string
}

export interface AuthMe {
  authenticated: boolean
  user_id: string | null
  demo_mode: boolean
  password_required: boolean
  users: string[]
  admin_enabled: boolean
}

export interface SystemStatus {
  ready: boolean
  booting: boolean
  stage: string
  error: string | null
  boot_seconds: number | null
  kb: string | null
}

export interface PhaseMark {
  label: string
  at: number
}

export interface HistoryPage {
  thread_id: string
  messages: Message[]
  /** 本页在完整历史中的起始下标，点赞 key 依赖它，不能用页内下标 */
  start: number
  total: number
  has_more: boolean
}
