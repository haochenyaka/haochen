import { http } from '@/api/client'

export interface DatasetInfo {
  path: string
  name: string
  description: string
  case_count: number
  fingerprint: string
}

export interface MetricsModel {
  questions: number
  avg_tool_accuracy: number
  avg_args_match_rate: number
  task_success_rate: number | null
  total_passed: number
  by_tag: Record<string, { count: number; pass_rate: number }>
}

export interface ReportItem {
  id: number
  dataset_path: string
  dataset_fingerprint: string
  config_fingerprint: string
  model: string | null
  total_cases: number
  metrics: MetricsModel
  superseded_by: number | null
  invalidated_reason: string | null
  created_at: string
}

export interface OutcomeModel {
  item_id: string
  name: string
  tool_accuracy: number
  task_success: boolean | null
  tags: string[]
}

export interface ReportDetail extends ReportItem {
  outcomes: OutcomeModel[]
}

export async function listDatasets(): Promise<DatasetInfo[]> {
  const response = await http.get<DatasetInfo[]>('/api/admin/eval/agent/datasets')
  return response.data
}

export async function runEval(datasetPath: string): Promise<ReportDetail> {
  const response = await http.post<ReportDetail>('/api/admin/eval/agent/run', {
    dataset_path: datasetPath,
  })
  return response.data
}

export async function listReports(limit = 20): Promise<ReportItem[]> {
  const response = await http.get<ReportItem[]>('/api/admin/eval/agent/reports', {
    params: { limit },
  })
  return response.data
}

export async function getReport(reportId: number): Promise<ReportDetail> {
  const response = await http.get<ReportDetail>(`/api/admin/eval/agent/reports/${reportId}`)
  return response.data
}

export async function invalidateReport(reportId: number): Promise<ReportItem> {
  const response = await http.post<ReportItem>(`/api/admin/eval/agent/reports/${reportId}/invalidate`)
  return response.data
}