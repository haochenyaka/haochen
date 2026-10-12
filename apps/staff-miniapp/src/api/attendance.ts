import { request } from '@/api/client'

export interface ClockResult {
  id: string
  direction: string
  review_status: string
  face_score: number | null
  badge_ok: boolean | null
  uniform_ok: boolean | null
}

export interface AttendanceRecord {
  id: string
  direction: string
  photo_url: string
  face_score: number | null
  badge_ok: boolean | null
  uniform_ok: boolean | null
  review_status: string
  reviewed_by: string | null
  reviewed_at: string | null
  created_at: string
}

const DIRECTION_LABELS: Record<string, string> = {
  check_in: '上班打卡',
  check_out: '下班打卡',
}

const REVIEW_LABELS: Record<string, string> = {
  auto_pass: '审核通过',
  manual_review: '待人工复核',
  approved: '人工通过',
  rejected: '人工驳回',
}

export function directionLabel(dir: string): string {
  return DIRECTION_LABELS[dir] || dir
}

export function reviewLabel(status: string): string {
  return REVIEW_LABELS[status] || status
}

export function clock(direction: string, photoUrl: string): Promise<ClockResult> {
  return request<ClockResult>('/api/staff/attendance/clock', {
    method: 'POST',
    data: { direction, photo_url: photoUrl },
  })
}

export function fetchMyRecords(limit = 20, offset = 0): Promise<AttendanceRecord[]> {
  return request<AttendanceRecord[]>(
    `/api/staff/attendance/me?limit=${limit}&offset=${offset}`,
  )
}