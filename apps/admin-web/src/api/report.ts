import { http } from '@/api/client'

export interface ReportWindow {
  start: string
  end: string
}

export interface ReceptionModel {
  threads: number
  runs: number
  messages: number
}

export interface ResolutionModel {
  terminal_runs: number
  first_resolved_runs: number
  first_resolution_rate: number | null
  tasks_succeeded: number
  tasks_failed: number
  task_success_rate: number | null
}

export interface HandoffRateModel {
  handoffs: number
  threads: number
  rate: number | null
}

export interface RecallModel {
  offline_recall_at_k: number | null
  offline_k: number | null
  offline_report_id: number | null
  online_runs_with_retrieval: number
  online_runs_cited: number
  online_citation_rate: number | null
}

export interface CacheHitModel {
  prompt_tokens: number
  cached_prompt_tokens: number
  hit_rate: number | null
  estimated_saving: number
}

export interface PerformanceModel {
  ttft_p50_ms: number | null
  ttft_p95_ms: number | null
  ttft_samples: number
  tool_calls: number
  tool_completed: number
  tool_success_rate: number | null
  runs_with_attempts: number
  runs_retried: number
  model_retry_rate: number | null
}

export interface OverviewResponse {
  window: ReportWindow
  reception: ReceptionModel
  resolution: ResolutionModel
  handoff: HandoffRateModel
  recall: RecallModel
  cache: CacheHitModel
  performance: PerformanceModel
}

export interface TokenBucketModel {
  key: string
  prompt_tokens: number
  completion_tokens: number
  cached_prompt_tokens: number
}

export interface TokensResponse {
  window: ReportWindow
  totals: { prompt_tokens: number; completion_tokens: number; cached_prompt_tokens: number }
  by_model: TokenBucketModel[]
  by_agent: TokenBucketModel[]
  by_day: TokenBucketModel[]
}

export interface NameCount {
  name: string
  count: number
}

export interface HotQuestionsResponse {
  window: ReportWindow
  top_tools: NameCount[]
  top_keywords: NameCount[]
}

export interface DailyCountModel {
  day: string
  threads: number
  runs: number
  messages: number
}

export interface ReceptionSeriesResponse {
  window: ReportWindow
  series: DailyCountModel[]
}

export async function fetchOverview(params: { start: string; end: string }): Promise<OverviewResponse> {
  const response = await http.get<OverviewResponse>('/api/admin/reports/overview', { params })
  return response.data
}

export async function fetchTokens(params: { start: string; end: string }): Promise<TokensResponse> {
  const response = await http.get<TokensResponse>('/api/admin/reports/tokens', { params })
  return response.data
}

export async function fetchHotQuestions(params: { start: string; end: string; limit?: number }): Promise<HotQuestionsResponse> {
  const response = await http.get<HotQuestionsResponse>('/api/admin/reports/hot-questions', { params })
  return response.data
}

export async function fetchReceptionSeries(params: { start: string; end: string }): Promise<ReceptionSeriesResponse> {
  const response = await http.get<ReceptionSeriesResponse>('/api/admin/reports/reception-series', { params })
  return response.data
}