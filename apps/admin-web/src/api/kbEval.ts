import { http } from '@/api/client'

export interface DatasetInfo {
  path: string
  name: string
  description: string
  case_count: number
}

export interface KbMetricsModel {
  questions: number
  k: number
  recall_at_k: number
  precision_at_k: number
  f1_at_k: number
  primary_top1_rate: number
  hit_rate: number
  answer_correctness: number | null
  overall_score: number
  overall_metric: string
}

export interface KbOutcomeModel {
  item_id: string
  question: string
  gold_titles: string[]
  primary_title: string | null
  recall: number
  precision: number
  f1: number
  primary_hit: boolean
  hit: boolean
  answer_correctness: number | null
}

export interface KbReportItem {
  id: number
  dataset_path: string
  dataset_fingerprint: string
  content_fingerprint: string
  config_fingerprint: string
  k: number
  questions: number
  evaluator: string | null
  metrics: KbMetricsModel
  superseded_by: number | null
  invalidated_reason: string | null
  created_at: string
}

export interface KbReportDetail extends KbReportItem {
  outcomes: KbOutcomeModel[]
  retrieval_config: Record<string, unknown>
  comparable_reports: number[]
  superseded_reports: number[]
}

export async function listDatasets(): Promise<DatasetInfo[]> {
  const response = await http.get<DatasetInfo[]>('/api/admin/eval/kb/datasets')
  return response.data
}

export async function runKbEval(datasetPath: string, k = 10): Promise<KbReportDetail> {
  const response = await http.post<KbReportDetail>('/api/admin/eval/kb/run', {
    dataset_path: datasetPath,
    k,
  })
  return response.data
}

export async function listKbReports(limit = 20): Promise<KbReportItem[]> {
  const response = await http.get<KbReportItem[]>('/api/admin/eval/kb/reports', {
    params: { limit },
  })
  return response.data
}

export async function getKbReport(reportId: number): Promise<KbReportDetail> {
  const response = await http.get<KbReportDetail>(`/api/admin/eval/kb/reports/${reportId}`)
  return response.data
}

export async function invalidateKbReport(reportId: number, reason?: string): Promise<KbReportItem> {
  const response = await http.post<KbReportItem>(
    `/api/admin/eval/kb/reports/${reportId}/invalidate`,
    reason ? { reason } : {},
  )
  return response.data
}