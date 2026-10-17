from datetime import date
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from app.data_pipeline.business_aggregates import build_room_type_monthly


def _write(path: Path, rows: list[dict[str, object]]) -> str:
    pq.write_table(pa.Table.from_pylist(rows), path)
    return str(path)


def _room_types() -> list[dict[str, object]]:
    return [
        {
            "id": "rt-king",
            "code": "KING",
            "name": "大床房",
            "active": True,
            "updated_at": "2026-01-01T00:00:00",
        },
        {
            "id": "rt-twin",
            "code": "TWIN",
            "name": "双床房",
            "active": True,
            "updated_at": "2026-01-01T00:00:00",
        },
        {
            "id": "rt-empty",
            "code": "EMPTY",
            "name": "空置房型",
            "active": True,
            "updated_at": "2026-01-01T00:00:00",
        },
    ]


def _rooms() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(10):
        rows.append({"id": f"king-{index}", "room_type_id": "rt-king", "updated_at": "2026-01-01"})
    for index in range(5):
        rows.append({"id": f"twin-{index}", "room_type_id": "rt-twin", "updated_at": "2026-01-01"})
    for index in range(4):
        rows.append(
            {"id": f"empty-{index}", "room_type_id": "rt-empty", "updated_at": "2026-01-01"}
        )
    return rows


def _order(
    *,
    order_id: str,
    room_type_id: str,
    status: str,
    nights: int,
    amount_fen: int,
    check_in: date,
    check_out: date,
    updated_at: str,
) -> dict[str, object]:
    return {
        "id": order_id,
        "room_type_id": room_type_id,
        "status": status,
        "nights": nights,
        "amount_fen": amount_fen,
        "check_in_date": check_in.isoformat(),
        "check_out_date": check_out.isoformat(),
        "created_at": "2025-12-01T00:00:00",
        "updated_at": updated_at,
    }


def _build(tmp_path: Path, orders: list[dict[str, object]]) -> list[dict[str, object]]:
    con = duckdb.connect()
    try:
        table = build_room_type_monthly(
            con,
            orders_paths=[_write(tmp_path / "orders.parquet", orders)],
            room_types_paths=[_write(tmp_path / "room_types.parquet", _room_types())],
            rooms_paths=[_write(tmp_path / "rooms.parquet", _rooms())],
        )
        return [dict(row) for row in table.to_pylist()]
    finally:
        con.close()


def test_monthly_occupancy_and_adr_with_cross_month_stay(tmp_path: Path) -> None:
    orders = [
        _order(
            order_id="o1",
            room_type_id="rt-king",
            status="created",
            nights=3,
            amount_fen=30000,
            check_in=date(2026, 1, 30),
            check_out=date(2026, 2, 2),
            updated_at="2025-12-30T00:00:00",
        ),
        _order(
            order_id="o1",
            room_type_id="rt-king",
            status="completed",
            nights=3,
            amount_fen=30000,
            check_in=date(2026, 1, 30),
            check_out=date(2026, 2, 2),
            updated_at="2026-02-03T00:00:00",
        ),
        _order(
            order_id="o2",
            room_type_id="rt-king",
            status="cancelled",
            nights=2,
            amount_fen=20000,
            check_in=date(2026, 1, 15),
            check_out=date(2026, 1, 17),
            updated_at="2026-01-10T00:00:00",
        ),
        _order(
            order_id="o3",
            room_type_id="rt-twin",
            status="paid",
            nights=2,
            amount_fen=30000,
            check_in=date(2026, 2, 1),
            check_out=date(2026, 2, 3),
            updated_at="2026-02-01T00:00:00",
        ),
    ]

    rows = _build(tmp_path, orders)
    by_key = {(str(row["month"]), str(row["room_type_code"])): row for row in rows}

    assert len(rows) == 6  # 2 个月 × 3 个房型
    jan_king = by_key[("2026-01-01", "KING")]
    assert jan_king["sold_room_nights"] == 2
    assert jan_king["orders"] == 1
    assert jan_king["available_room_nights"] == 310
    assert jan_king["occupancy_rate"] == round(2 / 310, 6)
    assert jan_king["revenue_fen"] == 20000.0
    assert jan_king["adr_fen"] == 10000.0

    feb_king = by_key[("2026-02-01", "KING")]
    assert feb_king["sold_room_nights"] == 1
    assert feb_king["occupancy_rate"] == round(1 / 280, 6)
    assert feb_king["adr_fen"] == 10000.0

    feb_twin = by_key[("2026-02-01", "TWIN")]
    assert feb_twin["sold_room_nights"] == 2
    assert feb_twin["available_room_nights"] == 140
    assert feb_twin["adr_fen"] == 15000.0

    jan_empty = by_key[("2026-01-01", "EMPTY")]
    assert jan_empty["sold_room_nights"] == 0
    assert jan_empty["occupancy_rate"] == 0.0
    assert jan_empty["adr_fen"] is None


def test_no_stay_orders_yields_empty_result(tmp_path: Path) -> None:
    rows = _build(
        tmp_path,
        [
            _order(
                order_id="o1",
                room_type_id="rt-king",
                status="cancelled",
                nights=1,
                amount_fen=10000,
                check_in=date(2026, 1, 1),
                check_out=date(2026, 1, 2),
                updated_at="2026-01-01T00:00:00",
            )
        ],
    )
    assert rows == []
