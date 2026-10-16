<script setup lang="tsx">
import { onMounted, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { listPendingApprovals, decideApproval, type Approval } from '@/api/approval'
import { useAuthState } from '@/stores/auth'
import { EmptyState, SectionCard } from '@/components'

const { session } = useAuthState()
const canOperate = session && (session.role === 'admin' || session.role === 'manager')

const loading = ref(true)
const approvals = ref<Approval[]>([])
const deciding = ref('')

function formatTime(iso: string | null) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('zh-CN')
}

async function loadData() {
  loading.value = true
  try {
    approvals.value = await listPendingApprovals()
  } catch {
    message.error('加载待审批列表失败')
  } finally {
    loading.value = false
  }
}

function handleDecide(record: Approval, approved: boolean) {
  Modal.confirm({
    title: approved ? '确认批准' : '确认拒绝',
    content: `工具「${record.tool_name}」的本次调用将被${approved ? '执行' : '拒绝'}，Run 随即恢复。`,
    okText: approved ? '批准' : '拒绝',
    okType: approved ? 'primary' : 'danger',
    cancelText: '取消',
    onOk: async () => {
      deciding.value = record.id
      try {
        await decideApproval(record.id, approved)
        message.success(approved ? '已批准，运行恢复中' : '已拒绝，运行恢复中')
        await loadData()
      } catch {
        message.error('操作失败（可能已被处理或运行状态已变化）')
        await loadData()
      } finally {
        deciding.value = ''
      }
    },
  })
}

const columns = [
  { title: '工具', dataIndex: 'tool_name', key: 'tool_name', width: 180 },
  {
    title: '原因',
    dataIndex: 'reason',
    key: 'reason',
    ellipsis: true,
    customRender: ({ text }: { text: string | null }) => text || '-',
  },
  {
    title: '入参摘要',
    key: 'args_digest',
    ellipsis: true,
    customRender: ({ record }: { record: Approval }) =>
      record.args_digest ? JSON.stringify(record.args_digest) : '-',
  },
  {
    title: '发起时间',
    dataIndex: 'created_at',
    key: 'created_at',
    width: 170,
    customRender: ({ text }: { text: string }) => formatTime(text),
  },
  {
    title: '操作',
    key: 'action',
    width: 160,
    customRender: ({ record }: { record: Approval }) => {
      if (!canOperate) return null
      return (
        <a-space>
          <a-button
            size="small"
            type="primary"
            loading={deciding.value === record.id}
            onClick={() => handleDecide(record, true)}
          >
            批准
          </a-button>
          <a-button
            size="small"
            danger
            loading={deciding.value === record.id}
            onClick={() => handleDecide(record, false)}
          >
            拒绝
          </a-button>
        </a-space>
      )
    },
  },
]

onMounted(async () => {
  await loadData()
})
</script>

<template>
  <a-spin :spinning="loading">
    <SectionCard title="工具审批">
      <template #extra>
        <a-space>
          <a-badge :count="approvals.length" :overflow-count="99">
            <a-tag color="orange">待审批</a-tag>
          </a-badge>
          <a-button size="small" @click="loadData">刷新</a-button>
        </a-space>
      </template>
      <a-alert
        type="info"
        show-icon
        :banner="true"
        style="margin-bottom:12px"
        message="危险工具调用会挂起等待人工审批；批准后执行，拒绝后回执进入对话。"
      />
      <a-table
        :data-source="approvals"
        :columns="columns"
        :pagination="false"
        size="small"
        row-key="id"
      >
        <template #empty>
          <EmptyState description="暂无待审批" />
        </template>
      </a-table>
    </SectionCard>
  </a-spin>
</template>