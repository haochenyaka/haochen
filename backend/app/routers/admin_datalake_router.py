"""数据湖查询接口：DuckDB OLAP 查询、表管理、分区浏览。

权限：admin/manager。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.security import Identity, require_roles
from app.services.datalake_query_service import DatalakeQueryError, DatalakeQueryService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/datalake", tags=["admin-datalake"])

datalake_guard = require_roles("admin", "manager")
GuardDep = Annotated[Identity, Depends(datalake_guard)]


class QueryRequest(BaseModel):
    sql: str = Field(..., min_length=4, max_length=10000)
    limit: int = Field(default=1000, ge=1, le=50000)


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list]
    row_count: int
    sql: str


class TableInfo(BaseModel):
    name: str


class PartitionInfo(BaseModel):
    path: str
    table: str
    date: str | None = None
    part: int | None = None


class TablesResponse(BaseModel):
    tables: list[TableInfo]


class PartitionsResponse(BaseModel):
    table: str
    partitions: list[PartitionInfo]


class RoomTypeMonthlyResponse(BaseModel):
    rows: list[dict[str, Any]]


@router.get("/tables", response_model=TablesResponse)
async def list_datalake_tables(
    _identity: GuardDep,
) -> TablesResponse:
    """列出数据湖中所有可查询的表。"""
    svc = DatalakeQueryService()
    tables = await svc.list_tables()
    return TablesResponse(tables=[TableInfo(name=t) for t in tables])


@router.get("/tables/{table}/partitions", response_model=PartitionsResponse)
async def list_table_partitions(
    table: str,
    _identity: GuardDep,
) -> PartitionsResponse:
    """查看表的分区文件列表。"""
    svc = DatalakeQueryService()
    partitions = await svc.list_partitions(table)
    return PartitionsResponse(
        table=table,
        partitions=[PartitionInfo(**p) for p in partitions],
    )


@router.post("/query", response_model=QueryResponse)
async def query_datalake(
    body: QueryRequest,
    _identity: GuardDep,
) -> QueryResponse:
    """在数据湖上执行只读 SQL 查询。"""
    svc = DatalakeQueryService()
    try:
        result = await svc.query(body.sql, limit=body.limit)
        return QueryResponse(**result)
    except DatalakeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/analysis/room-type-monthly", response_model=RoomTypeMonthlyResponse)
async def room_type_monthly(
    _identity: GuardDep,
    month_from: Annotated[date | None, Query(description="起始月份，如 2026-01-01")] = None,
    month_to: Annotated[date | None, Query(description="结束月份，含当月")] = None,
) -> RoomTypeMonthlyResponse:
    """各房型月度入住率与客单价（DuckDB 预聚合）。"""
    if month_from is not None and month_to is not None and month_from > month_to:
        raise HTTPException(status_code=400, detail="month_from 不能晚于 month_to")
    svc = DatalakeQueryService()
    try:
        rows = await svc.room_type_monthly(month_from=month_from, month_to=month_to)
    except DatalakeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return RoomTypeMonthlyResponse(rows=rows)


@router.get("/preview/{table}", response_model=QueryResponse)
async def preview_table(
    table: str,
    _identity: GuardDep,
    limit: int = 20,
) -> QueryResponse:
    """预览表的前 N 行数据。"""
    svc = DatalakeQueryService()
    try:
        result = await svc.preview_table(table, limit=limit)
        return QueryResponse(**result)
    except DatalakeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
