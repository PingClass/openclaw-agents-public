// 示範模式（計畫 3.7）：課堂投影用，字放大，只顯示總覽與一條流程的時間軸。
// 開關記在 localStorage；網址參數 ?demo=1／?demo=0 可以直接指定（會一併記住），方便把連結貼給投影電腦。
import { ref } from 'vue'

export const DEMO_STORAGE_KEY = 'flow-controller.demo-mode'

export const demoMode = ref(false)

function readStored(): boolean {
  try {
    return globalThis.localStorage?.getItem(DEMO_STORAGE_KEY) === '1'
  } catch {
    return false
  }
}

function store(on: boolean): void {
  try {
    if (on) globalThis.localStorage?.setItem(DEMO_STORAGE_KEY, '1')
    else globalThis.localStorage?.removeItem(DEMO_STORAGE_KEY)
  } catch {
    // 無痕視窗或封鎖儲存：只在這次瀏覽有效
  }
}

/** 讀取記住的設定（App 啟動時呼叫一次）。 */
export function initDemoMode(): void {
  demoMode.value = readStored()
}

export function setDemoMode(on: boolean): void {
  demoMode.value = on
  store(on)
}

/** 網址的 demo 參數：'1'／'true'／'on' 開、'0'／'false'／'off' 關，其他值不理會。 */
export function applyDemoQuery(value: unknown): void {
  if (typeof value !== 'string') return
  if (['1', 'true', 'on'].includes(value)) setDemoMode(true)
  else if (['0', 'false', 'off'].includes(value)) setDemoMode(false)
}
