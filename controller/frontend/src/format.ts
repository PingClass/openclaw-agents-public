// 顯示用的格式化與標籤。時間一律以台北時間顯示。

export const TZ = 'Asia/Taipei'

export const FLOW_STATUS_LABELS: Record<string, string> = {
  running: '進行中',
  waiting_approval: '等待確認',
  succeeded: '完成',
  failed: '失敗',
  expired: '逾時結束',
  cancelled: '已取消',
}

export const STEP_STATUS_LABELS: Record<string, string> = {
  pending: '尚未開始',
  dispatched: '已派出',
  running: '執行中',
  waiting_approval: '等待確認',
  succeeded: '完成',
  failed: '失敗',
  skipped: '略過',
}

export const AGENT_STATUS_LABELS: Record<string, string> = {
  idle: '閒置',
  running: '執行中',
  error: '異常',
}

export const STUCK_LABELS: Record<string, string> = {
  agent_no_report: 'agent 沒回報',
  approval_waiting_too_long: '等人太久',
  retries_exhausted: '重試用完',
  system_error: '系統錯誤',
}

// 流程類型的顯示名稱；未列出的直接顯示代碼
export const FLOW_TYPE_LABELS: Record<string, string> = {
  demo: '示範流程',
  visit: '① 客戶拜訪',
  refund: '② 客訴退款',
  onboarding: '③ 新人到職',
}

export const APPROVAL_STATUS_LABELS: Record<string, string> = {
  pending: '等待中',
  decided: '已決定',
  expired: '已逾時',
  cancelled: '已取消',
}

/** 收件匣裡確認通知的狀態（後端 notification.approval_status） */
export const INBOX_APPROVAL_LABELS: Record<string, string> = {
  pending: '等待確認',
  superseded: '已重送，這個連結已失效',
  link_expired: '連結已過期，請 admin 重送',
  decided: '已決定',
  expired: '已逾時',
  cancelled: '流程已取消',
}

/** Agent 詳情的「結果」篩選（後端 /agents/{id}?result=） */
export const AGENT_RESULT_LABELS: Record<string, string> = {
  succeeded: '完成',
  failed: '失敗',
  running: '執行中',
  skipped: '略過',
}

export function label(map: Record<string, string>, key: string | null | undefined): string {
  if (!key) return '—'
  return map[key] ?? key
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  const total = Math.max(0, Math.floor(seconds))
  if (total < 60) return `${total} 秒`
  const minutes = Math.floor(total / 60)
  if (minutes < 60) return `${minutes} 分鐘`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) {
    const rest = minutes % 60
    return rest ? `${hours} 小時 ${rest} 分鐘` : `${hours} 小時`
  }
  return `${Math.floor(hours / 24)} 天`
}

const timeFormatter = new Intl.DateTimeFormat('zh-TW', {
  timeZone: TZ,
  month: 'numeric',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return timeFormatter.format(date)
}

// 畫面上不顯示電話、地址（後端已過濾，前端再擋一次）。規則與後端 app/sanitize.py 一致。
const SENSITIVE_KEY = /(phone|mobile|tel(ephone)?$|^tel_|fax|address|addr|電話|手機|地址|住址)/i

export function isSensitiveKey(key: string): boolean {
  return SENSITIVE_KEY.test(key)
}

// 自由文字（摘要、錯誤、通知內文、確認意見）裡的電話號碼也遮掉：手機 09xx-xxx-xxx、+886 9xx…、
// 市話 0x-xxxx-xxxx（含不加分隔的 02xxxxxxxx 與 +886-2-…）。寧可多遮，不可漏遮。
// 地址無法可靠辨識，由後端與 skill 避免寫進摘要（計畫 3.7「畫面上的個資」）。
const PHONE_IN_TEXT =
  /(?:\+886[-\s]?|\b0)9\d{2}[-\s]?\d{3}[-\s]?\d{3}\b|(?:\+886[-\s]?\(?|\(?\b0)\d{1,2}\)?[-\s]?\d{3,4}[-\s]?\d{4}\b/g

export function maskText(text: string | null | undefined): string {
  if (!text) return ''
  return text.replace(PHONE_IN_TEXT, '（電話已隱藏）')
}

export function sanitize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sanitize)
  if (typeof value === 'string') return maskText(value)
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {}
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      if (!isSensitiveKey(key)) out[key] = sanitize(item)
    }
    return out
  }
  return value
}

/** 把物件攤平成可顯示的「欄位：值」列表（巢狀物件以 JSON 呈現）。 */
export function entries(value: unknown): { key: string; value: string }[] {
  const clean = sanitize(value)
  if (!clean || typeof clean !== 'object' || Array.isArray(clean)) return []
  return Object.entries(clean as Record<string, unknown>).map(([key, item]) => ({
    key,
    value:
      item === null || item === undefined
        ? '—'
        : typeof item === 'object'
          ? JSON.stringify(item, null, 1)
          : String(item),
  }))
}

/**
 * 確認連結是 `<public_url>/approvals/<id>?token=…` 的絕對網址。
 * 同一個前端內改用相對路徑開啟，避免 public_url 設成別的主機名時跳出去。
 */
export function localApprovalPath(link: string | null | undefined): string | null {
  if (!link) return null
  try {
    const url = new URL(link, 'http://placeholder.invalid')
    if (!url.pathname.startsWith('/approvals/')) return null
    return `${url.pathname}${url.search}`
  } catch {
    return null
  }
}
