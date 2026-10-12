<template>
  <view class="page">
    <view
      v-if="handoffStatus === 'pending'"
      class="handoff-banner"
    >
      <text class="handoff-text">已通知前台，人工客服将尽快在本会话回复</text>
    </view>

    <view class="topbar">
      <text class="title">智能助手</text>
      <text
        class="handoff-action"
        @click="askHandoff"
      >
        转人工
      </text>
    </view>

    <scroll-view
      class="messages"
      scroll-y
      :scroll-into-view="scrollAnchor"
    >
      <view
        v-if="messages.length === 0 && !pendingText"
        class="empty"
      >
        <text class="empty-title">向皓晨酒店智能助手提问</text>
        <text class="empty-desc">入住政策、餐饮时间、周边推荐都可以问</text>
        <view class="suggestions">
          <text
            v-for="item in suggestions"
            :key="item"
            class="suggestion"
            @click="fillInput(item)"
          >
            {{ item }}
          </text>
        </view>
      </view>

      <view
        v-if="pendingText"
        class="row row-user"
      >
        <view class="bubble bubble-user">
          <text class="text">{{ pendingText }}</text>
        </view>
      </view>

      <view
        v-for="item in messages"
        :key="item.message_id"
        class="row"
        :class="item.role === 'user' ? 'row-user' : 'row-assistant'"
      >
        <view
          class="bubble"
          :class="item.role === 'user' ? 'bubble-user' : 'bubble-assistant'"
        >
          <text class="text">{{ item.text }}</text>

          <view
            v-if="item.citations.length > 0"
            class="citations"
          >
            <text
              v-for="(citation, index) in item.citations"
              :key="citation.chunk_id"
              class="citation-chip"
              @click="toggleCitations(item.message_id)"
            >
              [{{ index + 1 }}] {{ citation.document_title }}
            </text>
          </view>
          <view
            v-if="expanded[item.message_id]"
            class="citation-detail"
          >
            <text
              v-for="(citation, index) in item.citations"
              :key="citation.chunk_id"
              class="citation-line"
            >
              [{{ index + 1 }}] {{ citation.document_title }} · 第 {{ citation.ordinal + 1 }} 段 ·
              相关度 {{ citation.score.toFixed(2) }}
            </text>
          </view>

          <view
            v-if="item.role === 'assistant'"
            class="actions"
          >
            <text
              class="action"
              :class="{ 'action-active': item.my_rating === 'up' }"
              @click="rate(item, 'up')"
            >
              有帮助
            </text>
            <text
              class="action"
              :class="{ 'action-active': item.my_rating === 'down' }"
              @click="rate(item, 'down')"
            >
              没帮助
            </text>
          </view>
        </view>
      </view>

      <view
        v-if="draft.active"
        class="row row-assistant"
      >
        <view class="bubble bubble-assistant">
          <text class="text">{{ draft.text || '正在思考…' }}</text>
          <text
            v-if="draft.activity"
            class="activity"
          >
            {{ draft.activity }}
          </text>
          <text
            v-if="draft.error"
            class="activity activity-error"
          >
            {{ draft.error }}
          </text>
        </view>
      </view>

      <view
        id="msg-bottom"
        class="anchor"
      />
    </scroll-view>

    <view class="composer">
      <input
        v-model="input"
        class="input"
        :disabled="busy"
        placeholder="问问酒店的事…"
        confirm-type="send"
        @confirm="send"
      >
      <button
        class="send"
        :disabled="busy || !input.trim()"
        @click="send"
      >
        {{ busy ? '生成中' : '发送' }}
      </button>
    </view>
  </view>
</template>

<script setup lang="ts">
import { nextTick, reactive, ref } from 'vue'
import { onLoad } from '@dcloudio/uni-app'
import {
  type ChatMessage,
  type HandoffState,
  type Rating,
  type SubmitResult,
  fetchHandoff,
  fetchRequestStatus,
  listMessages,
  requestHandoff,
  setFeedback,
  submitMessage,
} from '@/api/chat'
import { openRunStream, type RunEvent, type StreamHandle } from '@/api/stream'
import { getSession, requireAuth } from '@/stores/auth'

