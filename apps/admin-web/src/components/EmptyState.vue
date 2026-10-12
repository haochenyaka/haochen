<script setup lang="ts">
/**
 * 统一空态 —— 图标 + 描述 + 可选行动按钮，替换 8+ 处裸 <a-empty>。
 */
import { InboxOutlined } from '@ant-design/icons-vue'

withDefaults(
  defineProps<{
    description?: string
    actionText?: string
  }>(),
  { description: '暂无数据', actionText: undefined },
)

defineEmits<{ action: [] }>()
</script>

<template>
  <div class="empty-state">
    <component :is="InboxOutlined" class="empty-state__icon" />
    <p class="empty-state__desc">{{ description }}</p>
    <a-button v-if="actionText" type="primary" size="small" @click="$emit('action')">
      {{ actionText }}
    </a-button>
  </div>
</template>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 16px;
  gap: 12px;
}

.empty-state__icon {
  color: var(--brand-3);
  font-size: 44px;
}

.empty-state__desc {
  margin: 0;
  color: var(--text-4);
  font-size: 13px;
}
</style>