import { request } from '@/api/client'

export interface Availability {
  check_in_date: string
  check_out_date: string
  nights: number
  total_rooms: number
  booked_rooms: number
  available_rooms: number
  amount_fen: number
}

export interface RoomTypeOffer {
  code: string
  name: string
  description: string | null
  base_price_fen: number
  capacity: number
  bed_type: string
  amenities: string[]
  availability: Availability | null
}

export interface RoomTypeDetail extends RoomTypeOffer {
  images: string[]
  active: boolean
}

export function listRoomTypes(checkInDate?: string, checkOutDate?: string): Promise<RoomTypeOffer[]> {
  const params: string[] = []
  if (checkInDate) {
    params.push(`check_in_date=${checkInDate}`)
  }
  if (checkOutDate) {
    params.push(`check_out_date=${checkOutDate}`)
  }
  const query = params.length > 0 ? `?${params.join('&')}` : ''
  return request<RoomTypeOffer[]>(`/api/guest/room-types${query}`)
}

export function getRoomType(code: string, checkInDate?: string, checkOutDate?: string): Promise<RoomTypeDetail> {
  const params: string[] = []
  if (checkInDate) {
    params.push(`check_in_date=${checkInDate}`)
  }
  if (checkOutDate) {
    params.push(`check_out_date=${checkOutDate}`)
  }
  const query = params.length > 0 ? `?${params.join('&')}` : ''
  return request<RoomTypeDetail>(`/api/guest/room-types/${code}${query}`)
}

export function formatFen(fen: number): string {
  return `¥${(fen / 100).toFixed(2)}`
}