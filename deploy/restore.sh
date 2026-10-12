#!/usr/bin/env bash
# HaoChen PostgreSQL 数据库恢复脚本
# 用法: ./restore.sh <备份文件> [目标数据库名]
# 警告: 恢复会覆盖目标数据库所有数据

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "用法: $0 <备份文件.sql.gz> [目标数据库名]"
    echo "示例: $0 ./backups/haochen_20260930_120000.sql.gz haochen_restored"
    exit 1
fi

BACKUP_FILE="$1"
TARGET_DB="${2:-haochen}"

if [ ! -f "${BACKUP_FILE}" ]; then
    echo "错误: 备份文件不存在: ${BACKUP_FILE}"
    exit 1
fi

DB_URL="${DATABASE_URL:-postgresql://haochen:haochen@localhost:5432/haochen}"

# 从 DATABASE_URL 提取连接参数
DB_USER=$(echo "${DB_URL}" | sed -n 's|.*://\([^:]*\):.*|\1|p')
DB_PASS=$(echo "${DB_URL}" | sed -n 's|.*://[^:]*:\([^@]*\)@.*|\1|p')
DB_HOST=$(echo "${DB_URL}" | sed -n 's|.*@\([^:]*\):.*|\1|p')
DB_PORT=$(echo "${DB_URL}" | sed -n 's|.*:\([0-9]*\)/.*|\1|p')

echo "=== HaoChen 数据库恢复 ==="
echo "时间: $(date)"
echo "备份文件: ${BACKUP_FILE}"
echo "目标数据库: ${TARGET_DB}"

# 确认
read -p "此操作将覆盖目标数据库所有数据。继续? (y/N) " CONFIRM
if [ "${CONFIRM}" != "y" ] && [ "${CONFIRM}" != "Y" ]; then
    echo "已取消"
    exit 0
fi

# 创建目标数据库（如不存在）
PGPASSWORD="${DB_PASS}" psql \
    -h "${DB_HOST}" \
    -p "${DB_PORT}" \
    -U "${DB_USER}" \
    -d "postgres" \
    -c "SELECT 1 FROM pg_database WHERE datname='${TARGET_DB}'" 2>/dev/null | grep -q 1 || \
    PGPASSWORD="${DB_PASS}" createdb \
        -h "${DB_HOST}" \
        -p "${DB_PORT}" \
        -U "${DB_USER}" \
        "${TARGET_DB}"

echo "恢复中..."

# 解压并恢复
gunzip -c "${BACKUP_FILE}" | PGPASSWORD="${DB_PASS}" pg_restore \
    -h "${DB_HOST}" \
    -p "${DB_PORT}" \
    -U "${DB_USER}" \
    -d "${TARGET_DB}" \
    --no-owner \
    --no-acl \
    --clean \
    --if-exists \
    --verbose 2>&1

echo "恢复完成: ${BACKUP_FILE} → ${TARGET_DB}"

# 验证：检查关键表行数
echo "=== 恢复验证 ==="
PGPASSWORD="${DB_PASS}" psql \
    -h "${DB_HOST}" \
    -p "${DB_PORT}" \
    -U "${DB_USER}" \
    -d "${TARGET_DB}" \
    -c "
    SELECT 'rooms' as table_name, count(*) as rows FROM rooms
    UNION ALL SELECT 'orders', count(*) FROM orders
    UNION ALL SELECT 'messages', count(*) FROM messages
    UNION ALL SELECT 'agent_runs', count(*) FROM agent_runs
    ORDER BY table_name;"

exit 0