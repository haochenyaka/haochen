#!/usr/bin/env bash
# HaoChen PostgreSQL 数据库备份脚本
# 用法: ./backup.sh [输出目录]
# 输出: 压缩的 SQL 转储 + 备份清单

set -euo pipefail

BACKUP_DIR="${1:-./backups}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/haochen_${TIMESTAMP}.sql.gz"
MANIFEST="${BACKUP_DIR}/manifest.txt"
DB_URL="${DATABASE_URL:-postgresql://haochen:haochen@localhost:5432/haochen}"

mkdir -p "${BACKUP_DIR}"

echo "=== HaoChen 数据库备份 ==="
echo "时间: $(date)"
echo "备份目标: ${BACKUP_FILE}"

# 从 DATABASE_URL 提取连接参数
# 格式: postgresql://user:password@host:port/database
DB_USER=$(echo "${DB_URL}" | sed -n 's|.*://\([^:]*\):.*|\1|p')
DB_PASS=$(echo "${DB_URL}" | sed -n 's|.*://[^:]*:\([^@]*\)@.*|\1|p')
DB_HOST=$(echo "${DB_URL}" | sed -n 's|.*@\([^:]*\):.*|\1|p')
DB_PORT=$(echo "${DB_URL}" | sed -n 's|.*:\([0-9]*\)/.*|\1|p')
DB_NAME=$(echo "${DB_URL}" | sed -n 's|.*/\([^?]*\).*|\1|p')

echo "导出数据库 ${DB_NAME}..."

# 导出为压缩 SQL
PGPASSWORD="${DB_PASS}" pg_dump \
    -h "${DB_HOST}" \
    -p "${DB_PORT}" \
    -U "${DB_USER}" \
    -d "${DB_NAME}" \
    --no-owner \
    --no-acl \
    --format=custom \
    --compress=9 \
    --file="${BACKUP_FILE%.gz}" 2>&1

# 压缩
gzip -f "${BACKUP_FILE%.gz}"

# 验证
gunzip -t "${BACKUP_FILE}" && echo "备份完整性验证通过"

# 记录备份信息
BACKUP_SIZE=$(stat -c%s "${BACKUP_FILE}" 2>/dev/null || stat -f%z "${BACKUP_FILE}")
{
    echo "备份时间: ${TIMESTAMP}"
    echo "数据库: ${DB_NAME}"
    echo "备份文件: ${BACKUP_FILE}"
    echo "文件大小: ${BACKUP_SIZE} 字节"
    echo "---"
} >> "${MANIFEST}"

echo "备份完成: ${BACKUP_FILE}"
echo "文件大小: ${BACKUP_SIZE} 字节"

# 保留最近 30 天备份，删除更早的
find "${BACKUP_DIR}" -name "haochen_*.sql.gz" -mtime +30 -delete

exit 0