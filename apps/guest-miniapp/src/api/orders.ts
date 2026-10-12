import { request } from '@/api/client'

export interface OrderSummary {
  order_no: string
  status: string
  room_type_code: string | null
  room_type_name: string | null
  check_in_date: string
  check_out_date: string
  nights: number
  amount_fen: number
  paid_amount_fen: number | null
  room_no: string | null
  created_at: string
}

export interface CredentialSummary {
  version: number
  reason: string
  valid_from: string
  valid_to: string
}

export interface InvoiceSummary {
  id: string
  title: string
  tax_no: string | null
  amount_fen: number
  status: string
  created_at: string
  issued_at: string | null
}

export interface OrderDetail extends OrderSummary {
  paid_at: string | null
  checked_in_at: string | null
  completed_at: string | null
  cancelled_at: string | null
  cancel_reason: string | null
  floor: number | null
  credential: CredentialSummary | null
  invoice: InvoiceSummary | null
  events: Array<{ type: string; created_at: string }>
}

export interface PayInitiationResponse {
  order_no: string
  amount_fen: number
  prepay_id: string
  pay_params: Record<string, unknown>
}

export function listOrders(limit?: number): Promise<OrderSummary[]> {
  const query = limit === undefined ? '' : `?limit=${limit}`
  return request<OrderSummary[]>(`/api/guest/orders${query}`)
}

export function getOrder(orderNo: string): Promise<OrderDetail> {
  return request<OrderDetail>(`/api/guest/orders/${orderNo}`)
}

export function createOrder(body: {
  room_type_code: string
  check_in_date: string
  check_out_date: string
  guest_count?: number
  idempotency_key?: string
}): Promise<OrderDetail> {
  return request<OrderDetail>('/api/guest/orders', { method: 'POST', data: body as Record<string, unknown> })
}

export function rescheduleOrder(orderNo: string, checkInDate: string, checkOutDate: string): Promise<OrderDetail> {
  return request<OrderDetail>(`/api/guest/orders/${orderNo}/reschedule`, {
    method: 'POST',
    data: { check_in_date: checkInDate, check_out_date: checkOutDate } as Record<string, unknown>,
  })
}

export function renewOrder(orderNo: string, checkOutDate: string): Promise<OrderDetail> {
  return request<OrderDetail>(`/api/guest/orders/${orderNo}/renew`, {
    method: 'POST',
    data: { check_out_date: checkOutDate } as Record<string, unknown>,
  })
}

export function cancelOrder(orderNo: string, reason?: string): Promise<OrderDetail> {
  return request<OrderDetail>(`/api/guest/orders/${orderNo}/cancel`, {
    method: 'POST',
    data: reason ? { reason } as Record<string, unknown> : {},
  })
}

export function applyInvoice(orderNo: string, title: string, taxNo?: string): Promise<InvoiceSummary> {
  const data: Record<string, unknown> = { title }
  if (taxNo) {
    data.tax_no = taxNo
  }
  return request<InvoiceSummary>(`/api/guest/orders/${orderNo}/invoice`, { method: 'POST', data })
}

export function payOrder(orderNo: string): Promise<PayInitiationResponse> {
  return request<PayInitiationResponse>(`/api/guest/orders/${orderNo}/pay`, { method: 'POST', data: {} })
}

export function checkoutOrder(orderNo: string): Promise<{ order_no: string; status: string }> {
  return request<{ order_no: string; status: string }>(`/api/guest/services/orders/${orderNo}/checkout`, { method: 'POST' })
}

export const ORDER_STATUS_LABELS: Record<string, string> = {
  created: '待支付',
  pending_payment: '支付中',
  paid: '已支付',
  checked_in: '已入住',
  checkout_pending: '待退房',
  completed: '已完成',
  cancelled: '已取消',
}

export function isTerminal(status: string): boolean {
  return status === 'completed' || status === 'cancelled'
}

export function canPay(status: string): boolean {
  return status === 'created' || status === 'pending_payment'
}

export function canCancel(status: string): boolean {
  return status === 'created' || status === 'pending_payment' || status === 'paid'
}

export function canReschedule(status: string): boolean {
  return status === 'created' || status === 'paid'
}

export function canRenew(status: string): boolean {
  return status === 'paid' || status === 'checked_in'
}

export function canInvoice(status: string): boolean {
  return status === 'paid' || status === 'checked_in' || status === 'checkout_pending' || status === 'completed'
}

export function formatFen(fen: number): string {
  return `¥${(fen / 100).toFixed(2)}`
}

export function statusColor(status: string): string {
  const colors: Record<string, string> = {
    created: '#f59e0b',
    pending_payment: '#f59e0b',
    paid: '#1f6feb',
    checked_in: '#10b981',
    checkout_pending: '#10b981',
    completed: '#6b7280',
    cancelled: '#d03050',
  }
  return colors[status] || '#6b7280'
}