// controller 後端 API 的回應型別（契約見 controller/backend/README.md 與 app/views.py）。

export type Role = 'admin' | 'viewer' | string

export interface Me {
  username: string
  display_name: string | null
  role: Role
  identity_id: number | null
}

export interface IdentityBrief {
  id: number
  hrm_employee_id: string
  employee_number: string | null
  display_name: string | null
  role: string | null
  /** 確認由誰處理：agent（員工 agent 代理）／human（真人確認）；舊版後端沒有這個欄位 */
  confirm_mode?: ConfirmMode | null
}

export type ConfirmMode = 'agent' | 'human'

export interface ConfirmModeView {
  recipient: IdentityBrief
  confirm_mode: ConfirmMode
  human: boolean
  agent_id: string | null
  changed?: boolean
}

export interface Stuck {
  code: 'agent_no_report' | 'approval_waiting_too_long' | 'retries_exhausted' | 'system_error' | string
  reason: string
  step: string
  waited_seconds: number
}

export interface WaitingFor {
  kind: 'agent' | 'human'
  id: string | null
  name: string
}

export interface FlowSummary {
  id: string
  type: string
  title: string | null
  status: string
  revision: number
  current_step: string | null
  current_step_title: string | null
  current_step_status: string | null
  waiting_for: WaitingFor | null
  elapsed_seconds: number | null
  step_elapsed_seconds: number | null
  stuck: Stuck | null
  created_by: string | null
  /** 課堂示範模式的流程（時限縮短）；舊版後端沒有這個欄位 */
  demo?: boolean
  created_at: string | null
  updated_at: string | null
  finished_at: string | null
}

export interface StepView {
  name: string
  title: string | null
  kind: string
  assignee: string
  status: string
  summary: string | null
  error: string | null
  error_kind: string | null
  attempts: number
  failures: number
  max_attempts: number
  deadline_at: string | null
  dispatched_at: string | null
  started_at: string | null
  finished_at: string | null
  duration_seconds: number | null
  stuck: Stuck | null
}

export interface ApprovalView {
  id: string
  flow_id: string
  step: string
  approver: IdentityBrief | null
  question: string
  options: { value: string; label: string }[]
  status: string
  decision: string | null
  comment: string | null
  decided_at: string | null
  expires_at: string | null
  remaining_seconds: number | null
  sent_count: number
  created_at: string | null
}

export interface AuditEntry {
  id: number
  flow_id: string | null
  actor: string
  action: string
  detail: Record<string, unknown>
  created_at: string | null
}

/** 流程送給客戶的訊息（LINE 接上前是模擬通道，後端 channel_messages） */
export interface ChannelMessageView {
  id: number
  channel: string
  direction: 'in' | 'out'
  conversation_id: string | null
  agent_id: string
  external_customer_id: string | null
  display_name: string | null
  body: string
  flow_id: string | null
  created_at: string | null
}

export interface FlowDetail extends FlowSummary {
  steps: StepView[]
  approvals: ApprovalView[]
  audit: AuditEntry[]
  messages?: ChannelMessageView[]
}

export interface FlowList {
  total: number
  items: FlowSummary[]
}

export interface PendingApproval {
  approval_id: string
  flow_id: string
  flow_title: string | null
  step: string
  step_title: string | null
  approver: IdentityBrief | null
  question: string
  deadline_at: string | null
  remaining_seconds: number | null
}

/** OpenClaw gateway 健康檢查（後端每分鐘查 /readyz）。 */
export interface GatewayStatus {
  status: 'ok' | 'offline' | 'degraded' | 'unknown' | string
  message?: string | null
  failing: string[]
  checked_at?: string | null
}

export interface StatsSummary {
  generated_at: string
  counts: {
    /** 只算 status=running（agent 或系統處理中），不含等待確認 */
    running: number
    waiting_approval: number
    /** 進行中＋等待確認（舊版後端沒有這個欄位） */
    active?: number
    stuck: number
    succeeded_today: number
    failed_today: number
  }
  stuck: FlowSummary[]
  pending_approvals: PendingApproval[]
  gateway?: GatewayStatus
}

export interface AgentCard {
  id: string
  display_name: string | null
  emoji: string | null
  role: string
  enabled: boolean
  status: 'idle' | 'running' | 'error' | string
  current_steps: {
    flow_id: string
    flow_title: string | null
    step: string
    step_title: string | null
    status: string
    dispatched_at: string | null
    deadline_at: string | null
    overdue: boolean
  }[]
  today: { succeeded: number; failed: number }
  avg_duration_seconds: number | null
  last_seen_at: string | null
  last_error_at: string | null
  last_error: string | null
}

export interface AgentList {
  gateway?: GatewayStatus
  items: AgentCard[]
}

export interface AgentStepRecord {
  flow_id: string
  flow_title: string | null
  flow_type: string
  step: string
  step_title: string | null
  status: string
  attempts: number
  dispatched_at: string | null
  finished_at: string | null
  duration_seconds: number | null
  summary: string | null
  error: string | null
  error_kind?: string | null
}

export interface AgentDetail extends AgentCard {
  steps: AgentStepRecord[]
  gateway?: GatewayStatus
}

export interface PublicApproval {
  id: string
  flow_title: string | null
  step_title: string | null
  approver_name: string | null
  question: string
  options: { value: string; label: string }[]
  context: Record<string, unknown>
  /** 確認時要填的欄位（例如 ② 的退款金額）；applies_to 列出要帶這個欄位的選項 */
  fields?: ApprovalField[]
  expires_at: string | null
  remaining_seconds: number
}

export interface ApprovalField {
  name: string
  label: string
  type: 'int'
  applies_to: string[]
  default?: number | null
  min?: number
  max?: number
}

export interface NotificationItem {
  id: number
  kind: string
  title: string
  body: string | null
  link: string | null
  link_hidden: boolean
  flow_id: string | null
  approval_id: string | null
  /** 所附確認的狀態：pending／superseded（已重送）／link_expired／decided／expired／cancelled；非確認通知為 null */
  approval_status?: string | null
  /** 已決定時的決定（顯示文字） */
  approval_decision?: string | null
  created_at: string | null
  read_at: string | null
}

export interface Inbox {
  recipient: IdentityBrief
  unread_count: number
  items: NotificationItem[]
}

export interface Recipient extends IdentityBrief {
  unread_count: number
}
