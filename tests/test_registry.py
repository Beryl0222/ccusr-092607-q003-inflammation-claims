import unittest

from inflammation_claims.errors import (
    ReviewGateError,
    StateGateError,
    UnknownAggregate,
)
from inflammation_claims.store import EventStore
from inflammation_claims.registry import Registry

from scenarios import (
    ANALYST,
    CLINICAL_REVIEWER,
    PRIVACY_REVIEWER,
    STAT_REVIEWER,
    approve_claim,
    build_registry,
    freeze_default_cohort,
    register_default_analysis,
    submit_default_claim,
)


class AnalysisRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry()
        freeze_default_cohort(self.registry)

    def test_receipt_replay_is_idempotent(self):
        first = register_default_analysis(self.registry, receipt="rcpt-x")
        second = register_default_analysis(self.registry, receipt="rcpt-x")
        self.assertEqual(first, second)

    def test_same_body_different_receipt_is_same_run(self):
        first = register_default_analysis(self.registry, receipt="rcpt-1")
        second = register_default_analysis(self.registry, receipt="rcpt-2")
        self.assertEqual(first, second)

    def test_same_analysis_id_different_code_hash_is_isolated(self):
        v1 = register_default_analysis(self.registry, receipt="r1", code_hash="code-v1")
        v2 = register_default_analysis(self.registry, receipt="r2", code_hash="code-v2")
        self.assertNotEqual(v1, v2)
        self.assertTrue(v1.startswith("cox-crp-imt::"))
        self.assertEqual(2, len(self.registry.by_analysis["cox-crp-imt"]))

    def test_different_sample_scope_or_result_is_isolated(self):
        base = register_default_analysis(self.registry, receipt="r1")
        wider = register_default_analysis(
            self.registry, receipt="r2", sample_scope="n=13002;crp_measured"
        )
        rerun = register_default_analysis(self.registry, receipt="r3", result_hash="result-bbb")
        self.assertEqual(3, len({base, wider, rerun}))

    def test_analysis_pins_cohort_version(self):
        run_ref = register_default_analysis(self.registry)
        self.assertEqual(1, self.registry.runs[run_ref]["cohort_version"])

    def test_cannot_register_against_unfrozen_cohort(self):
        with self.assertRaises(UnknownAggregate):
            register_default_analysis(self.registry, study_id="no-such-study")


class ReviewGateTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry()
        freeze_default_cohort(self.registry)

    def _roster(self, *, analyst_approves=True, independent=True, privacy=False, clinical=False):
        roster = []
        if analyst_approves:
            roster.append({"reviewer_id": ANALYST, "role": "statistical", "decision": "approved"})
        if independent:
            roster.append(
                {"reviewer_id": STAT_REVIEWER, "role": "statistical", "decision": "approved"}
            )
        if privacy:
            roster.append(
                {"reviewer_id": PRIVACY_REVIEWER, "role": "privacy", "decision": "approved"}
            )
        if clinical:
            roster.append(
                {"reviewer_id": CLINICAL_REVIEWER, "role": "clinical", "decision": "approved"}
            )
        return roster

    def test_analyst_alone_cannot_approve(self):
        submit_default_claim(self.registry)
        with self.assertRaises(ReviewGateError) as ctx:
            self.registry.review_claim(
                "claim-001",
                certainty="moderate",
                reviewers=[
                    {"reviewer_id": ANALYST, "role": "statistical", "decision": "approved"}
                ],
                decision="approved",
            )
        self.assertIn("independent_approver", ctx.exception.missing)
        self.assertEqual("in_review", self.registry.claims["claim-001"].status)

    def test_independent_approval_suffices_for_population_claim(self):
        submit_default_claim(self.registry, individual_risk=False)
        self.registry.review_claim(
            "claim-001",
            certainty="moderate",
            reviewers=self._roster(),
            decision="approved",
        )
        self.assertEqual("approved", self.registry.claims["claim-001"].status)

    def test_individual_risk_requires_privacy_and_clinical(self):
        submit_default_claim(self.registry, individual_risk=True)
        with self.assertRaises(ReviewGateError) as ctx:
            self.registry.review_claim(
                "claim-001",
                certainty="moderate",
                reviewers=self._roster(),
                decision="approved",
            )
        self.assertEqual(
            ["privacy_review", "clinical_semantic_review"], ctx.exception.missing
        )

    def test_individual_risk_passes_with_both_specialist_reviews(self):
        submit_default_claim(self.registry, individual_risk=True)
        self.registry.review_claim(
            "claim-001",
            certainty="high",
            reviewers=self._roster(privacy=True, clinical=True),
            decision="approved",
        )
        self.assertEqual("approved", self.registry.claims["claim-001"].status)

    def test_analyst_cannot_sneak_through_specialist_roles(self):
        submit_default_claim(self.registry, individual_risk=True)
        roster = [
            {"reviewer_id": ANALYST, "role": "statistical", "decision": "approved"},
            {"reviewer_id": STAT_REVIEWER, "role": "statistical", "decision": "approved"},
            {"reviewer_id": ANALYST, "role": "privacy", "decision": "approved"},
            {"reviewer_id": ANALYST, "role": "clinical", "decision": "approved"},
        ]
        with self.assertRaises(ReviewGateError) as ctx:
            self.registry.review_claim(
                "claim-001", certainty="high", reviewers=roster, decision="approved"
            )
        self.assertIn("privacy_review", ctx.exception.missing)

    def test_changes_requested_allows_resubmission(self):
        submit_default_claim(self.registry)
        self.registry.review_claim(
            "claim-001",
            certainty="low",
            reviewers=self._roster(),
            decision="changes_requested",
            note="补充敏感性分析",
        )
        self.assertEqual("changes_requested", self.registry.claims["claim-001"].status)
        run_v2 = register_default_analysis(self.registry, receipt="r2", result_hash="result-v2")
        submit_default_claim(self.registry, run_ref=run_v2)
        self.assertEqual("in_review", self.registry.claims["claim-001"].status)

    def test_only_approved_claim_can_be_published(self):
        submit_default_claim(self.registry)
        with self.assertRaises(StateGateError):
            self.registry.release_statement(
                "stmt-1",
                claim_ref="claim-001",
                audience="patient",
                wording="炎症高不等于你一定会得病",
            )


