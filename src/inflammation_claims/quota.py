"""受控查询额度的原子占用。

多项分析并发消耗同一额度池时，占用在同一把锁内完成"检查-扣减-登记预留"；
相同预留标识重放是幂等的；分析注册成功前放弃占用可退还。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .errors import QuotaExhausted


@dataclass(frozen=True)
class QuotaSnapshot:
    budget: int
    used: int
    reserved: dict[str, int]

    @property
    def remaining(self) -> int:
        return self.budget - self.used


class QueryQuota:
    def __init__(self, budget: int):
        if budget < 0:
            raise ValueError("额度不能为负")
        self._budget = budget
        self._used = 0
        self._reservations: dict[str, int] = {}
        self._lock = threading.RLock()

    @property
    def remaining(self) -> int:
        with self._lock:
            return self._budget - self._used

    def snapshot(self) -> QuotaSnapshot:
        with self._lock:
            return QuotaSnapshot(self._budget, self._used, dict(self._reservations))

    def reserve(self, reservation_id: str, units: int) -> int:
        """原子占用额度。

        - 相同 reservation_id 重放：返回首次占用的额度，不重复扣减；
        - 余额不足：抛 QuotaExhausted，余额不变。
        """
        if units <= 0:
            raise ValueError("占用额度必须为正整数")
        with self._lock:
            if reservation_id in self._reservations:
                return self._reservations[reservation_id]
            if self._used + units > self._budget:
                raise QuotaExhausted(
                    f"受控查询额度不足：需要 {units}，剩余 {self._budget - self._used}"
                )
            self._used += units
            self._reservations[reservation_id] = units
            return units

    def release(self, reservation_id: str) -> int:
        """退还预留（注册失败等回滚路径）。返回退还量；未知预留返回 0。"""
        with self._lock:
            units = self._reservations.pop(reservation_id, 0)
            self._used = max(0, self._used - units)
            return units
