"""长期记忆写入域：Run 终态后抽取候选记忆并写 `guest_memories`。

读取与注入由中间件工位（`app.agents.middlewares.memory`）与 `DbMemoryProvider` 承担；
本包只负责写入侧：抽取任务提交、候选校验与落库。为避免与 `app.agents` 形成导入环，
本包不在 `__init__` 聚合导出：`extraction`（抽取与校验）、`service`（提交与落库）、
`task_handlers`（`memory.extract` 处理器）。
"""
