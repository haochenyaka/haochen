<template>
  <view class="page">
    <view class="hero">
      <view class="avatar">
        <text class="avatar__text">{{ (session?.displayName || '洁').slice(0, 2) }}</text>
      </view>
      <view class="hero__info">
        <text class="hero__name">{{ session?.displayName }}</text>
        <text class="hero__meta">工号：{{ session?.username }}</text>
      </view>
    </view>

    <view class="card">
      <view class="row">
        <text class="row__label">登录时间</text>
        <text class="row__value">{{ loggedInAtText }}</text>
      </view>
    </view>

    <view class="card card--action" @click="handleLogout">
      <text class="logout">退出登录</text>
    </view>

    <text class="version">皓晨保洁小程序 · {{ version }}</text>
  </view>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { requireAuth, signOut, useAuthState } from '@/stores/auth'

const { session } = useAuthState()
const version = '0.1.0'

const loggedInAtText = computed(() => {
  const raw = session?.loggedInAt
  if (!raw) {
    return '—'
  }
  return raw.replace('T', ' ').slice(0, 19)
})

function handleLogout(): void {
  signOut()
}

onShow(() => {
  requireAuth()
})
</script>

<style lang="scss" scoped>
.page {
  padding: 32rpx;
}

.hero {
  display: flex;
  align-items: center;
  gap: 28rpx;
  padding: 48rpx 40rpx;
  color: #ffffff;
  background: linear-gradient(135deg, #1a3a6b, #24487c);
  border-radius: 24rpx;

  &__info {
    display: flex;
    flex-direction: column;
  }

  &__name {
    font-size: 36rpx;
    font-weight: 600;
  }

  &__meta {
    margin-top: 12rpx;
    font-size: 24rpx;
    opacity: 0.85;
  }
}

.avatar {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 96rpx;
  height: 96rpx;
  background: rgba(255, 255, 255, 0.18);
  border: 2rpx solid rgba(201, 162, 39, 0.6);
  border-radius: 50%;

  &__text {
    font-size: 34rpx;
    font-weight: 600;
    color: #e3c45d;
  }
}

.card {
  display: flex;
  flex-direction: column;
  margin-top: 24rpx;
  padding: 36rpx 32rpx;
  background-color: #ffffff;
  border-radius: 20rpx;
  box-shadow: 0 4rpx 16rpx rgba(26, 58, 107, 0.08);

  &--action {
    align-items: center;
  }
}

.row {
  display: flex;
  align-items: center;
  justify-content: space-between;

  &__label {
    color: #6b7280;
    font-size: 26rpx;
  }

  &__value {
    color: #1f2329;
    font-size: 26rpx;
  }
}

.logout {
  color: #d03050;
  font-size: 30rpx;
}

.version {
  display: block;
  margin-top: 48rpx;
  color: #9ca3af;
  font-size: 24rpx;
  text-align: center;
}
</style>
