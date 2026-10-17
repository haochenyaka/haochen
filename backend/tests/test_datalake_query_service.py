"""数据湖房型月度查询：预聚合读取与缺失报错。"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.data_pipeline import lake_storage
from app.services.datalake_query_service import DatalakeQueryError, DatalakeQueryService

PARTITION = "agg_room_type_monthly/year=2026/month=10/day=17/part-000000.parquet"


class _FakeLake:
    def __init__(self, path: Path | None) -> None:
        self._path = path

    async def list_partitions(self, table: str) -> list[str]:
        return [PARTITION] if self._path is not None else []

    async def read_parquet_to_temp(self, object_key: str) -> Path:
        assert self._path is not None
        return self._path


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeLake) -> None:
    monkeypatch.setattr(lake_storage.LakeStorage, "from_settings", classmethod(lambda cls: fake))


def _write_preagg(tmp_path: Path) -> Path:
    path = tmp_path / "agg.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "month": date(2026, 1, 1),
                    "room_type_code": "KING",
                    "sold_room_nights": 10,
                },
                {
                    "month": date(2026, 2, 1),
                    "room_type_code": "KING",
                    "sold_room_nights": 20,
                },
            ]
        ),
        path,
    )
    return path


def test_room_type_monthly_reads_and_filters_preaggregate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, _FakeLake(_write_preagg(tmp_path)))
    svc = DatalakeQueryService()
    rows = asyncio.run(svc.room_type_monthly(month_from=date(2026, 2, 1)))
    assert rows == [
        {"month": "2026-02-01", "room_type_code": "KING", "sold_room_nights": 20},
    ]


def test_room_type_monthly_without_preaggregate_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeLake(None))
    svc = DatalakeQueryService()
    with pytest.raises(DatalakeQueryError):
        asyncio.run(svc.room_type_monthly())
