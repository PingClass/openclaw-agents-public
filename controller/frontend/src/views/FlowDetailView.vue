<script setup lang="ts">
import { computed, watch } from 'vue'
import { fetchFlow } from '../api'
import AdminOnly from '../components/AdminOnly.vue'
import FlowActions from '../components/FlowActions.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { demoMode } from '../demo'
import { FLOW_TYPE_LABELS, STUCK_LABELS, formatDuration, formatTime, label, maskText, sanitize } from '../format'
import { useLiveUpdates } from '../live'
import { useResource } from '../resource'
import { isAdmin } from '../session'
import type { ApprovalView, StepView } from '../types'

const props = defineProps<{ id: string }>()
const flow = useResource(() => fetchFlow(props.id))
watch(() => props.id, () => void flow.reload(), { immediate: true })
useLiveUpdates(() => void flow.reload(), {
  filter: (event) => !event.data.flow_id || event.data.flow_id === props.id,
})

const data = computed(() => flow.data.value)
const stuckStep = computed(() => data.value?.steps.find((step) => step.name === data.value?.stuck?.step) ?? null)

const STUCK_HINTS: Record<string, string> = {
  agent_no_report: '先到 Agent 頁確認這隻 agent 與 gateway 是否正常，再按「重試這一步」重新派工。',
  system_error: '先確認 ADM／CRM／HRM 是否恢復，再按「重試這一步」。',
  retries_exhausted: '看下方時間軸的錯誤訊息，排除原因後按「重試這一步」，或取消流程。',
  approval_waiting_too_long: '可以提醒確認者，或按「重送確認通知」產生新連結。',
}

function approvalFor(step: StepView): ApprovalView | undefined {
  const list = data.value?.approvals.filter((approval) => approval.step === step.name) ?? []
  return list[list.length - 1]
}

function assigneeText(step: StepView): string {
  if (step.assignee !== 'human') return step.assignee
  const approver = approvalFor(step)?.approver
  return approver ? `人工確認：${approver.display_name ?? approver.employee_number ?? ''}` : '人工確認'
}

function decisionText(approval: ApprovalView): string {
  if (!approval.decision) return '—'
  return approval.options.find((option) => option.value === approval.decision)?.label ?? approval.decision
}

function detailText(detail: Record<string, unknown>): string {
  const clean = sanitize(detail) as Record<string, unknown>
  return Object.keys(clean).length ? JSON.stringify(clean) : ''
}
</script>

