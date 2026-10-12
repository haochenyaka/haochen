<template>
  <text class="hc-section-title">{{ title }}</text>
</template>

<script setup lang="ts">
/** 区块小标题。 */
defineProps<{ title: string }>()
</script>

<style lang="scss" scoped>
.hc-section-title {
  display: block;
  margin: 40rpx 8rpx 20rpx;
  color: #6b7280;
  font-size: 26rpx;
  font-weight: 500;
}
</style>