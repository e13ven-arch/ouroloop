# M3：配对任务评测、裁决与完整版本发布

> 待实现；扩 `evolve/task_eval.py`、`registry.py`、`release.py`，新增薄 `evolve/task_gate.py`。依赖 M0/M1 和 M5 预算；不改变旧 decision gate 的统计或授权语义。

## 1. 固定评测计划

`TaskEvalPlan` 在第一次候选运行前冻结：`profile=task, candidate_ref, control_ref, parent_release, core_ref, split_ref, task_ids, repeats, order_seed, per_attempt_budget, retry_policy, screen_ids, gate_plan, price_ref?, cache_policy`。task_ids 必须为当前 scope 的 dev；最终评测使用另一个只报告、不安装的计划。相同任务/重复构成 pair，各 arm 从独立 reset 开始，执行顺序按 seed 交替/随机；供应商不支持 seed 时如实记录随机性。

`GatePlan` 的必填字段：`mode(quality|economy), min_groups, min_gain, max_quality_loss, max_runtime_cost_ratio?, max_regression_rate, confidence_level, bootstrap_seed, bootstrap_replicates, protected_requirements, missing_cost_policy`。阈值由实验发起者在基线前确定，框架不替研究员事后调宽；没有配置则拒绝正式执行。quality 模式允许成本约束为空，但未知成本必须显式允许；economy 模式必须有可测成本。默认统计展示 95% 配对区间，具体参数入计划。

开发门使用以下透明政策，不声称消除了自适应搜索偏差：

- 完整性、目标绑定和受保护约束先检查；缺配对、样本组不足、预算不足 => inconclusive；候选结构/执行缺陷 => invalid。
- quality：候选完整通过率增益达到 min_gain，任务回归率不超过 max_regression_rate，满足预定成本及保护约束 => accepted，否则 rejected。区间同时报告；**开发接受不等于显著提升**。min_gain 必须 > 0，避免把并列随机抖动当改进。
- economy：质量差的区间下界不低于 `-max_quality_loss`，成本差区间支持降低且满足 max_runtime_cost_ratio，保护约束不退步 => accepted；区间无法判定 => inconclusive；明确违约 => rejected。
- 改动只解决接线缺陷但没有效果证据，可以保留为未晋升构建产物；不能把结构修复凭据伪造成效果凭据。人工修改基线形成新实验身份。

按任务先平均 repeats，再计算任务平均成功率。任务回归率为候选任务均值低于控制的任务比例；同计划始终使用该定义。置信区间在 family 层配对 bootstrap，抽中一家族时带上其所有任务/重复，以任务权重重算差值，不能将每条轨迹当独立样本。家族数不足则标记区间不可靠，按 min_groups 处理。adaptive dev 区间仅描述这次比较，正式收益用锁定版本的独立最终计划。

## 2. 构建、筛选与完整执行

1. 校验完整 policy set、数据血缘、预算、artifact 可读性和模型加载身份；按冻结模板补齐实际 artifact ref 后签发限定 plan/scope 的 EvaluationPermit，以此装配隔离候选。permit 不具有安装权限。
2. 固定 screen_ids 同时覆盖目标失败和原成功任务；其选择只依赖先前 dev 事实，并记录选择方法。先检查候选目标可达、输入存在、没有非法动作。
3. screen 只淘汰确定的配置/执行缺陷或触发预定明显回归规则的候选。没触发不一律淘汰；需要该条件的策略未触发时记录机制证据不足。小样本均值不可直接晋升。
4. 剩余预算足够才运行完整 dev 计划。screen 结果只在全部身份、任务、预算、重复和顺序条件相同且 cache_policy 允许时复用，否则另跑，不能只补失败题。
5. 完整结果生成不可变 EvaluationReceipt。receipt 记录全部 attempts 与费用，主指标使用按协议选定的有效 pair；原失败/无效记录不得删除。

不要共享两个 arm 的可变 runtime、session、世界或上下文缓存。供应商缓存命中可以保留，但记录真实计费与缓存策略；测时延应声明冷/热条件。

## 3. 错误与资源清理

现有 run_attempt 的 episode/make_agent 在 try 外、score 在主异常处理外，run_tasks 的 close 也非 finally；需重构统一资源生命周期，保留老函数签名的兼容包装。

| 分类 | 示例 | 结果与重试 |
|---|---|---|
| 有效任务结果 | 未完成、错误合法工具参数、拒答、超步骤/预算 | 官方 scorer 对留下的状态评分；保留失败或成功，不重抽直到成功 |
| 候选缺陷 | 自有策略输出非法动作、schema 不符、确定性配置错误 | receipt invalid，保留故障与费用；即使还能评分也不能排除坏题后晋升 |
| 基础设施故障 | provider 暂时不可达、环境初始化失败、存储错误 | 记录 invalid attempt，按冻结 retry_policy 有限补跑；未补齐 pair => inconclusive |
| 评测器/契约错误 | 官方 scorer 崩溃、工具版本漂移 | 停止本评测，inconclusive；修 K 后新计划，不能让研究员修改评分 |

仅可信适配器/控制器产生的类型化错误可分类为基础设施；未知异常默认停止并记录待诊断，不由研究员一句“环境坏了”触发重跑。有限重试均扣总预算。异常不能阻止 finally 关闭 episode；cleanup 错误另存，不能覆盖首个错误。付费响应丢失按 M5 的 unknown 处理。

