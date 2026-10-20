import { enableAutoUnmount, flushPromises } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ADMIN, VIEWER, jsonError, mockFetch, mountAt } from './helpers'
import { agentCard, flowDetail, flowSummary, inbox, STUCK, summary } from './fixtures'

enableAutoUnmount(afterEach)
afterEach(() => vi.restoreAllMocks())

describe('登入與路由守衛', () => {
  it('未登入時導到登入頁，登入後回到原本要去的頁面', async () => {
    let loggedIn = false
    mockFetch({
      'GET /api/auth/me': () => (loggedIn ? ADMIN : jsonError(401, 'unauthorized')),
      'POST /api/auth/login': (_url: URL, init: RequestInit) => {
        const body = JSON.parse(String(init.body))
        if (body.password !== 'pw') return jsonError(401, 'bad_credentials', '帳號或密碼錯誤')
        loggedIn = true
        return ADMIN
      },
      'GET /api/flows': { total: 0, items: [] },
    })
    const { wrapper, router } = await mountAt('/flows?stuck=1')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.next).toBe('/flows?stuck=1')

    await wrapper.get('input[autocomplete="username"]').setValue('admin')
    await wrapper.get('input[autocomplete="current-password"]').setValue('wrong')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('帳號或密碼錯誤')

    await wrapper.get('input[autocomplete="current-password"]').setValue('pw')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/flows?stuck=1')
    expect(wrapper.text()).toContain('講師')
  })

  it('登入的 next 只接受站內路徑；後端錯誤顯示通用訊息', async () => {
    let calls = 0
    mockFetch({
      'GET /api/auth/me': jsonError(401, 'unauthorized'),
      'POST /api/auth/login': () => (++calls === 1 ? jsonError(500, 'boom') : ADMIN),
      'GET /api/stats/summary': summary(),
      'GET /api/agents': { items: [] },
    })
    const { wrapper, router } = await mountAt('/login?next=//evil.example')
    await wrapper.get('input[autocomplete="username"]').setValue('admin')
    await wrapper.get('input[autocomplete="current-password"]').setValue('pw')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('登入失敗')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/')
  })

  it('登出回登入頁；未知路徑顯示找不到', async () => {
    mockFetch({ 'GET /api/auth/me': VIEWER, 'POST /api/auth/logout': { ok: true } })
    const { wrapper, router } = await mountAt('/nope')
    expect(wrapper.text()).toContain('找不到頁面')
    expect(wrapper.text()).toContain('唯讀')
    await wrapper.get('[data-testid="logout"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('login')
  })
})

