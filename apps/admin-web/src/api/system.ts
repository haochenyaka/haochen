import { http } from '@/api/client'

export interface HealthPayload {
  status: string
}

export interface ReadinessPayload {
  status: string
  checks: Record<string, string>
}

export async function fetchHealth(): Promise<HealthPayload> {
  const response = await http.get<HealthPayload>('/api/health')
  return response.data
}

export async function fetchReadiness(): Promise<ReadinessPayload> {
  const response = await http.get<ReadinessPayload>('/api/system/ready')
  return response.data
}
