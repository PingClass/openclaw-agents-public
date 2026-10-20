// 讀取資料的共用邏輯：載入中、錯誤訊息、session 過期時回登入頁。
import { ref, shallowRef, type Ref, type ShallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError } from './api'
import { resetSession } from './session'

export interface Resource<T> {
  data: ShallowRef<T | null>
  error: Ref<string>
  /** 後端錯誤代碼（detail.code），沒有錯誤時為空字串 */
  code: Ref<string>
  loading: Ref<boolean>
  reload: () => Promise<void>
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message
  return '無法連線到 controller，請稍後再試。'
}

export function useResource<T>(loader: () => Promise<T>): Resource<T> {
  const router = useRouter()
  const data = shallowRef<T | null>(null)
  const error = ref('')
  const code = ref('')
  const loading = ref(false)
  let seq = 0

  async function reload() {
    const mine = ++seq
    loading.value = true
    try {
      const result = await loader()
      if (mine !== seq) return
      data.value = result
      error.value = ''
      code.value = ''
    } catch (err) {
      if (mine !== seq) return
      if (err instanceof ApiError && err.status === 401) {
        resetSession()
        await router.push({ name: 'login', query: { next: router.currentRoute.value.fullPath } })
        return
      }
      error.value = errorMessage(err)
      code.value = err instanceof ApiError ? err.code : 'network_error'
    } finally {
      if (mine === seq) loading.value = false
    }
  }

  return { data, error, code, loading, reload }
}
