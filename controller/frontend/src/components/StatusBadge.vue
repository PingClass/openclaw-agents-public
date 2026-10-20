<script setup lang="ts">
import { computed } from 'vue'
import { FLOW_STATUS_LABELS, STEP_STATUS_LABELS, AGENT_STATUS_LABELS, APPROVAL_STATUS_LABELS, label } from '../format'

const props = defineProps<{ status: string | null; kind?: 'flow' | 'step' | 'agent' | 'approval' }>()
const maps = { flow: FLOW_STATUS_LABELS, step: STEP_STATUS_LABELS, agent: AGENT_STATUS_LABELS, approval: APPROVAL_STATUS_LABELS }
const text = computed(() => label(maps[props.kind ?? 'flow'], props.status))
</script>

<template>
  <span class="badge" :class="'st-' + (status ?? 'none')">{{ text }}</span>
</template>
