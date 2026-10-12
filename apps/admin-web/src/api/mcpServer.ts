import { http } from '@/api/client'

export interface McpServer {
  id: number
  slug: string
  name: string
  description: string | null
  server_url: string
  config_json: Record<string, unknown> | null
  active: boolean
  created_at: string
  updated_at: string
}

export interface CreateMcpServerBody {
  slug: string
  name: string
  server_url: string
  description?: string | null
  config_json?: Record<string, unknown> | null
  active?: boolean
}

export interface UpdateMcpServerBody {
  name?: string | null
  description?: string | null
  server_url?: string | null
  config_json?: Record<string, unknown> | null
  active?: boolean | null
}

export async function listMcpServers(activeOnly = false): Promise<McpServer[]> {
  const response = await http.get<McpServer[]>('/api/admin/mcp-servers', {
    params: { active_only: activeOnly },
  })
  return response.data
}

export async function getMcpServer(serverId: number): Promise<McpServer> {
  const response = await http.get<McpServer>(`/api/admin/mcp-servers/${serverId}`)
  return response.data
}

export async function createMcpServer(body: CreateMcpServerBody): Promise<McpServer> {
  const response = await http.post<McpServer>('/api/admin/mcp-servers', body)
  return response.data
}

export async function updateMcpServer(serverId: number, body: UpdateMcpServerBody): Promise<McpServer> {
  const response = await http.put<McpServer>(`/api/admin/mcp-servers/${serverId}`, body)
  return response.data
}

export async function deleteMcpServer(serverId: number): Promise<void> {
  await http.delete(`/api/admin/mcp-servers/${serverId}`)
}