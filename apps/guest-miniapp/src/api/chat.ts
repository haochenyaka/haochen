import { request } from '@/api/client'

export type Rating = 'up' | 'down'

/** 检索引用：与后端 model_audit.retrieval_refs 同口径，只读展示。 */
export interface Citation {
  chunk_id: string
  document_id: string
  document_title: string
  ordinal: number
  score: number
  recall_score: number
}

export interface ChatMessage {
  message_id: string
  role: string
  text: string
  run_id: string | null
  created_at: string
  citations: Citation[]
  my_rating: Rating | null
}

export interface SubmitResult {
  thread_id: string
  message_id: string
  request_id: string
  run_id: string | null
  queue_position: number | null
  deduplicated: boolean
}

export interface RequestStatus {
  request_id: string
  thread_id: string
  status: string
  queue_position: number | null
  run_id: string | null
  run_status: string | null
  error: string | null
}

/** 转人工：none 表示从未请求过，pending 等待人工接单。 */
export interface HandoffState {
  thread_id: string
  request_id: string | null
  status: string
  reason: string | null
  created_at: string | null
}

interface MessagesPayload {
  messages: ChatMessage[]
}

export function listMessages(threadId: string, limit?: number): Promise<ChatMessage[]> {
  const query = limit === undefined ? '' : `?limit=${limit}`
  return request<MessagesPayload>(`/api/guest/chat/threads/${threadId}/messages${query}`).then(
    (payload) => payload.messages,
  )
}

export function submitMessage(
  threadId: string | null,
  text: string,
  requestId: string,
): Promise<SubmitResult> {
  const body: Record<string, unknown> = {
    content: { text },
    agent_slug: 'front-desk',
    request_id: requestId,
    policy: 'enqueue',
  }
  if (threadId !== null) {
    body.thread_id = threadId
  }
  return request<SubmitResult>('/api/guest/chat/messages', { method: 'POST', data: body })
}

export function setFeedback(
  messageId: string,
  rating: Rating | null,
): Promise<{ message_id: string; rating: Rating | null }> {
  return request<{ message_id: string; rating: Rating | null }>(
    `/api/guest/chat/messages/${messageId}/feedback`,
    { method: 'PUT', data: { rating } },
  )
}

export function fetchRequestStatus(requestId: string): Promise<RequestStatus> {
  return request<RequestStatus>(`/api/guest/chat/requests/${requestId}`)
}

export function requestHandoff(threadId: string, reason?: string): Promise<HandoffState> {
  const data: Record<string, unknown> = reason === undefined ? {} : { reason }
  return request<HandoffState>(`/api/guest/chat/threads/${threadId}/handoff`, {
    method: 'POST',
    data,
  })
}

export function fetchHandoff(threadId: string): Promise<HandoffState> {
  return request<HandoffState>(`/api/guest/chat/threads/${threadId}/handoff`)
}
