<script setup lang="ts">
/**
 * ECharts 基座封装 —— 负责实例生命周期、自适应与品牌默认样式。
 * 用法：<BaseChart :option="option" height="280px" />
 */
import { onBeforeUnmount, onMounted, shallowRef, watch } from 'vue'
import type { EChartsCoreOption } from 'echarts/core'
import { CHART_INK, CHART_PALETTE, echarts } from './echarts'

const props = withDefaults(
  defineProps<{
    option: EChartsCoreOption
    height?: string
  }>(),
  { height: '280px' },
)

const root = shallowRef<HTMLDivElement | null>(null)
const chart = shallowRef<ReturnType<typeof echarts.init> | null>(null)
let observer: ResizeObserver | null = null

/** 合并品牌默认样式（色板 + 文本/网格色），再叠加调用方 option。 */
function resolveOption(option: EChartsCoreOption): EChartsCoreOption {
  return {
    color: CHART_PALETTE,
    textStyle: { color: CHART_INK.text, fontFamily: 'inherit' },
    ...option,
  }
}

function render(): void {
  if (!chart.value) return
  chart.value.setOption(resolveOption(props.option), true)
}

onMounted(() => {
  if (!root.value) return
  chart.value = echarts.init(root.value)
  render()
  observer = new ResizeObserver(() => chart.value?.resize())
  observer.observe(root.value)
})

watch(
  () => props.option,
  () => render(),
  { deep: true },
)

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  chart.value?.dispose()
  chart.value = null
})
</script>

<template>
  <div ref="root" class="base-chart" :style="{ height }" />
</template>

<style scoped>
.base-chart {
  width: 100%;
}
</style>