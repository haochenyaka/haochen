"""数据湖查询接口：DuckDB OLAP 查询、表管理、分区浏览。

权限：admin/manager。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
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
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/preview/{table}", response_model=QueryResponse)
async def preview_table(
    table: str,
    _identity: GuardDep,
    limit: int = 20,
) -> QueryResponse:
    """预览表的前 N 行数据。"""
    svc = DatalakeQueryService()
    try:
        result = await svc.query(f"SELECT * FROM \"{table}\" LIMIT {min(limit, 100)}", limit=0)
        return QueryResponse(**result)
    except DatalakeQueryError as e:
        raise HTTPException(status_code=400, detail=str(e))