import { API_BASE_URL, authHeaders } from '@/api/client'

/** 事件包络：run_id/thread_id/agent_slug/seq + 事件自有字段（与后端 run_events 一致）。 */
export interface RunEventPayload {
  run_id: string
  thread_id: string
  agent_slug: string
  seq?: number
  projected?: boolean
  text?: string
  message_id?: string
  tool?: string
  call_id?: string
  args?: Record<string, unknown>
  status?: string
  duration_ms?: number
  error?: string | null
  error_class?: string | null
  reason?: string
  detail?: string | null
}

export interface RunEvent {
  id: string
  event: string
  data: RunEventPayload
}

export interface StreamHandlers {
  onEvent: (event: RunEvent) => void
  /** 订阅不可用或断开：调用方据此降级为轮询。 */
  onError?: (message: string) => void
  onClose?: () => void
}

export interface StreamHandle {
  close: () => void
}

type Transport = 'fetch' | 'chunked' | 'unsupported'

/** 传输能力探测：H5 用 fetch 流，微信小程序用分块请求，其余平台降级轮询。 */
export function resolveTransport(): Transport {
  if (typeof window !== 'undefined' && typeof window.fetch === 'function') {
    return 'fetch'
  }
  const scope = globalThis as Record<string, unknown>
  if (typeof scope.wx === 'object' && scope.wx !== null) {
    return 'chunked'
  }
  return 'unsupported'
}

function parseFrame(
  frame: string,
  emit: (event: string, id: string, data: string) => void,
): void {
  let id = ''
  let name = 'message'
  const dataLines: string[] = []
  for (const rawLine of frame.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (!line || line.startsWith(':')) {
      continue
    }
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '')
    if (field === 'id') {
      id = value
    } else if (field === 'event') {
      name = value
    } else if (field === 'data') {
      dataLines.push(value)
    }
  }
  if (dataLines.length > 0) {
    emit(name, id, dataLines.join('\n'))
  }
}

function createChunkParser(handlers: StreamHandlers): (chunk: string) => void {
  let buffer = ''
  return (chunk: string) => {
    buffer += chunk.replace(/\r\n/g, '\n')
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      parseFrame(frame, (event, id, data) => {
        try {
          handlers.onEvent({ id, event, data: JSON.parse(data) as RunEventPayload })
        } catch {
          // 单帧解析失败不中断订阅（心跳与注释帧本就不带 data）
        }
      })
      boundary = buffer.indexOf('\n\n')
    }
  }
}

interface ChunkedRequestTask {
  abort: () => void
  onChunkReceived?: (callback: (result: { data: ArrayBuffer }) => void) => void
}

function createUtf8Decoder(): (data: ArrayBuffer) => string {
  let pending = new Uint8Array(0)
  return (data: ArrayBuffer) => {
    const incoming = new Uint8Array(data)
    const bytes = new Uint8Array(pending.length + incoming.length)
    bytes.set(pending)
    bytes.set(incoming, pending.length)
    let output = ''
    let index = 0
    while (index < bytes.length) {
      const byte = bytes[index]
      let size = 1
      let code = byte
      if (byte >= 0xf0) {
        size = 4
        code = byte & 0x07
      } else if (byte >= 0xe0) {
        size = 3
        code = byte & 0x0f
      } else if (byte >= 0xc0) {
        size = 2
        code = byte & 0x1f
      }
      if (index + size > bytes.length) {
        break
      }
      for (let offset = 1; offset < size; offset += 1) {
        code = (code << 6) | (bytes[index + offset] & 0x3f)
      }
      output += String.fromCodePoint(code)
      index += size
    }
    pending = bytes.slice(index)
    return output
  }
}

/**
 * 订阅 Run 事件流（GET /api/guest/chat/stream?run_id=）。
 *
 * 断线续传由调用方按需重建；流是投影，最终以数据库历史为准，故这里的失败只做降级提示。
 */
export function openRunStream(runId: string, handlers: StreamHandlers): StreamHandle {
  const path = `/api/guest/chat/stream?run_id=${encodeURIComponent(runId)}`
  const transport = resolveTransport()

  if (transport === 'fetch') {
    const controller = new AbortController()
    const parse = createChunkParser(handlers)
    const decoder = new TextDecoder()
    fetch(`${API_BASE_URL}${path}`, { headers: authHeaders(), signal: controller.signal })
      .then(async (response) => {
        if (!response.ok || !response.body) {
          handlers.onError?.(`订阅失败（HTTP ${response.status}）`)
          return
        }
        const reader = response.body.getReader()
        for (;;) {
          const { done, value } = await reader.read()
          if (done) {
            break
          }
          parse(decoder.decode(value, { stream: true }))
        }
        handlers.onClose?.()
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return
        }
        handlers.onError?.(error instanceof Error ? error.message : '事件流中断')
      })
    return { close: () => controller.abort() }
  }

  if (transport === 'chunked') {
    const parse = createChunkParser(handlers)
    const decode = createUtf8Decoder()
    let closed = false
    const task = uni.request({
      url: `${API_BASE_URL}${path}`,
      method: 'GET',
      header: authHeaders(),
      enableChunked: true,
      success: (response) => {
        if ((response.statusCode ?? 0) >= 300) {
          handlers.onError?.(`订阅失败（HTTP ${response.statusCode}）`)
        }
      },
      fail: (error) => {
        handlers.onError?.(error.errMsg || '事件流中断')
      },
      complete: () => {
        if (!closed) {
          handlers.onClose?.()
        }
      },
    }) as unknown as ChunkedRequestTask
    task.onChunkReceived?.((chunk) => {
      parse(decode(chunk.data))
    })
    return {
      close: () => {
        closed = true
        task.abort()
      },
    }
  }

  handlers.onError?.('当前平台不支持事件流')
  return { close: () => undefined }
}
