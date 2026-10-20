// P2.5 前端補完：Agent 頁、Agent 詳情、重試／重送／取消、示範模式、收件匣狀態、總覽數字、gateway 提示。
import { enableAutoUnmount, flushPromises } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DEMO_STORAGE_KEY, applyDemoQuery, demoMode, initDemoMode, setDemoMode } from '../src/demo'
import { maskText, sanitize } from '../src/format'
import { ADMIN, VIEWER, jsonError, mockFetch, mountAt } from './helpers'
import { STUCK, agentCard, flowDetail, inbox, summary } from './fixtures'
import type { FlowDetail } from '../src/types'

enableAutoUnmount(afterEach)
afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  demoMode.value = false
})

const OFFLINE = { status: 'offline', message: 'gateway 離線', failing: [], checked_at: '2026-10-12T02:09:00Z' }

/** draft 步驟派出後 agent 沒回報（卡住），admin 可以重試 */
function stuckFlow(overrides: Partial<FlowDetail> = {}): FlowDetail {
  const base = flowDetail()
  return {
    ...base,
    status: 'running',
    revision: 7,
    current_step: 'draft',
    current_step_title: '產生草稿',
    waiting_for: { kind: 'agent', id: 'admin-agent', name: '行政助理' },
    stuck: STUCK,
    steps: [
      { ...base.steps[0], status: 'dispatched', finished_at: null, summary: null, error: '上次：ADM 0912-345-678 連線失敗', stuck: STUCK },
      { ...base.steps[1], status: 'pending', stuck: null },
      base.steps[2],
    ],
    approvals: [],
    ...overrides,
  }
}

describe('Agent 頁 /agents', () => {
  it('五隻助理 agent 卡片：狀態、正在執行、今日數字、平均耗時、最後回報、最近錯誤；gateway 離線提示', async () => {
    const { calls } = mockFetch({
      'GET /api/auth/me': VIEWER,
      'GET /api/agents': {
        gateway: OFFLINE,
        items: [
          agentCard(),
          agentCard({ id: 'cs-agent', display_name: '客服', status: 'idle', current_steps: [], last_error: null }),
          agentCard({ id: 'finance-agent', display_name: '財會', status: 'error', current_steps: [], last_error: '回報失敗：電話 0912-345-678' }),
        ],
      },
    })
    const { wrapper } = await mountAt('/agents')
    expect(calls.find((c) => c.path === '/api/agents')!.url.searchParams.get('role')).toBe('assistant')
    expect(wrapper.get('[data-testid="gateway-banner"]').text()).toContain('gateway 離線')
    expect(wrapper.get('[data-testid="agent-counts"]').text()).toContain('執行中 1・異常 1・閒置 1')
    const cards = wrapper.findAll('.agent-card')
    expect(cards).toHaveLength(3)
    expect(cards[0].text()).toContain('產生草稿')
    expect(cards[0].text()).toContain('完成 3・失敗 1')
    expect(cards[0].text()).toContain('1 分鐘')
    expect(cards[0].get('a.agent-name').attributes('href')).toBe('/agents/admin-agent')
    expect(cards[2].text()).toContain('異常')
    expect(cards[2].text()).not.toContain('0912')
    expect(wrapper.find('nav a[href="/agents"]').classes()).toContain('active')
  })

  it('頻道異常與空清單、錯誤', async () => {
    mockFetch({
      'GET /api/auth/me': VIEWER,
      'GET /api/agents': { gateway: { status: 'degraded', failing: ['line'] }, items: [] },
    })
    const { wrapper } = await mountAt('/agents')
    const banner = wrapper.get('[data-testid="gateway-banner"]')
    expect(banner.text()).toContain('頻道異常（line）')
    expect(banner.classes()).toContain('gw-degraded')
    expect(wrapper.text()).toContain('還沒有助理 agent')
    wrapper.unmount()
    vi.restoreAllMocks()

    mockFetch({ 'GET /api/auth/me': VIEWER, 'GET /api/agents': { gateway: { status: 'ok', failing: [] }, items: [] } })
    const ok = await mountAt('/agents')
    expect(ok.wrapper.find('[data-testid="gateway-banner"]').exists()).toBe(false)
    ok.wrapper.unmount()
    vi.restoreAllMocks()

    mockFetch({ 'GET /api/auth/me': VIEWER, 'GET /api/agents': jsonError(500, 'boom', '伺服器錯誤') })
    const failed = await mountAt('/agents')
    expect(failed.wrapper.get('[role="alert"]').text()).toContain('伺服器錯誤')
  })
})

