"""测试共用的典型场景构造。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inflammation_claims.registry import Registry
from inflammation_claims.store import EventStore

ANALYST = "analyst-01"
STAT_REVIEWER = "stat-reviewer-01"
PRIVACY_REVIEWER = "privacy-officer-01"
CLINICAL_REVIEWER = "clinical-editor-01"


def build_registry(path: str | Path | None = None) -> Registry:
    return Registry(EventStore(path))


def freeze_default_cohort(
    registry: Registry,
    study_id: str = "inflammation-study",
    *,
    slice_hash: str = "slice-v1-hash",
    inclusion_version: str = "incl-2026-09",
) -> str:
    return registry.freeze_cohort(
        study_id,
        inclusion_version=inclusion_version,
        sample_processing="EDTA 血浆，30 分钟内离心，-80℃ 保存",
        indicator_definitions={"CRP": "免疫比浊法 mg/L", "IL6": "电化学发光 pg/mL"},
        covariates=["年龄", "性别", "BMI", "吸烟", "基线用药"],
        slice_hash=slice_hash,
        imaging_measurements={"carotid_IMT": "MR-Brachial 工具，双侧均值 mm"},
    )


def register_default_analysis(
    registry: Registry,
    *,
    analysis_id: str = "cox-crp-imt",
    receipt: str = "receipt-001",
    code_hash: str = "code-abc",
    sample_scope: str = "n=12480;crp_measured",
    result_hash: str = "result-aaa",
    study_id: str = "inflammation-study",
) -> str:
    return registry.register_analysis(
        analysis_id,
        study_id=study_id,
        code_hash=code_hash,
        sample_scope=sample_scope,
        result_hash=result_hash,
        receipt=receipt,
        analysis_plan={"model": "Cox 比例风险", "exposure": "log(CRP)", "outcome": "MACE 5 年"},
        query_units=10,
    )


def submit_default_claim(
    registry: Registry,
    *,
    claim_id: str = "claim-001",
    run_ref: str | None = None,
    analyst: str = ANALYST,
    individual_risk: bool = False,
    certainty: str | None = None,
    limitations: list[str] | None = None,
) -> str:
    if run_ref is None:
        run_ref = register_default_analysis(registry)
    return registry.submit_claim(
        claim_id,
        run_ref=run_ref,
        analyst=analyst,
        effect={"HR": 1.12, "CI95": [1.05, 1.19], "p": 0.0004},
        individual_risk=individual_risk,
        certainty=certainty,
        causal_assumptions=["无未测量混杂", "CRP 测量误差非差异"],
        limitations=limitations if limitations is not None else ["观察性研究，残余混杂可能存在"],
    )


def approve_claim(
    registry: Registry,
    claim_id: str = "claim-001",
    *,
    individual_risk: bool = False,
    certainty: str = "moderate",
    analyst: str = ANALYST,
) -> None:
    reviewers = [
        {"reviewer_id": analyst, "role": "statistical", "decision": "approved"},
        {"reviewer_id": STAT_REVIEWER, "role": "statistical", "decision": "approved"},
    ]
    if individual_risk:
        reviewers += [
            {"reviewer_id": PRIVACY_REVIEWER, "role": "privacy", "decision": "approved"},
            {"reviewer_id": CLINICAL_REVIEWER, "role": "clinical", "decision": "approved"},
        ]
    registry.review_claim(
        claim_id,
        certainty=certainty,
        reviewers=reviewers,
        decision="approved",
    )


def approved_claim_scenario(
    registry: Registry, *, individual_risk: bool = False, claim_id: str = "claim-001"
) -> str:
    freeze_default_cohort(registry)
    submit_default_claim(registry, claim_id=claim_id, individual_risk=individual_risk)
    approve_claim(registry, claim_id, individual_risk=individual_risk)
    return claim_id
