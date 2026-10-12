import { request } from '@/api/client'

export interface DeliveryResponse {
  id: string
  room_no: string
  item: { name: string; quantity: number }
  status: string
  external_delivery_id: string | null
  created_at: string
  finished_at: string | null
}

export interface CleanResponse {
  id: string
  task_type: string
  room_no: string
  status: string
  created_at: string
}

export interface ComplaintResponse {
  id: string
  status: string
  created_at: string
}

export function requestDelivery(itemName: string, quantity: number, roomNo?: string): Promise<DeliveryResponse> {
  return request<DeliveryResponse>('/api/guest/services/deliveries', {
    method: 'POST',
    data: { item_name: itemName, item_quantity: quantity, room_no: roomNo } as Record<string, unknown>,
  })
}

export function listDeliveries(limit?: number): Promise<DeliveryResponse[]> {
  const query = limit === undefined ? '' : `?limit=${limit}`
  return request<DeliveryResponse[]>(`/api/guest/services/deliveries${query}`)
}

export function requestClean(roomNo: string, note?: string): Promise<CleanResponse> {
  return request<CleanResponse>('/api/guest/services/clean', {
    method: 'POST',
    data: { room_no: roomNo, note } as Record<string, unknown>,
  })
}

export function submitComplaint(content: string, category?: string, roomNo?: string): Promise<ComplaintResponse> {
  return request<ComplaintResponse>('/api/guest/services/complaints', {
    method: 'POST',
    data: { content, category, room_no: roomNo } as Record<string, unknown>,
  })
}