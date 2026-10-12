<script setup lang="tsx">
import { nextTick, onMounted, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  listHandoffs,
  acceptHandoff,
  closeHandoff,
  sendOperatorReply,
  getThreadDetail,
  type HandoffSummary,
  type ThreadDetail,
  type MessageView,
} from '@/api/conversation'
import { useAuthState } from '@/stores/auth'
import { EmptyState, SectionCard } from '@/components'

const { session } = useAuthState()
const canOperate = session && (session.role === 'admin' || session.role === 'operator')

const loading = ref(true)
const handoffs = ref<HandoffSummary[]>([])
const total = ref(0)
const offset = ref(0)
const limit = 20
const filterStatus = ref('')

// Detail drawer
const drawerOpen = ref(false)
const detailLoading = ref(false)
const detail = ref<ThreadDetail | null>(null)
const workingHandoff = ref<HandoffSummary | null>(null)

// Chat input
const replyText = ref('')
const sending = ref(false)

// Close modal
const closeModalVisible = ref(false)
const closeNote = ref('')
const closingId = ref('')

const STATUS_OPTIONS = [
  { value: '', label: '全部' },
  { value: 'pending', label: '待接单' },
  { value: 'accepted', label: '已接单' },
  { value: 'closed', label: '已关闭' },
]

const STATUS_COLORS: Record<string, string> = { pending: 'orange', accepted: 'blue', closed: 'default' }
const STATUS_LABELS: Record<string, string> = { pending: '待接单', accepted: '已接单', closed: '已关闭' }

function statusTag(text: string) {
  return { color: STATUS_COLORS[text] || 'default', label: STATUS_LABELS[text] || text }
}

const columns = [
  { title: '用户', dataIndex: 'username', key: 'username', width: 100 },
  { title: '原因', dataIndex: 'reason', key: 'reason', ellipsis: true },
  {
    title: '状态',
    key: 'status',
    width: 100,
    customRender: ({ text }: { text: string }) => {
      const tag = statusTag(text)
      return <a-tag color={tag.color}>{tag.label}</a-tag>
    },
  },
  {
    title: '接单人',
    key: 'assigned_to',
    width: 100,
    customRender: ({ record }: { record: HandoffSummary }) => record.assigned_to_username || '-',
  },
  {
    title: '创建时间',
    dataIndex: 'created_at',
    key: 'created_at',
    width: 160,
    customRender: ({ text }: { text: string }) => formatTime(text),
  },
  {
    title: '操作',
    key: 'action',
    width: 200,
    customRender: ({ record }: { record: HandoffSummary }) => {
      if (!canOperate) return null
      if (record.status === 'pending') {
        return (
          <a-space>
            <a-button size="small" type="primary" onClick={(e: Event) => { e.stopPropagation(); handleAccept(record.id) }}>接单</a-button>
            <a-button size="small" onClick={(e: Event) => { e.stopPropagation(); handleReject(record.id) }}>忽略</a-button>
          </a-space>
        )
      }
      if (record.status === 'accepted') {
        return <a-button size="small" onClick={(e: Event) => { e.stopPropagation(); confirmClose(record.id) }}>关单</a-button>
      }
      return null
    },
  },
]

onMounted(async () => {
  await loadData()
})

async function loadData() {
  loading.value = true
  try {
    const params: Record<string, string | number> = { offset: offset.value, limit }
    if (filterStatus.value) params.status = filterStatus.value
    const resp = await listHandoffs(params)
    handoffs.value = resp.items
    total.value = resp.total
  } catch {
    message.error('加载转人工列表失败')
  } finally {
    loading.value = false
  }
}

function handleFilter() {
  offset.value = 0
  loadData()
}

function handlePageChange(page: number) {
  offset.value = (page - 1) * limit
  loadData()
}

async function handleAccept(handoffId: string) {
  try {
    await acceptHandoff(handoffId)
    message.success('已接单')
    if (workingHandoff.value?.id === handoffId) {
      workingHandoff.value.status = 'accepted'
    }
    await loadData()
  } catch {
    message.error('接单失败')
  }
}

function confirmClose(handoffId: string) {
  closingId.value = handoffId
  closeNote.value = ''
  closeModalVisible.value = true
}

async function handleClose() {
  if (!closingId.value) return
  try {
    await closeHandoff(closingId.value, { note: closeNote.value || undefined })
    message.success('已关单')
    if (workingHandoff.value?.id === closingId.value) {
      workingHandoff.value.status = 'closed'
    }
    closeModalVisible.value = false
    await loadData()
  } catch {
    message.error('关单失败')
  }
}

