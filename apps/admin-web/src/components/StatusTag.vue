<script setup lang="ts">
/**
 * 状态标签 —— 集中 状态→色 映射，替换各处零散 <a-tag :color> 与硬编码色。
 *
 * 支持三类：
 *  - 通用语义：status 传 'success' | 'warning' | 'error' | 'processing' | 'default'
 *  - 业务状态：传 statusMap 做字符串→语义映射（如 { completed: 'success' }）
 */
import { computed } from 'vue'

type Semantic = 'success' | 'warning' | 'error' | 'processing' | 'default'

const props = withDefaults(
  defineProps<{
    status: string
    label?: string
    statusMap?: Record<string, Semantic>
  }>(),
  { label: undefined, statusMap: undefined },
)

const SEMANTIC_COLORS: Record<Semantic, string> = {
  success: 'success',
  warning: 'warning',
  error: 'error',
  processing: 'processing',
  default: 'default',
}

const resolved = computed<Semantic>(() => {
  const mapped = props.statusMap?.[props.status]
  if (mapped) return mapped
  if (props.status in SEMANTIC_COLORS) return props.status as Semantic
  return 'processing'
})

const text = computed(() => props.label ?? props.status)
</script>

<template>
  <a-tag :color="SEMANTIC_COLORS[resolved]" class="status-tag">{{ text }}</a-tag>
</template>

<style scoped>
.status-tag {
  border-radius: var(--radius-sm);
  font-weight: 500;
}
</style>