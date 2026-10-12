<template>
  <view class="page">
    <!-- 日期选择 -->
    <view class="date-bar">
      <view class="date-field">
        <text class="date-label">入住</text>
        <picker mode="date" :value="checkIn" :start="today" @change="onCheckIn">
          <view class="date-value">{{ checkIn || '请选择' }}</view>
        </picker>
      </view>
      <text class="date-arrow">→</text>
      <view class="date-field">
        <text class="date-label">退房</text>
        <picker mode="date" :value="checkOut" :start="checkIn || today" @change="onCheckOut">
          <view class="date-value">{{ checkOut || '请选择' }}</view>
        </picker>
      </view>
      <button class="search-btn" size="mini" @click="load">查询</button>
    </view>

    <!-- 房型列表 -->
    <view v-if="loading" class="loading"><text>加载中…</text></view>
    <view v-else-if="offers.length === 0" class="empty">
      <text class="empty-text">暂无可用房型</text>
    </view>
    <scroll-view v-else class="list" scroll-y>
      <view
        v-for="offer in offers"
        :key="offer.code"
        class="card"
        @click="bookRoom(offer)"
      >
        <view class="card-header">
          <text class="card-name">{{ offer.name }}</text>
          <text class="card-price">{{ formatFen(offer.base_price_fen) }}<text class="per-night">/晚</text></text>
        </view>
        <view class="card-meta">
          <text class="meta-tag">{{ offer.bed_type === 'king' ? '大床' : '双床' }}</text>
          <text class="meta-tag">{{ offer.capacity }}人</text>
        </view>
        <text class="card-desc">{{ offer.description || '' }}</text>
        <view v-if="offer.availability" class="avail-row">
          <text class="avail-text" :class="{ sold: offer.availability.available_rooms === 0 }">
            {{ offer.availability.available_rooms > 0 ? `剩 ${offer.availability.available_rooms} 间` : '已满' }}
          </text>
          <text class="total-text">{{ formatFen(offer.availability.amount_fen) }} / {{ offer.availability.nights }}晚</text>
        </view>
      </view>
    </scroll-view>
  </view>
</template>

<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { ref } from 'vue'
import { listRoomTypes, formatFen } from '@/api/room-types'
import { requireAuth } from '@/stores/auth'

const today = new Date().toISOString().slice(0, 10)
const checkIn = ref('')
const checkOut = ref('')
const loading = ref(false)
const offers = ref<Array<{
  code: string
  name: string
  description: string | null
  base_price_fen: number
  capacity: number
  bed_type: string
  amenities: string[]
  availability: {
    nights: number
    available_rooms: number
    amount_fen: number
  } | null
}>>([])

async function load(): Promise<void> {
  loading.value = true
  try {
    offers.value = await listRoomTypes(
      checkIn.value || undefined,
      checkOut.value || undefined,
    )
  } catch {
    offers.value = []
  }
  loading.value = false
}

function onCheckIn(e: { detail: { value: string } }): void {
  checkIn.value = e.detail.value
  if (checkOut.value && checkOut.value <= checkIn.value) {
    checkOut.value = ''
  }
}

function onCheckOut(e: { detail: { value: string } }): void {
  checkOut.value = e.detail.value
}

function bookRoom(offer: { code: string }): void {
  uni.navigateTo({
    url: `/pages/rooms/booking?code=${offer.code}&checkIn=${checkIn.value || ''}&checkOut=${checkOut.value || ''}`,
  })
}

onShow(() => {
  if (!requireAuth()) return
  void load()
})
</script>

<style lang="scss" scoped>
.page {
  padding: 0;
}

.date-bar {
  display: flex;
  align-items: center;
  gap: 16rpx;
  padding: 24rpx 32rpx;
  background: #ffffff;

  .date-field {
    flex: 1;

    .date-label {
      display: block;
      font-size: 22rpx;
      color: #6b7280;
    }

    .date-value {
      margin-top: 4rpx;
      font-size: 28rpx;
      color: #1f2329;
      padding: 12rpx 0;
    }
  }

  .date-arrow {
    color: #c9a227;
    font-size: 28rpx;
    margin-top: 24rpx;
  }

  .search-btn {
    margin-top: 20rpx;
    background-color: #1a3a6b;
    color: #ffffff;
    border-radius: 12rpx;
  }
}

.loading {
  text-align: center;
  padding: 160rpx;
  color: #9ca3af;
}

.empty {
  padding: 160rpx;
  text-align: center;

  .empty-text {
    color: #9ca3af;
    font-size: 28rpx;
  }
}

.list {
  padding: 24rpx 32rpx;

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

      .card-name {
        font-size: 30rpx;
        font-weight: 600;
        color: #1f2329;
      }

      .card-price {
        color: #1a3a6b;
        font-size: 34rpx;
        font-weight: 700;

        .per-night {
          font-size: 22rpx;
          font-weight: 400;
          color: #6b7280;
        }
      }
    }

    .card-meta {
      display: flex;
      gap: 12rpx;
      margin-top: 16rpx;

      .meta-tag {
        padding: 4rpx 16rpx;
        background-color: #eef2f8;
        color: #1a3a6b;
        font-size: 22rpx;
        border-radius: 8rpx;
      }
    }

    .card-desc {
      display: block;
      margin-top: 12rpx;
      color: #6b7280;
      font-size: 24rpx;
    }

    .avail-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 20rpx;
      padding-top: 20rpx;
      border-top: 2rpx solid #f0ede6;

      .avail-text {
        font-size: 26rpx;
        color: #2e9e5b;

        &.sold {
          color: #d03050;
        }
      }

      .total-text {
        font-size: 26rpx;
        color: #1f2329;
      }
    }
  }
}
</style>