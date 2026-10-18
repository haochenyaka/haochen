<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch, type Component } from 'vue'
import { useRouter } from 'vue-router'
import {
  ApiOutlined,
  ArrowRightOutlined,
  AuditOutlined,
  CheckCircleOutlined,
  CustomerServiceOutlined,
  FileTextOutlined,
  MessageOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons-vue'
import {
  fetchAlerts,
  fetchRunStatusDistribution,
  type AlertsModel,
  type RunStatusDistributionModel,
} from '@/api/health'
import { fetchReadiness, type ReadinessPayload } from '@/api/system'
import {
  fetchHotQuestions,
  fetchOverview,
  fetchReceptionSeries,
  type DailyCountModel,
  type NameCount,
  type OverviewResponse,
  type ReportWindow,
} from '@/api/report'
import { getLatestScorecard, type ScorecardResponse } from '@/api/quality'
import { listKbReports, type KbReportItem } from '@/api/kbEval'
import { listReports, type ReportItem } from '@/api/eval'
import { listPendingApprovals } from '@/api/approval'
import {
  listConversations,
  listHandoffs,
  type ConversationSummary,
  type HandoffSummary,
} from '@/api/conversation'
import {
  getPipelineStats,
  listRuns,
  type PipelineRun,
  type PipelineStats,
} from '@/api/dataPipeline'
import { EmptyState, SectionCard, StatCard, StatGrid, StatusTag } from '@/components'
import ChartCard from '@/components/chart/ChartCard.vue'
import BaseChart from '@/components/chart/BaseChart.vue'
import { CHART_INK, CHART_PALETTE } from '@/components/chart/echarts'
import { ADMIN_MENU } from '@/router'
import { useAuthState } from '@/stores/auth'
import { resolveNavIcon } from '@/theme/navIcons'

type RangeKey = 'today' | '7d' | '30d'
type Tone = 'success' | 'warning' | 'danger' | 'info' | 'neutral'
type TrendType = 'up' | 'down' | 'neutral'

interface Trend {
  text: string
  type: TrendType
}

interface KpiItem {
  key: string
  title: string
  value: string
  suffix?: string
  icon: Component
  route: string
  trend?: Trend
}

interface WorkItem {
  key: string
  title: string
  count: number
  hint: string
  route: string
  icon: Component
  tone: Tone
}

interface QualityDimension {
  key: string
  label: string
  score: number
  count: number
  actionable: number
}

interface EvalHighlight {
  key: string
  kind: string
  title: string
  metric: string
  createdAt: string
  route: string
}

const RANGE_OPTIONS: Array<{ label: string; value: RangeKey }> = [
  { label: '今日', value: 'today' },
  { label: '近 7 天', value: '7d' },
  { label: '近 30 天', value: '30d' },
]

const RANGE_DAYS: Record<RangeKey, number> = { today: 1, '7d': 7, '30d': 30 }

const SERVICE_LABELS: Record<string, string> = {
  postgres: 'PostgreSQL',
  redis: 'Redis',
  milvus: '向量库',
  knowledge: '知识库',
  llm: '模型服务',
  database: '数据库',
}

const PIPELINE_STATUS_MAP: Record<string, 'success' | 'warning' | 'error' | 'processing' | 'default'> = {
  pending: 'default',
  running: 'processing',
  completed: 'success',
  failed: 'error',
}

const PIPELINE_STATUS_LABELS: Record<string, string> = {
  pending: '等待中',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
}

const HANDOFF_LABELS: Record<string, string> = {
  pending: '待接线',
  accepted: '处理中',
  closed: '已关闭',
}

const router = useRouter()
const { session } = useAuthState()
const canManage = computed(() => session?.role === 'admin' || session?.role === 'manager')
const displayName = computed(() => session?.displayName || session?.username || '')

const range = ref<RangeKey>('7d')
const loading = ref(true)
const refreshing = ref(false)
const lastUpdated = ref<Date | null>(null)
const activityTab = ref('conversations')

const readiness = ref<ReadinessPayload | null>(null)
const alerts = ref<AlertsModel | null>(null)
const overview = ref<OverviewResponse | null>(null)
const previousOverview = ref<OverviewResponse | null>(null)
const series = ref<DailyCountModel[]>([])
const runStatus = ref<RunStatusDistributionModel | null>(null)
const hotKeywords = ref<NameCount[]>([])
const pendingApprovals = ref(0)
const pendingHandoffs = ref(0)
const recentHandoffs = ref<HandoffSummary[]>([])
const pendingConversations = ref(0)
const conversationTotal = ref(0)
const recentConversations = ref<ConversationSummary[]>([])
const pipelineStats = ref<PipelineStats | null>(null)
const recentRuns = ref<PipelineRun[]>([])
const scorecard = ref<ScorecardResponse | null>(null)
const agentReports = ref<ReportItem[]>([])
const kbReports = ref<KbReportItem[]>([])

const errors = reactive<Record<string, string>>({})

function errorMessage(error: unknown): string {
  return (error as { message?: string }).message || '加载失败'
}

function markError(key: string, error: unknown): void {
  errors[key] = errorMessage(error)
}

function resetErrors(): void {
  for (const key of Object.keys(errors)) delete errors[key]
}

function buildWindow(key: RangeKey): { current: ReportWindow; previous: ReportWindow } {
  const end = new Date()
  const days = RANGE_DAYS[key]
  if (key === 'today') {
    const start = new Date(end)
    start.setHours(0, 0, 0, 0)
    if (start.getTime() >= end.getTime()) {
      start.setTime(end.getTime() - 60_000)
    }
    const previousEnd = new Date(start)
    const previousStart = new Date(previousEnd)
    previousStart.setDate(previousStart.getDate() - 1)
    return {
      current: { start: start.toISOString(), end: end.toISOString() },
      previous: { start: previousStart.toISOString(), end: previousEnd.toISOString() },
    }
  }
  const start = new Date(end.getTime() - days * 86_400_000)
  const previousEnd = new Date(start)
  const previousStart = new Date(previousEnd.getTime() - days * 86_400_000)
  return {
    current: { start: start.toISOString(), end: end.toISOString() },
    previous: { start: previousStart.toISOString(), end: previousEnd.toISOString() },
  }
}

function seriesWindow(): ReportWindow {
  const end = new Date()
  const start = new Date(end.getTime() - 14 * 86_400_000)
  return { start: start.toISOString(), end: end.toISOString() }
}

async function loadReadiness(): Promise<void> {
  delete errors.readiness
  try {
    readiness.value = await fetchReadiness()
  } catch (error) {
    markError('readiness', error)
  }
}

async function loadAlerts(): Promise<void> {
  delete errors.alerts
  try {
    alerts.value = await fetchAlerts()
  } catch (error) {
    markError('alerts', error)
  }
}

async function loadOverview(): Promise<void> {
  delete errors.overview
  const window = buildWindow(range.value)
  try {
    const [current, previous] = await Promise.allSettled([
      fetchOverview(window.current),
      fetchOverview(window.previous),
    ])
    if (previous.status === 'fulfilled') {
      previousOverview.value = previous.value
    }
    if (current.status === 'rejected') {
      throw current.reason
    }
    overview.value = current.value
  } catch (error) {
    markError('overview', error)
  }
}

async function loadSeries(): Promise<void> {
  delete errors.series
  try {
    series.value = (await fetchReceptionSeries(seriesWindow())).series
  } catch (error) {
    markError('series', error)
  }
}

async function loadRunStatus(): Promise<void> {
  delete errors.runStatus
  try {
    runStatus.value = await fetchRunStatusDistribution(buildWindow(range.value).current)
  } catch (error) {
    markError('runStatus', error)
  }
}

async function loadHotQuestions(): Promise<void> {
  delete errors.hot
  try {
    const response = await fetchHotQuestions({ ...buildWindow(range.value).current, limit: 8 })
    hotKeywords.value = response.top_keywords
  } catch (error) {
    markError('hot', error)
  }
}

async function loadApprovals(): Promise<void> {
  delete errors.approvals
  try {
    pendingApprovals.value = (await listPendingApprovals()).length
  } catch (error) {
    markError('approvals', error)
  }
}

async function loadConversations(): Promise<void> {
  delete errors.handoff
  delete errors.conversations
  const [handoffs, conversations] = await Promise.allSettled([
    listHandoffs({ status: 'pending', limit: 5 }),
    listConversations({ limit: 5 }),
  ])
  if (handoffs.status === 'fulfilled') {
    pendingHandoffs.value = handoffs.value.total
    recentHandoffs.value = handoffs.value.items
  } else {
    markError('handoff', handoffs.reason)
  }
  if (conversations.status === 'fulfilled') {
    conversationTotal.value = conversations.value.total
    recentConversations.value = conversations.value.items
  } else {
    markError('conversations', conversations.reason)
  }
  if (!canManage.value) {
    try {
      pendingConversations.value = (
        await listConversations({ status: 'handoff_pending', limit: 1 })
      ).total
    } catch {
      // 客服端次要指标，失败不阻塞其他区块
    }
  }
}

async function loadPipeline(): Promise<void> {
  delete errors.pipeline
  const [stats, runs] = await Promise.allSettled([getPipelineStats(), listRuns({ limit: 5 })])
  if (stats.status === 'fulfilled') {
    pipelineStats.value = stats.value
  }
  if (runs.status === 'fulfilled') {
    recentRuns.value = runs.value.runs
  }
  if (stats.status === 'rejected' && runs.status === 'rejected') {
    markError('pipeline', stats.reason)
  }
}

async function loadQuality(): Promise<void> {
  delete errors.quality
  try {
    scorecard.value = await getLatestScorecard()
  } catch (error) {
    markError('quality', error)
  }
}

async function loadEvals(): Promise<void> {
  delete errors.eval
  const [agent, kb] = await Promise.allSettled([listReports(3), listKbReports(5)])
  if (agent.status === 'fulfilled') {
    agentReports.value = agent.value
  }
  if (kb.status === 'fulfilled') {
    kbReports.value = kb.value.filter((item) => !item.superseded_by)
  }
  if (agent.status === 'rejected' && kb.status === 'rejected') {
    markError('eval', agent.reason)
  }
}

type LoadTask = () => Promise<void>

/**
 * 限制并发加载：本地与默认部署的数据库连接池较小，仪表盘一次性并发
 * 十几个查询会瞬时占满连接池，反而触发自身告警。
 */
async function runLimited(tasks: LoadTask[], limit: number): Promise<void> {
  const queue = [...tasks]
  const workers = Array.from({ length: Math.min(limit, queue.length) }, async () => {
    for (;;) {
      const task = queue.shift()
      if (!task) return
      await task()
    }
  })
  await Promise.all(workers)
}

async function loadAll(options?: { silent?: boolean }): Promise<void> {
  if (options?.silent) {
    refreshing.value = true
  } else {
    loading.value = true
  }
  resetErrors()

  const tasks: LoadTask[] = [loadReadiness, loadConversations]
  if (canManage.value) {
    // 告警快照单独先取，避免并发查询把连接池瞬时占满造成误报。
    await loadAlerts()
    tasks.push(
      loadOverview,
      loadSeries,
      loadRunStatus,
      loadHotQuestions,
      loadApprovals,
      loadPipeline,
      loadQuality,
      loadEvals,
    )
  }

  await runLimited(tasks, 2)
  lastUpdated.value = new Date()
  loading.value = false
  refreshing.value = false
}

async function refreshRange(): Promise<void> {
  if (!canManage.value) return
  refreshing.value = true
  await Promise.all([loadOverview(), loadRunStatus(), loadHotQuestions()])
  refreshing.value = false
  lastUpdated.value = new Date()
}

watch(range, () => {
  void refreshRange()
})

onMounted(() => {
  void loadAll()
})

function refresh(): void {
  void loadAll({ silent: true })
}

function go(name: string): void {
  void router.push({ name })
}

function fmtNum(value: number | null | undefined): string {
  if (value == null) return '—'
  return new Intl.NumberFormat('zh-CN').format(Math.round(value))
}

function fmtPct(value: number | null | undefined, digits = 1): string {
  if (value == null) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

function pctParts(value: number | null | undefined): { value: string; suffix: string } {
  if (value == null) return { value: '—', suffix: '' }
  return { value: (value * 100).toFixed(1), suffix: '%' }
}

function countTrend(current: number | null | undefined, previous: number | null | undefined): Trend | undefined {
  if (current == null || previous == null) return undefined
  if (previous <= 0) return { text: '暂无上期对比', type: 'neutral' }
  const diff = ((current - previous) / previous) * 100
  const sign = diff >= 0 ? '+' : ''
  return { text: `较上期 ${sign}${diff.toFixed(1)}%`, type: 'neutral' }
}

function rateTrend(
  current: number | null | undefined,
  previous: number | null | undefined,
  lowerIsBetter = false,
): Trend | undefined {
  if (current == null || previous == null) return undefined
  const diff = (current - previous) * 100
  const sign = diff >= 0 ? '+' : ''
  const good = lowerIsBetter ? diff < 0 : diff > 0
  const bad = lowerIsBetter ? diff > 0 : diff < 0
  return {
    text: `较上期 ${sign}${diff.toFixed(1)}pp`,
    type: diff === 0 ? 'neutral' : good ? 'up' : bad ? 'down' : 'neutral',
  }
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const month = date.getMonth() + 1
  const day = date.getDate()
  const hour = String(date.getHours()).padStart(2, '0')
  const minute = String(date.getMinutes()).padStart(2, '0')
  return `${month}/${day} ${hour}:${minute}`
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

function datasetName(path: string): string {
  return path.split(/[\\/]/).pop() || path
}

function progressColor(score: number): string {
  if (score >= 0.85) return '#2E9E5B'
  if (score >= 0.7) return '#D98A1F'
  return '#D03050'
}

const greeting = computed(() => {
  const hour = new Date().getHours()
  if (hour < 6) return '凌晨好'
  if (hour < 12) return '上午好'
  if (hour < 14) return '中午好'
  if (hour < 18) return '下午好'
  return '晚上好'
})

const dateLabel = computed(() =>
  new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    weekday: 'long',
  }).format(new Date()),
)

const updatedLabel = computed(() =>
  lastUpdated.value
    ? lastUpdated.value.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    : '—',
)

const systemStatus = computed<{ text: string; tone: Tone }>(() => {
  if (errors.readiness) return { text: '服务不可达', tone: 'danger' }
  if (!readiness.value) return { text: '检测中', tone: 'neutral' }
  if (readiness.value.status !== 'ready') return { text: '服务降级', tone: 'warning' }
  if (canManage.value && alerts.value?.status === 'critical') {
    return { text: '存在严重告警', tone: 'danger' }
  }
  if (canManage.value && alerts.value?.status === 'warning') {
    return { text: '存在告警', tone: 'warning' }
  }
  return { text: '系统正常', tone: 'success' }
})

const serviceChecks = computed(() =>
  Object.entries(readiness.value?.checks ?? {}).map(([key, value]) => ({
    key,
    label: SERVICE_LABELS[key] ?? key,
    value,
    ok: value === 'ok',
  })),
)

const triggeredAlerts = computed(() => (alerts.value?.rules ?? []).filter((rule) => rule.triggered))

const scorecardDimensions = computed<QualityDimension[]>(() => {
  const value = scorecard.value
  if (!value) return []
  return [
    { key: 'coverage', label: '覆盖率', score: value.coverage.score, count: value.coverage.count, actionable: value.coverage.actionable },
    { key: 'freshness', label: '新鲜度', score: value.freshness.score, count: value.freshness.count, actionable: value.freshness.actionable },
    { key: 'correctness', label: '正确性', score: value.correctness.score, count: value.correctness.count, actionable: value.correctness.actionable },
    { key: 'consistency', label: '一致性', score: value.consistency.score, count: value.consistency.count, actionable: value.consistency.actionable },
    { key: 'recall_quality', label: '召回质量', score: value.recall_quality.score, count: value.recall_quality.count, actionable: value.recall_quality.actionable },
  ]
})

const scorecardActionable = computed(() =>
  scorecardDimensions.value.reduce((total, dimension) => total + dimension.actionable, 0),
)

const latestHandoffReason = computed(() => recentHandoffs.value[0]?.reason || '')

const pendingTotal = computed(() => {
  if (!canManage.value) {
    return pendingHandoffs.value + pendingConversations.value
  }
  const failed = pipelineStats.value?.status_counts?.failed ?? 0
  return pendingApprovals.value + pendingHandoffs.value + failed + scorecardActionable.value
})

const heroSubtitle = computed(() => {
  if (loading.value) return '正在加载运营数据…'
  if (errors.readiness) return '服务状态暂不可达，以下数据可能不完整。'
  if (pendingTotal.value > 0) {
    if (!canManage.value) {
      return `当前有 ${pendingTotal.value} 项待处理事项，建议优先处理接线工单与挂起会话。`
    }
    return `当前有 ${pendingTotal.value} 项待处理事项，建议优先处理待审批与接线工单。`
  }
  return '当前没有待处理事项，各项运营指标运行平稳。'
})

const kpis = computed<KpiItem[]>(() => {
  if (!canManage.value) {
    return [
      {
        key: 'handoff',
        title: '待接线工单',
        value: fmtNum(pendingHandoffs.value),
        icon: CustomerServiceOutlined,
        route: 'handoff',
      },
      {
        key: 'pending-conversations',
        title: '待处理会话',
        value: fmtNum(pendingConversations.value),
        icon: MessageOutlined,
        route: 'conversations',
      },
      {
        key: 'total-conversations',
        title: '会话总数',
        value: fmtNum(conversationTotal.value),
        icon: FileTextOutlined,
        route: 'conversations',
      },
    ]
  }

  const resolution = pctParts(overview.value?.resolution.first_resolution_rate)
  const handoff = pctParts(overview.value?.handoff.rate)
  return [
    {
      key: 'threads',
      title: '会话量',
      value: fmtNum(overview.value?.reception.threads),
      icon: MessageOutlined,
      route: 'reports',
      trend: countTrend(overview.value?.reception.threads, previousOverview.value?.reception.threads),
    },
    {
      key: 'runs',
      title: '运行次数',
      value: fmtNum(overview.value?.reception.runs),
      icon: ThunderboltOutlined,
      route: 'health',
      trend: countTrend(overview.value?.reception.runs, previousOverview.value?.reception.runs),
    },
    {
      key: 'resolution',
      title: '一次解决率',
      value: resolution.value,
      suffix: resolution.suffix,
      icon: CheckCircleOutlined,
      route: 'reports',
      trend: rateTrend(
        overview.value?.resolution.first_resolution_rate,
        previousOverview.value?.resolution.first_resolution_rate,
      ),
    },
    {
      key: 'handoff-rate',
      title: '转人工率',
      value: handoff.value,
      suffix: handoff.suffix,
      icon: CustomerServiceOutlined,
      route: 'handoff',
      trend: rateTrend(overview.value?.handoff.rate, previousOverview.value?.handoff.rate, true),
    },
  ]
})

const kpiColumns = computed(() => (canManage.value ? 4 : 3))

const workItems = computed<WorkItem[]>(() => {
  if (!canManage.value) {
    return [
      {
        key: 'handoff',
        title: '待接线工单',
        count: pendingHandoffs.value,
        hint: pendingHandoffs.value ? latestHandoffReason.value || '客人等待人工客服接入' : '暂无待接线工单',
        route: 'handoff',
        icon: CustomerServiceOutlined,
        tone: pendingHandoffs.value ? 'warning' : 'success',
      },
      {
        key: 'conversations',
        title: '待处理会话',
        count: pendingConversations.value,
        hint: pendingConversations.value ? '会话已挂起，等待接线处理' : '暂无挂起会话',
        route: 'conversations',
        icon: MessageOutlined,
        tone: pendingConversations.value ? 'warning' : 'success',
      },
    ]
  }

  const failed = pipelineStats.value?.status_counts?.failed ?? 0
  const running = pipelineStats.value?.status_counts?.running ?? 0
  const worst = [...scorecardDimensions.value].sort((a, b) => b.actionable - a.actionable)[0]
  return [
    {
      key: 'approvals',
      title: '待审批工具调用',
      count: pendingApprovals.value,
      hint: pendingApprovals.value ? '危险工具调用等待审批' : '暂无待审批',
      route: 'approvals',
      icon: SafetyCertificateOutlined,
      tone: pendingApprovals.value ? 'warning' : 'success',
    },
    {
      key: 'handoff',
      title: '待接线工单',
      count: pendingHandoffs.value,
      hint: pendingHandoffs.value ? latestHandoffReason.value || '客人等待人工客服接入' : '暂无待接线工单',
      route: 'handoff',
      icon: CustomerServiceOutlined,
      tone: pendingHandoffs.value ? 'warning' : 'success',
    },
    {
      key: 'pipeline',
      title: '失败数据管道',
      count: failed,
      hint: failed ? `${running} 个运行中，建议尽快排查` : running ? `${running} 个管道运行中` : '管道运行正常',
      route: 'data-pipeline',
      icon: ApiOutlined,
      tone: failed ? 'danger' : running ? 'info' : 'success',
    },
    {
      key: 'quality',
      title: '质量待治理项',
      count: scorecardActionable.value,
      hint: worst && worst.actionable > 0 ? `${worst.label} ${worst.actionable} 项` : '五维指标健康',
      route: 'quality-scorecard',
      icon: AuditOutlined,
      tone: scorecardActionable.value ? 'warning' : 'success',
    },
  ]
})

const evalHighlights = computed<EvalHighlight[]>(() => {
  const agent = agentReports.value.map((report) => ({
    key: `agent-${report.id}`,
    kind: '智能体',
    title: datasetName(report.dataset_path),
    metric: `任务成功率 ${fmtPct(report.metrics.task_success_rate)} · 工具准确率 ${fmtPct(report.metrics.avg_tool_accuracy)}`,
    createdAt: report.created_at,
    route: 'eval-agent',
  }))
  const kb = kbReports.value.map((report) => ({
    key: `kb-${report.id}`,
    kind: '知识库',
    title: datasetName(report.dataset_path),
    metric: `recall@${report.metrics.k} ${fmtPct(report.metrics.recall_at_k)} · F1 ${fmtPct(report.metrics.f1_at_k)}`,
    createdAt: report.created_at,
    route: 'eval-kb',
  }))
  return [...agent, ...kb]
    .sort((a, b) => b.createdAt.localeCompare(a.createdAt))
    .slice(0, 4)
})

const navCards = computed(() =>
  ADMIN_MENU.filter((item) => item.name !== 'dashboard')
    .filter((item) => !item.roles || (session !== null && item.roles.includes(session.role)))
    .map((item) => ({ ...item, icon: resolveNavIcon(item.name) })),
)

const chartError = computed(() => errors.series || errors.runStatus || '')
const workbenchError = computed(() => errors.approvals || errors.pipeline || errors.handoff || '')
const qualityError = computed(() => errors.quality || errors.eval || '')

const trendOption = computed(() => ({
  color: [CHART_PALETTE[0], CHART_PALETTE[1], CHART_PALETTE[2]],
  tooltip: { trigger: 'axis' },
  legend: {
    data: ['会话', '运行', '消息'],
    top: 0,
    itemWidth: 14,
    itemHeight: 8,
    textStyle: { color: CHART_INK.text },
  },
  grid: { left: 44, right: 16, top: 40, bottom: 28 },
  xAxis: {
    type: 'category',
    boundaryGap: false,
    data: series.value.map((item) => item.day.slice(5, 10).replace('-', '/')),
    axisLine: { lineStyle: { color: CHART_INK.axis } },
    axisLabel: { color: CHART_INK.text },
  },
  yAxis: {
    type: 'value',
    splitLine: { lineStyle: { color: CHART_INK.split } },
    axisLabel: { color: CHART_INK.text },
  },
  series: [
    {
      name: '会话',
      type: 'line',
      smooth: true,
      symbol: 'none',
      lineStyle: { width: 2 },
      areaStyle: { opacity: 0.12 },
      data: series.value.map((item) => item.threads),
    },
    {
      name: '运行',
      type: 'line',
      smooth: true,
      symbol: 'none',
      lineStyle: { width: 2 },
      data: series.value.map((item) => item.runs),
    },
    {
      name: '消息',
      type: 'line',
      smooth: true,
      symbol: 'none',
      lineStyle: { width: 1.5, type: 'dashed' },
      data: series.value.map((item) => item.messages),
    },
  ],
}))

const runStatusOption = computed(() => {
  const counts = runStatus.value?.counts ?? {}
  const completed = counts.completed ?? 0
  const failed = counts.failed ?? 0
  const interrupted = runStatus.value?.interrupted ?? 0
  const total = runStatus.value?.total ?? 0
  const other = Math.max(0, total - completed - failed - interrupted)
  const pieData = [
    { name: '已完成', value: completed, itemStyle: { color: CHART_PALETTE[3] } },
    { name: '已失败', value: failed, itemStyle: { color: CHART_PALETTE[6] } },
    { name: '已中断', value: interrupted, itemStyle: { color: CHART_PALETTE[4] } },
  ]
  if (other > 0) {
    pieData.push({ name: '其他', value: other, itemStyle: { color: CHART_PALETTE[5] } })
  }
  return {
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: {
      bottom: 0,
      itemWidth: 12,
      itemHeight: 8,
      textStyle: { color: CHART_INK.text },
    },
    series: [
      {
        type: 'pie',
        radius: ['54%', '74%'],
        center: ['50%', '44%'],
        label: { show: false },
        emphasis: {
          label: { show: true, fontSize: 16, fontWeight: 600, color: CHART_INK.text },
        },
        data: pieData,
      },
    ],
  }
})

const hotKeywordsOption = computed(() => ({
  color: [CHART_PALETTE[1]],
  tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
  grid: { left: 96, right: 24, top: 12, bottom: 20 },
  xAxis: {
    type: 'value',
    splitLine: { lineStyle: { color: CHART_INK.split } },
    axisLabel: { color: CHART_INK.text },
  },
  yAxis: {
    type: 'category',
    inverse: true,
    data: hotKeywords.value.map((item) => item.name),
    axisLine: { lineStyle: { color: CHART_INK.axis } },
    axisLabel: { color: CHART_INK.text },
  },
  series: [
    {
      type: 'bar',
      barWidth: 12,
      itemStyle: { borderRadius: [0, 6, 6, 0], color: CHART_PALETTE[1] },
      data: hotKeywords.value.map((item) => item.count),
    },
  ],
}))

const qualityRadarOption = computed(() => {
  const dimensions = scorecardDimensions.value
  return {
    tooltip: {},
    radar: {
      indicator: dimensions.map((dimension) => ({ name: dimension.label, max: 100 })),
      radius: '62%',
      axisName: { color: CHART_INK.text, fontSize: 12 },
      axisLine: { lineStyle: { color: CHART_INK.axis } },
      splitLine: { lineStyle: { color: CHART_INK.split } },
      splitArea: { areaStyle: { color: ['rgba(26, 58, 107, 0.02)', 'rgba(26, 58, 107, 0.05)'] } },
    },
    series: [
      {
        type: 'radar',
        symbolSize: 5,
        lineStyle: { width: 2, color: CHART_PALETTE[0] },
        itemStyle: { color: CHART_PALETTE[0] },
        areaStyle: { color: 'rgba(26, 58, 107, 0.18)' },
        data: [
          {
            name: '质量评分',
            value: dimensions.map((dimension) => +(dimension.score * 100).toFixed(1)),
          },
        ],
      },
    ],
  }
})
</script>

<template>
  <div class="dashboard">
    <!-- Hero -->
    <section class="hero">
      <span class="hero__glow hero__glow--gold" />
      <span class="hero__glow hero__glow--blue" />
      <div class="hero__main">
        <p class="hero__date">{{ dateLabel }}</p>
        <h1 class="hero__title">{{ greeting }}，{{ displayName }}</h1>
        <p class="hero__subtitle">{{ heroSubtitle }}</p>
        <div class="hero__meta">
          <span class="status-pill" :class="`is-${systemStatus.tone}`">
            <span class="status-pill__dot" />
            {{ systemStatus.text }}
          </span>
          <span
            v-for="check in serviceChecks"
            :key="check.key"
            class="service-chip"
            :class="{ 'is-off': !check.ok }"
            :title="`${check.label}: ${check.value}`"
          >
            <span class="service-chip__dot" />
            {{ check.label }}
          </span>
        </div>
      </div>
      <div class="hero__side">
        <a-radio-group
          v-model:value="range"
          button-style="solid"
          size="small"
          class="hero__range"
        >
          <a-radio-button
            v-for="option in RANGE_OPTIONS"
            :key="option.value"
            :value="option.value"
          >
            {{ option.label }}
          </a-radio-button>
        </a-radio-group>
        <div class="hero__refresh">
          <span>数据更新于 {{ updatedLabel }}</span>
          <a-button
            type="text"
            size="small"
            class="hero__refresh-btn"
            :loading="refreshing"
            @click="refresh"
          >
            <template #icon><ReloadOutlined /></template>
          </a-button>
        </div>
      </div>
    </section>

    <!-- KPI -->
    <div
      v-if="loading"
      class="kpi-skeleton"
      :style="{ '--kpi-cols': kpiColumns }"
    >
      <div
        v-for="index in kpiColumns"
        :key="index"
        class="skeleton-card"
      >
        <a-skeleton
          active
          :paragraph="{ rows: 1 }"
        />
      </div>
    </div>
    <StatGrid
      v-else
      :column="kpiColumns"
    >
      <div
        v-for="item in kpis"
        :key="item.key"
        class="kpi-click"
        @click="go(item.route)"
      >
        <StatCard
          :title="item.title"
          :value="item.value"
          :suffix="item.suffix"
          :icon="item.icon"
          :trend="item.trend?.text"
          :trend-type="item.trend?.type"
        />
      </div>
    </StatGrid>
    <a-alert
      v-if="canManage && errors.overview"
      type="warning"
      :message="`经营指标暂不可用：${errors.overview}`"
      show-icon
      class="section-gap"
    />

    <!-- 告警 -->
    <section
      v-if="canManage"
      class="alert-section"
    >
      <a-skeleton
        v-if="loading"
        active
        :paragraph="{ rows: 1 }"
      />
      <a-alert
        v-else-if="errors.alerts"
        type="warning"
        :message="`告警评估不可用：${errors.alerts}`"
        show-icon
      />
      <a-alert
        v-else-if="triggeredAlerts.length === 0"
        type="success"
        :message="`${alerts?.rules.length ?? 0} 项运行规则全部正常`"
        show-icon
      />
      <a-alert
        v-else
        :type="alerts?.status === 'critical' ? 'error' : 'warning'"
        :message="`${triggeredAlerts.length} 项告警待关注`"
        show-icon
      >
        <template #description>
          <div class="alert-detail">
            <span
              v-for="rule in triggeredAlerts"
              :key="rule.rule"
            >{{ rule.summary }}</span>
          </div>
        </template>
        <template #action>
          <a-button
            type="link"
            size="small"
            @click="go('health')"
          >
            查看详情
          </a-button>
        </template>
      </a-alert>
    </section>

    <!-- 趋势与终态 -->
    <a-row
      v-if="canManage"
      :gutter="[16, 16]"
      class="section-gap"
    >
      <a-col
        :xs="24"
        :lg="16"
      >
        <ChartCard
          title="近 14 天接待趋势"
          :option="trendOption"
          :loading="loading"
          :empty="!loading && series.length === 0"
          empty-text="暂无趋势数据"
          height="300px"
        >
          <template #extra>
            <span class="card-extra">会话 / 运行 / 消息</span>
          </template>
        </ChartCard>
      </a-col>
      <a-col
        :xs="24"
        :lg="8"
      >
        <ChartCard
          title="Run 终态分布"
          :option="runStatusOption"
          :loading="loading"
          :empty="!loading && !runStatus"
          empty-text="暂无运行数据"
          height="300px"
        >
          <template #extra>
            <span class="card-extra">中断率 {{ fmtPct(runStatus?.interrupt_ratio) }}</span>
          </template>
        </ChartCard>
      </a-col>
    </a-row>
    <a-alert
      v-if="canManage && chartError"
      type="warning"
      :message="`运行图表暂不可用：${chartError}`"
      show-icon
      class="section-gap"
    />

    <!-- 待处理 + 质量 -->
    <a-row
      :gutter="[16, 16]"
      class="section-gap"
    >
      <a-col
        :xs="24"
        :xl="canManage ? 10 : 24"
      >
        <SectionCard title="待处理事项">
          <template #extra>
            <span class="card-extra">点击进入对应工作台</span>
          </template>
          <a-skeleton
            v-if="loading"
            active
            :paragraph="{ rows: 4 }"
          />
          <template v-else>
            <a-alert
              v-if="workbenchError"
              type="warning"
              :message="`部分待办数据暂不可用：${workbenchError}`"
              show-icon
              class="inner-alert"
            />
            <div class="workbench">
              <button
                v-for="item in workItems"
                :key="item.key"
                type="button"
                class="workbench__item"
                :class="`is-${item.tone}`"
                @click="go(item.route)"
              >
                <span class="workbench__icon">
                  <component :is="item.icon" />
                </span>
                <span class="workbench__body">
                  <span class="workbench__value">{{ item.count }}</span>
                  <span class="workbench__title">{{ item.title }}</span>
                  <span class="workbench__hint">{{ item.hint }}</span>
                </span>
                <ArrowRightOutlined class="workbench__arrow" />
              </button>
            </div>
          </template>
        </SectionCard>
      </a-col>

      <a-col
        v-if="canManage"
        :xs="24"
        :xl="14"
      >
        <SectionCard title="质量与评估">
          <template #extra>
            <a
              class="card-link"
              @click="go('quality-scorecard')"
            >评分卡详情</a>
          </template>
          <a-skeleton
            v-if="loading"
            active
            :paragraph="{ rows: 5 }"
          />
          <template v-else>
            <a-alert
              v-if="qualityError"
              type="warning"
              :message="`质量数据暂不可用：${qualityError}`"
              show-icon
              class="inner-alert"
            />
            <template v-if="scorecard">
              <a-row :gutter="16">
                <a-col
                  :xs="24"
                  :md="12"
                >
                  <BaseChart
                    :option="qualityRadarOption"
                    height="250px"
                  />
                </a-col>
                <a-col
                  :xs="24"
                  :md="12"
                >
                  <div class="quality-score">
                    <span class="quality-score__value">{{ (scorecard.overall_score * 100).toFixed(1) }}</span>
                    <span class="quality-score__suffix">%</span>
                    <span class="quality-score__label">综合评分</span>
                  </div>
                  <div
                    v-for="dimension in scorecardDimensions"
                    :key="dimension.key"
                    class="quality-dim"
                  >
                    <div class="quality-dim__head">
                      <span>{{ dimension.label }}</span>
                      <span>{{ fmtPct(dimension.score) }}</span>
                    </div>
                    <a-progress
                      :percent="+(dimension.score * 100).toFixed(1)"
                      :show-info="false"
                      size="small"
                      :stroke-color="progressColor(dimension.score)"
                    />
                  </div>
                </a-col>
              </a-row>
              <a-divider style="margin: 14px 0" />
              <div class="eval-list">
                <button
                  v-for="item in evalHighlights.slice(0, 2)"
                  :key="item.key"
                  type="button"
                  class="eval-item"
                  @click="go(item.route)"
                >
                  <span class="eval-item__kind">{{ item.kind }}</span>
                  <span class="eval-item__title">{{ item.title }}</span>
                  <span class="eval-item__metric">{{ item.metric }}</span>
                  <span class="eval-item__time">{{ formatDate(item.createdAt) }}</span>
                </button>
                <EmptyState
                  v-if="evalHighlights.length === 0"
                  description="暂无评估报告"
                />
              </div>
            </template>
            <EmptyState
              v-else-if="!qualityError"
              description="暂无评分卡数据"
              action-text="前往生成"
              @action="go('quality-scorecard')"
            />
          </template>
        </SectionCard>
      </a-col>
    </a-row>

    <!-- 最近动态 + 热门问题 -->
    <a-row
      :gutter="[16, 16]"
      class="section-gap"
    >
      <a-col
        :xs="24"
        :lg="canManage ? 15 : 24"
      >
        <SectionCard title="最近动态">
          <a-tabs
            v-model:active-key="activityTab"
            size="small"
          >
            <a-tab-pane
              key="conversations"
              tab="最近会话"
            >
              <a-alert
                v-if="errors.conversations"
                type="warning"
                :message="errors.conversations"
                show-icon
                class="inner-alert"
              />
              <a-list
                v-if="recentConversations.length > 0"
                :data-source="recentConversations"
                size="small"
              >
                <template #renderItem="{ item }">
                  <a-list-item
                    class="activity-item"
                    @click="go('conversations')"
                  >
                    <a-list-item-meta>
                      <template #title>
                        <span class="activity-title">{{ item.title || item.user_display_name || item.username }}</span>
                      </template>
                      <template #description>
                        <span>
                          {{ item.agent_slug }} · {{ item.message_count }} 条消息 ·
                          {{ formatDateTime(item.last_message_at || item.updated_at) }}
                        </span>
                      </template>
                    </a-list-item-meta>
                    <template #extra>
                      <a-tag
                        v-if="item.handoff_status"
                        :color="item.handoff_status === 'pending' ? 'orange' : 'blue'"
                      >
                        {{ HANDOFF_LABELS[item.handoff_status] || item.handoff_status }}
                      </a-tag>
                    </template>
                  </a-list-item>
                </template>
              </a-list>
              <EmptyState
                v-else-if="!loading"
                description="暂无会话"
              />
            </a-tab-pane>

            <a-tab-pane
              v-if="canManage"
              key="pipeline"
              tab="管道运行"
            >
              <a-list
                v-if="recentRuns.length > 0"
                :data-source="recentRuns"
                size="small"
              >
                <template #renderItem="{ item }">
                  <a-list-item
                    class="activity-item"
                    @click="go('data-pipeline')"
                  >
                    <a-list-item-meta>
                      <template #title>
                        <span class="activity-title">{{ item.source_table || item.task_id || '管道运行' }}</span>
                      </template>
                      <template #description>
                        <span>
                          {{ fmtNum(item.processed_rows) }} 行 ·
                          {{ item.duration_seconds != null ? `${item.duration_seconds.toFixed(1)}s` : '—' }} ·
                          {{ formatDateTime(item.created_at) }}
                        </span>
                      </template>
                    </a-list-item-meta>
                    <template #extra>
                      <StatusTag
                        :status="item.status"
                        :status-map="PIPELINE_STATUS_MAP"
                        :label="PIPELINE_STATUS_LABELS[item.status] || item.status"
                      />
                    </template>
                  </a-list-item>
                </template>
              </a-list>
              <EmptyState
                v-else-if="!loading"
                description="暂无管道运行记录"
              />
            </a-tab-pane>

            <a-tab-pane
              v-if="canManage"
              key="eval"
              tab="评估报告"
            >
              <div
                v-if="evalHighlights.length > 0"
                class="eval-list"
              >
                <button
                  v-for="item in evalHighlights"
                  :key="item.key"
                  type="button"
                  class="eval-item"
                  @click="go(item.route)"
                >
                  <span class="eval-item__kind">{{ item.kind }}</span>
                  <span class="eval-item__title">{{ item.title }}</span>
                  <span class="eval-item__metric">{{ item.metric }}</span>
                  <span class="eval-item__time">{{ formatDate(item.createdAt) }}</span>
                </button>
              </div>
              <EmptyState
                v-else-if="!loading"
                description="暂无评估报告"
              />
            </a-tab-pane>
          </a-tabs>
        </SectionCard>
      </a-col>

      <a-col
        v-if="canManage"
        :xs="24"
        :lg="9"
      >
        <ChartCard
          title="热门问题"
          :option="hotKeywordsOption"
          :loading="loading"
          :empty="!loading && hotKeywords.length === 0"
          empty-text="暂无热门问题数据"
          height="320px"
        >
          <template #extra>
            <span class="card-extra">TOP {{ hotKeywords.length || 8 }}</span>
          </template>
        </ChartCard>
      </a-col>
    </a-row>

    <!-- 功能导航 -->
    <SectionCard
      title="功能导航"
      class="section-gap"
    >
      <template #extra>
        <span class="card-extra">按角色展示全部 PC 模块</span>
      </template>
      <div class="nav-grid">
        <button
          v-for="item in navCards"
          :key="item.name"
          type="button"
          class="nav-card"
          @click="go(item.name)"
        >
          <span class="nav-card__icon">
            <component :is="item.icon" />
          </span>
          <span class="nav-card__body">
            <span class="nav-card__title">{{ item.title }}</span>
            <span class="nav-card__desc">{{ item.description }}</span>
          </span>
          <ArrowRightOutlined class="nav-card__arrow" />
        </button>
      </div>
    </SectionCard>
  </div>
</template>

<style scoped>
.dashboard {
  width: 100%;
  max-width: 1560px;
  margin: 0 auto;
}

.section-gap {
  margin-top: 16px;
}

/* ───── Hero ───── */
.hero {
  position: relative;
  display: flex;
  justify-content: space-between;
  gap: 24px;
  overflow: hidden;
  padding: 28px 32px;
  color: #fff;
  background: linear-gradient(120deg, var(--brand-9) 0%, var(--brand-6) 58%, #24487c 100%);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-lg);
}

.hero__glow {
  position: absolute;
  border-radius: 50%;
  pointer-events: none;
}

.hero__glow--gold {
  top: -150px;
  right: -60px;
  width: 340px;
  height: 340px;
  background: radial-gradient(circle, rgba(201, 162, 39, 0.3), transparent 65%);
}

.hero__glow--blue {
  bottom: -190px;
  left: 34%;
  width: 280px;
  height: 280px;
  background: radial-gradient(circle, rgba(126, 154, 193, 0.24), transparent 65%);
}

.hero__main,
.hero__side {
  position: relative;
  z-index: 1;
}

.hero__main {
  min-width: 0;
}

.hero__date {
  margin: 0 0 8px;
  color: rgba(255, 255, 255, 0.68);
  font-size: 13px;
  letter-spacing: 1px;
}

.hero__title {
  margin: 0;
  font-size: 26px;
  font-weight: 600;
  letter-spacing: 0.5px;
}

.hero__subtitle {
  margin: 10px 0 18px;
  color: rgba(255, 255, 255, 0.78);
  font-size: 13px;
}

.hero__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  align-items: center;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 12px;
  font-size: 12px;
  background: rgba(255, 255, 255, 0.12);
  border-radius: 999px;
}

.status-pill__dot {
  width: 7px;
  height: 7px;
  background: #9ca3af;
  border-radius: 50%;
}

.status-pill.is-success .status-pill__dot {
  background: #5bd68a;
  box-shadow: 0 0 0 3px rgba(91, 214, 138, 0.18);
}

.status-pill.is-warning .status-pill__dot {
  background: #f0b44c;
  box-shadow: 0 0 0 3px rgba(240, 180, 76, 0.18);
}

.status-pill.is-danger .status-pill__dot {
  background: #ff6b81;
  box-shadow: 0 0 0 3px rgba(255, 107, 129, 0.18);
}

.service-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: rgba(255, 255, 255, 0.7);
  font-size: 12px;
}

