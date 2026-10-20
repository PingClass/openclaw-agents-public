<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { fetchFlows, type FlowFilters } from '../api'
import StatusBadge from '../components/StatusBadge.vue'
import { FLOW_STATUS_LABELS, FLOW_TYPE_LABELS, formatDuration, formatTime, label } from '../format'
import { useLiveUpdates } from '../live'
import { useResource } from '../resource'

const PAGE_SIZE = 50
const route = useRoute()
const router = useRouter()

const q = (key: string) => (typeof route.query[key] === 'string' ? (route.query[key] as string) : '')

// 篩選條件放在網址上，重新整理或分享連結時保留
const fromQuery = () => ({ type: q('type'), status: q('status'), from: q('from'), to: q('to'), stuck: q('stuck') === '1' })
const form = reactive(fromQuery())
// 上一頁／下一頁改變網址時，表單跟著同步
watch(() => route.query, () => Object.assign(form, fromQuery()))
const page = computed(() => Math.max(0, Number(q('page') || 0) || 0))

const filters = computed<FlowFilters>(() => ({
  type: q('type') || undefined,
  status: q('status') || undefined,
  from: q('from') || undefined,
  to: q('to') || undefined,
  stuck: q('stuck') === '1',
  limit: PAGE_SIZE,
  offset: page.value * PAGE_SIZE,
}))

const flows = useResource(() => fetchFlows(filters.value))
watch(filters, () => void flows.reload(), { immediate: true, deep: true })
useLiveUpdates(() => void flows.reload())

function apply() {
  const query: Record<string, string> = {}
  if (form.type) query.type = form.type
  if (form.status) query.status = form.status
  if (form.from) query.from = form.from
  if (form.to) query.to = form.to
  if (form.stuck) query.stuck = '1'
  void router.push({ name: 'flows', query })
}

function reset() {
  Object.assign(form, { type: '', status: '', from: '', to: '', stuck: false })
  apply()
}

function goPage(next: number) {
  void router.push({ name: 'flows', query: { ...route.query, page: next ? String(next) : undefined } })
}

const total = computed(() => flows.data.value?.total ?? 0)
const lastPage = computed(() => Math.max(0, Math.ceil(total.value / PAGE_SIZE) - 1))
</script>

<template>
  <div class="page">
    <h1>工作流</h1>
    <form class="panel filters" @submit.prevent="apply">
      <label>類型
        <select v-model="form.type" name="type">
          <option value="">全部</option>
          <option v-for="(text, value) in FLOW_TYPE_LABELS" :key="value" :value="value">{{ text }}</option>
        </select>
      </label>
      <label>狀態
        <select v-model="form.status" name="status">
          <option value="">全部</option>
          <option value="running,waiting_approval">進行中＋等待確認</option>
          <option v-for="(text, value) in FLOW_STATUS_LABELS" :key="value" :value="value">{{ text }}</option>
        </select>
      </label>
      <label>從<input v-model="form.from" type="date" name="from" /></label>
      <label>到<input v-model="form.to" type="date" name="to" /></label>
      <label class="check"><input v-model="form.stuck" type="checkbox" name="stuck" />只看卡住的</label>
      <div class="filter-buttons">
        <button type="submit">篩選</button>
        <button type="button" class="ghost" @click="reset">清除</button>
      </div>
    </form>

    <p v-if="flows.error.value" role="alert" class="error">{{ flows.error.value }}</p>
    <p class="muted small">共 {{ total }} 條</p>
    <div class="table-wrap">
      <table class="flows-table">
        <thead>
          <tr><th>流程</th><th>類型</th><th>目前步驟</th><th>等誰</th><th>已經過</th><th>狀態</th><th>建立時間</th></tr>
        </thead>
        <tbody>
          <tr v-for="flow in flows.data.value?.items ?? []" :key="flow.id" :class="{ 'stuck-row': flow.stuck }">
            <td>
              <router-link :to="`/flows/${flow.id}`">{{ flow.title || flow.id }}</router-link>
              <span v-if="flow.demo" class="tag demo" data-testid="demo-tag">示範</span>
              <p v-if="flow.stuck" class="reason small">{{ flow.stuck.reason }}</p>
            </td>
            <td>{{ label(FLOW_TYPE_LABELS, flow.type) }}</td>
            <td>{{ flow.current_step_title || flow.current_step || '—' }}</td>
            <td>{{ flow.waiting_for?.name ?? '—' }}</td>
            <td>{{ formatDuration(flow.elapsed_seconds) }}</td>
            <td><StatusBadge :status="flow.status" /><span v-if="flow.stuck" class="tag danger">卡住</span></td>
            <td>{{ formatTime(flow.created_at) }}</td>
          </tr>
          <tr v-if="flows.data.value && !flows.data.value.items.length">
            <td colspan="7" class="muted">沒有符合條件的流程</td>
          </tr>
        </tbody>
      </table>
    </div>
    <nav v-if="lastPage > 0" class="pager">
      <button type="button" class="ghost" :disabled="page === 0" @click="goPage(page - 1)">上一頁</button>
      <span>{{ page + 1 }} / {{ lastPage + 1 }}</span>
      <button type="button" class="ghost" :disabled="page >= lastPage" @click="goPage(page + 1)">下一頁</button>
    </nav>
  </div>
</template>
