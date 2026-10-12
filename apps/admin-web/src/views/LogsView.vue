<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  fetchLogs,
  fetchRunTimeline,
  softDeleteLogs,
  type LogEntryResponse,
  type AuditEntryResponse,
} from '@/api/log'
import { EmptyState, SectionCard } from '@/components'

const entries = ref<LogEntryResponse[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)
const loading = ref(true)
const error = ref('')
const detailRunId = ref('')
const detailEntries = ref<AuditEntryResponse[]>([])
const detailVisible = ref(false)
const detailLoading = ref(false)

const filters = ref({
  kind: '',
  status: '',
  runId: '',
})

onMounted(async () => {
  await loadData()
})

async function loadData() {
  loading.value = true
  error.value = ''
  try {
    const params: Record<string, unknown> = { page: page.value, page_size: pageSize.value }
    if (filters.value.kind) params.kind = filters.value.kind
    if (filters.value.status) params.status = filters.value.status
    if (filters.value.runId) params.run_id = filters.value.runId
    const result = await fetchLogs(params)
    entries.value = result.entries
    total.value = result.total
  } catch (err) {
    error.value = (err as { message?: string }).message || '加载失败'
  } finally {
    loading.value = false
  }
}

function onPageChange(p: number) {
  page.value = p
  loadData()
}

async function showDetail(runId: string) {
  detailRunId.value = runId
  detailVisible.value = true
  detailLoading.value = true
  try {
    const result = await fetchRunTimeline(runId)
    detailEntries.value = result.entries
  } catch (err) {
    detailEntries.value = []
    error.value = (err as { message?: string }).message || '加载详情失败'
  } finally {
    detailLoading.value = false
  }
}

function closeDetail() {
  detailVisible.value = false
  detailRunId.value = ''
  detailEntries.value = []
}

async function handleDelete(ids: number[]) {
  if (!confirm(`确认软删 ${ids.length} 条审计记录？此操作不可逆。`)) return
  try {
    const result = await softDeleteLogs({ tool_ids: ids })
    alert(`已软删 ${result.total_affected} 条，操作日志 id=${result.log_id}`)
    await loadData()
  } catch (err) {
    alert('删除失败：' + ((err as { message?: string }).message || '未知错误'))
  }
}

const columns = [
  { title: '类型', dataIndex: 'kind', key: 'kind', width: 80 },
  { title: 'ID', dataIndex: 'id', key: 'id', width: 80 },
  { title: 'Agent', dataIndex: 'agent_slug', key: 'agent_slug', width: 100 },
  { title: '模型/工具', key: 'model', width: 130 },
  { title: '状态', dataIndex: 'status', key: 'status', width: 80 },
  { title: 'token', dataIndex: 'prompt_tokens', key: 'prompt_tokens', width: 80 },
  { title: '客人', dataIndex: 'guest_username', key: 'guest_username', width: 100 },
  { title: '时间', dataIndex: 'created_at', key: 'created_at', width: 160 },
  { title: '操作', key: 'action', width: 160 },
]

const detailColumns = [
  { title: '类型', dataIndex: 'kind', key: 'kind', width: 60 },
  { title: 'seq', dataIndex: 'seq', key: 'seq', width: 50 },
  { title: '模型/工具', key: 'model', width: 120 },
  { title: '状态', dataIndex: 'status', key: 'status', width: 70 },
  { title: '耗时', dataIndex: 'duration_ms', key: 'duration_ms', width: 70 },
  { title: '时间', dataIndex: 'created_at', key: 'created_at', width: 150 },
]
</script>

<template>
  <a-spin :spinning="loading">
    <a-alert v-if="error" type="warning" :message="error" show-icon style="margin-bottom:16px" />

    <!-- 筛选栏 -->
    <SectionCard title="筛选" compact>
      <a-space wrap>
        <a-select
          v-model:value="filters.kind"
          placeholder="类型"
          style="width:120px"
          allow-clear
        >
          <a-select-option value="">全部</a-select-option>
          <a-select-option value="model">模型调用</a-select-option>
          <a-select-option value="tool">工具调用</a-select-option>
        </a-select>
        <a-input v-model:value="filters.status" placeholder="状态" style="width:120px" allow-clear />
        <a-input v-model:value="filters.runId" placeholder="Run ID" style="width:200px" allow-clear />
        <a-button type="primary" @click="loadData">查询</a-button>
      </a-space>
    </SectionCard>

    <!-- 日志列表 -->
    <SectionCard title="智能体日志">
      <a-table
        :data-source="entries"
        :columns="columns"
        :pagination="false"
        size="small"
        row-key="id"
      >
        <template #bodyCell="{ column, text, record }: { column: { key: string }, text: unknown, record: LogEntryResponse }">
          <template v-if="column.key === 'kind'">
            {{ text === 'model' ? '模型' : '工具' }}
          </template>
          <template v-else-if="column.key === 'model'">
            {{ record.model ?? record.tool_name ?? '-' }}
          </template>
          <template v-else-if="column.key === 'prompt_tokens'">
            {{ record.kind === 'model' ? (text ?? '-') : '-' }}
          </template>
          <template v-else-if="column.key === 'action'">
            <div style="display:flex;gap:8px">
              <a-button size="small" @click="showDetail(record.run_id)">详情</a-button>
              <span v-if="record.deleted_at" style="color:var(--text-4)">已删除</span>
              <a-button v-else size="small" danger @click="handleDelete([record.id])">删除</a-button>
            </div>
          </template>
        </template>
      </a-table>

      <a-pagination
        v-if="total > pageSize"
        v-model:current="page"
        :total="total"
        :page-size="pageSize"
        :show-total="(t: number) => `共 ${t} 条`"
        style="margin-top:16px;text-align:right"
        @change="onPageChange"
      />
    </SectionCard>

    <!-- 详情抽屉 -->
    <a-drawer
      :open="detailVisible"
      title="Run 审计时间线"
      placement="right"
      width="640"
      @close="closeDetail"
    >
      <a-spin :spinning="detailLoading">
        <a-table
          :data-source="detailEntries"
          :columns="detailColumns"
          :pagination="false"
          size="small"
          row-key="id"
        >
          <template #bodyCell="{ column, text, record }: { column: { key: string }, text: unknown, record: AuditEntryResponse }">
            <template v-if="column.key === 'kind'">
              {{ text === 'model' ? '模型' : '工具' }}
            </template>
            <template v-else-if="column.key === 'model'">
              {{ record.model ?? record.tool_name ?? '-' }}
            </template>
            <template v-else-if="column.key === 'duration_ms'">
              {{ text != null ? `${text}ms` : '-' }}
            </template>
          </template>
        </a-table>
        <EmptyState v-if="!detailLoading && !detailEntries.length" description="该 Run 无审计条目" />
      </a-spin>
    </a-drawer>
  </a-spin>
</template>