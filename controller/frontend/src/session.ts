// 登入狀態。角色 admin 可以做寫入操作（P2.5 開放）；viewer 只能看。
import { computed, reactive } from 'vue'
import { fetchMe, login as apiLogin, logout as apiLogout } from './api'
import type { Me } from './types'

export const session = reactive<{ me: Me | null; loaded: boolean }>({ me: null, loaded: false })

export const isAdmin = computed(() => session.me?.role === 'admin')

/** 讀取目前登入者；未登入回傳 null（不丟錯）。 */
export async function loadMe(force = false): Promise<Me | null> {
  if (session.loaded && !force) return session.me
  try {
    session.me = await fetchMe()
  } catch {
    // 401 或後端連不上都當作未登入，登入頁會再顯示錯誤
    session.me = null
  }
  session.loaded = true
  return session.me
}

export async function signIn(username: string, password: string): Promise<Me> {
  const me = await apiLogin(username, password)
  session.me = me
  session.loaded = true
  return me
}

export async function signOut(): Promise<void> {
  try {
    await apiLogout()
  } finally {
    session.me = null
    session.loaded = true
  }
}

export function resetSession(): void {
  session.me = null
  session.loaded = false
}
