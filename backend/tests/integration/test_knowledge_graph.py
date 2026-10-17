"""关系图集成测试（真实 PostgreSQL + Neo4j）：房型-设施-周边关系的导入与查询。

Neo4j 不可达时整组跳过（与 PostgreSQL 集成测试同一策略）；导入按业务码 MERGE，
因此在共享开发图上重复执行不会产生重复节点。集成测试共用一个临时数据库，
房型数量断言只针对本测试创建的编码。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.graph import GraphError, KnowledgeGraph
from app.knowledge.graph_seed import HOTEL_CODE, SAMPLE_PLACES, seed_graph
from app.repositories import RoomTypeRepository
from tests.integration.db_utils import run_with_session

pytestmark = pytest.mark.integration

AMENITY_BY_TYPE = {
    "KING": ("wifi", "智能门锁", "投影"),
    "FAMILY": ("wifi", "智能门锁", "投影", "儿童用品"),
}


async def _graph_or_skip() -> KnowledgeGraph:
    graph = KnowledgeGraph.create()
    try:
        await graph.verify()
    except GraphError as exc:
        await graph.close()
        pytest.skip(f"Neo4j 不可达，跳过关系图集成测试：{exc}")
    return graph


async def _create_room_types(session: AsyncSession, *, suffix: str) -> tuple[dict[str, str], str]:
    """创建本批次房型，并附加批次唯一设施用于在共享图上隔离断言。"""
    repo = RoomTypeRepository(session)
    codes: dict[str, str] = {}
    tag = f"测试批次-{suffix}"
    for code, amenities in AMENITY_BY_TYPE.items():
        stored = f"{code}-{suffix}"
        await repo.create(
            code=stored,
            name=f"图谱测试{code}",
            base_price_fen=39800,
            capacity=2 if code == "KING" else 4,
            bed_type="king",
            amenities=[*amenities, tag],
        )
        codes[code] = stored
    await session.commit()
    return codes, tag


def _test_hotel_code() -> str:
    """集成测试使用独立酒店节点，避免把测试房型挂到真实门店上。"""
    return f"test-hotel-{uuid4().hex[:8]}"


def test_graph_seed_is_idempotent_and_queries_are_ordered(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        graph = await _graph_or_skip()
        hotel_code = _test_hotel_code()
        await _create_room_types(session, suffix=uuid4().hex[:6])
        try:
            first = await seed_graph(
                session, graph=graph, hotel_code=hotel_code, hotel_name="图谱测试酒店"
            )
            second = await seed_graph(
                session, graph=graph, hotel_code=hotel_code, hotel_name="图谱测试酒店"
            )
            places = await graph.recommend_nearby(hotel_code, limit=50)
            transit = await graph.recommend_nearby(hotel_code, category="交通", limit=50)
            walkable = await graph.recommend_nearby(hotel_code, max_walk_minutes=5, limit=50)
            other_hotel = await graph.recommend_nearby("no-such-hotel", limit=5)
            return {
                "first": (first.places, first.amenities),
                "second": (second.places, second.amenities),
                "place_count": len(places),
                "walk_order": [place.walk_minutes for place in places],
                "transit": [place.category for place in transit],
                "walkable_max": max(place.walk_minutes for place in walkable),
                "other_hotel": other_hotel,
            }
        finally:
            await graph.close()

    values = run_with_session(runtime_db_url, _body)
    assert values["first"] == values["second"]
    assert values["first"][0] == len(SAMPLE_PLACES)
    # 共享测试库会累积批次设施，只要求不少于基础四类
    assert values["first"][1] >= 4
    assert values["place_count"] == len(SAMPLE_PLACES)
    assert values["walk_order"] == sorted(values["walk_order"])
    assert values["transit"] == ["交通", "交通"]
    assert values["walkable_max"] <= 5
    assert values["other_hotel"] == []


def test_room_type_overview_returns_seeded_amenities(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        graph = await _graph_or_skip()
        codes, _ = await _create_room_types(session, suffix=uuid4().hex[:6])
        try:
            await seed_graph(
                session, graph=graph, hotel_code=_test_hotel_code(), hotel_name="图谱测试酒店"
            )
            family = await graph.room_type_overview(codes["FAMILY"])
            king = await graph.room_type_overview(codes["KING"])
            return {
                "family": family,
                "king": king,
                "missing": await graph.room_type_overview("不存在的房型"),
            }
        finally:
            await graph.close()

    values = run_with_session(runtime_db_url, _body)
    family, king = values["family"], values["king"]
    assert family is not None and king is not None
    assert family.capacity == 4
    assert king.capacity == 2
    assert "免费 WiFi" in family.amenities
    assert "儿童用品" in family.amenities
    assert "儿童用品" not in king.amenities
    assert values["missing"] is None


def test_recommend_nearby_rejects_non_positive_limit(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> bool:
        graph = await _graph_or_skip()
        try:
            with pytest.raises(GraphError, match="limit"):
                await graph.recommend_nearby(HOTEL_CODE, limit=0)
            with pytest.raises(GraphError, match="max_distance_m"):
                await graph.recommend_nearby(HOTEL_CODE, max_distance_m=0)
        finally:
            await graph.close()
        return True

    assert run_with_session(runtime_db_url, _body) is True


def test_multi_hop_insight_combines_amenity_and_nearby_filters(runtime_db_url: str) -> None:
    """两跳房型设施（AND）与一跳周边按类别/距离过滤，组合结果一次返回。"""

    async def _body(session: AsyncSession) -> dict[str, Any]:
        graph = await _graph_or_skip()
        hotel_code = _test_hotel_code()
        codes, tag = await _create_room_types(session, suffix=uuid4().hex[:6])
        try:
            await seed_graph(session, graph=graph, hotel_code=hotel_code, hotel_name="图谱测试酒店")
            projected = await graph.multi_hop_insight(
                hotel_code, amenities=["投影", tag], room_limit=50, place_limit=0
            )
            family = await graph.multi_hop_insight(
                hotel_code,
                amenities=["投影", "儿童用品", tag],
                room_limit=50,
                place_limit=0,
            )
            impossible = await graph.multi_hop_insight(
                hotel_code,
                amenities=["投影", "不存在的设施", tag],
                room_limit=50,
                place_limit=0,
            )
            nearby = await graph.multi_hop_insight(
                hotel_code,
                place_category="餐饮",
                max_distance_m=500,
                room_limit=0,
                place_limit=50,
            )
            combined = await graph.multi_hop_insight(
                hotel_code,
                amenities=["投影", tag],
                place_category="餐饮",
                max_distance_m=500,
                room_limit=50,
                place_limit=50,
            )
            skipped = await graph.multi_hop_insight(hotel_code, room_limit=0, place_limit=0)
            return {
                "codes": codes,
                "projected": [room.code for room in projected.rooms],
                "projected_amenities": [room.amenities for room in projected.rooms],
                "family": [room.code for room in family.rooms],
                "impossible": [room.code for room in impossible.rooms],
                "nearby": [
                    (place.code, place.category, place.distance_m) for place in nearby.places
                ],
                "combined_rooms": [room.code for room in combined.rooms],
                "combined_places": [place.code for place in combined.places],
                "skipped_empty": skipped.is_empty,
            }
        finally:
            await graph.close()

    values = run_with_session(runtime_db_url, _body)
    test_codes = set(values["codes"].values())
    assert test_codes <= set(values["projected"])
    assert all("投影仪" in amenities for amenities in values["projected_amenities"])
    assert values["codes"]["FAMILY"] in values["family"]
    assert values["codes"]["KING"] not in values["family"]
    assert values["impossible"] == []
    assert values["nearby"]
    assert all(category == "餐饮" and distance <= 500 for _, category, distance in values["nearby"])
    assert "coffee" in {code for code, _, _ in values["nearby"]}
    assert "snack-street" not in {code for code, _, _ in values["nearby"]}
    assert test_codes <= set(values["combined_rooms"])
    assert "coffee" in values["combined_places"]
    assert "snack-street" not in values["combined_places"]
    assert values["skipped_empty"] is True
