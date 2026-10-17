"""可复现基准：PostgreSQL 直查 vs DuckDB 预聚合。

固定业务问题：各房型月度入住率与客单价（口径见
`app/data_pipeline/business_aggregates.py`）。

流程：
1. 生成确定性的合成订单/房型/房间数据（规模可控）；
2. 用与生产管道相同的 `build_room_type_monthly` 生成预聚合 Parquet；
3. PostgreSQL 侧把同一批数据写入会话级临时表，直查展开入住夜后聚合；
4. 两侧各预热一次，重复计时，校验结果一致并输出数据规模与耗时。

临时表仅存在于当前连接，不写入业务库。示例：
    python bench_datalake.py --orders 200000 --months 12 --repeats 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import random
import statistics
import sys
import tempfile
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any

import asyncpg
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.data_pipeline.business_aggregates import (
    STAY_STATUSES,
    build_room_type_monthly,
)

STATUS_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("completed", 82),
    ("checked_in", 8),
    ("checkout_pending", 4),
    ("paid", 4),
    ("cancelled", 2),
)

PG_SQL = f"""
WITH stay_orders AS (
    SELECT id, room_type_id, amount_fen, nights, check_in_date, check_out_date
    FROM bench_orders
    WHERE status IN ({", ".join(f"'{status}'" for status in STAY_STATUSES)})
      AND nights > 0
      AND check_out_date > check_in_date
),
stay_nights AS (
    SELECT
        o.id AS order_id,
        o.room_type_id,
        o.amount_fen::numeric / o.nights AS revenue_fen,
        d::date AS stay_date
    FROM stay_orders o
    CROSS JOIN LATERAL generate_series(
        o.check_in_date::timestamp,
        o.check_out_date::timestamp - interval '1 day',
        interval '1 day'
    ) AS d
),
bounds AS (
    SELECT
        date_trunc('month', MIN(check_in_date)) AS min_month,
        date_trunc('month', MAX(check_out_date)) AS max_month
    FROM stay_orders
),
months AS (
    SELECT generate_series(
        min_month::timestamp, max_month::timestamp, interval '1 month'
    )::date AS month
    FROM bounds
),
grid AS (
    SELECT
        m.month,
        rt.id AS room_type_id,
        rt.code AS room_type_code,
        rt.name AS room_type_name,
        COUNT(r.id)::int AS room_count,
        EXTRACT(DAY FROM (m.month + interval '1 month - 1 day'))::int AS days_in_month
    FROM months m
    CROSS JOIN bench_room_types rt
    LEFT JOIN bench_rooms r ON r.room_type_id = rt.id
    WHERE rt.active
    GROUP BY m.month, rt.id, rt.code, rt.name
),
sold AS (
    SELECT
        date_trunc('month', stay_date)::date AS month,
        room_type_id,
        COUNT(*) AS sold_room_nights,
        COUNT(DISTINCT order_id) AS orders,
        SUM(revenue_fen) AS revenue_fen
    FROM stay_nights
    GROUP BY 1, 2
)
SELECT
    g.month,
    g.room_type_code,
    g.room_type_name,
    g.room_count,
    g.days_in_month,
    g.room_count * g.days_in_month AS available_room_nights,
    COALESCE(s.sold_room_nights, 0) AS sold_room_nights,
    COALESCE(s.orders, 0) AS orders,
    ROUND(COALESCE(s.revenue_fen, 0), 2)::float8 AS revenue_fen,
    CASE
        WHEN g.room_count * g.days_in_month > 0
        THEN ROUND(
            COALESCE(s.sold_room_nights, 0)::numeric / (g.room_count * g.days_in_month), 6
        )::float8
        ELSE NULL
    END AS occupancy_rate,
    CASE
        WHEN COALESCE(s.sold_room_nights, 0) > 0
        THEN ROUND(s.revenue_fen / s.sold_room_nights, 2)::float8
        ELSE NULL
    END AS adr_fen
