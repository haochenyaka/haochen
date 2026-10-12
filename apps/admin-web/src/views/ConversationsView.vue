<script setup lang="tsx">
import { onMounted, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  listConversations,
  getThreadDetail,
  setLabel,
  removeLabel,
  type ConversationSummary,
  type ThreadDetail,
} from '@/api/conversation'
import { EmptyState, SectionCard } from '@/components'

const loading = ref(true)
const detailLoading = ref(false)
const conversations = ref<ConversationSummary[]>([])
const total = ref(0)
const offset = ref(0)
const limit = 20

// Filters
const filterAgent = ref('')
const filterStatus = ref('')
const filterLabel = ref('')
const filterSearch = ref('')

// Detail drawer
const drawerOpen = ref(false)
const detail = ref<ThreadDetail | null>(null)

// Label form
const newLabel = ref('resolved')
const newLabelComment = ref('')
const addingLabel = ref(false)

const LABEL_OPTIONS = [
  { value: 'resolved', label: '已解决' },
  { value: 'unresolved', label: '未解决' },
  { value: 'handoff_requested', label: '已转人工' },
  { value: 'complaint_risk', label: '投诉风险' },
  { value: 'hallucination_suspected', label: '疑似幻觉' },
  { value: 'other', label: '其他' },
]

const STATUS_OPTIONS = [
  { value: '', label: '全部' },
  { value: 'handoff_pending', label: '待转人工' },
]

const columns = [
  { title: '用户', dataIndex: 'username', key: 'username', width: 120 },
  { title: '标题', dataIndex: 'title', key: 'title', ellipsis: true },
  { title: 'Agent', dataIndex: 'agent_slug', key: 'agent_slug', width: 100 },
  { title: '消息', dataIndex: 'message_count', key: 'message_count', width: 60 },
  {
    title: '转人工',
    key: 'handoff_status',
    width: 100,
    customRender: ({ record }: { record: ConversationSummary }) => {
      const status = record.handoff_status
      if (!status) return ''
      const colors: Record<string, string> = { pending: 'orange', accepted: 'blue', closed: 'default' }
      const labels: Record<string, string> = { pending: '待接单', accepted: '已接单', closed: '已关闭' }
      return (
        <a-tag color={colors[status] || 'default'}>{labels[status] || status}</a-tag>
      )
    },
  },
  {
    title: '质检标签',
    key: 'labels',
    width: 180,
    customRender: ({ record }: { record: ConversationSummary }) =>
      record.labels.map((lb) => (
        <a-tag key={lb} color="purple" style="margin-bottom:2px">{lb}</a-tag>
      )),
  },
  { title: '最后活动', dataIndex: 'last_message_at', key: 'last_message_at', width: 160 },
]

onMounted(async () => {
  await loadData()
})

