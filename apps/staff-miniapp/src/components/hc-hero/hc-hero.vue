<template>
  <view class="hc-hero">
    <slot>
      <text v-if="title" class="hc-hero__title">{{ title }}</text>
      <text v-if="subtitle" class="hc-hero__sub">{{ subtitle }}</text>
    </slot>
  </view>
</template>

<script setup lang="ts">
/** 顶部品牌渐变 hero 卡。 */
defineProps<{
  title?: string
  subtitle?: string
}>()
</script>

<style lang="scss" scoped>
.hc-hero {
  display: flex;
  flex-direction: column;
  padding: 48rpx 40rpx;
  color: #ffffff;
  background: linear-gradient(135deg, #1a3a6b, #24487c);
  border-radius: 24rpx;
}

.hc-hero__title {
  font-size: 40rpx;
  font-weight: 600;
}

.hc-hero__sub {
  margin-top: 16rpx;
  font-size: 26rpx;
  opacity: 0.9;
}
</style>