<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  getLatestScorecard,
  computeScorecard,
  listScorecards,
  runQaAudit,
  type ScorecardResponse,
  type ScorecardItem,
  type QaAuditResult,
} from '@/api/quality'
import { EmptyState, SectionCard, StatCard, StatGrid } from '@/components'

const latest = ref<ScorecardResponse | null>(null)
const history = ref<ScorecardItem[]>([])
const loading = ref(true)
const computing = ref(false)
const auditing = ref(false)
const error = ref('')
const auditResult = ref<QaAuditResult | null>(null)
const showAuditResult = ref(false)

onMounted(async () => {
  try {
    const [sc, items] = await Promise.all([getLatestScorecard(), listScorecards()])
    latest.value = sc
    history.value = items
  } catch (err) {
    error.value = (err as { message?: string }).message || '加载失败'
  } finally {
    loading.value = false
  }
})

async function handleCompute() {
  computing.value = true
  error.value = ''
  try {
    const result = await computeScorecard()
    latest.value = result
    history.value = await listScorecards()
  } catch (err) {
    error.value = (err as { message?: string }).message || '计算失败'
  } finally {
    computing.value = false
  }
}

async function handleAudit() {
  auditing.value = true
  error.value = ''
  try {
    auditResult.value = await runQaAudit()
    showAuditResult.value = true
  } catch (err) {
    error.value = (err as { message?: string }).message || '审计扫描失败'
  } finally {
    auditing.value = false
  }
}

function scoreColor(value: number): string {
  if (value >= 0.85) return 'green'
  if (value >= 0.7) return 'orange'
  return 'red'
}
</script>

<template>
  <a-space direction="vertical" :size="16" style="width: 100%">
    <a-alert v-if="error" type="warning" :message="error" show-icon closable @close="error = ''" />

    <SectionCard title="数据质量评分卡">
      <template #extra>
        <a-space>
          <a-button :loading="auditing" :disabled="auditing" @click="handleAudit">
            QA 扫描
          </a-button>
          <a-button type="primary" :loading="computing" :disabled="computing" @click="handleCompute">
            计算评分卡
          </a-button>
        </a-space>
      </template>

      <a-spin :spinning="loading">
        <EmptyState v-if="!loading && !latest" description="暂无评分卡数据，请点击「计算评分卡」" />
        <div v-else-if="latest">
          <StatGrid :column="3">
            <StatCard title="综合评分" :value="(latest.overall_score * 100).toFixed(1)" suffix="%" />
            <StatCard title="生成方式" :value="latest.generated_by === 'manual' ? '手动' : '自动'" />
            <StatCard title="生成时间" :value="latest.created_at?.substring(0, 10) || '—'" />
          </StatGrid>

          <a-divider>五维评分</a-divider>

          <a-table
            :data-source="[
              { key: 'coverage', name: '覆盖率', score: latest.coverage.score, count: latest.coverage.count, desc: '知识缺口数' },
              { key: 'freshness', name: '新鲜度', score: latest.freshness.score, count: latest.freshness.count, desc: '过期文档数' },
              { key: 'correctness', name: '正确性', score: latest.correctness.score, count: latest.correctness.count, desc: '幻觉嫌疑数' },
              { key: 'consistency', name: '一致性', score: latest.consistency.score, count: latest.consistency.count, desc: '内容冲突数' },
              { key: 'recall_quality', name: '召回质量', score: latest.recall_quality.score, count: latest.recall_quality.count, desc: '低召回项数' },
            ]"
            :columns="[
              { title: '维度', dataIndex: 'name', width: 100 },
              { title: '评分', dataIndex: 'score', width: 120 },
              { title: '问题数', dataIndex: 'count', width: 80 },
              { title: '说明', dataIndex: 'desc', width: 120 },
              { title: '状态', key: 'status', width: 100 },
            ]"
            :pagination="false"
            row-key="key"
            size="small"
          >
            <template #bodyCell="{ column, record }: { column: { dataIndex?: string; key?: string }; record: { score: number; count: number; desc: string } }">
              <template v-if="column.dataIndex === 'score'">
                <a-tag :color="scoreColor(record.score)" style="font-size: 14px">
                  {{ (record.score * 100).toFixed(1) }}%
                </a-tag>
              </template>
              <template v-else-if="column.key === 'status'">
                <a-tag v-if="record.count === 0" color="green">健康</a-tag>
                <a-tag v-else-if="record.score >= 0.7" color="orange">需关注</a-tag>
                <a-tag v-else color="red">需治理</a-tag>
              </template>
            </template>
          </a-table>

          <div v-if="latest.summary" style="margin-top: 12px; padding: 16px; background: var(--bg-hover); border-radius: var(--radius-md); color: var(--text-2); font-size: 13px">
            {{ latest.summary }}
          </div>
        </div>
      </a-spin>
    </SectionCard>

    <SectionCard v-if="showAuditResult && auditResult" title="QA 审计结果">
      <a-descriptions :column="4" size="small" bordered>
        <a-descriptions-item label="新增标注">{{ auditResult.labels_created }} 条</a-descriptions-item>
        <a-descriptions-item label="过期文档">{{ auditResult.stale_documents.length }} 篇</a-descriptions-item>
        <a-descriptions-item label="知识缺口">{{ auditResult.coverage_gaps.length }} 个</a-descriptions-item>
        <a-descriptions-item label="低召回 KB">{{ auditResult.low_recall_kbs.length }} 个</a-descriptions-item>
      </a-descriptions>

      <a-collapse style="margin-top: 12px">
        <a-collapse-panel v-if="auditResult.stale_documents.length > 0" key="stale" header="过期文档">
          <a-table
            :data-source="auditResult.stale_documents"
            :columns="[
              { title: '标题', dataIndex: 'title', ellipsis: true },
              { title: '过期天数', dataIndex: 'stale_days', width: 100 },
            ]"
            row-key="id"
            size="small"
            :pagination="false"
          />
        </a-collapse-panel>
        <a-collapse-panel v-if="auditResult.coverage_gaps.length > 0" key="gaps" header="知识缺口">
          <a-table
            :data-source="auditResult.coverage_gaps"
            :columns="[
              { title: '问题', dataIndex: 'question', ellipsis: true },
              { title: '状态', dataIndex: 'status', width: 100 },
            ]"
            row-key="message_id"
            size="small"
            :pagination="false"
          />
        </a-collapse-panel>
      </a-collapse>
    </SectionCard>

    <SectionCard title="历史记录">
      <a-spin :spinning="loading">
        <EmptyState v-if="!loading && history.length === 0" description="暂无历史记录" />
        <a-table
          v-else
          :data-source="history"
          :columns="[
            { title: 'ID', dataIndex: 'id', width: 60 },
            { title: '综合评分', dataIndex: 'overall_score', width: 120 },
            { title: '生成方式', dataIndex: 'generated_by', width: 100 },
            { title: '摘要', dataIndex: 'summary', ellipsis: true },
            { title: '时间', dataIndex: 'created_at', width: 170 },
          ]"
          :pagination="{ pageSize: 10 }"
          row-key="id"
          size="small"
        >
          <template #bodyCell="{ column, record }: { column: { dataIndex?: string }; record: { overall_score: number } }">
            <template v-if="column.dataIndex === 'overall_score'">
              <a-tag :color="scoreColor(record.overall_score)">
                {{ (record.overall_score * 100).toFixed(1) }}%
              </a-tag>
            </template>
          </template>
        </a-table>
      </a-spin>
    </SectionCard>
  </a-space>
</template>