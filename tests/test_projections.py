import unittest

from inflammation_claims.projections import project_claim, project_statement, trace_statement

from scenarios import (
    approve_claim,
    build_registry,
    freeze_default_cohort,
    register_default_analysis,
    submit_default_claim,
)


def _full_scenario(*, individual_risk=True):
    registry = build_registry()
    freeze_default_cohort(registry)
    submit_default_claim(registry, individual_risk=individual_risk)
    approve_claim(registry, individual_risk=individual_risk)
    registry.release_statement(
        "stmt-1",
        claim_ref="claim-001",
        audience="patient",
        wording="队列中 CRP 较高者风险更高，但这不能预测你个人一定患病",
        limitations=["观察性关联，非个人诊断"],
    )
    return registry


class ClaimProjectionTests(unittest.TestCase):
    def test_researcher_sees_fingerprints_and_internal_fields(self):
        registry = _full_scenario()
        view = project_claim(registry, "claim-001", "researcher")
        self.assertEqual("code-abc", view["analysis_run"]["code_hash"])
        self.assertEqual("slice-v1-hash", view["cohort_slice_hash"])
        self.assertEqual("receipt-001", view["analysis_run_internal"]["receipt"])
        self.assertIn("stat-reviewer-01", {r["reviewer_id"] for r in view["reviews"][0]["reviewers"]})

    def test_reviewer_sees_chain_but_not_operational_fields(self):
        registry = _full_scenario()
        view = project_claim(registry, "claim-001", "reviewer")
        self.assertIn("cohort_snapshot", view)
        self.assertNotIn("analysis_run_internal", view)
        self.assertNotIn("slice_hash", jsonish_keys(view["cohort_snapshot"]))
        self.assertEqual([], view["gate"]["currently_missing"])
        self.assertIn("clinical_semantic_review", view["gate"]["required_gates"])

    def test_editor_gets_plain_evidence_packet(self):
        registry = _full_scenario()
        view = project_claim(registry, "claim-001", "science_editor")
        self.assertNotIn("analysis_run", view)
        self.assertNotIn("reviews", view)
        self.assertTrue(view["population_not_individual"])
        self.assertEqual("moderate", view["certainty"])
        self.assertIn("观察性研究，残余混杂可能存在", view["limitations"])

    def test_unknown_role_rejected(self):
        registry = _full_scenario()
        with self.assertRaises(ValueError):
            project_claim(registry, "claim-001", "marketing")


def jsonish_keys(obj):
    return set(obj.keys())


class StatementProjectionTests(unittest.TestCase):
    def test_editor_sees_publish_safety_and_erratum_flag(self):
        registry = _full_scenario()
        view = project_statement(registry, "stmt-1", "science_editor")
        self.assertTrue(view["safe_to_publish"])

        registry.record_change(
            "chg-1",
            "sample_withdrawal",
            effective_at="2026-09-26T09:00:00+08:00",
            study_id="inflammation-study",
        )
        view_after = project_statement(registry, "stmt-1", "science_editor")
        self.assertFalse(view_after["safe_to_publish"])
        self.assertEqual("erratum_attached", view_after["status"])
        self.assertTrue(view_after["claim"]["has_open_errata"])

    def test_revision_points_editor_to_latest(self):
        registry = _full_scenario()
        registry.record_change(
            "chg-1",
            "model_recomputed",
            effective_at="2026-09-26T09:00:00+08:00",
            analysis_id="cox-crp-imt",
        )
        new_run = register_default_analysis(
            registry, receipt="r2", code_hash="code-v2", result_hash="result-v2"
        )
        submit_default_claim(registry, run_ref=new_run)
        approve_claim(registry)
        registry.release_statement(
            "stmt-2",
            claim_ref="claim-001",
            audience="patient",
            wording="勘误后表述",
            supersedes="stmt-1",
        )
        old = project_statement(registry, "stmt-1", "science_editor")
        new = project_statement(registry, "stmt-2", "science_editor")
        self.assertEqual("stmt-2", old["use_latest"])
        self.assertTrue(new["safe_to_publish"])
        self.assertIsNone(new["use_latest"])


class TraceabilityTests(unittest.TestCase):
    def test_trace_covers_data_analysis_review_limitations_and_revisions(self):
        registry = _full_scenario()
        registry.record_change(
            "chg-1",
            "indicator_correction",
            effective_at="2026-09-26T09:00:00+08:00",
            study_id="inflammation-study",
        )
        new_run = register_default_analysis(
            registry, receipt="r2", code_hash="code-v2", result_hash="result-v2"
        )
        submit_default_claim(registry, run_ref=new_run)
        approve_claim(registry)
        registry.release_statement(
            "stmt-2",
            claim_ref="claim-001",
            audience="patient",
            wording="勘误后表述",
            limitations=["观察性关联，非个人诊断"],
            supersedes="stmt-1",
        )

        trace = trace_statement(registry, "stmt-1")
        self.assertEqual(["stmt-1", "stmt-2"], trace["revision_chain"])
        self.assertFalse(trace["is_current"])
        self.assertEqual(1, trace["data_version"]["cohort_version"])
        self.assertEqual("incl-2026-09", trace["data_version"]["inclusion_version"])
        self.assertEqual("slice-v1-hash", trace["data_version"]["slice_hash"])
        self.assertEqual("code-abc", trace["analysis_version"]["code_hash"])
        reviewer_ids = {
            r["reviewer_id"]
            for review in trace["review_responsibility"]
            for r in review["reviewers"]
        }
        self.assertIn("privacy-officer-01", reviewer_ids)
        self.assertIn("clinical-editor-01", reviewer_ids)
        self.assertIn("观察性研究，残余混杂可能存在", trace["claim"]["limitations"])
        self.assertIn("观察性关联，非个人诊断", trace["statement"]["limitations"])
        self.assertEqual(1, len(trace["claim"]["change_history"]))

        latest_trace = trace_statement(registry, "stmt-2")
        self.assertTrue(latest_trace["is_current"])
        self.assertEqual("code-v2", latest_trace["analysis_version"]["code_hash"])


if __name__ == "__main__":
    unittest.main()
