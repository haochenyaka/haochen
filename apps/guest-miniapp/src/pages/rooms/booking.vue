<template>
  <view class="page">
    <view v-if="loading" class="loading"><text>加载中…</text></view>
    <view v-else-if="!offer" class="empty"><text>房型不存在</text></view>
    <scroll-view v-else class="content" scroll-y>
      <!-- 房型信息 -->
      <view class="hero-card">
        <text class="name">{{ offer.name }}</text>
        <text class="price">{{ formatFen(offer.base_price_fen) }}<text class="per">/晚</text></text>
      </view>

      <!-- 预订表单 -->
      <view class="card">
        <text class="section-title">预订信息</text>

        <text class="field-label">入住日期</text>
        <picker mode="date" :value="checkInDate" :start="today" @change="onCheckIn">
          <view class="picker">{{ checkInDate || '请选择' }}</view>
        </picker>

        <text class="field-label">退房日期</text>
        <picker mode="date" :value="checkOutDate" :start="minCheckOut" @change="onCheckOut">
          <view class="picker">{{ checkOutDate || '请选择' }}</view>
        </picker>

        <text class="field-label">入住人数</text>
        <view class="stepper">
          <text class="step-btn" @click="decrement">−</text>
          <text class="step-val">{{ guestCount }}</text>
          <text class="step-btn" @click="increment">+</text>
        </view>
      </view>

      <!-- 费用预览 -->
      <view class="card" v-if="nights > 0">
        <text class="section-title">费用明细</text>
        <view class="fee-row">
          <text class="fee-label">{{ offer.name }} × {{ nights }}晚</text>
          <text class="fee-val">{{ formatFen(totalFen) }}</text>
        </view>
        <view class="fee-row total">
          <text class="fee-label">合计</text>
          <text class="fee-val">{{ formatFen(totalFen) }}</text>
        </view>
      </view>

      <text v-if="error" class="error">{{ error }}</text>

      <button class="book-btn" :disabled="!canBook || submitting" @click="handleBook">
        {{ submitting ? '提交中…' : '提交订单' }}
      </button>
    </scroll-view>
  </view>
</template>

<script setup lang="ts">
import { onLoad } from '@dcloudio/uni-app'
import { ref, computed } from 'vue'
import { getRoomType, formatFen } from '@/api/room-types'
import { createOrder } from '@/api/orders'

const today = new Date().toISOString().slice(0, 10)

const code = ref('')
const offer = ref<{ code: string; name: string; base_price_fen: number; capacity: number } | null>(null)
const loading = ref(true)
const error = ref('')
const submitting = ref(false)

const checkInDate = ref('')
const checkOutDate = ref('')
const guestCount = ref(1)
const idempotencyKey = ref('')

const minCheckOut = computed(() => {
  if (!checkInDate.value) return today
  const d = new Date(checkInDate.value)
  d.setDate(d.getDate() + 1)
  return d.toISOString().slice(0, 10)
})

const nights = computed(() => {
  if (!checkInDate.value || !checkOutDate.value) return 0
  const ci = new Date(checkInDate.value)
  const co = new Date(checkOutDate.value)
  return Math.max(0, Math.round((co.getTime() - ci.getTime()) / 86400000))
})

const totalFen = computed(() => {
  if (!offer.value) return 0
  return offer.value.base_price_fen * nights.value
})

const canBook = computed(() => {
  return offer.value !== null && checkInDate.value !== '' && checkOutDate.value !== '' && nights.value > 0
})

