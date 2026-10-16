"""Supervisor 多智能体运行时：路由决策、回环子图工厂与拓扑装配。

为避免包初始化与 `buildin` 子 Agent 之间形成导入环，本包不在 `__init__` 里聚合导出，
各符号从具体子模块导入：
- `routing`：路由常量、`RouteDecision`、`RouteOutcome`、`SupervisorRouter`
- `state`：`SupervisorState`
- `subgraph`：`build_loop_subgraph`
- `assembly`：`LoopSubAgent`、`build_agent_pipeline`
- `graph`：`build_supervisor_graph`、`SUPERVISOR_SLUG`、`SUPERVISOR_TOOL_IDS`
"""