## 4. 授权、安装、恢复与回滚

新 task/research profile 使用 SystemRelease 和 `rsi/active.json`。模型卡片是 artifact 元信息，不是动作授权；旧 `models/champion.json` 不被此路径读取。accepted receipt 必须由固定 evaluator 的已完成操作登记，并与 subject、父版本、K、完整 plan 逐项匹配，不能接受研究员提供的自填 JSON。

```text
accepted receipt
  → 生成完整 SystemRelease（只替换对应 owner，另一方精确沿用父版本）
  → 隔离装配 / 服务加载与身份探针 / 已注册目标检查
  → 持久化 install intent 与 preflight 结果
  → 锁内检查 expected_parent + active revision + 未撤销状态
  → 原子替换 active.json（唯一提交点）
  → 写 installed receipt 和事件
```

preflight 失败：installation_failed，旧指针不动，可以在 artifact 和计划不变时重试安装，无需重跑评测。父版本已变：stale_parent，不能自动 rebase 或把另一 owner 的新版本偷偷合并；重新基于现状构造评测计划。评测中的分支只能更新自己的 active 文件，不能更新主线。

崩溃恢复读取 operation intent 与 active.last_operation_id：已提交则补齐 installed 事件；未提交则继续 preflight/CAS；两个 worker 重放同 operation 只得一个提交。若后续已有新版本，查不可变安装链确认历史提交，不能把 active 改回旧 intent。引用对象/receipt 在 pointer 之前 durable 写入，禁止指向半写 artifact。

撤销顺序：先登记 release/policy revocation，再移动主线指针到最近已授权、已装载、未撤销且 K 兼容的完整祖先；无可用祖先则进入明确 defaults release，不能拼出从未评测的 W/R。依赖被撤销 artifact 的后代同样不可用。旧 session 的动作按 M1 撤销规则处理。撤销事件不可抹掉，后续复用需新版本及新评测。

## 5. 用例与验证

| ID | 场景 | 预期 |
|---|---|---|
| ER-01 | 候选 screen 2/2，全 dev 退步 | 不在 screen 安装；按完整结果 rejected |
| ER-02 | 一侧一次 provider 故障后补跑成功 | 原 attempt 与费用保留，固定 pair 填齐；没有选择性删失败 |
| ER-03 | 候选在特定题返回非法 action | invalid，不能作为 infra 排除该题取得高分 |
| ER-04 | quality 小幅正增益且达门槛、区间含零 | 可以开发 accepted，但报告不得写已证明显著收益 |
| ER-05 | economy 成本缺失或非劣性区间过宽 | inconclusive，不把未知成本置零或“不显著”当非劣 |
| ER-06 | accepted 后模型服务加载错误 | installation_failed，active 不变；修服务后可重试同安装 |
| ER-07 | W、R 两个候选都基于同一 parent 并发安装 | 一个 CAS 成功，另一个 stale_parent；不混装 |
| ER-08 | pointer 写完进程崩溃；恢复同 operation | 补 receipt，不重复提交、不回滚已提交状态 |
| ER-09 | scorer/close/make_agent 任一抛错 | 产生对应记录、资源关闭、原错误保留，不丢失预算 |
| ER-10 | 撤销模型；旧 session 和新 session 同时运行 | 旧 session 固定默认，新 session 合法祖先；无幽灵授权 |

复用 `test_benchmark.py`、`test_automationbench.py`、`test_gate_bypass.py`、`test_gate_record.py`、`test_roles.py`；新增 task gate 和 SystemRelease 故障注入测试。旧 GateRecord 不能被新 receipt 测试替代。

## 6. 实现状态（2026-09-27）

已实现：`ouroloop/evolve/task_gate.py`（`GatePlan`/`TaskEvalPlan`、交替顺序、按任务聚合、家族层配对 bootstrap、回归率、`integrity`、`judge`、`screen`、`evaluate_task`）、`ouroloop/release.py` 的 `Releases`（bootstrap/permit/candidate_release/accepted_release、`authorize`、`register_receipt`、`install`→preflight→锁内 CAS→原子替换、`recover`、`revoke`、`last_good`、`rollback`、`resolve_profile`）、`ouroloop/evolve/task_eval.py` 的统一资源生命周期与 failure_class 分类。

开发接受与显著性分开：`uncertainty.significant` 显式写 true/false，区间含零时 reason 里说明"不是显著提升"。economy 模式下未测成本一律 inconclusive。

验收：`python -m pytest tests/test_task_gate.py -q` → 23 passed，覆盖 ER-01～ER-08、ER-10 及统计定义；ER-03/ER-09 见 `tests/test_policy_runtime.py`，崩溃恢复矩阵见 `tests/test_rsi_integration.py::test_it04_*`。

基础设施补跑按计划的 `retry_policy` 执行（`task_gate.refill`）：只补 `failure_class == infrastructure` 的配对，原尝试与费用留在记录里，补跑结果写 `supersedes`；agent 自己失败的任务和候选缺陷都不重抽。

已知缺口：`cache_policy` 记录在计划里但不复用 screen 结果（总是重跑）；旧 decision profile 的 GateRecord 路径未改动。
