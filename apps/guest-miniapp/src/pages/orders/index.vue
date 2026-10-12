<template>
  <view class="page">
    <view v-if="loading" class="loading"><text>加载中…</text></view>
    <hc-empty
      v-else-if="orders.length === 0"
      description="暂无订单，去首页浏览房型并预订吧"
      action-text="浏览房型"
      glyph="🧾"
      @action="goBooking"
    />
    <scroll-view v-else class="list" scroll-y @refresherrefresh="refresh" refresher-enabled>
      <view
        v-for="order in orders"
        :key="order.order_no"
        class="card"
        @click="openDetail(order.order_no)"
      >
        <view class="card-header">
          <text class="room-name">{{ order.room_type_name || order.room_type_code }}</text>
          <text class="status" :style="{ color: statusColor(order.status) }">
            {{ statusLabel(order.status) }}
          </text>
        </view>
        <view class="card-body">
          <text class="date">{{ order.check_in_date }} → {{ order.check_out_date }}</text>
          <text class="amount">{{ formatFen(order.amount_fen) }}</text>
        </view>
        <view class="card-footer">
          <text class="nights">{{ nightsText(order.nights) }}</text>
          <text v-if="order.room_no" class="room-no">房间 {{ order.room_no }}</text>
        </view>
      </view>
    </scroll-view>
  </view>
</template>

<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { ref } from 'vue'
import { listOrders, ORDER_STATUS_LABELS, statusColor, formatFen } from '@/api/orders'
import { requireAuth } from '@/stores/auth'

const loading = ref(true)
const orders = ref<Array<{
  order_no: string
  status: string
  room_type_code: string | null
  room_type_name: string | null
  check_in_date: string
  check_out_date: string
  nights: number
  amount_fen: number
  room_no: string | null
}>>([])

function statusLabel(status: string): string {
  return ORDER_STATUS_LABELS[status] || status
}

function nightsText(nights: number): string {
  return `${nights}晚`
}

async function load(): Promise<void> {
  try {
    orders.value = await listOrders()
  } catch {
    // 静默失败
  }
  loading.value = false
}

async function refresh(): Promise<void> {
  await load()
  uni.stopPullDownRefresh()
}

function openDetail(orderNo: string): void {
  uni.navigateTo({ url: `/pages/orders/detail?orderNo=${orderNo}` })
}

function goBooking(): void {
  uni.switchTab({ url: '/pages/home/index' })
}

onShow(() => {
  if (!requireAuth()) {
    return
  }
  void load()
})
</script>

<style lang="scss" scoped>
.page {
  min-height: 100vh;
  padding: 0 0 32rpx;
}

.loading {
  text-align: center;
  padding: 160rpx;
  color: #9ca3af;
}

.list {
  padding: 24rpx;

  .card {
    margin-bottom: 20rpx;
    padding: 32rpx;
    background-color: #ffffff;
    border-radius: 20rpx;
    box-shadow: 0 4rpx 16rpx rgba(26, 58, 107, 0.08);

    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;

      .room-name {
        font-size: 30rpx;
        font-weight: 600;
        color: #1f2329;
      }

      .status {
        font-size: 26rpx;
        font-weight: 500;
      }
    }

    .card-body {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 20rpx;

      .date {
        color: #4b5563;
        font-size: 26rpx;
      }

      .amount {
        color: #1a3a6b;
        font-size: 30rpx;
        font-weight: 600;
      }
    }

    .card-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 20rpx;
      padding-top: 20rpx;
      border-top: 2rpx solid #f0ede6;

      .nights {
        color: #6b7280;
        font-size: 24rpx;
      }

      .room-no {
        color: #c9a227;
        font-size: 24rpx;
        font-weight: 500;
      }
    }
  }
}
</style>