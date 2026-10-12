<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { fetchReadiness, type ReadinessPayload } from '@/api/system'
import { fetchOverview, type OverviewResponse } from '@/api/report'
import { getLatestScorecard, type ScorecardResponse } from '@/api/quality'
import { listKbReports, type KbReportItem } from '@/api/kbEval'
import { EmptyState, SectionCard, StatCard, StatGrid } from '@/components'

const router = useRouter()

const readiness = ref<ReadinessPayload | null>(null)
const errorText = ref('')
const overview = ref<OverviewResponse | null>(null)
const overviewError = ref('')
const scorecard = ref<ScorecardResponse | null>(null)
const scorecardError = ref('')
const kbReports = ref<KbReportItem[]>([])

const today = '2026-09-30'

const modules = [
  { title: 'Agent 配置与管理', stage: 'P6', route: 'agents' },
  { title: '会话监控与质检', stage: 'P7', route: 'conversations' },
  { title: '数据报表', stage: 'P7', route: 'reports' },
  { title: '运行健康面板', stage: 'P7', route: 'health' },
  { title: '智能体日志', stage: 'P7', route: 'logs' },
  { title: '酒店基础数据', stage: 'P2', route: 'data' },
  { title: '记忆管理', stage: 'P7', route: 'memories' },
  { title: '人工接线', stage: 'P7', route: 'handoff' },
  { title: 'MCP 服务器', stage: 'P7', route: 'mcp-servers' },
  { title: '智能体评估', stage: 'P8', route: 'eval-agent' },
  { title: '知识库评估', stage: 'P8', route: 'eval-kb' },
  { title: '数据质量评分卡', stage: 'P8', route: 'quality-scorecard' },
]

function go(route: string) {
  router.push({ name: route })
}

onMounted(async () => {
  // 服务状态
  try {
    readiness.value = await fetchReadiness()
  } catch (error) {
    errorText.value = (error as { message?: string }).message || '未知错误'
  }

  // 今日运行概览
  try {
    overview.value = await fetchOverview({ start: today, end: today })
  } catch (error) {
    overviewError.value = (error as { message?: string }).message || '未知错误'
  }

  // 最新评分卡
  try {
    scorecard.value = await getLatestScorecard()
  } catch (error) {
    scorecardError.value = (error as { message?: string }).message || '未知错误'
  }

  // 最近知识库评估报告
  try {
    const reports = await listKbReports(5)
    kbReports.value = reports.filter((r) => !r.superseded_by)
  } catch {
    // 静默失败
  }
})
</script>

