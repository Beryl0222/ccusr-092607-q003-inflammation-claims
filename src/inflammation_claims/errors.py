"""登记服务的领域错误。"""

from __future__ import annotations


class RegistryError(Exception):
    """所有登记服务错误的基类。"""


class ContractViolation(RegistryError):
    """事件或命令不满足领域契约。"""

    def __init__(self, issues: list[str]):
        super().__init__("；".join(issues))
        self.issues = issues


class UnknownAggregate(RegistryError):
    """引用了不存在的聚合。"""


class IdempotencyConflict(RegistryError):
    """同一事件标识携带了不同的事件体。"""


class ConcurrencyConflict(RegistryError):
    """聚合版本与期望版本不一致。"""


class StateGateError(RegistryError):
    """当前聚合状态不允许该操作。"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class ReviewGateError(RegistryError):
    """评审未通过分权或专项复核闸门。"""

    def __init__(self, missing: list[str], message: str):
        super().__init__(message)
        self.missing = missing


class QuotaExhausted(RegistryError):
    """受控查询额度不足，占用未成功。"""


class BatchError(RegistryError):
    """批量重算规格非法（环路、缺失依赖等）。"""


class BatchInterrupted(RegistryError):
    """批量重算被主动中断，已完成节点的状态已经持久化。"""
