import { createApp } from 'vue'
import App from './App.vue'
import { initDemoMode } from './demo'
import { createAppRouter } from './router'
import './style.css'

initDemoMode()
createApp(App).use(createAppRouter()).mount('#app')