class CascadeTests(unittest.TestCase):
    def _approved_with_statement(self, *, individual_risk=False):
        registry = build_registry()
        claim_id = "claim-001"
        freeze_default_cohort(registry)
        submit_default_claim(registry, claim_id=claim_id, individual_risk=individual_risk)
        approve_claim(registry, claim_id, individual_risk=individual_risk)
        registry.release_statement(
            "stmt-1",
            claim_ref=claim_id,
            audience="patient",
            wording="队列中 CRP 较高者 5 年 MACE 风险增加 12%（HR 1.12）",
            limitations=["群体关联，不构成个人诊断"],
        )
        return registry

    def test_sample_withdrawal_reopens_only_dependent_claims(self):
        registry = self._approved_with_statement()
        # 第二个独立研究的结论不应被波及。
        freeze_default_cohort(registry, "other-study", slice_hash="other-hash")
        other_run = register_default_analysis(
            registry, study_id="other-study", receipt="other-r2"
        )
        registry.submit_claim(
            "claim-other",
            run_ref=other_run,
            analyst=ANALYST,
            effect={"HR": 1.0},
            individual_risk=False,
        )
        approve_claim(registry, "claim-other")

        reopened = registry.record_change(
            "chg-1",
            "sample_withdrawal",
            effective_at="2026-09-26T09:00:00+08:00",
            study_id="inflammation-study",
        )
        self.assertEqual(["claim-001"], reopened)
        self.assertEqual("reopened", registry.claims["claim-001"].status)
        self.assertEqual("approved", registry.claims["claim-other"].status)

    def test_published_statement_keeps_snapshot_and_gets_erratum(self):
        registry = self._approved_with_statement()
        original_wording = registry.statements["stmt-1"].wording
        registry.record_change(
            "chg-1",
            "indicator_correction",
            effective_at="2026-09-26T09:00:00+08:00",
            study_id="inflammation-study",
        )
        stmt = registry.statements["stmt-1"]
        self.assertEqual("erratum_attached", stmt.status)
        self.assertEqual(original_wording, stmt.wording)
        self.assertEqual("indicator_correction", stmt.errata[0]["reason"])

    def test_rereviewed_claim_releases_revision_chain(self):
        registry = self._approved_with_statement()
        registry.record_change(
            "chg-1",
            "model_recomputed",
            effective_at="2026-09-26T09:00:00+08:00",
            analysis_id="cox-crp-imt",
        )
        new_run = register_default_analysis(
            registry, receipt="r-new", code_hash="code-v2", result_hash="result-v2"
        )
        submit_default_claim(registry, run_ref=new_run)
        approve_claim(registry)
        registry.release_statement(
            "stmt-2",
            claim_ref="claim-001",
            audience="patient",
            wording="更正后：风险增加 9%（HR 1.09）",
            supersedes="stmt-1",
        )
        self.assertEqual("released", registry.statements["stmt-2"].status)
        self.assertEqual("erratum_attached", registry.statements["stmt-1"].status)

    def test_change_scoped_to_old_cohort_versions(self):
        registry = build_registry()
        freeze_default_cohort(registry)
        submit_default_claim(registry)
        approve_claim(registry)
        # 冻结 v2 切片；该结论仍锚定 v1。
        registry.freeze_cohort(
            "inflammation-study",
            inclusion_version="incl-2026-10",
            sample_processing="EDTA 血浆，30 分钟内离心，-80℃ 保存",
            indicator_definitions={"CRP": "免疫比浊法 mg/L"},
            covariates=["年龄"],
            slice_hash="slice-v2-hash",
        )
        reopened = registry.record_change(
            "chg-2",
            "sample_withdrawal",
            effective_at="2026-09-26T09:00:00+08:00",
            study_id="inflammation-study",
            new_cohort_version=2,
        )
        self.assertEqual(["claim-001"], reopened)


if __name__ == "__main__":
    unittest.main()
