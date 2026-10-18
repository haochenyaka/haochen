<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  listRules,
  createRule,
  updateRule,
  deleteRule,
  triggerPipeline,
  listRuns,
  getPipelineStats,
  type CleaningRule,
  type PipelineRun,
  type PipelineStats,
  type RuleCreateBody,
  type RuleUpdateBody,
} from '@/api/dataPipeline'
import { EmptyState, SectionCard, StatCard, StatGrid } from '@/components'

/* ---- State ---- */

const loading = ref(true)
const error = ref('')
const activeTab = ref('overview')

// Stats
const stats = ref<PipelineStats | null>(null)

// Rules
const rules = ref<CleaningRule[]>([])
const ruleModalVisible = ref(false)
const editingRule = ref<CleaningRule | null>(null)
const ruleForm = ref<RuleCreateBody>({ name: '', task_type: '' })
const savingRule = ref(false)

// Runs
const runs = ref<PipelineRun[]>([])
const runsLoading = ref(false)
const runFilter = ref<string>('')

// Trigger
const triggering = ref<Record<number, boolean>>({})

/* ---- Pipeline types for display ---- */

const PIPELINE_TYPES: Record<string, string> = {
  'data_pipeline.clean_conversations': '会话数据脱敏',
  'data_pipeline.dedup_feedback': '反馈数据去重',
  'data_pipeline.archive_audit_logs': '审计记录归档',
  'data_pipeline.validate_kb_consistency': '知识库一致性校验',
  'data_pipeline.clean_qa_labels': '质检标签清洗',
  'data_pipeline.export_messages': '数据湖-消息导出',
  'data_pipeline.export_audits': '数据湖-审计导出',
  'data_pipeline.export_business': '数据湖-订单导出',
  'data_pipeline.build_aggregates': '数据湖-预聚合',
}

const TRIGGER_LABELS: Record<string, string> = {
  manual: '手动',
  scheduled: '定时',
  api: 'API',
}

const STATUS_COLORS: Record<string, string> = {
  pending: 'default',
  running: 'processing',
  completed: 'success',
  failed: 'error',
}

const STATUS_LABELS: Record<string, string> = {
  pending: '等待中',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
}

/* ---- Lifecycle ---- */

onMounted(async () => {
  try {
    const [s, r, rs] = await Promise.all([
      getPipelineStats(),
      listRules(),
      listRuns({ limit: 20 }),
    ])
    stats.value = s
    rules.value = r.rules
    runs.value = rs.runs
  } catch (err) {
    error.value = (err as { message?: string }).message || '加载失败'
  } finally {
    loading.value = false
  }
})

/* ---- Stats ---- */

const statsItems = computed(() => [
  { title: '清洗规则', value: stats.value?.total_rules ?? 0 },
  { title: '已启用', value: stats.value?.enabled_rules ?? 0 },
  { title: '运行总次数', value: stats.value?.total_runs ?? 0 },
  { title: '运行成功', value: stats.value?.status_counts?.completed ?? 0 },
  { title: '运行失败', value: stats.value?.status_counts?.failed ?? 0 },
  { title: '运行中', value: stats.value?.status_counts?.running ?? 0 },
])

/* ---- Rule CRUD ---- */

function openCreateModal() {
  editingRule.value = null
  ruleForm.value = { name: '', task_type: '', description: null, schedule_cron: null, config: null }
  ruleModalVisible.value = true
}

function openEditModal(rule: CleaningRule) {
  editingRule.value = rule
  ruleForm.value = {
    name: rule.name,
    description: rule.description,
    task_type: rule.task_type,
    schedule_cron: rule.schedule_cron,
    config: rule.config,
  }
  ruleModalVisible.value = true
}

async function handleSaveRule() {
  if (!ruleForm.value.name || !ruleForm.value.task_type) {
    message.warning('请填写规则名称和任务类型')
    return
  }
  savingRule.value = true
  try {
    if (editingRule.value) {
      const updated = await updateRule(editingRule.value.id, ruleForm.value as RuleUpdateBody)
      const idx = rules.value.findIndex((r) => r.id === editingRule.value!.id)
      if (idx >= 0) rules.value[idx] = updated
      message.success('规则已更新')
    } else {
      const created = await createRule(ruleForm.value)
      rules.value.unshift(created)
      message.success('规则已创建')
    }
    ruleModalVisible.value = false
  } catch (err) {
    message.error((err as { message?: string }).message || '保存失败')
  } finally {
    savingRule.value = false
  }
}

