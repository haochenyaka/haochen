"""业务预聚合：订单入住夜展开 → 房型月度入住率与客单价。

口径：
- 有效订单：已进入在住/离店链路的状态（paid / checked_in / checkout_pending / completed），
  取消与未支付订单不计入；
- 入住夜：按 `[check_in_date, check_out_date)` 逐夜展开，跨月订单按夜归属；
- 可售房晚：房型当前房间数 × 当月天数（库存快照，不回溯历史房态）；
- 入住率 = 已售房晚 / 可售房晚；
- 客单价（ADR）= 房费收入 / 已售房晚，房费按夜均摊。

订单与维度表可能因增量导出产生多版本，聚合前按主键取 `updated_at` 最新一行。
"""

from __future__ import annotations

from collections.abc import Sequence

import duckdb
import pyarrow as pa

ROOM_TYPE_MONTHLY_TABLE = "agg_room_type_monthly"

STAY_STATUSES: tuple[str, ...] = ("paid", "checked_in", "checkout_pending", "completed")

_ROOM_TYPE_MONTHLY_SQL = f"""
WITH orders AS (
    SELECT * FROM (
        SELECT
            *,
            row_number() OVER (
                PARTITION BY id ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
            ) AS _rn
        FROM orders_raw
    ) WHERE _rn = 1
),
room_types AS (
    SELECT * FROM (
        SELECT
            *,
            row_number() OVER (PARTITION BY id ORDER BY updated_at DESC NULLS LAST) AS _rn
        FROM room_types_raw
    ) WHERE _rn = 1
),
rooms AS (
    SELECT * FROM (
        SELECT
            *,
            row_number() OVER (PARTITION BY id ORDER BY updated_at DESC NULLS LAST) AS _rn
        FROM rooms_raw
    ) WHERE _rn = 1
),
room_inventory AS (
    SELECT room_type_id, COUNT(*) AS room_count
    FROM rooms
    GROUP BY room_type_id
),
stay_orders AS (
    SELECT
        id AS order_id,
        room_type_id,
        amount_fen,
        nights,
        CAST(check_in_date AS DATE) AS check_in_date,
        CAST(check_out_date AS DATE) AS check_out_date
    FROM orders
    WHERE status IN ({", ".join(f"'{status}'" for status in STAY_STATUSES)})
      AND nights > 0
      AND CAST(check_out_date AS DATE) > CAST(check_in_date AS DATE)
),
stay_nights AS (
    SELECT
        order_id,
        room_type_id,
        CAST(amount_fen AS DOUBLE) / nights AS revenue_fen,
        CAST(UNNEST(
            generate_series(
                CAST(check_in_date AS TIMESTAMP),
                CAST(check_out_date AS TIMESTAMP) - INTERVAL 1 DAY,
                INTERVAL 1 DAY
            )
        ) AS DATE) AS stay_date
    FROM stay_orders
),
bounds AS (
    SELECT
        date_trunc('month', MIN(check_in_date)) AS min_month,
        date_trunc('month', MAX(check_out_date)) AS max_month
    FROM stay_orders
),
months AS (
    SELECT CAST(UNNEST(
        generate_series(
            CAST(min_month AS TIMESTAMP),
            CAST(max_month AS TIMESTAMP),
            INTERVAL 1 MONTH
        )
    ) AS DATE) AS month
    FROM bounds
),
grid AS (
    SELECT
        m.month,
        rt.id AS room_type_id,
        rt.code AS room_type_code,
        rt.name AS room_type_name,
        COALESCE(ri.room_count, 0) AS room_count,
        CAST(date_diff('day', m.month, m.month + INTERVAL 1 MONTH) AS INTEGER) AS days_in_month
    FROM months m
    CROSS JOIN room_types rt
    LEFT JOIN room_inventory ri ON ri.room_type_id = rt.id
    WHERE rt.active
),
sold AS (
    SELECT
        date_trunc('month', stay_date)::DATE AS month,
        room_type_id,
        COUNT(*) AS sold_room_nights,
        COUNT(DISTINCT order_id) AS orders,
        SUM(revenue_fen) AS revenue_fen
    FROM stay_nights
    GROUP BY month, room_type_id
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
    ROUND(COALESCE(s.revenue_fen, 0), 2) AS revenue_fen,
    CASE
        WHEN g.room_count * g.days_in_month > 0
        THEN ROUND(
            COALESCE(s.sold_room_nights, 0) * 1.0 / (g.room_count * g.days_in_month), 6
        )
        ELSE NULL
    END AS occupancy_rate,
    CASE
        WHEN COALESCE(s.sold_room_nights, 0) > 0
        THEN ROUND(s.revenue_fen / s.sold_room_nights, 2)
        ELSE NULL
    END AS adr_fen
FROM grid g
LEFT JOIN sold s ON s.month = g.month AND s.room_type_id = g.room_type_id
ORDER BY g.month, g.room_type_code
"""


def _sql_path_list(paths: Sequence[str]) -> str:
    quoted = ", ".join("'" + str(path).replace("'", "''") + "'" for path in paths)
    return f"[{quoted}]"


def build_room_type_monthly(
    con: duckdb.DuckDBPyConnection,
    *,
    orders_paths: Sequence[str],
    room_types_paths: Sequence[str],
    rooms_paths: Sequence[str],
) -> pa.Table:
    """在 DuckDB 连接上构建房型月度入住率/客单价聚合。"""
    for view, paths in (
        ("orders_raw", orders_paths),
        ("room_types_raw", room_types_paths),
        ("rooms_raw", rooms_paths),
    ):
        con.execute(
            f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet({_sql_path_list(paths)})"
        )
    return con.execute(_ROOM_TYPE_MONTHLY_SQL).fetch_arrow_table()


__all__ = [
    "ROOM_TYPE_MONTHLY_TABLE",
    "STAY_STATUSES",
    "build_room_type_monthly",
]