<template>
  <a-space
    direction="vertical"
    :size="16"
    style="width: 100%"
  >
    <!-- 服务状态 -->
    <SectionCard title="服务状态">
      <a-spin :spinning="readiness === null && !errorText">
        <template v-if="readiness">
          <a-descriptions
            :column="3"
            size="small"
          >
            <a-descriptions-item label="总体">
              <a-tag :color="readiness.status === 'ready' ? 'green' : 'orange'">
                {{ readiness.status === 'ready' ? '就绪' : '降级' }}
              </a-tag>
            </a-descriptions-item>
            <a-descriptions-item
              v-for="(value, key) in readiness.checks"
              :key="key"
              :label="String(key)"
            >
              {{ value }}
            </a-descriptions-item>
          </a-descriptions>
        </template>
        <a-alert
          v-else-if="errorText"
          type="warning"
          :message="`服务不可达：${errorText}`"
          show-icon
        />
      </a-spin>
    </SectionCard>

    <!-- 今日运行概览 -->
    <SectionCard title="今日运行概览">
      <template v-if="overview">
        <StatGrid :column="4">
          <StatCard title="会话数" :value="overview.reception.threads" />
          <StatCard title="运行次数" :value="overview.reception.runs" />
          <StatCard title="消息量" :value="overview.reception.messages" />
          <StatCard
            title="首次解决率"
            :value="overview.resolution.first_resolution_rate != null ? +(overview.resolution.first_resolution_rate * 100).toFixed(1) : '—'"
            :suffix="overview.resolution.first_resolution_rate != null ? '%' : ''"
          />
        </StatGrid>
        <a-divider style="margin: 16px 0" />
        <StatGrid :column="4">
          <StatCard
            title="任务成功率"
            :value="overview.resolution.task_success_rate != null ? +(overview.resolution.task_success_rate * 100).toFixed(1) : '—'"
            :suffix="overview.resolution.task_success_rate != null ? '%' : ''"
          />
          <StatCard
            title="缓存命中率"
            :value="overview.cache.hit_rate != null ? +(overview.cache.hit_rate * 100).toFixed(1) : '—'"
            :suffix="overview.cache.hit_rate != null ? '%' : ''"
          />
          <StatCard
            title="TTFT P50"
            :value="overview.performance.ttft_p50_ms != null ? `${overview.performance.ttft_p50_ms} ms` : '—'"
          />
          <StatCard
            title="工具调用成功率"
            :value="overview.performance.tool_success_rate != null ? +(overview.performance.tool_success_rate * 100).toFixed(1) : '—'"
            :suffix="overview.performance.tool_success_rate != null ? '%' : ''"
          />
        </StatGrid>
      </template>
      <a-alert
        v-else-if="overviewError"
        type="warning"
        :message="`报表数据暂不可用：${overviewError}`"
        show-icon
      />
    </SectionCard>

    <!-- 质量概览 -->
    <a-row :gutter="16">
      <a-col :span="12">
        <SectionCard title="数据质量评分卡">
          <a-spin :spinning="scorecard === null && !scorecardError">
            <template v-if="scorecard">
              <div style="text-align: center; margin-bottom: 16px">
              <a-progress
                type="dashboard"
                :percent="Math.round(scorecard.overall_score * 100)"
                :format="() => ((scorecard?.overall_score ?? 0) * 100).toFixed(1) + '%'"
                :stroke-color="scorecard.overall_score >= 0.8 ? '#52c41a' : scorecard.overall_score >= 0.6 ? '#faad14' : '#ff4d4f'"
              />
            </div>
            <a-descriptions
              :column="1"
              size="small"
            >
              <a-descriptions-item label="覆盖率">
                {{ (scorecard.coverage.score * 100).toFixed(1) }}%
              </a-descriptions-item>
              <a-descriptions-item label="新鲜度">
                {{ (scorecard.freshness.score * 100).toFixed(1) }}%
              </a-descriptions-item>
              <a-descriptions-item label="正确性">
                {{ (scorecard.correctness.score * 100).toFixed(1) }}%
              </a-descriptions-item>
              <a-descriptions-item label="一致性">
                {{ (scorecard.consistency.score * 100).toFixed(1) }}%
              </a-descriptions-item>
              <a-descriptions-item label="召回质量">
                {{ (scorecard.recall_quality.score * 100).toFixed(1) }}%
              </a-descriptions-item>
            </a-descriptions>
            <template v-if="scorecard.summary">
              <a-divider style="margin: 12px 0" />
              <p style="color: var(--text-3); font-size: 13px">{{ scorecard.summary }}</p>
            </template>
            </template>
            <EmptyState
              v-else-if="!scorecardError"
              description="暂无评分卡数据"
              action-text="前往生成"
              @action="go('quality-scorecard')"
            />
          </a-spin>
        </SectionCard>
      </a-col>

      <a-col :span="12">
        <SectionCard title="知识库评估">
          <template v-if="kbReports.length > 0">
            <a-list
              :data-source="kbReports.slice(0, 3)"
              size="small"
            >
              <template #renderItem="{ item }">
                <a-list-item>
                  <a-list-item-meta>
                    <template #title>
                      <a @click="go('eval-kb')">
                        {{ item.dataset_path.split('/').pop() || item.dataset_path }} (#{{ item.id }})
                      </a>
                    </template>
                    <template #description>
                      召回@{{ item.metrics.k }}: {{ (item.metrics.recall_at_k * 100).toFixed(1) }}%
                      · F1: {{ (item.metrics.f1_at_k * 100).toFixed(1) }}%
                      · {{ item.created_at.slice(0, 10) }}
                    </template>
                  </a-list-item-meta>
                </a-list-item>
              </template>
            </a-list>
          </template>
          <template v-else>
            <EmptyState
              description="暂无评估报告"
              action-text="前往评估"
              @action="go('eval-kb')"
            />
          </template>
        </SectionCard>
      </a-col>
    </a-row>

    <!-- 快捷导航 -->
    <SectionCard title="快捷入口">
      <a-space wrap>
        <a-button @click="go('reports')">
          数据报表
        </a-button>
        <a-button @click="go('health')">
          运行健康面板
        </a-button>
        <a-button @click="go('eval-kb')">
          知识库评估
        </a-button>
        <a-button @click="go('quality-scorecard')">
          数据质量评分卡
        </a-button>
        <a-button @click="go('logs')">
          智能体日志
        </a-button>
      </a-space>
    </SectionCard>

    <!-- 模块进度 -->
    <SectionCard title="模块进度">
      <a-list
        :data-source="modules"
        size="small"
      >
        <template #renderItem="{ item }">
          <a-list-item>
            <a @click="go(item.route)">{{ item.title }}</a>
            <a-tag :color="item.stage.startsWith('P8') ? 'green' : item.stage.startsWith('P7') ? 'blue' : 'orange'">
              {{ item.stage }} 接入
            </a-tag>
          </a-list-item>
        </template>
      </a-list>
    </SectionCard>
  </a-space>
</template>