# 领域约定

记录队列分析、证据强度、结论审查和公开表述之间的领域关系。

聚合对象包括`cohort_snapshot`、`analysis_run`、`research_claim`、`publication_statement`。事件类型包括`COHORT_FROZEN`、`ANALYSIS_REGISTERED`、`CLAIM_SUBMITTED`、`CLAIM_REVIEWED`、`STATEMENT_RELEASED`、`EVIDENCE_RETRACTED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `ANALYSIS_REGISTERED`：载荷还需包含 `code_hash`, `cohort_version`。
- `CLAIM_SUBMITTED`：载荷还需包含 `analysis_ref`, `analyst`。
- `CLAIM_REVIEWED`：载荷还需包含 `certainty`, `reviewers`。
- `EVIDENCE_RETRACTED`：载荷还需包含 `affected_refs`, `effective_at`。

相同事件标识的业务幂等、冲突隔离和状态推进由 `src/inflammation_claims/registry.py` 之上的登记服务负责；本仓库的契约层只定义可稳定交换的基础事实。

## 登记服务的治理规则

`Registry` 把基础事实推进为可治理的结论生命周期：

- **冻结切片**：`freeze_cohort` 固化纳入版本、样本处理、指标定义、影像测量、协变量与切片指纹；每次冻结即新的 `cohort_version`，分析运行钉住所用版本。
- **分析隔离与幂等**：`register_analysis` 以"研究+队列版本+代码指纹+样本范围+结果指纹"派生 run 标识。相同回执幂等重放；分析标识一致而任一指纹不同则隔离为独立 run。
- **分权评审**：结论经 `CLAIM_SUBMITTED` 进入评审，批准必须有非分析者本人的独立同意；涉及个体风险的结论还需隐私与临床语义两个角色批准，分析者不能用兼任角色自行放行。
- **发布快照**：`STATEMENT_RELEASED` 只能挂接已批准结论，并在事件中冻结当时的分析 run、效应估计、评审责任与限制条件。
- **变更级联**：样本撤回/指标更正只重开锚定旧切片的结论，模型重算只重开对应分析标识的结论；已发布表述不删除，原快照保留并挂接勘误，修订版通过 `supersedes` 串链。
- **配额与重算**：`QueryQuota` 原子占用受控查询额度（预留标识幂等、失败退还）；`RecomputeBatch` 按 DAG 调度，节点状态持久化，中断恢复只继承已完成节点，失败依赖阻塞下游。
- **角色视图**：`projections` 为研究者（全指纹）、审稿人（证据链与闸门，去操作字段）、科普编辑（措辞包与"群体关联非个人必然"标志，无指纹）提供不同粒度；`trace_statement` 给出任一公开说法到数据版本、审查责任、限制条件与修订链的完整血缘。
