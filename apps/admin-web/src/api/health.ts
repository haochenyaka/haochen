import { http } from '@/api/client'

export interface RunStatusDistributionModel {
  counts: Record<string, number>
  total: number
  interrupted: number
  interrupt_ratio: number | null
}

export interface CacheHitTrendModel {
  prompt_tokens: number
  cached_prompt_tokens: number
  hit_rate: number | null
  estimated_saving: number
}

export interface AlertRuleModel {
  rule: string
  severity: 'warning' | 'critical' | string
  triggered: boolean
  value: number | null
  threshold: number | null
  summary: string
  facts: Record<string, unknown>
}

export interface AlertsModel {
  status: 'ok' | 'warning' | 'critical' | string
  evaluated_at: string
  rules: AlertRuleModel[]
}

export async function fetchRunStatusDistribution(params: { start?: string; end?: string }): Promise<RunStatusDistributionModel> {
  const response = await http.get<RunStatusDistributionModel>('/api/admin/health/run-status-distribution', { params })
  return response.data
}

export async function fetchCacheHitTrend(params: { start: string; end: string }): Promise<CacheHitTrendModel[]> {
  const response = await http.get<CacheHitTrendModel[]>('/api/admin/health/cache-hit-trend', { params })
  return response.data
}

export async function fetchAlerts(): Promise<AlertsModel> {
  const response = await http.get<AlertsModel>('/api/admin/health/alerts')
  return response.data
}