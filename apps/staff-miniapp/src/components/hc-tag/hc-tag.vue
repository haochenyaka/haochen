<template>
  <text class="hc-tag" :class="`hc-tag--${type}`">{{ text }}</text>
</template>

<script setup lang="ts">
/** 状态标签：藏青 / 金 / 语义三态。 */
withDefaults(
  defineProps<{
    text: string
    type?: 'brand' | 'gold' | 'success' | 'warning' | 'error' | 'info'
  }>(),
  { type: 'brand' },
)
</script>

<style lang="scss" scoped>
.hc-tag {
  display: inline-block;
  padding: 4rpx 16rpx;
  font-size: 22rpx;
  border-radius: 8rpx;
}

.hc-tag--brand {
  color: #1a3a6b;
  background: #eef2f8;
}

.hc-tag--gold {
  color: #8a6d15;
  background: #fbf6e7;
}

.hc-tag--success {
  color: #2e9e5b;
  background: #eaf7ef;
}

.hc-tag--warning {
  color: #b06d12;
  background: #fdf4e6;
}

.hc-tag--error {
  color: #d03050;
  background: #fdeef1;
}

.hc-tag--info {
  color: #6b7280;
  background: #f2f3f5;
}
</style>