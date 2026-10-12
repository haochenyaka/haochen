import axios, { type AxiosError } from 'axios'
import { router } from '@/router'
import { clearSession, getToken } from '@/stores/auth'

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? ''

export interface ApiError {
  status: number
  message: string
  retryable: boolean
}

export const http = axios.create({
  baseURL: API_BASE_URL,
  timeout: 15000,
})

http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.set('Authorization', `Bearer ${token}`)
  }
  return config
})

function extractDetail(body: Record<string, unknown>): string | null {
  const detail = body.detail
  if (typeof detail === 'string' && detail) {
    return detail
  }
  if (Array.isArray(detail)) {
    const parts: string[] = []
    for (const item of detail) {
      if (item && typeof item === 'object') {
        const msg = (item as Record<string, unknown>).msg
        if (typeof msg === 'string' && msg) parts.push(msg)
      }
    }
    if (parts.length) return parts.join('；')
  }
  return null
}

function toApiError(error: AxiosError): ApiError {
  const status = error.response?.status ?? 0
  const payload = error.response?.data
  let message = status === 0 ? '网络不可达' : `请求失败（HTTP ${status}）`
  if (payload && typeof payload === 'object') {
    const body = payload as Record<string, unknown>
    const extracted = extractDetail(body)
    if (extracted) {
      message = extracted
    } else if (body.error && typeof body.error === 'object') {
      const nested = body.error as Record<string, unknown>
      if (typeof nested.message === 'string' && nested.message) {
        message = nested.message
      }
    }
  }
  return { status, message, retryable: status === 0 || status >= 500 }
}

export function onUnauthorized(): void {
  clearSession()
  const current = router.currentRoute.value
  if (current.name !== 'login') {
    void router.replace({ name: 'login', query: { redirect: current.fullPath } })
  }
}

http.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    // 401 会话失效即退出登录；403 是权限不足，保留会话并把原因交给调用方
    if ((error.response?.status ?? 0) === 401) {
      onUnauthorized()
    }
    return Promise.reject(toApiError(error))
  },
)
