<script setup lang="ts">
// 流程的寫入操作（計畫 3.7 ④）：重試這一步、重送確認通知、取消流程。
// 只有 admin 看得到（外層包 AdminOnly）；每個操作都要再確認一次；後端帶 revision 防止過期操作，並寫入稽核。
import { computed, ref } from 'vue'
import { ApiError, cancelFlow, resendApproval, retryStep } from '../api'
import { errorMessage } from '../resource'
import type { FlowDetail } from '../types'

type Action = 'retry' | 'resend' | 'cancel'

const props = defineProps<{ flow: FlowDetail }>()
const emit = defineEmits<{ done: [action: Action] }>()

const active = computed(() => props.flow.status === 'running' || props.flow.status === 'waiting_approval')
const currentStep = computed(() => props.flow.steps.find((step) => step.name === props.flow.current_step) ?? null)
const pendingApproval = computed(() => props.flow.approvals.find((approval) => approval.status === 'pending') ?? null)

// 可重試：目前步驟失敗，或卡住（agent 沒回報、系統錯誤、重試用完）；等人確認的步驟改用「重送確認通知」
const canRetry = computed(() => {
  const step = currentStep.value
  if (!active.value || !step) return false
  if (step.status === 'waiting_approval') return false
  if (!['failed', 'dispatched', 'running'].includes(step.status)) return false
  return Boolean(props.flow.stuck || step.status === 'failed')
})
const canResend = computed(() => active.value && Boolean(pendingApproval.value))

const stepName = computed(() => currentStep.value?.title || currentStep.value?.name || '')
const approverName = computed(() => pendingApproval.value?.approver?.display_name || '確認者')

const confirming = ref<Action | null>(null)
const reason = ref('')
const busy = ref(false)
const message = ref('')
const error = ref('')

const prompts = computed<Record<Action, { title: string; body: string; confirm: string }>>(() => ({
  retry: {
    title: `重試「${stepName.value}」？`,
    body: `會重新派工給 ${currentStep.value?.assignee ?? ''}，沿用同一個 Idempotency-Key，系統不會重複寫入。`,
    confirm: '確定重試',
  },
  resend: {
    title: `重送確認通知給${approverName.value}？`,
    body: '會產生新的確認連結並送到收件匣，舊連結立即失效。',
    confirm: '確定重送',
  },
  cancel: {
    title: '取消這條流程？',
    body: '流程會結束、未完成的步驟會略過、等待中的確認連結失效。已經寫進 ADM／CRM 的資料不會自動還原。',
    confirm: '確定取消流程',
  },
}))

const titles: Record<Action, string> = {
  retry: '已重試這一步，等 agent 回報。',
  resend: '已重送確認通知（舊連結已失效）。',
  cancel: '流程已取消。',
}

function ask(action: Action) {
  confirming.value = action
  reason.value = ''
  message.value = ''
  error.value = ''
}

function close() {
  confirming.value = null
}

async function run() {
  const action = confirming.value
  if (!action || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const revision = props.flow.revision
    if (action === 'retry') await retryStep(props.flow.id, props.flow.current_step ?? '', revision)
    else if (action === 'resend') await resendApproval(pendingApproval.value!.id, revision)
    else await cancelFlow(props.flow.id, revision, reason.value.trim())
    message.value = titles[action]
    confirming.value = null
    emit('done', action)
  } catch (err) {
    if (err instanceof ApiError && err.code === 'revision_conflict') {
      error.value = '流程剛剛有變化，畫面已更新，請確認後再操作一次。'
      confirming.value = null
      emit('done', action)
    } else {
      error.value = errorMessage(err)
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="panel actions" data-testid="flow-actions">
    <h2>操作</h2>
    <div class="action-row">
      <button
        type="button"
        data-action="retry"
        :disabled="!canRetry || busy"
        :title="canRetry ? `重新派工「${stepName}」` : '目前沒有失敗或卡住的步驟'"
        @click="ask('retry')"
      >
        重試這一步
      </button>
      <button
        type="button"
        data-action="resend"
        :disabled="!canResend || busy"
        :title="canResend ? `重送給${approverName}` : '目前沒有等待中的確認'"
        @click="ask('resend')"
      >
        重送確認通知
      </button>
      <button
        type="button"
        class="danger"
        data-action="cancel"
        :disabled="!active || busy"
        :title="active ? '結束這條流程' : '流程已結束'"
        @click="ask('cancel')"
      >
        取消流程
      </button>
    </div>

    <div v-if="confirming" class="confirm-box" role="alertdialog" aria-modal="false" :aria-label="prompts[confirming].title" data-testid="confirm-box">
      <strong>{{ prompts[confirming].title }}</strong>
      <p class="small">{{ prompts[confirming].body }}</p>
      <label v-if="confirming === 'cancel'" class="comment">取消原因（選填，會寫入稽核）
        <textarea v-model="reason" name="reason" rows="2" maxlength="500" />
      </label>
      <div class="action-row">
        <button type="button" :class="{ danger: confirming === 'cancel' }" data-testid="confirm-yes" :disabled="busy" @click="run">
          {{ busy ? '處理中…' : prompts[confirming].confirm }}
        </button>
        <button type="button" class="ghost" data-testid="confirm-no" :disabled="busy" @click="close">先不要</button>
      </div>
    </div>

    <p v-if="message" class="ok-text" role="status" data-testid="action-message">{{ message }}</p>
    <p v-if="error" class="error" role="alert" data-testid="action-error">{{ error }}</p>
    <p class="muted small">每次操作都會寫入下方的稽核紀錄。</p>
  </section>
</template>
