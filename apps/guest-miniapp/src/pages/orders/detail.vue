<template>
  <view class="page">
    <view v-if="loading" class="loading"><text>加载中…</text></view>
    <view v-else-if="!order" class="empty"><text>订单不存在</text></view>
    <scroll-view v-else class="content" scroll-y>
      <!-- 状态 & 金额 -->
      <view class="hero" :style="{ background: heroBg }">
        <text class="hero-status">{{ statusLabel(order.status) }}</text>
        <text class="hero-amount">{{ formatFen(order.amount_fen) }}</text>
        <text class="hero-dates">{{ order.check_in_date }} → {{ order.check_out_date }} · {{ order.nights }}晚</text>
      </view>

      <!-- 操作按钮 -->
      <view class="actions" v-if="actions.length > 0">
        <button
          v-for="act in actions"
          :key="act.label"
          class="action-btn"
          :class="act.type"
          @click="act.action"
        >
          {{ act.label }}
        </button>
      </view>

      <!-- 订单信息 -->
      <view class="section">
        <text class="section-title">订单信息</text>
        <view class="row"><text class="row-label">单号</text><text class="row-val">{{ order.order_no }}</text></view>
        <view class="row"><text class="row-label">房型</text><text class="row-val">{{ order.room_type_name || order.room_type_code }}</text></view>
        <view class="row"><text class="row-label">房间</text><text class="row-val">{{ order.room_no || '—' }}</text></view>
        <view class="row" v-if="order.floor"><text class="row-label">楼层</text><text class="row-val">{{ order.floor }}F</text></view>
        <view class="row"><text class="row-label">入住人数</text><text class="row-val">{{ guestCount }}</text></view>
        <view class="row"><text class="row-label">已付金额</text><text class="row-val">{{ order.paid_amount_fen !== null ? formatFen(order.paid_amount_fen) : '—' }}</text></view>
        <view class="row" v-if="order.cancel_reason"><text class="row-label">取消原因</text><text class="row-val">{{ order.cancel_reason }}</text></view>
      </view>

      <!-- 门锁凭证 -->
      <view class="section" v-if="order.credential">
        <text class="section-title">门锁密码</text>
        <view class="row"><text class="row-label">版本</text><text class="row-val">V{{ order.credential.version }}</text></view>
        <view class="row"><text class="row-label">生效</text><text class="row-val">{{ formatTime(order.credential.valid_from) }}</text></view>
        <view class="row"><text class="row-label">失效</text><text class="row-val">{{ formatTime(order.credential.valid_to) }}</text></view>
      </view>

      <!-- 发票 -->
      <view class="section" v-if="order.invoice">
        <text class="section-title">发票信息</text>
        <view class="row"><text class="row-label">抬头</text><text class="row-val">{{ order.invoice.title }}</text></view>
        <view class="row"><text class="row-label">税号</text><text class="row-val">{{ order.invoice.tax_no || '—' }}</text></view>
        <view class="row"><text class="row-label">金额</text><text class="row-val">{{ formatFen(order.invoice.amount_fen) }}</text></view>
        <view class="row"><text class="row-label">状态</text><text class="row-val">{{ invoiceStatus(order.invoice.status) }}</text></view>
      </view>

      <!-- 事件时间线 -->
      <view class="section">
        <text class="section-title">时间线</text>
        <view v-for="ev in events" :key="ev.type + ev.created_at" class="event-row">
          <view class="event-dot" :style="{ background: eventColor(ev.type) }" />
          <view class="event-body">
            <text class="event-label">{{ eventLabel(ev.type) }}</text>
            <text class="event-time">{{ formatTime(ev.created_at) }}</text>
          </view>
        </view>
      </view>
    </scroll-view>

    <!-- 改期弹窗 -->
    <view v-if="showReschedule" class="overlay" @click="showReschedule = false">
      <view class="sheet" @click.stop>
        <text class="sheet-title">改期</text>
        <text class="field-label">新入住日期</text>
        <picker mode="date" :value="newCheckIn" :start="today" @change="onCheckInChange">
          <view class="picker">{{ newCheckIn || '请选择' }}</view>
        </picker>
        <text class="field-label">新退房日期</text>
        <picker mode="date" :value="newCheckOut" :start="newCheckIn || today" @change="onCheckOutChange">
          <view class="picker">{{ newCheckOut || '请选择' }}</view>
        </picker>
        <view class="sheet-actions">
          <button class="cancel-btn" @click="showReschedule = false">取消</button>
          <button class="confirm-btn" :disabled="!newCheckIn || !newCheckOut" @click="confirmReschedule">确认改期</button>
        </view>
        <text v-if="rescheduleError" class="error">{{ rescheduleError }}</text>
      </view>
    </view>

    <!-- 续订弹窗 -->
    <view v-if="showRenew" class="overlay" @click="showRenew = false">
      <view class="sheet" @click.stop>
        <text class="sheet-title">续订</text>
        <text class="field-label">新退房日期</text>
        <picker mode="date" :value="newCheckOut" :start="tomorrow" @change="onRenewChange">
          <view class="picker">{{ newCheckOut || '请选择' }}</view>
        </picker>
        <view class="sheet-actions">
          <button class="cancel-btn" @click="showRenew = false">取消</button>
          <button class="confirm-btn" :disabled="!newCheckOut" @click="confirmRenew">确认续订</button>
        </view>
        <text v-if="renewError" class="error">{{ renewError }}</text>
      </view>
    </view>
  </view>