<template>
  <div class="page">
    <router-link :to="demoMode ? '/' : '/flows'" class="back">{{ demoMode ? '← 總覽' : '← 工作流列表' }}</router-link>
    <p v-if="flow.error.value" role="alert" class="error">{{ flow.error.value }}</p>
    <template v-if="data">
      <header class="flow-head">
        <div>
          <p class="eyebrow">
            {{ label(FLOW_TYPE_LABELS, data.type) }}・<span class="mono">{{ data.id }}</span>
            <span v-if="data.demo" class="tag demo" data-testid="demo-tag" title="課堂示範模式：派車確認截止＝拜訪前 5 分鐘、報帳核准 15 分鐘">示範模式</span>
          </p>
          <h1>{{ data.title || data.id }}</h1>
        </div>
        <StatusBadge :status="data.status" />
      </header>

      <section v-if="data.stuck" class="panel stuck-panel" data-testid="flow-stuck">
        <strong>卡住：{{ label(STUCK_LABELS, data.stuck.code) }}</strong>
        <p class="reason">{{ data.stuck.reason }}</p>
        <p class="small">
          已等 {{ formatDuration(data.stuck.waited_seconds) }}
          <template v-if="stuckStep">・卡在「{{ stuckStep.title || stuckStep.name }}」（{{ stuckStep.assignee === 'human' ? '人工確認' : stuckStep.assignee }}）</template>
          <template v-if="stuckStep?.assignee && stuckStep.assignee !== 'human' && stuckStep.assignee !== 'controller'">
            ・<router-link :to="`/agents/${stuckStep.assignee}`">看 {{ stuckStep.assignee }} 的狀態</router-link>
          </template>
        </p>
        <p v-if="stuckStep?.error" class="small">最後的錯誤：{{ maskText(stuckStep.error) }}</p>
        <p v-if="isAdmin && !demoMode && STUCK_HINTS[data.stuck.code]" class="small hint" data-testid="stuck-hint">建議：{{ STUCK_HINTS[data.stuck.code] }}</p>
      </section>

      <!-- ④ 操作按鈕：viewer 看不到；放在卡住原因下方，「看到卡住 → 找到原因 → 重試」不用捲動 -->
      <AdminOnly v-if="!demoMode"><FlowActions :flow="data" @done="flow.reload()" /></AdminOnly>

      <dl v-if="!demoMode" class="kv panel">
        <dt>目前步驟</dt><dd>{{ data.current_step_title || data.current_step || '—' }}</dd>
        <dt>等誰</dt><dd>{{ data.waiting_for?.name ?? '—' }}</dd>
        <dt>已經過</dt><dd>{{ formatDuration(data.elapsed_seconds) }}</dd>
        <dt>啟動者</dt><dd>{{ data.created_by || '—' }}</dd>
        <dt>建立時間</dt><dd>{{ formatTime(data.created_at) }}</dd>
        <dt>結束時間</dt><dd>{{ formatTime(data.finished_at) }}</dd>
      </dl>

      <!-- ① 步驟時間軸 -->
      <section class="panel" data-testid="timeline">
        <h2>步驟時間軸</h2>
        <ol class="timeline">
          <li
            v-for="step in data.steps"
            :key="step.name"
            class="tl-item"
            :class="['tl-' + step.status, { 'tl-stuck': step.stuck, 'tl-current': step.name === data.current_step }]"
            :data-step="step.name"
          >
            <div class="tl-head">
              <strong>{{ step.title || step.name }}</strong>
              <StatusBadge :status="step.status" kind="step" />
            </div>
            <p v-if="step.stuck" class="reason">⚠ {{ step.stuck.reason }}</p>
            <dl class="kv small">
              <dt>執行者</dt><dd>{{ assigneeText(step) }}</dd>
              <dt>開始</dt><dd>{{ formatTime(step.dispatched_at || step.started_at) }}</dd>
              <dt>結束</dt><dd>{{ formatTime(step.finished_at) }}</dd>
              <dt>耗時</dt><dd>{{ formatDuration(step.duration_seconds) }}</dd>
              <dt v-if="step.attempts > 0 && (step.max_attempts > 1 || step.attempts > 1)">嘗試</dt>
              <dd v-if="step.attempts > 0 && (step.max_attempts > 1 || step.attempts > 1)">第 {{ step.attempts }} 次（上限 {{ step.max_attempts }}）</dd>
              <dt v-if="step.deadline_at && !step.finished_at">時限</dt>
              <dd v-if="step.deadline_at && !step.finished_at">{{ formatTime(step.deadline_at) }}</dd>
            </dl>
            <p v-if="step.summary" class="summary">{{ maskText(step.summary) }}</p>
            <p v-if="step.error" class="error small">錯誤：{{ maskText(step.error) }}</p>
          </li>
        </ol>
      </section>

      <!-- 對外訊息：流程送給客戶的回覆（LINE 接上前記在模擬通道） -->
      <section v-if="data.messages?.length" class="panel" data-testid="messages">
        <h2>對外訊息</h2>
        <ul class="messages">
          <li v-for="message in data.messages" :key="message.id">
            <p class="small muted">
              {{ formatTime(message.created_at) }}・{{ message.direction === 'out' ? '送給客戶' : '客戶傳來' }}
              ・{{ message.channel === 'sim' ? '模擬通道' : message.channel }}
              <template v-if="message.conversation_id">・對話 <span class="mono">{{ message.conversation_id }}</span></template>
            </p>
            <p class="summary">{{ maskText(message.body) }}</p>
          </li>
        </ul>
      </section>

      <!-- ② 人工確認紀錄 -->
      <section v-if="!demoMode" class="panel" data-testid="approvals">
        <h2>人工確認紀錄</h2>
        <p v-if="!data.approvals.length" class="muted">沒有人工確認</p>
        <div v-else class="table-wrap">
          <table>
            <thead><tr><th>確認人</th><th>問題</th><th>狀態</th><th>決定</th><th>意見</th><th>時間</th><th>通知次數</th></tr></thead>
            <tbody>
              <tr v-for="approval in data.approvals" :key="approval.id">
                <td>{{ approval.approver?.display_name || '（未指定）' }}<span v-if="approval.approver?.employee_number" class="muted">（{{ approval.approver.employee_number }}）</span></td>
                <td>{{ maskText(approval.question) }}</td>
                <td><StatusBadge :status="approval.status" kind="approval" /></td>
                <td>{{ decisionText(approval) }}</td>
                <td>{{ maskText(approval.comment) || '—' }}</td>
                <td>
                  <template v-if="approval.decided_at">{{ formatTime(approval.decided_at) }}</template>
                  <template v-else-if="approval.status === 'pending'">剩 {{ formatDuration(approval.remaining_seconds) }}</template>
                  <template v-else>{{ formatTime(approval.expires_at) }}</template>
                </td>
                <td>{{ approval.sent_count }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- ③ 稽核紀錄 -->
      <section v-if="!demoMode" class="panel" data-testid="audit">
        <h2>稽核紀錄</h2>
        <div class="table-wrap">
          <table class="audit">
            <thead><tr><th>時間</th><th>執行者</th><th>動作</th><th>內容</th></tr></thead>
            <tbody>
              <tr v-for="entry in data.audit" :key="entry.id">
                <td>{{ formatTime(entry.created_at) }}</td>
                <td class="mono">{{ entry.actor }}</td>
                <td class="mono">{{ entry.action }}</td>
                <td class="mono detail">{{ detailText(entry.detail) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
    <p v-else-if="flow.loading.value" class="muted">載入中…</p>
  </div>
</template>
