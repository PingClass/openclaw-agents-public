<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { fetchAgents, fetchSummary } from '../api'
import AgentCardView from '../components/AgentCardView.vue'
import GatewayBanner from '../components/GatewayBanner.vue'
import { STUCK_LABELS, formatDuration, formatTime, label } from '../format'
import { useLiveUpdates } from '../live'
import { useResource } from '../resource'

const summary = useResource(fetchSummary)
const agents = useResource(() => fetchAgents('assistant'))

function refresh() {
  void summary.reload()
  void agents.reload()
}
onMounted(refresh)
useLiveUpdates(refresh)

const counts = computed(() => summary.data.value?.counts)
// 「進行中」不含等待確認（兩個數字不重疊）。舊版後端的 running 含等待確認、沒有 active 欄位，這裡扣回來。
const running = computed(() => {
  const c = counts.value
  if (!c) return undefined
  return c.active === undefined ? Math.max(0, c.running - c.waiting_approval) : c.running
})
const tiles = computed(() => [
  { key: 'running', label: '進行中', hint: 'agent 處理中', value: running.value },
  { key: 'waiting_approval', label: '等待確認', hint: '等人決定', value: counts.value?.waiting_approval },
  { key: 'stuck', label: '卡住', hint: '已含在前兩項', value: counts.value?.stuck, alert: (counts.value?.stuck ?? 0) > 0 },
  { key: 'succeeded_today', label: '今日完成', value: counts.value?.succeeded_today },
  { key: 'failed_today', label: '今日失敗', value: counts.value?.failed_today },
])
</script>

<template>
  <div class="page">
    <h1>總覽</h1>
    <GatewayBanner :gateway="summary.data.value?.gateway" />
    <p v-if="summary.error.value" role="alert" class="error">{{ summary.error.value }}</p>

    <!-- ② 卡住清單：最上方、紅色 -->
    <section v-if="summary.data.value?.stuck.length" class="panel stuck-panel" data-testid="stuck-list">
      <h2>卡住（{{ summary.data.value.stuck.length }}）</h2>
      <ul class="rows">
        <li v-for="flow in summary.data.value.stuck" :key="flow.id" class="row stuck-row">
          <div>
            <router-link :to="`/flows/${flow.id}`" class="title">{{ flow.title || flow.id }}</router-link>
            <p class="reason">{{ flow.stuck?.reason }}</p>
          </div>
          <div class="meta">
            <span class="tag danger">{{ label(STUCK_LABELS, flow.stuck?.code) }}</span>
            <span>卡在：{{ flow.current_step_title || flow.current_step }}</span>
            <span v-if="flow.waiting_for">等：{{ flow.waiting_for.name }}</span>
            <span>已等 {{ formatDuration(flow.stuck?.waited_seconds) }}</span>
          </div>
        </li>
      </ul>
    </section>

    <!-- ① 今日統計 -->
    <section class="tiles" data-testid="stats">
      <div v-for="tile in tiles" :key="tile.key" class="tile" :class="{ alert: tile.alert }">
        <span class="tile-label">{{ tile.label }}<small v-if="tile.hint" class="tile-hint">（{{ tile.hint }}）</small></span>
        <strong class="tile-value">{{ tile.value ?? '—' }}</strong>
      </div>
    </section>

    <!-- ③ 等待確認清單 -->
    <section class="panel" data-testid="pending-approvals">
      <h2>等待確認</h2>
      <p v-if="!summary.data.value?.pending_approvals.length" class="muted">目前沒有等待確認的項目</p>
      <ul v-else class="rows">
        <li v-for="item in summary.data.value.pending_approvals" :key="item.approval_id" class="row">
          <div>
            <router-link :to="`/flows/${item.flow_id}`" class="title">{{ item.flow_title || item.flow_id }}</router-link>
            <p>{{ item.question }}</p>
          </div>
          <div class="meta">
            <span>確認人：{{ item.approver?.display_name || '（未指定）' }}</span>
            <span>剩 {{ formatDuration(item.remaining_seconds) }}（{{ formatTime(item.deadline_at) }} 截止）</span>
          </div>
        </li>
      </ul>
    </section>

    <!-- ④ 助理 agent 狀態卡 -->
    <section data-testid="agents">
      <h2><router-link to="/agents" class="h-link">助理 agent</router-link></h2>
      <p v-if="agents.error.value" role="alert" class="error">{{ agents.error.value }}</p>
      <div class="agent-grid">
        <AgentCardView v-for="agent in agents.data.value?.items ?? []" :key="agent.id" :agent="agent" />
      </div>
    </section>
    <p v-if="summary.data.value" class="muted small">資料時間：{{ formatTime(summary.data.value.generated_at) }}</p>
  </div>
</template>