</template>

<script setup lang="ts">
import { onShow, onLoad } from '@dcloudio/uni-app'
import { ref, computed } from 'vue'
import {
  getOrder,
  cancelOrder,
  rescheduleOrder,
  renewOrder,
  applyInvoice,
  payOrder,
  checkoutOrder,
  ORDER_STATUS_LABELS,
  formatFen,
  canPay,
  canCancel,
  canReschedule,
  canRenew,
  canInvoice,
} from '@/api/orders'
import { requireAuth } from '@/stores/auth'

const orderNo = ref('')
const order = ref<{
  order_no: string
  status: string
  room_type_code: string | null
  room_type_name: string | null
  check_in_date: string
  check_out_date: string
  nights: number
  amount_fen: number
  paid_amount_fen: number | null
  room_no: string | null
  floor: number | null
  cancel_reason: string | null
  credential: { version: number; reason: string; valid_from: string; valid_to: string } | null
  invoice: { title: string; tax_no: string | null; amount_fen: number; status: string } | null
  events: Array<{ type: string; created_at: string }>
} | null>(null)
const loading = ref(true)
const guestCount = ref(1)

// 改期
const showReschedule = ref(false)
const newCheckIn = ref('')
const newCheckOut = ref('')
const rescheduleError = ref('')

// 续订
const showRenew = ref(false)
const renewError = ref('')

const today = computed(() => new Date().toISOString().slice(0, 10))
const tomorrow = computed(() => {
  const d = new Date()
  d.setDate(d.getDate() + 1)
  return d.toISOString().slice(0, 10)
})

const heroBg = computed(() => {
  const s = order.value?.status
  if (s === 'cancelled') return 'linear-gradient(135deg, #9ca3af, #d1d5db)'
  if (s === 'completed') return 'linear-gradient(135deg, #6b7280, #9ca3af)'
  if (s === 'checked_in' || s === 'checkout_pending') return 'linear-gradient(135deg, #2e9e5b, #34c9a0)'
  return 'linear-gradient(135deg, #1a3a6b, #24487c)'
})

const events = computed(() => {
  if (!order.value) return []
  return order.value.events.map((e) => ({
    type: e.type,
    created_at: e.created_at,
  }))
})

const actions = computed(() => {
  if (!order.value) return []
  const s = order.value.status
  const list: Array<{ label: string; type: string; action: () => void }> = []

  if (canPay(s)) {
    list.push({ label: '去支付', type: 'primary', action: handlePay })
  }
  if (canCancel(s)) {
    list.push({ label: '取消订单', type: 'danger', action: handleCancel })
  }
  if (canReschedule(s)) {
    list.push({ label: '改期', type: 'default', action: openReschedule })
  }
  if (canRenew(s)) {
    list.push({ label: '续订', type: 'default', action: openRenew })
  }
  if (canInvoice(s) && !order.value.invoice) {
    list.push({ label: '申请发票', type: 'default', action: handleInvoice })
  }
  if (s === 'checked_in') {
    list.push({ label: '申请退房', type: 'danger', action: handleCheckout })
  }

  return list
})

function statusLabel(status: string): string {
  return ORDER_STATUS_LABELS[status] || status
}

function formatTime(iso: string): string {
  return iso.replace('T', ' ').slice(0, 16)
}

function invoiceStatus(st: string): string {
  const map: Record<string, string> = { requested: '已申请', issued: '已开具', cancelled: '已作废' }
  return map[st] || st
}