function handleReject(handoffId: string) {
  Modal.confirm({
    title: '确认忽略',
    content: '忽略后将关闭此转人工请求。',
    okText: '确认忽略',
    okType: 'danger',
    cancelText: '取消',
    onOk: async () => {
      try {
        await closeHandoff(handoffId)
        message.success('已忽略')
        if (workingHandoff.value?.id === handoffId) {
          workingHandoff.value.status = 'closed'
        }
        await loadData()
      } catch {
        message.error('操作失败')
      }
    },
  })
}

const messageListRef = ref<HTMLDivElement | null>(null)

async function viewConversation(threadId: string, handoff: HandoffSummary) {
  workingHandoff.value = handoff
  workingHandoff.value.status = handoff.status
  drawerOpen.value = true
  detailLoading.value = true
  replyText.value = ''
  try {
    detail.value = await getThreadDetail(threadId)
    await nextTick()
    scrollToBottom()
  } catch {
    message.error('加载会话详情失败')
    drawerOpen.value = false
  } finally {
    detailLoading.value = false
  }
}

function closeDrawer() {
  drawerOpen.value = false
  detail.value = null
  workingHandoff.value = null
}

function scrollToBottom() {
  if (messageListRef.value) {
    messageListRef.value.scrollTop = messageListRef.value.scrollHeight
  }
}

async function handleSendReply() {
  if (!replyText.value.trim() || !detail.value || sending.value) return
  sending.value = true
  const text = replyText.value.trim()
  replyText.value = ''
  try {
    const resp = await sendOperatorReply(detail.value.thread_id, { text })
    // Append the operator's message to the local message list
    detail.value.messages.push({
      message_id: resp.message_id,
      seq: detail.value.messages.length + 1,
      role: 'assistant',
      text: resp.text,
      run_id: null,
      created_at: resp.created_at,
      meta: { operator: true } as Record<string, unknown>,
    })
    await nextTick()
    scrollToBottom()
  } catch {
    message.error('发送失败')
    replyText.value = text
  } finally {
    sending.value = false
  }
}

function isOperator(msg: MessageView): boolean {
  return msg.role === 'assistant' && msg.meta?.operator === true
}

function roleLabel(msg: MessageView): string {
  if (isOperator(msg)) return '客服'
  if (msg.role === 'user') return '客人'
  if (msg.role === 'assistant') return 'AI'
  return msg.role
}

function roleTagColor(msg: MessageView): string {
  if (isOperator(msg)) return 'green'
  if (msg.role === 'user') return 'blue'
  if (msg.role === 'assistant') return 'purple'
  return 'default'
}

function formatTime(iso: string | null) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('zh-CN')
}
</script>

