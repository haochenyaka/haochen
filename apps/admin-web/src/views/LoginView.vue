<script setup lang="ts">
import { reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { useRoute, useRouter } from 'vue-router'
import { login } from '@/api/auth'
import { isAdminRole, signIn } from '@/stores/auth'

const router = useRouter()
const route = useRoute()
const pending = ref(false)
const form = reactive<{ username: string; password: string }>({ username: '', password: '' })

async function handleSubmit(): Promise<void> {
  const username = form.username.trim()
  if (!username || !form.password) {
    message.warning('请输入用户名与密码')
    return
  }
  pending.value = true
  try {
    const payload = await login(username, form.password)
    if (!isAdminRole(payload.user.role)) {
      message.error('该账号无权登录管理后台')
      return
    }
    signIn({
      token: payload.token,
      username: payload.user.username,
      displayName: payload.user.display_name,
      role: payload.user.role,
    })
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/dashboard'
    await router.replace(redirect)
  } catch (error) {
    message.error((error as { message?: string }).message || '登录失败')
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <!-- 装饰光晕 -->
    <div class="glow glow-1" />
    <div class="glow glow-2" />

    <div class="login-card">
      <div class="card-inner">
        <div class="brand">
          <svg class="brand-icon" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
            <rect x="8" y="28" width="48" height="30" rx="3" fill="currentColor" opacity="0.9" />
            <rect x="14" y="34" width="8" height="10" rx="1" fill="white" opacity="0.85" />
            <rect x="28" y="34" width="8" height="10" rx="1" fill="white" opacity="0.85" />
            <rect x="42" y="34" width="8" height="10" rx="1" fill="white" opacity="0.85" />
            <rect x="14" y="48" width="36" height="6" rx="1" fill="white" opacity="0.4" />
            <path d="M32 4L4 28h8v4l20-16 20 16v-4l-8-6.4V12h-4v5.6L32 4z" fill="currentColor" />
            <circle cx="36" cy="18" r="3" fill="white" opacity="0.9" />
          </svg>
          <h1 class="brand-title">皓晨酒店</h1>
          <p class="brand-sub">管理后台</p>
        </div>

        <a-form layout="vertical" class="login-form" @submit.prevent="handleSubmit">
          <a-form-item label="用户名">
            <a-input
              v-model:value="form.username"
              placeholder="请输入用户名"
              autocomplete="username"
              size="large"
              allow-clear
            />
          </a-form-item>
          <a-form-item label="密码">
            <a-input-password
              v-model:value="form.password"
              placeholder="请输入密码"
              autocomplete="current-password"
              size="large"
              @press-enter="handleSubmit"
            />
          </a-form-item>
          <a-button type="primary" block size="large" :loading="pending" @click="handleSubmit">
            登 录
          </a-button>
        </a-form>

        <p class="hint">账号由管理员在「酒店基础数据 → 账号」中维护。</p>
      </div>
    </div>

    <p class="footer">&copy; 2026 皓晨酒店 · 管理后台 v1.0</p>
  </div>
</template>

<style scoped>
/* ───── 页面容器 ───── */
.login-page {
  position: relative;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  background: linear-gradient(135deg, #0a162b 0%, #12284b 40%, #1a3a6b 60%, #0e1f3b 100%);
}

/* ───── 装饰光晕 ───── */
.glow {
  position: absolute;
  border-radius: 50%;
  filter: blur(80px);
  pointer-events: none;
}
.glow-1 {
  width: 400px;
  height: 400px;
  top: -120px;
  right: -80px;
  background: radial-gradient(circle, rgba(201, 162, 39, 0.22), transparent 70%);
}
.glow-2 {
  width: 350px;
  height: 350px;
  bottom: -100px;
  left: -80px;
  background: radial-gradient(circle, rgba(79, 115, 165, 0.30), transparent 70%);
}

/* ───── 登录卡片 ───── */
.login-card {
  position: relative;
  z-index: 1;
  width: 420px;
  max-width: 92vw;
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(24px);
  -webkit-backdrop-filter: blur(24px);
  border: 1px solid rgba(255, 255, 255, 0.10);
  box-shadow: 0 8px 40px rgba(0, 0, 0, 0.3);
  animation: fadeInUp 0.6s ease-out;
}

.card-inner {
  padding: 40px 36px 28px;
}

/* ───── 品牌区域 ───── */
.brand {
  text-align: center;
  margin-bottom: 32px;
}

.brand-icon {
  width: 52px;
  height: 52px;
  color: #c9a227;
  margin-bottom: 12px;
}

.brand-title {
  margin: 0;
  font-size: 24px;
  font-weight: 700;
  color: #f5f3ec;
  letter-spacing: 4px;
}

.brand-sub {
  margin: 4px 0 0;
  font-size: 13px;
  color: #c9a227;
  letter-spacing: 6px;
  opacity: 0.85;
}

/* ───── 表单 ───── */
:deep(.login-form .ant-form-item-label > label) {
  color: rgba(255, 255, 255, 0.75);
  font-size: 13px;
}

:deep(.login-form .ant-input),
:deep(.login-form .ant-input-password) {
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid rgba(255, 255, 255, 0.12);
  color: #f0f4ff;
}

:deep(.login-form .ant-input::placeholder),
:deep(.login-form .ant-input-password input::placeholder) {
  color: rgba(255, 255, 255, 0.3);
}

:deep(.login-form .ant-input-password .ant-input) {
  background: transparent;
}

:deep(.login-form .ant-input-password .ant-input-password-icon) {
  color: rgba(255, 255, 255, 0.5);
}

:deep(.login-form .ant-input:hover),
:deep(.login-form .ant-input-password:hover) {
  border-color: rgba(201, 162, 39, 0.6);
}

:deep(.login-form .ant-input:focus),
:deep(.login-form .ant-input-password:focus-within) {
  border-color: #c9a227;
  box-shadow: 0 0 0 2px rgba(201, 162, 39, 0.15);
}

/* 用户名输入文字改为黑色，去掉 hover/focus 方框 */
:deep(.login-form .ant-form-item:first-child .ant-input) {
  color: #000;
}
:deep(.login-form .ant-form-item:first-child .ant-input:hover),
:deep(.login-form .ant-form-item:first-child .ant-input:focus) {
  border-color: rgba(255, 255, 255, 0.12);
  box-shadow: none;
}

/* 密码输入框去掉所有方框 */
:deep(.login-form .ant-input-password) {
  border: none;
  background: rgba(255, 255, 255, 0.05);
}
:deep(.login-form .ant-input-password .ant-input) {
  border: none;
  background: transparent;
}
:deep(.login-form .ant-input-password:hover),
:deep(.login-form .ant-input-password:focus-within) {
  border: none;
  box-shadow: none;
}

:deep(.login-form .ant-btn-primary) {
  height: 44px;
  font-size: 15px;
  border-radius: 8px;
  background: linear-gradient(135deg, #1a3a6b, #24487c);
  border: none;
  box-shadow: 0 4px 14px rgba(26, 58, 107, 0.35);
  letter-spacing: 4px;
  transition: all 0.25s;
}

:deep(.login-form .ant-btn-primary:hover) {
  transform: translateY(-1px);
  background: linear-gradient(135deg, #24487c, #c9a227);
  box-shadow: 0 6px 20px rgba(201, 162, 39, 0.35);
}

:deep(.login-form .ant-btn-primary:active) {
  transform: translateY(0);
}

/* ───── 提示 ───── */
.hint {
  margin: 20px 0 0;
  text-align: center;
  color: rgba(255, 255, 255, 0.3);
  font-size: 12px;
}

/* ───── 底部 ───── */
.footer {
  position: absolute;
  bottom: 24px;
  color: rgba(255, 255, 255, 0.2);
  font-size: 12px;
  letter-spacing: 1px;
}

/* ───── 入场动画 ───── */
@keyframes fadeInUp {
  from {
    opacity: 0;
    transform: translateY(24px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
</style>