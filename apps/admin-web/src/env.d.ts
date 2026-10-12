/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端 API 基址；留空走同源路径（开发态由 vite dev server 代理 /api 到后端） */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
