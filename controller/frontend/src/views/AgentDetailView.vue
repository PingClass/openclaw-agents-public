<script setup lang="ts">
// Agent 詳情（計畫 3.7）：這隻 agent 的步驟紀錄（時間、流程、步驟、結果、耗時、錯誤），可依日期與結果篩選。
// 篩選條件放在網址上，重新整理或分享連結時保留。
import { computed, reactive, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { fetchAgent, type AgentStepFilters } from '../api'
import AgentCardView from '../components/AgentCardView.vue'
import GatewayBanner from '../components/GatewayBanner.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { AGENT_RESULT_LABELS, FLOW_TYPE_LABELS, formatDuration, formatTime, label, maskText } from '../format'
import { useLiveUpdates } from '../live'
import { useResource } from '../resource'

const props = defineProps<{ id: string }>()
const route = useRoute()
const router = useRouter()

const q = (key: string) => (typeof route.query[key] === 'string' ? (route.query[key] as string) : '')
const fromQuery = () => ({ result: q('result'), from: q('from'), to: q('to') })
const form = reactive(fromQuery())
watch(() => route.query, () => Object.assign(form, fromQuery()))

const filters = computed<AgentStepFilters>(() => ({
  result: q('result') || undefined,
  from: q('from') || undefined,
  to: q('to') || undefined,
}))

const agent = useResource(() => fetchAgent(props.id, filters.value))
watch([() => props.id, filters], () => void agent.reload(), { immediate: true, deep: true })
useLiveUpdates(() => void agent.reload())

function apply() {
  const query: Record<string, string> = {}
  if (form.result) query.result = form.result
  if (form.from) query.from = form.from
  if (form.to) query.to = form.to
  void router.push({ name: 'agent', params: { id: props.id }, query })
}

function reset() {
  Object.assign(form, { result: '', from: '', to: '' })
  apply()
}

const steps = computed(() => agent.data.value?.steps ?? [])
const filtered = computed(() => Boolean(filters.value.result || filters.value.from || filters.value.to))
</script>

<template>
  <div class="page">
    <router-link to="/agents" class="back">← 助理 agent</router-link>
    <p v-if="agent.error.value" role="alert" class="error">{{ agent.error.value }}</p>
    <template v-if="agent.data.value">
      <h1>{{ agent.data.value.display_name || agent.data.value.id }}</h1>
      <GatewayBanner :gateway="agent.data.value.gateway" />
      <AgentCardView :agent="agent.data.value" class="agent-detail-card" />

      <form class="panel filters" data-testid="agent-filters" @submit.prevent="apply">
        <label>結果
          <select v-model="form.result" name="result">
            <option value="">全部</option>
            <option v-for="(text, value) in AGENT_RESULT_LABELS" :key="value" :value="value">{{ text }}</option>
          </select>
        </label>
        <label>從<input v-model="form.from" type="date" name="from" /></label>
        <label>到<input v-model="form.to" type="date" name="to" /></label>
        <div class="filter-buttons">
          <button type="submit">篩選</button>
          <button type="button" class="ghost" @click="reset">清除</button>
        </div>
      </form>

      <section class="panel" data-testid="agent-steps">
        <h2>步驟紀錄（{{ steps.length }}）</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th>派出時間</th><th>流程</th><th>步驟</th><th>結果</th><th>耗時</th><th>摘要／錯誤</th></tr>
            </thead>
            <tbody>
              <tr v-for="record in steps" :key="record.flow_id + record.step + record.dispatched_at" :class="{ 'failed-row': record.status === 'failed' }">
                <td>{{ formatTime(record.dispatched_at) }}</td>
                <td>
                  <router-link :to="`/flows/${record.flow_id}`">{{ record.flow_title || record.flow_id }}</router-link>
                  <p class="muted small">{{ label(FLOW_TYPE_LABELS, record.flow_type) }}</p>
                </td>
                <td>{{ record.step_title || record.step }}<span v-if="record.attempts > 1" class="muted small">（第 {{ record.attempts }} 次）</span></td>
                <td><StatusBadge :status="record.status" kind="step" /></td>
                <td>{{ formatDuration(record.duration_seconds) }}</td>
                <td>
                  <span v-if="record.summary">{{ maskText(record.summary) }}</span>
                  <p v-if="record.error" class="error small">錯誤：{{ maskText(record.error) }}</p>
                </td>
              </tr>
              <tr v-if="!steps.length">
                <td colspan="6" class="muted">{{ filtered ? '沒有符合條件的步驟紀錄' : '還沒有派給這隻 agent 的步驟' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
    <p v-else-if="agent.loading.value" class="muted">載入中…</p>
  </div>
</template>
