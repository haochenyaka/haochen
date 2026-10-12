import { http } from '@/api/client'

export type AccountRole = 'admin' | 'manager' | 'operator' | 'staff' | 'customer'

export interface AccountIdentity {
  id: string
  username: string
  display_name: string | null
  role: AccountRole
  via: string
}

export interface LoginResponse {
  token: string
  expires_in: number
  user: AccountIdentity
}

export async function login(username: string, password: string): Promise<LoginResponse> {
  const response = await http.post<LoginResponse>('/api/auth/login', { username, password })
  return response.data
}

export async function fetchMe(): Promise<AccountIdentity> {
  const response = await http.get<AccountIdentity>('/api/auth/me')
  return response.data
}

export async function logout(): Promise<void> {
  await http.post('/api/auth/logout')
}
