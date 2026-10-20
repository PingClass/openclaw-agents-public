<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '../api'
import { signIn } from '../session'

const route = useRoute()
const router = useRouter()
const username = ref('')
const password = ref('')
const error = ref('')
const busy = ref(false)

function safeNext(): string {
  const next = typeof route.query.next === 'string' ? route.query.next : '/'
  // 只允許站內路徑，避免被導到外部網址
  return next.startsWith('/') && !next.startsWith('//') ? next : '/'
}

async function submit() {
  error.value = ''
  busy.value = true
  try {
    await signIn(username.value, password.value)
    password.value = ''
    await router.push(safeNext())
  } catch (err) {
    error.value = err instanceof ApiError && err.status === 401 ? '帳號或密碼錯誤。' : '登入失敗，請稍後再試。'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <form class="login-card" @submit.prevent="submit">
      <p class="eyebrow">Workflow Controller</p>
      <h1>工作流控制台</h1>
      <label>帳號<input v-model="username" required autocomplete="username" /></label>
      <label>密碼<input v-model="password" required type="password" autocomplete="current-password" /></label>
      <p v-if="error" role="alert" class="error">{{ error }}</p>
      <button type="submit" :disabled="busy">{{ busy ? '登入中…' : '登入' }}</button>
    </form>
  </main>
</template>
