# 领域约定

记录队列分析、证据强度、结论审查和公开表述之间的领域关系。

聚合对象包括`cohort_snapshot`、`analysis_run`、`research_claim`、`publication_statement`。事件类型包括`COHORT_FROZEN`、`ANALYSIS_REGISTERED`、`CLAIM_REVIEWED`、`STATEMENT_RELEASED`、`EVIDENCE_RETRACTED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `ANALYSIS_REGISTERED`：载荷还需包含 `code_hash`, `cohort_version`。
- `CLAIM_REVIEWED`：载荷还需包含 `certainty`, `reviewers`。
- `EVIDENCE_RETRACTED`：载荷还需包含 `affected_refs`, `effective_at`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
