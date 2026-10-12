"""关系图导入：把 PostgreSQL 的房型/设施与样例周边地点写进 Neo4j（幂等 MERGE）。

运行：`python -m app.knowledge.graph_seed`（可重复执行：节点与关系按业务码 MERGE 更新）。

周边地点为**样例数据**（虚构占位，距离与步行时间为估算值），正式上线前必须替换为
门店实测数据；房型与设施来自 PostgreSQL，图只在导入时同步一次。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionFactory
from app.knowledge.graph import KnowledgeGraph, PlaceFact, RoomTypeFact
from app.repositories import RoomTypeRepository

logger = logging.getLogger(__name__)

HOTEL_CODE = "haochen-city-new-area"
HOTEL_NAME = "皓晨酒店（城市新区店）"


@dataclass(frozen=True)
class GraphSeedReport:
    room_types: int = 0
    places: int = 0
    amenities: int = 0


# 样例周边地点：类别口径固定为 便利店 / 交通 / 餐饮 / 医疗 / 休闲 / 购物
SAMPLE_PLACES: tuple[PlaceFact, ...] = (
    PlaceFact(
        code="store-24h",
        name="24 小时便利店",
        category="便利店",
        description="楼下左转 180 米，饮料、零食、日用品与雨具齐全，可扫码支付。",
        distance_m=180,
        walk_minutes=3,
    ),
    PlaceFact(
        code="bus-stop",
        name="晨光路公交站",
        category="交通",
        description="门口右转直行，途经 12 路、37 路，可直达老城区与火车站。",
        distance_m=220,
        walk_minutes=3,
    ),
    PlaceFact(
        code="coffee",
        name="晨光咖啡（新区店）",
        category="餐饮",
        description="步行 5 分钟，早 8:00 开始营业，适合临时办公。",
        distance_m=350,
        walk_minutes=5,
    ),
    PlaceFact(
        code="metro-line3",
        name="地铁 3 号线晨光路站 B 口",
        category="交通",
        description="步行 6 分钟，两站可达高铁站，四站可达市中心。",
        distance_m=450,
        walk_minutes=6,
    ),
    PlaceFact(
        code="snack-street",
        name="晨光小吃街",
        category="餐饮",
        description="傍晚开始营业，本地小吃与夜宵集中，人均 30-60 元。",
        distance_m=600,
        walk_minutes=8,
    ),
    PlaceFact(
        code="pharmacy",
        name="24 小时药店",
        category="医疗",
        description="门口左转直行，夜间有值班药师，可代煎中药。",
        distance_m=700,
        walk_minutes=9,
    ),
    PlaceFact(
        code="park",
        name="晨光公园",
        category="休闲",
        description="开放式公园，有 1.2 公里环形步道，适合晨跑与散步。",
        distance_m=900,
        walk_minutes=12,
    ),
    PlaceFact(
        code="mall",
        name="新区购物中心",
        category="购物",
        description="含超市、影院与儿童乐园，负一层有大型连锁超市。",
        distance_m=1200,
        walk_minutes=16,
    ),
    PlaceFact(
        code="hospital-er",
        name="市第二医院急诊",
        category="医疗",
        description="打车约 6 分钟，24 小时急诊；前台可协助叫车。",
        distance_m=1500,
        walk_minutes=20,
    ),
)


async def _room_type_facts(session: AsyncSession) -> list[RoomTypeFact]:
    rows = await RoomTypeRepository(session).list_all()
    return [
        RoomTypeFact(
            code=row.code,
            name=row.name,
            capacity=row.capacity,
            bed_type=row.bed_type,
            base_price_fen=row.base_price_fen,
            active=row.active,
            amenities=tuple(row.amenities or ()),
        )
        for row in rows
    ]


async def seed_graph(
    session: AsyncSession, *, graph: KnowledgeGraph | None = None
) -> GraphSeedReport:
    room_types = await _room_type_facts(session)
    facts = graph or KnowledgeGraph.create()
    try:
        await facts.verify()
        written_types = await facts.sync_room_types(HOTEL_CODE, HOTEL_NAME, room_types)
        written_places = await facts.sync_places(HOTEL_CODE, SAMPLE_PLACES)
    finally:
        if graph is None:
            await facts.close()
    return GraphSeedReport(
        room_types=written_types,
        places=written_places,
        amenities=len({amenity for item in room_types for amenity in item.amenities}),
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    async with SessionFactory() as session:
        report = await seed_graph(session)
    logger.info(
        "关系图导入完成：房型=%d 设施=%d 样例周边地点=%d",
        report.room_types,
        report.amenities,
        report.places,
    )
    logger.info("周边地点为样例数据，上线前需替换为门店实测数据")


if __name__ == "__main__":
    asyncio.run(main())
