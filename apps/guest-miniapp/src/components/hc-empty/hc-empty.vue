<template>
  <view class="hc-empty">
    <view class="hc-empty__icon">
      <text class="hc-empty__glyph">{{ glyph }}</text>
    </view>
    <text class="hc-empty__desc">{{ description }}</text>
    <button v-if="actionText" class="hc-empty__btn" @click="$emit('action')">
      {{ actionText }}
    </button>
  </view>
</template>

<script setup lang="ts">
/** 统一空态。 */
withDefaults(
  defineProps<{
    description?: string
    actionText?: string
    glyph?: string
  }>(),
  { description: '暂无数据', actionText: undefined, glyph: '🗂' },
)

defineEmits<{ action: [] }>()
</script>

<style lang="scss" scoped>
.hc-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 120rpx 32rpx;
}

.hc-empty__icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 120rpx;
  height: 120rpx;
  margin-bottom: 24rpx;
  background: #eef2f8;
  border-radius: 50%;
}

.hc-empty__glyph {
  font-size: 52rpx;
}

.hc-empty__desc {
  color: #9ca3af;
  font-size: 26rpx;
}

.hc-empty__btn {
  margin-top: 40rpx;
  width: 300rpx;
  color: #ffffff;
  font-size: 28rpx;
  background: #1a3a6b;
  border-radius: 16rpx;
}
</style>