describe('總覽 /', () => {
  it('卡住清單置頂並標紅，顯示統計、等待確認與 agent 卡', async () => {
    mockFetch({
      'GET /api/auth/me': VIEWER,
      'GET /api/stats/summary': summary(),
      'GET /api/agents': { items: [agentCard(), agentCard({ id: 'cs-agent', display_name: '客服', status: 'idle', current_steps: [], last_error: null })] },
    })
    const { wrapper } = await mountAt('/')
    const sections = wrapper.findAll('section')
    expect(sections[0].attributes('data-testid')).toBe('stuck-list')
    expect(sections[0].classes()).toContain('stuck-panel')
    expect(sections[0].text()).toContain('admin-agent 已 7 分鐘沒有回報')
    expect(sections[0].text()).toContain('agent 沒回報')
    expect(sections[0].text()).toContain('等：行政助理')
    expect(sections[0].text()).toContain('已等 7 分鐘')

    const stats = wrapper.get('[data-testid="stats"]').text()
    for (const text of ['進行中', '等待確認', '卡住', '今日完成', '今日失敗']) expect(stats).toContain(text)
    expect(wrapper.get('[data-testid="stats"] .tile.alert').text()).toContain('1')

    const pending = wrapper.get('[data-testid="pending-approvals"]').text()
    expect(pending).toContain('王經理')
    expect(pending).toContain('請確認報帳草稿')
    expect(pending).toContain('剩 2 小時')

    const agents = wrapper.findAll('.agent-card')
    expect(agents).toHaveLength(2)
    expect(agents[0].text()).toContain('執行中')
    expect(agents[0].text()).toContain('（已逾時）')
    expect(agents[0].text()).toContain('ADM 無法連線')
    expect(agents[1].text()).toContain('目前沒有執行中的步驟')
  })

  it('沒有卡住時不顯示卡住清單；API 失敗顯示錯誤', async () => {
    mockFetch({
      'GET /api/auth/me': VIEWER,
      'GET /api/stats/summary': summary({ stuck: [], pending_approvals: [] }),
      'GET /api/agents': jsonError(500, 'boom', '伺服器錯誤'),
    })
    const { wrapper } = await mountAt('/')
    expect(wrapper.find('[data-testid="stuck-list"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('目前沒有等待確認的項目')
    expect(wrapper.get('[data-testid="agents"] [role="alert"]').text()).toContain('伺服器錯誤')
  })

  it('session 過期時回登入頁', async () => {
    let first = true
    mockFetch({
      'GET /api/auth/me': () => (first ? ((first = false), VIEWER) : jsonError(401, 'unauthorized')),
      'GET /api/stats/summary': jsonError(401, 'unauthorized'),
      'GET /api/agents': jsonError(401, 'unauthorized'),
    })
    const { router } = await mountAt('/')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('login')
  })
})

describe('工作流列表 /flows', () => {
  it('依網址的篩選條件查詢，送出篩選會更新網址', async () => {
    const { calls } = mockFetch({
      'GET /api/auth/me': VIEWER,
      'GET /api/flows': {
        total: 2,
        items: [flowSummary({ stuck: STUCK }), flowSummary({ id: 'fl_2', title: null, status: 'succeeded', waiting_for: null, current_step: null, current_step_title: null })],
      },
    })
    const { wrapper, router } = await mountAt('/flows?type=demo&stuck=1')
    const first = calls.find((c) => c.path === '/api/flows')!
    expect(first.url.searchParams.get('type')).toBe('demo')
    expect(first.url.searchParams.get('stuck')).toBe('true')
    expect((wrapper.get('input[name="stuck"]').element as HTMLInputElement).checked).toBe(true)

    const rows = wrapper.findAll('tbody tr')
    expect(rows[0].classes()).toContain('stuck-row')
    expect(rows[0].text()).toContain('admin-agent 已 7 分鐘沒有回報')
    expect(rows[0].text()).toContain('示範流程')
    expect(rows[0].text()).toContain('行政助理')
    expect(rows[1].text()).toContain('fl_2')
    expect(rows[1].text()).toContain('完成')

    await wrapper.get('select[name="status"]').setValue('running,waiting_approval')
    await wrapper.get('input[name="from"]').setValue('2026-10-01')
    await wrapper.get('input[name="to"]').setValue('2026-10-12')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(router.currentRoute.value.query).toEqual({
      type: 'demo', status: 'running,waiting_approval', from: '2026-10-01', to: '2026-10-12', stuck: '1',
    })
    const last = calls.filter((c) => c.path === '/api/flows').at(-1)!
    expect(last.url.searchParams.get('from')).toBe('2026-10-01')

    await wrapper.findAll('.filter-buttons button')[1].trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query).toEqual({})
  })

  it('課堂示範模式的流程在列表與詳情標示「示範」', async () => {
    mockFetch({
      'GET /api/auth/me': VIEWER,
      'GET /api/flows': { total: 2, items: [flowSummary({ demo: true }), flowSummary({ id: 'fl_2' })] },
      'GET /api/flows/fl_1': flowDetail({ demo: true }),
    })
    const { wrapper, router } = await mountAt('/flows')
    const rows = wrapper.findAll('tbody tr')
    expect(rows[0].find('[data-testid="demo-tag"]').text()).toBe('示範')
    expect(rows[1].find('[data-testid="demo-tag"]').exists()).toBe(false)
    await router.push('/flows/fl_1')
    await flushPromises()
    expect(wrapper.get('[data-testid="demo-tag"]').text()).toBe('示範模式')
  })

  it('空結果與分頁', async () => {
    mockFetch({
      'GET /api/auth/me': VIEWER,
      'GET /api/flows': (url: URL) =>
        url.searchParams.get('offset') === '50'
          ? { total: 51, items: [] }
          : { total: 51, items: [flowSummary()] },
    })
    const { wrapper, router } = await mountAt('/flows')
    expect(wrapper.text()).toContain('1 / 2')
    await wrapper.findAll('.pager button')[1].trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query.page).toBe('1')
    expect(wrapper.text()).toContain('沒有符合條件的流程')
    await wrapper.findAll('.pager button')[0].trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query.page).toBeUndefined()
  })
})

