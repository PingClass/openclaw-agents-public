import { createRouter, createWebHistory, type RouteRecordRaw, type RouterHistory } from 'vue-router'
import { applyDemoQuery } from './demo'
import { loadMe } from './session'
import LoginView from './views/LoginView.vue'
import OverviewView from './views/OverviewView.vue'
import FlowsView from './views/FlowsView.vue'
import FlowDetailView from './views/FlowDetailView.vue'
import ApprovalView from './views/ApprovalView.vue'
import InboxView from './views/InboxView.vue'
import AgentsView from './views/AgentsView.vue'
import AgentDetailView from './views/AgentDetailView.vue'
import NotFoundView from './views/NotFoundView.vue'

declare module 'vue-router' {
  interface RouteMeta {
    /** 免登入（登入頁、一次性確認頁） */
    public?: boolean
    /** 不顯示站台的導覽列（確認頁給手機用） */
    bare?: boolean
    title?: string
  }
}

export const routes: RouteRecordRaw[] = [
  { path: '/login', name: 'login', component: LoginView, meta: { public: true, bare: true, title: '登入' } },
  { path: '/', name: 'overview', component: OverviewView, meta: { title: '總覽' } },
  { path: '/flows', name: 'flows', component: FlowsView, meta: { title: '工作流' } },
  { path: '/flows/:id', name: 'flow', component: FlowDetailView, props: true, meta: { title: '工作流詳情' } },
  { path: '/agents', name: 'agents', component: AgentsView, meta: { title: 'Agent' } },
  { path: '/agents/:id', name: 'agent', component: AgentDetailView, props: true, meta: { title: 'Agent 詳情' } },
  { path: '/inbox', name: 'inbox', component: InboxView, meta: { title: '通知收件匣' } },
  {
    path: '/approvals/:id',
    name: 'approval',
    component: ApprovalView,
    props: (route) => ({ id: String(route.params.id), token: String(route.query.token ?? '') }),
    meta: { public: true, bare: true, title: '確認' },
  },
  { path: '/:pathMatch(.*)*', name: 'not-found', component: NotFoundView, meta: { title: '找不到頁面' } },
]

export function createAppRouter(history: RouterHistory = createWebHistory()) {
  const router = createRouter({ history, routes })
  router.beforeEach(async (to) => {
    // ?demo=1／?demo=0 切換示範模式（記在 localStorage）
    applyDemoQuery(to.query.demo)
    if (to.meta.public) return true
    const me = await loadMe()
    if (!me) return { name: 'login', query: { next: to.fullPath } }
    return true
  })
  router.afterEach((to) => {
    if (typeof document !== 'undefined') document.title = `${to.meta.title ?? ''}｜工作流控制台`
  })
  return router
}