function eventLabel(type: string): string {
  const map: Record<string, string> = {
    created: '下单',
    payment_initiated: '发起支付',
    paid: '支付成功',
    checked_in: '办理入住',
    checkout_initiated: '申请退房',
    checkout_completed: '退房完成',
    checkout_pending: '退房处理中',
    completed: '完成',
    cancelled: '取消',
    rescheduled: '改期',
    renewed: '续订',
    credential_issued: '下发密码',
    credential_extended: '密码展期',
    credential_notified: '下发通知',
    credential_revoked: '密码吊销',
    invoice_applied: '申请发票',
    checkin_blocked: '入住受阻',
  }
  return map[type] || type
}

function eventColor(type: string): string {
  if (type.includes('cancel') || type.includes('revoke')) return '#d03050'
  if (type.includes('paid') || type.includes('credential')) return '#2e9e5b'
  if (type.includes('check')) return '#1a3a6b'
  if (type.includes('complete')) return '#6b7280'
  return '#c9a227'
}

onLoad((params) => {
  orderNo.value = params?.orderNo || ''
})

async function load(): Promise<void> {
  if (!orderNo.value) {
    loading.value = false
    return
  }
  try {
    order.value = await getOrder(orderNo.value)
  } catch {
    order.value = null
  }
  loading.value = false
}

async function handlePay(): Promise<void> {
  if (!order.value) return
  try {
    const result = await payOrder(order.value.order_no)
    // 微信小程序：调起支付
    const isMp = typeof (globalThis as Record<string, unknown>).wx === 'object'
    if (isMp) {
      const { pay_params } = result
      await new Promise<void>((resolve, reject) => {
        uni.requestPayment({
          provider: 'wxpay',
          timeStamp: String(pay_params.timeStamp ?? ''),
          nonceStr: String(pay_params.nonceStr ?? ''),
          package: String(pay_params.package ?? ''),
          signType: String(pay_params.signType ?? 'MD5'),
          paySign: String(pay_params.paySign ?? ''),
          success: () => resolve(),
          fail: (err) => reject(new Error(err.errMsg || '支付取消')),
        })
      })
    } else {
      uni.showToast({ title: 'H5 支付模拟：支付参数已获取', icon: 'none' })
    }
    // 支付成功/模拟后刷新订单
    await load()
  } catch (err) {
    const msg = (err as { message?: string }).message || '支付失败'
    uni.showToast({ title: msg, icon: 'none' })
  }
}

async function handleCancel(): Promise<void> {
  if (!order.value) return
  uni.showModal({
    title: '取消订单',
    content: '确定要取消这个订单吗？',
    success: async (res) => {
      if (!res.confirm || !order.value) return
      try {
        order.value = await cancelOrder(order.value.order_no)
        uni.showToast({ title: '已取消', icon: 'success' })
      } catch (err) {
        const msg = (err as { message?: string }).message || '取消失败'
        uni.showToast({ title: msg, icon: 'none' })
      }
    },
  })
}

function openReschedule(): void {
  if (!order.value) return
  newCheckIn.value = order.value.check_in_date
  newCheckOut.value = order.value.check_out_date
  rescheduleError.value = ''
  showReschedule.value = true
}

async function confirmReschedule(): Promise<void> {
  if (!order.value || !newCheckIn.value || !newCheckOut.value) return
  try {
    order.value = await rescheduleOrder(order.value.order_no, newCheckIn.value, newCheckOut.value)
    showReschedule.value = false
    uni.showToast({ title: '改期成功', icon: 'success' })
  } catch (err) {
    rescheduleError.value = (err as { message?: string }).message || '改期失败'
  }
}

function openRenew(): void {
  if (!order.value) return
  newCheckOut.value = order.value.check_out_date
  renewError.value = ''
  showRenew.value = true
}

async function confirmRenew(): Promise<void> {
  if (!order.value || !newCheckOut.value) return
  try {
    order.value = await renewOrder(order.value.order_no, newCheckOut.value)
    showRenew.value = false
    uni.showToast({ title: '续订成功', icon: 'success' })
  } catch (err) {
    renewError.value = (err as { message?: string }).message || '续订失败'
  }
}

async function handleInvoice(): Promise<void> {
  if (!order.value) return
  uni.showModal({
    title: '申请发票',
    content: '发票抬头默认使用个人，确定申请？',
    success: async (res) => {
      if (!res.confirm || !order.value) return
      try {
        const invoice = await applyInvoice(order.value.order_no, '个人')
        if (invoice) {
          await load()
          uni.showToast({ title: '发票已申请', icon: 'success' })
        }
      } catch (err) {
        const msg = (err as { message?: string }).message || '申请失败'
        uni.showToast({ title: msg, icon: 'none' })
      }
    },
  })
}

