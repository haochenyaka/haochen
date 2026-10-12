<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { fetchOverview, fetchReceptionSeries, fetchHotQuestions, type OverviewResponse, type DailyCountModel } from '@/api/report'
import { EmptyState, SectionCard, StatCard, StatGrid } from '@/components'
import ChartCard from '@/components/chart/ChartCard.vue'
import { CHART_INK, CHART_PALETTE } from '@/components/chart/echarts'

const overview = ref<OverviewResponse | null>(null)
const series = ref<DailyCountModel[]>([])
const topKeywords = ref<Array<{ name: string; count: number }>>([])
const loading = ref(true)
const error = ref('')

const now = new Date()
const thirtyDaysAgo = new Date(now.getTime() - 30 * 86400 * 1000)
const fmt = (d: Date) => d.toISOString()

function fmtRate(rate: number | null): string {
  if (rate == null) return '-'
  return `${(rate * 100).toFixed(1)}%`
}

function fmtMs(ms: number | null): string {
  if (ms == null) return '-'
  return `${ms.toFixed(0)}ms`
}

/** 每日接待趋势折线。 */
const trendOption = computed(() => ({
  color: [CHART_PALETTE[0], CHART_PALETTE[1], CHART_PALETTE[2]],
  tooltip: { trigger: 'axis' },
  legend: { data: ['线程', '运行', '消息'], top: 0, textStyle: { color: CHART_INK.text } },
  grid: { left: 40, right: 16, top: 36, bottom: 28 },
  xAxis: {
    type: 'category',
    data: series.value.map((item) => item.day?.split('T')[0] ?? item.day),
    axisLine: { lineStyle: { color: CHART_INK.axis } },
    axisLabel: { color: CHART_INK.text },
  },
  yAxis: {
    type: 'value',
    splitLine: { lineStyle: { color: CHART_INK.split } },
    axisLabel: { color: CHART_INK.text },
  },
  series: [
    { name: '线程', type: 'line', smooth: true, data: series.value.map((item) => item.threads) },
    { name: '运行', type: 'line', smooth: true, data: series.value.map((item) => item.runs) },
    { name: '消息', type: 'line', smooth: true, data: series.value.map((item) => item.messages) },
  ],
}))

/** 缓存命中环形图。 */
const cacheOption = computed(() => {
  const hit = overview.value?.cache.hit_rate ?? 0
  const hitPct = Math.max(0, Math.min(1, hit)) * 100
  return {
    color: [CHART_PALETTE[0], CHART_PALETTE[1]],
    tooltip: { trigger: 'item', formatter: '{b}: {d}%' },
    series: [
      {
        type: 'pie',
        radius: ['58%', '78%'],
        label: { formatter: '{b}\n{d}%', color: CHART_INK.text },
        data: [
          { name: '缓存命中', value: hitPct },
          { name: '未命中', value: 100 - hitPct },
        ],
      },
    ],
  }
})

/** 热门问题横向条形图。 */
const keywordOption = computed(() => ({
  color: [CHART_PALETTE[0]],
  tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
  grid: { left: 90, right: 24, top: 12, bottom: 24 },
  xAxis: {
    type: 'value',
    splitLine: { lineStyle: { color: CHART_INK.split } },
    axisLabel: { color: CHART_INK.text },
  },
  yAxis: {
    type: 'category',
    inverse: true,
    data: topKeywords.value.map((item) => item.name),
    axisLine: { lineStyle: { color: CHART_INK.axis } },
    axisLabel: { color: CHART_INK.text },
  },
  series: [
    {
      type: 'bar',
      barWidth: 14,
      itemStyle: { borderRadius: [0, 6, 6, 0] },
      data: topKeywords.value.map((item) => item.count),
    },
  ],
}))