async function handleDeleteRule(rule: CleaningRule) {
  Modal.confirm({
    title: `确认删除规则「${rule.name}」？`,
    content: '删除后运行历史仍保留，但无法再次触发。',
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    onOk: async () => {
      try {
        await deleteRule(rule.id)
        rules.value = rules.value.filter((r) => r.id !== rule.id)
        message.success('规则已删除')
      } catch (err) {
        message.error((err as { message?: string }).message || '删除失败')
      }
    },
  })
}

async function handleToggleRule(rule: CleaningRule) {
  try {
    const updated = await updateRule(rule.id, { enabled: !rule.enabled })
    const idx = rules.value.findIndex((r) => r.id === rule.id)
    if (idx >= 0) rules.value[idx] = updated
    message.success(updated.enabled ? '规则已启用' : '规则已停用')
  } catch (err) {
    message.error((err as { message?: string }).message || '操作失败')
  }
}

async function handleTriggerRule(rule: CleaningRule) {
  triggering.value[rule.id] = true
  try {
    const result = await triggerPipeline(rule.id)
    message.success(`管道已触发，运行 ID: ${result.run_id.substring(0, 8)}...`)
    // 刷新运行记录
    const rs = await listRuns({ limit: 20 })
    runs.value = rs.runs
    const s = await getPipelineStats()
    stats.value = s
  } catch (err) {
    message.error((err as { message?: string }).message || '触发失败')
  } finally {
    triggering.value[rule.id] = false
  }
}

/* ---- Run history ---- */

async function refreshRuns() {
  runsLoading.value = true
  try {
    const params: { limit?: number; status?: string } = { limit: 20 }
    if (runFilter.value) params.status = runFilter.value
    const rs = await listRuns(params)
    runs.value = rs.runs
  } catch (err) {
    message.error((err as { message?: string }).message || '加载运行记录失败')
  } finally {
    runsLoading.value = false
  }
}

function pipelineTypeLabel(taskType: string | null): string {
  if (!taskType) return '-'
  return PIPELINE_TYPES[taskType] || taskType
}

function fmtDuration(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return '-'
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  return `${(seconds / 60).toFixed(1)}min`
}

function ruleTaskTypeLabel(rule: CleaningRule): string {
  return PIPELINE_TYPES[rule.task_type] || rule.task_type
}
</script>

