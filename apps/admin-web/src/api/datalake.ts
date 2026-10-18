import { http } from '@/api/client'

export interface DatalakeTable {
  name: string
}

export interface DatalakePartition {
  path: string
  table: string
  date: string | null
  part: number | null
}

export interface DatalakeQueryResult {
  columns: string[]
  rows: unknown[][]
  row_count: number
  sql: string
}

export interface RoomTypeMonthlyRow {
  month: string
  room_type_code: string
  room_type_name?: string
  occupancy_rate?: number
  adr_fen?: number
  [key: string]: unknown
}

export async function listDatalakeTables(): Promise<DatalakeTable[]> {
  const response = await http.get<{ tables: DatalakeTable[] }>('/api/admin/datalake/tables')
  return response.data.tables
}

export async function listDatalakePartitions(table: string): Promise<DatalakePartition[]> {
  const response = await http.get<{ table: string; partitions: DatalakePartition[] }>(
    `/api/admin/datalake/tables/${encodeURIComponent(table)}/partitions`,
  )
  return response.data.partitions
}

export async function queryDatalake(sql: string, limit = 1000): Promise<DatalakeQueryResult> {
  const response = await http.post<DatalakeQueryResult>('/api/admin/datalake/query', { sql, limit })
  return response.data
}

export async function previewDatalakeTable(table: string, limit = 20): Promise<DatalakeQueryResult> {
  const response = await http.get<DatalakeQueryResult>(
    `/api/admin/datalake/preview/${encodeURIComponent(table)}`,
    { params: { limit } },
  )
  return response.data
}

export async function getRoomTypeMonthly(
  monthFrom?: string,
  monthTo?: string,
): Promise<RoomTypeMonthlyRow[]> {
  const response = await http.get<{ rows: RoomTypeMonthlyRow[] }>(
    '/api/admin/datalake/analysis/room-type-monthly',
    { params: { month_from: monthFrom, month_to: monthTo } },
  )
  return response.data.rows
}