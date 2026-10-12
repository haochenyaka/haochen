import { http } from '@/api/client'

export interface RoomType {
  id: string
  code: string
  name: string
  base_price_fen: number
  capacity: number
  bed_type: string
  description: string | null
  amenities: string[] | null
  active: boolean
}

export interface RoomTypeCreate {
  code: string
  name: string
  base_price_fen: number
  capacity: number
  bed_type: string
  description?: string | null
  amenities?: string[] | null
  active?: boolean
}

export type RoomTypePatch = Partial<Omit<RoomTypeCreate, 'code'>>

export interface Room {
  id: string
  room_no: string
  room_type_id: string
  floor: number
  status: string
  lock_device_no: string | null
}

export interface RoomCreate {
  room_no: string
  room_type_id: string
  floor: number
  status?: string
  lock_device_no?: string | null
}

export type RoomPatch = Partial<Omit<RoomCreate, 'room_no'>>

export interface Staff {
  id: string
  staff_no: string
  name: string
  position: string
  user_id: string | null
  status: string
}

export interface StaffCreate {
  staff_no: string
  name: string
  position?: string
  user_id?: string | null
  status?: string
}

export type StaffPatch = Partial<Omit<StaffCreate, 'staff_no'>>

export interface Account {
  id: string
  username: string
  display_name: string | null
  role: string
  status: string
  last_login_at: string | null
}

export interface AccountCreate {
  username: string
  password: string
  role: string
  display_name?: string | null
}

export interface AccountPatch {
  display_name?: string | null
  role?: string
  status?: string
  password?: string
}

export const ROOM_STATUSES = ['available', 'occupied', 'cleaning', 'maintenance'] as const
export const ROOM_STATUS_LABELS: Record<string, string> = {
  available: '空闲',
  occupied: '在住',
  cleaning: '清洁中',
  maintenance: '维修',
}
export const ACCOUNT_STATUSES = ['active', 'disabled'] as const
export const ACCOUNT_STATUS_LABELS: Record<string, string> = {
  active: '启用',
  disabled: '禁用',
}
export const ACCOUNT_ROLES = ['admin', 'manager', 'operator', 'staff', 'customer'] as const
export const ACCOUNT_ROLE_LABELS: Record<string, string> = {
  admin: '管理员',
  manager: '店长',
  operator: '客服',
  staff: '员工',
  customer: '客人',
}
export const STAFF_POSITIONS = ['housekeeping', 'front_desk', 'concierge', 'maintenance', 'management'] as const
export const STAFF_POSITION_LABELS: Record<string, string> = {
  housekeeping: '客房保洁',
  front_desk: '前台接待',
  concierge: '礼宾',
  maintenance: '维修',
  management: '管理',
}

export interface HotelConfig {
  hotel_name: string
  address: string | null
  phone: string | null
  brand: string | null
  description: string | null
}

export async function listRoomTypes(includeInactive = true, search?: string): Promise<RoomType[]> {
  const params: Record<string, unknown> = { include_inactive: includeInactive }
  if (search) params.search = search
  const response = await http.get<RoomType[]>('/api/admin/room-types', { params })
  return response.data
}

export async function createRoomType(body: RoomTypeCreate): Promise<RoomType> {
  const response = await http.post<RoomType>('/api/admin/room-types', body)
  return response.data
}

export async function patchRoomType(id: string, body: RoomTypePatch): Promise<RoomType> {
  const response = await http.patch<RoomType>(`/api/admin/room-types/${id}`, body)
  return response.data
}

export async function listRooms(params?: { room_type_id?: string; status_filter?: string; search?: string }): Promise<Room[]> {
  const response = await http.get<Room[]>('/api/admin/rooms', { params })
  return response.data
}

export async function createRoom(body: RoomCreate): Promise<Room> {
  const response = await http.post<Room>('/api/admin/rooms', body)
  return response.data
}

export async function patchRoom(id: string, body: RoomPatch): Promise<Room> {
  const response = await http.patch<Room>(`/api/admin/rooms/${id}`, body)
  return response.data
}

export async function listStaff(params?: { status_filter?: string; search?: string }): Promise<Staff[]> {
  const response = await http.get<Staff[]>('/api/admin/staff', { params })
  return response.data
}

export async function createStaff(body: StaffCreate): Promise<Staff> {
  const response = await http.post<Staff>('/api/admin/staff', body)
  return response.data
}

export async function patchStaff(id: string, body: StaffPatch): Promise<Staff> {
  const response = await http.patch<Staff>(`/api/admin/staff/${id}`, body)
  return response.data
}

export async function listAccounts(params?: { role?: string; search?: string }): Promise<Account[]> {
  const response = await http.get<Account[]>('/api/admin/users', { params })
  return response.data
}

export async function createAccount(body: AccountCreate): Promise<Account> {
  const response = await http.post<Account>('/api/admin/users', body)
  return response.data
}

export async function patchAccount(id: string, body: AccountPatch): Promise<Account> {
  const response = await http.patch<Account>(`/api/admin/users/${id}`, body)
  return response.data
}

export async function getHotelConfig(): Promise<HotelConfig> {
  const response = await http.get<HotelConfig>('/api/admin/hotel-config')
  return response.data
}

export async function updateHotelConfig(body: HotelConfig): Promise<HotelConfig> {
  const response = await http.put<HotelConfig>('/api/admin/hotel-config', body)
  return response.data
}
