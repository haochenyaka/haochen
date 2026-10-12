<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  listDatasets,
  listReports,
  runEval,
  getReport,
  type DatasetInfo,
  type ReportItem,
  type ReportDetail,
} from '@/api/eval'
import { EmptyState, SectionCard } from '@/components'

const datasets = ref<DatasetInfo[]>([])
const reports = ref<ReportItem[]>([])
const loading = ref(true)
const running = ref(false)
const error = ref('')
const selectedReport = ref<ReportDetail | null>(null)
const detailLoading = ref(false)

onMounted(async () => {
  try {
    const [ds, rps] = await Promise.all([listDatasets(), listReports()])
    datasets.value = ds
    reports.value = rps
  } catch (err) {
    error.value = (err as { message?: string }).message || '加载失败'
  } finally {
    loading.value = false
  }
})

async function handleRun(datasetPath: string) {
  running.value = true
  error.value = ''
  try {
    const detail = await runEval(datasetPath)
    // refresh report list
    reports.value = await listReports()
    selectedReport.value = detail
  } catch (err) {
    error.value = (err as { message?: string }).message || '评估执行失败'
  } finally {
    running.value = false
  }
}

async function viewDetail(reportId: number) {
  detailLoading.value = true
  try {
    selectedReport.value = await getReport(reportId)
  } catch (err) {
    error.value = (err as { message?: string }).message || '加载详情失败'
  } finally {
    detailLoading.value = false
  }
}

function tagColor(accuracy: number): string {
  if (accuracy >= 1) return 'green'
  if (accuracy >= 0.5) return 'orange'
  return 'red'
}
</script>

