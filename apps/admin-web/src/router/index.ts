import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { getSession, type AdminRole } from '@/stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    title?: string
    public?: boolean
    roles?: AdminRole[]
    stage?: string
  }
}

interface AdminPage {
  name: string
  path: string
  title: string
  stage: string
  roles?: AdminRole[]
}

const pages: AdminPage[] = [
  { name: 'dashboard', path: 'dashboard', title: '仪表盘', stage: 'P1 已接入运行与审计事实，P7 完善报表' },
  { name: 'agents', path: 'agents', title: 'Agent 配置与管理', stage: 'P7 Agent 配置台（配置/Skill/MCP 管理）', roles: ['admin'] },
  {
    name: 'mcp-servers',
    path: 'mcp-servers',
    title: 'MCP 服务器',
    stage: 'P7 MCP 服务器管理（CRUD + 启停）',
    roles: ['admin', 'manager'],
  },
  {
    name: 'conversations',
    path: 'conversations',
    title: '会话监控与质检',
    stage: 'P7 会话监控（列表/详情/质检标签 CRUD）',
    roles: ['admin', 'manager', 'operator'],
  },
  {
    name: 'reports',
    path: 'reports',
    title: '数据报表',
    stage: 'P7 报表：终态分布、解决率、转人工率与缓存命中率等 Agent 性能指标',
    roles: ['admin', 'manager'],
  },
  { name: 'health', path: 'health', title: '运行健康面板', stage: 'P7 健康面板：Run 终态分布、TTFT、缓存命中率趋势', roles: ['admin', 'manager'] },
  { name: 'logs', path: 'logs', title: '智能体日志', stage: 'P7 日志检索（审计读接口 500 条 + truncated）', roles: ['admin', 'manager'] },
  { name: 'data', path: 'data', title: '酒店基础数据', stage: 'P7 基础数据增强（搜索/设施/房态快捷/酒店配置）', roles: ['admin', 'manager'] },
  { name: 'memories', path: 'memories', title: '记忆管理', stage: 'P7 记忆查看/敏感标记/软删/恢复', roles: ['admin', 'manager'] },
  {
    name: 'handoff',
    path: 'handoff',
    title: '人工接线',
    stage: 'P7 人工工作台（接单/关单/客服回复/对话面板）',
    roles: ['admin', 'manager', 'operator'],
  },
  {
    name: 'eval-agent',
    path: 'eval-agent',
    title: '智能体评估',
    stage: 'P8 智能体评估：Langfuse Dataset 驱动端到端实验',
    roles: ['admin', 'manager'],
  },
  {
    name: 'eval-kb',
    path: 'eval-kb',
    title: '知识库评估',
    stage: 'P8 知识库评估回归：评估集扩充、基线作废与回归报告',
    roles: ['admin', 'manager'],
  },
  {
    name: 'quality-scorecard',
    path: 'quality-scorecard',
    title: '数据质量评分卡',
    stage: 'P8 评分卡：覆盖率/新鲜度/正确性/一致性/召回质量五维评分',
    roles: ['admin', 'manager'],
  },
  {
    name: 'data-pipeline',
    path: 'data-pipeline',
    title: '数据管道',
    stage: 'P8 数据中台：清洗规则管理、管道触发、运行记录',
    roles: ['admin', 'manager'],
  },
]

export const ADMIN_MENU = pages.map((page) => ({
  name: page.name,
  title: page.title,
  roles: page.roles,
}))

const children: RouteRecordRaw[] = pages.map((page) => ({
  path: page.path,
  name: page.name,
  component:
    page.name === 'dashboard'
      ? () => import('@/views/DashboardView.vue')
      : page.name === 'data'
        ? () => import('@/views/DataView.vue')
        : page.name === 'reports'
          ? () => import('@/views/ReportsView.vue')
          : page.name === 'logs'
            ? () => import('@/views/LogsView.vue')
            : page.name === 'health'
              ? () => import('@/views/HealthView.vue')
              : page.name === 'agents'
                ? () => import('@/views/AgentConfigView.vue')
                : page.name === 'mcp-servers'
                ? () => import('@/views/McpServersView.vue')
                : page.name === 'conversations'
                ? () => import('@/views/ConversationsView.vue')
                : page.name === 'handoff'
                ? () => import('@/views/HandoffView.vue')
                : page.name === 'memories'
                ? () => import('@/views/MemoriesView.vue')
                : page.name === 'eval-agent'
                ? () => import('@/views/AgentEvalView.vue')
                : page.name === 'eval-kb'
                ? () => import('@/views/KbEvalView.vue')
                : page.name === 'quality-scorecard'
                ? () => import('@/views/QualityScorecardView.vue')
                : page.name === 'data-pipeline'
                ? () => import('@/views/DataPipelineView.vue')
                : () => import('@/views/PlaceholderView.vue'),
  meta: { title: page.title, roles: page.roles, stage: page.stage },
}))

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { title: '登录', public: true },
    },
    {
      path: '/',
      component: () => import('@/layouts/AdminLayout.vue'),
      redirect: '/dashboard',
      children,
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: () => import('@/views/NotFoundView.vue'),
      meta: { title: '页面不存在', public: true },
    },
  ],
})

router.beforeEach((to) => {
  const session = getSession()
  if (!to.meta.public && !session) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && session) {
    return { path: '/dashboard' }
  }
  const roles = to.meta.roles
  if (roles && session && !roles.includes(session.role)) {
    return { path: '/dashboard' }
  }
  return true
})
