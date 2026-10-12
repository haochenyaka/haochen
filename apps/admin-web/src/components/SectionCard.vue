<script setup lang="ts">
/**
 * 统一内容卡片壳 —— 全站替换裸 <a-card :bordered="false">。
 * 承载一致的圆角、藏青阴影、内边距与标题排版。
 */
withDefaults(
  defineProps<{
    title?: string
    /** 紧凑内边距（表格/列表容器用） */
    compact?: boolean
  }>(),
  { title: undefined, compact: false },
)
</script>

<template>
  <a-card :bordered="false" class="section-card" :body-style="compact ? { padding: '12px 16px' } : undefined">
    <template v-if="title || $slots.extra" #title>
      <div class="section-card__head">
        <span class="section-card__title">{{ title }}</span>
        <span v-if="$slots.extra" class="section-card__extra"><slot name="extra" /></span>
      </div>
    </template>
    <slot />
  </a-card>
</template>

<style scoped>
.section-card {
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-card);
}

.section-card :deep(.ant-card-head) {
  min-height: 52px;
  border-bottom: 1px solid var(--divider);
}

.section-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.section-card__title {
  position: relative;
  padding-left: 12px;
  color: var(--text-1);
  font-size: 16px;
  font-weight: 600;
}

.section-card__title::before {
  position: absolute;
  top: 50%;
  left: 0;
  width: 3px;
  height: 16px;
  background: var(--gold-6);
  border-radius: 2px;
  transform: translateY(-50%);
  content: '';
}

.section-card__extra {
  color: var(--text-3);
  font-weight: 400;
}
</style>