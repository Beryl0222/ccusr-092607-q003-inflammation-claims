"""慢性炎症研究结论登记服务。

在事件契约之上提供登记、复核、发布与追溯的领域服务：

- 队列快照按版本冻结，保存纳入标准、样本处理、指标定义、影像测量与协变量；
- 分析回执幂等登记：相同回执重放返回原登记，标识一致而代码指纹、
  样本范围或结果不同的回执被隔离；
- 多项分析并发消耗同一受控查询额度时，在单把锁内检查并扣减，保证原子占用；
- 结论冻结实际使用的数据切片与分析版本；统计分析者不能单独批准自己的解释，
  涉及个体风险的结论还需隐私与临床语义复核；
- 样本撤回、指标更正或模型重算只重开依赖它们的结论，已发布表述保留原快照
  并挂接勘误；
- 批量重算按依赖顺序执行，中断后从未完成项继续；
- 按研究者、审稿人、科普编辑返回不同粒度的证据视图；
- 任一公开表述可追溯到数据版本、审查责任、限制条件与后续修订。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

REVIEW_ROLES = ("statistical", "privacy", "clinical_semantic")
CORRECTION_KINDS = ("sample_withdrawal", "indicator_correction", "model_recalculation")
VIEW_ROLES = ("researcher", "reviewer", "editor")
RECEIPT_REQUIRED = (
    "analysis_id",
    "analyst_id",
    "cohort_id",
    "cohort_version",
    "code_hash",
    "sample_scope_hash",
    "result_hash",
    "plan",
    "effect_estimates",
    "indicator_ids",
)


class RegistryError(Exception):
    """登记服务拒绝操作时抛出，code 为稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class CohortSnapshot:
    """队列快照：纳入版本、样本处理、指标定义、影像测量与协变量的冻结切片。"""

    cohort_id: str
    version: int
    inclusion: Mapping[str, Any]
    sample_processing: Mapping[str, Any]
    indicator_definitions: Mapping[str, Any]
    imaging_measurements: Mapping[str, Any]
    covariates: tuple[str, ...]
    sample_scope_hash: str
    frozen_at: str


@dataclass(frozen=True)
class AnalysisRun:
    """一次登记的分析运行，指纹覆盖代码、队列、样本范围与结果。"""

    analysis_id: str
    version: int
    analyst_id: str
    cohort_id: str
    cohort_version: int
    code_hash: str
    sample_scope_hash: str
    result_hash: str
    plan: Mapping[str, Any]
    effect_estimates: Mapping[str, Any]
    indicator_ids: tuple[str, ...]
    fingerprint: str
    registered_at: str


@dataclass(frozen=True)
class QuarantineEntry:
    """被隔离的冲突回执：标识一致但指纹与已登记运行不同。"""

    analysis_id: str
    version: int
    reason: str
    registered_fingerprint: str
    rejected_fingerprint: str
    receipt: Mapping[str, Any]
    quarantined_at: str


@dataclass
class Claim:
    """研究结论：冻结实际使用的数据切片与分析版本。"""

    claim_id: str
    title: str
    author_id: str
    analysis_id: str
    analysis_version: int
    analysis_fingerprint: str
    cohort_id: str
    cohort_version: int
    causal_hypothesis: str
    certainty: str
    limitations: tuple[str, ...]
    involves_individual_risk: bool
    status: str
    version: int
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Review:
    """一条复核意见，归属于结论的某个版本。"""

    claim_id: str
    claim_version: int
    reviewer_id: str
    role: str
    decision: str
    comment: str
    reviewed_at: str


@dataclass
class Statement:
    """对外表述：发布时冻结结论快照，更正后保留快照并挂接勘误。"""

    statement_id: str
    claim_id: str
    audience: str
    text: str
    editor_id: str
    released_at: str
    snapshot: dict[str, Any]
    status: str
    errata: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Quota:
    """受控查询额度。"""

    quota_id: str
    total: int
    used: int = 0

    @property
    def remaining(self) -> int:
        return self.total - self.used


@dataclass(frozen=True)
class CorrectionResult:
    """一次更正的结果：被重开的结论与被挂勘误的表述。"""

    correction_id: str
    kind: str
    reopened_claims: tuple[str, ...]
    corrected_statements: tuple[str, ...]


