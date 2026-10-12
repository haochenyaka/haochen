<template>
  <view class="page">
    <view class="header">
      <text class="title">今日任务</text>
      <text class="meta">{{ session?.displayName }}</text>
      <view class="header-badge">
        <text class="header-badge__num">{{ pendingCount }}</text>
        <text class="header-badge__label">待处理</text>
      </view>
    </view>

    <view class="tabs">
      <view
        v-for="tab in tabs"
        :key="tab.key"
        class="tab"
        :class="{ active: activeTab === tab.key }"
        @tap="switchTab(tab.key)"
      >
        <text>{{ tab.label }}</text>
      </view>
    </view>

    <view v-if="loading" class="loading">
      <text>加载中…</text>
    </view>

    <hc-empty
      v-else-if="tasks.length === 0"
      :description="`当前没有${activeTabLabel}任务`"
      glyph="✅"
    />

    <view v-else class="list">
      <view
        v-for="task in tasks"
        :key="task.id"
        class="task-card"
        @tap="goDetail(task.id)"
      >
        <view class="task-top">
          <text class="task-type">{{ typeLabel(task.task_type) }}</text>
          <hc-tag :text="statusText(task.status)" :type="statusType(task.status)" />
        </view>
        <text class="task-room">房间 {{ task.room_no }}</text>
        <text class="task-time">{{ formatTime(task.created_at) }}</text>
      </view>
    </view>
  </view>
</template>

<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { ref, computed } from 'vue'
import { fetchTasks, statusLabel, taskTypeLabel, type TaskItem } from '@/api/tasks'
import { requireAuth, useAuthState } from '@/stores/auth'

const { session } = useAuthState()
const loading = ref(false)
const tasks = ref<TaskItem[]>([])
const activeTab = ref('pending')

const tabs = [
  { key: 'pending', label: '待处理' },
  { key: 'in_progress', label: '进行中' },
  { key: 'done', label: '待验收' },
  { key: '', label: '全部' },
]

const activeTabLabel = computed(() => {
  return tabs.find(t => t.key === activeTab.value)?.label || ''
})

const pendingCount = computed(() => {
  return tasks.value.filter(t => ['pending', 'accepted', 'in_progress'].includes(t.status)).length
})

function typeLabel(taskType: string): string {
  return taskTypeLabel(taskType)
}

function statusText(status: string): string {
  return statusLabel(status)
}

/** 任务状态 → 标签语义类型。 */
function statusType(status: string): 'brand' | 'gold' | 'success' | 'warning' | 'error' | 'info' {
  if (status === 'pending') return 'warning'
  if (status === 'accepted' || status === 'in_progress') return 'brand'
  if (status === 'done') return 'gold'
  if (status === 'verified' || status === 'closed') return 'success'
  if (status === 'escalated') return 'error'
  return 'info'
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso)
    return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
  } catch {
    return iso
  }
}

async function loadTasks(): Promise<void> {
  loading.value = true
  try {
    tasks.value = await fetchTasks(activeTab.value || undefined)
  } catch {
    tasks.value = []
  } finally {
    loading.value = false
  }
}

function switchTab(key: string): void {
  activeTab.value = key
  void loadTasks()
}

function goDetail(taskId: string): void {
  uni.navigateTo({ url: `/pages/tasks/detail?id=${taskId}` })
}

onShow(() => {
  if (!requireAuth()) return
  void loadTasks()
})
</script>

<style lang="scss" scoped>
.page {
  padding: 32rpx;
}

.header {
  position: relative;
  display: flex;
  flex-direction: column;
  padding: 40rpx;
  background: linear-gradient(135deg, #1a3a6b, #24487c);
  border-radius: 24rpx;
  color: #ffffff;

  .title { font-size: 40rpx; font-weight: 600; }
  .meta { margin-top: 16rpx; font-size: 26rpx; opacity: 0.9; }

  .header-badge {
    position: absolute;
    top: 40rpx;
    right: 40rpx;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 12rpx 24rpx;
    background: rgba(201, 162, 39, 0.18);
    border: 2rpx solid rgba(201, 162, 39, 0.6);
    border-radius: 16rpx;

    &__num {
      color: #e3c45d;
      font-size: 40rpx;
      font-weight: 700;
      line-height: 1.1;
    }

    &__label {
      margin-top: 2rpx;
      color: rgba(255, 255, 255, 0.75);
      font-size: 20rpx;
    }
  }
}

.tabs {
  display: flex;
  margin: 24rpx 0;
  gap: 16rpx;

  .tab {
    padding: 12rpx 28rpx;
    border-radius: 40rpx;
    background: #ffffff;
    color: #6b7280;
    font-size: 26rpx;
    box-shadow: 0 4rpx 16rpx rgba(26, 58, 107, 0.06);

    &.active {
      background: #1a3a6b;
      color: #ffffff;
      box-shadow: 0 4rpx 12rpx rgba(26, 58, 107, 0.28);
    }
  }
}

.loading {
  text-align: center;
  padding: 80rpx;
  color: #6b7280;
}

.list {
  display: flex;
  flex-direction: column;
  gap: 24rpx;
}

.task-card {
  display: flex;
  flex-direction: column;
  padding: 32rpx;
  background: #ffffff;
  border-radius: 20rpx;
  box-shadow: 0 4rpx 16rpx rgba(26, 58, 107, 0.08);
  gap: 12rpx;

  .task-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .task-type { font-size: 30rpx; font-weight: 600; color: #1f2329; }
  .task-room { color: #4b5563; font-size: 28rpx; }
  .task-time { color: #9ca3af; font-size: 24rpx; }
}
</style>