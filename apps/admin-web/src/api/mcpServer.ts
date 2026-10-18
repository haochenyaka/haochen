import { http } from '@/api/client'

export interface McpTool {
  name: string
  description: string
}

export interface McpServer {
  id: number
  slug: string
  name: string
  description: string | null
  server_url: string
  transport: string
  config_json: Record<string, unknown> | null
  active: boolean
  last_status: string | null
  last_error: string | null
  last_tested_at: string | null
  discovered_tools: McpTool[] | null
  created_at: string
  updated_at: string
}

export interface CreateMcpServerBody {
  slug: string
  name: string
  server_url: string
  transport?: string
  description?: string | null
  config_json?: Record<string, unknown> | null
  active?: boolean
}

export interface UpdateMcpServerBody {
  name?: string | null
  description?: string | null
  server_url?: string | null
  transport?: string | null
  config_json?: Record<string, unknown> | null
  active?: boolean | null
}

export interface McpProbeResult {
  ok: boolean
  status: string
  latency_ms: number
  error: string | null
  tool_count: number
}

export interface McpToolsResponse {
  server_id: number
  slug: string
  tools: McpTool[]
}

export const MCP_TRANSPORTS: { value: string; label: string }[] = [
  { value: 'streamable_http', label: 'streamable_http（推荐）' },
  { value: 'sse', label: 'sse' },
]

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

export async function testMcpServer(serverId: number): Promise<McpProbeResult> {
  const response = await http.post<McpProbeResult>(`/api/admin/mcp-servers/${serverId}/test`)
  return response.data
}

export async function listMcpTools(serverId: number): Promise<McpToolsResponse> {
  const response = await http.get<McpToolsResponse>(`/api/admin/mcp-servers/${serverId}/tools`)
  return response.data
}