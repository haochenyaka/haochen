import { http } from '@/api/client'

export interface LogEntryResponse {
  kind: string
  id: number
  run_id: string
  seq: number
  agent_slug: string
  status: string
  created_at: string
  duration_ms: number | null
  error: string | null
  model: string | null
  prompt_tokens: number | null
  cached_prompt_tokens: number | null
  completion_tokens: number | null
  retrieval_ref_count: number | null
  tool_name: string | null
  args_digest: Record<string, unknown> | null
  deleted_at: string | null
  guest_username: string | null
  guest_nickname: string | null
}

export interface LogListResponse {
  entries: LogEntryResponse[]
  total: number
  page: number
  page_size: number
  has_more: boolean
}

export interface AuditEntryResponse {
  kind: string
  id: number
  run_id: string
  seq: number
  agent_slug: string
  status: string
  created_at: string
  duration_ms: number | null
  error: string | null
  model: string | null
  prompt_tokens: number | null
  cached_prompt_tokens: number | null
  completion_tokens: number | null
  retrieval_refs: Record<string, unknown>[] | null
  tool_name: string | null
  args_digest: Record<string, unknown> | null
}

export interface AuditTimelineResponse {
  entries: AuditEntryResponse[]
  truncated: boolean
}

export interface SoftDeleteRequest {
  model_ids?: number[]
  tool_ids?: number[]
}

export interface SoftDeleteResponse {
  model_affected: number
  tool_affected: number
  total_affected: number
  log_id: number
}

export interface AdminActionLogResponse {
  id: number
  actor_user_id: string
  actor_username: string
  actor_role: string
  action: string
  target_ids: number[]
  target_count: number
  detail: Record<string, unknown> | null
  created_at: string
}

export async function fetchLogs(params: Record<string, unknown>): Promise<LogListResponse> {
  const response = await http.get<LogListResponse>('/api/admin/logs', { params })
  return response.data
}

export async function fetchRunTimeline(runId: string): Promise<AuditTimelineResponse> {
  const response = await http.get<AuditTimelineResponse>(`/api/admin/logs/runs/${runId}/timeline`)
  return response.data
}

export async function softDeleteLogs(body: SoftDeleteRequest): Promise<SoftDeleteResponse> {
  const response = await http.delete<SoftDeleteResponse>('/api/admin/logs/delete', { data: body })
  return response.data
}

export async function fetchActionLogs(params: { page: number; page_size: number }): Promise<AdminActionLogResponse[]> {
  const response = await http.get<AdminActionLogResponse[]>('/api/admin/logs/action-logs', { params })
  return response.data
}