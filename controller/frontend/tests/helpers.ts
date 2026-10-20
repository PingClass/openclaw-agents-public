import { flushPromises, mount } from '@vue/test-utils'
import { vi } from 'vitest'
import { createMemoryHistory } from 'vue-router'
import App from '../src/App.vue'
import { createAppRouter } from '../src/router'
import { DEMO_STORAGE_KEY, demoMode } from '../src/demo'
import { resetSession } from '../src/session'
import type { Me } from '../src/types'

export type Handler = (url: URL, init: RequestInit) => unknown | Response
export type Routes = Record<string, Handler | unknown>

export interface FetchCall {
  method: string
  path: string
  url: URL
  body: unknown
}

/**
 * 依「METHOD /path」（不含 query）回應假資料。值可以是物件、Response 或函式。
 * 沒對應到的請求回 404。
 */
export function mockFetch(routes: Routes) {
  const calls: FetchCall[] = []
  const spy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init = {}) => {
    const url = new URL(String(input), 'http://localhost')
    const method = (init.method ?? 'GET').toUpperCase()
    const key = `${method} ${url.pathname}`
    calls.push({ method, path: url.pathname, url, body: init.body ? JSON.parse(String(init.body)) : undefined })
    if (!(key in routes)) {
      return new Response(JSON.stringify({ detail: { code: 'not_found', message: `沒有 ${key}` } }), { status: 404 })
    }
    const value = routes[key]
    const result = typeof value === 'function' ? (value as Handler)(url, init) : value
    if (result instanceof Response) return result
    return new Response(JSON.stringify(result), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
  return { spy, calls }
}

export function jsonError(status: number, code: string, message = code): Response {
  return new Response(JSON.stringify({ detail: { code, message } }), { status })
}

export const ADMIN: Me = { username: 'admin', display_name: '講師', role: 'admin', identity_id: null }
export const VIEWER: Me = { username: 'guest', display_name: null, role: 'viewer', identity_id: null }

/** 以完整 App＋router 掛載指定路徑。 */
export async function mountAt(path: string, options: { demo?: boolean } = {}) {
  resetSession()
  // 示範模式是全站狀態：每個測試從關閉開始（options.demo 模擬 localStorage 已記住開啟）
  localStorage.removeItem(DEMO_STORAGE_KEY)
  demoMode.value = Boolean(options.demo)
  const router = createAppRouter(createMemoryHistory())
  await router.push(path)
  await router.isReady()
  const wrapper = mount(App, { global: { plugins: [router] }, attachTo: document.body })
  await flushPromises()
  await flushPromises()
  return { wrapper, router }
}
