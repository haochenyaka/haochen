<template>
  <view class="page">
    <hc-hero :title="`您好，${session?.displayName ?? ''}`" subtitle="皓晨无人酒店 · 入住期间服务由智能助手全程响应" />

    <hc-section-title title="服务入口" />
    <view class="grid">
      <view
        v-for="item in services"
        :key="item.title"
        class="grid-item"
        @click="openService(item)"
      >
        <view class="grid-item__head">
          <text class="item-title">{{ item.title }}</text>
          <text class="item-arrow">›</text>
        </view>
        <text class="item-desc">{{ item.desc }}</text>
        <text class="item-stage">{{ item.stage }}</text>
      </view>
    </view>

    <hc-section-title title="服务状态" />
    <view class="status-card">
      <view class="status-line">
        <text class="status-key">接口连通</text>
        <hc-tag :text="healthText" :type="healthText === '正常' ? 'success' : 'warning'" />
      </view>
      <view v-for="(value, key) in checks" :key="key" class="status-line">
        <text class="status-key">依赖 {{ key }}</text>
        <text class="status-val">{{ value }}</text>
      </view>
    </view>
  </view>
</template>

<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { ref } from 'vue'
import { fetchReadiness } from '@/api/system'
import { requireAuth, useAuthState } from '@/stores/auth'

const { session } = useAuthState()
const healthText = ref('检测中…')
const checks = ref<Record<string, string>>({})

const services = [
  { title: '在线咨询', desc: '酒店政策、周边推荐', stage: '已接入知识库', path: '/pages/chat/index' },
  { title: '房型与预订', desc: '查询房型、下单与改期', stage: 'P4 订单链路', path: '/pages/rooms/index' },
  { title: '客房服务', desc: '呼叫清洁、物品配送', stage: 'P5 派单与配送', path: '/pages/services/room-service' },
  { title: '退房与发票', desc: '退房、发票与投诉', stage: 'P5 退房链路', path: '/pages/orders/index' },
]

function openService(item: { path?: string }): void {
  if (!item.path) {
    uni.showToast({ title: '该服务尚未开放', icon: 'none' })
    return
  }
  uni.navigateTo({ url: item.path })
}

async function loadStatus(): Promise<void> {
  try {
    const payload = await fetchReadiness()
    healthText.value = payload.status === 'ready' ? '正常' : '降级'
    checks.value = payload.checks
  } catch (err) {
    healthText.value = `不可达（${(err as { message?: string }).message || '未知错误'}）`
    checks.value = {}
  }
}

onShow(() => {
  if (!requireAuth()) {
    return
  }
  void loadStatus()
})
</script>

<style lang="scss" scoped>
.page {
  padding: 32rpx;
}

.grid {
  display: flex;
  flex-wrap: wrap;
  gap: 24rpx;
}

.grid-item {
  display: flex;
  flex-direction: column;
  width: calc(50% - 12rpx);
  padding: 32rpx;
  background-color: #ffffff;
  border-radius: 20rpx;
  box-shadow: 0 4rpx 16rpx rgba(26, 58, 107, 0.08);
  box-sizing: border-box;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .item-title {
    font-size: 30rpx;
    font-weight: 600;
    color: #1f2329;
  }

  .item-arrow {
    color: #c9a227;
    font-size: 34rpx;
    line-height: 1;
  }

  .item-desc {
    margin-top: 12rpx;
    color: #6b7280;
    font-size: 24rpx;
  }

  .item-stage {
    margin-top: 24rpx;
    color: #1a3a6b;
    font-size: 22rpx;
  }
}

.status-card {
  display: flex;
  flex-direction: column;
  padding: 32rpx;
  background-color: #ffffff;
  border-radius: 20rpx;
  box-shadow: 0 4rpx 16rpx rgba(26, 58, 107, 0.08);

  .status-line {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 16rpx;

    &:first-child {
      margin-top: 0;
    }
  }

  .status-key {
    color: #6b7280;
    font-size: 26rpx;
  }

  .status-val {
    color: #1f2329;
    font-size: 26rpx;
  }
}
</style>
