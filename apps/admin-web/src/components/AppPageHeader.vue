<script setup lang="ts">
/**
 * 页头 —— 页标题 + 描述 + 右侧操作区，左侧金色竖条呼应品牌。
 */
defineProps<{
  title: string
  description?: string
}>()
</script>

<template>
  <div class="page-header">
    <div class="page-header__main">
      <h2 class="page-header__title">{{ title }}</h2>
      <p v-if="description" class="page-header__desc">{{ description }}</p>
    </div>
    <div v-if="$slots.actions" class="page-header__actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.page-header__title {
  position: relative;
  margin: 0;
  padding-left: 14px;
  color: var(--text-1);
  font-size: 20px;
  font-weight: 600;
}

.page-header__title::before {
  position: absolute;
  top: 50%;
  left: 0;
  width: 4px;
  height: 20px;
  background: var(--gradient-gold);
  border-radius: 2px;
  transform: translateY(-50%);
  content: '';
}

.page-header__desc {
  margin: 8px 0 0 14px;
  color: var(--text-3);
  font-size: 13px;
}

.page-header__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
</style>