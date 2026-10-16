"""报表数据湖探测的降级契约：湖不可达时返回 None 走上游 PostgreSQL 回退，不上抛。

报表对 token/tool 重聚合优先查数据湖预聚合表；探湖失败若上抛，报表/健康面板接口会因
数据湖抖动直接 5xx（而非按设计降级重算）。
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.data_pipeline import lake_storage
from app.services.report_service import ReportService

AGG_TABLE = "agg_token_daily"
SQL = "SELECT day, tokens FROM agg_token_daily"
PARTITION = f"{AGG_TABLE}/year=2026/month=10/day=01/part-000000.parquet"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class _UnreachableLake:
    """数据湖替身：探测即抛，模拟 MinIO 不可达。"""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def list_partitions(self, table: str) -> list[str]:
        raise self._error


class _FakeLake:
    """数据湖替身：分区清单指向本地 Parquet 文件。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def list_partitions(self, table: str) -> list[str]:
        return [PARTITION]

    async def read_parquet_to_temp(self, object_key: str) -> Path:
        return self._path


def _install(monkeypatch: Any, fake: Any) -> None:
    monkeypatch.setattr(lake_storage.LakeStorage, "from_settings", classmethod(lambda cls: fake))


def test_duckdb_query_degrades_when_lake_unreachable(monkeypatch: Any) -> None:
    _install(monkeypatch, _UnreachableLake(ConnectionError("minio unreachable")))
    assert run(ReportService(None)._duckdb_query_agg(AGG_TABLE, SQL)) is None


def test_duckdb_query_still_reads_preaggregates(monkeypatch: Any, tmp_path: Path) -> None:
    path = tmp_path / "part.parquet"
    pq.write_table(pa.table({"day": [date(2026, 10, 1)], "tokens": [123]}), path)
    _install(monkeypatch, _FakeLake(path))
    assert run(ReportService(None)._duckdb_query_agg(AGG_TABLE, SQL)) == [
        {"day": date(2026, 10, 1), "tokens": 123}
    ]