FROM grid g
LEFT JOIN sold s ON s.month = g.month AND s.room_type_id = g.room_type_id
ORDER BY g.month, g.room_type_code
"""


def _shift_month(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def _month_start_today() -> date:
    today = date.today()
    return today.replace(day=1)


def generate_dataset(
    *,
    orders: int,
    months: int,
    room_type_count: int,
    rooms_per_type: int,
    seed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """生成确定性的订单、房型、房间数据（日期覆盖最近 N 个自然月）。"""
    rng = random.Random(seed)
    first_month = _shift_month(_month_start_today(), -(months - 1))
    last_month = _month_start_today()
    horizon_days = (last_month - first_month).days

    room_types: list[dict[str, Any]] = []
    base_prices = [39800, 45800, 59800, 69800, 128800, 52800, 88800, 36800]
    for index in range(room_type_count):
        room_type_id = uuid.UUID(int=(seed + 1) << 96 | index + 1)
        base_price = base_prices[index % len(base_prices)] + index * 1000
        room_types.append(
            {
                "id": room_type_id,
                "code": f"RT-{index + 1:02d}",
                "name": f"基准房型{index + 1}",
                "base_price_fen": base_price,
                "active": True,
                "updated_at": datetime(2024, 1, 1, tzinfo=UTC),
            }
        )

    rooms: list[dict[str, Any]] = []
    for room_type in room_types:
        for _ in range(rooms_per_type):
            rooms.append(
                {
                    "id": uuid.UUID(int=(seed + 2) << 96 | (len(rooms) + 1)),
                    "room_type_id": room_type["id"],
                    "room_no": f"{len(rooms) + 101}",
                    "status": "available",
                    "updated_at": datetime(2024, 1, 1, tzinfo=UTC),
                }
            )

    order_rows: list[dict[str, Any]] = []
    statuses = [status for status, _ in STATUS_WEIGHTS]
    weights = [weight for _, weight in STATUS_WEIGHTS]
    for index in range(orders):
        room_type = room_types[index % len(room_types)]
        check_in = first_month + timedelta(days=rng.randrange(max(horizon_days, 1)))
        nights = rng.choice((1, 1, 2, 2, 3, 4))
        check_out = check_in + timedelta(days=nights)
        status = rng.choices(statuses, weights=weights, k=1)[0]
        amount = int(room_type["base_price_fen"] * nights * rng.uniform(0.85, 1.15))
        created_at = datetime.combine(
            check_in - timedelta(days=rng.randrange(1, 15)),
            dt_time(hour=rng.randrange(24), minute=rng.randrange(60)),
            tzinfo=UTC,
        )
        updated_at = created_at + timedelta(minutes=rng.randrange(0, 1440))
        order_rows.append(
            {
                "id": uuid.UUID(int=(seed + 3) << 96 | (index + 1)),
                "room_type_id": room_type["id"],
                "status": status,
                "check_in_date": check_in,
                "check_out_date": check_out,
                "nights": nights,
                "amount_fen": amount,
                "paid_amount_fen": amount if status not in {"created", "cancelled"} else None,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )

    return order_rows, room_types, rooms


def _parquet_rows(
    orders: list[dict[str, Any]],
    room_types: list[dict[str, Any]],
    rooms: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """转为与导出管道一致的字符串/ISO 形态。"""

    def clean(row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for key, value in item.items():
            if isinstance(value, uuid.UUID):
                item[key] = str(value)
            elif isinstance(value, datetime):
                item[key] = value.isoformat()
        return item

    return (
        [clean(row) for row in orders],
        [clean(row) for row in room_types],
        [clean(row) for row in rooms],
    )


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")


def _percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return ordered[index]


def _pg_dsn() -> str:
    url = make_url(get_settings().database_url)
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


async def run_pg(
    *,
    orders: list[dict[str, Any]],
    room_types: list[dict[str, Any]],
    rooms: list[dict[str, Any]],
    repeats: int,
) -> tuple[list[dict[str, Any]], list[float]]:
    conn = await asyncpg.connect(_pg_dsn())
    try:
        await conn.execute(
            """
            CREATE TEMP TABLE bench_orders (
                id uuid PRIMARY KEY,
                room_type_id uuid NOT NULL,
                status text NOT NULL,
                check_in_date date NOT NULL,
                check_out_date date NOT NULL,
                nights int NOT NULL,
                amount_fen int NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TEMP TABLE bench_room_types (
                id uuid PRIMARY KEY,
                code text NOT NULL,
                name text NOT NULL,
                base_price_fen int NOT NULL,
                active boolean NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TEMP TABLE bench_rooms (
                id uuid PRIMARY KEY,
                room_type_id uuid NOT NULL
            )
            """
        )

        order_columns = [
            "id",
            "room_type_id",
            "status",
            "check_in_date",
            "check_out_date",
            "nights",
            "amount_fen",
        ]
        await conn.copy_records_to_table(
            "bench_orders",
            records=[tuple(row[column] for column in order_columns) for row in orders],
            columns=order_columns,
        )
        await conn.copy_records_to_table(
            "bench_room_types",
            records=[
                (row["id"], row["code"], row["name"], row["base_price_fen"], row["active"])
                for row in room_types
            ],
            columns=["id", "code", "name", "base_price_fen", "active"],
        )
        await conn.copy_records_to_table(
            "bench_rooms",
            records=[(row["id"], row["room_type_id"]) for row in rooms],
            columns=["id", "room_type_id"],
        )

        await conn.fetch(PG_SQL)
        timings: list[float] = []
        rows: list[dict[str, Any]] = []
        for _ in range(repeats):
            started = time.perf_counter()
            records = await conn.fetch(PG_SQL)
            timings.append((time.perf_counter() - started) * 1000)
            rows = [dict(record) for record in records]
        return rows, timings
    finally:
        await conn.close()


def run_duckdb(preagg_path: Path, *, repeats: int) -> tuple[list[dict[str, Any]], list[float]]:
    con = duckdb.connect()
    try:
        sql = (
            f"SELECT * FROM read_parquet('{preagg_path.as_posix()}') ORDER BY month, room_type_code"
        )
        con.execute(sql).fetchall()
        timings: list[float] = []
        rows: list[dict[str, Any]] = []
        for _ in range(repeats):
            started = time.perf_counter()
            result = con.execute(sql)
            columns = [desc[0] for desc in result.description]
            rows = [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
            timings.append((time.perf_counter() - started) * 1000)
        return rows, timings
    finally:
        con.close()


def _result_map(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    mapped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        month = row["month"]
        month_key = month.isoformat() if hasattr(month, "isoformat") else str(month)
        mapped[(month_key[:10], str(row["room_type_code"]))] = row
    return mapped


def compare_results(pg_rows: list[dict[str, Any]], duck_rows: list[dict[str, Any]]) -> list[str]:
    """返回不一致项；空列表表示完全一致。"""
    pg_map = _result_map(pg_rows)
    duck_map = _result_map(duck_rows)
    problems: list[str] = []
    if pg_map.keys() != duck_map.keys():
        only_pg = sorted(pg_map.keys() - duck_map.keys())
        only_duck = sorted(duck_map.keys() - pg_map.keys())
        problems.append(f"行集合不同: only_pg={only_pg[:5]} only_duck={only_duck[:5]}")
        return problems

    for key, pg_row in pg_map.items():
        duck_row = duck_map[key]
        for column, tolerance in (
            ("room_count", 0),
            ("days_in_month", 0),
            ("available_room_nights", 0),
            ("sold_room_nights", 0),
            ("orders", 0),
            ("revenue_fen", 0.01),
            ("adr_fen", 0.01),
            ("occupancy_rate", 1e-6),
        ):
            left, right = pg_row[column], duck_row[column]
            if left is None or right is None:
                if left != right:
                    problems.append(f"{key} {column}: pg={left} duck={right}")
                continue
            if abs(float(left) - float(right)) > tolerance:
                problems.append(f"{key} {column}: pg={left} duck={right}")
    return problems


def _summary(timings: list[float]) -> dict[str, float]:
    return {
        "min_ms": round(min(timings), 3),
        "median_ms": round(statistics.median(timings), 3),
        "max_ms": round(max(timings), 3),
        "p95_ms": round(_percentile(timings, 0.95), 3),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="PostgreSQL 直查 vs DuckDB 预聚合基准")
    parser.add_argument("--orders", type=int, default=200_000)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--room-types", type=int, default=5)
    parser.add_argument("--rooms-per-type", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    if args.orders <= 0 or args.months <= 0 or args.room_types <= 0:
        parser.error("orders/months/room-types 必须为正数")
    if args.repeats <= 0:
        parser.error("repeats 必须为正数")

    orders, room_types, rooms = generate_dataset(
        orders=args.orders,
        months=args.months,
        room_type_count=args.room_types,
        rooms_per_type=args.rooms_per_type,
        seed=args.seed,
    )
    parquet_orders, parquet_room_types, parquet_rooms = _parquet_rows(orders, room_types, rooms)

    temp_dir = Path(tempfile.mkdtemp(prefix="haochen-datalake-bench-"))
    orders_path = temp_dir / "orders.parquet"
    room_types_path = temp_dir / "room_types.parquet"
    rooms_path = temp_dir / "rooms.parquet"
    preagg_path = temp_dir / "agg_room_type_monthly.parquet"
    _write_parquet(orders_path, parquet_orders)
    _write_parquet(room_types_path, parquet_room_types)
    _write_parquet(rooms_path, parquet_rooms)

    con = duckdb.connect()
    try:
        preagg = build_room_type_monthly(
            con,
            orders_paths=[str(orders_path)],
            room_types_paths=[str(room_types_path)],
            rooms_paths=[str(rooms_path)],
        )
    finally:
        con.close()
    _write_parquet(preagg_path, preagg.to_pylist())

    sold_nights = sum(
        order["nights"]
        for order in orders
        if order["status"] in STAY_STATUSES
        and order["nights"] > 0
        and order["check_out_date"] > order["check_in_date"]
    )

    print("=== 数据规模 ===")
    print(
        f"orders={len(orders):,}  months={args.months}  room_types={len(room_types)}  "
        f"rooms={len(rooms):,}  sold_nights={sold_nights:,}  preagg_rows={preagg.num_rows}"
    )
    print(f"临时目录: {temp_dir}")

    try:
        pg_rows, pg_timings = await run_pg(
            orders=orders, room_types=room_types, rooms=rooms, repeats=args.repeats
        )
        duck_rows, duck_timings = run_duckdb(preagg_path, repeats=args.repeats)
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"PostgreSQL 不可达或查询失败: {exc}", file=sys.stderr)
        return 2

    problems = compare_results(pg_rows, duck_rows)
    pg_stats = _summary(pg_timings)
    duck_stats = _summary(duck_timings)
    speedup = pg_stats["median_ms"] / duck_stats["median_ms"] if duck_stats["median_ms"] else 0

    print("\n=== 耗时（仅查询，热身后取中位数）===")
    print(
        f"PostgreSQL 直查: {pg_stats['median_ms']:.2f} ms "
        f"(min {pg_stats['min_ms']:.2f} / p95 {pg_stats['p95_ms']:.2f} / "
        f"max {pg_stats['max_ms']:.2f})"
    )
    print(
        f"DuckDB 预聚合:  {duck_stats['median_ms']:.2f} ms "
        f"(min {duck_stats['min_ms']:.2f} / p95 {duck_stats['p95_ms']:.2f} / "
        f"max {duck_stats['max_ms']:.2f})"
    )
    print(f"中位数加速比:   {speedup:.2f}x")
    print(f"结果一致性:     {'一致' if not problems else '不一致'}")
    for problem in problems[:10]:
        print(f"  - {problem}")

    report = {
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "duckdb": duckdb.__version__,
            "postgresql": await _pg_version(),
        },
        "dataset": {
            "orders": len(orders),
            "months": args.months,
            "room_types": len(room_types),
            "rooms": len(rooms),
            "sold_nights": sold_nights,
            "preaggregate_rows": preagg.num_rows,
            "seed": args.seed,
        },
        "postgresql_direct": pg_stats,
        "duckdb_preaggregate": duck_stats,
        "speedup_median": round(speedup, 2),
        "consistent": not problems,
        "mismatches": problems,
    }
    if args.json_out:
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入: {args.json_out}")

    if not args.keep_temp:
        for path in temp_dir.glob("*"):
            path.unlink()
        temp_dir.rmdir()
    return 0 if not problems else 1


async def _pg_version() -> str:
    conn = await asyncpg.connect(_pg_dsn())
    try:
        return str(await conn.fetchval("SHOW server_version"))
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