describe('工作流詳情 /flows/:id', () => {
  it('時間軸顯示執行者、狀態、耗時、摘要；卡住步驟標紅並顯示原因', async () => {
    mockFetch({ 'GET /api/auth/me': VIEWER, 'GET /api/flows/fl_1': flowDetail() })
    const { wrapper } = await mountAt('/flows/fl_1')
    expect(wrapper.get('[data-testid="flow-stuck"]').text()).toContain('等王經理確認草稿')

    const draft = wrapper.get('[data-step="draft"]')
    expect(draft.text()).toContain('admin-agent')
    expect(draft.text()).toContain('完成')
    expect(draft.text()).toContain('3 分鐘')
    expect(draft.text()).toContain('草稿已產生')
    expect(draft.text()).toContain('第 2 次（上限 2）')

    const confirm = wrapper.get('[data-step="confirm"]')
    expect(confirm.classes()).toContain('tl-stuck')
    expect(confirm.text()).toContain('人工確認：王經理')
    expect(confirm.text()).toContain('等王經理確認草稿，已 2 小時')

    const approvals = wrapper.get('[data-testid="approvals"]').text()
    expect(approvals).toContain('退回重擬')
    expect(approvals).toContain('改時間')
    expect(approvals).toContain('剩 1 小時')

    const audit = wrapper.get('[data-testid="audit"]').text()
    expect(audit).toContain('flow.started')
    expect(audit).not.toContain('0912')
  })

  it('② 送給客戶的回覆顯示在「對外訊息」；沒有訊息時不顯示這一區', async () => {
    const detail = {
      ...flowDetail(), type: 'refund',
      messages: [{
        id: 1, channel: 'sim', direction: 'out', conversation_id: 'sim-cs-agent-C-007', agent_id: 'cs-agent',
        external_customer_id: 'C-007', display_name: null, body: '我們將退款 NT$1,200，聯絡電話 0912-345-678',
        flow_id: 'fl_1', created_at: '2026-10-12T03:00:00Z',
      }],
    }
    mockFetch({ 'GET /api/auth/me': VIEWER, 'GET /api/flows/fl_1': detail })
    let { wrapper } = await mountAt('/flows/fl_1')
    const messages = wrapper.get('[data-testid="messages"]').text()
    expect(messages).toContain('送給客戶')
    expect(messages).toContain('模擬通道')
    expect(messages).toContain('sim-cs-agent-C-007')
    expect(messages).toContain('NT$1,200')
    expect(messages).not.toContain('0912-345-678')
    expect(wrapper.text()).toContain('② 客訴退款')
    wrapper.unmount()
    vi.restoreAllMocks()
    mockFetch({ 'GET /api/auth/me': VIEWER, 'GET /api/flows/fl_1': flowDetail() })
    ;({ wrapper } = await mountAt('/flows/fl_1'))
    expect(wrapper.find('[data-testid="messages"]').exists()).toBe(false)
  })

  it('viewer 看不到任何寫入按鈕', async () => {
    mockFetch({ 'GET /api/auth/me': VIEWER, 'GET /api/flows/fl_1': flowDetail() })
    const { wrapper } = await mountAt('/flows/fl_1')
    expect(wrapper.find('[data-testid="flow-actions"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('重試這一步')
    expect(wrapper.text()).not.toContain('取消流程')
    expect(wrapper.text()).not.toContain('重送確認通知')
  })

  it('找不到流程時顯示錯誤', async () => {
    mockFetch({ 'GET /api/auth/me': VIEWER, 'GET /api/flows/nope': jsonError(404, 'flow_not_found', '找不到流程') })
    const { wrapper } = await mountAt('/flows/nope')
    expect(wrapper.get('[role="alert"]').text()).toContain('找不到流程')
  })
})

describe('確認頁 /approvals/:id', () => {
  const approval = {
    id: 'ap_1', flow_title: '示範：派車', step_title: '確認派車', approver_name: '王經理',
    question: '請確認派車草稿', options: [{ value: 'approve', label: '核准' }, { value: 'reject', label: '不核准' }],
    context: { 車輛: 'ABC-123', 客戶電話: '0912', customer_address: '台北市', 時段: '10/13 09:00-12:00' },
    expires_at: '2026-10-12T10:00:00Z', remaining_seconds: 5400,
  }

  it('免登入開啟、不顯示電話地址、送出決定', async () => {
    const { calls } = mockFetch({
      'GET /api/approvals/ap_1': approval,
      'POST /api/approvals/ap_1/decision': { id: 'ap_1', status: 'decided', decision: 'approve' },
    })
    const { wrapper } = await mountAt('/approvals/ap_1?token=tok123')
    expect(calls.some((c) => c.path === '/api/auth/me')).toBe(false)
    expect(wrapper.find('.topbar').exists()).toBe(false)
    expect(calls[0].url.searchParams.get('token')).toBe('tok123')
    const context = wrapper.get('[data-testid="approval-context"]').text()
    expect(context).toContain('ABC-123')
    expect(context).not.toContain('0912')
    expect(context).not.toContain('台北市')
    expect(wrapper.text()).toContain('剩 1 小時 30 分鐘')

    await wrapper.get('textarea').setValue('  請準時  ')
    await wrapper.get('[data-option="approve"]').trigger('click')
    await flushPromises()
    expect(calls.at(-1)!.body).toEqual({ token: 'tok123', decision: 'approve', comment: '請準時' })
    expect(wrapper.text()).toContain('已送出：核准')
  })

  it('有欄位（② 的退款金額）時顯示輸入框，只在適用的選項帶上 data', async () => {
    const refund = {
      ...approval, step_title: '客服主管決定退款', question: '請決定退款',
      options: [{ value: 'approve', label: '同意退款' }, { value: 'reject', label: '不退款' }],
      fields: [{ name: 'amount_twd', label: '退款金額（NT$）', type: 'int', applies_to: ['approve'], default: 1500, min: 1 }],
    }
    const { calls } = mockFetch({
      'GET /api/approvals/ap_1': refund,
      'POST /api/approvals/ap_1/decision': { id: 'ap_1', status: 'decided', decision: 'approve' },
    })
    const { wrapper } = await mountAt('/approvals/ap_1?token=t')
    const input = wrapper.get('input[name="amount_twd"]')
    expect((input.element as HTMLInputElement).value).toBe('1500')
    expect(wrapper.get('[data-field="amount_twd"]').text()).toContain('同意退款')
    await input.setValue('1200')
    await wrapper.get('[data-option="approve"]').trigger('click')
    await flushPromises()
    expect(calls.at(-1)!.body).toEqual({ token: 't', decision: 'approve', comment: null, data: { amount_twd: 1200 } })
  })

  it('不適用欄位的選項不帶 data', async () => {
    const refund = {
      ...approval,
      options: [{ value: 'approve', label: '同意退款' }, { value: 'reject', label: '不退款' }],
      fields: [{ name: 'amount_twd', label: '退款金額（NT$）', type: 'int', applies_to: ['approve'], default: null }],
    }
    const { calls } = mockFetch({
      'GET /api/approvals/ap_1': refund,
      'POST /api/approvals/ap_1/decision': { id: 'ap_1', status: 'decided', decision: 'reject' },
    })
    const { wrapper } = await mountAt('/approvals/ap_1?token=t')
    await wrapper.get('[data-option="reject"]').trigger('click')
    await flushPromises()
    expect(calls.at(-1)!.body).toEqual({ token: 't', decision: 'reject', comment: null })
  })

  it('連結用過、無效、缺 token 時顯示原因', async () => {
    mockFetch({
      'GET /api/approvals/used': jsonError(410, 'token_used'),
      'GET /api/approvals/other': jsonError(418, 'weird', '奇怪的錯誤'),
    })
    let { wrapper } = await mountAt('/approvals/used?token=x')
    expect(wrapper.text()).toContain('這個確認連結已經用過了')
    wrapper.unmount()
    ;({ wrapper } = await mountAt('/approvals/other?token=x'))
    expect(wrapper.text()).toContain('奇怪的錯誤')
    wrapper.unmount()
    ;({ wrapper } = await mountAt('/approvals/used'))
    expect(wrapper.text()).toContain('連結缺少 token')
  })

  it('送出失敗時留在頁面並顯示原因；連不上也有訊息', async () => {
    let fail = 'gone'
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.startsWith('/api/approvals/ap_1?')) return new Response(JSON.stringify(approval), { status: 200 })
      if (init?.method === 'POST' && fail === 'gone') { fail = 'net'; return jsonError(410, 'token_expired') }
      throw new TypeError('network down')
    })
    const { wrapper } = await mountAt('/approvals/ap_1?token=t')
    await wrapper.get('[data-option="reject"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('已經過期')
    await wrapper.get('[data-option="reject"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('無法連線')
  })
})

describe('通知收件匣 /inbox', () => {
  it('admin 可切換收件人、看到確認連結並開啟確認頁、標已讀', async () => {
    const { calls } = mockFetch({
      'GET /api/auth/me': ADMIN,
      'GET /api/inbox/recipients': {
        items: [
          { id: 1, hrm_employee_id: 'h1', employee_number: 'E001', display_name: '王經理', role: 'sales_manager', unread_count: 1 },
          { id: 2, hrm_employee_id: 'h2', employee_number: 'E002', display_name: '李主任', role: 'cs_manager', unread_count: 0 },
        ],
      },
      'GET /api/inbox': (url: URL) =>
        url.searchParams.get('recipient') === 'h2'
          ? inbox({ recipient: { id: 2, hrm_employee_id: 'h2', employee_number: 'E002', display_name: '李主任', role: null }, unread_count: 0, items: [] })
          : inbox(),
      'POST /api/inbox/11/read': { id: 11, read_at: '2026-10-12T03:00:00Z' },
      'POST /api/inbox/read-all': { marked: 1 },
      'GET /api/approvals/ap_new': { id: 'ap_new', flow_title: 'x', step_title: 'y', approver_name: null, question: 'Q?', options: [], context: {}, expires_at: null, remaining_seconds: 60 },
    })
    const { wrapper, router } = await mountAt('/inbox')
    // admin 沒對應確認者：預設第一位
    expect(router.currentRoute.value.query.recipient).toBe('h1')
    expect(wrapper.text()).toContain('王經理 的收件匣')
    expect(wrapper.get('select[name="recipient"]').text()).toContain('未讀 1')

    const link = wrapper.get('[data-testid="approval-link"]')
    expect(link.attributes('href')).toBe('/approvals/ap_new?token=tok123')

    await wrapper.get('[data-id="11"] button').trigger('click')
    await flushPromises()
    expect(calls.some((c) => c.method === 'POST' && c.path === '/api/inbox/11/read')).toBe(true)
    await wrapper.get('.inbox-bar button').trigger('click')
    await flushPromises()
    const readAll = calls.find((c) => c.path === '/api/inbox/read-all')!
    expect(readAll.url.searchParams.get('recipient')).toBe('h1')

    await wrapper.get('select[name="recipient"]').setValue('h2')
    await flushPromises()
    expect(router.currentRoute.value.query.recipient).toBe('h2')
    expect(wrapper.text()).toContain('收件匣是空的')

    await wrapper.get('input[name="unread"]').setValue(true)
    await flushPromises()
    expect(wrapper.text()).toContain('沒有未讀通知')

    await router.push('/approvals/ap_new?token=tok123')
    await flushPromises()
    expect(wrapper.text()).toContain('Q?')
  })

  it('admin 可以把確認者切成真人確認（二次確認），也能切回 agent 代理', async () => {
    let mode: 'agent' | 'human' = 'agent'
    const person = { id: 3, hrm_employee_id: 'h3', employee_number: 'AGT-GOBTA', display_name: 'Gobta', role: 'sales' }
    const { calls } = mockFetch({
      'GET /api/auth/me': ADMIN,
      'GET /api/inbox/recipients': () => ({ items: [{ ...person, confirm_mode: mode, unread_count: 1 }] }),
      'GET /api/inbox': () => inbox({ recipient: { ...person, confirm_mode: mode } }),
      'PUT /api/inbox/confirm-mode': (_url: URL, init: RequestInit) => {
        mode = JSON.parse(String(init.body)).mode
        return { recipient: { ...person, confirm_mode: mode }, confirm_mode: mode, human: mode === 'human', agent_id: 'gobta', changed: true }
      },
    })
    const { wrapper } = await mountAt('/inbox?recipient=h3')
    const bar = () => wrapper.get('[data-testid="confirm-mode"]')
    expect(bar().text()).toContain('agent 代理')

    // 先不要：不呼叫 API
    await wrapper.get('[data-testid="confirm-mode-human"]').trigger('click')
    expect(wrapper.get('[data-testid="confirm-mode-box"]').text()).toContain('把 Gobta 切成「真人確認」？')
    await wrapper.get('[data-testid="confirm-mode-no"]').trigger('click')
    expect(wrapper.find('[data-testid="confirm-mode-box"]').exists()).toBe(false)
    expect(calls.some((c) => c.method === 'PUT')).toBe(false)

    await wrapper.get('[data-testid="confirm-mode-human"]').trigger('click')
    await wrapper.get('[data-testid="confirm-mode-yes"]').trigger('click')
    await flushPromises()
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.url.searchParams.get('recipient')).toBe('h3')
    expect(put.body).toEqual({ mode: 'human' })
    expect(bar().text()).toContain('真人確認')
    expect(wrapper.get('select[name="recipient"]').text()).toContain('真人確認')

    await wrapper.get('[data-testid="confirm-mode-agent"]').trigger('click')
    expect(wrapper.get('[data-testid="confirm-mode-box"]').text()).toContain('切回「agent 代理」')
    await wrapper.get('[data-testid="confirm-mode-yes"]').trigger('click')
    await flushPromises()
    expect(calls.filter((c) => c.method === 'PUT').map((c) => (c.body as { mode: string }).mode)).toEqual(['human', 'agent'])
    expect(bar().text()).toContain('agent 代理')
  })

  it('切換確認方式失敗時顯示錯誤；viewer 看不到切換', async () => {
    const person = { id: 3, hrm_employee_id: 'h3', employee_number: null, display_name: 'Gobta', role: 'sales', confirm_mode: 'agent' as const }
    mockFetch({
      'GET /api/auth/me': ADMIN,
      'GET /api/inbox/recipients': { items: [] },
      'GET /api/inbox': inbox({ recipient: person }),
      'PUT /api/inbox/confirm-mode': jsonError(403, 'forbidden', '只有 admin 可以執行這個操作'),
    })
    const { wrapper } = await mountAt('/inbox?recipient=h3')
    await wrapper.get('[data-testid="confirm-mode-human"]').trigger('click')
    await wrapper.get('[data-testid="confirm-mode-yes"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('只有 admin')
    wrapper.unmount()
    vi.restoreAllMocks()

    mockFetch({ 'GET /api/auth/me': { ...VIEWER, identity_id: 3 }, 'GET /api/inbox': inbox({ recipient: person }) })
    const viewer = await mountAt('/inbox')
    expect(viewer.wrapper.find('[data-testid="confirm-mode"]').exists()).toBe(false)
  })

  it('viewer 只看自己的收件匣、看不到別人的確認連結、不能標已讀', async () => {
    const { calls } = mockFetch({
      'GET /api/auth/me': { ...VIEWER, identity_id: 5 },
      'GET /api/inbox': inbox({ items: [{ ...inbox().items[0], link: null, link_hidden: true }] }),
    })
    const { wrapper } = await mountAt('/inbox?recipient=h9')
    expect(calls.find((c) => c.path === '/api/inbox')!.url.searchParams.get('recipient')).toBeNull()
    expect(calls.some((c) => c.path === '/api/inbox/recipients')).toBe(false)
    expect(wrapper.find('select[name="recipient"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="approval-link"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('確認連結只給本人與 admin')
    expect(wrapper.find('[data-id="11"] button').exists()).toBe(false)
  })

  it('帳號沒有對應確認者時說明原因；標已讀失敗顯示錯誤', async () => {
    mockFetch({ 'GET /api/auth/me': VIEWER, 'GET /api/inbox': jsonError(404, 'recipient_not_found', '找不到收件人') })
    const { wrapper } = await mountAt('/inbox')
    expect(wrapper.get('[data-testid="no-inbox"]').text()).toContain('沒有對應的確認者')
    wrapper.unmount()
    vi.restoreAllMocks()

    mockFetch({
      'GET /api/auth/me': { ...VIEWER, identity_id: 1 },
      'GET /api/inbox': inbox(),
      'POST /api/inbox/11/read': jsonError(403, 'not_your_inbox', '只有收件人本人或 admin 可以標為已讀'),
      'POST /api/inbox/read-all': jsonError(403, 'not_your_inbox', '不行'),
    })
    const second = await mountAt('/inbox')
    await second.wrapper.get('[data-id="11"] button').trigger('click')
    await flushPromises()
    expect(second.wrapper.get('[role="alert"]').text()).toContain('只有收件人本人')
    await second.wrapper.get('.inbox-bar button').trigger('click')
    await flushPromises()
    expect(second.wrapper.get('[role="alert"]').text()).toContain('不行')
  })
})
