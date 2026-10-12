<script setup lang="ts">
/**
 * 图表卡片 —— SectionCard(标题) + BaseChart + 空态/加载 组合壳。
 */
import type { EChartsCoreOption } from 'echarts/core'
import SectionCard from '../SectionCard.vue'
import EmptyState from '../EmptyState.vue'
import BaseChart from './BaseChart.vue'

withDefaults(
  defineProps<{
    title: string
    option: EChartsCoreOption
    height?: string
    loading?: boolean
    /** 无数据时展示空态 */
    empty?: boolean
    emptyText?: string
  }>(),
  { height: '280px', loading: false, empty: false, emptyText: '暂无数据' },
)
</script>

<template>
  <SectionCard :title="title">
    <template v-if="$slots.extra" #extra><slot name="extra" /></template>
    <a-spin :spinning="loading">
      <EmptyState v-if="empty" :description="emptyText" />
      <BaseChart v-else :option="option" :height="height" />
    </a-spin>
  </SectionCard>
</template>