const THREAD_STORAGE_KEY = 'haochen.chat.threads'
const POLL_INTERVAL_MS = 1500
const POLL_ATTEMPTS = 120
const TERMINAL_EVENTS = new Set(['run.completed', 'run.failed', 'run.cancelled'])
const TERMINAL_RUN_STATUSES = new Set(['completed', 'failed', 'cancelled'])

const TOOL_LABELS: Record<string, string> = {
  query_kb: '正在检索酒店知识库…',
  get_current_time: '正在获取当前时间…',
}

const suggestions = ['早餐几点结束？', '能延迟退房吗？', '周边有什么推荐？']

const messages = ref<ChatMessage[]>([])
const input = ref('')
const busy = ref(false)
const pendingText = ref('')
const scrollAnchor = ref('')
const expanded = reactive<Record<string, boolean>>({})
const handoffStatus = ref('none')
const draft = reactive({ active: false, text: '', activity: '', error: '' })
const threadId = ref<string | null>(readThreadId())

function readThreadId(): string | null {
  const username = getSession()?.username
  if (!username) {
    return null
  }
  try {
    const raw = uni.getStorageSync(THREAD_STORAGE_KEY)
    if (!raw || typeof raw !== 'string') {
      return null
    }
    const map = JSON.parse(raw) as Record<string, string>
    return map[username] ?? null
  } catch {
    return null
  }
}

function writeThreadId(id: string): void {
  const username = getSession()?.username
  if (!username) {
    return
  }
  let map: Record<string, string> = {}
  try {
    const raw = uni.getStorageSync(THREAD_STORAGE_KEY)
    if (raw && typeof raw === 'string') {
      map = JSON.parse(raw) as Record<string, string>
    }
  } catch {
    map = {}
  }
  map[username] = id
  uni.setStorageSync(THREAD_STORAGE_KEY, JSON.stringify(map))
}

function clearThreadId(): void {
  const username = getSession()?.username
  if (!username) {
    return
  }
  try {
    const raw = uni.getStorageSync(THREAD_STORAGE_KEY)
    const map = raw && typeof raw === 'string' ? (JSON.parse(raw) as Record<string, string>) : {}
    delete map[username]
    uni.setStorageSync(THREAD_STORAGE_KEY, JSON.stringify(map))
  } catch {
    uni.removeStorageSync(THREAD_STORAGE_KEY)
  }
}

function errorStatus(error: unknown): number {
  return typeof error === 'object' && error !== null && 'status' in error
    ? Number((error as { status: unknown }).status)
    : 0
}

function toastError(error: unknown): void {
  const message =
    typeof error === 'object' && error !== null && 'message' in error
      ? String((error as { message: unknown }).message)
      : '操作失败'
  uni.showToast({ title: message, icon: 'none' })
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms)
  })
}

function newRequestId(): string {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (char) => {
    const random = (Math.random() * 16) | 0
    const value = char === 'x' ? random : (random & 0x3) | 0x8
    return value.toString(16)
  })
}

function scrollToBottom(): void {
  scrollAnchor.value = ''
  void nextTick(() => {
    scrollAnchor.value = 'msg-bottom'
  })
}

function fillInput(text: string): void {
  input.value = text
}

function toggleCitations(messageId: string): void {
  expanded[messageId] = !expanded[messageId]
}

function toolActivity(tool?: string): string {
  if (!tool) {
    return '正在处理…'
  }
  return TOOL_LABELS[tool] ?? `正在调用 ${tool}…`
}

async function loadHistory(): Promise<void> {
  if (!threadId.value) {
    return
  }
  try {
    messages.value = await listMessages(threadId.value)
  } catch (error) {
    if (errorStatus(error) === 404) {
      // 线程已不存在（例如开发环境重置数据）：从新会话开始
      clearThreadId()
      threadId.value = null
      messages.value = []
      return
    }
    toastError(error)
    return
  }
  try {
    handoffStatus.value = (await fetchHandoff(threadId.value)).status
  } catch {
    handoffStatus.value = 'none'
  }
  scrollToBottom()
}

async function waitForRun(requestId: string): Promise<string | null> {
  for (let attempt = 0; attempt < POLL_ATTEMPTS; attempt += 1) {
    await sleep(POLL_INTERVAL_MS)
    try {
      const status = await fetchRequestStatus(requestId)
      if (status.run_id) {
        return status.run_id
      }
      if (status.status === 'cancelled' || status.run_status === 'failed') {
        return null
      }
    } catch {
      return null
    }
  }
  return null
}

