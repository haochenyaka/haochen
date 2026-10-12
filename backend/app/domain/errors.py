"""领域错误词汇：服务层抛出，路由层映射 HTTP 语义（404/409/422），杜绝静默失败。"""

from __future__ import annotations


class DomainError(Exception):
    """领域错误基类：可预期的业务失败。"""


class NotFoundError(DomainError):
    def __init__(self, resource: str, key: object) -> None:
        super().__init__(f"{resource} 不存在：{key}")
        self.resource = resource
        self.key = key


class ValidationFailedError(DomainError, ValueError):
    """入参违反领域不变量（如退房日期不晚于入住日期）。"""


class ConflictError(DomainError):
    """状态冲突：非法迁移、并发改写或重复副作用。"""


class PermissionDeniedError(DomainError):
    """资源不属于当前身份：越权读取或跨归属操作一律拒绝（A6）。"""