onLoad((params) => {
  code.value = params?.code || ''
  checkInDate.value = params?.checkIn || ''
  checkOutDate.value = params?.checkOut || ''
  idempotencyKey.value = `book-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  void load()
})

async function load(): Promise<void> {
  if (!code.value) {
    loading.value = false
    return
  }
  try {
    offer.value = await getRoomType(code.value, checkInDate.value || undefined, checkOutDate.value || undefined)
  } catch {
    offer.value = null
  }
  loading.value = false
}

function onCheckIn(e: { detail: { value: string } }): void {
  checkInDate.value = e.detail.value
  if (checkOutDate.value && checkOutDate.value <= checkInDate.value) {
    checkOutDate.value = ''
  }
}

function onCheckOut(e: { detail: { value: string } }): void {
  checkOutDate.value = e.detail.value
}

function decrement(): void {
  if (guestCount.value > 1) guestCount.value -= 1
}

function increment(): void {
  if (offer.value && guestCount.value < offer.value.capacity) {
    guestCount.value += 1
  }
}

async function handleBook(): Promise<void> {
  if (!canBook.value) return
  submitting.value = true
  error.value = ''
  try {
    const order = await createOrder({
      room_type_code: code.value,
      check_in_date: checkInDate.value,
      check_out_date: checkOutDate.value,
      guest_count: guestCount.value,
      idempotency_key: idempotencyKey.value,
    })
    uni.redirectTo({ url: `/pages/orders/detail?orderNo=${order.order_no}` })
  } catch (err) {
    error.value = (err as { message?: string }).message || '下单失败，请重试'
  }
  submitting.value = false
}
</script>

<style lang="scss" scoped>
.page {
  min-height: 100vh;
}

.loading, .empty {
  text-align: center;
  padding: 160rpx;
  color: #9aa1ab;
}

.hero-card {
  padding: 48rpx 40rpx;
  background: linear-gradient(135deg, #1a3a6b, #24487c);
  color: #ffffff;

  .name {
    font-size: 36rpx;
    font-weight: 600;
  }

  .price {
    display: block;
    margin-top: 12rpx;
    font-size: 40rpx;
    font-weight: 700;

    .per {
      font-size: 24rpx;
      font-weight: 400;
      opacity: 0.8;
    }
  }
}

.card {
  margin: 20rpx 32rpx;
  padding: 32rpx;
  background-color: #ffffff;
  border-radius: 20rpx;
  box-shadow: 0 4rpx 16rpx rgba(26, 58, 107, 0.08);

  .section-title {
    display: block;
    margin-bottom: 24rpx;
    font-size: 28rpx;
    font-weight: 600;
    color: #1f2329;
  }

  .field-label {
    display: block;
    margin-top: 24rpx;
    color: #6b7280;
    font-size: 26rpx;

    &:first-child {
      margin-top: 0;
    }
  }

  .picker {
    margin-top: 8rpx;
    padding: 20rpx 24rpx;
    background-color: #f4f2ec;
    border-radius: 16rpx;
    font-size: 28rpx;
    color: #1f2329;
  }

  .stepper {
    display: flex;
    align-items: center;
    gap: 32rpx;
    margin-top: 16rpx;

    .step-btn {
      width: 64rpx;
      height: 64rpx;
      line-height: 64rpx;
      text-align: center;
      background-color: #eef2f8;
      border-radius: 50%;
      font-size: 36rpx;
      color: #1a3a6b;
    }

    .step-val {
      font-size: 32rpx;
      font-weight: 600;
      min-width: 48rpx;
      text-align: center;
    }
  }

  .fee-row {
    display: flex;
    justify-content: space-between;
    margin-top: 16rpx;

    .fee-label {
      color: #6b7280;
      font-size: 26rpx;
    }

    .fee-val {
      color: #1f2329;
      font-size: 26rpx;
    }

    &.total {
      margin-top: 20rpx;
      padding-top: 20rpx;
      border-top: 2rpx solid #f0ede6;

      .fee-label {
        font-size: 28rpx;
        font-weight: 600;
        color: #1f2329;
      }

      .fee-val {
        font-size: 32rpx;
        font-weight: 700;
        color: #1a3a6b;
      }
    }
  }
}

.error {
  display: block;
  margin: 16rpx 32rpx;
  color: #d03050;
  font-size: 26rpx;
  text-align: center;
}

.book-btn {
  margin: 24rpx 32rpx 48rpx;
  background-color: #1a3a6b;
  color: #ffffff;
  border-radius: 16rpx;
  height: 88rpx;
  line-height: 88rpx;
  font-size: 30rpx;

  &[disabled] {
    opacity: 0.5;
  }
}
</style>