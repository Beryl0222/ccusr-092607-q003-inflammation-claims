# 慢性炎症研究结论登记库

保存队列研究结论的**数据切片、分析版本、审查责任、证据确定性、因果假设与对外表述**，
让"炎症增加心脏风险"这类说法在论文、患者科普和药物靶点讨论中各守各的证据强度，
不把统计关联写成个人必然结果。

## 目录

- `contracts/domain.schema.json`：事件信封与载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/inflammation_claims/`
  - `contracts.py`：基础契约校验（不改写输入）。
  - `store.py`：仅追加事件存储，事件标识幂等与聚合版本并发控制，可选 JSONL 落盘。
  - `registry.py`：登记服务——冻结切片、分析注册/隔离、分权评审、发布快照、变更级联。
  - `quota.py`：受控查询额度的原子占用（幂等预留、失败退还）。
  - `recompute.py`：DAG 批量重算，断点从未完成依赖继续，失败依赖阻塞下游。
  - `projections.py`：研究者/审稿人/科普编辑三种粒度视图与公开说法全血缘溯源。
- `tests/`：契约、存储、评审闸门、级联、配额、断点续算、角色视图与端到端链路测试。
- `docs/domain.md`：领域对象、事件与治理规则语义。

## 治理规则一览

| 需求 | 落地位置 |
| --- | --- |
| 冻结实际使用的数据切片与分析版本 | `freeze_cohort` + `ANALYSIS_REGISTERED.cohort_version` |
| 分析人不能单独批准自己的解释 | `registry._missing_gate_approvals` |
| 个体风险需隐私 + 临床语义复核 | 同上，按 `individual_risk` 加闸 |
| 撤回/更正/重算只重开依赖结论 | `Registry.record_change` 依赖传播 |
| 已发布材料保留原快照并挂勘误 | 表述状态 `erratum_attached`，原文不改写 |
| 相同回执幂等重放 | `by_receipt` / `EventStore` 事件标识 |
| 同分析异指纹必须隔离 | run 标识含代码/样本/结果指纹 |
| 并发消耗同一额度原子占用 | `QueryQuota.reserve` |
| 批量重算中断后从未完成依赖继续 | `RecomputeBatch` 进度持久化 |
| 按角色返回证据粒度 | `projections.project_claim/project_statement` |
| 公开说法可追到版本、责任、限制、修订 | `projections.trace_statement` |

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m inflammation_claims.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。
