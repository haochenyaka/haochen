import { http } from '@/api/client'

/* ---- Models ---- */

export interface CleaningRule {
  id: number
  name: string
  description: string | null
  task_type: string
  handler_version: number
  schedule_cron: string | null
  config: Record<string, unknown> | null
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface PipelineRun {
  id: string
  rule_id: number | null
  task_id: string | null
  trigger: string
  task_type: string | null
  status: string
  source_table: string | null
  processed_rows: number | null
  affected_rows: number | null
  error_rows: number | null
  duration_seconds: number | null
  summary: Record<string, unknown> | null
  error: string | null
  created_at: string
  finished_at: string | null
}

export interface PipelineStats {
  total_rules: number
  enabled_rules: number
  total_runs: number
  status_counts: Record<string, number>
}

export interface RuleListResponse {
  rules: CleaningRule[]
}

export interface RunListResponse {
  runs: PipelineRun[]
}

export interface TriggerResponse {
  run_id: string
}

export interface RuleCreateBody {
  name: string
  description?: string | null
  task_type: string
  schedule_cron?: string | null
  config?: Record<string, unknown> | null
}

export interface RuleUpdateBody {
  name?: string
  description?: string | null
  schedule_cron?: string | null
  config?: Record<string, unknown> | null
  enabled?: boolean
}

/* ---- API functions ---- */

export async function listRules(): Promise<RuleListResponse> {
  const response = await http.get<RuleListResponse>('/api/admin/data-pipeline/rules')
  return response.data
}

export async function getRule(ruleId: number): Promise<CleaningRule> {
  const response = await http.get<CleaningRule>(`/api/admin/data-pipeline/rules/${ruleId}`)
  return response.data
}

export async function createRule(body: RuleCreateBody): Promise<CleaningRule> {
  const response = await http.post<CleaningRule>('/api/admin/data-pipeline/rules', body)
  return response.data
}

export async function updateRule(ruleId: number, body: RuleUpdateBody): Promise<CleaningRule> {
  const response = await http.put<CleaningRule>(`/api/admin/data-pipeline/rules/${ruleId}`, body)
  return response.data
}

export async function deleteRule(ruleId: number): Promise<void> {
  await http.delete(`/api/admin/data-pipeline/rules/${ruleId}`)
}

export async function triggerPipeline(ruleId: number): Promise<TriggerResponse> {
  const response = await http.post<TriggerResponse>(`/api/admin/data-pipeline/rules/${ruleId}/trigger`)
  return response.data
}

export async function triggerTask(taskType: string): Promise<TriggerResponse> {
  const response = await http.post<TriggerResponse>(`/api/admin/data-pipeline/trigger/${taskType}`)
  return response.data
}

export async function listRuns(params?: { rule_id?: number; status?: string; limit?: number }): Promise<RunListResponse> {
  const response = await http.get<RunListResponse>('/api/admin/data-pipeline/runs', { params })
  return response.data
}

export async function getRun(runId: string): Promise<PipelineRun> {
  const response = await http.get<PipelineRun>(`/api/admin/data-pipeline/runs/${runId}`)
  return response.data
}

export async function getPipelineStats(): Promise<PipelineStats> {
  const response = await http.get<PipelineStats>('/api/admin/data-pipeline/stats')
  return response.data
}