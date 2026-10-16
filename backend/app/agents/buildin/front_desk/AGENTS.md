# front-desk 前台入口（supervisor）

对外是客人的唯一入口，对内是多智能体调度：把诉求派发给合适的子 Agent，并在多诉求时
依次派发后汇总。

## 拓扑

```
START → supervisor（按诉求判定派发目标）
          ├─→ consult      前台咨询（知识问答、房型、时间）
          ├─→ order        订单（预订/改期/续订/取消/发票）
          ├─→ room_service 客房服务（清洁/送物/机器人配送）
          └─→ handoff      转人工（投诉/退款/赔偿 → 生成待处理工单）
                 └─→ 回到 supervisor，无需再派发则 finish → END
```

> 由编译图渲染的 mermaid 版本（与代码同步、含节点职责表）：`docs/agent-topology.md`。

## 路由规则

- 决策以 LLM 结构化输出为主，失败或不可用时落关键词兜底（见 `supervisor/routing.py`）。
- 关键词兜底的优先级：转人工 > 客房服务 > 订单 > 咨询；无命中时派发咨询。
- 已派发过的子 Agent 不重复派发；单次 Run 派发次数有上限，避免空转。
- 未被子 Agent 声明的工具（授权快照里多出来的）归咨询子 Agent。
- 授权感知：决策目标在当前快照下无可见工具、而其他子 Agent 有工具时改派后者；全部子
  Agent 都无工具时按原决策派发（保留无工具直答）。

## 配置

- 本 slug 的 `system_prompt` 覆盖作用于默认子 Agent（咨询）；`model` / `temperature`
  作用于全图。
- 子 Agent 各自的提示词与工具在各自子包（`front_desk_consult` / `front_desk_order` /
  `room_service` / `handoff`）的 AGENTS.md 与本类的 `default_system_prompt` 里声明。

> 说明：本文件供人阅读（拓扑与路由约定），不参与 supervisor 的模型提示词构造。