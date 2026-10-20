<script setup lang="ts">
import { computed, watchEffect } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { demoMode, setDemoMode } from './demo'
import { liveMode } from './live'
import { session, signOut } from './session'

const route = useRoute()
const router = useRouter()
const bare = computed(() => Boolean(route.meta.bare))
const liveText = computed(() =>
  liveMode.value === 'sse' ? '即時' : liveMode.value === 'polling' ? '輪詢中（每 10 秒）' : '連線中',
)

// 示範模式：根元素加 class，整站以 rem 為單位的字一起放大
watchEffect(() => {
  if (typeof document !== 'undefined') document.documentElement.classList.toggle('demo-mode', demoMode.value && !bare.value)
})

async function toggleDemo() {
  setDemoMode(!demoMode.value)
  // 網址上的 demo 參數拿掉，避免重新整理時又被切回去
  if (route.query.demo !== undefined) {
    const query = { ...route.query }
    delete query.demo
    await router.replace({ query })
  }
}

async function logoutAndLeave() {
  await signOut()
  await router.push({ name: 'login' })
}
</script>

<template>
  <router-view v-if="bare" />
  <div v-else class="shell" :class="{ demo: demoMode }">
    <header class="topbar">
      <router-link to="/" class="brand">工作流控制台</router-link>
      <nav>
        <router-link to="/" exact-active-class="active">總覽</router-link>
        <template v-if="!demoMode">
          <router-link to="/flows" active-class="active">工作流</router-link>
          <router-link to="/agents" active-class="active">Agent</router-link>
          <router-link to="/inbox" active-class="active">收件匣</router-link>
        </template>
      </nav>
      <div class="who">
        <span class="live" :class="liveMode" :title="'更新方式：' + liveText">{{ liveText }}</span>
        <button
          type="button"
          class="ghost demo-toggle"
          data-testid="demo-toggle"
          :aria-pressed="demoMode"
          @click="toggleDemo"
        >
          {{ demoMode ? '結束示範' : '示範模式' }}
        </button>
        <template v-if="!demoMode">
          <span v-if="session.me" class="user">
            {{ session.me.display_name || session.me.username }}
            <span class="role-tag">{{ session.me.role === 'admin' ? 'admin' : '唯讀' }}</span>
          </span>
          <button v-if="session.me" class="ghost" type="button" data-testid="logout" @click="logoutAndLeave">登出</button>
        </template>
      </div>
    </header>
    <main class="content"><router-view /></main>
  </div>
</template>
