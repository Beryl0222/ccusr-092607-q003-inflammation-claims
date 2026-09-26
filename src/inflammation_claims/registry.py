"""慢性炎症研究结论登记服务。

在事件存储之上实现治理规则：

- 队列冻结、分析注册（同分析标识异指纹隔离、回执幂等）；
- 结论提交与分权评审（分析人不得单独批准；个体风险需隐私+临床语义双审）；
- 对外表述发布；
- 样本撤回/指标更正/模型重算只重开传递依赖到的结论，已发布材料保留快照并挂接勘误；
- 全部裁决基于仅追加事件，状态可随时从事件流重建。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .errors import ReviewGateError, StateGateError, UnknownAggregate
from .store import EventStore

# ---------------------------------------------------------------- 常量

REVIEWER_ROLES = ("statistical", "privacy", "clinical")
AUDIENCES = ("paper", "patient", "drug_target")
CHANGE_KINDS = ("sample_withdrawal", "indicator_correction", "model_recomputed")

_COHORT_REQUIRED = (
    "inclusion_version",
    "sample_processing",
    "indicator_definitions",
    "covariates",
    "slice_hash",
)
_ANALYSIS_REQUIRED = (
    "analysis_id",
    "study_id",
    "code_hash",
    "cohort_version",
    "sample_scope",
    "result_hash",
    "receipt",
)
_SUBMIT_REQUIRED = ("analysis_ref", "analyst", "effect", "individual_risk")
_RELEASE_REQUIRED = ("claim_ref", "audience", "wording")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require(payload: Mapping[str, Any], fields: Sequence[str], where: str) -> None:
    missing = [f for f in fields if f not in payload]
    if missing:
        from .errors import ContractViolation

        raise ContractViolation([f"{where}.{f}: 缺少必填字段" for f in missing])


# ---------------------------------------------------------------- 读模型

@dataclass
class ClaimState:
    claim_id: str
    status: str
    analyst: str | None
    current_run_ref: str | None
    individual_risk: bool
    certainty: str | None
    limitations: list[str]
    causal_assumptions: list[str]
    effect: dict[str, Any] | None
    reviews: list[dict[str, Any]]
    changes: list[dict[str, Any]]


@dataclass
class StatementState:
    statement_id: str
    claim_ref: str
    claim_run_ref: str | None
    audience: str
    wording: str
    limitations: list[str]
    supersedes: str | None
    status: str
    errata: list[dict[str, Any]]
    review_snapshot: dict[str, Any] | None = None
    claim_snapshot: dict[str, Any] | None = None


class Registry:
    def __init__(self, store: EventStore):
        self.store = store
        self._rebuild()

    def _rebuild(self) -> None:
        self.cohorts: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.by_analysis: dict[str, list[str]] = {}
        self.by_receipt: dict[str, str] = {}
        self.claims: dict[str, ClaimState] = {}
        self.statements: dict[str, StatementState] = {}
        for event in self.store.all_events():
            self._fold(event)

    def _fold(self, event: Mapping[str, Any]) -> None:
        etype = event["event_type"]
        atype = event["aggregate_type"]
        aid = event["aggregate_id"]
        p = event["payload"]
        if etype == "COHORT_FROZEN":
            self.cohorts[aid] = dict(p, frozen_event_version=event["version"])
        elif etype == "ANALYSIS_REGISTERED":
            run = dict(p, run_id=aid)
            self.runs[aid] = run
            self.by_analysis.setdefault(p["analysis_id"], []).append(aid)
            self.by_receipt[p["receipt"]] = aid
        elif atype == "research_claim" and etype == "CLAIM_SUBMITTED":
            effect = p.get("effect")
            previous = self.claims.get(aid)
            self.claims[aid] = ClaimState(
                claim_id=aid,
                status="in_review",
                analyst=p["analyst"],
                current_run_ref=p["analysis_ref"],
                individual_risk=bool(p.get("individual_risk")),
                certainty=p.get("certainty"),
                limitations=list(p.get("limitations", [])),
                causal_assumptions=list(p.get("causal_assumptions", [])),
                effect=dict(effect) if isinstance(effect, Mapping) else None,
                # 评审与变更历史跨轮次保留，审查责任不可被重开抹掉。
                reviews=list(previous.reviews) if previous else [],
                changes=list(previous.changes) if previous else [],
            )
        elif etype == "CLAIM_REVIEWED":
            claim = self.claims[aid]
            claim.reviews.append(dict(p, at=event["occurred_at"]))
            if p.get("decision") == "approved" and _review_gates_pass(claim, p):
                claim.status = "approved"
                claim.certainty = p.get("certainty")
            elif p.get("decision") == "changes_requested":
                claim.status = "changes_requested"
        elif etype == "STATEMENT_RELEASED":
            self.statements[aid] = StatementState(
                statement_id=aid,
                claim_ref=p["claim_ref"],
                claim_run_ref=p.get("claim_run_ref"),
                audience=p["audience"],
                wording=p["wording"],
                limitations=list(p.get("limitations", [])),
                supersedes=p.get("supersedes"),
                status="released",
                errata=[],
                review_snapshot=p.get("review_snapshot"),
                claim_snapshot=p.get("claim_snapshot"),
            )
        elif etype == "EVIDENCE_RETRACTED":
            refs = [r["id"] if isinstance(r, Mapping) else r for r in p.get("affected_refs", [])]
            if atype == "research_claim" and aid in refs and aid in self.claims:
                claim = self.claims[aid]
                claim.status = "reopened"
                claim.changes.append(
                    {"change_id": p["change_id"], "reason": p["reason"], "effective_at": p["effective_at"]}
                )
            elif atype == "publication_statement" and aid in refs and aid in self.statements:
                self.statements[aid].status = "erratum_attached"
                self.statements[aid].errata.append(
                    {
                        "change_id": p["change_id"],
                        "reason": p["reason"],
                        "effective_at": p["effective_at"],
                        "note": p.get("note", ""),
                    }
                )

    # ------------------------------------------------------------- 命令

    def freeze_cohort(
        self,
        study_id: str,
        *,
        inclusion_version: str,
        sample_processing: str,
        indicator_definitions: Mapping[str, Any],
        covariates: Sequence[str],
        slice_hash: str,
        imaging_measurements: Mapping[str, Any] | None = None,
        occurred_at: str | None = None,
    ) -> str:
        """冻结一个队列数据切片。再次冻结即新的 cohort_version。"""
        next_version = self.store.version("cohort_snapshot", study_id) + 1
        payload: dict[str, Any] = {
            "study_id": study_id,
            "cohort_version": next_version,
            "inclusion_version": inclusion_version,
            "sample_processing": sample_processing,
            "indicator_definitions": dict(indicator_definitions),
            "imaging_measurements": dict(imaging_measurements or {}),
            "covariates": list(covariates),
            "slice_hash": slice_hash,
        }
        _require(payload, _COHORT_REQUIRED, "payload")
        self._append(
            "COHORT_FROZEN", "cohort_snapshot", study_id, next_version, payload, occurred_at
        )
        return study_id

    def register_analysis(
        self,
        analysis_id: str,
        *,
        study_id: str,
        code_hash: str,
        sample_scope: str,
        result_hash: str,
        receipt: str,
        analysis_plan: Mapping[str, Any] | None = None,
        query_units: int = 0,
        occurred_at: str | None = None,
    ) -> str:
        """注册一次实际执行的分析。

        - 相同回执（receipt）重放：幂等返回既有 run；
        - 分析标识相同但代码指纹/样本范围/结果不同：隔离为新的 run，
          返回以分析标识和指纹派生的新 run_ref。
        """
        if receipt in self.by_receipt:
            return self.by_receipt[receipt]
        cohort = self.cohorts.get(study_id)
        if cohort is None:
            raise UnknownAggregate(f"队列尚未冻结：{study_id}")
        fingerprint = _stable_hash(
            {
                "study_id": study_id,
                "cohort_version": cohort["cohort_version"],
                "code_hash": code_hash,
                "sample_scope": sample_scope,
                "result_hash": result_hash,
            }
        )[:12]
        run_id = f"{analysis_id}::{fingerprint}"
        for existing in self.by_analysis.get(analysis_id, []):
            run = self.runs[existing]
            if (
                run["study_id"],
                run["cohort_version"],
                run["code_hash"],
                run["sample_scope"],
                run["result_hash"],
            ) == (
                study_id,
                cohort["cohort_version"],
                code_hash,
                sample_scope,
                result_hash,
            ):
                # 完全相同的执行体：按执行体幂等（即使回执不同）。
                return existing
        payload = {
            "analysis_id": analysis_id,
            "run_id": run_id,
            "study_id": study_id,
            "cohort_version": cohort["cohort_version"],
            "code_hash": code_hash,
            "sample_scope": sample_scope,
            "result_hash": result_hash,
            "analysis_plan": dict(analysis_plan or {}),
            "receipt": receipt,
            "query_units": query_units,
        }
        _require(payload, _ANALYSIS_REQUIRED, "payload")
        next_version = self.store.version("analysis_run", run_id) + 1
        self._append(
            "ANALYSIS_REGISTERED", "analysis_run", run_id, next_version, payload, occurred_at
        )
        return run_id

    def submit_claim(
        self,
        claim_id: str,
        *,
        run_ref: str,
        analyst: str,
        effect: Mapping[str, Any],
        individual_risk: bool,
        certainty: str | None = None,
        causal_assumptions: Sequence[str] = (),
        limitations: Sequence[str] = (),
        occurred_at: str | None = None,
    ) -> str:
        """提交结论解释进入评审。重开后的再次提交开启新一轮评审。"""
        if run_ref not in self.runs:
            raise UnknownAggregate(f"分析运行不存在：{run_ref}")
        if claim_id in self.claims and self.claims[claim_id].status not in (
            "reopened",
            "changes_requested",
        ):
            raise StateGateError(
                "claim_not_reopenable",
                f"结论 {claim_id} 当前状态 {self.claims[claim_id].status}，不能再次提交",
            )
        payload = {
            "analysis_ref": run_ref,
            "analyst": analyst,
            "effect": dict(effect),
            "individual_risk": individual_risk,
            "certainty": certainty,
            "causal_assumptions": list(causal_assumptions),
            "limitations": list(limitations),
        }
        _require(payload, _SUBMIT_REQUIRED, "payload")
        next_version = self.store.version("research_claim", claim_id) + 1
        self._append(
            "CLAIM_SUBMITTED", "research_claim", claim_id, next_version, payload, occurred_at
        )
        return claim_id

    def review_claim(
        self,
        claim_id: str,
        *,
        certainty: str,
        reviewers: Sequence[Mapping[str, Any]],
        decision: str,
        note: str = "",
        occurred_at: str | None = None,
    ) -> None:
        """登记一次评审结论；批准必须通过分权与专项复核闸门。"""
        claim = self.claims.get(claim_id)
        if claim is None:
            raise UnknownAggregate(f"结论不存在：{claim_id}")
        if claim.status not in ("in_review", "changes_requested"):
            raise StateGateError(
                "claim_not_in_review", f"结论 {claim_id} 当前状态 {claim.status}，不接受评审"
            )
        roster = [dict(r) for r in reviewers]
        _validate_roster(roster)
        payload = {
            "certainty": certainty,
            "reviewers": roster,
            "decision": decision,
            "note": note,
        }
        if decision == "approved":
            missing = _missing_gate_approvals(claim, roster)
            if missing:
                raise ReviewGateError(missing, f"结论 {claim_id} 未通过评审闸门：{', '.join(missing)}")
        elif decision not in ("changes_requested",):
            raise StateGateError("bad_decision", f"未知评审决定：{decision}")
        next_version = self.store.version("research_claim", claim_id) + 1
        self._append(
            "CLAIM_REVIEWED", "research_claim", claim_id, next_version, payload, occurred_at
        )

    def release_statement(
        self,
        statement_id: str,
        *,
        claim_ref: str,
        audience: str,
        wording: str,
        limitations: Sequence[str] = (),
        supersedes: str | None = None,
        occurred_at: str | None = None,
    ) -> str:
        """发布对外表述。只能挂接在已批准结论上；修订版通过 supersedes 串链。"""
        claim = self.claims.get(claim_ref)
        if claim is None:
            raise UnknownAggregate(f"结论不存在：{claim_ref}")
        if claim.status != "approved":
            raise StateGateError(
                "claim_not_approved",
                f"结论 {claim_ref} 当前状态 {claim.status}，不能对外发布",
            )
        if audience not in AUDIENCES:
            raise StateGateError("bad_audience", f"受众必须是 {AUDIENCES} 之一")
        if supersedes is not None and supersedes not in self.statements:
            raise UnknownAggregate(f"被修订表述不存在：{supersedes}")
        approving_review = next(
            (r for r in reversed(claim.reviews) if r.get("decision") == "approved"), None
        )
        payload = {
            "claim_ref": claim_ref,
            "claim_run_ref": claim.current_run_ref,
            "audience": audience,
            "wording": wording,
            "limitations": list(limitations),
            "supersedes": supersedes,
            # 审查责任随表述冻结：旧说法永远指向批准它的那次评审。
            "review_snapshot": {
                "analyst": claim.analyst,
                "certainty": approving_review["certainty"] if approving_review else claim.certainty,
                "reviewed_at": approving_review["at"] if approving_review else None,
                "reviewers": approving_review["reviewers"] if approving_review else [],
            },
            # 结论语义同样冻结，旧表述不随后来重算而改写。
            "claim_snapshot": {
                "certainty": claim.certainty,
                "individual_risk": claim.individual_risk,
                "effect": claim.effect,
                "limitations": claim.limitations,
                "causal_assumptions": claim.causal_assumptions,
            },
        }
        _require(payload, _RELEASE_REQUIRED, "payload")
        self._append(
            "STATEMENT_RELEASED", "publication_statement", statement_id, 1, payload, occurred_at
        )
        return statement_id

    def record_change(
        self,
        change_id: str,
        reason: str,
        *,
        effective_at: str,
        study_id: str | None = None,
        new_cohort_version: int | None = None,
        analysis_id: str | None = None,
        note: str = "",
        occurred_at: str | None = None,
    ) -> list[str]:
        """登记样本撤回/指标更正/模型重算，只重开传递依赖到的结论。

        返回受影响并重开的 claim_id 列表。已发布表述不删除，自动挂接勘误。
        """
        if reason not in CHANGE_KINDS:
            raise StateGateError("bad_change_reason", f"变更类型必须是 {CHANGE_KINDS} 之一")
        affected_claims = self._dependent_claims(
            reason, study_id=study_id, new_cohort_version=new_cohort_version, analysis_id=analysis_id
        )
        affected_statements = [
            sid
            for sid, st in self.statements.items()
            if st.claim_ref in affected_claims and st.status == "released"
        ]
        refs = [{"kind": "research_claim", "id": cid} for cid in affected_claims]
        refs += [{"kind": "publication_statement", "id": sid} for sid in affected_statements]
        ts = occurred_at or _now()
        for cid in affected_claims:
            version = self.store.version("research_claim", cid) + 1
            self._append(
                "EVIDENCE_RETRACTED",
                "research_claim",
                cid,
                version,
                {
                    "change_id": change_id,
                    "reason": reason,
                    "affected_refs": refs,
                    "effective_at": effective_at,
                    "note": note,
                },
                ts,
            )
        for sid in affected_statements:
            version = self.store.version("publication_statement", sid) + 1
            self._append(
                "EVIDENCE_RETRACTED",
                "publication_statement",
                sid,
                version,
                {
                    "change_id": change_id,
                    "reason": reason,
                    "affected_refs": refs,
                    "effective_at": effective_at,
                    "note": note,
                },
                ts,
            )
        return affected_claims

    # ----------------------------------------------------------- 内部

    def _dependent_claims(
        self,
        reason: str,
        *,
        study_id: str | None,
        new_cohort_version: int | None,
        analysis_id: str | None,
    ) -> list[str]:
        out: set[str] = set()
        for cid, claim in self.claims.items():
            if claim.status == "reopened" or claim.current_run_ref is None:
                continue
            run = self.runs.get(claim.current_run_ref)
            if run is None:
                continue
            if reason in ("sample_withdrawal", "indicator_correction"):
                if run["study_id"] == study_id and (
                    new_cohort_version is None or run["cohort_version"] < new_cohort_version
                ):
                    out.add(cid)
            elif reason == "model_recomputed":
                if run["analysis_id"] == analysis_id:
                    out.add(cid)
        return sorted(out)

    def _append(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        version: int,
        payload: Mapping[str, Any],
        occurred_at: str | None,
    ) -> None:
        event_id = f"{event_type}-{aggregate_id}-v{version}"
        event = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at or _now(),
            "version": version,
            "payload": dict(payload),
        }
        self.store.append(event, expected_version=version - 1)
        self._fold(event)


# ---------------------------------------------------------------- 评审闸门


def _validate_roster(roster: list[dict[str, Any]]) -> None:
    from .errors import ContractViolation

    issues: list[str] = []
    for i, r in enumerate(roster):
        if not r.get("reviewer_id"):
            issues.append(f"payload.reviewers[{i}].reviewer_id: 缺少评审人")
        if r.get("role") not in REVIEWER_ROLES:
            issues.append(f"payload.reviewers[{i}].role: 必须是 {REVIEWER_ROLES} 之一")
        if r.get("decision") not in ("approved", "changes_requested"):
            issues.append(f"payload.reviewers[{i}].decision: 必须是 approved/changes_requested")
    if issues:
        raise ContractViolation(issues)


def _review_gates_pass(claim: ClaimState, review_payload: Mapping[str, Any]) -> bool:
    return not _missing_gate_approvals(claim, review_payload["reviewers"])


def _missing_gate_approvals(
    claim: ClaimState, roster: Sequence[Mapping[str, Any]]
) -> list[str]:
    approvers = [r for r in roster if r.get("decision") == "approved"]
    independent = [r for r in approvers if r.get("reviewer_id") != claim.analyst]
    missing: list[str] = []
    # 分权：产出解释的统计分析者不能单独批准。
    if not independent:
        missing.append("independent_approver")
    if claim.individual_risk:
        roles = {r.get("role") for r in independent}
        if "privacy" not in roles:
            missing.append("privacy_review")
        if "clinical" not in roles:
            missing.append("clinical_semantic_review")
    return missing


def _stable_hash(obj: Any) -> str:
    import hashlib

    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
