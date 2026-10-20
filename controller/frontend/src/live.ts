// 即時更新（計畫 3.7）：優先用 SSE（GET /api/events），斷線時改成每 10 秒輪詢，
// 並定期嘗試重連 SSE，連上後停止輪詢。
import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue'

export type LiveMode = 'connecting' | 'sse' | 'polling'

export interface LiveEvent {
  type: string
  data: Record<string, unknown>
}

// 後端會送出的事件（app/engine.py、app/notify.py）
export const LIVE_EVENT_TYPES = [
  'flow.created',
  'flow.updated',
  'flow.stuck',
  'flow.succeeded',
  'flow.failed',
  'flow.expired',
  'flow.cancelled',
  'step.updated',
  'approval.created',
  'approval.decided',
  'approval.resent',
  'notification.created',
] as const

export interface EventSourceLike {
  addEventListener(type: string, listener: (event: MessageEvent) => void): void
  close(): void
  onerror: ((event: Event) => void) | null
}

export interface LiveOptions {
  url?: string
  pollMs?: number
  reconnectMs?: number
  debounceMs?: number
  /** 只處理符合條件的事件（例如詳情頁只看自己的 flow_id）；不給就全部處理 */
  filter?: (event: LiveEvent) => boolean
  createEventSource?: (url: string) => EventSourceLike
}

/** 全站共用的連線狀態，標題列顯示用。 */
export const liveMode: Ref<LiveMode> = ref('connecting')

export class LiveUpdates {
  mode: LiveMode = 'connecting'
  private source: EventSourceLike | null = null
  private pollTimer: ReturnType<typeof setInterval> | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private debounceTimer: ReturnType<typeof setTimeout> | null = null
  private stopped = true
  private readonly opts: Required<Omit<LiveOptions, 'filter'>> & Pick<LiveOptions, 'filter'>

  constructor(
    private readonly onChange: (event?: LiveEvent) => void,
    options: LiveOptions = {},
    private readonly onMode: (mode: LiveMode) => void = () => {},
  ) {
    this.opts = {
      url: options.url ?? '/api/events',
      pollMs: options.pollMs ?? 10_000,
      reconnectMs: options.reconnectMs ?? 30_000,
      debounceMs: options.debounceMs ?? 300,
      filter: options.filter,
      createEventSource:
        options.createEventSource ??
        ((url: string) => new EventSource(url, { withCredentials: true }) as unknown as EventSourceLike),
    }
  }

  start(): void {
    this.stopped = false
    this.connect()
  }

  stop(): void {
    this.stopped = true
    this.source?.close()
    this.source = null
    this.clearPolling()
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    if (this.debounceTimer) clearTimeout(this.debounceTimer)
    this.reconnectTimer = null
    this.debounceTimer = null
  }

  private setMode(mode: LiveMode): void {
    this.mode = mode
    this.onMode(mode)
  }

  private connect(): void {
    if (this.stopped) return
    let source: EventSourceLike
    try {
      source = this.opts.createEventSource(this.opts.url)
    } catch {
      this.fallback()
      return
    }
    this.source = source
    source.addEventListener('hello', () => {
      const wasPolling = this.mode === 'polling'
      this.setMode('sse')
      this.clearPolling()
      // 從輪詢切回 SSE：補抓一次，避免漏掉中斷期間的變化
      if (wasPolling) this.onChange()
    })
    for (const type of LIVE_EVENT_TYPES) {
      source.addEventListener(type, (message: MessageEvent) => {
        let data: Record<string, unknown> = {}
        try {
          data = JSON.parse(String(message.data ?? '{}'))
        } catch {
          data = {}
        }
        const event = { type, data }
        if (this.opts.filter && !this.opts.filter(event)) return
        this.schedule(event)
      })
    }
    source.onerror = () => this.fallback()
  }

  private schedule(event: LiveEvent): void {
    if (this.debounceTimer) clearTimeout(this.debounceTimer)
    this.debounceTimer = setTimeout(() => {
      this.debounceTimer = null
      this.onChange(event)
    }, this.opts.debounceMs)
  }

  private fallback(): void {
    this.source?.close()
    this.source = null
    if (this.stopped) return
    this.setMode('polling')
    if (!this.pollTimer) this.pollTimer = setInterval(() => this.onChange(), this.opts.pollMs)
    if (!this.reconnectTimer) {
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null
        this.connect()
      }, this.opts.reconnectMs)
    }
  }

  private clearPolling(): void {
    if (this.pollTimer) clearInterval(this.pollTimer)
    this.pollTimer = null
  }
}

/** 在元件掛載期間保持即時更新。 */
export function useLiveUpdates(onChange: (event?: LiveEvent) => void, options: LiveOptions = {}) {
  const live = new LiveUpdates(onChange, options, (mode) => (liveMode.value = mode))
  onMounted(() => live.start())
  onBeforeUnmount(() => live.stop())
  return live
}
