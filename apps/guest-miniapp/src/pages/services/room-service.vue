<template>
  <view class="page">
    <view class="section">
      <text class="section-title">呼叫清洁</text>
      <text class="section-desc">通知保洁人员上门清洁房间</text>
      <input v-model="cleanRoomNo" class="input" placeholder="房间号（如 1206）" />
      <input v-model="cleanNote" class="input" placeholder="备注（可选）" />
      <button class="btn btn-primary" :disabled="!cleanRoomNo" @tap="handleClean">
        呼叫清洁
      </button>
      <text v-if="cleanResult" class="success">{{ cleanResult }}</text>
    </view>

    <view class="section">
      <text class="section-title">物品配送</text>
      <text class="section-desc">机器人配送物品到房间</text>
      <input v-model="deliveryItem" class="input" placeholder="物品名称" />
      <input v-model="deliveryQuantity" class="input" placeholder="数量" type="number" />
      <input v-model="deliveryRoomNo" class="input" placeholder="房间号（如 1206）" />
      <button class="btn btn-primary" :disabled="!deliveryItem" @tap="handleDelivery">
        呼叫配送
      </button>
      <text v-if="deliveryResult" class="success">{{ deliveryResult }}</text>
    </view>

    <view class="section">
      <text class="section-title">我的配送</text>
      <text class="section-desc">查看最近的配送记录</text>
      <button class="btn btn-default" @tap="loadDeliveries">刷新</button>
      <view v-if="deliveries.length > 0" class="delivery-list">
        <view v-for="d in deliveries" :key="d.id" class="delivery-item">
          <text class="delivery-name">{{ d.item.name }} × {{ d.item.quantity }}</text>
          <text class="delivery-status">{{ statusText(d.status) }}</text>
          <text class="delivery-room">{{ d.room_no }}</text>
        </view>
      </view>
      <text v-else class="empty-text">暂无配送记录</text>
    </view>

    <view class="section">
      <text class="section-title">投诉与反馈</text>
      <input v-model="complaintContent" class="input textarea" placeholder="请描述您的问题或建议" />
      <button class="btn btn-danger" :disabled="!complaintContent" @tap="handleComplaint">
        提交反馈
      </button>
      <text v-if="complaintResult" class="success">{{ complaintResult }}</text>
    </view>
  </view>
</template>

<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { ref } from 'vue'
import { requestDelivery, listDeliveries, requestClean, submitComplaint, type DeliveryResponse } from '@/api/services'
import { requireAuth } from '@/stores/auth'

// Clean
const cleanRoomNo = ref('')
const cleanNote = ref('')
const cleanResult = ref('')

// Delivery
const deliveryItem = ref('')
const deliveryQuantity = ref('1')
const deliveryRoomNo = ref('')
const deliveryResult = ref('')
const deliveries = ref<DeliveryResponse[]>([])

// Complaint
const complaintContent = ref('')
const complaintResult = ref('')

const STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  dispatching: '调度中',
  delivering: '配送中',
  arrived: '已到达',
  delivered: '已送达',
  failed: '配送失败',
}

function statusText(status: string): string {
  return STATUS_LABELS[status] || status
}

async function handleClean(): Promise<void> {
  cleanResult.value = ''
  try {
    const result = await requestClean(cleanRoomNo.value, cleanNote.value || undefined)
    cleanResult.value = `清洁请求已提交（任务编号：${result.id.slice(0, 8)}…）`
    cleanRoomNo.value = ''
    cleanNote.value = ''
  } catch (e) {
    cleanResult.value = (e as { message?: string }).message || '请求失败'
  }
}

async function handleDelivery(): Promise<void> {
  deliveryResult.value = ''
  try {
    const qty = parseInt(deliveryQuantity.value || '1', 10)
    const result = await requestDelivery(deliveryItem.value, qty, deliveryRoomNo.value || undefined)
    deliveryResult.value = `配送请求已提交（编号：${result.id.slice(0, 8)}…）`
    deliveryItem.value = ''
  } catch (e) {
    deliveryResult.value = (e as { message?: string }).message || '请求失败'
  }
}

async function loadDeliveries(): Promise<void> {
  try {
    deliveries.value = await listDeliveries()
  } catch {
    deliveries.value = []
  }
}

async function handleComplaint(): Promise<void> {
  complaintResult.value = ''
  try {
    const result = await submitComplaint(complaintContent.value)
    complaintResult.value = `感谢您的反馈（编号：${result.id.slice(0, 8)}…）`
    complaintContent.value = ''
  } catch (e) {
    complaintResult.value = (e as { message?: string }).message || '提交失败'
  }
}

onShow(() => {
  if (!requireAuth()) return
  void loadDeliveries()
})
</script>

<style lang="scss" scoped>
.page {
  padding: 32rpx;
}

.section {
  padding: 32rpx;
  margin-bottom: 24rpx;
  background: #ffffff;
  border-radius: 20rpx;
  box-shadow: 0 4rpx 16rpx rgba(26, 58, 107, 0.08);

  .section-title {
    display: block;
    font-size: 32rpx;
    font-weight: 600;
    color: #1f2329;
    margin-bottom: 8rpx;
  }

  .section-desc {
    display: block;
    color: #6b7280;
    font-size: 26rpx;
    margin-bottom: 20rpx;
  }
}

.input {
  display: block;
  width: 100%;
  margin-bottom: 16rpx;
  padding: 20rpx 24rpx;
  background: #f4f2ec;
  border-radius: 16rpx;
  font-size: 28rpx;
  box-sizing: border-box;

  &.textarea {
    min-height: 120rpx;
  }
}

.btn {
  width: 100%;
  height: 80rpx;
  line-height: 80rpx;
  border-radius: 16rpx;
  font-size: 28rpx;
  text-align: center;
  margin-top: 8rpx;

  &[disabled] { opacity: 0.5; }
}

.btn-primary { background: #1a3a6b; color: #ffffff; }
.btn-default { background: #eef2f8; color: #1a3a6b; }
.btn-danger { background: #d03050; color: #ffffff; }

.success {
  display: block;
  margin-top: 16rpx;
  color: #2e9e5b;
  font-size: 26rpx;
}

.delivery-list {
  margin-top: 16rpx;
}

.delivery-item {
  display: flex;
  justify-content: space-between;
  padding: 20rpx 0;
  border-bottom: 2rpx solid #f0ede6;

  .delivery-name { font-size: 26rpx; font-weight: 500; }
  .delivery-status { color: #1a3a6b; font-size: 24rpx; }
  .delivery-room { color: #6b7280; font-size: 24rpx; }
}

.empty-text {
  display: block;
  margin-top: 16rpx;
  color: #9ca3af;
  font-size: 26rpx;
  text-align: center;
}
</style>