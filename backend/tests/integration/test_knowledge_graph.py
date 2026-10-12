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


async def _create_room_types(session: AsyncSession, *, suffix: str) -> dict[str, str]:
    repo = RoomTypeRepository(session)
    codes: dict[str, str] = {}
    for code, amenities in AMENITY_BY_TYPE.items():
        stored = f"{code}-{suffix}"
        await repo.create(
            code=stored,
            name=f"图谱测试{code}",
            base_price_fen=39800,
            capacity=2 if code == "KING" else 4,
            bed_type="king",
            amenities=list(amenities),
        )
        codes[code] = stored
    await session.commit()
    return codes


def test_graph_seed_is_idempotent_and_queries_are_ordered(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        graph = await _graph_or_skip()
        await _create_room_types(session, suffix=uuid4().hex[:6])
        try:
            first = await seed_graph(session, graph=graph)
            second = await seed_graph(session, graph=graph)
            places = await graph.recommend_nearby(HOTEL_CODE, limit=50)
            transit = await graph.recommend_nearby(HOTEL_CODE, category="交通", limit=50)
            walkable = await graph.recommend_nearby(HOTEL_CODE, max_walk_minutes=5, limit=50)
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
    assert values["first"][1] == 4
    assert values["place_count"] == len(SAMPLE_PLACES)
    assert values["walk_order"] == sorted(values["walk_order"])
    assert values["transit"] == ["交通", "交通"]
    assert values["walkable_max"] <= 5
    assert values["other_hotel"] == []


def test_room_type_overview_returns_seeded_amenities(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        graph = await _graph_or_skip()
        codes = await _create_room_types(session, suffix=uuid4().hex[:6])
        try:
            await seed_graph(session, graph=graph)
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
        finally:
            await graph.close()
        return True

    assert run_with_session(runtime_db_url, _body) is True
