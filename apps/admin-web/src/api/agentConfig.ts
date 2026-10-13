import { http } from '@/api/client'

export interface ConfigItem {
  key: string
  label: string
  kind: string
  default: unknown | null
  options: string[]
  help_text: string | null
  required: boolean
  group: string
}

export interface AgentDetail {
  slug: string
  name: string
  description: string
  config_items: ConfigItem[]
  tool_ids: string[]
  skill_ids: string[]
  knowledge_base_ids: string[]
  configured: boolean
  config: Record<string, unknown> | null
  middleware: Record<string, unknown>
  enabled_tools: string[] | null
  enabled_skills: string[] | null
  enabled_knowledge_bases: string[] | null
}

export interface Tool {
  name: string
  display_name: string
  description: string
  category: string
  skill: string | null
  dangerous: boolean
  read_only: boolean
}

export interface Skill {
  slug: string
  name: string
  description: string
  tool_dependencies: string[]
  mcp_dependencies: string[]
  skill_dependencies: string[]
}

export interface SaveAgentConfigBody {
  config_json?: Record<string, unknown> | null
  enabled_tools?: string[] | null
  enabled_skills?: string[] | null
  enabled_knowledge_bases?: string[] | null
}

export async function listAgents(): Promise<AgentDetail[]> {
  const response = await http.get<AgentDetail[]>('/api/admin/agent-config/agents')
  return response.data
}

export async function getAgentDetail(slug: string): Promise<AgentDetail> {
  const response = await http.get<AgentDetail>(`/api/admin/agent-config/agents/${slug}`)
  return response.data
}

export async function saveAgentConfig(slug: string, body: SaveAgentConfigBody): Promise<AgentDetail> {
  const response = await http.put<AgentDetail>(`/api/admin/agent-config/agents/${slug}`, body)
  return response.data
}

export async function deleteAgentConfig(slug: string): Promise<void> {
  await http.delete(`/api/admin/agent-config/agents/${slug}`)
}

export async function listTools(): Promise<Tool[]> {
  const response = await http.get<Tool[]>('/api/admin/agent-config/tools')
  return response.data
}

export async function listSkills(): Promise<Skill[]> {
  const response = await http.get<Skill[]>('/api/admin/agent-config/skills')
  return response.data
}