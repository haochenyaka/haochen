import { http } from '@/api/client'

export interface ConversationSummary {
  thread_id: string
  title: string | null
  agent_slug: string
  username: string
  user_display_name: string | null
  message_count: number
  handoff_status: string | null
  labels: string[]
  last_message_at: string | null
  created_at: string
  updated_at: string
}

export interface MessageView {
  message_id: string
  seq: number
  role: string
  text: string
  run_id: string | null
  created_at: string
  meta: Record<string, unknown> | null
}

export interface Label {
  id: number
  label: string
  source: string
  confidence: number | null
  comment: string | null
  created_by: string | null
  created_at: string
}

export interface ThreadDetail {
  thread_id: string
  title: string | null
  agent_slug: string
  username: string
  user_display_name: string | null
  created_at: string
  updated_at: string
  messages: MessageView[]
  labels: Label[]
}

export interface ConversationListResponse {
  items: ConversationSummary[]
  total: number
}

export interface HandoffSummary {
  id: string
  thread_id: string
  username: string
  reason: string | null
  status: string
  assigned_to_username: string | null
  assigned_at: string | null
  note: string | null
  closed_at: string | null
  created_at: string
  updated_at: string
}

export interface HandoffListResponse {
  items: HandoffSummary[]
  total: number
}

export interface OperatorReplyBody {
  text: string
}

export interface OperatorReplyResponse {
  message_id: string
  thread_id: string
  text: string
  created_at: string
}

export async function listConversations(params: {
  agent_slug?: string
  status?: string
  label?: string
  search?: string
  offset?: number
  limit?: number
}): Promise<ConversationListResponse> {
  const response = await http.get<ConversationListResponse>('/api/admin/conversations', { params })
  return response.data
}

export async function getThreadDetail(threadId: string): Promise<ThreadDetail> {
  const response = await http.get<ThreadDetail>(`/api/admin/conversations/${threadId}`)
  return response.data
}

export async function setLabel(threadId: string, body: { label: string; comment?: string | null }): Promise<Label> {
  const response = await http.post<Label>(`/api/admin/conversations/${threadId}/labels`, body)
  return response.data
}

export async function removeLabel(threadId: string, labelId: number): Promise<void> {
  await http.delete(`/api/admin/conversations/${threadId}/labels/${labelId}`)
}

export async function listHandoffs(params: {
  status?: string
  offset?: number
  limit?: number
}): Promise<HandoffListResponse> {
  const response = await http.get<HandoffListResponse>('/api/admin/conversations/handoffs/all', { params })
  return response.data
}

export async function acceptHandoff(handoffId: string, body?: { note?: string }): Promise<HandoffSummary> {
  const response = await http.post<HandoffSummary>(`/api/admin/conversations/handoffs/${handoffId}/accept`, body ?? {})
  return response.data
}

export async function closeHandoff(handoffId: string, body?: { note?: string }): Promise<HandoffSummary> {
  const response = await http.post<HandoffSummary>(`/api/admin/conversations/handoffs/${handoffId}/close`, body ?? {})
  return response.data
}

export async function sendOperatorReply(threadId: string, body: OperatorReplyBody): Promise<OperatorReplyResponse> {
  const response = await http.post<OperatorReplyResponse>(`/api/admin/conversations/threads/${threadId}/operator-reply`, body)
  return response.data
}