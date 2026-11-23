import { createRouter, createWebHistory } from 'vue-router'

/**
 * Every entry in the sidebar lands on a list or a workspace, never on a bare
 * document - and the ten master and transaction areas share one page rather
 * than being ten copies of it. Ten near-identical pages is how four of them
 * come to disagree about what a status chip means.
 */
const routes = [
  { path: '/', name: 'Overview', component: () => import('@/pages/Overview.vue'),
    meta: { title: 'Overview' } },
  { path: '/export', name: 'Export', component: () => import('@/pages/ExportPage.vue'),
    meta: { title: 'Export to Tally' } },
  { path: '/import', name: 'Import', component: () => import('@/pages/ImportPage.vue'),
    meta: { title: 'Import from Tally' } },
  { path: '/history', name: 'History', component: () => import('@/pages/History.vue'),
    meta: { title: 'Sync History' } },
  { path: '/validation', name: 'Validation', component: () => import('@/pages/Validation.vue'),
    meta: { title: 'Validation & Errors' } },
  { path: '/mapping', name: 'Mapping', component: () => import('@/pages/Mapping.vue'),
    meta: { title: 'Mapping' } },
  { path: '/settings', name: 'Settings', component: () => import('@/pages/Settings.vue'),
    meta: { title: 'Settings' } },
  { path: '/data/:area', name: 'Area', component: () => import('@/pages/AreaDetail.vue'),
    meta: { title: 'Data' } },
  // Anything else goes to the Overview rather than to a blank screen. There is
  // no useful "not found" in an application of eight pages.
  { path: '/:pathMatch(.*)*', redirect: { name: 'Overview' } },
]

export default createRouter({
  history: createWebHistory('/tally-connector'),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})
