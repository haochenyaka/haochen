<script setup lang="ts">
/**
 * 页工具栏 —— 左侧筛选/操作，右侧按钮，替换旧 global.css 的 .toolbar 与各页筛选栏。
 */
</script>

<template>
  <div class="page-toolbar">
    <div class="page-toolbar__left">
      <slot />
    </div>
    <div v-if="$slots.right" class="page-toolbar__right">
      <slot name="right" />
    </div>
  </div>
</template>

<style scoped>
.page-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  margin-bottom: 16px;
  gap: 12px;
}

.page-toolbar__left,
.page-toolbar__right {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
</style>