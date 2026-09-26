"""按角色粒度的证据投影与公开说法溯源。

三种角色看到同一条结论的不同粒度：

- ``researcher``：全部指纹、分析方案、效应估计与评审明细；
- ``reviewer``：裁决所需的证据链与评审闸门状态，看不到内部操作字段；
- ``science_editor``：面向科普的措辞包——确定性分级、限制条件、
  "群体关联不等于个人必然"标志，不含代码/切片指纹。

``trace_statement`` 给出任一公开说法的完整血缘：数据切片版本、
分析代码指纹与样本范围、评审责任、限制条件、勘误与修订链。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .registry import Registry

ROLES = ("researcher", "reviewer", "science_editor")

# 结论投影的字段白名单：粒度差异在这里显式可审。
_RUN_INTERNAL = ("receipt", "query_units")
_COHORT_INTERNAL = ("slice_hash", "frozen_event_version")
_ANALYSIS_PACKET = (
    "run_id",
    "analysis_id",
    "study_id",
    "cohort_version",
    "code_hash",
    "sample_scope",
    "result_hash",
    "analysis_plan",
)
_COHORT_PACKET = (
    "study_id",
    "cohort_version",
    "inclusion_version",
    "sample_processing",
    "indicator_definitions",
    "imaging_measurements",
    "covariates",
)


def _drop(obj: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: v for k, v in obj.items() if k not in keys}


def _analysis_packet(registry: Registry, run_ref: str, role: str) -> dict[str, Any] | None:
    run = registry.runs.get(run_ref)
    if run is None:
        return None
    packet = {k: run.get(k) for k in _ANALYSIS_PACKET}
    if role == "reviewer":
        packet = _drop(packet, _RUN_INTERNAL)
    return packet


def _cohort_packet(registry: Registry, run_ref: str, role: str) -> dict[str, Any] | None:
    run = registry.runs.get(run_ref)
    if run is None:
        return None
    cohort = registry.cohorts.get(run["study_id"])
    if cohort is None:
        return None
    packet = {k: cohort.get(k) for k in _COHORT_PACKET}
    if role == "reviewer":
        packet = _drop(packet, _COHORT_INTERNAL)
    return packet


def _gate_summary(claim: Any) -> dict[str, Any]:
    from .registry import _missing_gate_approvals  # 复用同一套闸门口径

    latest_reviewers: list[dict[str, Any]] = []
    if claim.reviews:
        latest_reviewers = list(claim.reviews[-1].get("reviewers", []))
    return {
        "analyst": claim.analyst,
        "individual_risk": claim.individual_risk,
        "required_gates": (
            ["independent_approver", "privacy_review", "clinical_semantic_review"]
            if claim.individual_risk
            else ["independent_approver"]
        ),
        "currently_missing": _missing_gate_approvals(claim, latest_reviewers),
    }


def project_claim(registry: Registry, claim_id: str, role: str) -> dict[str, Any]:
    """按角色返回结论的证据投影。"""
    if role not in ROLES:
        raise ValueError(f"未知角色：{role}")
    claim = registry.claims.get(claim_id)
    if claim is None:
        raise KeyError(f"结论不存在：{claim_id}")

    if role == "science_editor":
        return {
            "claim_id": claim.claim_id,
            "status": claim.status,
            "certainty": claim.certainty,
            "individual_risk": claim.individual_risk,
            "population_not_individual": True,
            "effect": claim.effect,
            "limitations": claim.limitations,
            "causal_assumptions": claim.causal_assumptions,
            "has_open_errata": _claim_has_open_errata(registry, claim_id),
        }

    packet: dict[str, Any] = {
        "claim_id": claim.claim_id,
        "status": claim.status,
        "certainty": claim.certainty,
        "individual_risk": claim.individual_risk,
        "effect": claim.effect,
        "limitations": claim.limitations,
        "causal_assumptions": claim.causal_assumptions,
        "gate": _gate_summary(claim),
        "reviews": claim.reviews,
        "changes": claim.changes,
        "analysis_run": _analysis_packet(registry, claim.current_run_ref, role),
        "cohort_snapshot": _cohort_packet(registry, claim.current_run_ref, role),
    }
    if role == "researcher":
        run = registry.runs.get(claim.current_run_ref or "", {})
        packet["analysis_run_internal"] = {k: run.get(k) for k in _RUN_INTERNAL if k in run}
        cohort = registry.cohorts.get(run.get("study_id", ""), {})
        packet["cohort_slice_hash"] = cohort.get("slice_hash")
    return packet


def project_statement(registry: Registry, statement_id: str, role: str) -> dict[str, Any]:
    """按角色返回对外表述视图。编辑只拿到可安全公开的字段。"""
    if role not in ROLES:
        raise ValueError(f"未知角色：{role}")
    st = registry.statements.get(statement_id)
    if st is None:
        raise KeyError(f"表述不存在：{statement_id}")

    superseded_by = _supersession_index(registry).get(statement_id)
    if role == "science_editor":
        return {
            "statement_id": st.statement_id,
            "audience": st.audience,
            "wording": st.wording,
            "limitations": st.limitations,
            "status": st.status,
            "safe_to_publish": st.status == "released" and superseded_by is None,
            "errata": [{"reason": e["reason"], "note": e["note"]} for e in st.errata],
            "use_latest": superseded_by,
            "claim": project_claim(registry, st.claim_ref, "science_editor"),
        }
    return {
        "statement_id": st.statement_id,
        "claim_ref": st.claim_ref,
        "audience": st.audience,
        "wording": st.wording,
        "limitations": st.limitations,
        "supersedes": st.supersedes,
        "superseded_by": superseded_by,
        "status": st.status,
        "errata": st.errata,
        "claim": project_claim(registry, st.claim_ref, role),
    }


def trace_statement(registry: Registry, statement_id: str) -> dict[str, Any]:
    """公开说法全血缘：数据版本、分析指纹、审查责任、限制与后续修订。"""
    st = registry.statements.get(statement_id)
    if st is None:
        raise KeyError(f"表述不存在：{statement_id}")
    claim = registry.claims.get(st.claim_ref)
    # 表述冻结的是发布当时的分析版本，而非结论当前版本。
    run_ref = st.claim_run_ref or (claim.current_run_ref if claim else None)
    run = registry.runs.get(run_ref or "") if run_ref else {}
    cohort = registry.cohorts.get(run.get("study_id", "")) if run else None
    chain = _revision_chain(registry, statement_id)
    frozen_claim = st.claim_snapshot or {}
    return {
        "statement": asdict(st),
        "revision_chain": chain,
        "is_current": chain[-1] == statement_id,
        "claim": {
            "claim_id": st.claim_ref,
            "status": claim.status if claim else None,
            "analyst": claim.analyst if claim else None,
            "individual_risk": frozen_claim.get(
                "individual_risk", claim.individual_risk if claim else None
            ),
            "certainty": frozen_claim.get("certainty", claim.certainty if claim else None),
            "effect": frozen_claim.get("effect", claim.effect if claim else None),
            "limitations": frozen_claim.get(
                "limitations", claim.limitations if claim else None
            ),
            "causal_assumptions": frozen_claim.get(
                "causal_assumptions", claim.causal_assumptions if claim else None
            ),
            # 后来发生的修订始终可见，不属于冻结内容。
            "change_history": claim.changes if claim else [],
        },
        "review_responsibility": _review_responsibility(st, claim),
        "analysis_version": {
            "run_id": run_ref,
            "analysis_id": run.get("analysis_id"),
            "code_hash": run.get("code_hash"),
            "sample_scope": run.get("sample_scope"),
            "result_hash": run.get("result_hash"),
            "analysis_plan": run.get("analysis_plan"),
        },
        "data_version": {
            "study_id": run.get("study_id"),
            "cohort_version": run.get("cohort_version"),
            "inclusion_version": cohort.get("inclusion_version") if cohort else None,
            "sample_processing": cohort.get("sample_processing") if cohort else None,
            "indicator_definitions": cohort.get("indicator_definitions") if cohort else None,
            "imaging_measurements": cohort.get("imaging_measurements") if cohort else None,
            "covariates": cohort.get("covariates") if cohort else None,
            "slice_hash": cohort.get("slice_hash") if cohort else None,
        },
    }


def _review_responsibility(st: Any, claim: Any) -> list[dict[str, Any]]:
    """优先返回发布时冻结的评审责任；历史日志缺失时回退到当前评审链。"""
    snapshot = getattr(st, "review_snapshot", None)
    if snapshot:
        return [
            {
                "at": snapshot.get("reviewed_at"),
                "decision": "approved",
                "certainty": snapshot.get("certainty"),
                "reviewers": snapshot.get("reviewers", []),
            }
        ]
    return [
        {
            "at": review["at"],
            "decision": review["decision"],
            "certainty": review["certainty"],
            "reviewers": review["reviewers"],
        }
        for review in claim.reviews
    ]


def _supersession_index(registry: Registry) -> dict[str, str]:
    """旧表述 -> 修订它的新表述。"""
    index: dict[str, str] = {}
    for sid, st in registry.statements.items():
        if st.supersedes:
            index[st.supersedes] = sid
    return index


def _revision_chain(registry: Registry, statement_id: str) -> list[str]:
    """从该表述最早的版本到最新版本的有序链。"""
    superseded_by = _supersession_index(registry)
    oldest = statement_id
    while registry.statements[oldest].supersedes:
        oldest = registry.statements[oldest].supersedes  # type: ignore[assignment]
    chain = [oldest]
    while superseded_by.get(chain[-1]):
        chain.append(superseded_by[chain[-1]])
    return chain


def _claim_has_open_errata(registry: Registry, claim_id: str) -> bool:
    return any(
        st.claim_ref == claim_id and st.status == "erratum_attached"
        for st in registry.statements.values()
    )
