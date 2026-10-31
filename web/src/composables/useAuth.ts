import { ref } from 'vue'

import { api } from '../api/client'
import type { AuthMe } from '../types'

/** 模块级单例：整个应用只拉一次身份，视图之间共享。 */
const me = ref<AuthMe | null>(null)
let inflight: Promise<AuthMe> | null = null

export function useAuth() {
  async function load(force = false): Promise<AuthMe> {
    if (me.value && !force) return me.value
    if (inflight) return inflight
    inflight = api
      .get<AuthMe>('/api/auth/me')
      .then((data) => {
        me.value = data
        return data
      })
      .finally(() => {
        inflight = null
      })
    return inflight
  }

  async function login(password: string): Promise<void> {
    await api.post('/api/auth/login', { password })
    await load(true)
  }

  async function logout(): Promise<void> {
    await api.post('/api/auth/logout')
    me.value = null
    await load(true)
  }

  async function switchUser(userId: string): Promise<void> {
    await api.post('/api/auth/switch', { user_id: userId })
    await load(true)
  }

  return { me, load, login, logout, switchUser }
}
