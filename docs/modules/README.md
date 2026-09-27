# RSI 模块详细设计与开发入口

> 2026-09-27，配套 v0.5。本文档集原为**待实现契约**；M0～M7 与集成验收现已实现，各文档末尾的“实现状态”一节写明落点、验收命令、真实测试结果与已知缺口。先读 [架构审查](../RSI_REVIEW.md)和 [总设计](../DESIGN.md)。
>
> 工程闭环已通过离线夹具验收（`python -m pytest -q` → 553 passed），**但没有任何真实模型的业务成绩**：夹具的数字由夹具定义，脚本研究员的提案不是自动发现，C1～C4 全部未运行。

目标交付：同一个受预算约束的循环能改进任务策略 W，也能提出、验证并安装研究策略 R；后续执行实际使用更新版本。先完成工程闭环，再用独立实验判断 C1～C4 是否成立。完整性不意味着无限自修改，不要求每轮训练。

## 文档与模块责任

| 模块 | 详细设计 | 独占的新文件；主要改造点 | 依赖 |
|---|---|---|---|
| M0 公共契约 | [00_CONTRACTS](00_CONTRACTS.md) | `evolve/contracts.py`；公共类型、schema 与兼容读取 | 无 |
| M1 策略与实际运行 | [01_POLICY_RUNTIME](01_POLICY_RUNTIME.md) | `policies.py`、`research/inspection.py`；`agent/`、`runtime.py`、`stop.py` | M0 |
| M2 证据、分组与训练 | [02_EVIDENCE_TRAINING](02_EVIDENCE_TRAINING.md) | `research/context.py`；`ledger.py`、`adapters/automationbench.py`、`evolve/build.py`、`evolve/pipeline.py` | M0，M1 输入契约 |
| M3 完整任务评测与发布 | [03_EVALUATION_RELEASE](03_EVALUATION_RELEASE.md) | `evolve/task_gate.py`；`evolve/task_eval.py`、`evolve/registry.py`、`release.py` | M0、M1、M5 预算接口 |
| M4 研究与经验循环 | [04_RESEARCH_LOOP](04_RESEARCH_LOOP.md) | `research/experiment.py`；`research/agent.py`、`basic.py`、`loop.py`、`evolve/experience.py` | M0～M3、M5 |
| M5 执行状态与总预算 | [05_EXECUTION_BUDGET](05_EXECUTION_BUDGET.md) | `evolve/execution.py`；provider 调用边界 | M0 |
| M6 研究策略递归评测 | [06_RESEARCH_CAMPAIGN](06_RESEARCH_CAMPAIGN.md) | `research/campaign.py`；复用 M3～M5 的入口 | M0～M5 |
| M7 Benchmark 与报告 | [07_BENCH_REPORT](07_BENCH_REPORT.md) | `bench.py`、`benchmark.py`、`report.py`、`cli.py`、`examples/automationbench/` | 各模块按阶段接入 |
| 集成验收 | [08_INTEGRATION_CASES](08_INTEGRATION_CASES.md) | 测试与可复现夹具；不另建生产子系统 | 全部 |

表内代码路径相对 `ouroloop/`，唯 `examples/automationbench/` 相对仓库根。模块 M0～M7 是开发分工，**不是**路线图的里程碑 M1～M7；里程碑映射如下。

## 实施顺序与共享文件

按此顺序实现完毕；每一步的验收命令见对应文档的“实现状态”。

1. M0 冻结公共类型；M5 先提供本地预算账和状态存储。离线验证，不调用付费模型。
2. M1、M2 完成真实输入与数据契约；M7 接 `baseline`/`collect`。对应路线图 M2～M3。
3. M2 完成无副作用的训练构建；M3 完成任务评测和完整发布。对应路线图 M4。
4. M4 接通固定 R 的任务循环；M7 接 `improve`。对应路线图 M5。
5. M6 接研究候选、隔离 campaign、R1 回用；M7 接 `research`。对应路线图 M7。
6. 冻结所有待报告版本与比较协议后，由 M7 执行独立 `evaluate`/`report`。对应路线图 M6 及 C4 最终验证，可统一安排，避免过早用掉测试集。

M0 是共享类型唯一所有者。M1 修改 `runtime.py` 的策略 pin/调用；M3 只提供 release 读取、授权与安装 API。M2 修改 `pipeline.py` 的训练抽取；M3 不复制训练逻辑。M5 独占调度整合；M4 提供一轮函数。M7 独占 CLI 与 RunManifest 序列化；其他模块通过 M0 约定字段提出改动。多人同时实现时先合并公共接口，按以上所有权处理共享文件，不能各建一套同名记录。

## 开发交付规则

- 每个模块先核对当前代码和本文列出的差距；保留既有 CLI、decision profile 与旧账本读取。
- 只实现已注册目标、声明式配置和现有可信执行器。生成 Python、任意补丁、额外专家池、向量库均不属于首版必要范围。
- 每个公开接口的错误分类、持久化和重启行为与成功路径一起交付。示例中的假结果只进测试夹具，不能进业务报告。
- 每份模块文档的用例 ID 都是验收条目；覆盖关键路径即可，不为数据类字段机械增加测试。集成用例负责跨模块故障。
- 未完成的后续模块使用明确的 stub 并拒绝生产执行；不得静默退到不同评测器或伪造 accepted。
- 设计更改先修改公共契约及受影响文档。实现完成后更新状态和真实验证记录，不提前把“设计支持”写成“已经证明”。

默认部署为一个本地 Python 控制进程、已有作业/模型服务和文件账本；模块是代码边界，不是新增微服务。
