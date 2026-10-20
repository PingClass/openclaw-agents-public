import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LiveUpdates, type EventSourceLike } from '../src/live'

class FakeSource implements EventSourceLike {
  listeners = new Map<string, ((event: MessageEvent) => void)[]>()
  onerror: ((event: Event) => void) | null = null
  closed = false
  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener])
  }
  close() {
    this.closed = true
  }
  emit(type: string, data: unknown = {}) {
    for (const listener of this.listeners.get(type) ?? []) listener({ data: JSON.stringify(data) } as MessageEvent)
  }
}

describe('即時更新：SSE 與輪詢備援', () => {
  let sources: FakeSource[]
  const factory = () => {
    const source = new FakeSource()
    sources.push(source)
    return source
  }

  beforeEach(() => {
    vi.useFakeTimers()
    sources = []
  })
  afterEach(() => vi.useRealTimers())

  it('收到 hello 進入 SSE 模式，事件經防抖後觸發更新', () => {
    const onChange = vi.fn()
    const modes: string[] = []
    const live = new LiveUpdates(onChange, { createEventSource: factory }, (m) => modes.push(m))
    live.start()
    sources[0].emit('hello')
    expect(live.mode).toBe('sse')
    sources[0].emit('step.updated', { flow_id: 'fl_1' })
    sources[0].emit('flow.updated', { flow_id: 'fl_1' })
    expect(onChange).not.toHaveBeenCalled()
    vi.advanceTimersByTime(300)
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange.mock.calls[0][0]).toEqual({ type: 'flow.updated', data: { flow_id: 'fl_1' } })
    expect(modes).toEqual(['sse'])
    live.stop()
    expect(sources[0].closed).toBe(true)
  })

  it('斷線時每 10 秒輪詢，之後重連成功就停止輪詢並補抓一次', () => {
    const onChange = vi.fn()
    const live = new LiveUpdates(onChange, { createEventSource: factory, reconnectMs: 30_000 })
    live.start()
    sources[0].onerror?.(new Event('error'))
    expect(live.mode).toBe('polling')
    expect(sources[0].closed).toBe(true)
    vi.advanceTimersByTime(10_000)
    expect(onChange).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(10_000)
    expect(onChange).toHaveBeenCalledTimes(2)
    vi.advanceTimersByTime(10_000) // 30 秒：第三次輪詢＋重連
    expect(onChange).toHaveBeenCalledTimes(3)
    expect(sources).toHaveLength(2)
    sources[1].emit('hello')
    expect(live.mode).toBe('sse')
    expect(onChange).toHaveBeenCalledTimes(4) // 切回 SSE 補抓
    vi.advanceTimersByTime(60_000)
    expect(onChange).toHaveBeenCalledTimes(4)
    live.stop()
  })

  it('EventSource 無法建立時直接輪詢；filter 過濾不相關事件；壞掉的 JSON 不會拋錯', () => {
    const onChange = vi.fn()
    const broken = new LiveUpdates(onChange, {
      createEventSource: () => {
        throw new Error('no EventSource')
      },
    })
    broken.start()
    expect(broken.mode).toBe('polling')
    vi.advanceTimersByTime(10_000)
    expect(onChange).toHaveBeenCalledTimes(1)
    broken.stop()
    vi.advanceTimersByTime(60_000)
    expect(onChange).toHaveBeenCalledTimes(1)

    const filtered = vi.fn()
    const live = new LiveUpdates(filtered, {
      createEventSource: factory,
      filter: (event) => event.data.flow_id === 'mine',
    })
    live.start()
    sources[0].emit('step.updated', { flow_id: 'other' })
    vi.advanceTimersByTime(500)
    expect(filtered).not.toHaveBeenCalled()
    for (const listener of sources[0].listeners.get('step.updated') ?? []) listener({ data: '{bad' } as MessageEvent)
    vi.advanceTimersByTime(500)
    expect(filtered).not.toHaveBeenCalled()
    sources[0].emit('step.updated', { flow_id: 'mine' })
    vi.advanceTimersByTime(500)
    expect(filtered).toHaveBeenCalledTimes(1)
    live.stop()
  })
})
