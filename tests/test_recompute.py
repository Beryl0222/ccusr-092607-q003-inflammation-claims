import tempfile
import threading
import unittest
from pathlib import Path

from inflammation_claims.errors import BatchError, BatchInterrupted, QuotaExhausted
from inflammation_claims.quota import QueryQuota
from inflammation_claims.recompute import RecomputeBatch

from scenarios import build_registry, freeze_default_cohort


def _node(node_id, *, code_hash=None, result_hash=None, deps=(), units=0):
    return {
        "node_id": node_id,
        "analysis_id": f"analysis-{node_id}",
        "study_id": "inflammation-study",
        "code_hash": code_hash or f"code-{node_id}",
        "sample_scope": f"scope-{node_id}",
        "result_hash": result_hash or f"result-{node_id}",
        "receipt": f"receipt-{node_id}",
        "deps": list(deps),
        "query_units": units,
    }


class RecomputeBatchTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry()
        freeze_default_cohort(self.registry)

    def test_dag_runs_in_dependency_order(self):
        nodes = [_node("a"), _node("b", deps=["a"]), _node("c", deps=["a"]), _node("d", deps=["b", "c"])]
        order: list[str] = []
        order_lock = threading.Lock()

        def runner(node):
            with order_lock:
                order.append(node["node_id"])
            return node

        batch = RecomputeBatch("batch-1", nodes, self.registry, max_workers=4, runner=runner)
        result = batch.run()
        self.assertEqual({"a", "b", "c", "d"}, set(result.completed))
        # 所有下游都在其依赖完成之后启动。
        self.assertEqual("a", order[0])
        self.assertLess(order.index("b"), order.index("d"))
        self.assertLess(order.index("c"), order.index("d"))

    def test_cycle_is_rejected(self):
        nodes = [_node("a", deps=["b"]), _node("b", deps=["a"])]
        with self.assertRaises(BatchError):
            RecomputeBatch("batch-x", nodes, self.registry)

    def test_missing_dependency_is_rejected(self):
        with self.assertRaises(BatchError):
            RecomputeBatch("batch-x", [_node("a", deps=["ghost"])], self.registry)

    def test_failed_dependency_blocks_downstream(self):
        nodes = [_node("a"), _node("b", deps=["a"])]

        def runner(node):
            if node["node_id"] == "a":
                raise RuntimeError("分析代码崩溃")
            return node

        batch = RecomputeBatch("batch-2", nodes, self.registry, runner=runner)
        result = batch.run()
        self.assertEqual(["a"], result.failed)
        self.assertEqual(["b"], result.blocked)

    def test_failed_node_retry_does_not_recompute_upstream(self):
        nodes = [_node("a"), _node("b", deps=["a"]), _node("c", deps=["b"])]

        def flaky_runner(node):
            if node["node_id"] == "b":
                raise RuntimeError("第一次跑到 b 失败")
            return node

        with tempfile.TemporaryDirectory() as tmp:
            progress = Path(tmp) / "progress.json"
            first = RecomputeBatch(
                "batch-3", nodes, self.registry, progress_path=progress, runner=flaky_runner
            )
            result = first.run()
            self.assertEqual(["a"], result.completed)
            self.assertEqual(["b"], result.failed)
            self.assertEqual(["c"], result.blocked)
            a_run_ref = result.nodes["a"].run_ref

            # 同批次规格 + 同进度文件恢复：已完成的 a 被继承且不再执行；
            # 上轮 failed 的 b 与 blocked 的 c 重新参与调度。
            executed: list[str] = []

            def tracking_runner(node):
                executed.append(node["node_id"])
                return node

            retry = RecomputeBatch(
                "batch-3",
                nodes,
                self.registry,
                progress_path=progress,
                runner=tracking_runner,
            )
            retry_result = retry.run()
            self.assertEqual({"a", "b", "c"}, set(retry_result.completed))
            self.assertEqual(["b", "c"], executed)
            self.assertEqual(a_run_ref, retry_result.nodes["a"].run_ref)

    def test_interrupted_batch_can_continue_from_progress(self):
        nodes = [_node("a"), _node("b"), _node("c", deps=["a", "b"])]
        cancel = threading.Event()

        def runner(node):
            if node["node_id"] == "a":
                cancel.set()
            return node

        with tempfile.TemporaryDirectory() as tmp:
            progress = Path(tmp) / "progress.json"
            batch = RecomputeBatch(
                "batch-4", nodes, self.registry, progress_path=progress, runner=runner, max_workers=1
            )
            with self.assertRaises(BatchInterrupted):
                batch.run(cancel=cancel)
            saved = progress.read_text(encoding="utf-8")
            self.assertIn("completed", saved)

            cont = RecomputeBatch(
                "batch-4",
                nodes,
                self.registry,
                progress_path=progress,
                runner=lambda n: n,
                max_workers=1,
            )
            result = cont.run()
            self.assertEqual({"a", "b", "c"}, set(result.completed))

    def test_quota_consumption_is_atomic_per_batch(self):
        nodes = [_node("a", units=60), _node("b", units=50, deps=["a"])]
        quota = QueryQuota(100)
        batch = RecomputeBatch("batch-q", nodes, self.registry, quota=quota)
        result = batch.run()
        self.assertEqual("completed", result.nodes["a"].status)
        self.assertEqual("failed", result.nodes["b"].status)
        self.assertIn("QuotaExhausted", result.nodes["b"].error or "")
        # a 成功占用 60；b 未能占用，余额停在 40。
        self.assertEqual(40, quota.remaining)

    def test_quota_reservation_replays_idempotently(self):
        quota = QueryQuota(100)
        nodes_a = [_node("a", units=30)]
        nodes_b = [_node("a", units=30)]
        with tempfile.TemporaryDirectory() as tmp:
            progress = Path(tmp) / "p.json"
            b1 = RecomputeBatch("dup", nodes_a, self.registry, quota=quota, progress_path=progress)
            b1.run()
            b2 = RecomputeBatch("dup", nodes_b, self.registry, quota=quota, progress_path=progress)
            b2.run()
            self.assertEqual(70, quota.remaining)

    def test_replayed_registration_keeps_same_run_ref(self):
        nodes = [_node("a")]
        with tempfile.TemporaryDirectory() as tmp:
            progress = Path(tmp) / "p.json"
            b1 = RecomputeBatch("dup2", nodes, self.registry, progress_path=progress)
            r1 = b1.run()
            b2 = RecomputeBatch("dup2", nodes, self.registry, progress_path=progress)
            r2 = b2.run()
            self.assertEqual(r1.nodes["a"].run_ref, r2.nodes["a"].run_ref)


if __name__ == "__main__":
    unittest.main()