<template>
  <div>
    <a-spin :spinning="loading">
      <SectionCard title="人工接线工作台">
        <template #extra>
          <a-badge :count="handoffs.filter(h => h.status === 'pending').length" :overflow-count="99">
            <a-tag color="orange">待接单</a-tag>
          </a-badge>
        </template>

        <!-- 筛选栏 -->
        <a-space style="margin-bottom:16px" wrap>
          <a-select v-model:value="filterStatus" placeholder="状态筛选" style="width:140px" allow-clear>
            <a-select-option v-for="opt in STATUS_OPTIONS" :key="opt.value" :value="opt.value">
              {{ opt.label }}
            </a-select-option>
          </a-select>
          <a-button type="primary" @click="handleFilter">查询</a-button>
        </a-space>

        <a-table
          :data-source="handoffs"
          :columns="columns"
          :pagination="{ current: offset / limit + 1, total, pageSize: limit, onChange: handlePageChange }"
          size="small"
          row-key="id"
          :custom-row="(record: HandoffSummary) => ({
            style: { cursor: 'pointer' },
            onClick: () => viewConversation(record.thread_id, record),
          })"
        >
          <template #empty>
            <EmptyState description="暂无转人工请求" />
          </template>
        </a-table>
      </SectionCard>
    </a-spin>

    <!-- 会话工作台抽屉 -->
    <a-drawer
      :open="drawerOpen"
      @close="closeDrawer"
      :title="detail ? `会话：${detail.username}` : '会话详情'"
      width="720"
      placement="right"
    >
      <a-spin :spinning="detailLoading">
        <template v-if="detail && workingHandoff">
          <!-- Handoff 信息头 -->
          <a-descriptions size="small" :column="2" style="margin-bottom:12px" bordered>
            <a-descriptions-item label="状态">
              <a-tag :color="statusTag(workingHandoff.status).color">{{ statusTag(workingHandoff.status).label }}</a-tag>
            </a-descriptions-item>
            <a-descriptions-item label="接单人">
              {{ workingHandoff.assigned_to_username || '-' }}
            </a-descriptions-item>
            <a-descriptions-item label="Agent">{{ detail.agent_slug }}</a-descriptions-item>
            <a-descriptions-item label="用户">{{ detail.username }}</a-descriptions-item>
            <a-descriptions-item label="转人工原因" :span="2">{{ workingHandoff.reason || '-' }}</a-descriptions-item>
            <a-descriptions-item v-if="workingHandoff.note" label="附言" :span="2">{{ workingHandoff.note }}</a-descriptions-item>
            <a-descriptions-item v-if="workingHandoff.assigned_at" label="接单时间">{{ formatTime(workingHandoff.assigned_at) }}</a-descriptions-item>
            <a-descriptions-item v-if="workingHandoff.closed_at" label="关单时间">{{ formatTime(workingHandoff.closed_at) }}</a-descriptions-item>
          </a-descriptions>

          <!-- 操作按钮 -->
          <a-space v-if="canOperate" style="margin-bottom:12px">
            <a-button v-if="workingHandoff.status === 'pending'" type="primary" size="small" @click="handleAccept(workingHandoff.id)">接单</a-button>
            <a-button v-if="workingHandoff.status === 'accepted'" size="small" @click="confirmClose(workingHandoff.id)">关单</a-button>
          </a-space>

          <!-- 消息区 -->
          <div class="chat-container">
            <div class="message-list" ref="messageListRef">
              <div v-if="detail.messages.length === 0" class="empty-msg">暂无消息</div>
              <div
                v-for="msg in detail.messages"
                :key="msg.message_id"
                :class="['msg-bubble', msg.role, { operator: isOperator(msg) }]"
              >
                <div class="msg-bubble-header">
                  <a-tag :color="roleTagColor(msg)" size="small">{{ roleLabel(msg) }}</a-tag>
                  <span class="msg-time">{{ formatTime(msg.created_at) }}</span>
                </div>
                <div class="msg-bubble-text">{{ msg.text }}</div>
              </div>
            </div>

            <!-- 输入区 -->
            <div v-if="canOperate && workingHandoff.status === 'accepted'" class="chat-input-area">
              <a-textarea
                v-model:value="replyText"
                placeholder="输入客服回复..."
                :rows="3"
                :maxlength="2000"
                @keydown.ctrl.enter="handleSendReply"
              />
              <div class="chat-input-actions">
                <span class="input-hint">Ctrl+Enter 发送</span>
                <a-button type="primary" :loading="sending" :disabled="!replyText.trim()" @click="handleSendReply">
                  发送
                </a-button>
              </div>
            </div>
            <div v-else-if="workingHandoff.status === 'pending'" class="chat-input-disabled">
              <a-alert message="请先接单后再回复" type="info" show-icon :banner="true" />
            </div>
            <div v-else-if="workingHandoff.status === 'closed'" class="chat-input-disabled">
              <a-alert message="此会话已关闭" type="warning" show-icon :banner="true" />
            </div>
          </div>
        </template>
      </a-spin>
    </a-drawer>

    <!-- 关单确认弹窗 -->
    <a-modal
      v-model:open="closeModalVisible"
      title="关单确认"
      ok-text="确认关单"
      cancel-text="取消"
      @ok="handleClose"
    >
      <a-form layout="vertical">
        <a-form-item label="附言（可选）">
          <a-textarea v-model:value="closeNote" placeholder="关单备注" :rows="3" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<style scoped>
.chat-container {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 380px);
  min-height: 300px;
  border: 1px solid var(--divider);
  border-radius: 8px;
  overflow: hidden;
}

.message-list {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  background: var(--bg-hover);
}

.empty-msg {
  text-align: center;
  color: var(--text-4);
  padding: 48px 0;
}

.msg-bubble {
  margin-bottom: 12px;
  max-width: 85%;
}

.msg-bubble.user {
  margin-right: auto;
}

.msg-bubble.assistant:not(.operator) {
  margin-right: auto;
}

.msg-bubble.operator {
  margin-left: auto;
}

.msg-bubble-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}

.msg-time {
  font-size: 11px;
  color: var(--text-4);
}

.msg-bubble-text {
  padding: 10px 14px;
  border-radius: 8px;
  white-space: pre-wrap;
  font-size: 13px;
  line-height: 1.6;
  word-break: break-word;
}

.msg-bubble.user .msg-bubble-text {
  background: var(--brand-1);
  border: 1px solid var(--brand-2);
}

.msg-bubble.assistant:not(.operator) .msg-bubble-text {
  background: #f5f3ec;
  border: 1px solid var(--gold-2);
}

.msg-bubble.operator .msg-bubble-text {
  background: #eef7f1;
  border: 1px solid #c7e6d2;
}

.chat-input-area {
  border-top: 1px solid var(--divider);
  padding: 12px;
  background: #fff;
}

.chat-input-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 8px;
}

.input-hint {
  font-size: 12px;
  color: var(--text-4);
}

.chat-input-disabled {
  border-top: 1px solid var(--divider);
  padding: 12px;
}
</style>