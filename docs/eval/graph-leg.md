# 图补充腿自证（P3.2，2026-10-17）

## 验证口径

- 目标：验证 Neo4j 多跳组合（`Hotel → RoomType → Amenity` 与 `Hotel → Place`）能把设施 AND 过滤、周边类别与距离上限组合成一次查询，并作为 **KB 召回的补充腿** 进入同一条带编号的引用上下文。
- 抽取：`graph_retrieval.extract_graph_query` 用确定性关键词/正则识别设施、周边类别、距离与步行时间；未命中不产生任何图查询。
- 数据：本地演示图（当前 PostgreSQL 房型 + 9 个样例周边地点），真实 Neo4j；`graph_seed` 已重新同步。
- 边界：这是**关系型问题的定向验证**，不等同于 169 题 KB 文档问答的总体召回率变化；图事实与文档分块不是同一种口径。

## 实测结果

| 问题 | 抽取条件 | 图腿返回 | 结论 |
| --- | --- | --- | --- |
| 带投影的房型附近500米内有早餐的店 | 设施=投影；类别=餐饮；距离≤500m；房型与周边两腿 | 皓晨大床房、皓晨双床房、皓晨行政房；晨光咖啡（350m/5min） | 设施 AND 与距离约束同时生效，两跳与一跳组合到同一结果 |
| 哪些房型有儿童用品 | 设施=儿童用品；仅房型腿 | 皓晨家庭套房 | 唯一命中，未把仅有 WiFi/投影的房型混入 |
| 有带投影的房型吗 | 设施=投影；仅房型腿 | 皓晨大床房、皓晨双床房、皓晨行政房（上限 3 条，按价格排序） | 房型设施两跳查询可独立使用 |
| 步行5分钟内有地铁吗 | 类别=交通；步行≤5min；仅周边腿 | 晨光路公交站（220m/3min） | 地铁站为 6min，被步行上限正确排除 |
| 附近500米内有药店吗 | 类别=医疗；距离≤500m；仅周边腿 | 空 | 药店实际 700m，距离约束避免误报 |
| 附近有什么 | 无类别/距离/步行条件 | 不触发 | 泛化问题不被无关周边事实污染 |
| 早餐时间是几点 | 普通知识库问题 | 不触发 | 图腿不介入文档型问答 |

## 召回增益的诚实表述

- 上表中的房型-设施关系、周边类别与 `distance_m` / `walk_minutes` 边属性位于 Neo4j；KB 分块没有等价的边与距离字段，单靠向量/BM25 召回**无法保证**“设施全部命中 + 距离上限”的组合约束。
- 因此这里衡量的是**关系事实覆盖**：4 个有结果的结构化问题均返回了预期实体（房型腿受 3 条上限、周边腿受 5 条上限约束），越界问题（500m 内药店）返回空而不是最近项；普通文档问题保持零扰动。
- 合流后 KB 片段与图事实共用连续编号，引用清单以 `source=graph` 区分来源；图故障或超时只丢弃图事实，不影响 KB 结果。

## 测试证据

- `tests/test_graph_retrieval.py`：抽取口径、组合查询入参、降级与编号渲染（不依赖 Neo4j）。
- `tests/integration/test_knowledge_graph.py::test_multi_hop_insight_combines_amenity_and_nearby_filters`：真实 Neo4j 上的设施 AND、距离过滤、组合查询与 limit=0 跳过。
- `tests/integration/test_knowledge_retrieval.py::test_graph_leg_merges_into_cited_context`：图事实与 KB 分块合流后的编号与引用来源。
- `tests/integration/test_knowledge_retrieval.py::test_graph_leg_keeps_context_non_empty_without_kb_match`：只有图事实时上下文仍非空，不会被工具误判为“无相关资料”。

```bash
cd backend
.venv/Scripts/python.exe -m pytest tests/test_graph_retrieval.py -q
.venv/Scripts/python.exe -m pytest \
  tests/integration/test_knowledge_graph.py \
  tests/integration/test_knowledge_retrieval.py -q
```

## 已知限制

- 抽取是关键词/正则规则，不理解同义改写（如未列出的设施别名）；未命中时退回纯 KB 检索，不报错。
- 仅覆盖已建模关系：房型设施、酒店周边类别/距离/步行时间；停车、营业时间等未建模属性仍以 KB 文档为准。
- 周边地点为样例数据，距离与步行时间为估算值，上线前需替换为门店实测数据。
- 图腿有查询上限（房型 3 条、周边 5 条）与 3 秒超时，超过上限的候选不会进入上下文。
