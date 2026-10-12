"""Neo4j 关系图：房型-设施-周边关系，供房型设施问答与周边推荐使用。

图模型（节点键都是稳定业务码，导入用 MERGE 保证幂等）：

    (:Hotel {code, name})-[:HAS_ROOM_TYPE]->(:RoomType {code, name, capacity, bed_type,
                                                       base_price_fen, active, sample})
    (:RoomType)-[:HAS_AMENITY]->(:Amenity {code, name})
    (:Hotel)-[:NEARBY {distance_m, walk_minutes}]->(:Place {code, name, category,
                                                            description, sample})

不变量：
- 图只存关系与检索用属性，价格等事实以 PostgreSQL 为准（导入时同步一次，不反向写回）；
- 读接口不写入；图不可用时抛 GraphError，由调用方决定降级话术；
- 查询结果只含被要求的上限条数，排序口径固定（步行时间 → 距离 → 名称）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

AMENITY_LABELS = {
    "wifi": "免费 WiFi",
    "智能门锁": "智能门锁",
    "投影": "投影仪",
    "儿童用品": "儿童用品",
}


class GraphError(Exception):
    """关系图不可达或写入失败。"""


@dataclass(frozen=True)
class RoomTypeFact:
    code: str
    name: str
    capacity: int
    bed_type: str
    base_price_fen: int
    active: bool
    amenities: tuple[str, ...]


@dataclass(frozen=True)
class PlaceFact:
    code: str
    name: str
    category: str
    description: str
    distance_m: int
    walk_minutes: int


@dataclass(frozen=True)
class RoomTypeOverview:
    code: str
    name: str
    capacity: int
    bed_type: str
    base_price_fen: int
    active: bool
    amenities: tuple[str, ...]


@dataclass(frozen=True)
class NearbyPlace:
    code: str
    name: str
    category: str
    description: str
    distance_m: int
    walk_minutes: int


class KnowledgeGraph:
    def __init__(self, driver: AsyncDriver, *, database: str) -> None:
        self._driver = driver
        self._database = database

    @classmethod
    def create(cls, *, settings: Settings | None = None) -> KnowledgeGraph:
        resolved = settings or get_settings()
        try:
            driver = AsyncGraphDatabase.driver(
                resolved.neo4j_uri,
                auth=(resolved.neo4j_user, resolved.neo4j_password),
                connection_timeout=resolved.neo4j_timeout_seconds,
            )
        except Exception as exc:  # pragma: no cover - 连接失败路径由调用方收敛
            raise GraphError(f"Neo4j 驱动初始化失败：{exc}") from exc
        return cls(driver, database=resolved.neo4j_database)

    async def close(self) -> None:
        await self._driver.close()

    async def verify(self) -> bool:
        try:
            await self._driver.verify_connectivity()
        except Exception as exc:
            raise GraphError(f"Neo4j 不可达：{exc}") from exc
        return True

    async def sync_room_types(
        self, hotel_code: str, hotel_name: str, room_types: Sequence[RoomTypeFact]
    ) -> int:
        query = (
            "MERGE (h:Hotel {code: $hotel_code}) SET h.name = $hotel_name "
            "WITH h "
            "UNWIND $room_types AS item "
            "MERGE (rt:RoomType {code: item.code}) "
            "SET rt.name = item.name, rt.capacity = item.capacity, rt.bed_type = item.bed_type, "
            "rt.base_price_fen = item.base_price_fen, rt.active = item.active, rt.sample = true "
            "MERGE (h)-[:HAS_ROOM_TYPE]->(rt) "
            "WITH rt, item "
            "UNWIND item.amenities AS amenity_code "
            "MERGE (a:Amenity {code: amenity_code}) "
            "SET a.name = coalesce($labels[amenity_code], amenity_code), a.sample = true "
            "MERGE (rt)-[:HAS_AMENITY]->(a) "
            "RETURN count(*) AS written"
        )
        params = {
            "hotel_code": hotel_code,
            "hotel_name": hotel_name,
            "room_types": [
                {
                    "code": item.code,
                    "name": item.name,
                    "capacity": item.capacity,
                    "bed_type": item.bed_type,
                    "base_price_fen": item.base_price_fen,
                    "active": item.active,
                    "amenities": list(item.amenities),
                }
                for item in room_types
            ],
            "labels": AMENITY_LABELS,
        }
        records = await self._run(query, params)
        return len(room_types) if records else 0

    async def sync_places(self, hotel_code: str, places: Sequence[PlaceFact]) -> int:
        query = (
            "MERGE (h:Hotel {code: $hotel_code}) "
            "WITH h "
            "UNWIND $places AS item "
            "MERGE (p:Place {code: item.code}) "
            "SET p.name = item.name, p.category = item.category, "
            "p.description = item.description, p.sample = true "
            "MERGE (h)-[r:NEARBY]->(p) "
            "SET r.distance_m = item.distance_m, r.walk_minutes = item.walk_minutes "
            "RETURN count(*) AS written"
        )
        records = await self._run(
            query,
            {
                "hotel_code": hotel_code,
                "places": [
                    {
                        "code": item.code,
                        "name": item.name,
                        "category": item.category,
                        "description": item.description,
                        "distance_m": item.distance_m,
                        "walk_minutes": item.walk_minutes,
                    }
                    for item in places
                ],
            },
        )
        return len(places) if records else 0

    async def room_type_overview(self, code: str) -> RoomTypeOverview | None:
        query = (
            "MATCH (rt:RoomType {code: $code}) "
            "OPTIONAL MATCH (rt)-[:HAS_AMENITY]->(a:Amenity) "
            "RETURN rt.code AS code, rt.name AS name, rt.capacity AS capacity, "
            "rt.bed_type AS bed_type, rt.base_price_fen AS base_price_fen, "
            "rt.active AS active, collect(a.name) AS amenities"
        )
        records = await self._run(query, {"code": code})
        if not records:
            return None
        record = records[0]
        return RoomTypeOverview(
            code=str(record["code"]),
            name=str(record["name"]),
            capacity=int(record["capacity"]),
            bed_type=str(record["bed_type"]),
            base_price_fen=int(record["base_price_fen"]),
            active=bool(record["active"]),
            amenities=tuple(sorted(str(item) for item in record["amenities"] if item)),
        )

    async def recommend_nearby(
        self,
        hotel_code: str,
        *,
        category: str | None = None,
        max_walk_minutes: int | None = None,
        limit: int = 5,
    ) -> list[NearbyPlace]:
        """周边推荐：默认按步行时间从近到远，可选类别与步行上限过滤。"""
        if limit <= 0:
            raise GraphError("limit 必须为正数")
        query = (
            "MATCH (h:Hotel {code: $hotel_code})-[r:NEARBY]->(p:Place) "
            "WHERE ($category IS NULL OR p.category = $category) "
            "AND ($max_walk_minutes IS NULL OR r.walk_minutes <= $max_walk_minutes) "
            "RETURN p.code AS code, p.name AS name, p.category AS category, "
            "p.description AS description, r.distance_m AS distance_m, "
            "r.walk_minutes AS walk_minutes "
            "ORDER BY r.walk_minutes, r.distance_m, p.name "
            "LIMIT $limit"
        )
        records = await self._run(
            query,
            {
                "hotel_code": hotel_code,
                "category": category,
                "max_walk_minutes": max_walk_minutes,
                "limit": limit,
            },
        )
        return [
            NearbyPlace(
                code=str(record["code"]),
                name=str(record["name"]),
                category=str(record["category"]),
                description=str(record["description"]),
                distance_m=int(record["distance_m"]),
                walk_minutes=int(record["walk_minutes"]),
            )
            for record in records
        ]

    async def _run(self, query: str, params: dict[str, Any]) -> list[Any]:
        try:
            async with self._driver.session(database=self._database) as session:
                result = await session.run(query, params)
                return [record async for record in result]
        except Exception as exc:
            raise GraphError(f"图查询失败：{exc}") from exc
