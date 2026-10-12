<template>
  <view class="page">
    <view class="hero">
      <text class="brand">皓晨保洁</text>
      <text class="slogan">任务接收 · 退房改密留证 · 打卡审核</text>
    </view>
    <view class="card">
      <text class="label">员工工号</text>
      <input
        v-model="staffNo"
        class="field"
        placeholder="请输入员工工号"
        :disabled="pending"
        @confirm="handleLogin"
      >
      <view class="submit">
        <wd-button type="primary" block size="large" :loading="pending" @click="handleLogin">
          {{ pending ? '登录中…' : '登录' }}
        </wd-button>
      </view>
      <text
        v-if="error"
        class="error"
      >
        {{ error }}
      </text>
      <text class="hint">开发态登录：P2 授权基座与 P5 员工账号体系接入后替换。</text>
    </view>
  </view>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { fetchHealth } from '@/api/system'
import { signIn } from '@/stores/auth'

const staffNo = ref('')
const pending = ref(false)
const error = ref('')

async function handleLogin(): Promise<void> {
  const no = staffNo.value.trim()
  if (!no) {
    error.value = '请输入员工工号'
    return
  }
  pending.value = true
  error.value = ''
  try {
    await fetchHealth()
  } catch (err) {
    error.value = `服务不可达：${(err as { message?: string }).message || '未知错误'}`
    pending.value = false
    return
  }
  signIn(no, `保洁 ${no}`)
  pending.value = false
  uni.switchTab({ url: '/pages/tasks/index' })
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
