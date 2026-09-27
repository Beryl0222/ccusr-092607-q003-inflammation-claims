# 领域约定

记录队列分析、证据强度、结论审查和公开表述之间的领域关系。

聚合对象包括`cohort_snapshot`、`analysis_run`、`research_claim`、`publication_statement`。事件类型包括`COHORT_FROZEN`、`ANALYSIS_REGISTERED`、`CLAIM_REVIEWED`、`STATEMENT_RELEASED`、`EVIDENCE_RETRACTED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `ANALYSIS_REGISTERED`：载荷还需包含 `code_hash`, `cohort_version`。
- `CLAIM_REVIEWED`：载荷还需包含 `certainty`, `reviewers`。
- `EVIDENCE_RETRACTED`：载荷还需包含 `affected_refs`, `effective_at`。

相同事件标识的业务幂等、冲突隔离和状态推进由登记服务负责；契约层只定义可稳定交换的基础事实。

## 登记服务语义

`src/inflammation_claims/registry.py` 在契约之上提供领域服务（内存实现，事件经 `events()` 外发，形状符合上述契约）：

- 队列快照按 `(cohort_id, version)` 冻结，保存纳入版本、样本处理、指标定义、影像测量与协变量；同一版本重复冻结被拒绝。
- 分析回执以 `(analysis_id, version)` 登记，`version` 缺省为 1：相同回执重放返回原登记且不重复占用额度；标识一致而代码指纹、样本范围或结果不同的回执被隔离进隔离区。模型重算等合法新版本必须显式给出 `version`。
- 受控查询额度在单把锁内检查并扣减，多项分析并发消耗同一额度时原子占用、不会超发。
- 结论冻结实际使用的分析版本、分析指纹与队列版本；结论作者或分析产出者不能批准该解释；涉及个体风险的结论还需隐私与临床语义两个角色的批准意见。
- 样本撤回、指标更正、模型重算按依赖关系只重开受影响结论；已发布表述保留原快照并挂接勘误，更正按 `correction_id` 幂等。
- 批量重算按任务依赖顺序执行，中断后从未完成任务继续；重算产出以同一分析的下一版本登记，不触发隔离。
- 视图按角色裁剪：研究者看全部细节，审稿人看证据与复核链（隐去产出者身份），科普编辑只看已批准结论的公开表述、证据确定性与限制条件。
- `trace_statement` 从任一公开表述回溯数据版本、审查责任、限制条件与后续勘误。
