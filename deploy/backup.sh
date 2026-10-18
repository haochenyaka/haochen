#!/usr/bin/env bash
# HaoChen PostgreSQL 逻辑备份
#
# 用法: ./backup.sh [输出目录]
#
# 可选环境变量（均有安全默认值）：
#   DATABASE_URL              连接串；也可用 DB_HOST/DB_PORT/DB_USER/DB_PASS/DB_NAME 逐项覆盖
#   BACKUP_DIR                本地备份目录，默认 ./backups
#   BACKUP_RETENTION_DAYS     本地与异地保留天数，默认 30
#   BACKUP_ENCRYPTION_KEY     设置后使用 AES-256-CBC(pbkdf2) 加密备份
#   BACKUP_REQUIRE_ENCRYPTION true 时未配置密钥直接失败（生产必须为 true）
#   OFFSITE_BACKUP_DIR        异地目录（必须是不同故障域，如独立挂载/对象存储网关）
#   BACKUP_REQUIRE_OFFSITE    true 时未配置异地目录直接失败（生产必须为 true）
#
# 产物：haochen_<UTC时间>.dump.gz[.enc] + 同名 .sha256，并追加记录到 manifest.txt。
# 退出码非 0 表示本次备份失败；调度侧应保留失败日志并告警。

set -euo pipefail

log() {
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

fail() {
    log "ERROR: $*" >&2
    exit 1
}

BACKUP_DIR="${BACKUP_DIR:-${1:-./backups}}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
DB_URL="${DATABASE_URL:-postgresql://haochen:haochen@localhost:5432/haochen}"

DB_USER="${DB_USER:-$(printf '%s' "${DB_URL}" | sed -n 's|.*://\([^:]*\):.*|\1|p')}"
DB_PASS="${DB_PASS:-$(printf '%s' "${DB_URL}" | sed -n 's|.*://[^:]*:\([^@]*\)@.*|\1|p')}"
DB_HOST="${DB_HOST:-$(printf '%s' "${DB_URL}" | sed -n 's|.*@\([^:]*\):.*|\1|p')}"
DB_PORT="${DB_PORT:-$(printf '%s' "${DB_URL}" | sed -n 's|.*:\([0-9]*\)/.*|\1|p')}"
DB_NAME="${DB_NAME:-$(printf '%s' "${DB_URL}" | sed -n 's|.*/\([^?]*\).*|\1|p')}"

DB_USER="${DB_USER:-haochen}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-haochen}"

[[ "${RETENTION_DAYS}" =~ ^[0-9]+$ ]] || fail "BACKUP_RETENTION_DAYS 必须是非负整数"

mkdir -p "${BACKUP_DIR}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BASE="haochen_${STAMP}"
DUMP_FILE="${BACKUP_DIR}/${BASE}.dump"
ARCHIVE_FILE="${DUMP_FILE}.gz"
FINAL_FILE="${ARCHIVE_FILE}"

log "=== HaoChen 数据库备份 ==="
log "目标: ${DB_USER}@${DB_HOST}:${DB_PORT}/${DB_NAME} -> ${BACKUP_DIR}"

cleanup() {
    rm -f "${DUMP_FILE}"
}
trap cleanup EXIT

if [[ -n "${DB_PASS}" ]]; then
    export PGPASSWORD="${DB_PASS}"
fi

pg_dump \
    -h "${DB_HOST}" \
    -p "${DB_PORT}" \
    -U "${DB_USER}" \
    -d "${DB_NAME}" \
    --no-owner \
    --no-acl \
    --format=custom \
    --compress=9 \
    --file="${DUMP_FILE}"

gzip -9 -f "${DUMP_FILE}"

if [[ -n "${BACKUP_ENCRYPTION_KEY:-}" ]]; then
    command -v openssl >/dev/null 2>&1 || fail "已配置 BACKUP_ENCRYPTION_KEY，但系统缺少 openssl"
    openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
        -in "${ARCHIVE_FILE}" \
        -out "${ARCHIVE_FILE}.enc" \
        -pass env:BACKUP_ENCRYPTION_KEY
    rm -f "${ARCHIVE_FILE}"
    FINAL_FILE="${ARCHIVE_FILE}.enc"
    # 解密回读校验，确保密钥可用且归档可还原
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
        -in "${FINAL_FILE}" \
        -pass env:BACKUP_ENCRYPTION_KEY | gzip -t
elif [[ "${BACKUP_REQUIRE_ENCRYPTION:-false}" == "true" ]]; then
    fail "BACKUP_REQUIRE_ENCRYPTION=true，但未设置 BACKUP_ENCRYPTION_KEY"
else
    gzip -t "${FINAL_FILE}"
fi

CHECKSUM="$(sha256sum "${FINAL_FILE}" | awk '{print $1}')"
printf '%s  %s\n' "${CHECKSUM}" "$(basename "${FINAL_FILE}")" > "${FINAL_FILE}.sha256"
SIZE_BYTES="$(wc -c < "${FINAL_FILE}" | tr -d ' ')"

{
    echo "time=${STAMP}"
    echo "database=${DB_NAME}"
    echo "file=$(basename "${FINAL_FILE}")"
    echo "size_bytes=${SIZE_BYTES}"
    echo "sha256=${CHECKSUM}"
    echo "encrypted=$([[ "${FINAL_FILE}" == *.enc ]] && echo true || echo false)"
    echo "---"
} >> "${BACKUP_DIR}/manifest.txt"

cp_file_offsite() {
    local src="$1"
    local dest_dir="$2"
    cp -f "${src}" "${src}.sha256" "${dest_dir}/"
    (
        cd "${dest_dir}"
        sha256sum -c "$(basename "${src}").sha256" >/dev/null
    )
}

OFFSITE_DIR="${OFFSITE_BACKUP_DIR:-}"
if [[ -n "${OFFSITE_DIR}" ]]; then
    mkdir -p "${OFFSITE_DIR}"
    if [[ "$(cd "${OFFSITE_DIR}" && pwd -P)" == "$(cd "${BACKUP_DIR}" && pwd -P)" ]]; then
        fail "OFFSITE_BACKUP_DIR 不能与 BACKUP_DIR 相同"
    fi
    cp_file_offsite "${FINAL_FILE}" "${OFFSITE_DIR}"
    find "${OFFSITE_DIR}" -maxdepth 1 -type f -name 'haochen_*' -mtime +"${RETENTION_DAYS}" -delete
    log "异地副本已校验: ${OFFSITE_DIR}/$(basename "${FINAL_FILE}")"
elif [[ "${BACKUP_REQUIRE_OFFSITE:-false}" == "true" ]]; then
    fail "BACKUP_REQUIRE_OFFSITE=true，但未设置 OFFSITE_BACKUP_DIR"
else
    log "WARN: 未配置 OFFSITE_BACKUP_DIR，本次只有本地副本"
fi

find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'haochen_*' -mtime +"${RETENTION_DAYS}" -delete

log "备份完成: ${FINAL_FILE} (${SIZE_BYTES} 字节)"
log "校验和: ${CHECKSUM}"
