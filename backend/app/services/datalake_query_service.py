"""数据湖查询服务：使用 DuckDB 读取 MinIO Parquet 进行 OLAP 分析。

设计：
- 查询时从 MinIO 下载 Parquet 到临时目录，DuckDB 读取本地文件；
- 返回结果以 Arrow Table 形式，转 Python dict 列表；
- SQL 执行在子线程中进行，不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from app.data_pipeline.business_aggregates import ROOM_TYPE_MONTHLY_TABLE
from app.data_pipeline.lake_storage import LakeStorage

logger = logging.getLogger(__name__)

# 允许的 SELECT 查询前缀（安全控制）
_ALLOWED_PREFIXES = ("SELECT", "WITH", "DESCRIBE", "SHOW", "PRAGMA")
# 禁止的 SQL 关键字（只读查询）
_BLOCKED_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "GRANT", "COPY")


class DatalakeQueryError(Exception):
    pass


class DatalakeQueryService:
    """DuckDB 数据湖查询服务。"""

    def __init__(self) -> None:
        self._storage = LakeStorage.from_settings()

    async def list_tables(self) -> list[str]:
        """列出数据湖中可查询的表。"""
        return await self._storage.list_tables()

    async def list_partitions(self, table: str) -> list[dict[str, Any]]:
        """列出表的分区信息。"""
        files = await self._storage.list_partitions(table)
        partitions: list[dict[str, Any]] = []
        for f in files:
            parsed = _parse_object_key(f)
            if parsed:
                partitions.append(parsed)
        return partitions

    async def room_type_monthly(
        self, *, month_from: date | None = None, month_to: date | None = None
    ) -> list[dict[str, Any]]:
        """查询房型月度入住率与客单价预聚合表。"""
        files = await self._storage.list_partitions(ROOM_TYPE_MONTHLY_TABLE)
        if not files:
            raise DatalakeQueryError(
                "房型月度预聚合表不存在，请先运行 data_pipeline.build_aggregates"
            )

        local_paths = [str(await self._storage.read_parquet_to_temp(f)) for f in files]
        clauses = ["1 = 1"]
        params: list[Any] = []
        if month_from is not None:
            clauses.append("month >= CAST(? AS DATE)")
            params.append(month_from.isoformat())
        if month_to is not None:
            clauses.append("month <= CAST(? AS DATE)")
            params.append(month_to.isoformat())
        sql = (
            f"SELECT * FROM read_parquet({_path_list(local_paths)}) "
            f"WHERE {' AND '.join(clauses)} ORDER BY month, room_type_code"
        )

        def _run() -> list[dict[str, Any]]:
            con = duckdb.connect()
            try:
                rel = con.execute(sql, params)
                columns = [desc[0] for desc in rel.description]
                return [
                    {
                        column: _serialize_value(value)
                        for column, value in zip(columns, row, strict=True)
                    }
                    for row in rel.fetchall()
                ]
            except Exception as e:
                raise DatalakeQueryError(str(e)) from e
            finally:
                con.close()

        return await asyncio.to_thread(_run)

    async def query(self, sql: str, limit: int = 1000) -> dict[str, Any]:
        """执行只读 SQL 查询，返回结果集。

        Args:
            sql: SELECT 查询语句
            limit: 最大返回行数

        Returns:
            {"columns": [...], "rows": [[...], ...], "row_count": N}
        """
        sql_stripped = sql.strip()
        prefix = sql_stripped.split()[0].upper() if sql_stripped else ""

        if prefix not in _ALLOWED_PREFIXES:
            raise DatalakeQueryError(
                f"不支持的查询类型: {prefix}，仅允许 SELECT/WITH/DESCRIBE/SHOW/PRAGMA"
            )

        for kw in _BLOCKED_KEYWORDS:
            if kw in sql_stripped.upper():
                raise DatalakeQueryError(f"查询包含禁止的关键字: {kw}")

        # 异步执行 DuckDB 查询
        result = await asyncio.to_thread(self._execute_query, sql_stripped, limit)
        return result

    def _execute_query(self, sql: str, limit: int) -> dict[str, Any]:
        """在子线程中执行 DuckDB 查询。"""
        con = duckdb.connect()
        try:
            # 加载 Parquet 文件系统支持
            con.execute("SET threads TO 4")
            con.execute("SET memory_limit = '512MB'")

            # 列出数据湖表并注册为视图
            self._register_tables(con)

            # 执行查询
            sql_with_limit = f"SELECT * FROM ({sql}) AS _q LIMIT {limit}" if limit > 0 else sql
            rel = con.execute(sql_with_limit)
            columns = [desc[0] for desc in rel.description]
            rows = rel.fetchall()

            # 转换行为 dict 列表
            result_rows = []
            for row in rows:
                result_rows.append([_serialize_value(v) for v in row])

            return {
                "columns": columns,
                "rows": result_rows,
                "row_count": len(result_rows),
                "sql": sql,
            }
        except Exception as e:
            raise DatalakeQueryError(str(e)) from e
        finally:
            con.close()

    def _register_tables(self, con: duckdb.DuckDBPyConnection) -> None:
        """扫描 MinIO 数据湖，将 Parquet 文件注册为 DuckDB 视图。"""
        table_files: dict[str, list[str]] = {}

        # 同步调用（已在子线程中）
        client = self._storage._client
        bucket = self._storage._bucket
        try:
            for obj in client.list_objects(bucket, prefix="", recursive=True):
                if obj.object_name and obj.object_name.endswith(".parquet"):
                    parts = obj.object_name.split("/")
                    table_name = parts[0]
                    if table_name not in table_files:
                        table_files[table_name] = []
                    # 下载到临时文件
                    local = _download_to_temp(client, bucket, obj.object_name)
                    if local:
                        table_files[table_name].append(str(local))
        except Exception:
            logger.warning("数据湖扫描失败，部分表可能不可用", exc_info=True)

        for table_name, files in table_files.items():
            if files:
                try:
                    con.execute(
                        f'CREATE OR REPLACE VIEW "{table_name}" AS '
                        f"SELECT * FROM read_parquet({files})"
                    )
                    logger.info("registered view: %s (%d files)", table_name, len(files))
                except Exception:
                    logger.warning("注册视图失败: %s", table_name, exc_info=True)

    async def execute_aggregate(self, sql: str) -> list[dict[str, Any]]:
        """执行聚合查询，返回 dict 列表。"""
        result = await self.query(sql, limit=10000)
        columns = result["columns"]
        rows = result["rows"]
        return [dict(zip(columns, row, strict=True)) for row in rows]


def _path_list(paths: list[str]) -> str:
    quoted = ", ".join("'" + path.replace("'", "''") + "'" for path in paths)
    return f"[{quoted}]"


def _parse_object_key(key: str) -> dict[str, Any] | None:
    """解析 Parquet 对象键为结构化信息。"""
    from app.data_pipeline.lake_storage import _parse_date_path

    parsed = _parse_date_path(key)
    if not parsed:
        return {"path": key, "table": key.split("/")[0] if "/" in key else key}
    return {"path": key, "table": parsed[0], "date": parsed[1].isoformat(), "part": parsed[2]}


def _serialize_value(v: Any) -> Any:
    """DuckDB 返回值的序列化（处理 DuckDB 特有类型）。"""
    if v is None:
        return None
    if hasattr(v, "isoformat"):  # date, datetime
        return v.isoformat()
    if hasattr(v, "as_py"):  # pyarrow scalar
        return v.as_py()
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def _download_to_temp(client: Any, bucket: str, object_key: str) -> Path | None:
    """下载 MinIO 对象到临时文件。"""
    try:
        response = client.get_object(bucket, object_key)
        data = response.read()
        response.close()
        response.release_conn()
        tmp = Path(tempfile.mkdtemp()) / object_key.replace("/", "_")
        tmp.write_bytes(data)
        return tmp
    except Exception:
        logger.warning("下载失败: %s", object_key, exc_info=True)
        return None