<template>
  <a-space direction="vertical" :size="16" style="width: 100%">
    <a-alert v-if="error" type="warning" :message="error" show-icon closable @close="error = ''" />

    <SectionCard title="数据管道">
      <a-tabs v-model:activeKey="activeTab">
        <!-- ========== 概览 ========== -->
        <a-tab-pane key="overview" tab="概览">
          <a-spin :spinning="loading">
            <StatGrid :column="6" :gutter="12">
              <StatCard
                v-for="item in statsItems"
                :key="item.title"
                :title="item.title"
                :value="item.value"
              />
            </StatGrid>

            <a-divider>快速操作</a-divider>
            <a-space>
              <a-button type="primary" @click="activeTab = 'rules'">管理规则</a-button>
              <a-button @click="activeTab = 'runs'">查看运行记录</a-button>
            </a-space>
          </a-spin>
        </a-tab-pane>

        <!-- ========== 规则管理 ========== -->
        <a-tab-pane key="rules" tab="清洗规则">
          <template #extra>
            <a-button type="primary" @click="openCreateModal">新建规则</a-button>
          </template>

          <a-spin :spinning="loading">
            <EmptyState
              v-if="!loading && rules.length === 0"
              description="暂无清洗规则，请新建"
            />
            <a-table
              v-if="!loading && rules.length > 0"
              :data-source="rules"
              :columns="[
                { title: '名称', dataIndex: 'name', width: 150 },
                { title: '任务类型', dataIndex: 'task_type', width: 180 },
                { title: '定时调度', dataIndex: 'schedule_cron', width: 120 },
                { title: '状态', dataIndex: 'enabled', width: 80 },
                { title: '操作', key: 'actions', width: 280 },
              ]"
              :pagination="{ pageSize: 10 }"
              row-key="id"
              size="small"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.dataIndex === 'task_type'">
                  {{ ruleTaskTypeLabel(record) }}
                </template>
                <template v-else-if="column.dataIndex === 'schedule_cron'">
                  {{ record.schedule_cron || '仅手动' }}
                </template>
                <template v-else-if="column.dataIndex === 'enabled'">
                  <a-switch :checked="record.enabled" @click="handleToggleRule(record)" />
                </template>
                <template v-else-if="column.key === 'actions'">
                  <a-space>
                    <a-button size="small" type="primary" ghost :loading="triggering[record.id]" :disabled="!record.enabled" @click="handleTriggerRule(record)">
                      执行
                    </a-button>
                    <a-button size="small" @click="openEditModal(record)">编辑</a-button>
                    <a-button size="small" danger @click="handleDeleteRule(record)">删除</a-button>
                  </a-space>
                </template>
              </template>
            </a-table>
          </a-spin>
        </a-tab-pane>

        <!-- ========== 运行记录 ========== -->
        <a-tab-pane key="runs" tab="运行记录">
          <template #extra>
            <a-space>
              <a-select v-model:value="runFilter" style="width: 120px" allow-clear placeholder="状态筛选" @change="refreshRuns">
                <a-select-option value="">全部</a-select-option>
                <a-select-option value="pending">等待中</a-select-option>
                <a-select-option value="running">运行中</a-select-option>
                <a-select-option value="completed">已完成</a-select-option>
                <a-select-option value="failed">失败</a-select-option>
              </a-select>
              <a-button @click="refreshRuns">刷新</a-button>
            </a-space>
          </template>

          <a-spin :spinning="runsLoading">
            <EmptyState
              v-if="!runsLoading && runs.length === 0"
              description="暂无运行记录"
            />
            <a-table
              v-if="!runsLoading && runs.length > 0"
              :data-source="runs"
              :columns="[
                { title: '管道', dataIndex: 'task_type', width: 180 },
                { title: '触发方式', dataIndex: 'trigger', width: 90 },
                { title: '状态', dataIndex: 'status', width: 90 },
                { title: '数据源', dataIndex: 'source_table', width: 130 },
                { title: '处理行数', dataIndex: 'processed_rows', width: 90 },
                { title: '影响行数', dataIndex: 'affected_rows', width: 90 },
                { title: '耗时', dataIndex: 'duration_seconds', width: 80 },
                { title: '时间', dataIndex: 'created_at', width: 160 },
                { title: '错误', dataIndex: 'error', width: 200, ellipsis: true },
              ]"
              :pagination="{ pageSize: 10 }"
              row-key="id"
              size="small"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.dataIndex === 'task_type'">
                  {{ pipelineTypeLabel(record.task_type) }}
                </template>
                <template v-else-if="column.dataIndex === 'trigger'">
                  {{ TRIGGER_LABELS[record.trigger] || record.trigger }}
                </template>
                <template v-else-if="column.dataIndex === 'status'">
                  <a-tag :color="STATUS_COLORS[record.status] || 'default'">
                    {{ STATUS_LABELS[record.status] || record.status }}
                  </a-tag>
                </template>
                <template v-else-if="column.dataIndex === 'source_table'">
                  {{ record.source_table || '-' }}
                </template>
                <template v-else-if="column.dataIndex === 'duration_seconds'">
                  {{ fmtDuration(record.duration_seconds) }}
                </template>
                <template v-else-if="column.dataIndex === 'created_at'">
                  {{ record.created_at?.substring(0, 19)?.replace('T', ' ') }}
                </template>
                <template v-else-if="column.dataIndex === 'error'">
                  <span v-if="record.error" style="color: var(--error)">{{ record.error }}</span>
                  <span v-else>-</span>
                </template>
              </template>
            </a-table>
          </a-spin>
        </a-tab-pane>
      </a-tabs>
    </SectionCard>

    <!-- ========== 规则编辑弹窗 ========== -->
    <a-modal
      v-model:open="ruleModalVisible"
      :title="editingRule ? '编辑规则' : '新建规则'"
      :confirm-loading="savingRule"
      @ok="handleSaveRule"
      @cancel="ruleModalVisible = false"
    >
      <a-form layout="vertical">
        <a-form-item label="规则名称" required>
          <a-input v-model:value="ruleForm.name" placeholder="如：会话数据脱敏" />
        </a-form-item>
        <a-form-item label="任务类型" required>
          <a-select v-model:value="ruleForm.task_type" placeholder="选择清洗管道">
            <a-select-option
              v-for="(label, type) in PIPELINE_TYPES"
              :key="type"
              :value="type"
            >
              {{ label }}
            </a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item label="描述">
          <a-textarea v-model:value="ruleForm.description" :rows="2" placeholder="规则说明" />
        </a-form-item>
        <a-form-item label="定时调度 (cron)">
          <a-input v-model:value="ruleForm.schedule_cron" placeholder="留空=仅手动触发，如 0 2 * * *" />
        </a-form-item>
      </a-form>
    </a-modal>
  </a-space>
</template>