@dataclass(frozen=True)
class RecalcTask:
    task_id: str
    analysis_id: str
    depends_on: tuple[str, ...] = ()


@dataclass
class RecalcBatch:
    """批量重算：记录已完成任务，中断后从未完成依赖继续。"""

    batch_id: str
    tasks: dict[str, RecalcTask]
    done: set[str] = field(default_factory=set)

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(sorted(t for t in self.tasks if t not in self.done))


def _fingerprint(parts: Mapping[str, Any]) -> str:
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ClaimRegistry:
    """慢性炎症研究结论登记服务（内存实现，事件可外发校验）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cohorts: dict[tuple[str, int], CohortSnapshot] = {}
        self._analyses: dict[tuple[str, int], AnalysisRun] = {}
        self._quarantine: list[QuarantineEntry] = []
        self._claims: dict[str, Claim] = {}
        self._reviews: list[Review] = []
        self._statements: dict[str, Statement] = {}
        self._corrections: dict[str, CorrectionResult] = {}
        self._quotas: dict[str, Quota] = {}
        self._batches: dict[str, RecalcBatch] = {}
        self._events: list[dict[str, Any]] = []
        self._event_seq = 0
        self._event_versions: dict[tuple[str, str], int] = {}

    # --- 队列快照 ---

    def freeze_cohort(
        self,
        *,
        cohort_id: str,
        version: int,
        inclusion: Mapping[str, Any],
        sample_processing: Mapping[str, Any],
        indicator_definitions: Mapping[str, Any],
        imaging_measurements: Mapping[str, Any],
        covariates: Iterable[str],
        sample_scope_hash: str,
    ) -> CohortSnapshot:
        """冻结一个队列版本；同一版本重复冻结被拒绝。"""
        with self._lock:
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise RegistryError("version_invalid", "队列版本必须是正整数")
            key = (cohort_id, version)
            if key in self._cohorts:
                raise RegistryError("cohort_version_frozen", f"队列 {cohort_id}@{version} 已冻结")
            snapshot = CohortSnapshot(
                cohort_id=cohort_id,
                version=version,
                inclusion=dict(inclusion),
                sample_processing=dict(sample_processing),
                indicator_definitions=dict(indicator_definitions),
                imaging_measurements=dict(imaging_measurements),
                covariates=tuple(covariates),
                sample_scope_hash=sample_scope_hash,
                frozen_at=self._now(),
            )
            self._cohorts[key] = snapshot
            self._emit(
                "COHORT_FROZEN",
                "cohort_snapshot",
                f"{cohort_id}@{version}",
                {"cohort_id": cohort_id, "version": version, "sample_scope_hash": sample_scope_hash},
            )
            return snapshot

    # --- 受控查询额度 ---

    def define_quota(self, quota_id: str, total: int) -> Quota:
        with self._lock:
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise RegistryError("quota_invalid", "额度总量必须是非负整数")
            if quota_id in self._quotas:
                raise RegistryError("quota_exists", f"额度 {quota_id} 已定义")
            quota = Quota(quota_id=quota_id, total=total)
            self._quotas[quota_id] = quota
            return replace(quota)

    def acquire_quota(self, quota_id: str, amount: int) -> Quota:
        """原子占用额度：检查与扣减在同一把锁内完成，并发不会超发。"""
        with self._lock:
            self._consume_quota(quota_id, amount)
            return replace(self._quotas[quota_id])

    def quota_status(self, quota_id: str) -> Quota:
        with self._lock:
            return replace(self._quota(quota_id))

    def _quota(self, quota_id: str) -> Quota:
        quota = self._quotas.get(quota_id)
        if quota is None:
            raise RegistryError("quota_unknown", f"额度 {quota_id} 未定义")
        return quota

    def _consume_quota(self, quota_id: str, amount: int) -> None:
        quota = self._quota(quota_id)
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise RegistryError("quota_amount_invalid", "占用量必须是非负整数")
        if quota.used + amount > quota.total:
            raise RegistryError("quota_exhausted", f"额度 {quota_id} 不足：剩余 {quota.remaining}")
        quota.used += amount

    # --- 分析登记 ---

    def register_analysis(
        self,
        receipt: Mapping[str, Any],
        *,
        quota_id: str | None = None,
        quota_cost: int = 0,
    ) -> AnalysisRun:
        """登记分析回执。

        回执标识为 (analysis_id, version)，version 缺省为 1。相同回执重放
        返回原登记（不重复占用额度、不重复登记）；标识一致而代码指纹、样本
        范围或结果不同的回执被隔离并抛出冲突。需要登记同一分析的新版本时
        （如模型重算）必须显式给出 version。
        """
        with self._lock:
            if not isinstance(receipt, Mapping):
                raise RegistryError("receipt_invalid", "分析回执必须是映射")
            missing = [key for key in RECEIPT_REQUIRED if key not in receipt]
            if missing:
                raise RegistryError("receipt_invalid", f"分析回执缺少字段: {', '.join(missing)}")
            analysis_id = str(receipt["analysis_id"])
            version = receipt.get("version")
            if version is None:
                version = 1
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise RegistryError("version_invalid", "分析版本必须是正整数")
            fingerprint = _fingerprint(
                {key: receipt[key] for key in ("code_hash", "cohort_id", "cohort_version", "sample_scope_hash", "result_hash")}
            )
            key = (analysis_id, version)
            existing = self._analyses.get(key)
            if existing is not None:
                if existing.fingerprint == fingerprint:
                    return existing
                self._quarantine.append(
                    QuarantineEntry(
                        analysis_id=analysis_id,
                        version=version,
                        reason="标识一致但代码指纹、样本范围或结果不同",
                        registered_fingerprint=existing.fingerprint,
                        rejected_fingerprint=fingerprint,
                        receipt=dict(receipt),
                        quarantined_at=self._now(),
                    )
                )
                raise RegistryError(
                    "analysis_conflict_quarantined",
                    f"分析 {analysis_id}@{version} 与已登记运行冲突，已隔离",
                )
            cohort_key = (receipt["cohort_id"], receipt["cohort_version"])
            if cohort_key not in self._cohorts:
                raise RegistryError("cohort_unknown", f"队列 {cohort_key[0]}@{cohort_key[1]} 未冻结")
            if quota_id is not None:
                self._consume_quota(quota_id, quota_cost)
            run = AnalysisRun(
                analysis_id=analysis_id,
                version=version,
                analyst_id=str(receipt["analyst_id"]),
                cohort_id=str(receipt["cohort_id"]),
                cohort_version=int(receipt["cohort_version"]),
                code_hash=str(receipt["code_hash"]),
                sample_scope_hash=str(receipt["sample_scope_hash"]),
                result_hash=str(receipt["result_hash"]),
                plan=dict(receipt["plan"]),
                effect_estimates=dict(receipt["effect_estimates"]),
                indicator_ids=tuple(receipt["indicator_ids"]),
                fingerprint=fingerprint,
                registered_at=self._now(),
            )
            self._analyses[key] = run
            self._emit(
                "ANALYSIS_REGISTERED",
                "analysis_run",
                f"{analysis_id}@{version}",
                {
                    "analysis_id": analysis_id,
                    "version": version,
                    "analyst_id": run.analyst_id,
                    "code_hash": run.code_hash,
                    "cohort_version": run.cohort_version,
                    "sample_scope_hash": run.sample_scope_hash,
                    "result_hash": run.result_hash,
                    "fingerprint": fingerprint,
                },
            )
            return run

    def get_analysis(self, analysis_id: str, version: int | None = None) -> AnalysisRun:
        with self._lock:
            return self._get_analysis(analysis_id, version)

    def _get_analysis(self, analysis_id: str, version: int | None) -> AnalysisRun:
        if version is None:
            versions = [v for (a, v) in self._analyses if a == analysis_id]
            if not versions:
                raise RegistryError("analysis_unknown", f"分析 {analysis_id} 未登记")
            version = max(versions)
        run = self._analyses.get((analysis_id, version))
        if run is None:
            raise RegistryError("analysis_unknown", f"分析 {analysis_id}@{version} 未登记")
        return run

    def _next_analysis_version(self, analysis_id: str) -> int:
        return max((v for (a, v) in self._analyses if a == analysis_id), default=0) + 1

    def quarantined(self) -> tuple[QuarantineEntry, ...]:
        with self._lock:
            return tuple(self._quarantine)

    # --- 结论生命周期 ---

    def submit_claim(
        self,
        *,
        claim_id: str,
        analysis_id: str,
        author_id: str,
        title: str,
        causal_hypothesis: str,
        certainty: str,
        limitations: Iterable[str] = (),
        involves_individual_risk: bool = False,
        analysis_version: int | None = None,
    ) -> Claim:
        """提交结论并冻结实际使用的数据切片与分析版本。"""
        with self._lock:
            if claim_id in self._claims:
                raise RegistryError("claim_exists", f"结论 {claim_id} 已存在")
            run = self._get_analysis(analysis_id, analysis_version)
            claim = Claim(
                claim_id=claim_id,
                title=title,
                author_id=author_id,
                analysis_id=run.analysis_id,
                analysis_version=run.version,
                analysis_fingerprint=run.fingerprint,
                cohort_id=run.cohort_id,
                cohort_version=run.cohort_version,
                causal_hypothesis=causal_hypothesis,
                certainty=certainty,
                limitations=tuple(limitations),
                involves_individual_risk=bool(involves_individual_risk),
                status="in_review",
                version=1,
            )
            claim.history.append(self._trace("submitted", f"冻结分析 {run.analysis_id}@{run.version}"))
            self._claims[claim_id] = claim
            return claim

    def record_review(
        self,
        *,
        claim_id: str,
        reviewer_id: str,
        role: str,
        decision: str,
        comment: str = "",
    ) -> Review:
        """记录复核意见；产出者对自己的解释作出的批准直接被拒绝。"""
        with self._lock:
            claim = self._claim(claim_id)
            if claim.status != "in_review":
                raise RegistryError("claim_state_invalid", f"结论 {claim_id} 当前状态 {claim.status} 不能复核")
            if role not in REVIEW_ROLES:
                raise RegistryError("role_unknown", f"复核角色 {role} 未登记")
            if decision not in ("approve", "reject"):
                raise RegistryError("decision_unknown", f"复核结论 {decision} 未登记")
            if decision == "approve" and reviewer_id in self._producers(claim):
                raise RegistryError("self_approval_forbidden", "统计分析者不能批准自己产出的解释")
            review = Review(
                claim_id=claim_id,
                claim_version=claim.version,
                reviewer_id=reviewer_id,
                role=role,
                decision=decision,
                comment=comment,
                reviewed_at=self._now(),
            )
            self._reviews.append(review)
            self._emit(
                "CLAIM_REVIEWED",
                "research_claim",
                claim_id,
                {
                    "claim_id": claim_id,
                    "reviewer_id": reviewer_id,
                    "role": role,
                    "decision": decision,
                    "certainty": claim.certainty,
                    "reviewers": sorted({r.reviewer_id for r in self._current_reviews(claim)}),
                },
            )
            return review

    def approve_claim(self, *, claim_id: str, approver_id: str) -> Claim:
        """批准结论：批准人不能是产出者，且必需角色的批准意见齐备。"""
        with self._lock:
            claim = self._claim(claim_id)
            if claim.status != "in_review":
                raise RegistryError("claim_state_invalid", f"结论 {claim_id} 当前状态 {claim.status} 不能批准")
            producers = self._producers(claim)
            if approver_id in producers:
                raise RegistryError("self_approval_forbidden", "统计分析者不能单独批准自己产出的解释")
            reviews = self._current_reviews(claim)
            if any(r.decision == "reject" for r in reviews):
                raise RegistryError("review_rejected", "存在否决意见，需修改后重新提交")
            approved_roles = {
                r.role for r in reviews if r.decision == "approve" and r.reviewer_id not in producers
            }
            required = {"statistical"}
            if claim.involves_individual_risk:
                required |= {"privacy", "clinical_semantic"}
            missing = sorted(required - approved_roles)
            if missing:
                raise RegistryError("review_missing", f"缺少复核: {', '.join(missing)}")
            claim.status = "approved"
            claim.history.append(self._trace("approved", f"批准人 {approver_id}"))
            self._emit(
                "CLAIM_REVIEWED",
                "research_claim",
                claim_id,
                {
                    "claim_id": claim_id,
                    "reviewer_id": approver_id,
                    "role": "approval",
                    "decision": "approve",
                    "certainty": claim.certainty,
                    "reviewers": sorted({r.reviewer_id for r in self._current_reviews(claim)}),
                },
            )
            return claim

    def resubmit_claim(self, *, claim_id: str, analysis_version: int | None = None) -> Claim:
        """重开后的结论重新冻结（默认最新的）分析版本并回到复核流程。"""
        with self._lock:
            claim = self._claim(claim_id)
            if claim.status != "reopened":
                raise RegistryError("claim_state_invalid", f"结论 {claim_id} 当前状态 {claim.status} 不能重新提交")
            run = self._get_analysis(claim.analysis_id, analysis_version)
            claim.analysis_version = run.version
            claim.analysis_fingerprint = run.fingerprint
            claim.cohort_id = run.cohort_id
            claim.cohort_version = run.cohort_version
            claim.version += 1
            claim.status = "in_review"
            claim.history.append(self._trace("resubmitted", f"重新冻结分析 {run.analysis_id}@{run.version}"))
            return claim

    def get_claim(self, claim_id: str) -> Claim:
        with self._lock:
            return self._claim(claim_id)

    def _claim(self, claim_id: str) -> Claim:
        claim = self._claims.get(claim_id)
        if claim is None:
            raise RegistryError("claim_unknown", f"结论 {claim_id} 未登记")
        return claim

    def _producers(self, claim: Claim) -> set[str]:
        producers = {claim.author_id}
        run = self._analyses.get((claim.analysis_id, claim.analysis_version))
        if run is not None:
            producers.add(run.analyst_id)
        return producers

    def _current_reviews(self, claim: Claim) -> list[Review]:
        return [r for r in self._reviews if r.claim_id == claim.claim_id and r.claim_version == claim.version]

    # --- 对外表述 ---

    def release_statement(
        self,
        *,
        statement_id: str,
        claim_id: str,
        audience: str,
        text: str,
        editor_id: str,
    ) -> Statement:
        """发布对外表述，冻结当时的结论快照。"""
        with self._lock:
            claim = self._claim(claim_id)
            if claim.status != "approved":
                raise RegistryError("claim_state_invalid", f"结论 {claim_id} 未批准，不能发布")
            if statement_id in self._statements:
                raise RegistryError("statement_exists", f"表述 {statement_id} 已发布")
            snapshot = {
                "claim_id": claim.claim_id,
                "claim_version": claim.version,
                "certainty": claim.certainty,
                "causal_hypothesis": claim.causal_hypothesis,
                "limitations": list(claim.limitations),
                "cohort_id": claim.cohort_id,
                "cohort_version": claim.cohort_version,
                "analysis_id": claim.analysis_id,
                "analysis_version": claim.analysis_version,
                "analysis_fingerprint": claim.analysis_fingerprint,
            }
            statement = Statement(
                statement_id=statement_id,
                claim_id=claim_id,
                audience=audience,
                text=text,
                editor_id=editor_id,
                released_at=self._now(),
                snapshot=snapshot,
                status="published",
            )
            self._statements[statement_id] = statement
            self._emit(
                "STATEMENT_RELEASED",
                "publication_statement",
                statement_id,
                {
                    "statement_id": statement_id,
                    "claim_id": claim_id,
                    "audience": audience,
                    "editor_id": editor_id,
                },
            )
            return statement

    def get_statement(self, statement_id: str) -> Statement:
        with self._lock:
            statement = self._statements.get(statement_id)
            if statement is None:
                raise RegistryError("statement_unknown", f"表述 {statement_id} 未登记")
            return statement

    # --- 更正与重开 ---

    def apply_correction(
        self,
        *,
        correction_id: str,
        kind: str,
        effective_at: str,
        reason: str,
        cohort_id: str | None = None,
        cohort_version: int | None = None,
        indicator_ids: Iterable[str] = (),
        analysis_ids: Iterable[str] = (),
    ) -> CorrectionResult:
        """样本撤回、指标更正或模型重算：只重开依赖它们的结论。

        已发布表述保留原快照并挂接勘误；同一 correction_id 重复应用幂等。
        """
        with self._lock:
            known = self._corrections.get(correction_id)
            if known is not None:
                return known
            if kind not in CORRECTION_KINDS:
                raise RegistryError("correction_kind_unknown", f"更正类型 {kind} 未登记")
            affected = self._affected_runs(
                kind,
                cohort_id=cohort_id,
                cohort_version=cohort_version,
                indicator_ids=tuple(indicator_ids),
                analysis_ids=tuple(analysis_ids),
            )
            affected_keys = {(run.analysis_id, run.version) for run in affected}
            reopened: list[str] = []
            corrected: list[str] = []
            for claim in sorted(self._claims.values(), key=lambda c: c.claim_id):
                if (claim.analysis_id, claim.analysis_version) not in affected_keys:
                    continue
                if claim.status != "reopened":
                    claim.status = "reopened"
                    claim.history.append(self._trace("reopened", f"{kind}: {reason}"))
                    reopened.append(claim.claim_id)
                statement_ids = []
                for statement in self._claim_statements(claim.claim_id):
                    statement.errata.append(
                        {
                            "correction_id": correction_id,
                            "kind": kind,
                            "reason": reason,
                            "effective_at": effective_at,
                            "attached_at": self._now(),
                        }
                    )
                    statement.status = "corrected"
                    corrected.append(statement.statement_id)
                    statement_ids.append(statement.statement_id)
                self._emit(
                    "EVIDENCE_RETRACTED",
                    "research_claim",
                    claim.claim_id,
                    {
                        "correction_id": correction_id,
                        "kind": kind,
                        "reason": reason,
                        "affected_refs": [claim.claim_id, *statement_ids],
                        "effective_at": effective_at,
                    },
                )
            result = CorrectionResult(
                correction_id=correction_id,
                kind=kind,
                reopened_claims=tuple(reopened),
                corrected_statements=tuple(sorted(corrected)),
            )
            self._corrections[correction_id] = result
            return result

    def _affected_runs(
        self,
        kind: str,
        *,
        cohort_id: str | None,
        cohort_version: int | None,
        indicator_ids: tuple[str, ...],
        analysis_ids: tuple[str, ...],
    ) -> list[AnalysisRun]:
        runs = list(self._analyses.values())
        if kind == "sample_withdrawal":
            if cohort_id is None:
                raise RegistryError("correction_target_missing", "样本撤回必须指定队列")
            return [
                r
                for r in runs
                if r.cohort_id == cohort_id and (cohort_version is None or r.cohort_version == cohort_version)
            ]
        if kind == "indicator_correction":
            if cohort_id is None or not indicator_ids:
                raise RegistryError("correction_target_missing", "指标更正必须指定队列与指标")
            wanted = set(indicator_ids)
            return [
                r
                for r in runs
                if r.cohort_id == cohort_id
                and (cohort_version is None or r.cohort_version == cohort_version)
                and wanted & set(r.indicator_ids)
            ]
        if not analysis_ids:
            raise RegistryError("correction_target_missing", "模型重算必须指定分析标识")
        wanted_ids = set(analysis_ids)
        return [r for r in runs if r.analysis_id in wanted_ids]

    # --- 批量重算 ---

    def create_recalculation(self, *, batch_id: str, tasks: Iterable[Mapping[str, Any]]) -> RecalcBatch:
        """创建批量重算；任务按 depends_on 声明依赖。"""
        with self._lock:
            if batch_id in self._batches:
                raise RegistryError("batch_exists", f"重算批次 {batch_id} 已存在")
            built: dict[str, RecalcTask] = {}
            for item in tasks:
                task = RecalcTask(
                    task_id=str(item["task_id"]),
                    analysis_id=str(item["analysis_id"]),
                    depends_on=tuple(item.get("depends_on", ())),
                )
                if task.task_id in built:
                    raise RegistryError("task_duplicate", f"任务 {task.task_id} 重复")
                built[task.task_id] = task
            unknown = sorted({dep for task in built.values() for dep in task.depends_on} - set(built))
            if unknown:
                raise RegistryError("task_dependency_unknown", f"未知依赖任务: {', '.join(unknown)}")
            batch = RecalcBatch(batch_id=batch_id, tasks=built)
            self._batches[batch_id] = batch
            return batch

    def run_recalculation(
        self,
        batch_id: str,
        execute: Callable[[RecalcTask], Mapping[str, Any]],
        *,
        max_tasks: int | None = None,
    ) -> tuple[str, ...]:
        """按依赖顺序执行重算；中断后再次调用从未完成任务继续。

        execute 返回新回执，登记为同一分析的下一版本，不触发隔离。
        回调抛出的异常向上传播，已完成任务保留在批次中。
        """
        completed: list[str] = []
        while True:
            with self._lock:
                batch = self._batches.get(batch_id)
                if batch is None:
                    raise RegistryError("batch_unknown", f"重算批次 {batch_id} 不存在")
                if max_tasks is not None and len(completed) >= max_tasks:
                    return tuple(completed)
                ready = sorted(
                    (
                        task
                        for task in batch.tasks.values()
                        if task.task_id not in batch.done and set(task.depends_on) <= batch.done
                    ),
                    key=lambda task: task.task_id,
                )
                if not ready:
                    if batch.pending:
                        raise RegistryError(
                            "task_dependency_unresolved",
                            f"依赖无法满足: {', '.join(batch.pending)}",
                        )
                    return tuple(completed)
                task = ready[0]
            receipt = dict(execute(task))
            receipt.setdefault("analysis_id", task.analysis_id)
            with self._lock:
                if "version" not in receipt:
                    receipt["version"] = self._next_analysis_version(task.analysis_id)
                self.register_analysis(receipt)
                batch.done.add(task.task_id)
            completed.append(task.task_id)

    def batch_status(self, batch_id: str) -> RecalcBatch:
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise RegistryError("batch_unknown", f"重算批次 {batch_id} 不存在")
            return batch

    # --- 角色视图与追溯 ---

    def view_claim(self, claim_id: str, *, role: str) -> dict[str, Any]:
        """按研究者、审稿人、科普编辑的权限返回适当粒度的证据。"""
        with self._lock:
            if role not in VIEW_ROLES:
                raise RegistryError("role_unknown", f"视图角色 {role} 未登记")
            claim = self._claim(claim_id)
            if role == "researcher":
                return self._researcher_view(claim)
            if role == "reviewer":
                return self._reviewer_view(claim)
            return self._editor_view(claim)

    def _researcher_view(self, claim: Claim) -> dict[str, Any]:
        run = self._analyses.get((claim.analysis_id, claim.analysis_version))
        cohort = self._cohorts.get((claim.cohort_id, claim.cohort_version))
        return {
            "role": "researcher",
            "claim_id": claim.claim_id,
            "title": claim.title,
            "status": claim.status,
            "version": claim.version,
            "author_id": claim.author_id,
            "certainty": claim.certainty,
            "causal_hypothesis": claim.causal_hypothesis,
            "limitations": list(claim.limitations),
            "involves_individual_risk": claim.involves_individual_risk,
            "analysis": self._analysis_dict(run) if run else None,
            "cohort": self._cohort_dict(cohort) if cohort else None,
            "reviews": [self._review_dict(r) for r in self._current_reviews(claim)],
            "statements": [self._statement_dict(s) for s in self._claim_statements(claim.claim_id)],
            "history": [dict(h) for h in claim.history],
        }

    def _reviewer_view(self, claim: Claim) -> dict[str, Any]:
        run = self._analyses.get((claim.analysis_id, claim.analysis_version))
        analysis = None
        if run is not None:
            analysis = {
                "analysis_id": run.analysis_id,
                "version": run.version,
                "code_hash": run.code_hash,
                "plan": dict(run.plan),
                "effect_estimates": dict(run.effect_estimates),
                "indicator_ids": list(run.indicator_ids),
                "cohort_id": run.cohort_id,
                "cohort_version": run.cohort_version,
            }
        return {
            "role": "reviewer",
            "claim_id": claim.claim_id,
            "title": claim.title,
            "status": claim.status,
            "version": claim.version,
            "certainty": claim.certainty,
            "causal_hypothesis": claim.causal_hypothesis,
            "limitations": list(claim.limitations),
            "involves_individual_risk": claim.involves_individual_risk,
            "analysis": analysis,
            "cohort": {"cohort_id": claim.cohort_id, "version": claim.cohort_version},
            "reviews": [self._review_dict(r) for r in self._current_reviews(claim)],
        }

    def _editor_view(self, claim: Claim) -> dict[str, Any]:
        statements = self._claim_statements(claim.claim_id)
        if claim.status != "approved" and not statements:
            raise RegistryError("claim_not_public", f"结论 {claim.claim_id} 未批准，科普编辑不可见")
        return {
            "role": "editor",
            "claim_id": claim.claim_id,
            "title": claim.title,
            "status": claim.status,
            "certainty": claim.certainty,
            "causal_hypothesis": claim.causal_hypothesis,
            "limitations": list(claim.limitations),
            "statements": [self._statement_dict(s) for s in statements],
        }

    def trace_statement(self, statement_id: str) -> dict[str, Any]:
        """从公开表述回溯数据版本、审查责任、限制条件与后续修订。"""
        with self._lock:
            statement = self.get_statement(statement_id)
            claim = self._claim(statement.claim_id)
            snapshot = statement.snapshot
            return {
                "statement_id": statement.statement_id,
                "text": statement.text,
                "audience": statement.audience,
                "status": statement.status,
                "released_at": statement.released_at,
                "editor_id": statement.editor_id,
                "claim": {
                    "claim_id": claim.claim_id,
                    "title": claim.title,
                    "status": claim.status,
                    "current_version": claim.version,
                },
                "data_version": {
                    "cohort_id": snapshot["cohort_id"],
                    "cohort_version": snapshot["cohort_version"],
                    "analysis_id": snapshot["analysis_id"],
                    "analysis_version": snapshot["analysis_version"],
                    "analysis_fingerprint": snapshot["analysis_fingerprint"],
                },
                "review_trail": [
                    self._review_dict(r)
                    for r in self._reviews
                    if r.claim_id == claim.claim_id and r.claim_version == snapshot["claim_version"]
                ],
                "limitations": list(snapshot["limitations"]),
                "certainty": snapshot["certainty"],
                "errata": [dict(e) for e in statement.errata],
            }

    # --- 事件 ---

    def events(self) -> list[dict[str, Any]]:
        """已发生事件，形状符合领域事件契约，可交由 validate_event 校验。"""
        with self._lock:
            return [dict(event, payload=dict(event["payload"])) for event in self._events]

    def _emit(self, event_type: str, aggregate_type: str, aggregate_id: str, payload: Mapping[str, Any]) -> None:
        key = (aggregate_type, aggregate_id)
        self._event_versions[key] = self._event_versions.get(key, 0) + 1
        self._event_seq += 1
        self._events.append(
            {
                "event_id": f"evt-{self._event_seq:06d}",
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "occurred_at": self._now(),
                "version": self._event_versions[key],
                "payload": dict(payload),
            }
        )

    # --- 视图辅助 ---

    def _claim_statements(self, claim_id: str) -> list[Statement]:
        return sorted(
            (s for s in self._statements.values() if s.claim_id == claim_id),
            key=lambda s: s.statement_id,
        )

    @staticmethod
    def _analysis_dict(run: AnalysisRun) -> dict[str, Any]:
        return {
            "analysis_id": run.analysis_id,
            "version": run.version,
            "analyst_id": run.analyst_id,
            "code_hash": run.code_hash,
            "sample_scope_hash": run.sample_scope_hash,
            "result_hash": run.result_hash,
            "fingerprint": run.fingerprint,
            "plan": dict(run.plan),
            "effect_estimates": dict(run.effect_estimates),
            "indicator_ids": list(run.indicator_ids),
            "cohort_id": run.cohort_id,
            "cohort_version": run.cohort_version,
            "registered_at": run.registered_at,
        }

    @staticmethod
    def _cohort_dict(cohort: CohortSnapshot) -> dict[str, Any]:
        return {
            "cohort_id": cohort.cohort_id,
            "version": cohort.version,
            "inclusion": dict(cohort.inclusion),
            "sample_processing": dict(cohort.sample_processing),
            "indicator_definitions": dict(cohort.indicator_definitions),
            "imaging_measurements": dict(cohort.imaging_measurements),
            "covariates": list(cohort.covariates),
            "sample_scope_hash": cohort.sample_scope_hash,
            "frozen_at": cohort.frozen_at,
        }

    @staticmethod
    def _review_dict(review: Review) -> dict[str, Any]:
        return {
            "reviewer_id": review.reviewer_id,
            "role": review.role,
            "decision": review.decision,
            "comment": review.comment,
            "reviewed_at": review.reviewed_at,
        }

    @staticmethod
    def _statement_dict(statement: Statement) -> dict[str, Any]:
        return {
            "statement_id": statement.statement_id,
            "audience": statement.audience,
            "text": statement.text,
            "status": statement.status,
            "errata": [dict(e) for e in statement.errata],
        }

    def _trace(self, action: str, note: str) -> dict[str, Any]:
        return {"action": action, "at": self._now(), "note": note}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
