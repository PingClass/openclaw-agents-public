<script setup lang="ts">
// Agent 頁（計畫 3.7）：五隻助理 agent 的狀態卡片，gateway 離線／頻道異常時置頂提示。
import { computed, onMounted } from 'vue'
import { fetchAgents } from '../api'
import AgentCardView from '../components/AgentCardView.vue'
import GatewayBanner from '../components/GatewayBanner.vue'
import { useLiveUpdates } from '../live'
import { useResource } from '../resource'

const agents = useResource(() => fetchAgents('assistant'))
onMounted(() => void agents.reload())
useLiveUpdates(() => void agents.reload())

const items = computed(() => agents.data.value?.items ?? [])
const counts = computed(() => ({
  running: items.value.filter((a) => a.status === 'running').length,
  error: items.value.filter((a) => a.status === 'error').length,
  idle: items.value.filter((a) => a.status === 'idle').length,
}))
</script>

<template>
  <div class="page">
    <h1>助理 agent</h1>
    <GatewayBanner :gateway="agents.data.value?.gateway" />
    <p v-if="agents.error.value" role="alert" class="error">{{ agents.error.value }}</p>
    <p v-if="agents.data.value" class="muted small" data-testid="agent-counts">
      執行中 {{ counts.running }}・異常 {{ counts.error }}・閒置 {{ counts.idle }}。點名字看這隻 agent 的步驟紀錄。
    </p>
    <div class="agent-grid">
      <AgentCardView v-for="agent in items" :key="agent.id" :agent="agent" />
    </div>
    <p v-if="agents.data.value && !items.length" class="muted">還沒有助理 agent（請先執行 <code>flow-controller seed-agents</code>）</p>
  </div>
</template>