async function handleCheckout(): Promise<void> {
  if (!order.value) return
  uni.showModal({
    title: '申请退房',
    content: '确定要退房吗？保洁人员将清洁房间并更新门锁密码。',
    success: async (res) => {
      if (!res.confirm || !order.value) return
      try {
        const result = await checkoutOrder(order.value.order_no)
        order.value = { ...order.value!, status: result.status }
        uni.showToast({ title: '退房申请已提交', icon: 'success' })
      } catch (err) {
        const msg = (err as { message?: string }).message || '退房申请失败'
        uni.showToast({ title: msg, icon: 'none' })
      }
    },
  })
}

function onCheckInChange(e: { detail: { value: string } }): void {
  newCheckIn.value = e.detail.value
  if (newCheckOut.value && newCheckOut.value <= newCheckIn.value) {
    newCheckOut.value = ''
  }
}

function onCheckOutChange(e: { detail: { value: string } }): void {
  newCheckOut.value = e.detail.value
}

function onRenewChange(e: { detail: { value: string } }): void {
  newCheckOut.value = e.detail.value
}

onShow(() => {
  if (!requireAuth()) return
  void load()
})
</script>

<style lang="scss" scoped>
.page {
  min-height: 100vh;
}

.loading, .empty {
  text-align: center;
  padding: 160rpx;
  color: #9ca3af;
}

.hero {
  padding: 48rpx 40rpx;
  color: #ffffff;

  .hero-status {
    font-size: 28rpx;
    opacity: 0.9;
  }

  .hero-amount {
    display: block;
    margin-top: 12rpx;
    font-size: 48rpx;
    font-weight: 700;
  }

  .hero-dates {
    display: block;
    margin-top: 8rpx;
    font-size: 26rpx;
    opacity: 0.85;
  }
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 16rpx;
  padding: 24rpx 32rpx;

  .action-btn {
    flex: 1;
    min-width: 180rpx;
    height: 72rpx;
    line-height: 72rpx;
    border-radius: 16rpx;
    font-size: 26rpx;
    text-align: center;

    &.primary {
      background-color: #1a3a6b;
      color: #ffffff;
    }

    &.danger {
      background-color: #fdeef1;
      color: #d03050;
      border: 2rpx solid #f6cdd6;
    }

    &.default {
      background-color: #eef2f8;
      color: #1a3a6b;
    }
  }
}

.section {
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

  .row {
    display: flex;
    justify-content: space-between;
    margin-top: 16rpx;

    &:first-child { margin-top: 0; }

    .row-label {
      color: #6b7280;
      font-size: 26rpx;
    }

    .row-val {
      color: #1f2329;
      font-size: 26rpx;
      text-align: right;
      max-width: 60%;
    }
  }
}

.event-row {
  display: flex;
  align-items: flex-start;
  margin-top: 20rpx;

  .event-dot {
    width: 16rpx;
    height: 16rpx;
    border-radius: 50%;
    margin-top: 8rpx;
    flex-shrink: 0;
  }

  .event-body {
    margin-left: 16rpx;

    .event-label {
      display: block;
      font-size: 26rpx;
      color: #1f2329;
    }

    .event-time {
      display: block;
      margin-top: 4rpx;
      font-size: 22rpx;
      color: #9ca3af;
    }
  }
}

.overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.4);
  z-index: 100;
  display: flex;
  align-items: flex-end;
}

.sheet {
  width: 100%;
  padding: 40rpx 32rpx;
  background: #ffffff;
  border-radius: 24rpx 24rpx 0 0;

  .sheet-title {
    display: block;
    margin-bottom: 32rpx;
    font-size: 32rpx;
    font-weight: 600;
    text-align: center;
  }

  .field-label {
    display: block;
    margin-top: 24rpx;
    color: #6b7280;
    font-size: 26rpx;
  }

  .picker {
    margin-top: 8rpx;
    padding: 20rpx 24rpx;
    background-color: #f4f2ec;
    border-radius: 16rpx;
    font-size: 28rpx;
    color: #1f2329;
  }

  .sheet-actions {
    display: flex;
    gap: 24rpx;
    margin-top: 40rpx;

    .cancel-btn, .confirm-btn {
      flex: 1;
      height: 80rpx;
      line-height: 80rpx;
      border-radius: 16rpx;
      font-size: 28rpx;
    }

    .cancel-btn {
      background-color: #eef2f8;
      color: #6b7280;
    }

    .confirm-btn {
      background-color: #1a3a6b;
      color: #ffffff;

      &[disabled] {
        opacity: 0.5;
      }
    }
  }

  .error {
    display: block;
    margin-top: 16rpx;
    color: #d03050;
    font-size: 24rpx;
    text-align: center;
  }
}
</style>