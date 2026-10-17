"""图检索腿单元验证：确定性抽取、组合查询入参、降级与带编号渲染（不依赖 Neo4j）。"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import Settings
from app.knowledge.graph import (
    GraphError,
    GraphInsight,
    MatchedRoomType,
    NearbyPlace,
)
from app.knowledge.graph_retrieval import (
    GraphQuery,
    GraphRetrieval,
    GraphRetriever,
    extract_graph_query,
)


def test_extract_combined_multi_hop_query() -> None:
    query = extract_graph_query("带投影的房型附近500米内有早餐的店")
    assert query is not None
    assert query.amenities == ("投影",)
    assert query.place_category == "餐饮"
    assert query.max_distance_m == 500
    assert query.wants_rooms is True
    assert query.wants_places is True


def test_extract_ignores_plain_knowledge_questions() -> None:
    for question in ("早餐时间是几点", "房价包含早餐吗", "你们几点退房", "会员积分怎么算"):
        assert extract_graph_query(question) is None


def test_extract_nearby_without_category_or_limit_does_not_trigger() -> None:
    assert extract_graph_query("附近有什么") is None


def test_extract_ignores_child_height_as_distance() -> None:
    assert extract_graph_query("1.2米以下儿童早餐多少钱") is None


def test_extract_walk_minutes_and_place_category() -> None:
    query = extract_graph_query("步行10分钟内有药店吗")
    assert query is not None
    assert query.place_category == "医疗"
    assert query.max_walk_minutes == 10
    assert query.wants_places is True
    assert query.wants_rooms is False


def test_extract_room_amenity_without_nearby_context() -> None:
    query = extract_graph_query("有带投影的房型吗")
    assert query is not None
    assert query.amenities == ("投影",)
    assert query.wants_rooms is True
    assert query.wants_places is False


def test_extract_nearby_place_does_not_turn_amenity_into_room_query() -> None:
    query = extract_graph_query("附近有带投影的餐厅吗")
    assert query is not None
    assert query.place_category == "餐饮"
    assert query.wants_rooms is False
    assert query.wants_places is True


def test_graph_retrieval_renders_numbered_facts() -> None:
    retrieval = GraphRetrieval(
        query=GraphQuery(amenities=("投影",), wants_rooms=True, wants_places=True),
        insight=GraphInsight(
            rooms=(
                MatchedRoomType(
                    code="KING-01",
                    name="高级大床房",
                    capacity=2,
                    bed_type="king",
                    base_price_fen=39800,
                    amenities=("免费 WiFi", "投影仪"),
                ),
            ),
            places=(
                NearbyPlace(
                    code="coffee",
                    name="晨光咖啡（新区店）",
                    category="餐饮",
                    description="早 8:00 开始营业。",
                    distance_m=350,
                    walk_minutes=5,
                ),
            ),
        ),
    )
    text = retrieval.render(start_index=3)
    assert text.startswith("关系图补充事实")
    assert "[3] 房型：高级大床房（2 人，king 床，398 元/晚），设施：免费 WiFi、投影仪" in text
    assert "[4] 周边：晨光咖啡（新区店）（餐饮），距离约 350 米，步行约 5 分钟" in text
    facts = retrieval.facts()
    assert [fact["kind"] for fact in facts] == ["room_type", "place"]
    assert all(fact["source"] == "graph" for fact in facts)


class _RecordingGraph:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def multi_hop_insight(self, hotel_code: str, **kwargs: Any) -> GraphInsight:
        self.calls.append({"hotel_code": hotel_code, **kwargs})
        return GraphInsight(
            rooms=(
                MatchedRoomType(
                    code="FAMILY-01",
                    name="亲子家庭房",
                    capacity=4,
                    bed_type="twin",
                    base_price_fen=49800,
                    amenities=("免费 WiFi", "投影仪", "儿童用品"),
                ),
            )
        )

    async def close(self) -> None:
        raise AssertionError("外部注入的图不应被关闭")


class _ExplodingGraph:
    async def multi_hop_insight(self, hotel_code: str, **kwargs: Any) -> GraphInsight:
        raise GraphError("替身注入故障")

    async def close(self) -> None:
        return None


def test_graph_retriever_passes_extracted_filters_to_multi_hop_query() -> None:
    graph = _RecordingGraph()
    retriever = GraphRetriever(graph=graph)  # type: ignore[arg-type]
    result = asyncio.run(retriever.retrieve("带投影的房型附近500米内有早餐的店"))
    assert result is not None and not result.is_empty
    assert result.query.place_category == "餐饮"
    call = graph.calls[0]
    assert call["amenities"] == ("投影",)
    assert call["place_category"] == "餐饮"
    assert call["max_distance_m"] == 500
    assert call["room_limit"] > 0
    assert call["place_limit"] > 0


def test_graph_retriever_skips_query_when_not_triggered() -> None:
    graph = _RecordingGraph()
    retriever = GraphRetriever(graph=graph)  # type: ignore[arg-type]
    assert asyncio.run(retriever.retrieve("早餐时间是几点")) is None
    assert graph.calls == []


def test_graph_retriever_degrades_instead_of_raising() -> None:
    retriever = GraphRetriever(
        graph=_ExplodingGraph(),  # type: ignore[arg-type]
        settings=Settings(kb_graph_timeout_seconds=1.0),
    )
    result = asyncio.run(retriever.retrieve("带投影的房型附近500米内有早餐的店"))
    assert result is not None
    assert result.degraded is True
    assert result.is_empty is True
    assert "关系图暂不可用" in result.notes[0]