.service-chip__dot {
  width: 5px;
  height: 5px;
  background: #5bd68a;
  border-radius: 50%;
}

.service-chip.is-off .service-chip__dot {
  background: #ff6b81;
}

.hero__side {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-shrink: 0;
}

.hero__range :deep(.ant-radio-button-wrapper) {
  color: rgba(255, 255, 255, 0.78);
  background: rgba(255, 255, 255, 0.08);
  border-color: rgba(255, 255, 255, 0.16);
}

.hero__range :deep(.ant-radio-button-wrapper-checked) {
  color: var(--brand-9);
  background: var(--gold-6);
  border-color: var(--gold-6);
}

.hero__refresh {
  display: flex;
  align-items: center;
  gap: 4px;
  color: rgba(255, 255, 255, 0.66);
  font-size: 12px;
}

.hero__refresh-btn {
  color: rgba(255, 255, 255, 0.78);
}

/* ───── KPI ───── */
.kpi-click {
  height: 100%;
  cursor: pointer;
}

.kpi-click :deep(.stat-card) {
  height: 100%;
}

.kpi-skeleton {
  display: grid;
  grid-template-columns: repeat(var(--kpi-cols, 4), minmax(0, 1fr));
  gap: 16px;
}

.skeleton-card {
  padding: 20px;
  background: var(--bg-container);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-card);
}

