<script setup lang="ts">
import type { AgentCard } from '../types'
import { formatDuration, formatTime, maskText } from '../format'
import StatusBadge from './StatusBadge.vue'

defineProps<{ agent: AgentCard }>()
</script>

<template>
  <article class="agent-card" :class="'agent-' + agent.status" :data-agent="agent.id">
    <header>
      <router-link :to="`/agents/${agent.id}`" class="agent-name">
        <strong><span v-if="agent.emoji" aria-hidden="true">{{ agent.emoji }} </span>{{ agent.display_name || agent.id }}</strong>
      </router-link>
      <StatusBadge :status="agent.status" kind="agent" />
    </header>
    <p class="muted mono">{{ agent.id }}<template v-if="!agent.enabled">・已停用</template></p>
    <ul v-if="agent.current_steps.length" class="current">
      <li v-for="step in agent.current_steps" :key="step.flow_id + step.step" :class="{ overdue: step.overdue }">
        <router-link :to="`/flows/${step.flow_id}`">{{ step.flow_title || step.flow_id }}</router-link>
        ・{{ step.step_title || step.step }}<span v-if="step.overdue">（已逾時）</span>
      </li>
    </ul>
    <p v-else class="muted">目前沒有執行中的步驟</p>
    <dl class="kv small">
      <dt>今日</dt><dd>完成 {{ agent.today.succeeded }}・失敗 {{ agent.today.failed }}</dd>
      <dt>平均耗時</dt><dd>{{ formatDuration(agent.avg_duration_seconds) }}</dd>
      <dt>最後回報</dt><dd>{{ formatTime(agent.last_seen_at) }}</dd>
    </dl>
    <p v-if="agent.last_error" class="error small">最近錯誤（{{ formatTime(agent.last_error_at) }}）：{{ maskText(agent.last_error) }}</p>
  </article>
</template>
