import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ExperimentSharedReportView from '@/views/ExperimentSharedReportView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/experiment-shared-report', name: 'experiment-shared-report', component: ExperimentSharedReportView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
