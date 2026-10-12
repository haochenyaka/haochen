import { request } from '@/api/client'

export interface TaskItem {
  id: string
  task_type: string
  room_id: string
  room_no: string
  order_id: string | null
  assigned_staff_id: string | null
  status: string
  payload: Record<string, unknown> | null
  evidence: Record<string, unknown> | null
  due_at: string | null
  accepted_at: string | null
  started_at: string | null
  done_at: string | null
  verified_at: string | null
  escalated_at: string | null
  created_at: string
}

const STATUS_LABELS: Record<string, string> = {
  pending: '待接单',
  accepted: '已接单',
  in_progress: '清洁中',
  done: '待验收',
  verified: '已验收',
  closed: '已完成',
  escalated: '已升级',
}

const TASK_TYPE_LABELS: Record<string, string> = {
  checkout_clean: '退房清洁',
  call_clean: '呼叫清洁',
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] || status
}

export function taskTypeLabel(taskType: string): string {
  return TASK_TYPE_LABELS[taskType] || taskType
}

export function fetchTasks(status?: string): Promise<TaskItem[]> {
  const query = status ? `?status=${status}` : ''
  return request<TaskItem[]>(`/api/staff/tasks${query}`)
}

export function fetchTaskDetail(taskId: string): Promise<TaskItem> {
  return request<TaskItem>(`/api/staff/tasks/${taskId}`)
}

export function acceptTask(taskId: string): Promise<TaskItem> {
  return request<TaskItem>(`/api/staff/tasks/${taskId}/accept`, { method: 'POST' })
}

export function startTask(taskId: string): Promise<TaskItem> {
  return request<TaskItem>(`/api/staff/tasks/${taskId}/start`, { method: 'POST' })
}

export function finishTask(taskId: string, evidence?: Record<string, unknown>): Promise<TaskItem> {
  return request<TaskItem>(`/api/staff/tasks/${taskId}/finish`, {
    method: 'POST',
    data: { evidence } as Record<string, unknown>,
  })
}

export function verifyTask(taskId: string, evidence?: Record<string, unknown>): Promise<TaskItem> {
  return request<TaskItem>(`/api/staff/tasks/${taskId}/verify`, {
    method: 'POST',
    data: { evidence } as Record<string, unknown>,
  })
}

export function closeTask(taskId: string): Promise<TaskItem> {
  return request<TaskItem>(`/api/staff/tasks/${taskId}/close`, { method: 'POST' })
}