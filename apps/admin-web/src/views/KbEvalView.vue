<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  listDatasets,
  listKbReports,
  runKbEval,
  getKbReport,
  invalidateKbReport,
  type DatasetInfo,
  type KbReportItem,
  type KbReportDetail,
} from '@/api/kbEval'
import { EmptyState, SectionCard } from '@/components'

const datasets = ref<DatasetInfo[]>([])
const reports = ref<KbReportItem[]>([])
const loading = ref(true)
const running = ref(false)
const error = ref('')
const selectedReport = ref<KbReportDetail | null>(null)
const detailLoading = ref(false)
const invalidateLoading = ref(false)

onMounted(async () => {
  try {
    const [ds, rps] = await Promise.all([listDatasets(), listKbReports()])
    datasets.value = ds
    reports.value = rps
  } catch (err) {
    error.value = (err as { message?: string }).message || '加载失败'
  } finally {
    loading.value = false
  }
})

async function handleRun() {
  if (datasets.value.length === 0) return
  running.value = true
  error.value = ''
  try {
    const detail = await runKbEval(datasets.value[0].path)
    reports.value = await listKbReports()
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
    selectedReport.value = await getKbReport(reportId)
  } catch (err) {
    error.value = (err as { message?: string }).message || '加载详情失败'
  } finally {
    detailLoading.value = false
  }
}

async function handleInvalidate(reportId: number) {
  invalidateLoading.value = true
  try {
    await invalidateKbReport(reportId, 'manual_invalidation')
    reports.value = await listKbReports()
    selectedReport.value = null
  } catch (err) {
    error.value = (err as { message?: string }).message || '作废失败'
  } finally {
    invalidateLoading.value = false
  }
}

function scoreColor(value: number): string {
  if (value >= 0.9) return 'green'
  if (value >= 0.7) return 'orange'
  return 'red'
}
</script>

