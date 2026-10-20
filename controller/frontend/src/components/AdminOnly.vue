<script setup lang="ts">
// 寫入操作的外框：只有 admin 會渲染內容，viewer 連按鈕的 DOM 都不會出現（計畫 7「前端」）。
import { isAdmin } from '../session'
</script>

<template>
  <slot v-if="isAdmin" />
</template>