/* ───── 告警 ───── */
.alert-section {
  margin-top: 16px;
}

.alert-detail {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
}

/* ───── 待处理 ───── */
.workbench {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.workbench__item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px;
  font: inherit;
  text-align: left;
  background: var(--bg-hover);
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background 0.2s, border-color 0.2s, box-shadow 0.2s, transform 0.2s;
}

.workbench__item:hover {
  background: #fff;
  border-color: var(--brand-2);
  box-shadow: var(--shadow-sm);
  transform: translateY(-1px);
}

.workbench__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 38px;
  height: 38px;
  color: var(--brand-6);
  font-size: 18px;
  background: var(--brand-1);
  border-radius: 10px;
  flex-shrink: 0;
}

.workbench__item.is-warning .workbench__icon {
  color: var(--warning);
  background: rgba(217, 138, 31, 0.12);
}

.workbench__item.is-danger .workbench__icon {
  color: var(--error);
  background: rgba(208, 48, 80, 0.1);
}

.workbench__item.is-success .workbench__icon {
  color: var(--success);
  background: rgba(46, 158, 91, 0.1);
}

.workbench__body {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.workbench__value {
  color: var(--brand-6);
  font-size: 22px;
  font-weight: 700;
  line-height: 1.1;
}

.workbench__title {
  color: var(--text-1);
  font-size: 13px;
  font-weight: 600;
}

.workbench__hint {
  overflow: hidden;
  color: var(--text-3);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.workbench__arrow {
  margin-left: auto;
  color: var(--text-4);
}

.inner-alert {
  margin-bottom: 12px;
}

/* ───── 质量 ───── */
.quality-score {
  display: flex;
  align-items: baseline;
  gap: 4px;
  margin-bottom: 12px;
}

.quality-score__value {
  color: var(--brand-6);
  font-size: 32px;
  font-weight: 700;
  line-height: 1;
}

.quality-score__suffix {
  color: var(--text-3);
  font-size: 14px;
}

.quality-score__label {
  margin-left: auto;
  color: var(--text-3);
  font-size: 12px;
}

.quality-dim {
  margin-bottom: 8px;
}

.quality-dim__head {
  display: flex;
  justify-content: space-between;
  margin-bottom: 2px;
  color: var(--text-2);
  font-size: 12px;
}

.eval-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.eval-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  font: inherit;
  text-align: left;
  background: var(--bg-hover);
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background 0.2s;
}

.eval-item:hover {
  background: var(--brand-1);
}

.eval-item__kind {
  padding: 1px 8px;
  color: var(--brand-6);
  font-size: 11px;
  background: var(--brand-1);
  border-radius: 999px;
  flex-shrink: 0;
}

.eval-item__title {
  overflow: hidden;
  color: var(--text-1);
  font-size: 13px;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.eval-item__metric {
  margin-left: auto;
  color: var(--text-3);
  font-size: 12px;
  white-space: nowrap;
}

.eval-item__time {
  color: var(--text-4);
  font-size: 12px;
  white-space: nowrap;
}

/* ───── 最近动态 ───── */
.activity-item {
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.activity-item:hover {
  background: var(--bg-hover);
}

.activity-title {
  color: var(--text-1);
  font-weight: 500;
}

/* ───── 功能导航 ───── */
.nav-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}

.nav-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  font: inherit;
  text-align: left;
  background: #fff;
  border: 1px solid var(--divider);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: border-color 0.2s, box-shadow 0.2s, transform 0.2s;
}

.nav-card:hover {
  border-color: var(--brand-2);
  box-shadow: var(--shadow-sm);
  transform: translateY(-1px);
}

.nav-card__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  color: var(--brand-6);
  font-size: 17px;
  background: var(--brand-1);
  border-radius: 10px;
  flex-shrink: 0;
}

.nav-card__body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.nav-card__title {
  color: var(--text-1);
  font-size: 14px;
  font-weight: 600;
}

.nav-card__desc {
  overflow: hidden;
  color: var(--text-3);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.nav-card__arrow {
  margin-left: auto;
  color: var(--text-4);
  flex-shrink: 0;
}

.card-extra {
  color: var(--text-3);
  font-size: 12px;
}

.card-link {
  color: var(--brand-6);
  font-size: 12px;
}

@media (max-width: 1200px) {
  .workbench {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 960px) {
  .hero {
    flex-direction: column;
    padding: 24px;
  }

  .hero__side {
    align-items: flex-start;
  }

  .hero__title {
    font-size: 22px;
  }

  .kpi-skeleton {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .eval-item {
    flex-wrap: wrap;
  }

  .eval-item__metric {
    margin-left: 0;
  }
}

@media (max-width: 640px) {
  .kpi-skeleton {
    grid-template-columns: 1fr;
  }
}
</style>
