<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import {
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  LogoutOutlined,
  BellOutlined,
} from '@ant-design/icons-vue'
import { fetchMe, logout } from '@/api/auth'
import { ADMIN_MENU } from '@/router'
import { ROLE_LABELS, clearSession, useAuthState } from '@/stores/auth'
import { navIconNode } from '@/theme/navIcons'

const { session } = useAuthState()
const route = useRoute()
const router = useRouter()
const collapsed = ref(false)

const menuItems = computed(() =>
  ADMIN_MENU.filter((item) => !item.roles || (session !== null && item.roles.includes(session.role))).map(
    (item) => ({
      key: item.name,
      icon: navIconNode(item.name),
      label: item.title,
    }),
  ),
)

const selectedKeys = computed(() => [String(route.name ?? 'dashboard')])

const displayName = computed(() => session?.displayName || session?.username || '')

const breadcrumbItems = computed(() => {
  const items = [{ title: '首页', path: '/dashboard' }]
  const title = route.meta.title
  if (title && title !== '仪表盘') {
    items.push({ title, path: route.path })
  }
  return items
})

function handleMenuClick({ key }: { key: string | number }): void {
  void router.push({ name: String(key) })
}

async function handleLogout(): Promise<void> {
  try {
    await logout()
  } catch {
    // 退出以本地清会话为准
  }
  clearSession()
  void router.replace({ name: 'login' })
}

onMounted(async () => {
  try {
    await fetchMe()
  } catch (error) {
    if ((error as { status?: number }).status === 401) {
      message.warning('会话已失效，请重新登录')
      clearSession()
      void router.replace({ name: 'login' })
    }
  }
})
</script>

<template>
  <a-layout class="shell">
    <!-- 侧边栏 -->
    <a-layout-sider
      v-model:collapsed="collapsed"
      collapsible
      :trigger="null"
      theme="dark"
      width="220"
      class="sider"
    >
      <div class="sider-header">
        <svg class="sider-logo" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
          <rect x="8" y="28" width="48" height="30" rx="3" fill="currentColor" opacity="0.9" />
          <rect x="14" y="34" width="8" height="10" rx="1" fill="white" opacity="0.85" />
          <rect x="28" y="34" width="8" height="10" rx="1" fill="white" opacity="0.85" />
          <rect x="42" y="34" width="8" height="10" rx="1" fill="white" opacity="0.85" />
          <rect x="14" y="48" width="36" height="6" rx="1" fill="white" opacity="0.4" />
          <path d="M32 4L4 28h8v4l20-16 20 16v-4l-8-6.4V12h-4v5.6L32 4z" fill="currentColor" />
          <circle cx="36" cy="18" r="3" fill="white" opacity="0.9" />
        </svg>
        <span v-show="!collapsed" class="sider-title">皓晨管理后台</span>
      </div>

      <a-menu
        theme="dark"
        mode="inline"
        :selected-keys="selectedKeys"
        :items="menuItems"
        @click="handleMenuClick"
      />
    </a-layout-sider>

    <!-- 主区域 -->
    <a-layout>
      <!-- 头部 -->
      <a-layout-header class="header">
        <div class="header-left">
          <span class="toggle-btn" @click="collapsed = !collapsed">
            <MenuFoldOutlined v-if="!collapsed" />
            <MenuUnfoldOutlined v-else />
          </span>
          <a-breadcrumb>
            <a-breadcrumb-item v-for="item in breadcrumbItems" :key="item.path">
              <template v-if="item.path !== route.path">
                <router-link :to="item.path">{{ item.title }}</router-link>
              </template>
              <template v-else>{{ item.title }}</template>
            </a-breadcrumb-item>
          </a-breadcrumb>
        </div>

        <div class="header-right">
          <BellOutlined class="icon-btn" />
          <a-divider type="vertical" />
          <a-tag class="role-tag">
            {{ session ? ROLE_LABELS[session.role] : '' }}
          </a-tag>
          <span class="user-name">{{ displayName }}</span>
          <a-button
            type="text"
            class="logout-btn"
            @click="handleLogout"
          >
            <template #icon><LogoutOutlined /></template>
            退出
          </a-button>
        </div>
      </a-layout-header>

      <!-- 内容 -->
      <a-layout-content class="content">
        <router-view v-slot="{ Component }">
          <transition name="page-fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </a-layout-content>
    </a-layout>
  </a-layout>
</template>

<style scoped>
.shell {
  min-height: 100vh;
}

/* ───── 侧边栏 ───── */
.sider {
  background: linear-gradient(180deg, var(--brand-9) 0%, var(--brand-8) 100%);
  box-shadow: 2px 0 8px rgba(26, 58, 107, 0.12);
}

.sider :deep(.ant-layout-sider-children) {
  background: transparent;
}

.sider-header {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.sider-logo {
  width: 28px;
  height: 28px;
  color: var(--gold-6);
  flex-shrink: 0;
}

.sider-title {
  color: #f3f1ea;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 2px;
  white-space: nowrap;
}

:deep(.ant-menu) {
  background: transparent !important;
}

:deep(.ant-menu-item) {
  margin: 2px 8px !important;
  border-radius: var(--radius-sm);
  position: relative;
}

:deep(.ant-menu-item-selected) {
  background: var(--gradient-brand) !important;
  box-shadow: 0 4px 12px rgba(26, 58, 107, 0.35);
}

/* 选中项金色左条 */
:deep(.ant-menu-item-selected)::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 18px;
  border-radius: 2px;
  background: var(--gold-6);
}

/* ───── 头部 ───── */
.header {
  position: sticky;
  top: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  height: 56px;
  background: rgba(255, 255, 255, 0.92);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--divider);
  box-shadow: 0 1px 4px rgba(26, 58, 107, 0.05);
  z-index: 10;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.toggle-btn {
  font-size: 18px;
  color: var(--text-3);
  cursor: pointer;
  display: flex;
  align-items: center;
  transition: color 0.2s;
}

.toggle-btn:hover {
  color: var(--brand-6);
}

.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.icon-btn {
  font-size: 18px;
  color: var(--text-4);
  cursor: pointer;
  transition: color 0.2s;
}

.icon-btn:hover {
  color: var(--brand-6);
}

.role-tag {
  font-size: 12px;
  color: var(--brand-6);
  background: var(--brand-1);
  border-color: transparent;
}

.user-name {
  color: var(--text-2);
  font-size: 14px;
  font-weight: 500;
}

.logout-btn {
  color: var(--text-4);
  font-size: 13px;
}

.logout-btn:hover {
  color: var(--error) !important;
}

/* ───── 内容区 ───── */
.content {
  margin: 0;
  padding: 24px;
  background: var(--bg-layout);
  min-height: calc(100vh - 56px);
}

/* ───── 页面过渡 ───── */
.page-fade-enter-active,
.page-fade-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}

.page-fade-enter-from {
  opacity: 0;
  transform: translateY(8px);
}

.page-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>