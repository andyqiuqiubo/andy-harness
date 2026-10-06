import { createRouter, createWebHistory } from 'vue-router'
import HomeView from '../views/HomeView.vue'
import ChatView from '../views/ChatView.vue'
import SettingsView from '../views/SettingsView.vue'
import InsightsView from '../views/InsightsView.vue'
import EvalLabView from '../views/EvalLabView.vue'
import TemplateHubView from '../views/TemplateHubView.vue'
import WorkflowView from '../views/WorkflowView.vue'
import WorkflowListView from '../views/WorkflowListView.vue'
import DevkitView from '../views/DevkitView.vue'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'home',
      component: HomeView,
    },
    {
      path: '/chat',
      name: 'chat',
      component: ChatView,
    },
    {
      path: '/settings',
      name: 'settings',
      component: SettingsView,
    },
    {
      path: '/insights',
      name: 'insights',
      component: InsightsView,
    },
    {
      path: '/evals',
      name: 'evals',
      component: EvalLabView,
    },
    {
      path: '/templates',
      name: 'templates',
      component: TemplateHubView,
    },
    {
      path: '/workflows',
      name: 'workflows',
      component: WorkflowListView,
    },
    {
      path: '/workflows/new',
      name: 'workflow-new',
      component: WorkflowView,
    },
    {
      path: '/workflows/:id',
      name: 'workflow-edit',
      component: WorkflowView,
    },
    {
      path: '/devkit',
      name: 'devkit',
      component: DevkitView,
    },
  ],
})

export default router
