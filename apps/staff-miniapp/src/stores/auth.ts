import { reactive } from 'vue'

export const AUTH_STORAGE_KEY = 'haochen.auth'

const LOGIN_PAGE = '/pages/login/index'
const PUBLIC_PAGES: readonly string[] = [LOGIN_PAGE]

export interface AuthSession {
  username: string
  displayName: string
  loggedInAt: string
}

function readSession(): AuthSession | null {
  try {
    const raw = uni.getStorageSync(AUTH_STORAGE_KEY)
    if (!raw || typeof raw !== 'string') {
      return null
    }
    const parsed = JSON.parse(raw) as Partial<AuthSession>
    if (!parsed.username) {
      return null
    }
    return {
      username: parsed.username,
      displayName: parsed.displayName || parsed.username,
      loggedInAt: parsed.loggedInAt || '',
    }
  } catch {
    return null
  }
}

const state = reactive<{ session: AuthSession | null }>({ session: readSession() })

export function useAuthState(): { session: AuthSession | null } {
  return state
}

export function getSession(): AuthSession | null {
  return state.session
}

export function signIn(username: string, displayName?: string): AuthSession {
  const session: AuthSession = {
    username,
    displayName: displayName || username,
    loggedInAt: new Date().toISOString(),
  }
  state.session = session
  uni.setStorageSync(AUTH_STORAGE_KEY, JSON.stringify(session))
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
