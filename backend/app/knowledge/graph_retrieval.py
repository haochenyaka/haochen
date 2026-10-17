"""图检索腿：从问题确定性抽取关系图条件，查询多跳事实并渲染为带编号的补充资料。

触发口径（不做模型抽取，未命中不产生任何图查询）：
- 房型设施：出现设施关键词，且问题带房型语境（房型/房间/客房/入住/住/床），
  或没有周边语境（如"有投影仪吗"）；
- 周边地点：出现附近性语境，并且至少给出类别、距离或步行时间之一，
  避免"附近有停车场吗"这类图未建模的问题被无关周边事实污染。

边界：图是知识库召回的**补充腿**，不是替代；图不可达或超时只降级为无图事实，
不阻塞、不改变知识库检索结果。图事实来自 Neo4j，价格以 PostgreSQL 导入值为准。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from app.config import Settings, get_settings
from app.knowledge.graph import (
    DEFAULT_HOTEL_CODE,
    GraphError,
    GraphInsight,
    KnowledgeGraph,
)

logger = logging.getLogger(__name__)

ROOM_LIMIT = 3
PLACE_LIMIT = 5
MIN_DISTANCE_M = 10  # 过滤“1.2 米以下儿童”这类身高描述
ROOM_MARKERS = ("房型", "房间", "客房", "入住", "住", "床")
NEARBY_MARKERS = (
    "附近",
    "周边",
    "周围",
    "旁边",
    "步行",
    "走路",
    "多远",
    "最近",
    "米内",
    "公里内",
    "米以内",
    "公里以内",
    "m内",
    "km内",
    "距酒店",
    "距这里",
    "距这儿",
)
AMENITY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("投影", ("投影", "投屏")),
    ("儿童用品", ("儿童用品", "儿童床", "婴儿床", "亲子")),
    ("智能门锁", ("智能门锁", "门锁")),
    ("wifi", ("wifi", "无线网", "无线网络")),
)
PLACE_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("便利店", ("便利店", "小卖部")),
    ("餐饮", ("早餐", "餐厅", "吃饭", "吃的", "小吃", "夜宵", "咖啡", "餐饮")),
    ("交通", ("地铁", "公交", "车站", "火车站", "高铁", "打车", "交通")),
    ("医疗", ("医院", "急诊", "药店", "买药", "医疗", "看病")),
    ("休闲", ("公园", "散步", "晨跑", "休闲", "景点", "好玩")),
    ("购物", ("商场", "购物", "超市", "影院", "买东西")),
)
DISTANCE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(公里|千米|km|米|m)", re.IGNORECASE)
WALK_MINUTES_RE = re.compile(
    r"(?:步行|走路|走过去?)\s*(?:约|大约)?\s*(\d+)\s*分钟"
    r"|(\d+)\s*分钟(?:以内|内)?\s*(?:步行|走路)"
)

GRAPH_HEADER = "关系图补充事实（房型-设施与酒店周边关系）："


@dataclass(frozen=True)
class GraphQuery:
    amenities: tuple[str, ...] = ()
    place_category: str | None = None
    max_distance_m: int | None = None
    max_walk_minutes: int | None = None
    wants_rooms: bool = False
    wants_places: bool = False

    @property
    def triggered(self) -> bool:
        return self.wants_rooms or self.wants_places


@dataclass(frozen=True)
class GraphRetrieval:
    query: GraphQuery
    insight: GraphInsight = field(default_factory=GraphInsight)
    degraded: bool = False
    notes: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return self.insight.is_empty

    def facts(self) -> list[dict[str, object]]:
        """与 render 同序：房型在前、周边在后；source 固定为 graph 便于审计区分。"""
        facts: list[dict[str, object]] = []
        for room in self.insight.rooms:
            facts.append(
                {
                    "source": "graph",
                    "kind": "room_type",
                    "code": room.code,
                    "name": room.name,
                    "capacity": room.capacity,
                    "bed_type": room.bed_type,
                    "base_price_fen": room.base_price_fen,
                    "amenities": list(room.amenities),
                }
            )
        for place in self.insight.places:
            facts.append(
                {
                    "source": "graph",
                    "kind": "place",
                    "code": place.code,
                    "name": place.name,
                    "category": place.category,
                    "description": place.description,
                    "distance_m": place.distance_m,
                    "walk_minutes": place.walk_minutes,
                }
            )
        return facts

    def render(self, *, start_index: int = 1) -> str:
        lines: list[str] = []
        index = start_index
        for room in self.insight.rooms:
            amenities = "、".join(room.amenities) or "无"
            lines.append(
                f"[{index}] 房型：{room.name}（{room.capacity} 人，{room.bed_type} 床，"
                f"{_yuan(room.base_price_fen)} 元/晚），设施：{amenities}"
            )
            index += 1
        for place in self.insight.places:
            lines.append(
                f"[{index}] 周边：{place.name}（{place.category}），"
                f"距离约 {place.distance_m} 米，步行约 {place.walk_minutes} 分钟；"
                f"{place.description}"
            )
            index += 1
        if not lines:
            return ""
        return f"{GRAPH_HEADER}\n" + "\n".join(lines)


class GraphRetriever:
    def __init__(
        self, *, graph: KnowledgeGraph | None = None, settings: Settings | None = None
    ) -> None:
        self._graph = graph
        self._settings = settings or get_settings()

    async def retrieve(self, question: str) -> GraphRetrieval | None:
        """命中条件时返回图事实；未命中返回 None，图故障返回降级结果。"""
        query = extract_graph_query(question)
        if query is None:
            return None
        graph = self._graph
        owned = graph is None
        try:
            if graph is None:
                graph = KnowledgeGraph.create(settings=self._settings)
            timeout = self._settings.kb_graph_timeout_seconds
            insight = await asyncio.wait_for(
                graph.multi_hop_insight(
                    DEFAULT_HOTEL_CODE,
                    amenities=query.amenities,
                    place_category=query.place_category,
                    max_distance_m=query.max_distance_m,
                    max_walk_minutes=query.max_walk_minutes,
                    room_limit=ROOM_LIMIT if query.wants_rooms else 0,
                    place_limit=PLACE_LIMIT if query.wants_places else 0,
                ),
                timeout=timeout if timeout > 0 else None,
            )
        except (GraphError, TimeoutError) as exc:
            logger.warning("graph leg degraded: query=%s error=%s", question, exc)
            return GraphRetrieval(
                query=query,
                degraded=True,
                notes=("关系图暂不可用，已跳过图谱补充。",),
            )
        finally:
            if owned and graph is not None:
                try:
                    await graph.close()
                except Exception:  # pragma: no cover - 关闭失败不影响检索结果
                    logger.warning("Neo4j driver close failed", exc_info=True)
        return GraphRetrieval(query=query, insight=insight)


def extract_graph_query(question: str) -> GraphQuery | None:
    """确定性抽取：返回 None 表示该问题不需要图检索腿。"""
    text = question.strip()
    if not text:
        return None
    lowered = text.lower()

    distance = _extract_distance(text)
    walk_minutes = _extract_walk_minutes(text)
    nearby = bool(distance or walk_minutes) or any(marker in lowered for marker in NEARBY_MARKERS)
    category = next(
        (
            name
            for name, keywords in PLACE_CATEGORY_KEYWORDS
            if any(keyword in lowered for keyword in keywords)
        ),
        None,
    )
    amenities = tuple(
        code
        for code, keywords in AMENITY_KEYWORDS
        if any(keyword in lowered for keyword in keywords)
    )
    room_marker = any(marker in text for marker in ROOM_MARKERS)
    wants_rooms = bool(amenities) and (room_marker or not nearby)
    wants_places = nearby and (
        category is not None or distance is not None or walk_minutes is not None
    )

    if not wants_rooms and not wants_places:
        return None
    return GraphQuery(
        amenities=amenities,
        place_category=category,
        max_distance_m=distance,
        max_walk_minutes=walk_minutes,
        wants_rooms=wants_rooms,
        wants_places=wants_places,
    )


def _extract_distance(text: str) -> int | None:
    match = DISTANCE_RE.search(text)
    if match is None:
        return None
    raw, unit = match.group(1), match.group(2).lower()
    meters = float(raw) * 1000 if unit in {"公里", "千米", "km"} else float(raw)
    if meters < MIN_DISTANCE_M:
        return None
    return int(round(meters))


def _extract_walk_minutes(text: str) -> int | None:
    match = WALK_MINUTES_RE.search(text)
    if match is None:
        return None
    value = match.group(1) or match.group(2)
    minutes = int(value)
    return minutes if minutes > 0 else None


def _yuan(fen: int) -> str:
    return f"{fen / 100:.2f}".rstrip("0").rstrip(".")


__all__ = [
    "GraphQuery",
    "GraphRetrieval",
    "GraphRetriever",
    "extract_graph_query",
]
