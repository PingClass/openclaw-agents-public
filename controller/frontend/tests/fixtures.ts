import type { AgentCard, FlowDetail, FlowSummary, Inbox, StatsSummary } from '../src/types'

export function flowSummary(overrides: Partial<FlowSummary> = {}): FlowSummary {
  return {
    id: 'fl_1',
    type: 'demo',
    title: '示範：派車',
    status: 'running',
    revision: 3,
    current_step: 'draft',
    current_step_title: '產生草稿',
    current_step_status: 'dispatched',
    waiting_for: { kind: 'agent', id: 'admin-agent', name: '行政助理' },
    elapsed_seconds: 600,
    step_elapsed_seconds: 420,
    stuck: null,
    created_by: 'agent:cs-agent',
    created_at: '2026-10-12T02:00:00Z',
    updated_at: '2026-10-12T02:01:00Z',
    finished_at: null,
    ...overrides,
  }
}

export const STUCK = {
  code: 'agent_no_report',
  reason: 'admin-agent 已 7 分鐘沒有回報',
  step: 'draft',
  waited_seconds: 420,
}

export function summary(overrides: Partial<StatsSummary> = {}): StatsSummary {
  return {
    generated_at: '2026-10-12T02:10:00Z',
    counts: { running: 2, waiting_approval: 1, stuck: 1, succeeded_today: 4, failed_today: 1 },
    stuck: [flowSummary({ stuck: STUCK })],
    pending_approvals: [
      {
        approval_id: 'ap_1',
        flow_id: 'fl_2',
        flow_title: '示範：報帳',
        step: 'confirm',
        step_title: '確認草稿',
        approver: { id: 1, hrm_employee_id: 'h1', employee_number: 'E001', display_name: '王經理', role: 'sales_manager' },
        question: '請確認報帳草稿',
        deadline_at: '2026-10-12T10:00:00Z',
        remaining_seconds: 7200,
      },
    ],
    ...overrides,
  }
}

export function agentCard(overrides: Partial<AgentCard> = {}): AgentCard {
  return {
    id: 'admin-agent',
    display_name: '行政助理',
    emoji: null,
    role: 'assistant',
    enabled: true,
    status: 'running',
    current_steps: [
      {
        flow_id: 'fl_1',
        flow_title: '示範：派車',
        step: 'draft',
        step_title: '產生草稿',
        status: 'dispatched',
        dispatched_at: '2026-10-12T02:00:00Z',
        deadline_at: '2026-10-12T02:05:00Z',
        overdue: true,
      },
    ],
    today: { succeeded: 3, failed: 1 },
    avg_duration_seconds: 95,
    last_seen_at: '2026-10-12T01:00:00Z',
    last_error_at: '2026-10-12T00:30:00Z',
    last_error: 'ADM 無法連線',
    ...overrides,
  }
}

export function flowDetail(overrides: Partial<FlowDetail> = {}): FlowDetail {
  return {
    ...flowSummary({ status: 'waiting_approval', current_step: 'confirm', current_step_title: '確認草稿',
      waiting_for: { kind: 'human', id: null, name: '王經理' },
      stuck: { code: 'approval_waiting_too_long', reason: '等王經理確認草稿，已 2 小時，14:00 截止', step: 'confirm', waited_seconds: 7200 } }),
    steps: [
      {
        name: 'draft', title: '產生草稿', kind: 'agent', assignee: 'admin-agent', status: 'succeeded',
        summary: '草稿已產生', error: null, error_kind: null, attempts: 2, failures: 1, max_attempts: 2,
        deadline_at: null, dispatched_at: '2026-10-12T02:00:00Z', started_at: null,
        finished_at: '2026-10-12T02:03:00Z', duration_seconds: 180, stuck: null,
      },
      {
        name: 'confirm', title: '確認草稿', kind: 'approval', assignee: 'human', status: 'waiting_approval',
        summary: null, error: null, error_kind: null, attempts: 1, failures: 0, max_attempts: 1,
        deadline_at: '2026-10-12T06:00:00Z', dispatched_at: null, started_at: '2026-10-12T02:03:00Z',
        finished_at: null, duration_seconds: null,
        stuck: { code: 'approval_waiting_too_long', reason: '等王經理確認草稿，已 2 小時，14:00 截止', step: 'confirm', waited_seconds: 7200 },
      },
      {
        name: 'finalize', title: '完成作業', kind: 'agent', assignee: 'finance-agent', status: 'pending',
        summary: null, error: null, error_kind: null, attempts: 0, failures: 0, max_attempts: 3,
        deadline_at: null, dispatched_at: null, started_at: null, finished_at: null, duration_seconds: null, stuck: null,
      },
    ],
    approvals: [
      {
        id: 'ap_old', flow_id: 'fl_1', step: 'confirm',
        approver: { id: 1, hrm_employee_id: 'h1', employee_number: 'E001', display_name: '王經理', role: 'sales_manager' },
        question: '請確認草稿 v1', options: [{ value: 'approve', label: '核准' }, { value: 'revise', label: '退回重擬' }],
        status: 'decided', decision: 'revise', comment: '改時間', decided_at: '2026-10-12T02:02:00Z',
        expires_at: '2026-10-12T06:00:00Z', remaining_seconds: null, sent_count: 1, created_at: '2026-10-12T02:01:00Z',
      },
      {
        id: 'ap_new', flow_id: 'fl_1', step: 'confirm',
        approver: { id: 1, hrm_employee_id: 'h1', employee_number: 'E001', display_name: '王經理', role: 'sales_manager' },
        question: '請確認草稿 v2', options: [{ value: 'approve', label: '核准' }],
        status: 'pending', decision: null, comment: null, decided_at: null,
        expires_at: '2026-10-12T06:00:00Z', remaining_seconds: 3600, sent_count: 2, created_at: '2026-10-12T02:03:00Z',
      },
    ],
    audit: [
      { id: 1, flow_id: 'fl_1', actor: 'agent:cs-agent', action: 'flow.started', detail: { type: 'demo', phone: '0912-000-000' }, created_at: '2026-10-12T02:00:00Z' },
      { id: 2, flow_id: 'fl_1', actor: 'controller', action: 'approval.created', detail: {}, created_at: '2026-10-12T02:03:00Z' },
    ],
    ...overrides,
  }
}

export function inbox(overrides: Partial<Inbox> = {}): Inbox {
  return {
    recipient: { id: 1, hrm_employee_id: 'h1', employee_number: 'E001', display_name: '王經理', role: 'sales_manager' },
    unread_count: 1,
    items: [
      {
        id: 11, kind: 'approval_request', title: '待確認：示範：派車', body: '請確認草稿',
        link: 'http://controller.example/approvals/ap_new?token=tok123', link_hidden: false,
        flow_id: 'fl_1', approval_id: 'ap_new', created_at: '2026-10-12T02:03:00Z', read_at: null,
      },
      {
        id: 10, kind: 'info', title: '流程已完成', body: null, link: null, link_hidden: false,
        flow_id: 'fl_0', approval_id: null, created_at: '2026-10-12T01:00:00Z', read_at: '2026-10-12T01:30:00Z',
      },
    ],
    ...overrides,
  }
}
