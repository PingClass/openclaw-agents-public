// controller 後端 API 的呼叫。全部同源、帶 session cookie；錯誤格式 {"detail": {"code", "message"}}。
import type {
  AgentDetail,
  AgentList,
  ApprovalView,
  ConfirmMode,
  ConfirmModeView,
  FlowDetail,
  FlowList,
  Inbox,
  Me,
  PublicApproval,
  FlowSummary,
  Recipient,
  StatsSummary,
} from './types'

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

type Query = Record<string, string | number | boolean | null | undefined>

export function buildQuery(params: Query = {}): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '' || value === false) continue
    search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...options.headers },
  })
  if (!response.ok) {
    let code = 'http_error'
    let message = `API ${response.status}`
    try {
      const body = await response.json()
      const detail = body?.detail
      if (detail && typeof detail === 'object') {
        code = detail.code ?? code
        message = detail.message ?? message
      } else if (typeof detail === 'string') {
        message = detail
      }
    } catch {
      // 不是 JSON：保留預設訊息
    }
    throw new ApiError(response.status, code, message)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

// ---------------------------------------------------------------- 登入
export const login = (username: string, password: string) => post<Me>('/auth/login', { username, password })
export const logout = () => post<{ ok: boolean }>('/auth/logout')
export const fetchMe = () => api<Me>('/auth/me')

// ---------------------------------------------------------------- 總覽、流程、agent
export const fetchSummary = () => api<StatsSummary>('/stats/summary')
export const fetchAgents = (role = 'assistant') => api<AgentList>(`/agents${buildQuery({ role })}`)

export interface AgentStepFilters {
  /** succeeded／failed／running／skipped */
  result?: string
  from?: string
  to?: string
  limit?: number
}
export const fetchAgent = (id: string, filters: AgentStepFilters = {}) =>
  api<AgentDetail>(`/agents/${encodeURIComponent(id)}${buildQuery({ ...filters })}`)

export interface FlowFilters {
  type?: string
  status?: string
  stuck?: boolean
  from?: string
  to?: string
  limit?: number
  offset?: number
}
export const fetchFlows = (filters: FlowFilters = {}) =>
  api<FlowList>(`/flows${buildQuery({ ...filters, stuck: filters.stuck ? true : undefined })}`)
export const fetchFlow = (id: string) => api<FlowDetail>(`/flows/${encodeURIComponent(id)}`)

// ---------------------------------------------------------------- 人的操作（只有 admin；後端寫入稽核）
// 都要帶畫面上的 revision，資料已被別人改過時後端回 409 revision_conflict。
export const retryStep = (flowId: string, step: string, revision: number) =>
  post<FlowSummary>(`/flows/${encodeURIComponent(flowId)}/steps/${encodeURIComponent(step)}/retry`, { revision })
export const cancelFlow = (flowId: string, revision: number, reason?: string) =>
  post<FlowSummary>(`/flows/${encodeURIComponent(flowId)}/cancel`, { revision, reason: reason || null })
export const resendApproval = (approvalId: string, revision: number) =>
  post<ApprovalView>(`/approvals/${encodeURIComponent(approvalId)}/resend`, { revision })

// ---------------------------------------------------------------- 確認頁（免登入，只認一次性 token）
export const fetchApproval = (id: string, token: string) =>
  api<PublicApproval>(`/approvals/${encodeURIComponent(id)}${buildQuery({ token })}`)
export const decideApproval = (
  id: string,
  token: string,
  decision: string,
  comment?: string,
  data?: Record<string, unknown>,
) =>
  post<{ id: string; status: string; decision: string }>(`/approvals/${encodeURIComponent(id)}/decision`, {
    token,
    decision,
    comment: comment || null,
    ...(data && Object.keys(data).length ? { data } : {}),
  })

// ---------------------------------------------------------------- 收件匣（0.5）
export const fetchInbox = (recipient?: string, unreadOnly = false) =>
  api<Inbox>(`/inbox${buildQuery({ recipient, unread_only: unreadOnly })}`)
export const fetchRecipients = () => api<{ items: Recipient[] }>('/inbox/recipients')
export const markRead = (id: number) => post<{ id: number; read_at: string }>(`/inbox/${id}/read`)
export const markAllRead = (recipient?: string) =>
  post<{ marked: number }>(`/inbox/read-all${buildQuery({ recipient })}`)
/** admin：把確認者切成真人確認（human）或 agent 代理（agent），後端寫入稽核 */
export const setConfirmMode = (recipient: string, mode: ConfirmMode) =>
  api<ConfirmModeView>(`/inbox/confirm-mode${buildQuery({ recipient })}`, {
    method: 'PUT',
    body: JSON.stringify({ mode }),
  })
