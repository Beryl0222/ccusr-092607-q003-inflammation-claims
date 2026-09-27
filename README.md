# 慢性炎症研究结论登记库

记录队列分析、证据强度、结论审查和公开表述之间的领域关系，并提供登记、复核、发布与追溯的登记服务。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/inflammation_claims/contracts.py`：事件信封基础校验。
- `src/inflammation_claims/registry.py`：登记服务——队列冻结、分析幂等登记与冲突隔离、额度原子占用、复核职责分离、发布勘误、更正重开、批量重算续跑、角色视图与追溯。
- `src/inflammation_claims/cli.py`：命令行校验入口。
- `tests/`：信封、时间、版本、事件载荷与登记服务行为测试。
- `docs/domain.md`：领域对象、事件语义与登记服务语义。

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
