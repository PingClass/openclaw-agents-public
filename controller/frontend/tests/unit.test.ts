import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, buildQuery, decideApproval, fetchFlows, fetchInbox, login, markAllRead } from '../src/api'
import { entries, formatDuration, formatTime, label, localApprovalPath, sanitize, FLOW_STATUS_LABELS } from '../src/format'
import { loadMe, session, signIn, signOut, resetSession, isAdmin } from '../src/session'
import { jsonError, mockFetch, ADMIN } from './helpers'

afterEach(() => vi.restoreAllMocks())

describe('API client', () => {
  it('組 query 時略過空值與 false', () => {
    expect(buildQuery({ a: 'x', b: '', c: undefined, d: null, e: false, f: true, g: 0 })).toBe('?a=x&f=true&g=0')
    expect(buildQuery()).toBe('')
  })

  it('登入送出帳密並帶同源 cookie', async () => {
    const { calls, spy } = mockFetch({ 'POST /api/auth/login': ADMIN })
    await expect(login('admin', 'pw')).resolves.toEqual(ADMIN)
    expect(calls[0].body).toEqual({ username: 'admin', password: 'pw' })
    expect(spy.mock.calls[0][1]).toMatchObject({ credentials: 'same-origin', method: 'POST' })
  })

  it('把後端的 {detail:{code,message}} 轉成 ApiError', async () => {
    mockFetch({ 'GET /api/x': jsonError(410, 'token_used', '確認連結已經使用過') })
    const err = await api('/x').catch((e) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err).toMatchObject({ status: 410, code: 'token_used', message: '確認連結已經使用過' })
  })

  it('非 JSON 錯誤與字串 detail 也能處理', async () => {
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response('oops', { status: 502 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Not Found' }), { status: 404 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    await expect(api('/a')).rejects.toMatchObject({ status: 502, code: 'http_error', message: 'API 502' })
    await expect(api('/b')).rejects.toMatchObject({ status: 404, message: 'Not Found' })
    await expect(api('/c')).resolves.toBeUndefined()
  })

  it('列表、收件匣、確認帶正確參數', async () => {
    const { calls } = mockFetch({
      'GET /api/flows': { total: 0, items: [] },
      'GET /api/inbox': {},
      'POST /api/inbox/read-all': { marked: 1 },
      'POST /api/approvals/ap_1/decision': { id: 'ap_1', status: 'decided', decision: 'approve' },
    })
    await fetchFlows({ type: 'demo', stuck: true, limit: 50, offset: 0 })
    await fetchFlows({ stuck: false })
    await fetchInbox('h1', true)
    await markAllRead('h1')
    await decideApproval('ap_1', 'tok', 'approve')
    expect(calls[0].url.search).toBe('?type=demo&stuck=true&limit=50&offset=0')
    expect(calls[1].url.search).toBe('')
    expect(calls[2].url.search).toBe('?recipient=h1&unread_only=true')
    expect(calls[3].url.search).toBe('?recipient=h1')
    expect(calls[4].body).toEqual({ token: 'tok', decision: 'approve', comment: null })
  })
})

describe('格式化', () => {
  it('耗時', () => {
    expect(formatDuration(null)).toBe('—')
    expect(formatDuration(42)).toBe('42 秒')
    expect(formatDuration(420)).toBe('7 分鐘')
    expect(formatDuration(7200)).toBe('2 小時')
    expect(formatDuration(7260)).toBe('2 小時 1 分鐘')
    expect(formatDuration(3 * 86400)).toBe('3 天')
  })

  it('時間以台北時區顯示', () => {
    expect(formatTime('2026-10-12T02:05:00Z')).toContain('10:05')
    expect(formatTime(null)).toBe('—')
    expect(formatTime('bad')).toBe('—')
  })

  it('標籤找不到就顯示代碼', () => {
    expect(label(FLOW_STATUS_LABELS, 'running')).toBe('進行中')
    expect(label(FLOW_STATUS_LABELS, 'weird')).toBe('weird')
    expect(label(FLOW_STATUS_LABELS, null)).toBe('—')
  })

  it('移除電話、地址欄位（巢狀與陣列也一樣）', () => {
    const clean = sanitize({
      name: '客戶 A', phone: '0912', customer_address: '台北', 電話: '1', nested: [{ mobile: 'x', note: 'ok' }],
    })
    expect(clean).toEqual({ name: '客戶 A', nested: [{ note: 'ok' }] })
    expect(entries({ a: 1, b: null, c: { d: 1 }, tel: '1' })).toEqual([
      { key: 'a', value: '1' },
      { key: 'b', value: '—' },
      { key: 'c', value: JSON.stringify({ d: 1 }, null, 1) },
    ])
    expect(entries(null)).toEqual([])
    expect(entries([1])).toEqual([])
  })

  it('確認連結改成站內相對路徑', () => {
    expect(localApprovalPath('http://other.host/approvals/ap_1?token=abc')).toBe('/approvals/ap_1?token=abc')
    expect(localApprovalPath('/approvals/ap_2?token=z')).toBe('/approvals/ap_2?token=z')
    expect(localApprovalPath('https://evil.example/steal')).toBeNull()
    expect(localApprovalPath(null)).toBeNull()
  })
})

describe('登入狀態', () => {
  it('讀取、快取、登出', async () => {
    resetSession()
    const { calls } = mockFetch({ 'GET /api/auth/me': ADMIN, 'POST /api/auth/logout': { ok: true } })
    expect(await loadMe()).toEqual(ADMIN)
    expect(isAdmin.value).toBe(true)
    await loadMe()
    expect(calls.filter((c) => c.path === '/api/auth/me')).toHaveLength(1)
    await signOut()
    expect(session.me).toBeNull()
  })

  it('未登入時回 null；登入後寫入狀態', async () => {
    resetSession()
    mockFetch({ 'GET /api/auth/me': jsonError(401, 'unauthorized'), 'POST /api/auth/login': ADMIN })
    expect(await loadMe()).toBeNull()
    await signIn('admin', 'pw')
    expect(session.me?.username).toBe('admin')
  })
})
