<template>
  <view class="page">
    <view class="hero">
      <text class="brand">皓晨酒店</text>
      <text class="slogan">无人酒店 · 全流程智能服务</text>
    </view>
    <!-- 微信小程序：自动触发登录 -->
    <view v-if="isMp" class="card">
      <wd-button type="primary" block size="large" :loading="pending" @click="handleWechatLogin">
        {{ pending ? '登录中…' : '微信一键登录' }}
      </wd-button>
      <text v-if="error" class="error">{{ error }}</text>
      <text class="hint">点击后弹出微信授权</text>
    </view>
    <!-- H5 开发态：用户名密码登录 -->
    <view v-else class="card">
      <text class="label">用户名</text>
      <input
        v-model="username"
        class="field"
        placeholder="开发用账号（如 guest-1001）"
        :disabled="pending"
        @confirm="handleDevLogin"
      >
      <text class="label">密码</text>
      <input
        v-model="password"
        class="field"
        type="password"
        placeholder="开发用密码"
        :disabled="pending"
        @confirm="handleDevLogin"
      >
      <view class="submit">
        <wd-button type="primary" block size="large" :loading="pending" @click="handleDevLogin">
          {{ pending ? '登录中…' : '登录' }}
        </wd-button>
      </view>
      <text v-if="error" class="error">{{ error }}</text>
      <text class="hint">H5 开发态登录</text>
    </view>
  </view>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { wechatLogin, devLogin } from '@/stores/auth'

const isMp = typeof (globalThis as Record<string, unknown>).wx === 'object'

const username = ref('')
const password = ref('')
const pending = ref(false)
const error = ref('')

async function handleWechatLogin(): Promise<void> {
  pending.value = true
  error.value = ''
  try {
    const { code } = await uni.login()
    if (!code) {
      error.value = '微信登录失败：未获取到授权码'
      pending.value = false
      return
    }
    await wechatLogin(code)
    uni.switchTab({ url: '/pages/home/index' })
  } catch (err) {
    const msg = (err as { message?: string }).message || (err as { errMsg?: string }).errMsg || '未知错误'
    error.value = `登录失败：${msg}`
  }
  pending.value = false
}

async function handleDevLogin(): Promise<void> {
  const name = username.value.trim()
  const pwd = password.value.trim()
  if (!name || !pwd) {
    error.value = '请输入用户名和密码'
    return
  }
  pending.value = true
  error.value = ''
  try {
    await devLogin(name, pwd)
    uni.switchTab({ url: '/pages/home/index' })
  } catch (err) {
    const msg = (err as { message?: string }).message || '登录失败'
    error.value = msg
  }
  pending.value = false
}
</script>

<style lang="scss" scoped>
.page {
  min-height: 100vh;
  padding: 120rpx 48rpx 48rpx;
  box-sizing: border-box;
  background: linear-gradient(160deg, #1a3a6b 0%, #24487c 240rpx, #f7f5f0 240rpx);
}

.hero {
  display: flex;
  flex-direction: column;
  margin-bottom: 72rpx;

  .brand {
    font-size: 64rpx;
    font-weight: 600;
    letter-spacing: 4rpx;
    color: #ffffff;
  }

  .slogan {
    margin-top: 16rpx;
    color: rgba(255, 255, 255, 0.75);
    font-size: 26rpx;
  }
}

.card {
  display: flex;
  flex-direction: column;
  padding: 48rpx 40rpx;
  background-color: #ffffff;
  border-radius: 24rpx;
  box-shadow: 0 8rpx 28rpx rgba(26, 58, 107, 0.12);

  .label {
    color: #6b7280;
    font-size: 26rpx;
    margin-top: 24rpx;

    &:first-child {
      margin-top: 0;
    }
  }

  .field {
    height: 88rpx;
    margin-top: 12rpx;
    padding: 0 24rpx;
    background-color: #f4f2ec;
    border-radius: 16rpx;
    font-size: 28rpx;
  }

  .submit {
    margin-top: 40rpx;
  }

  .error {
    margin-top: 24rpx;
    color: #d03050;
    font-size: 26rpx;
  }

  .hint {
    margin-top: 32rpx;
    color: #9ca3af;
    font-size: 24rpx;
  }
}
</style>