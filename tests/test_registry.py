import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inflammation_claims.contracts import validate_event
from inflammation_claims.registry import ClaimRegistry, RegistryError


def make_registry() -> ClaimRegistry:
    registry = ClaimRegistry()
    registry.freeze_cohort(
        cohort_id="cohort-a",
        version=1,
        inclusion={"criteria": "age>=40", "enrolled": 50231},
        sample_processing={"fasting": True, "assay": "hs-CRP"},
        indicator_definitions={"crp": {"unit": "mg/L"}, "il6": {"unit": "pg/mL"}},
        imaging_measurements={"cac": "Agatston"},
        covariates=("age", "sex", "bmi"),
        sample_scope_hash="scope-a-v1",
    )
    registry.freeze_cohort(
        cohort_id="cohort-b",
        version=1,
        inclusion={"criteria": "age>=50", "enrolled": 21040},
        sample_processing={"fasting": False, "assay": "hs-CRP"},
        indicator_definitions={"crp": {"unit": "mg/L"}},
        imaging_measurements={"cac": "Agatston"},
        covariates=("age", "sex"),
        sample_scope_hash="scope-b-v1",
    )
    return registry


def receipt(
    analysis_id: str,
    *,
    analyst_id: str = "analyst-1",
    cohort_id: str = "cohort-a",
    cohort_version: int = 1,
    code_hash: str = "sha:code-v1",
    sample_scope_hash: str = "scope-a-v1",
    result_hash: str = "result-1",
    indicator_ids=("crp",),
    version: int | None = None,
) -> dict:
    body = {
        "analysis_id": analysis_id,
        "analyst_id": analyst_id,
        "cohort_id": cohort_id,
        "cohort_version": cohort_version,
        "code_hash": code_hash,
        "sample_scope_hash": sample_scope_hash,
        "result_hash": result_hash,
        "plan": {"model": "cox", "adjust": ["age", "sex", "bmi"]},
        "effect_estimates": {"hr_per_sd": 1.18, "ci95": [1.09, 1.27]},
        "indicator_ids": list(indicator_ids),
    }
    if version is not None:
        body["version"] = version
    return body


def approved_claim(registry: ClaimRegistry, claim_id: str, analysis_id: str, **kwargs) -> None:
    params = dict(
        claim_id=claim_id,
        analysis_id=analysis_id,
        author_id="analyst-1",
        title="慢性炎症与心血管风险",
        causal_hypothesis="炎症标志物升高提高心血管事件风险",
        certainty="moderate",
        limitations=("观察性关联，不能推出个体必然结果",),
    )
    params.update(kwargs)
    registry.submit_claim(**params)
    registry.record_review(claim_id=claim_id, reviewer_id="reviewer-1", role="statistical", decision="approve")
    if params.get("involves_individual_risk"):
        registry.record_review(claim_id=claim_id, reviewer_id="privacy-1", role="privacy", decision="approve")
        registry.record_review(
            claim_id=claim_id, reviewer_id="clinician-1", role="clinical_semantic", decision="approve"
        )
    registry.approve_claim(claim_id=claim_id, approver_id="pi-1")


class CohortAndQuotaTests(unittest.TestCase):
    def test_cohort_version_cannot_be_frozen_twice(self) -> None:
        registry = make_registry()
        with self.assertRaises(RegistryError) as ctx:
            registry.freeze_cohort(
                cohort_id="cohort-a",
                version=1,
                inclusion={},
                sample_processing={},
                indicator_definitions={},
                imaging_measurements={},
                covariates=(),
                sample_scope_hash="scope-a-v1",
            )
        self.assertEqual("cohort_version_frozen", ctx.exception.code)

    def test_quota_is_acquired_atomically_under_concurrency(self) -> None:
        registry = make_registry()
        registry.define_quota("controlled-query", 5)
        barrier = threading.Barrier(10)
        outcomes: list[str] = []

        def worker() -> None:
            barrier.wait(timeout=10)
            try:
                registry.acquire_quota("controlled-query", 1)
                outcomes.append("ok")
            except RegistryError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(5, outcomes.count("ok"))
        self.assertEqual(5, outcomes.count("quota_exhausted"))
        status = registry.quota_status("controlled-query")
        self.assertEqual(5, status.used)
        self.assertEqual(0, status.remaining)


