<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { fetchRunStatusDistribution, fetchCacheHitTrend, type RunStatusDistributionModel, type CacheHitTrendModel } from '@/api/health'
import { SectionCard, StatCard, StatGrid } from '@/components'
import ChartCard from '@/components/chart/ChartCard.vue'
import { CHART_INK, CHART_PALETTE } from '@/components/chart/echarts'

const runtime = ref<RunStatusDistributionModel | null>(null)
const cacheTrend = ref<CacheHitTrendModel[]>([])
const loading = ref(true)
const error = ref('')

const now = new Date()
const thirtyDaysAgo = new Date(now.getTime() - 30 * 86400 * 1000)
const fmt = (d: Date) => d.toISOString()

/** Run 终态分布环形图。 */
const statusOption = computed(() => {
  const counts = runtime.value?.counts ?? {}
  return {
    color: [CHART_PALETTE[3], CHART_PALETTE[6], CHART_PALETTE[4]],
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, textStyle: { color: CHART_INK.text } },
    series: [
      {
        type: 'pie',
        radius: ['52%', '72%'],
        label: { color: CHART_INK.text },
        data: [
          { name: '已完成', value: counts.completed ?? 0 },
          { name: '已失败', value: counts.failed ?? 0 },
          { name: '已中断', value: runtime.value?.interrupted ?? 0 },
        ],
      },
    ],
  }
})

/** 缓存命中：接口返回聚合总量（非时间序列），故用占比环形图。 */
const cacheOption = computed(() => {
  const row = cacheTrend.value[0]
  const cached = row?.cached_prompt_tokens ?? 0
  const total = row?.prompt_tokens ?? 0
  const uncached = Math.max(0, total - cached)
  return {
    color: [CHART_PALETTE[1], CHART_PALETTE[3]],
    tooltip: { trigger: 'item', formatter: '{b}: {c} tokens ({d}%)' },
    legend: { bottom: 0, textStyle: { color: CHART_INK.text } },
    series: [
      {
        type: 'pie',
        radius: ['52%', '72%'],
        label: { color: CHART_INK.text },
        data: [
          { name: '缓存 tokens', value: cached },
          { name: '未缓存 tokens', value: uncached },
        ],
      },
    ],
  }
})

onMounted(async () => {
  try {
    const params = { start: fmt(thirtyDaysAgo), end: fmt(now) }
    const [dist, trend] = await Promise.all([
      fetchRunStatusDistribution(params),
      fetchCacheHitTrend(params),
    ])
    runtime.value = dist
    cacheTrend.value = trend
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

    <a-space direction="vertical" :size="16" style="width:100%">
      <template v-if="runtime">
        <SectionCard title="Run 概览">
          <StatGrid :column="4">
            <StatCard title="总计" :value="runtime.total" />
            <StatCard title="已完成" :value="runtime.counts.completed ?? 0" />
            <StatCard title="已失败" :value="runtime.counts.failed ?? 0" />
            <StatCard title="已中断" :value="runtime.interrupted" />
          </StatGrid>
          <a-divider style="margin:16px 0" />
          <div style="font-size:13px;color:var(--text-3);margin-bottom:8px">
            Interrupt 比例：{{ runtime.interrupt_ratio != null ? (runtime.interrupt_ratio * 100).toFixed(2) + '%' : '-' }}
          </div>
          <a-progress
            :percent="runtime.interrupt_ratio != null ? Math.round(runtime.interrupt_ratio * 10000) / 100 : 0"
            :show-info="false"
          />
        </SectionCard>
      </template>

      <a-row :gutter="16">
        <a-col :span="10">
          <ChartCard
            title="Run 终态分布"
            :option="statusOption"
            :empty="!runtime"
            height="300px"
          />
        </a-col>
        <a-col :span="14">
          <ChartCard
            title="缓存命中占比"
            :option="cacheOption"
            :empty="cacheTrend.length === 0"
            empty-text="暂无缓存命中数据"
            height="300px"
          >
            <template #extra>
              <span style="font-size:12px;color:var(--text-3)">
                命中率 {{ cacheTrend[0]?.hit_rate != null ? (cacheTrend[0].hit_rate * 100).toFixed(1) + '%' : '-' }}
                · 节省 ¥{{ (cacheTrend[0]?.estimated_saving ?? 0).toFixed(2) }}
              </span>
            </template>
          </ChartCard>
        </a-col>
      </a-row>
    </a-space>
  </a-spin>
</template>