describe('Agent 詳情 /agents/:id', () => {
  const detail = {
    ...agentCard(),
    gateway: { status: 'ok', failing: [] },
    steps: [
      {
        flow_id: 'fl_1', flow_title: '示範：派車', flow_type: 'visit', step: 'draft', step_title: '產生草稿',
        status: 'failed', attempts: 2, dispatched_at: '2026-10-12T02:00:00Z', finished_at: '2026-10-12T02:01:00Z',
        duration_seconds: 60, summary: '查空車', error: 'ADM 無法連線', error_kind: 'system',
      },
      {
        flow_id: 'fl_2', flow_title: null, flow_type: 'demo', step: 'draft', step_title: null,
        status: 'succeeded', attempts: 1, dispatched_at: '2026-10-12T01:00:00Z', finished_at: '2026-10-12T01:02:00Z',
        duration_seconds: 120, summary: '客戶電話 02-2345-6789 已確認', error: null,
      },
    ],
  }

  it('顯示步驟紀錄，可依日期與結果篩選（條件放在網址上）', async () => {
    const { calls } = mockFetch({
      'GET /api/auth/me': VIEWER,
      'GET /api/agents/admin-agent': (url: URL) =>
        url.searchParams.get('result') === 'succeeded' ? { ...detail, steps: [] } : detail,
    })
    const { wrapper, router } = await mountAt('/agents/admin-agent?from=2026-10-01')
    const first = calls.find((c) => c.path === '/api/agents/admin-agent')!
    expect(first.url.searchParams.get('from')).toBe('2026-10-01')
    expect(wrapper.get('h1').text()).toBe('行政助理')
    const rows = wrapper.findAll('[data-testid="agent-steps"] tbody tr')
    expect(rows).toHaveLength(2)
    expect(rows[0].classes()).toContain('failed-row')
    expect(rows[0].text()).toContain('① 客戶拜訪')
    expect(rows[0].text()).toContain('（第 2 次）')
    expect(rows[0].text()).toContain('錯誤：ADM 無法連線')
    expect(rows[0].text()).toContain('1 分鐘')
    expect(rows[1].text()).toContain('fl_2')
    expect(rows[1].text()).not.toContain('2345-6789')
    expect(rows[1].get('a').attributes('href')).toBe('/flows/fl_2')

    await wrapper.get('select[name="result"]').setValue('succeeded')
    await wrapper.get('input[name="to"]').setValue('2026-10-12')
    await wrapper.get('[data-testid="agent-filters"]').trigger('submit')
    await flushPromises()
    expect(router.currentRoute.value.query).toEqual({ result: 'succeeded', from: '2026-10-01', to: '2026-10-12' })
    const last = calls.filter((c) => c.path === '/api/agents/admin-agent').at(-1)!
    expect(last.url.searchParams.get('result')).toBe('succeeded')
    expect(wrapper.text()).toContain('沒有符合條件的步驟紀錄')

    await wrapper.findAll('.filter-buttons button')[1].trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query).toEqual({})
    expect(wrapper.findAll('[data-testid="agent-steps"] tbody tr')).toHaveLength(2)
  })

  it('沒有紀錄與找不到 agent', async () => {
    mockFetch({
      'GET /api/auth/me': VIEWER,
      'GET /api/agents/cs-agent': { ...detail, id: 'cs-agent', display_name: null, steps: [] },
      'GET /api/agents/nobody': jsonError(404, 'agent_not_found', '找不到 agent'),
    })
    const { wrapper, router } = await mountAt('/agents/cs-agent')
    expect(wrapper.get('h1').text()).toBe('cs-agent')
    expect(wrapper.text()).toContain('還沒有派給這隻 agent 的步驟')
    await router.push('/agents/nobody')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('找不到 agent')
  })
})

