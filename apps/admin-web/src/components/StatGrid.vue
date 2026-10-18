<script setup lang="ts">
/**
 * 指标卡排布容器 —— 自适应换行的栅格，配合 StatCard 使用。
 * 用法：<StatGrid><StatCard .../><StatCard .../></StatGrid>
 */
withDefaults(defineProps<{ column?: number; gutter?: number }>(), {
  column: 4,
  gutter: 16,
})
</script>

<template>
  <div
    class="stat-grid"
    :style="{ gap: `${gutter}px`, '--stat-cols': column }"
  >
    <slot />
  </div>
</template>

<style scoped>
.stat-grid {
  display: grid;
  grid-template-columns: repeat(var(--stat-cols, 4), minmax(0, 1fr));
}

.stat-grid :deep(> *) {
  min-width: 0;
}

@media (max-width: 1200px) {
  .stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 640px) {
  .stat-grid {
    grid-template-columns: 1fr;
  }
}
</style>