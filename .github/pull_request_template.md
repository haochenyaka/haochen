# 变更概述

<!-- 一到三句话说明改了什么、为什么改 -->

## 变更类型

- [ ] 功能（feat）
- [ ] 修复（fix）
- [ ] 重构 / 性能（refactor / perf）
- [ ] 测试（test）
- [ ] 文档（docs）
- [ ] 构建 / 依赖 / 流水线（chore / ci）

## 关联验收主张

<!-- 本 PR 属于哪条验收主张的取证链路；无关联填「不涉及」 -->

- [ ] A1 三端主链路闭环（P4/P5/P6）
- [ ] A2 密码零明文（P4）
- [ ] A3 事实先落库与幂等（P4/P5）
- [ ] A4 指标由审计派生（P7）
- [ ] A5 数据中心闭环（P8）
- [ ] A6 服务端授权（P2）
- [ ] 不涉及（纯基建 / 文档 / 依赖）

## 验证证据

<!-- 贴出实际执行的命令与结果；CI 无法覆盖的部分给出复现步骤 -->

- [ ] 后端：`ruff check . && ruff format --check . && mypy app`
- [ ] 后端：`pytest -q -m "not integration"`（单测）
- [ ] 后端：`pytest -q -m integration`（连真实 PostgreSQL / Redis）
- [ ] 前端：受影响工程 `npm run lint && npm run type-check && npm run build`
- [ ] 迁移：Alembic `upgrade head` / `downgrade base` / `upgrade head` 双向通过
- [ ] 手工验证（UI / 链路 / 故障注入）：
- 不适用项：

## 安全与合规

- [ ] 无密钥、令牌、真实凭据入库入仓（`.env.example` 仅占位符）
- [ ] 涉及密码 / 门锁链路的改动：明文扫描测试通过（订单、消息、记忆、审计、导出物）
- [ ] 越权负向用例已覆盖（涉及授权判定时）

## 迁移与兼容

- [ ] 无 Schema 变更
- [ ] 含 Alembic 迁移（版本号：），upgrade / downgrade 均已验证

## 文档同步

- [ ] 无文档变更
- [ ] 已同步：<!-- docs/design.md / docs/contracts/*.md / docs/mechanism-baseline.md / README 等 -->

## 提交规范

- [ ] Conventional commits（`feat: …` / `fix: …` / `docs: …` / `chore: …`）