async function pollRequest(requestId: string): Promise<void> {
  for (let attempt = 0; attempt < POLL_ATTEMPTS; attempt += 1) {
    await sleep(POLL_INTERVAL_MS)
    try {
      const status = await fetchRequestStatus(requestId)
      if (status.status === 'cancelled') {
        return
      }
      if (status.run_status && TERMINAL_RUN_STATUSES.has(status.run_status)) {
        return
      }
    } catch {
      return
    }
  }
}

function applyEvent(event: RunEvent): void {
  const payload = event.data
  if (event.event === 'run.started') {
    draft.activity = '正在思考…'
  } else if (event.event === 'message.delta') {
    draft.text += payload.text ?? ''
    draft.activity = ''
  } else if (event.event === 'tool.call') {
    draft.activity = toolActivity(payload.tool)
  } else if (event.event === 'tool.result') {
    draft.activity = ''
  } else if (event.event === 'message.completed') {
    draft.text = payload.text ?? draft.text
  } else if (event.event === 'run.failed') {
    draft.error = payload.error || '本次回复生成失败，请稍后重试'
  } else if (event.event === 'run.cancelled') {
    draft.activity = '已取消'
  }
}

function followRun(runId: string, requestId: string): Promise<void> {
  return new Promise((resolve) => {
    let settled = false
    let handle: StreamHandle | null = null
    const finish = (): void => {
      if (settled) {
        return
      }
      settled = true
      handle?.close()
      resolve()
    }
    handle = openRunStream(runId, {
      onEvent: (event) => {
        applyEvent(event)
        if (TERMINAL_EVENTS.has(event.event)) {
          finish()
        }
      },
      onError: (message) => {
        // 事件流不可用时降级轮询：终态事件缺失也不影响历史以数据库为准
        draft.activity = `${message}，已切换为轮询`
        void pollRequest(requestId).then(finish)
      },
      onClose: finish,
    })
  })
}

async function streamRun(result: SubmitResult): Promise<void> {
  let runId = result.run_id
  if (!runId) {
    draft.activity = `排队中（第 ${result.queue_position ?? '—'} 位）`
    runId = await waitForRun(result.request_id)
    if (!runId) {
      draft.error = '本次请求未能开始生成，请重试'
      return
    }
  }
  await followRun(runId, result.request_id)
}

async function send(): Promise<void> {
  const text = input.value.trim()
  if (!text || busy.value) {
    return
  }
  input.value = ''
  busy.value = true
  pendingText.value = text
  draft.active = true
  draft.text = ''
  draft.activity = '正在接入…'
  draft.error = ''
  scrollToBottom()
  try {
    const result = await submitMessage(threadId.value, text, newRequestId())
    threadId.value = result.thread_id
    writeThreadId(result.thread_id)
    await streamRun(result)
  } catch (error) {
    draft.error = errorStatus(error) === 0 ? '网络不可达，请重试' : ''
    toastError(error)
  } finally {
    busy.value = false
    pendingText.value = ''
    draft.active = false
    draft.activity = ''
    await loadHistory()
  }
}

async function rate(message: ChatMessage, rating: Rating): Promise<void> {
  const next = message.my_rating === rating ? null : rating
  try {
    const payload = await setFeedback(message.message_id, next)
    const target = messages.value.find((item) => item.message_id === message.message_id)
    if (target) {
      target.my_rating = payload.rating
    }
  } catch (error) {
    toastError(error)
  }
}

function askHandoff(): void {
  if (busy.value) {
    uni.showToast({ title: '稍等片刻', icon: 'none' })
    return
  }
  if (!threadId.value) {
    uni.showToast({ title: '先发送一条消息', icon: 'none' })
    return
  }
  uni.showModal({
    title: '转人工',
    content: '将通知前台人工客服跟进本次会话，是否继续？',
    success: (result) => {
      if (!result.confirm) {
        return
      }
      void submitHandoff()
    },
  })
}

async function submitHandoff(): Promise<void> {
  const current = threadId.value
  if (!current) {
    return
  }
  try {
    const state: HandoffState = await requestHandoff(current, '客人主动请求转人工')
    handoffStatus.value = state.status
    uni.showToast({ title: '已通知前台', icon: 'none' })
  } catch (error) {
    toastError(error)
  }
}

