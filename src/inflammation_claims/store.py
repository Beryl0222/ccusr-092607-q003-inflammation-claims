"""仅追加事件存储。

提供事件标识幂等、聚合版本乐观并发和可选的 JSONL 落盘重放。
存储不做任何业务裁决；业务闸门见 `registry.py`。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .contracts import validate_event
from .errors import ConcurrencyConflict, ContractViolation, IdempotencyConflict

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


def load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class AppendResult:
    event: dict[str, Any]
    replayed: bool


def _fingerprint(event: Mapping[str, Any]) -> str:
    """事件业务指纹：同一 event_id 只允许在时间戳上不同。"""
    body = {k: v for k, v in event.items() if k != "occurred_at"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)


class EventStore:
    """内存索引 + 可选 JSONL 日志的仅追加存储。"""

    def __init__(self, path: str | Path | None = None, schema: Mapping[str, Any] | None = None):
        self._schema = dict(schema) if schema is not None else load_schema()
        self._events: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._versions: dict[tuple[str, str], int] = {}
        self._lock = threading.RLock()
        self._path = Path(path) if path is not None else None
        if self._path is not None and self._path.exists():
            self._replay()

    # ----- 基础读写 -----

    def append(
        self,
        event: Mapping[str, Any],
        *,
        expected_version: int | None = None,
    ) -> AppendResult:
        """追加事件。

        - 契约不满足抛 `ContractViolation`；
        - 相同 event_id 且业务指纹相同：幂等返回既有事件（`replayed=True`）；
        - 相同 event_id 但业务指纹不同：抛 `IdempotencyConflict`；
        - `expected_version` 与聚合当前版本不符：抛 `ConcurrencyConflict`。
        """
        issues = validate_event(event, self._schema)
        if issues:
            raise ContractViolation([f"{i.field}: {i.message}" for i in issues])
        event = dict(event)
        key = (event["aggregate_type"], event["aggregate_id"])
        with self._lock:
            existing = self._by_id.get(event["event_id"])
            if existing is not None:
                if _fingerprint(existing) != _fingerprint(event):
                    raise IdempotencyConflict(
                        f"事件标识 {event['event_id']} 已用于不同的事件体"
                    )
                return AppendResult(existing, replayed=True)
            current = self._versions.get(key, 0)
            if expected_version is not None and current != expected_version:
                raise ConcurrencyConflict(
                    f"聚合 {key} 当前版本 {current}，期望 {expected_version}"
                )
            if event["version"] != current + 1:
                raise ConcurrencyConflict(
                    f"聚合 {key} 下一版本应为 {current + 1}，收到 {event['version']}"
                )
            self._events.append(event)
            self._by_id[event["event_id"]] = event
            self._versions[key] = event["version"]
            if self._path is not None:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                    fh.flush()
        return AppendResult(event, replayed=False)

    def stream(self, aggregate_type: str, aggregate_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(e)
                for e in self._events
                if e["aggregate_type"] == aggregate_type and e["aggregate_id"] == aggregate_id
            ]

    def all_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._events]

    def version(self, aggregate_type: str, aggregate_id: str) -> int:
        with self._lock:
            return self._versions.get((aggregate_type, aggregate_id), 0)

    def exists(self, aggregate_type: str, aggregate_id: str) -> bool:
        return self.version(aggregate_type, aggregate_id) > 0

    def _replay(self) -> None:
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            issues = validate_event(event, self._schema)
            if issues:
                raise ContractViolation(
                    [f"日志重放失败 {i.field}: {i.message}" for i in issues]
                )
            key = (event["aggregate_type"], event["aggregate_id"])
            current = self._versions.get(key, 0)
            if event["event_id"] in self._by_id:
                raise IdempotencyConflict(
                    f"日志中事件标识重复：{event['event_id']}"
                )
            if event["version"] != current + 1:
                raise ConcurrencyConflict(
                    f"日志中聚合 {key} 版本不连续：期望 {current + 1}，实际 {event['version']}"
                )
            self._events.append(event)
            self._by_id[event["event_id"]] = event
            self._versions[key] = event["version"]