describe('工作流詳情的操作（admin）', () => {
  it('看到卡住 → 找到原因 → 重試：二次確認後帶 revision 呼叫 API，並重新載入', async () => {
    let retried = false
    const { calls } = mockFetch({
      'GET /api/auth/me': ADMIN,
      'GET /api/flows/fl_1': () =>
        retried
          ? stuckFlow({ stuck: null, revision: 8, steps: stuckFlow().steps.map((s) => ({ ...s, stuck: null, attempts: s.name === 'draft' ? 3 : s.attempts })) })
          : stuckFlow(),
      'POST /api/flows/fl_1/steps/draft/retry': () => {
        retried = true
        return { id: 'fl_1' }
      },
    })
    const { wrapper } = await mountAt('/flows/fl_1')
    const stuck = wrapper.get('[data-testid="flow-stuck"]')
    expect(stuck.text()).toContain('admin-agent 已 7 分鐘沒有回報')
    expect(stuck.text()).toContain('卡在「產生草稿」（admin-agent）')
    expect(stuck.text()).toContain('最後的錯誤')
    expect(stuck.text()).not.toContain('0912')
    expect(stuck.get('a').attributes('href')).toBe('/agents/admin-agent')
    expect(wrapper.get('[data-testid="stuck-hint"]').text()).toContain('重試這一步')

    const actions = wrapper.get('[data-testid="flow-actions"]')
    expect(actions.get('[data-action="resend"]').attributes('disabled')).toBeDefined()
    expect(actions.get('[data-action="cancel"]').attributes('disabled')).toBeUndefined()
    const retry = actions.get('[data-action="retry"]')
    expect(retry.attributes('disabled')).toBeUndefined()

    // 先按「先不要」：不送出
    await retry.trigger('click')
    expect(wrapper.get('[data-testid="confirm-box"]').text()).toContain('重試「產生草稿」？')
    await wrapper.get('[data-testid="confirm-no"]').trigger('click')
    expect(wrapper.find('[data-testid="confirm-box"]').exists()).toBe(false)
    expect(calls.some((c) => c.method === 'POST')).toBe(false)

    await retry.trigger('click')
    await wrapper.get('[data-testid="confirm-yes"]').trigger('click')
    await flushPromises()
    const post = calls.find((c) => c.method === 'POST')!
    expect(post.path).toBe('/api/flows/fl_1/steps/draft/retry')
    expect(post.body).toEqual({ revision: 7 })
    expect(wrapper.get('[data-testid="action-message"]').text()).toContain('已重試這一步')
    expect(wrapper.find('[data-testid="flow-stuck"]').exists()).toBe(false)
    expect(wrapper.get('[data-step="draft"]').text()).toContain('第 3 次')
    expect(calls.filter((c) => c.path === '/api/flows/fl_1').length).toBeGreaterThanOrEqual(2)
  })

  it('重送確認通知：等人確認時才能按；人工確認步驟不能重試', async () => {
    const { calls } = mockFetch({
      'GET /api/auth/me': ADMIN,
      'GET /api/flows/fl_1': flowDetail(),
      'POST /api/approvals/ap_new/resend': { id: 'ap_new' },
    })
    const { wrapper } = await mountAt('/flows/fl_1')
    expect(wrapper.get('[data-action="retry"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-action="resend"]').trigger('click')
    expect(wrapper.get('[data-testid="confirm-box"]').text()).toContain('重送確認通知給王經理？')
    await wrapper.get('[data-testid="confirm-yes"]').trigger('click')
    await flushPromises()
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ revision: 3 })
    expect(wrapper.get('[data-testid="action-message"]').text()).toContain('舊連結已失效')
  })

  it('取消流程帶原因；revision 衝突時重新載入並提示；其他錯誤顯示原因', async () => {
    let attempt = 0
    const { calls } = mockFetch({
      'GET /api/auth/me': ADMIN,
      'GET /api/flows/fl_1': flowDetail(),
      'POST /api/flows/fl_1/cancel': () => {
        attempt += 1
        if (attempt === 1) return jsonError(409, 'revision_conflict', 'revision 不符')
        if (attempt === 2) return jsonError(409, 'flow_finished', '流程已結束')
        return { id: 'fl_1' }
      },
    })
    const { wrapper } = await mountAt('/flows/fl_1')
    await wrapper.get('[data-action="cancel"]').trigger('click')
    await wrapper.get('textarea[name="reason"]').setValue('  客戶改期  ')
    await wrapper.get('[data-testid="confirm-yes"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="action-error"]').text()).toContain('流程剛剛有變化')
    expect(wrapper.find('[data-testid="confirm-box"]').exists()).toBe(false)

    await wrapper.get('[data-action="cancel"]').trigger('click')
    await wrapper.get('[data-testid="confirm-yes"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="action-error"]').text()).toContain('流程已結束')
    expect(wrapper.find('[data-testid="confirm-box"]').exists()).toBe(true)

    await wrapper.get('[data-testid="confirm-yes"]').trigger('click')
    await flushPromises()
    const posts = calls.filter((c) => c.method === 'POST')
    expect(posts[0].body).toEqual({ revision: 3, reason: '客戶改期' })
    expect(posts[1].body).toEqual({ revision: 3, reason: null })
    expect(wrapper.get('[data-testid="action-message"]').text()).toContain('流程已取消')
  })

  it('流程結束後全部停用', async () => {
    mockFetch({
      'GET /api/auth/me': ADMIN,
      'GET /api/flows/fl_1': flowDetail({ status: 'succeeded', stuck: null, approvals: [] }),
    })
    const { wrapper } = await mountAt('/flows/fl_1')
    for (const action of ['retry', 'resend', 'cancel']) {
      expect(wrapper.get(`[data-action="${action}"]`).attributes('disabled')).toBeDefined()
    }
  })

  it('viewer 看到卡住原因，但沒有操作按鈕與建議', async () => {
    mockFetch({ 'GET /api/auth/me': VIEWER, 'GET /api/flows/fl_1': stuckFlow() })
    const { wrapper } = await mountAt('/flows/fl_1')
    expect(wrapper.get('[data-testid="flow-stuck"]').text()).toContain('沒有回報')
    expect(wrapper.find('[data-testid="flow-actions"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="stuck-hint"]').exists()).toBe(false)
    expect(wrapper.find('button[data-action]').exists()).toBe(false)
  })
})