<template>
  <a-space direction="vertical" :size="16" style="width: 100%">
    <a-alert v-if="error" type="warning" :message="error" show-icon closable @close="error = ''" />

    <SectionCard title="知识库评估">
      <a-spin :spinning="loading">
        <EmptyState v-if="!loading && datasets.length === 0" description="暂无可用评估集" />
        <div v-else>
          <a-descriptions :column="2" size="small" bordered>
            <a-descriptions-item label="评估集">
              <span v-if="datasets.length > 0">
                {{ datasets[0].name }}（{{ datasets[0].case_count }} 题）
              </span>
            </a-descriptions-item>
            <a-descriptions-item label="操作">
              <a-button
                type="primary"
                :loading="running"
                :disabled="running || datasets.length === 0"
                @click="handleRun"
              >
                运行评估
              </a-button>
            </a-descriptions-item>
          </a-descriptions>
        </div>
      </a-spin>
    </SectionCard>

    <SectionCard title="历史报告">
      <a-spin :spinning="loading">
        <EmptyState v-if="!loading && reports.length === 0" description="暂无评估报告" />
        <a-table
          v-else
          :data-source="reports"
          :columns="[
            { title: 'ID', dataIndex: 'id', width: 60 },
            { title: '评估集', dataIndex: 'dataset_path', ellipsis: true },
            { title: 'K', dataIndex: 'k', width: 50 },
            { title: '题数', dataIndex: 'questions', width: 60 },
            { title: 'recall@K', dataIndex: ['metrics', 'recall_at_k'], width: 100 },
            { title: 'precision@K', dataIndex: ['metrics', 'precision_at_k'], width: 100 },
            { title: 'overall', dataIndex: ['metrics', 'overall_score'], width: 100 },
            { title: '评判模型', dataIndex: 'evaluator', width: 120 },
            { title: '状态', key: 'status', width: 80 },
            { title: '时间', dataIndex: 'created_at', width: 170 },
            { title: '操作', key: 'action', width: 140 },
          ]"
          :pagination="{ pageSize: 10 }"
          row-key="id"
          size="small"
        >
          <template #bodyCell="{ column, record }: { column: { key?: string; dataIndex?: string | string[] }; record: KbReportItem }">
            <template v-if="column.dataIndex === 'metrics.recall_at_k' || column.dataIndex === 'metrics.precision_at_k'">
              <a-tag :color="scoreColor(record.metrics.recall_at_k)">
                {{ (record.metrics.recall_at_k * 100).toFixed(1) }}%
              </a-tag>
            </template>
            <template v-else-if="column.dataIndex === 'metrics.overall_score'">
              <a-tag :color="scoreColor(record.metrics.overall_score)">
                {{ (record.metrics.overall_score * 100).toFixed(1) }}%
              </a-tag>
            </template>
            <template v-else-if="column.key === 'status'">
              <a-tag v-if="record.superseded_by" color="default">已作废</a-tag>
              <a-tag v-else color="green">有效</a-tag>
            </template>
            <template v-else-if="column.key === 'action'">
              <a-space>
                <a-button size="small" @click="viewDetail(record.id)">详情</a-button>
                <a-button
                  v-if="!record.superseded_by"
                  size="small"
                  danger
                  :loading="invalidateLoading"
                  @click="handleInvalidate(record.id)"
                >
                  作废
                </a-button>
              </a-space>
            </template>
          </template>
        </a-table>
      </a-spin>
    </SectionCard>

    <SectionCard v-if="selectedReport" title="报告详情">
      <a-spin :spinning="detailLoading">
        <a-descriptions :column="3" size="small" bordered>
          <a-descriptions-item label="报告 ID">{{ selectedReport.id }}</a-descriptions-item>
          <a-descriptions-item label="评估集">{{ selectedReport.dataset_path }}</a-descriptions-item>
          <a-descriptions-item label="K 值">{{ selectedReport.k }}</a-descriptions-item>
          <a-descriptions-item label="recall@K">
            <a-tag :color="scoreColor(selectedReport.metrics.recall_at_k)">
              {{ (selectedReport.metrics.recall_at_k * 100).toFixed(1) }}%
            </a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="precision@K">
            <a-tag :color="scoreColor(selectedReport.metrics.precision_at_k)">
              {{ (selectedReport.metrics.precision_at_k * 100).toFixed(1) }}%
            </a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="F1@K">
            {{ (selectedReport.metrics.f1_at_k * 100).toFixed(1) }}%
          </a-descriptions-item>
          <a-descriptions-item label="primary@1">
            <a-tag :color="scoreColor(selectedReport.metrics.primary_top1_rate)">
              {{ (selectedReport.metrics.primary_top1_rate * 100).toFixed(1) }}%
            </a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="命中率">
            {{ (selectedReport.metrics.hit_rate * 100).toFixed(1) }}%
          </a-descriptions-item>
          <a-descriptions-item label="答案正确性">
            <span v-if="selectedReport.metrics.answer_correctness !== null">
              {{ (selectedReport.metrics.answer_correctness * 100).toFixed(1) }}%
            </span>
            <span v-else>未配置</span>
          </a-descriptions-item>
          <a-descriptions-item label="综合评分">
            <a-tag :color="scoreColor(selectedReport.metrics.overall_score)">
              {{ (selectedReport.metrics.overall_score * 100).toFixed(1) }}%
            </a-tag>
            <small>（{{ selectedReport.metrics.overall_metric }}）</small>
          </a-descriptions-item>
          <a-descriptions-item label="评判模型">{{ selectedReport.evaluator || '未配置' }}</a-descriptions-item>
          <a-descriptions-item label="创建时间">{{ selectedReport.created_at }}</a-descriptions-item>
        </a-descriptions>

        <a-divider>逐题结果</a-divider>

        <a-table
          :data-source="selectedReport.outcomes"
          :columns="[
            { title: '题目', dataIndex: 'question', ellipsis: true },
            { title: '金标', dataIndex: 'gold_titles', width: 200 },
            { title: 'recall', dataIndex: 'recall', width: 80 },
            { title: 'precision', dataIndex: 'precision', width: 80 },
            { title: 'F1', dataIndex: 'f1', width: 80 },
            { title: 'primary_hit', dataIndex: 'primary_hit', width: 90 },
            { title: 'hit', dataIndex: 'hit', width: 60 },
            { title: '答案正确性', dataIndex: 'answer_correctness', width: 110 },
          ]"
          row-key="item_id"
          size="small"
          :pagination="false"
        >
          <template #bodyCell="{ column, record }: { column: { dataIndex?: string }; record: { recall: number; precision: number; f1: number; primary_hit: boolean; hit: boolean; answer_correctness: number | null; gold_titles: string[] } }">
            <template v-if="column.dataIndex === 'recall' || column.dataIndex === 'precision' || column.dataIndex === 'f1'">
              <a-tag :color="scoreColor(record.recall)">
                {{ (record.recall * 100).toFixed(0) }}%
              </a-tag>
            </template>
            <template v-else-if="column.dataIndex === 'primary_hit'">
              <a-tag v-if="record.primary_hit" color="green">是</a-tag>
              <a-tag v-else color="red">否</a-tag>
            </template>
            <template v-else-if="column.dataIndex === 'hit'">
              <a-tag v-if="record.hit" color="green">是</a-tag>
              <a-tag v-else color="orange">否</a-tag>
            </template>
            <template v-else-if="column.dataIndex === 'answer_correctness'">
              <span v-if="record.answer_correctness !== null">
                <a-tag :color="scoreColor(record.answer_correctness)">
                  {{ (record.answer_correctness * 100).toFixed(0) }}%
                </a-tag>
              </span>
              <span v-else>-</span>
            </template>
            <template v-else-if="column.dataIndex === 'gold_titles'">
              <a-tag v-for="title in record.gold_titles" :key="title" color="blue">{{ title }}</a-tag>
            </template>
          </template>
        </a-table>
      </a-spin>
    </SectionCard>
  </a-space>
</template>