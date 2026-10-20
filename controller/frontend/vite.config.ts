import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// 開發時把 /api 轉給本機的 controller 後端；正式環境由後端同源提供 build 出來的靜態檔。
export default defineConfig({
  plugins: [vue()],
  server: { proxy: { '/api': process.env.VITE_API_PROXY_TARGET || 'http://localhost:18090' } },
  test: {
    environment: 'jsdom',
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,vue}'],
      exclude: ['src/main.ts', 'src/env.d.ts'],
      thresholds: { lines: 80 },
    },
  },
})