async function loadData() {
  loading.value = true
  try {
    const params: Record<string, string | number | undefined> = {
      offset: offset.value,
      limit,
    }
    if (filterAgent.value) params.agent_slug = filterAgent.value
    if (filterStatus.value) params.status = filterStatus.value
    if (filterLabel.value) params.label = filterLabel.value
    if (filterSearch.value) params.search = filterSearch.value
    const resp = await listConversations(params)
    conversations.value = resp.items
    total.value = resp.total
  } catch {
    message.error('加载会话列表失败')
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  offset.value = 0
  loadData()
}

function handlePageChange(page: number) {
  offset.value = (page - 1) * limit
  loadData()
}

async function openDetail(threadId: string) {
  detailLoading.value = true
  drawerOpen.value = true
  try {
    detail.value = await getThreadDetail(threadId)
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
}

async function handleAddLabel() {
  if (!detail.value) return
  addingLabel.value = true
  try {
    await setLabel(detail.value.thread_id, {
      label: newLabel.value,
      comment: newLabelComment.value || null,
    })
    message.success('标签已添加')
    newLabelComment.value = ''
    detail.value = await getThreadDetail(detail.value.thread_id)
    // Refresh list label cache
    await loadData()
  } catch {
    message.error('添加标签失败')
  } finally {
    addingLabel.value = false
  }
}

async function handleRemoveLabel(labelId: number) {
  if (!detail.value) return
  Modal.confirm({
    title: '确认删除标签',
    content: '删除后不可恢复。',
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    onOk: async () => {
      try {
        await removeLabel(detail.value!.thread_id, labelId)
        message.success('标签已删除')
        detail.value = await getThreadDetail(detail.value!.thread_id)
        await loadData()
      } catch {
        message.error('删除标签失败')
      }
    },
  })
}

function formatTime(iso: string | null) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('zh-CN')
}

function roleLabel(role: string) {
  const map: Record<string, string> = { user: '客人', assistant: 'AI', system: '系统' }
  return map[role] || role
}
</script>

<template>
  <div>
    <a-spin :spinning="loading">
      <SectionCard title="会话监控">
        <!-- 筛选栏 -->
        <a-space style="margin-bottom:16px" wrap>
          <a-input v-model:value="filterSearch" placeholder="搜索用户名" style="width:160px" allow-clear />
          <a-input v-model:value="filterAgent" placeholder="Agent slug" style="width:140px" allow-clear />
          <a-select
            v-model:value="filterStatus"
            placeholder="转人工状态"
            style="width:140px"
            allow-clear
          >
            <a-select-option v-for="opt in STATUS_OPTIONS" :key="opt.value" :value="opt.value">
              {{ opt.label }}
            </a-select-option>
          </a-select>
          <a-select
            v-model:value="filterLabel"
            placeholder="质检标签"
            style="width:140px"
            allow-clear
          >
            <a-select-option v-for="opt in LABEL_OPTIONS" :key="opt.value" :value="opt.value">
              {{ opt.label }}
            </a-select-option>
          </a-select>
          <a-button type="primary" @click="handleSearch">查询</a-button>
        </a-space>

        <a-table
          :data-source="conversations"
          :columns="columns"
          :pagination="{ current: offset / limit + 1, total, pageSize: limit, onChange: handlePageChange }"
          size="small"
          row-key="thread_id"
          :custom-row="(record: ConversationSummary) => ({ style: { cursor: 'pointer' }, onClick: () => openDetail(record.thread_id) })"
        >
          <template #empty>
            <EmptyState description="暂无会话数据" />
          </template>
        </a-table>
      </SectionCard>
    </a-spin>

    <!-- 会话详情抽屉 -->
    <a-drawer
      :open="drawerOpen"
      @close="closeDrawer"
      :title="detail ? `会话：${detail.username} (${detail.agent_slug})` : '会话详情'"
      width="680"
    >
      <a-spin :spinning="detailLoading">
        <template v-if="detail">
          <!-- 标签区 -->
          <a-card size="small" title="质检标签" style="margin-bottom:12px">
            <template #extra>
              <a-space>
                <a-select v-model:value="newLabel" style="width:140px" size="small">
                  <a-select-option v-for="opt in LABEL_OPTIONS" :key="opt.value" :value="opt.value">
                    {{ opt.label }}
                  </a-select-option>
                </a-select>
                <a-input v-model:value="newLabelComment" placeholder="备注" size="small" style="width:140px" />
                <a-button size="small" type="primary" :loading="addingLabel" @click="handleAddLabel">添加</a-button>
              </a-space>
            </template>
            <div v-if="detail.labels.length === 0" style="color:var(--text-4);font-size:13px">暂无标签</div>
            <div v-for="lb in detail.labels" :key="lb.id" style="margin-bottom:4px">
              <a-tag :color="lb.source === 'auto' ? 'geekblue' : 'purple'">
                {{ lb.label }}
                <span style="font-size:11px;margin-left:4px">({{ lb.source }})</span>
              </a-tag>
              <span v-if="lb.comment" style="font-size:12px;color:var(--text-3);margin-left:4px">{{ lb.comment }}</span>
              <span style="font-size:11px;color:var(--text-4);margin-left:4px">{{ lb.created_by }} · {{ formatTime(lb.created_at) }}</span>
              <a-button size="small" type="link" danger @click="handleRemoveLabel(lb.id)" style="font-size:11px">删除</a-button>
            </div>
          </a-card>

          <!-- 消息流 -->
          <a-card size="small" title="消息记录">
            <div v-if="detail.messages.length === 0" style="color:var(--text-4);padding:24px;text-align:center">暂无消息</div>
            <div v-for="msg in detail.messages" :key="msg.message_id" style="margin-bottom:12px">
              <div :class="['msg-row', msg.role]">
                <div class="msg-header">
                  <a-tag color="blue" size="small">{{ roleLabel(msg.role) }}</a-tag>
                  <span style="font-size:11px;color:var(--text-4)">{{ formatTime(msg.created_at) }}</span>
                  <span v-if="msg.run_id" style="font-size:10px;color:var(--text-4);margin-left:8px">{{ msg.run_id?.slice(0, 8) }}...</span>
                </div>
                <div class="msg-body">
                  <pre style="white-space:pre-wrap;margin:4px 0;font-size:13px;font-family:inherit">{{ msg.text }}</pre>
                </div>
              </div>
            </div>
          </a-card>
        </template>
      </a-spin>
    </a-drawer>
  </div>
</template>

<style scoped>
.msg-row {
  padding: 8px 12px;
  border-radius: 8px;
  background: var(--bg-hover);
}
.msg-row.user {
  background: var(--brand-1);
}
.msg-header {
  display: flex;
  align-items: center;
  margin-bottom: 2px;
}
.msg-body pre {
  margin: 0;
}
</style>