onLoad(() => {
  if (!requireAuth()) {
    return
  }
  void loadHistory()
})
</script>

<style lang="scss" scoped>
.page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background-color: #f7f5f0;
}

.handoff-banner {
  padding: 20rpx 32rpx;
  background-color: #fbf6e7;

  .handoff-text {
    color: #a8851f;
    font-size: 24rpx;
  }
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20rpx 32rpx;
  background-color: #ffffff;

  .title {
    font-size: 30rpx;
    font-weight: 600;
  }

  .handoff-action {
    padding: 8rpx 24rpx;
    color: #1a3a6b;
    font-size: 26rpx;
    border: 2rpx solid #c9a227;
    border-radius: 999rpx;
  }
}

.messages {
  flex: 1;
  padding: 24rpx 24rpx 0;
  box-sizing: border-box;
}

.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 120rpx 24rpx;

  .empty-title {
    font-size: 32rpx;
    font-weight: 600;
  }

  .empty-desc {
    margin-top: 16rpx;
    color: #6b7280;
    font-size: 26rpx;
  }
}

.suggestions {
  display: flex;
  flex-direction: column;
  align-items: center;
  margin-top: 40rpx;
}

.suggestion {
  margin-top: 16rpx;
  padding: 16rpx 32rpx;
  background-color: #ffffff;
  border-radius: 999rpx;
  color: #1a3a6b;
  font-size: 26rpx;
  box-shadow: 0 4rpx 16rpx rgba(26, 58, 107, 0.08);
}

.row {
  display: flex;
  margin-bottom: 24rpx;
}

.row-user {
  justify-content: flex-end;
}

.row-assistant {
  justify-content: flex-start;
}

.bubble {
  display: flex;
  flex-direction: column;
  max-width: 82%;
  padding: 24rpx 28rpx;
  border-radius: 20rpx;

  .text {
    font-size: 28rpx;
    line-height: 1.6;
    white-space: pre-wrap;
  }
}

.bubble-user {
  background-color: #1a3a6b;

  .text {
    color: #ffffff;
  }
}

.bubble-assistant {
  background-color: #ffffff;
}

.activity {
  margin-top: 12rpx;
  color: #9ca3af;
  font-size: 24rpx;
}

.activity-error {
  color: #d4380d;
}

.citations {
  display: flex;
  flex-wrap: wrap;
  gap: 12rpx;
  margin-top: 20rpx;
}

.citation-chip {
  padding: 8rpx 20rpx;
  background-color: #eef2f8;
  border-radius: 999rpx;
  color: #1a3a6b;
  font-size: 22rpx;
}

.citation-detail {
  margin-top: 16rpx;
  padding: 20rpx;
  background-color: #faf9f5;
  border-radius: 16rpx;
}

.citation-line {
  display: block;
  margin-top: 8rpx;
  color: #6b7280;
  font-size: 22rpx;

  &:first-child {
    margin-top: 0;
  }
}

.actions {
  display: flex;
  gap: 24rpx;
  margin-top: 20rpx;
}

.action {
  padding: 6rpx 20rpx;
  color: #6b7280;
  font-size: 24rpx;
  border: 2rpx solid #e5e7eb;
  border-radius: 999rpx;
}

.action-active {
  color: #1a3a6b;
  border-color: #c9a227;
}

.anchor {
  height: 8rpx;
}

.composer {
  display: flex;
  align-items: center;
  gap: 16rpx;
  padding: 20rpx 24rpx;
  padding-bottom: calc(20rpx + env(safe-area-inset-bottom));
  background-color: #ffffff;
}

.input {
  flex: 1;
  height: 76rpx;
  padding: 0 24rpx;
  background-color: #f4f2ec;
  border-radius: 999rpx;
  font-size: 28rpx;
}

.send {
  width: 160rpx;
  height: 76rpx;
  margin: 0;
  padding: 0;
  background-color: #1a3a6b;
  border-radius: 999rpx;
  color: #ffffff;
  font-size: 28rpx;
  line-height: 76rpx;

  &[disabled] {
    background-color: #a9b8d0;
    color: #ffffff;
  }
}
</style>
