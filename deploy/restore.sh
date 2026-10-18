#!/usr/bin/env bash
# HaoChen PostgreSQL 恢复脚本
#
# 用法: ./restore.sh <备份文件> [目标数据库名] [--yes]
#
# 特性：
# - 自动识别 .gz / .enc（AES-256-CBC，使用 BACKUP_ENCRYPTION_KEY 解密）
# - 备份旁存在 .sha256 时先校验完整性
# - 默认交互确认；CI 用 --yes 或 RESTORE_ASSUME_YES=true 非交互执行
# - 只恢复到指定目标库；从不隐式覆盖 DATABASE_URL 指向的库（目标名必须显式给出或取默认）
# - 恢复后校验 RESTORE_VERIFY_TABLES（逗号分隔，默认 rooms,orders,messages,agent_runs）
#
# 重要：生产恢复请先恢复到新库并完成校验，再由发布负责人切换连接串。

set -euo pipefail

usage() {
    echo "用法: $0 <备份文件> [目标数据库名] [--yes]"
    echo "示例: $0 ./backups/haochen_20261018T120000Z.dump.gz.enc haochen_restored --yes"
    exit 1
}

log() {
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

fail() {
    log "ERROR: $*" >&2
    exit 1
}

BACKUP_FILE=""
TARGET_DB="haochen"
POSITIONAL=0
ASSUME_YES="${RESTORE_ASSUME_YES:-false}"

for arg in "$@"; do
    case "${arg}" in
        --yes)
            ASSUME_YES="true"
            ;;
        --help|-h)
            usage
            ;;
        *)
            POSITIONAL=$((POSITIONAL + 1))
            if [[ "${POSITIONAL}" -eq 1 ]]; then
                BACKUP_FILE="${arg}"
            elif [[ "${POSITIONAL}" -eq 2 ]]; then
                TARGET_DB="${arg}"
            else
                usage
            fi
            ;;
    esac
done

[[ -n "${BACKUP_FILE}" ]] || usage
[[ -f "${BACKUP_FILE}" ]] || fail "备份文件不存在: ${BACKUP_FILE}"
[[ "${TARGET_DB}" =~ ^[A-Za-z0-9_]+$ ]] || fail "目标数据库名只能包含字母、数字和下划线"

DB_URL="${DATABASE_URL:-postgresql://haochen:haochen@localhost:5432/haochen}"
DB_USER="${DB_USER:-$(printf '%s' "${DB_URL}" | sed -n 's|.*://\([^:]*\):.*|\1|p')}"
DB_PASS="${DB_PASS:-$(printf '%s' "${DB_URL}" | sed -n 's|.*://[^:]*:\([^@]*\)@.*|\1|p')}"
DB_HOST="${DB_HOST:-$(printf '%s' "${DB_URL}" | sed -n 's|.*@\([^:]*\):.*|\1|p')}"
DB_PORT="${DB_PORT:-$(printf '%s' "${DB_URL}" | sed -n 's|.*:\([0-9]*\)/.*|\1|p')}"

DB_USER="${DB_USER:-haochen}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"

if [[ -n "${DB_PASS}" ]]; then
    export PGPASSWORD="${DB_PASS}"
fi

log "=== HaoChen 数据库恢复 ==="
log "备份文件: ${BACKUP_FILE}"
log "目标数据库: ${DB_USER}@${DB_HOST}:${DB_PORT}/${TARGET_DB}"

if [[ -f "${BACKUP_FILE}.sha256" ]]; then
    (
        cd "$(dirname "${BACKUP_FILE}")"
        sha256sum -c "$(basename "${BACKUP_FILE}").sha256"
    ) || fail "备份校验和验证失败"
    log "校验和验证通过"
else
    log "WARN: 未找到 ${BACKUP_FILE}.sha256，跳过校验和验证"
fi

if [[ "${ASSUME_YES}" != "true" ]]; then
    read -r -p "此操作将覆盖目标数据库 ${TARGET_DB} 的所有数据。继续? (y/N) " CONFIRM
    if [[ "${CONFIRM}" != "y" && "${CONFIRM}" != "Y" ]]; then
        log "已取消"
        exit 0
    fi
fi

DB_EXISTS="$(psql \
    -h "${DB_HOST}" \
    -p "${DB_PORT}" \
    -U "${DB_USER}" \
    -d postgres \
    -tAc "SELECT 1 FROM pg_database WHERE datname='${TARGET_DB}'")"

if [[ "${DB_EXISTS}" != "1" ]]; then
    log "创建目标数据库 ${TARGET_DB}"
    createdb -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" "${TARGET_DB}"
fi

stream_backup() {
    if [[ "${BACKUP_FILE}" == *.enc ]]; then
        [[ -n "${BACKUP_ENCRYPTION_KEY:-}" ]] || fail "备份已加密，但未设置 BACKUP_ENCRYPTION_KEY"
        openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
            -in "${BACKUP_FILE}" \
            -pass env:BACKUP_ENCRYPTION_KEY | gzip -dc
    elif [[ "${BACKUP_FILE}" == *.gz ]]; then
        gzip -dc "${BACKUP_FILE}"
    else
        cat "${BACKUP_FILE}"
    fi
}

log "恢复中..."
stream_backup | pg_restore \
    -h "${DB_HOST}" \
    -p "${DB_PORT}" \
    -U "${DB_USER}" \
    -d "${TARGET_DB}" \
    --no-owner \
    --no-acl \
    --clean \
    --if-exists \
    --exit-on-error

log "恢复完成，开始校验"

VERIFY_TABLES="${RESTORE_VERIFY_TABLES:-rooms,orders,messages,agent_runs}"
IFS=',' read -r -a TABLE_LIST <<< "${VERIFY_TABLES}"

for table in "${TABLE_LIST[@]}"; do
    table="$(printf '%s' "${table}" | tr -d '[:space:]')"
    [[ -n "${table}" ]] || continue
    [[ "${table}" =~ ^[A-Za-z0-9_]+$ ]] || fail "非法校验表名: ${table}"
    EXISTS="$(psql \
        -h "${DB_HOST}" \
        -p "${DB_PORT}" \
        -U "${DB_USER}" \
        -d "${TARGET_DB}" \
        -tAc "SELECT to_regclass('public.${table}') IS NOT NULL")"
    [[ "${EXISTS}" == "t" ]] || fail "恢复校验失败：缺少表 ${table}"
    ROWS="$(psql \
        -h "${DB_HOST}" \
        -p "${DB_PORT}" \
        -U "${DB_USER}" \
        -d "${TARGET_DB}" \
        -tAc "SELECT count(*) FROM ${table}")"
    log "  ${table}: ${ROWS} 行"
done

log "恢复校验通过: ${BACKUP_FILE} -> ${TARGET_DB}"