onMounted(async () => {
  try {
    const params = { start: fmt(thirtyDaysAgo), end: fmt(now) }
    const [ov, srs, hot] = await Promise.all([
      fetchOverview(params),
      fetchReceptionSeries(params),
      fetchHotQuestions({ ...params, limit: 10 }),
    ])
    overview.value = ov
    series.value = srs.series
    topKeywords.value = hot.top_keywords
  } catch (err) {
    error.value = (err as { message?: string }).message || '加载失败'
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <a-spin :spinning="loading">
    <a-alert v-if="error" type="warning" :message="error" show-icon style="margin-bottom:16px" />

    <template v-if="overview">
      <a-space direction="vertical" :size="16" style="width:100%">
        <!-- 核心指标 -->
        <SectionCard title="核心指标">
          <StatGrid :column="4">
            <StatCard title="会话线程（Thread）" :value="overview.reception.threads" />
            <StatCard title="运行次数（Run）" :value="overview.reception.runs" />
            <StatCard title="消息条数" :value="overview.reception.messages" />
            <StatCard title="一次解决率" :value="fmtRate(overview.resolution.first_resolution_rate)" />
          </StatGrid>
        </SectionCard>

        <!-- 接待量 / 解决率 -->
        <a-row :gutter="16">
          <a-col :span="12">
            <SectionCard title="接待量">
              <a-descriptions :column="1" size="small">
                <a-descriptions-item label="会话线程（Thread）">{{ overview.reception.threads }}</a-descriptions-item>
                <a-descriptions-item label="运行次数（Run）">{{ overview.reception.runs }}</a-descriptions-item>
                <a-descriptions-item label="消息条数">{{ overview.reception.messages }}</a-descriptions-item>
              </a-descriptions>
            </SectionCard>
          </a-col>
          <a-col :span="12">
            <SectionCard title="解决率">
              <a-descriptions :column="1" size="small">
                <a-descriptions-item label="终态 Run">{{ overview.resolution.terminal_runs }}</a-descriptions-item>
                <a-descriptions-item label="一次解决">{{ overview.resolution.first_resolved_runs }}</a-descriptions-item>
                <a-descriptions-item label="解决率">{{ fmtRate(overview.resolution.first_resolution_rate) }}</a-descriptions-item>
                <a-descriptions-item label="任务成功 / 失败">
                  {{ overview.resolution.tasks_succeeded }} / {{ overview.resolution.tasks_failed }}
                </a-descriptions-item>
                <a-descriptions-item label="任务成功率">{{ fmtRate(overview.resolution.task_success_rate) }}</a-descriptions-item>
              </a-descriptions>
            </SectionCard>
          </a-col>
        </a-row>

        <!-- 图表：趋势 / 缓存 -->
        <a-row :gutter="16">
          <a-col :span="16">
            <ChartCard
              title="每日接待趋势"
              :option="trendOption"
              :empty="series.length === 0"
              empty-text="暂无趋势数据"
              height="300px"
            />
          </a-col>
          <a-col :span="8">
            <ChartCard
              title="缓存命中"
              :option="cacheOption"
              height="300px"
            >
              <template #extra>
                <span style="font-size:12px;color:var(--text-3)">命中率 {{ fmtRate(overview.cache.hit_rate) }}</span>
              </template>
            </ChartCard>
          </a-col>
        </a-row>

        <!-- 热门问题条形图 -->
        <ChartCard
          title="热门问题"
          :option="keywordOption"
          :empty="topKeywords.length === 0"
          empty-text="暂无热门问题数据"
          :height="`${Math.max(200, topKeywords.length * 32 + 40)}px`"
        />

        <!-- 转人工 / 知识召回 -->
        <a-row :gutter="16">
          <a-col :span="12">
            <SectionCard title="转人工">
              <a-descriptions :column="2" size="small">
                <a-descriptions-item label="转人工数">{{ overview.handoff.handoffs }}</a-descriptions-item>
                <a-descriptions-item label="会话线程">{{ overview.handoff.threads }}</a-descriptions-item>
                <a-descriptions-item label="转人工率">{{ fmtRate(overview.handoff.rate) }}</a-descriptions-item>
              </a-descriptions>
            </SectionCard>
          </a-col>
          <a-col :span="12">
            <SectionCard title="知识召回">
              <a-descriptions :column="1" size="small">
                <a-descriptions-item label="离线 recall@k">
                  {{ overview.recall.offline_recall_at_k != null ? `${(overview.recall.offline_recall_at_k * 100).toFixed(1)}%` : '未评估' }}
                </a-descriptions-item>
                <a-descriptions-item label="线上引用率">{{ fmtRate(overview.recall.online_citation_rate) }}</a-descriptions-item>
              </a-descriptions>
            </SectionCard>
          </a-col>
        </a-row>

        <!-- 运行性能 -->
        <SectionCard title="运行性能">
          <a-descriptions :column="3" size="small">
            <a-descriptions-item label="TTFT P50">{{ fmtMs(overview.performance.ttft_p50_ms) }}</a-descriptions-item>
            <a-descriptions-item label="TTFT P95">{{ fmtMs(overview.performance.ttft_p95_ms) }}</a-descriptions-item>
            <a-descriptions-item label="TTFT 样本">{{ overview.performance.ttft_samples }}</a-descriptions-item>
            <a-descriptions-item label="工具调用">{{ overview.performance.tool_calls }}</a-descriptions-item>
            <a-descriptions-item label="工具成功率">{{ fmtRate(overview.performance.tool_success_rate) }}</a-descriptions-item>
            <a-descriptions-item label="模型重试率">{{ fmtRate(overview.performance.model_retry_rate) }}</a-descriptions-item>
          </a-descriptions>
          <template v-if="overview.cache.estimated_saving > 0">
            <a-divider style="margin:12px 0" />
            <div style="font-size:12px;color:var(--success)">
              缓存预估节省：¥{{ overview.cache.estimated_saving.toFixed(2) }}
            </div>
          </template>
        </SectionCard>
      </a-space>
    </template>

    <EmptyState v-if="!loading && !overview && !error" description="暂无报表数据" />
  </a-spin>
</template>