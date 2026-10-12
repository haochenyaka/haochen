# guest-miniapp（C 端客人小程序）

uni-app（Vue 3 + Vite + TypeScript）工程骨架：登录、tabBar 路由、请求层与鉴权拦截。
交付阶段：P0.7 空壳可登录；业务页面（咨询、预订、客房服务、退房）随 P3–P6 接入。

## 开发

```bash
npm install
npm run dev:h5          # h5 浏览器预览（默认 5173，/api 代理到 http://localhost:8000）
npm run dev:mp-weixin   # 微信开发者工具导入 dist/dev/mp-weixin
npm run type-check      # vue-tsc
npm run lint            # eslint
npm run build:h5        # 构建产物 dist/build/h5
```

后端联调：h5 开发态由 vite 代理 `/api`，无需额外配置；微信小程序端复制 `.env.example` 为
`.env.development.local` 并填局域网地址（如 `VITE_API_BASE_URL=http://192.168.1.10:8000`）。

## 登录（开发态）

登录页输入用户名 → 调用 `GET /api/health` 验证服务连通 → 本地保存会话（`uni.setStorageSync`）→
进入首页；未登录访问任意页面会被导航守卫拦截回登录页。请求层自动附带 `X-Dev-Username` 头
（后端开发态身份入口）。P2 授权基座与 P4 微信登录接入后替换为真实令牌。

## 结构

```
src/
├── api/            # 请求层：client.ts（uni.request 封装 + 鉴权头 + 401 处理）、system.ts
├── stores/auth.ts  # 会话状态、登录/登出、导航守卫
├── pages/          # login / home / orders / profile
├── pages.json      # 路由与 tabBar
└── App.vue         # 启动时安装导航守卫
```