describe('示範模式', () => {
  it('網址 ?demo=1 開啟並記住；只顯示總覽與時間軸、字放大；頁首可以關閉', async () => {
    mockFetch({
      'GET /api/auth/me': ADMIN,
      'GET /api/stats/summary': summary(),
      'GET /api/agents': { items: [agentCard()] },
      'GET /api/flows/fl_1': stuckFlow(),
    })
    const { wrapper, router } = await mountAt('/?demo=1')
    expect(demoMode.value).toBe(true)
    expect(localStorage.getItem(DEMO_STORAGE_KEY)).toBe('1')
    expect(document.documentElement.classList.contains('demo-mode')).toBe(true)
    expect(wrapper.get('.shell').classes()).toContain('demo')
    expect(wrapper.findAll('nav a').map((a) => a.text())).toEqual(['總覽'])
    expect(wrapper.find('[data-testid="logout"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="stats"]').exists()).toBe(true)

    await router.push('/flows/fl_1')
    await flushPromises()
    expect(wrapper.find('[data-testid="timeline"]').exists()).toBe(true)
    for (const id of ['approvals', 'audit', 'flow-actions']) expect(wrapper.find(`[data-testid="${id}"]`).exists()).toBe(false)
    expect(wrapper.get('.back').text()).toContain('總覽')

    await router.push('/flows/fl_1?demo=1')
    await wrapper.get('[data-testid="demo-toggle"]').trigger('click')
    await flushPromises()
    expect(demoMode.value).toBe(false)
    expect(router.currentRoute.value.query.demo).toBeUndefined()
    expect(localStorage.getItem(DEMO_STORAGE_KEY)).toBeNull()
    expect(document.documentElement.classList.contains('demo-mode')).toBe(false)
    expect(wrapper.find('[data-testid="audit"]').exists()).toBe(true)
  })

  it('從 localStorage 還原；?demo=0 關閉；無效值不理會；儲存失敗也不出錯', () => {
    localStorage.setItem(DEMO_STORAGE_KEY, '1')
    initDemoMode()
    expect(demoMode.value).toBe(true)
    applyDemoQuery('weird')
    expect(demoMode.value).toBe(true)
    applyDemoQuery(['1'])
    expect(demoMode.value).toBe(true)
    applyDemoQuery('0')
    expect(demoMode.value).toBe(false)
    expect(localStorage.getItem(DEMO_STORAGE_KEY)).toBeNull()

    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    setDemoMode(true)
    expect(demoMode.value).toBe(true)
    initDemoMode()
    expect(demoMode.value).toBe(false)
  })

  it('頁首按鈕開啟示範模式', async () => {
    mockFetch({ 'GET /api/auth/me': VIEWER, 'GET /api/stats/summary': summary(), 'GET /api/agents': { items: [] } })
    const { wrapper } = await mountAt('/')
    const toggle = wrapper.get('[data-testid="demo-toggle"]')
    expect(toggle.text()).toBe('示範模式')
    await toggle.trigger('click')
    await flushPromises()
    expect(demoMode.value).toBe(true)
    expect(wrapper.get('[data-testid="demo-toggle"]').text()).toBe('結束示範')
  })
})

