import { http } from '@/api/client'

export interface Approval {
  id: string
  run_id: string
  thread_id: string
  tool_name: string
  call_id: string
  reason: string | null
  args_digest: Record<string, unknown> | null
  status: string
  decided_by: string | null
  decided_at: string | null
  created_at: string
}

export async function listPendingApprovals(): Promise<Approval[]> {
  const response = await http.get<Approval[]>('/api/admin/approvals/pending')
  return response.data
}

export async function decideApproval(id: string, approved: boolean): Promise<Approval> {
  const response = await http.post<Approval>(`/api/admin/approvals/${id}/decide`, { approved })
  return response.data
}