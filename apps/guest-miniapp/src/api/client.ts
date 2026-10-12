import { clearSession, getToken } from '@/stores/auth'

export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? ''

/** 是否运行于小程序环境（微信/支付宝/字节等）。 */
function isMiniProgram(): boolean {
  return typeof wx !== 'undefined' || typeof my !== 'undefined' || typeof tt !== 'undefined'
}

export interface ApiError {
  status: number
  message: string
  retryable: boolean
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  data?: Record<string, unknown>
  /** 是否附带开发态身份头，默认附带；登录前探测（如健康检查）传 false */
  auth?: boolean
}

function toApiError(status: number, payload: unknown): ApiError {
  let message = `请求失败（HTTP ${status}）`
  if (payload && typeof payload === 'object') {
    const body = payload as Record<string, unknown>
    const detail = body.detail
    if (typeof detail === 'string' && detail) {
      message = detail
    } else if (body.error && typeof body.error === 'object') {
      const error = body.error as Record<string, unknown>
      if (typeof error.message === 'string' && error.message) {
        message = error.message
      }
    }
  }
  return { status, message, retryable: status >= 500 || status === 0 }
}

function handleUnauthorized(): void {
  clearSession()
  uni.reLaunch({ url: '/pages/login/index' })
}

/** 请求头：Bearer 令牌 + JSON。 */
export function authHeaders(): Record<string, string> {
  const header: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) {
    header['Authorization'] = `Bearer ${token}`
  }
  return header
}

export function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', data, auth = true } = options
  const header = auth ? authHeaders() : { 'Content-Type': 'application/json' }
  return new Promise<T>((resolve, reject) => {
    if (isMiniProgram() && !API_BASE_URL) {
      reject({
        status: 0,
        message:
          '小程序端需配置后端地址：复制 .env.example 为 .env.development.local，' +
          '设置 VITE_API_BASE_URL=http://你的局域网IP:8000（开发）或生产环境 HTTPS 地址',
        retryable: false,
      })
      return
    }
    uni.request({
      url: `${API_BASE_URL}${path}`,
      method,
      data,
      header,
      success: (response) => {
        const status = response.statusCode ?? 0
        if (status >= 200 && status < 300) {
          resolve(response.data as T)
          return
        }
        if (status === 401 || status === 403) {
          handleUnauthorized()
        }
        reject(toApiError(status, response.data))
      },
      fail: (error) => {
        reject({ status: 0, message: error.errMsg || '网络不可达', retryable: true })
      },
    })
  })
}