describe('總覽數字與 gateway', () => {
  it('「進行中」不含等待確認，並標示清楚；gateway 離線時提示', async () => {
    mockFetch({
      'GET /api/auth/me': VIEWER,
      'GET /api/stats/summary': summary({
        counts: { running: 1, waiting_approval: 2, active: 3, stuck: 1, succeeded_today: 0, failed_today: 0 },
        gateway: OFFLINE,
      }),
      'GET /api/agents': { items: [] },
    })
    const { wrapper } = await mountAt('/')
    const tiles = wrapper.findAll('[data-testid="stats"] .tile')
    expect(tiles[0].text()).toContain('進行中（agent 處理中）1')
    expect(tiles[1].text()).toContain('等待確認（等人決定）2')
    expect(tiles[2].text()).toContain('已含在前兩項')
    expect(wrapper.get('[data-testid="gateway-banner"]').text()).toContain('gateway 離線')
    expect(wrapper.get('[data-testid="agents"] h2 a').attributes('href')).toBe('/agents')
  })

  it('舊版後端（running 含等待確認、沒有 active）也不重疊', async () => {
    mockFetch({
      'GET /api/auth/me': VIEWER,
      'GET /api/stats/summary': summary({ counts: { running: 3, waiting_approval: 2, stuck: 0, succeeded_today: 0, failed_today: 0 } }),
      'GET /api/agents': { items: [] },
    })
    const { wrapper } = await mountAt('/')
    expect(wrapper.findAll('[data-testid="stats"] .tile')[0].text()).toContain('1')
    expect(wrapper.find('[data-testid="gateway-banner"]').exists()).toBe(false)
  })
})