<template>
  <a-space direction="vertical" :size="16" style="width: 100%">
    <a-alert v-if="error" type="warning" :message="error" show-icon closable @close="error = ''" />

    <SectionCard title="评估数据集">
      <a-spin :spinning="loading">
        <EmptyState v-if="!loading && datasets.length === 0" description="暂无可用数据集" />
        <a-list v-else :data-source="datasets" size="small">
          <template #renderItem="{ item }">
            <a-list-item>
              <a-list-item-meta>
                <template #title>
                  <span>{{ item.name }}</span>
                  <a-tag style="margin-left: 8px" color="blue">{{ item.case_count }} 用例</a-tag>
                </template>
                <template #description>
                  {{ item.description || '无描述' }}
                </template>
              </a-list-item-meta>
              <template #actions>
                <a-button
                  type="primary"
                  size="small"
                  :loading="running"
                  :disabled="running"
                  @click="handleRun(item.path)"
                >
                  运行实验
                </a-button>
              </template>
            </a-list-item>
          </template>
        </a-list>
      </a-spin>
    </SectionCard>

    <SectionCard title="历史报告">
      <a-spin :spinning="loading">
        <EmptyState v-if="!loading && reports.length === 0" description="暂无评估报告" />
        <a-table
          v-else
          :data-source="reports"
          :columns="[
            { title: 'ID', dataIndex: 'id', width: 80 },
            { title: '数据集', dataIndex: 'dataset_path', ellipsis: true },
            { title: '模型', dataIndex: 'model', width: 120 },
            { title: '用例数', dataIndex: 'total_cases', width: 80 },
            { title: '工具准确率', dataIndex: ['metrics', 'avg_tool_accuracy'], width: 120 },
            { title: '任务成功率', dataIndex: ['metrics', 'task_success_rate'], width: 120 },
            { title: '通过', dataIndex: ['metrics', 'total_passed'], width: 80 },
            { title: '状态', key: 'status', width: 100 },
            { title: '时间', dataIndex: 'created_at', width: 180 },
            { title: '操作', key: 'action', width: 100 },
          ]"
          :pagination="{ pageSize: 10 }"
          row-key="id"
          size="small"
          @row-click="(record: ReportItem) => viewDetail(record.id)"
        >
          <template #bodyCell="{ column, record }: { column: { key?: string; dataIndex?: string }; record: ReportItem }">
            <template v-if="column.dataIndex === 'metrics.avg_tool_accuracy'">
              <a-tag :color="tagColor(record.metrics.avg_tool_accuracy)">
                {{ (record.metrics.avg_tool_accuracy * 100).toFixed(1) }}%
              </a-tag>
            </template>
            <template v-else-if="column.dataIndex === 'metrics.task_success_rate'">
              <span v-if="record.metrics.task_success_rate !== null">
                {{ (record.metrics.task_success_rate * 100).toFixed(1) }}%
              </span>
              <span v-else>-</span>
            </template>
            <template v-else-if="column.dataIndex === 'metrics.total_passed'">
              {{ record.metrics.total_passed }}/{{ record.total_cases }}
            </template>
            <template v-else-if="column.key === 'status'">
              <a-tag v-if="record.superseded_by" color="default">已作废</a-tag>
              <a-tag v-else color="green">有效</a-tag>
            </template>
            <template v-else-if="column.key === 'action'">
              <a-button size="small" @click.stop="viewDetail(record.id)">详情</a-button>
            </template>
          </template>
        </a-table>
      </a-spin>
    </SectionCard>

    <SectionCard v-if="selectedReport" title="报告详情">
      <a-spin :spinning="detailLoading">
        <a-descriptions :column="3" size="small" bordered>
          <a-descriptions-item label="报告 ID">{{ selectedReport.id }}</a-descriptions-item>
          <a-descriptions-item label="数据集">{{ selectedReport.dataset_path }}</a-descriptions-item>
          <a-descriptions-item label="模型">{{ selectedReport.model || '未指定' }}</a-descriptions-item>
          <a-descriptions-item label="工具准确率">
            <a-tag :color="tagColor(selectedReport.metrics.avg_tool_accuracy)">
              {{ (selectedReport.metrics.avg_tool_accuracy * 100).toFixed(1) }}%
            </a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="参数匹配率">
            {{ (selectedReport.metrics.avg_args_match_rate * 100).toFixed(1) }}%
          </a-descriptions-item>
          <a-descriptions-item label="任务成功率">
            <span v-if="selectedReport.metrics.task_success_rate !== null">
              {{ (selectedReport.metrics.task_success_rate * 100).toFixed(1) }}%
            </span>
            <span v-else>-</span>
          </a-descriptions-item>
          <a-descriptions-item label="通过率">
            {{ selectedReport.metrics.total_passed }}/{{ selectedReport.total_cases }}
          </a-descriptions-item>
          <a-descriptions-item label="创建时间">{{ selectedReport.created_at }}</a-descriptions-item>
          <a-descriptions-item label="数据集指纹">
            <a-tooltip :title="selectedReport.dataset_fingerprint">
              <code>{{ selectedReport.dataset_fingerprint.substring(0, 16) }}...</code>
            </a-tooltip>
          </a-descriptions-item>
        </a-descriptions>

        <a-divider>逐条结果</a-divider>

        <a-table
          :data-source="selectedReport.outcomes"
          :columns="[
            { title: '用例', dataIndex: 'name', ellipsis: true },
            { title: '工具准确率', dataIndex: 'tool_accuracy', width: 120 },
            { title: '任务成功', dataIndex: 'task_success', width: 100 },
            { title: '标签', dataIndex: 'tags', width: 200 },
          ]"
          row-key="item_id"
          size="small"
          :pagination="false"
        >
          <template #bodyCell="{ column, record }: { column: { dataIndex?: string }; record: { tool_accuracy: number; task_success: boolean | null; tags: string[] } }">
            <template v-if="column.dataIndex === 'tool_accuracy'">
              <a-tag :color="tagColor(record.tool_accuracy)">
                {{ (record.tool_accuracy * 100).toFixed(0) }}%
              </a-tag>
            </template>
            <template v-else-if="column.dataIndex === 'task_success'">
              <a-tag v-if="record.task_success === true" color="green">是</a-tag>
              <a-tag v-else-if="record.task_success === false" color="red">否</a-tag>
              <span v-else>-</span>
            </template>
            <template v-else-if="column.dataIndex === 'tags'">
              <a-tag v-for="tag in record.tags" :key="tag" color="blue">{{ tag }}</a-tag>
            </template>
          </template>
        </a-table>
      </a-spin>
    </SectionCard>
  </a-space>
</template>