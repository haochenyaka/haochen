<template>
  <view class="page">
    <view class="card">
      <text class="title">今日打卡</text>
      <text class="desc">上班与下班打卡需拍照，经人脸比对、工牌校验与工服检测后生效。</text>
    </view>

    <view class="actions">
      <button
        class="action online"
        :class="{ disabled: todayCheckedIn }"
        :disabled="todayCheckedIn"
        @tap="handleClock('check_in')"
      >
        {{ todayCheckedIn ? '已上班打卡' : '上班打卡' }}
      </button>
      <button
        class="action offline"
        :class="{ disabled: todayCheckedOut }"
        :disabled="todayCheckedOut"
        @tap="handleClock('check_out')"
      >
        {{ todayCheckedOut ? '已下班打卡' : '下班打卡' }}
      </button>
    </view>

    <view v-if="loading" class="loading">
      <text>打卡处理中，请稍候...</text>
    </view>

    <view v-if="lastResult" class="result" :class="{ pass: lastResult.review_status === 'auto_pass', fail: lastResult.review_status !== 'auto_pass' }">
      <text class="result-title">{{ directionLabel(lastResult.direction) }}结果</text>
      <text class="result-status">审核状态：{{ reviewLabel(lastResult.review_status) }}</text>
      <text v-if="lastResult.face_score != null" class="result-score">人脸分数：{{ lastResult.face_score }}</text>
      <text class="result-check">工牌：{{ lastResult.badge_ok ? '通过' : '未通过' }}</text>
      <text class="result-check">工服：{{ lastResult.uniform_ok ? '通过' : '未通过' }}</text>
    </view>

    <view class="section">
      <text class="section-title">最近打卡记录</text>
    </view>

    <view v-if="records.length === 0" class="empty">
      <text>暂无打卡记录</text>
    </view>

    <view v-for="r in records" :key="r.id" class="record">
      <text class="record-dir">{{ directionLabel(r.direction) }}</text>
      <text class="record-status">{{ reviewLabel(r.review_status) }}</text>
      <text class="record-time">{{ formatTime(r.created_at) }}</text>
    </view>
  </view>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { requireAuth } from '@/stores/auth'
import type { AttendanceRecord, ClockResult } from '@/api/attendance'
import { clock, directionLabel, fetchMyRecords, reviewLabel } from '@/api/attendance'

const loading = ref(false)
const todayCheckedIn = ref(false)
const todayCheckedOut = ref(false)
const lastResult = ref<ClockResult | null>(null)
const records = ref<AttendanceRecord[]>([])

onShow(() => {
  if (!requireAuth()) return
  loadRecords()
})

function formatTime(iso: string): string {
  try {
    const d = new Date(iso)
    return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
  } catch {
    return iso
  }
}

async function loadRecords(): Promise<void> {
  try {
    const data = await fetchMyRecords(10)
    records.value = data
    todayCheckedIn.value = data.some((r) => r.direction === 'check_in')
    todayCheckedOut.value = data.some((r) => r.direction === 'check_out')
  } catch {
    // 静默失败
  }
}

async function handleClock(direction: string): Promise<void> {
  try {
    const res = await uni.chooseImage({ count: 1, sizeType: ['compressed'] })
    if (!res.tempFilePaths || res.tempFilePaths.length === 0) {
      uni.showToast({ title: '请选择或拍摄照片', icon: 'none' })
      return
    }
    loading.value = true
    const photoPath = res.tempFilePaths[0]
    const result = await clock(direction, photoPath)
    lastResult.value = result
    uni.showToast({
      title: result.review_status === 'auto_pass' ? '打卡成功' : '已提交复核',
      icon: result.review_status === 'auto_pass' ? 'success' : 'none',
    })
    await loadRecords()
  } catch (err: unknown) {
    const msg = err && typeof err === 'object' && 'message' in err
      ? (err as { message: string }).message
      : '打卡失败'
    uni.showToast({ title: msg, icon: 'none' })
  } finally {
    loading.value = false
  }
}
</script>

<style lang="scss" scoped>
.page {
  padding: 32rpx;
}

.card {
  display: flex;
  flex-direction: column;
  padding: 40rpx 32rpx;
  background-color: #ffffff;
  border-radius: 20rpx;
  box-shadow: 0 4rpx 16rpx rgba(26, 58, 107, 0.08);

  .title {
    font-size: 34rpx;
    font-weight: 600;
    color: #1f2329;
  }

  .desc {
    margin-top: 20rpx;
    color: #6b7280;
    font-size: 26rpx;
    line-height: 1.6;
  }
}

.actions {
  display: flex;
  flex-direction: column;
  margin-top: 32rpx;
  gap: 24rpx;

  .action {
    border-radius: 16rpx;
    padding: 28rpx;
    font-size: 30rpx;

    &.online {
      background-color: #1a3a6b;
      color: #ffffff;

      &.disabled {
        opacity: 0.5;
      }
    }

    &.offline {
      background-color: #ffffff;
      color: #1a3a6b;
      border: 2rpx solid #1a3a6b;

      &.disabled {
        opacity: 0.5;
      }
    }
  }
}

.loading {
  margin-top: 24rpx;
  padding: 24rpx;
  text-align: center;
  color: #6b7280;
  font-size: 26rpx;
}

.result {
  display: flex;
  flex-direction: column;
  margin-top: 24rpx;
  padding: 32rpx;
  border-radius: 20rpx;
  gap: 12rpx;

  &.pass {
    background-color: #eaf7ef;
  }

  &.fail {
    background-color: #fdf4e6;
  }

  .result-title {
    font-size: 30rpx;
    font-weight: 600;
  }

  .result-status {
    font-size: 28rpx;
  }

  .result-score,
  .result-check {
    font-size: 26rpx;
    color: #374151;
  }
}

.section {
  margin-top: 40rpx;

  .section-title {
    font-size: 30rpx;
    font-weight: 600;
    color: #1f2329;
  }
}

.empty {
  margin-top: 20rpx;
  padding: 40rpx;
  text-align: center;
  color: #9ca3af;
  font-size: 26rpx;
}

.record {
  display: flex;
  align-items: center;
  padding: 24rpx 0;
  border-bottom: 2rpx solid #f0ede6;
  gap: 16rpx;

  .record-dir {
    font-size: 28rpx;
    font-weight: 500;
    min-width: 120rpx;
    color: #1f2329;
  }

  .record-status {
    font-size: 26rpx;
    color: #374151;
    flex: 1;
  }

  .record-time {
    font-size: 24rpx;
    color: #9ca3af;
  }
}
</style>