describe('收件匣顯示確認的狀態', () => {
  it('已決定、已重送、已過期、已取消的確認不再給確認連結', async () => {
    const base = inbox().items[0]
    const items = [
      { ...base, id: 21, approval_status: 'pending' },
      { ...base, id: 22, approval_status: 'decided', approval_decision: '核准' },
      { ...base, id: 23, approval_status: 'superseded' },
      { ...base, id: 24, approval_status: 'link_expired' },
      { ...base, id: 25, approval_status: 'cancelled' },
      { ...base, id: 26, approval_status: 'expired', body: '請回電 0912-345-678' },
      { ...inbox().items[1], id: 27, approval_status: null },
    ]
    mockFetch({ 'GET /api/auth/me': { ...ADMIN, identity_id: 1 }, 'GET /api/inbox/recipients': { items: [] }, 'GET /api/inbox': inbox({ items }) })
    const { wrapper } = await mountAt('/inbox')
    const item = (id: number) => wrapper.get(`[data-id="${id}"]`)
    expect(item(21).find('[data-testid="approval-link"]').exists()).toBe(true)
    expect(item(21).get('[data-testid="approval-status"]').text()).toBe('等待確認')
    expect(item(22).get('[data-testid="approval-status"]').text()).toBe('已決定：核准')
    expect(item(23).get('[data-testid="approval-status"]').text()).toContain('已重送')
    expect(item(24).get('[data-testid="approval-status"]').text()).toContain('連結已過期')
    expect(item(25).get('[data-testid="approval-status"]').text()).toContain('流程已取消')
    expect(item(26).get('[data-testid="approval-status"]').text()).toBe('已逾時')
    expect(item(26).text()).not.toContain('0912')
    for (const id of [22, 23, 24, 25, 26]) {
      expect(item(id).find('[data-testid="approval-link"]').exists()).toBe(false)
      expect(item(id).classes()).toContain('closed')
    }
    expect(item(27).find('[data-testid="approval-status"]').exists()).toBe(false)
  })
})

describe('個資：自由文字裡的電話也遮掉', () => {
  it('maskText 遮手機與市話，不動日期、金額、車牌', () => {
    expect(maskText('請撥 0912-345-678 或 0912345678')).toBe('請撥 （電話已隱藏） 或 （電話已隱藏）')
    expect(maskText('+886 912 345 678')).toBe('（電話已隱藏）')
    expect(maskText('公司 02-2345-6789、(04) 2345 6789')).not.toMatch(/2345/)
    expect(maskText('2026-10-14 10:00 FAKE-002 NT$185 12000 km')).toBe('2026-10-14 10:00 FAKE-002 NT$185 12000 km')
    expect(maskText('市話 0223456789、+886-2-2345-6789、+886 (4) 2345 6789')).not.toMatch(/\d{4}/)
    expect(maskText('里程 12345678 km、單號 20261014001')).toBe('里程 12345678 km、單號 20261014001')
    expect(maskText(null)).toBe('')
    expect(sanitize({ note: '回電 0912-345-678', list: ['0912345678'] })).toEqual({
      note: '回電 （電話已隱藏）',
      list: ['（電話已隱藏）'],
    })
  })

  it('工作流詳情的確認意見與問題裡的電話也遮掉', async () => {
    const base = flowDetail()
    const approvals = base.approvals.map((a, i) =>
      i === 0 ? { ...a, question: '確認 0912-345-678 的拜訪？', comment: '請先打 02-2345-6789' } : a,
    )
    mockFetch({ 'GET /api/auth/me': VIEWER, 'GET /api/flows/fl_1': flowDetail({ approvals }) })
    const { wrapper } = await mountAt('/flows/fl_1')
    const text = wrapper.get('[data-testid="approvals"]').text()
    expect(text).not.toMatch(/0912|2345-6789/)
    expect(text).toContain('（電話已隱藏）')
  })
})
