import { request } from '@/api/client'

export interface HealthPayload {
  status: string
}

export interface ReadinessPayload {
  status: string
  checks: Record<string, string>
}

export function fetchHealth(): Promise<HealthPayload> {
  return request<HealthPayload>('/api/health', { auth: false })
}

export function fetchReadiness(): Promise<ReadinessPayload> {
  return request<ReadinessPayload>('/api/system/ready')
}
