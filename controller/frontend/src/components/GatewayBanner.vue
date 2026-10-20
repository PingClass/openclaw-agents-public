<script setup lang="ts">
// OpenClaw gateway 的狀態提示（計畫 3.7「Agent 狀態從哪來」）：離線或頻道異常時顯示在頁面上方。
import { computed } from 'vue'
import { formatTime } from '../format'
import type { GatewayStatus } from '../types'

const props = defineProps<{ gateway: GatewayStatus | null | undefined }>()
const status = computed(() => props.gateway?.status)
const text = computed(() => {
  if (status.value === 'offline') return 'OpenClaw gateway 離線：派工送不出去，agent 不會執行新的步驟。'
  if (status.value === 'degraded') {
    const failing = props.gateway?.failing ?? []
    return `頻道異常${failing.length ? `（${failing.join('、')}）` : ''}：agent 可能收不到或送不出訊息。`
  }
  return ''
})
</script>

<template>
  <p v-if="text" class="gateway-banner" :class="'gw-' + status" role="status" data-testid="gateway-banner">
    <strong>{{ text }}</strong>
    <span v-if="gateway?.checked_at" class="small">（{{ formatTime(gateway.checked_at) }} 檢查）</span>
  </p>
</template>
