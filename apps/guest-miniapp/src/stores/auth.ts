import { reactive } from 'vue'
import { request } from '@/api/client'

export const AUTH_STORAGE_KEY = 'haochen.auth'

const LOGIN_PAGE = '/pages/login/index'
const PUBLIC_PAGES: readonly string[] = [LOGIN_PAGE]

export interface AuthSession {
  token: string
  expiresIn: number
  userId: string
  username: string
  displayName: string
  role: string
  guestId?: string
  nickname?: string
  loggedInAt: string
}

interface WechatLoginResponse {
  token: string
  expires_in: number
  user: { id: string; username: string; display_name: string | null; role: string }
  guest: { id: string; nickname: string | null; openid: string }
}

interface PasswordLoginResponse {
  token: string
  expires_in: number
  user: { id: string; username: string; display_name: string | null; role: string }
}

function readSession(): AuthSession | null {
  try {
    const raw = uni.getStorageSync(AUTH_STORAGE_KEY)
    if (!raw || typeof raw !== 'string') {
      return null
    }
    const parsed = JSON.parse(raw) as Partial<AuthSession>
    if (!parsed.token || !parsed.userId) {
      return null
    }
    return parsed as AuthSession
  } catch {
    return null
  }
}

const state = reactive<{ session: AuthSession | null }>({ session: readSession() })

function persist(session: AuthSession): void {
  state.session = session
  uni.setStorageSync(AUTH_STORAGE_KEY, JSON.stringify(session))
}

export function useAuthState(): { session: AuthSession | null } {
  return state
}

export function getSession(): AuthSession | null {
  return state.session
}

export function getToken(): string | null {
  return state.session?.token ?? null
}

/** 微信小程序登录：uni.login() → code → 后端 code2session → 令牌。 */
export async function wechatLogin(code: string): Promise<AuthSession> {
  const raw = await request<Record<string, unknown>>('/api/guest/auth/wechat', {
    method: 'POST',
    data: { code },
  })
  const data = raw as unknown as WechatLoginResponse

  const missing: string[] = []
  if (!data.token) missing.push('token')
  if (!data.user) missing.push('user')
  if (!data.guest) missing.push('guest')
  if (missing.length > 0) {
    throw new Error(
      `服务端响应缺少字段 ${missing.join(', ')}，完整响应：${JSON.stringify(raw)}`
    )
  }

  const session: AuthSession = {
    token: data.token,
    expiresIn: data.expires_in,
    userId: data.user.id,
    username: data.user.username,
    displayName: data.user.display_name ?? data.user.username,
    role: data.user.role,
    guestId: data.guest.id,
    nickname: data.guest.nickname ?? undefined,
    loggedInAt: new Date().toISOString(),
  }
  persist(session)
  return session
}

/** H5 开发态登录：用户名密码 → 后端 /auth/login → 令牌。 */
export async function devLogin(username: string, password: string): Promise<AuthSession> {
  const data = await request<PasswordLoginResponse>('/api/auth/login', {
    method: 'POST',
    data: { username, password },
    auth: false,
  })
  const session: AuthSession = {
    token: data.token,
    expiresIn: data.expires_in,
    userId: data.user.id,
    username: data.user.username,
    displayName: data.user.display_name ?? data.user.username,
    role: data.user.role,
    loggedInAt: new Date().toISOString(),
  }
  persist(session)
  return session
}

export function clearSession(): void {
  state.session = null
  uni.removeStorageSync(AUTH_STORAGE_KEY)
}

export function signOut(): void {
  clearSession()
  uni.reLaunch({ url: LOGIN_PAGE })
}

/** 页面级守卫：未登录先回登录页。 */
export function requireAuth(): boolean {
  if (state.session) {
    return true
  }
  uni.reLaunch({ url: LOGIN_PAGE })
  return false
}

function isPublicPage(url?: string): boolean {
  if (!url) {
    return true
  }
  const path = url.split('?')[0]
  return PUBLIC_PAGES.includes(path)
}

/** 导航守卫：拦截四类跳转入口，未登录用户只允许去登录页。 */
export function installNavigationGuards(): void {
  const apis = ['navigateTo', 'redirectTo', 'reLaunch', 'switchTab'] as const
  for (const api of apis) {
    uni.addInterceptor(api, {
      invoke: (args: { url?: string }) => {
        if (isPublicPage(args?.url) || state.session) {
          return true
        }
        uni.reLaunch({ url: LOGIN_PAGE })
        return false
      },
    })
  }
}