<template>
  <view class="page">
    <view v-if="loading" class="loading">
      <text>加载中…</text>
    </view>

    <view v-else-if="!task" class="empty">
      <text class="empty-title">任务不存在</text>
    </view>

    <view v-else class="detail">
      <view class="section">
        <view class="section-header">
          <text class="task-type">{{ typeLabel(task.task_type) }}</text>
          <text class="task-status" :class="'status-' + task.status">{{ statusText(task.status) }}</text>
        </view>
        <text class="room-label">房间 {{ task.room_no }}</text>
        <text class="time-label">创建时间：{{ formatTime(task.created_at) }}</text>
      </view>

      <!-- 退房清洁：有当前密码 -->
      <view v-if="task.task_type === 'checkout_clean'" class="section">
        <text class="section-title">退房清洁说明</text>
        <text class="section-desc">清洁完成后，请修改门锁密码并拍照留证。</text>
      </view>

      <!-- 呼叫清洁 -->
      <view v-if="task.task_type === 'call_clean' && task.payload" class="section">
        <text class="section-title">清洁备注</text>
        <text class="section-desc">{{ (task.payload as Record<string, unknown>).note as string || '无' }}</text>
      </view>

      <!-- 操作按钮组 -->
      <view class="actions">
        <button
          v-if="task.status === 'pending'"
          class="btn btn-primary"
          @tap="handleAccept"
        >接单</button>

        <button
          v-if="task.status === 'accepted'"
          class="btn btn-primary"
          @tap="handleStart"
        >开始清洁</button>

        <button
          v-if="task.status === 'in_progress'"
          class="btn btn-primary"
          @tap="handleFinish"
        >完成清洁</button>

        <button
          v-if="task.status === 'done' && task.task_type === 'checkout_clean'"
          class="btn btn-warning"
          @tap="handleVerifyWithRotation"
        >确认已修改密码并验收</button>

        <button
          v-if="task.status === 'done' && task.task_type === 'call_clean'"
          class="btn btn-primary"
          @tap="handleVerify"
        >验收通过</button>

        <button
          v-if="task.status === 'verified'"
          class="btn btn-primary"
          @tap="handleClose"
        >关闭任务</button>
      </view>

      <!-- 操作反馈 -->
      <view v-if="error" class="error-toast">
        <text>{{ error }}</text>
      </view>
      <view v-if="success" class="success-toast">
        <text>{{ success }}</text>
      </view>
    </view>
  </view>
</template>

<script setup lang="ts">
import { onLoad } from '@dcloudio/uni-app'
import { ref } from 'vue'
import {
  fetchTaskDetail,
  acceptTask,
  startTask,
  finishTask,
  verifyTask,
  closeTask,
  statusLabel,
  taskTypeLabel,
  type TaskItem,
} from '@/api/tasks'
import { requireAuth } from '@/stores/auth'

const task = ref<TaskItem | null>(null)
const loading = ref(true)
const error = ref('')
const success = ref('')

function typeLabel(taskType: string): string {
  return taskTypeLabel(taskType)
}

function statusText(status: string): string {
  return statusLabel(status)
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso)
    return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
  } catch {
    return iso
  }
}

async function loadTask(taskId: string): Promise<void> {
  loading.value = true
  try {
    task.value = await fetchTaskDetail(taskId)
  } catch {
    task.value = null
  } finally {
    loading.value = false
  }
}

async function handleAccept(): Promise<void> {
  if (!task.value) return
  error.value = ''
  success.value = ''
  try {
    task.value = await acceptTask(task.value.id)
    success.value = '已接单'
  } catch (e) {
    error.value = (e as { message?: string }).message || '接单失败'
  }
}

async function handleStart(): Promise<void> {
  if (!task.value) return
  error.value = ''
  success.value = ''
  try {
    task.value = await startTask(task.value.id)
    success.value = '已开始清洁'
  } catch (e) {
    error.value = (e as { message?: string }).message || '操作失败'
  }
}

async function handleFinish(): Promise<void> {
  if (!task.value) return
  error.value = ''
  success.value = ''
  try {
    task.value = await finishTask(task.value.id)
    success.value = '清洁已完成，等待验收'
  } catch (e) {
    error.value = (e as { message?: string }).message || '操作失败'
  }
}

async function handleVerify(): Promise<void> {
  if (!task.value) return
  error.value = ''
  success.value = ''
  try {
    task.value = await verifyTask(task.value.id)
    success.value = '已验收通过'
  } catch (e) {
    error.value = (e as { message?: string }).message || '验收失败'
  }
}

async function handleVerifyWithRotation(): Promise<void> {
  if (!task.value) return
  error.value = ''
  success.value = ''
  try {
    task.value = await verifyTask(task.value.id, { password_rotated: true })
    success.value = '密码已修改，验收通过，退房完成'
  } catch (e) {
    error.value = (e as { message?: string }).message || '验收失败'
  }
}

async function handleClose(): Promise<void> {
  if (!task.value) return
  error.value = ''
  success.value = ''
  try {
    task.value = await closeTask(task.value.id)
    success.value = '任务已关闭'
  } catch (e) {
    error.value = (e as { message?: string }).message || '关闭失败'
  }
}

onLoad((options) => {
  if (!requireAuth()) return
  const taskId = options?.id as string
  if (taskId) {
    void loadTask(taskId)
  } else {
    loading.value = false
  }
})
</script>

<style lang="scss" scoped>
.page {
  padding: 32rpx;
}

.loading, .empty {
  text-align: center;
  padding: 160rpx;
  color: #6b7280;
}

.section {
  padding: 32rpx;
  margin-bottom: 24rpx;
  background: #ffffff;
  border-radius: 20rpx;
  box-shadow: 0 4rpx 16rpx rgba(26, 58, 107, 0.08);
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16rpx;

  .task-type { font-size: 36rpx; font-weight: 600; color: #1f2329; }
  .task-status { font-size: 28rpx; }

  .status-pending { color: #d98a1f; }
  .status-accepted { color: #1a3a6b; }
  .status-in_progress { color: #4f73a5; }
  .status-done { color: #c9a227; }
  .status-verified { color: #2e9e5b; }
  .status-closed { color: #6b7280; }
  .status-escalated { color: #d03050; }
}

.room-label {
  display: block;
  font-size: 32rpx;
  font-weight: 500;
  color: #4b5563;
}

.time-label {
  display: block;
  margin-top: 8rpx;
  color: #9ca3af;
  font-size: 24rpx;
}

.section-title {
  display: block;
  margin-bottom: 12rpx;
  font-size: 28rpx;
  font-weight: 600;
  color: #374151;
}

.section-desc {
  display: block;
  color: #6b7280;
  font-size: 26rpx;
  line-height: 1.6;
}

.actions {
  display: flex;
  flex-direction: column;
  gap: 24rpx;
  margin-top: 32rpx;
}

.btn {
  padding: 28rpx;
  border-radius: 16rpx;
  font-size: 30rpx;
  font-weight: 600;
  text-align: center;
  border: none;
}

.btn-primary {
  background: #1a3a6b;
  color: #ffffff;
}

.btn-warning {
  background: #c9a227;
  color: #ffffff;
}

.error-toast {
  margin-top: 24rpx;
  padding: 24rpx;
  background: #fdeef1;
  border-radius: 16rpx;
  color: #d03050;
  font-size: 26rpx;
  text-align: center;
}

.success-toast {
  margin-top: 24rpx;
  padding: 24rpx;
  background: #eaf7ef;
  border-radius: 16rpx;
  color: #2e9e5b;
  font-size: 26rpx;
  text-align: center;
}
</style>