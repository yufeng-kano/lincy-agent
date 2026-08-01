import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/monitor' },
    {
      path: '/monitor',
      name: 'monitor',
      component: () => import('@/pages/MonitorDashboard.vue'),
    },
    {
      path: '/monitor/requests',
      name: 'requests',
      component: () => import('@/pages/MonitorRequests.vue'),
    },
    {
      path: '/monitor/context',
      name: 'context',
      component: () => import('@/pages/MonitorContext.vue'),
    },
    {
      path: '/monitor/:id',
      name: 'session-detail',
      component: () => import('@/pages/MonitorSession.vue'),
    },
    {
      path: '/proxy',
      name: 'proxy',
      component: () => import('@/pages/ProxyPage.vue'),
    },
    {
      path: '/chat',
      name: 'chat',
      component: () => import('@/pages/ChatPage.vue'),
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('@/pages/SettingsPlaceholder.vue'),
    },
  ],
})

export default router
