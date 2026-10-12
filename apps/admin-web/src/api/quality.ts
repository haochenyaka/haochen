import { http } from '@/api/client'

export interface DimensionDetail {
  score: number
  count: number
  actionable: number
  detail: Record<string, unknown> | null
}

export interface ScorecardResponse {
  id: number
  generated_by: string
  coverage: DimensionDetail
  freshness: DimensionDetail
  correctness: DimensionDetail
  consistency: DimensionDetail
  recall_quality: DimensionDetail
  overall_score: number
  summary: string | null
  latest_kb_eval_report_id: number | null
  latest_agent_eval_report_id: number | null
  created_at: string
}

export interface ScorecardItem {
  id: number
  overall_score: number
  generated_by: string
  summary: string | null
  created_at: string
}

export interface QaAuditResult {
  labels_created: number
  stale_documents: { id: string; title: string; updated_at: string | null; stale_days: number }[]
  coverage_gaps: { message_id: string; question: string; status: string }[]
  low_recall_kbs: { report_id: number; recall_at_k: number; fingerprint: string; created_at: string }[]
}

export async function getLatestScorecard(): Promise<ScorecardResponse | null> {
  const response = await http.get<ScorecardResponse | null>('/api/admin/quality/scorecards/latest')
  return response.data
}

export async function computeScorecard(): Promise<ScorecardResponse> {
  const response = await http.post<ScorecardResponse>('/api/admin/quality/scorecards/compute')
  return response.data
}

export async function listScorecards(limit = 10): Promise<ScorecardItem[]> {
  const response = await http.get<ScorecardItem[]>('/api/admin/quality/scorecards', {
    params: { limit },
  })
  return response.data
}

export async function runQaAudit(): Promise<QaAuditResult> {
  const response = await http.get<QaAuditResult>('/api/admin/quality/audit')
  return response.data
}