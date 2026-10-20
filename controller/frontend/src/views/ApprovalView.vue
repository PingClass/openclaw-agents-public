<script setup lang="ts">
// 一次性確認頁（計畫 3.5、3.7）：手機優先、免登入，只顯示該步驟的 context。
import { computed, onMounted, ref } from 'vue'
import { ApiError, decideApproval, fetchApproval } from '../api'
import { entries, formatDuration, formatTime } from '../format'
import type { ApprovalField, PublicApproval } from '../types'

const props = defineProps<{ id: string; token: string }>()

const approval = ref<PublicApproval | null>(null)
const loading = ref(true)
const error = ref('')
const comment = ref('')
const busy = ref(false)
const done = ref<{ label: string } | null>(null)
// 確認時要填的欄位（例如退款金額），預設值由後端給
const fieldValues = ref<Record<string, string | number>>({})

const ERROR_TEXT: Record<string, string> = {
  token_used: '這個確認連結已經用過了。',
  token_expired: '這個確認連結已經過期。',
  invalid_token: '確認連結無效，可能已經重送過新的連結。',
  approval_closed: '這個確認已經不在等待中。',
  approval_not_found: '找不到這個確認。',
  flow_finished: '流程已經結束。',
}

function describe(err: unknown): string {
  if (err instanceof ApiError) return ERROR_TEXT[err.code] ?? err.message
  return '無法連線，請稍後再試。'
}

onMounted(async () => {
  if (!props.token) {
    error.value = '連結缺少 token，請從通知重新開啟。'
    loading.value = false
    return
  }
  try {
    approval.value = await fetchApproval(props.id, props.token)
    for (const field of approval.value.fields ?? []) {
      fieldValues.value[field.name] = field.default == null ? '' : String(field.default)
    }
  } catch (err) {
    error.value = describe(err)
  } finally {
    loading.value = false
  }
})

const contextRows = computed(() => entries(approval.value?.context))
const fields = computed<ApprovalField[]>(() => approval.value?.fields ?? [])

function fieldData(value: string): Record<string, unknown> | undefined {
  const data: Record<string, unknown> = {}
  for (const field of fields.value) {
    if (!field.applies_to.includes(value)) continue
    // type=number 的 v-model 會給數字，清空時給空字串
    const raw = String(fieldValues.value[field.name] ?? '').trim()
    if (raw !== '') data[field.name] = /^-?\d+$/.test(raw) ? Number(raw) : raw
  }
  return Object.keys(data).length ? data : undefined
}

async function choose(value: string, text: string) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await decideApproval(props.id, props.token, value, comment.value.trim() || undefined, fieldData(value))
    done.value = { label: text }
  } catch (err) {
    error.value = describe(err)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="approval-page">
    <div class="approval-card">
      <p class="eyebrow">工作流確認</p>
      <p v-if="loading" class="muted">載入中…</p>

      <template v-else-if="done">
        <h1>已送出：{{ done.label }}</h1>
        <p>謝謝，流程會接著處理。這個連結已失效，可以關閉頁面。</p>
      </template>

      <template v-else-if="approval">
        <p class="muted small">{{ approval.flow_title }}・{{ approval.step_title }}</p>
        <h1>{{ approval.question }}</h1>
        <p v-if="approval.approver_name" class="small">確認人：{{ approval.approver_name }}</p>
        <dl v-if="contextRows.length" class="context" data-testid="approval-context">
          <template v-for="row in contextRows" :key="row.key">
            <dt>{{ row.key }}</dt>
            <dd>{{ row.value }}</dd>
          </template>
        </dl>
        <p class="small muted">
          剩 {{ formatDuration(approval.remaining_seconds) }}，{{ formatTime(approval.expires_at) }} 截止
        </p>
        <label v-for="field in fields" :key="field.name" class="comment" :data-field="field.name">
          {{ field.label }}（選「{{ approval.options.filter((o) => field.applies_to.includes(o.value)).map((o) => o.label).join('／') }}」時使用）
          <input
            v-model="fieldValues[field.name]"
            type="number"
            inputmode="numeric"
            :min="field.min"
            :max="field.max"
            :name="field.name"
          />
        </label>
        <label class="comment">意見（選填）
          <textarea v-model="comment" rows="3" maxlength="2000" placeholder="例如：改成下午兩點" />
        </label>
        <p v-if="error" role="alert" class="error">{{ error }}</p>
        <div class="options">
          <button
            v-for="(option, index) in approval.options"
            :key="option.value"
            type="button"
            :class="{ ghost: index > 0, danger: option.value === 'reject' }"
            :disabled="busy"
            :data-option="option.value"
            @click="choose(option.value, option.label)"
          >
            {{ option.label }}
          </button>
        </div>
      </template>

      <template v-else>
        <h1>無法開啟確認</h1>
        <p role="alert" class="error">{{ error }}</p>
      </template>
    </div>
  </main>
</template>
