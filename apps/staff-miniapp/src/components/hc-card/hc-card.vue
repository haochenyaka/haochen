<template>
  <view class="hc-card">
    <slot />
  </view>
</template>

<script setup lang="ts">
/** 统一白底圆角阴影卡片。 */
</script>

<style lang="scss" scoped>
.hc-card {
  padding: 32rpx;
  background: #ffffff;
  border-radius: 20rpx;
  box-shadow: 0 4rpx 16rpx rgba(26, 58, 107, 0.08);
}
</style>