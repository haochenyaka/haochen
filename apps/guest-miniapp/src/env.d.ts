/// <reference types="vite/client" />

// 小程序平台全局对象（微信 wx / 支付宝 my / 字节 tt）。
// 仅用于运行时存在性判断；具体 API 由 uni-app 封装，此处无需完整类型。
declare const wx: Record<string, unknown>
declare const my: Record<string, unknown>
declare const tt: Record<string, unknown>

declare module '*.vue' {
  import { DefineComponent } from 'vue'
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const component: DefineComponent<{}, {}, any>
  export default component
}

interface ImportMetaEnv {
  /** 后端 API 基址；留空走同源路径（开发态由 vite dev server 代理 /api 到后端） */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