class AnalysisRegistrationTests(unittest.TestCase):
    def test_identical_receipt_replays_idempotently(self) -> None:
        registry = make_registry()
        registry.define_quota("q", 10)
        first = registry.register_analysis(receipt("ana-1"), quota_id="q", quota_cost=2)
        replay = registry.register_analysis(receipt("ana-1"), quota_id="q", quota_cost=2)
        self.assertIs(first, replay)
        self.assertEqual(2, registry.quota_status("q").used)
        events = [e for e in registry.events() if e["event_type"] == "ANALYSIS_REGISTERED"]
        self.assertEqual(1, len(events))

    def test_conflicting_receipt_is_quarantined(self) -> None:
        registry = make_registry()
        original = registry.register_analysis(receipt("ana-1"))
        for changed in (
            receipt("ana-1", code_hash="sha:code-v2"),
            receipt("ana-1", sample_scope_hash="scope-a-v1-subset"),
            receipt("ana-1", result_hash="result-2"),
        ):
            with self.assertRaises(RegistryError) as ctx:
                registry.register_analysis(changed)
            self.assertEqual("analysis_conflict_quarantined", ctx.exception.code)
        self.assertEqual(3, len(registry.quarantined()))
        self.assertEqual(original, registry.get_analysis("ana-1"))

    def test_new_version_of_same_analysis_is_not_quarantined(self) -> None:
        registry = make_registry()
        registry.register_analysis(receipt("ana-1"))
        rerun = registry.register_analysis(
            receipt("ana-1", code_hash="sha:code-v2", result_hash="result-2", version=2)
        )
        self.assertEqual(2, rerun.version)
        self.assertEqual(0, len(registry.quarantined()))

    def test_registration_consumes_quota_atomically_under_concurrency(self) -> None:
        registry = make_registry()
        registry.define_quota("q", 3)
        barrier = threading.Barrier(8)
        outcomes: list[str] = []

        def worker(index: int) -> None:
            barrier.wait(timeout=10)
            try:
                registry.register_analysis(receipt(f"ana-{index}"), quota_id="q", quota_cost=1)
                outcomes.append("ok")
            except RegistryError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(3, outcomes.count("ok"))
        self.assertEqual(5, outcomes.count("quota_exhausted"))
        registered = [e for e in registry.events() if e["event_type"] == "ANALYSIS_REGISTERED"]
        self.assertEqual(3, len(registered))

    def test_receipt_must_reference_frozen_cohort(self) -> None:
        registry = make_registry()
        with self.assertRaises(RegistryError) as ctx:
            registry.register_analysis(receipt("ana-1", cohort_id="cohort-x"))
        self.assertEqual("cohort_unknown", ctx.exception.code)


class ClaimReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = make_registry()
        self.registry.register_analysis(receipt("ana-1"))

    def submit(self, claim_id: str = "claim-1", **kwargs) -> None:
        params = dict(
            claim_id=claim_id,
            analysis_id="ana-1",
            author_id="analyst-1",
            title="慢性炎症与心血管风险",
            causal_hypothesis="炎症升高提高心血管事件风险",
            certainty="moderate",
            limitations=("观察性关联",),
        )
        params.update(kwargs)
        self.registry.submit_claim(**params)

    def test_claim_freezes_data_slice_and_analysis_version(self) -> None:
        self.submit()
        claim = self.registry.get_claim("claim-1")
        run = self.registry.get_analysis("ana-1")
        self.assertEqual(("ana-1", 1), (claim.analysis_id, claim.analysis_version))
        self.assertEqual(run.fingerprint, claim.analysis_fingerprint)
        self.assertEqual(("cohort-a", 1), (claim.cohort_id, claim.cohort_version))
        self.assertEqual("in_review", claim.status)

    def test_analyst_cannot_approve_own_interpretation(self) -> None:
        self.submit()
        with self.assertRaises(RegistryError) as ctx:
            self.registry.record_review(
                claim_id="claim-1", reviewer_id="analyst-1", role="statistical", decision="approve"
            )
        self.assertEqual("self_approval_forbidden", ctx.exception.code)
        self.registry.record_review(claim_id="claim-1", reviewer_id="reviewer-1", role="statistical", decision="approve")
        with self.assertRaises(RegistryError) as ctx:
            self.registry.approve_claim(claim_id="claim-1", approver_id="analyst-1")
        self.assertEqual("self_approval_forbidden", ctx.exception.code)

    def test_approval_requires_statistical_review(self) -> None:
        self.submit()
        with self.assertRaises(RegistryError) as ctx:
            self.registry.approve_claim(claim_id="claim-1", approver_id="pi-1")
        self.assertEqual("review_missing", ctx.exception.code)

    def test_individual_risk_requires_privacy_and_clinical_review(self) -> None:
        self.submit(involves_individual_risk=True)
        self.registry.record_review(claim_id="claim-1", reviewer_id="reviewer-1", role="statistical", decision="approve")
        with self.assertRaises(RegistryError) as ctx:
            self.registry.approve_claim(claim_id="claim-1", approver_id="pi-1")
        self.assertEqual("review_missing", ctx.exception.code)
        self.assertIn("privacy", str(ctx.exception))
        self.assertIn("clinical_semantic", str(ctx.exception))
        self.registry.record_review(claim_id="claim-1", reviewer_id="privacy-1", role="privacy", decision="approve")
        self.registry.record_review(
            claim_id="claim-1", reviewer_id="clinician-1", role="clinical_semantic", decision="approve"
        )
        claim = self.registry.approve_claim(claim_id="claim-1", approver_id="pi-1")
        self.assertEqual("approved", claim.status)

    def test_reject_blocks_approval(self) -> None:
        self.submit()
        self.registry.record_review(claim_id="claim-1", reviewer_id="reviewer-1", role="statistical", decision="reject")
        with self.assertRaises(RegistryError) as ctx:
            self.registry.approve_claim(claim_id="claim-1", approver_id="pi-1")
        self.assertEqual("review_rejected", ctx.exception.code)


class CorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = make_registry()
        self.registry.register_analysis(receipt("ana-crp", indicator_ids=("crp",)))
        self.registry.register_analysis(receipt("ana-il6", indicator_ids=("il6",)))
        self.registry.register_analysis(
            receipt("ana-b", cohort_id="cohort-b", sample_scope_hash="scope-b-v1", indicator_ids=("crp",))
        )
        approved_claim(self.registry, "claim-crp", "ana-crp")
        approved_claim(self.registry, "claim-il6", "ana-il6")
        approved_claim(self.registry, "claim-b", "ana-b")
        self.registry.release_statement(
            statement_id="stmt-crp",
            claim_id="claim-crp",
            audience="public",
            text="炎症指标升高与心血管风险相关",
            editor_id="editor-1",
        )

    def test_indicator_correction_reopens_only_dependent_claims(self) -> None:
        result = self.registry.apply_correction(
            correction_id="corr-1",
            kind="indicator_correction",
            effective_at="2026-09-26T00:00:00+00:00",
            reason="hs-CRP 批次校准值更正",
            cohort_id="cohort-a",
            cohort_version=1,
            indicator_ids=("crp",),
        )
        self.assertEqual(("claim-crp",), result.reopened_claims)
        self.assertEqual("reopened", self.registry.get_claim("claim-crp").status)
        self.assertEqual("approved", self.registry.get_claim("claim-il6").status)
        self.assertEqual("approved", self.registry.get_claim("claim-b").status)

    def test_sample_withdrawal_scopes_by_cohort_version(self) -> None:
        result = self.registry.apply_correction(
            correction_id="corr-2",
            kind="sample_withdrawal",
            effective_at="2026-09-26T00:00:00+00:00",
            reason="受试者撤回知情同意",
            cohort_id="cohort-a",
            cohort_version=1,
        )
        self.assertEqual(("claim-crp", "claim-il6"), result.reopened_claims)
        self.assertEqual("approved", self.registry.get_claim("claim-b").status)

    def test_published_statement_keeps_snapshot_and_gains_erratum(self) -> None:
        statement = self.registry.get_statement("stmt-crp")
        snapshot_before = dict(statement.snapshot)
        self.registry.apply_correction(
            correction_id="corr-3",
            kind="indicator_correction",
            effective_at="2026-09-26T00:00:00+00:00",
            reason="hs-CRP 批次校准值更正",
            cohort_id="cohort-a",
            cohort_version=1,
            indicator_ids=("crp",),
        )
        statement = self.registry.get_statement("stmt-crp")
        self.assertEqual(snapshot_before, statement.snapshot)
        self.assertEqual("corrected", statement.status)
        self.assertEqual(1, len(statement.errata))
        self.assertEqual("corr-3", statement.errata[0]["correction_id"])

    def test_correction_is_idempotent(self) -> None:
        kwargs = dict(
            correction_id="corr-4",
            kind="indicator_correction",
            effective_at="2026-09-26T00:00:00+00:00",
            reason="hs-CRP 批次校准值更正",
            cohort_id="cohort-a",
            cohort_version=1,
            indicator_ids=("crp",),
        )
        first = self.registry.apply_correction(**kwargs)
        second = self.registry.apply_correction(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(1, len(self.registry.get_statement("stmt-crp").errata))

    def test_model_recalculation_targets_named_analyses(self) -> None:
        result = self.registry.apply_correction(
            correction_id="corr-5",
            kind="model_recalculation",
            effective_at="2026-09-26T00:00:00+00:00",
            reason="比例风险假设复核后重算",
            analysis_ids=("ana-il6",),
        )
        self.assertEqual(("claim-il6",), result.reopened_claims)
        self.assertEqual("approved", self.registry.get_claim("claim-crp").status)


class RecalculationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = make_registry()
        self.registry.register_analysis(receipt("ana-1"))
        self.registry.register_analysis(receipt("ana-2", code_hash="sha:meta-v1", result_hash="result-meta"))
        approved_claim(self.registry, "claim-1", "ana-1")
        approved_claim(self.registry, "claim-2", "ana-2")
        self.registry.apply_correction(
            correction_id="corr-recalc",
            kind="model_recalculation",
            effective_at="2026-09-26T00:00:00+00:00",
            reason="模型假设复核后重算",
            analysis_ids=("ana-1", "ana-2"),
        )
        self.registry.create_recalculation(
            batch_id="batch-1",
            tasks=[
                {"task_id": "t1", "analysis_id": "ana-1"},
                {"task_id": "t2", "analysis_id": "ana-2", "depends_on": ["t1"]},
            ],
        )

    def new_receipt(self, task) -> dict:
        return receipt(
            task.analysis_id,
            code_hash=f"sha:recalc-{task.task_id}",
            result_hash=f"result-recalc-{task.task_id}",
        )

    def test_interrupted_batch_resumes_from_incomplete_dependencies(self) -> None:
        attempts = {"t2_failed": False}

        def flaky(task):
            if task.task_id == "t2" and not attempts["t2_failed"]:
                attempts["t2_failed"] = True
                raise RuntimeError("计算集群中断")
            return self.new_receipt(task)

        with self.assertRaises(RuntimeError):
            self.registry.run_recalculation("batch-1", flaky)
        self.assertEqual(("t2",), self.registry.batch_status("batch-1").pending)
        completed = self.registry.run_recalculation("batch-1", flaky)
        self.assertEqual(("t2",), completed)
        self.assertEqual((), self.registry.batch_status("batch-1").pending)
        self.assertEqual(2, self.registry.get_analysis("ana-1").version)
        self.assertEqual(2, self.registry.get_analysis("ana-2").version)
        self.assertEqual(0, len(self.registry.quarantined()))

    def test_completed_batch_does_not_register_again(self) -> None:
        self.assertEqual(("t1", "t2"), self.registry.run_recalculation("batch-1", self.new_receipt))
        self.assertEqual((), self.registry.run_recalculation("batch-1", self.new_receipt))
        self.assertEqual(2, self.registry.get_analysis("ana-1").version)

    def test_resubmit_after_recalculation_requires_fresh_review(self) -> None:
        self.registry.run_recalculation("batch-1", self.new_receipt)
        claim = self.registry.resubmit_claim(claim_id="claim-1")
        self.assertEqual(2, claim.analysis_version)
        self.assertEqual("in_review", claim.status)
        with self.assertRaises(RegistryError) as ctx:
            self.registry.approve_claim(claim_id="claim-1", approver_id="pi-1")
        self.assertEqual("review_missing", ctx.exception.code)
        self.registry.record_review(claim_id="claim-1", reviewer_id="reviewer-2", role="statistical", decision="approve")
        self.assertEqual("approved", self.registry.approve_claim(claim_id="claim-1", approver_id="pi-1").status)


class ViewAndTraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = make_registry()
        self.registry.register_analysis(receipt("ana-1"))
        approved_claim(
            self.registry,
            "claim-1",
            "ana-1",
            involves_individual_risk=True,
            limitations=("观察性关联，不能推出个体必然结果", "残余混杂可能存在"),
        )

    def test_editor_cannot_see_unapproved_claim(self) -> None:
        self.registry.submit_claim(
            claim_id="claim-draft",
            analysis_id="ana-1",
            author_id="analyst-1",
            title="草稿结论",
            causal_hypothesis="假设",
            certainty="low",
        )
        with self.assertRaises(RegistryError) as ctx:
            self.registry.view_claim("claim-draft", role="editor")
        self.assertEqual("claim_not_public", ctx.exception.code)

    def test_role_views_expose_appropriate_granularity(self) -> None:
        researcher = self.registry.view_claim("claim-1", role="researcher")
        self.assertEqual("analyst-1", researcher["author_id"])
        self.assertEqual("sha:code-v1", researcher["analysis"]["code_hash"])
        self.assertEqual("hs-CRP", researcher["cohort"]["sample_processing"]["assay"])

        reviewer = self.registry.view_claim("claim-1", role="reviewer")
        self.assertNotIn("author_id", reviewer)
        self.assertNotIn("analyst_id", reviewer["analysis"])
        self.assertEqual("sha:code-v1", reviewer["analysis"]["code_hash"])
        self.assertEqual(3, len(reviewer["reviews"]))

        editor = self.registry.view_claim("claim-1", role="editor")
        self.assertNotIn("analysis", editor)
        self.assertNotIn("author_id", editor)
        self.assertNotIn("reviews", editor)
        self.assertEqual("moderate", editor["certainty"])
        self.assertIn("观察性关联，不能推出个体必然结果", editor["limitations"])

    def test_unknown_role_is_rejected(self) -> None:
        with self.assertRaises(RegistryError) as ctx:
            self.registry.view_claim("claim-1", role="anonymous")
        self.assertEqual("role_unknown", ctx.exception.code)

    def test_statement_trace_covers_version_review_limits_and_errata(self) -> None:
        self.registry.release_statement(
            statement_id="stmt-1",
            claim_id="claim-1",
            audience="public",
            text="炎症与心脏风险相关（队列层面）",
            editor_id="editor-1",
        )
        self.registry.apply_correction(
            correction_id="corr-9",
            kind="indicator_correction",
            effective_at="2026-09-27T00:00:00+00:00",
            reason="指标单位更正",
            cohort_id="cohort-a",
            cohort_version=1,
            indicator_ids=("crp",),
        )
        trace = self.registry.trace_statement("stmt-1")
        self.assertEqual({"cohort_id": "cohort-a", "cohort_version": 1}, {
            "cohort_id": trace["data_version"]["cohort_id"],
            "cohort_version": trace["data_version"]["cohort_version"],
        })
        self.assertEqual(("ana-1", 1), (trace["data_version"]["analysis_id"], trace["data_version"]["analysis_version"]))
        roles = {entry["role"] for entry in trace["review_trail"]}
        self.assertEqual({"statistical", "privacy", "clinical_semantic"}, roles)
        self.assertIn("残余混杂可能存在", trace["limitations"])
        self.assertEqual("corr-9", trace["errata"][0]["correction_id"])
        self.assertEqual("reopened", trace["claim"]["status"])

    def test_editor_view_keeps_published_statement_with_errata_after_reopen(self) -> None:
        self.registry.release_statement(
            statement_id="stmt-1",
            claim_id="claim-1",
            audience="public",
            text="炎症与心脏风险相关（队列层面）",
            editor_id="editor-1",
        )
        self.registry.apply_correction(
            correction_id="corr-10",
            kind="sample_withdrawal",
            effective_at="2026-09-27T00:00:00+00:00",
            reason="受试者撤回",
            cohort_id="cohort-a",
            cohort_version=1,
        )
        editor = self.registry.view_claim("claim-1", role="editor")
        self.assertEqual("reopened", editor["status"])
        self.assertEqual("corrected", editor["statements"][0]["status"])
        self.assertEqual("corr-10", editor["statements"][0]["errata"][0]["correction_id"])


class EventContractTests(unittest.TestCase):
    def test_emitted_events_satisfy_domain_contract(self) -> None:
        schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        registry = make_registry()
        registry.register_analysis(receipt("ana-1"))
        approved_claim(registry, "claim-1", "ana-1")
        registry.release_statement(
            statement_id="stmt-1",
            claim_id="claim-1",
            audience="paper",
            text="队列层面关联",
            editor_id="editor-1",
        )
        registry.apply_correction(
            correction_id="corr-1",
            kind="sample_withdrawal",
            effective_at="2026-09-27T00:00:00+00:00",
            reason="受试者撤回",
            cohort_id="cohort-a",
            cohort_version=1,
        )
        events = registry.events()
        self.assertEqual(
            ["COHORT_FROZEN", "COHORT_FROZEN", "ANALYSIS_REGISTERED", "CLAIM_REVIEWED", "CLAIM_REVIEWED", "STATEMENT_RELEASED", "EVIDENCE_RETRACTED"],
            [event["event_type"] for event in events],
        )
        for event in events:
            self.assertEqual([], validate_event(event, schema), event["event_id"])


if __name__ == "__main__":
    unittest.main()
