import { reactive } from 'vue'

export const AUTH_STORAGE_KEY = 'haochen.admin.auth'

export type AdminRole = 'admin' | 'manager' | 'operator'

export const ROLE_LABELS: Record<AdminRole, string> = {
  admin: '管理员',
  manager: '店长',
  operator: '客服',
}

export interface AdminSession {
  token: string
  username: string
  displayName: string | null
  role: AdminRole
  loggedInAt: string
}

export function isAdminRole(value: unknown): value is AdminRole {
  return value === 'admin' || value === 'manager' || value === 'operator'
}

function readSession(): AdminSession | null {
  try {
    const raw = window.localStorage.getItem(AUTH_STORAGE_KEY)
    if (!raw) {
      return null
    }
    const parsed = JSON.parse(raw) as Partial<AdminSession>
    if (!parsed.token || !parsed.username || !isAdminRole(parsed.role)) {
      return null
    }
    return {
      token: parsed.token,
      username: parsed.username,
      displayName: parsed.displayName ?? null,
      role: parsed.role,
      loggedInAt: parsed.loggedInAt || '',
    }
  } catch {
    return null
  }
}

const state = reactive<{ session: AdminSession | null }>({ session: readSession() })

export function useAuthState(): { session: AdminSession | null } {
  return state
}

export function getSession(): AdminSession | null {
  return state.session
}

export function getToken(): string | null {
  return state.session?.token ?? null
}

export function signIn(session: Omit<AdminSession, 'loggedInAt'>): AdminSession {
  const stored: AdminSession = { ...session, loggedInAt: new Date().toISOString() }
  state.session = stored
  window.localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(stored))
  return stored
}

export function clearSession(): void {
  state.session = null
  window.localStorage.removeItem(AUTH_STORAGE_KEY)
}

export function hasRole(...roles: AdminRole[]): boolean {
  const session = state.session
  return session !== null && roles.includes(session.role)
}
