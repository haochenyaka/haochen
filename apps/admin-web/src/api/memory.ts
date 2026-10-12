import { http } from '@/api/client'

export interface MemoryItem {
  id: string
  guest_id: string
  guest_nickname: string | null
  guest_user_id: string | null
  category: string
  content: string
  sensitive: boolean
  source: string | null
  created_at: string
  updated_at: string
  deleted_at: string | null
}

export interface MemoryListResponse {
  items: MemoryItem[]
  total: number
  categories: string[]
}

export interface PatchMemoryBody {
  sensitive?: boolean | null
  category?: string | null
}

export async function listMemories(params: {
  guest_search?: string
  category?: string
  sensitive?: boolean | null
  include_deleted?: boolean
  offset?: number
  limit?: number
}): Promise<MemoryListResponse> {
  const response = await http.get<MemoryListResponse>('/api/admin/memories', { params })
  return response.data
}

export async function listMemoryCategories(): Promise<string[]> {
  const response = await http.get<string[]>('/api/admin/memories/categories')
  return response.data
}

export async function getMemory(memoryId: string): Promise<MemoryItem> {
  const response = await http.get<MemoryItem>(`/api/admin/memories/${memoryId}`)
  return response.data
}

export async function patchMemory(memoryId: string, body: PatchMemoryBody): Promise<MemoryItem> {
  const response = await http.patch<MemoryItem>(`/api/admin/memories/${memoryId}`, body)
  return response.data
}

export async function deleteMemory(memoryId: string, permanent = false): Promise<void> {
  const params = permanent ? { permanent: true } : {}
  await http.delete(`/api/admin/memories/${memoryId}`, { params })
}

export async function restoreMemory(memoryId: string): Promise<MemoryItem> {
  const response = await http.post<MemoryItem>(`/api/admin/memories/${memoryId}/restore`)
  return response.data
}