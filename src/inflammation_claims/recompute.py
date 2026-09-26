"""按依赖图批量重算分析，中断后从未完成的依赖继续。

- 节点构成 DAG，依赖未完成的节点不会启动；依赖失败则下游标记 blocked；
- 每个节点状态在落库后立即持久化（原子写 JSON），重跑时跳过已完成节点；
- 受控查询额度通过预留标识 ``batch_id:node_id`` 原子占用，并发批次互不超用；
- 注册动作在同一把注册锁内完成，配合登记服务的回执幂等，重放安全。
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .errors import BatchError, BatchInterrupted
from .quota import QueryQuota
from .registry import Registry

NodeRunner = Callable[[dict[str, Any]], Mapping[str, Any]]

_DONE = "completed"
_FAILED = "failed"
_BLOCKED = "blocked"
_TERMINAL = (_DONE, _FAILED, _BLOCKED)


@dataclass
class NodeResult:
    node_id: str
    status: str
    run_ref: str | None = None
    error: str | None = None


@dataclass
class BatchResult:
    batch_id: str
    nodes: dict[str, NodeResult] = field(default_factory=dict)

    @property
    def completed(self) -> list[str]:
        return [n for n, r in self.nodes.items() if r.status == _DONE]

    @property
    def failed(self) -> list[str]:
        return [n for n, r in self.nodes.items() if r.status == _FAILED]

    @property
    def blocked(self) -> list[str]:
        return [n for n, r in self.nodes.items() if r.status == _BLOCKED]

    @property
    def unfinished(self) -> list[str]:
        return [n for n, r in self.nodes.items() if r.status not in _TERMINAL]


class RecomputeBatch:
    def __init__(
        self,
        batch_id: str,
        nodes: Sequence[Mapping[str, Any]],
        registry: Registry,
        quota: QueryQuota | None = None,
        *,
        progress_path: str | Path | None = None,
        max_workers: int = 4,
        runner: NodeRunner | None = None,
    ):
        self.batch_id = batch_id
        self.spec = {n["node_id"]: dict(n) for n in nodes}
        self.registry = registry
        self.quota = quota
        self.max_workers = max(1, max_workers)
        self.runner = runner
        self._register_lock = threading.RLock()
        self._progress_path = Path(progress_path) if progress_path else None
        self.results: dict[str, NodeResult] = {}
        self._validate_dag()
        self._load_progress()

    # ------------------------------------------------------------ 规格

    def _validate_dag(self) -> None:
        for node_id, node in self.spec.items():
            for dep in node.get("deps", []):
                if dep not in self.spec:
                    raise BatchError(f"节点 {node_id} 依赖了不存在的节点 {dep}")
        # 三色 DFS 检测环路。
        color: dict[str, int] = {n: 0 for n in self.spec}

        def visit(nid: str, stack: list[str]) -> None:
            color[nid] = 1
            for dep in self.spec[nid].get("deps", []):
                if color[dep] == 1:
                    cycle = " -> ".join(stack[stack.index(dep):] + [dep])
                    raise BatchError(f"依赖图存在环路：{cycle}")
                if color[dep] == 0:
                    visit(dep, stack + [dep])
            color[nid] = 2

        for nid in self.spec:
            if color[nid] == 0:
                visit(nid, [nid])

    # ------------------------------------------------------------ 进度

    def _load_progress(self) -> None:
        if self._progress_path is None or not self._progress_path.exists():
            return
        raw = json.loads(self._progress_path.read_text(encoding="utf-8"))
        if raw.get("batch_id") != self.batch_id:
            raise BatchError("进度文件属于其他批次，不能继续")
        for node_id, item in raw.get("nodes", {}).items():
            # 断点恢复只继承已完成节点；上轮失败或被阻塞的节点重新参与调度。
            if node_id in self.spec and item.get("status") == _DONE:
                self.results[node_id] = NodeResult(node_id, **item)

    def _persist(self) -> None:
        if self._progress_path is None:
            return
        payload = {
            "batch_id": self.batch_id,
            "nodes": {
                nid: {"status": r.status, "run_ref": r.run_ref, "error": r.error}
                for nid, r in self.results.items()
            },
        }
        tmp = self._progress_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._progress_path)

    # ------------------------------------------------------------ 执行

    def unfinished(self) -> list[str]:
        return [
            nid
            for nid in self.spec
            if (r := self.results.get(nid)) is None or r.status not in _TERMINAL
        ]

    def run(self, cancel: threading.Event | None = None) -> BatchResult:
        """执行（或继续）批次。cancel 被置位时完成在途节点后抛 BatchInterrupted。"""
        while True:
            ready, blocked_now = self._schedule()
            for nid in blocked_now:
                self._record(NodeResult(nid, _BLOCKED, error="上游依赖失败或被阻塞"))
            if not ready:
                break
            if cancel is not None and cancel.is_set():
                self._persist()
                raise BatchInterrupted(
                    f"批次 {self.batch_id} 中断，未完成节点：{', '.join(self.unfinished())}"
                )
            workers = min(self.max_workers, len(ready))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for result in pool.map(self._execute_node, ready):
                    self._record(result)
        self._persist()
        if cancel is not None and cancel.is_set():
            raise BatchInterrupted(
                f"批次 {self.batch_id} 中断，未完成节点：{', '.join(self.unfinished())}"
            )
        return BatchResult(self.batch_id, dict(self.results))

    def _schedule(self) -> tuple[list[str], list[str]]:
        ready: list[str] = []
        blocked: list[str] = []
        for nid, node in self.spec.items():
            current = self.results.get(nid)
            if current is not None and current.status in _TERMINAL:
                continue
            dep_states = [self.results.get(d) for d in node.get("deps", [])]
            if any(s is not None and s.status in (_FAILED, _BLOCKED) for s in dep_states):
                blocked.append(nid)
            elif all(s is not None and s.status == _DONE for s in dep_states):
                ready.append(nid)
        return ready, blocked

    def _record(self, result: NodeResult) -> None:
        self.results[result.node_id] = result
        self._persist()

    def _execute_node(self, node_id: str) -> NodeResult:
        node = self.spec[node_id]
        reservation_id = f"{self.batch_id}:{node_id}"
        units = int(node.get("query_units", 0))
        try:
            if self.quota is not None and units > 0:
                self.quota.reserve(reservation_id, units)
            params = self.runner(dict(node)) if self.runner is not None else dict(node)
            with self._register_lock:
                run_ref = self.registry.register_analysis(
                    params["analysis_id"],
                    study_id=params["study_id"],
                    code_hash=params["code_hash"],
                    sample_scope=params["sample_scope"],
                    result_hash=params["result_hash"],
                    receipt=params["receipt"],
                    analysis_plan=params.get("analysis_plan"),
                    query_units=units,
                )
            return NodeResult(node_id, _DONE, run_ref=run_ref)
        except Exception as exc:  # 节点失败被记录为状态，不炸掉整个批次
            if self.quota is not None and units > 0:
                self.quota.release(reservation_id)
            return NodeResult(node_id, _FAILED, error=f"{type(exc).__name__}: {exc}")
