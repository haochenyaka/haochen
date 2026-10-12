# admin-web（管理后台）

Vue 3 + Vite + TypeScript + Ant Design Vue 工程骨架：登录、RBAC 菜单骨架、路由守卫与请求层。
交付阶段：P0.7 空壳可登录；Agent 配置（P6）、会话监控/报表/日志/记忆管理/人工接线（P7）随阶段接入。

## 开发

```bash
npm install
npm run dev             # 开发服务器（默认 5175，/api 代理到 http://localhost:8000）
npm run type-check      # vue-tsc
npm run lint            # eslint
npm run build           # 类型检查 + 构建产物 dist/
```

跨域部署时复制 `.env.example` 为 `.env.local` 并填 API 网关地址（`VITE_API_BASE_URL`）。

## 登录（开发态）

登录页输入用户名并选择角色（管理员 / 店长 / 客服）→ 调用 `GET /api/health` 验证服务连通 →
本地保存会话 → 进入仪表盘。菜单与路由按角色过滤：Agent 配置仅管理员可见；会话监控与人工接线
对客服开放；越权访问重定向回仪表盘。请求层自动附带 `X-Dev-Username` 头，401/403 清会话回登录页。
P2 授权基座落地后替换为真实账号与登录流程。

## 结构

```
src/
├── api/            # 请求层：client.ts（axios 实例 + 鉴权头 + 401/403 处理）、system.ts
├── stores/auth.ts  # 会话状态、角色判定、登录/登出
├── router/index.ts # 路由与菜单单一来源（角色过滤 + 守卫）
├── layouts/        # AdminLayout：侧边菜单 + 头部 + 内容区
└── views/          # LoginView / DashboardView / PlaceholderView / NotFoundView
```
