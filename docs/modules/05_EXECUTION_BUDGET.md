# M5：共同执行状态机与总预算

> 待实现；`evolve/execution.py` 为薄控制层，`generations.py` 选择旧场景或新 task/research profile。复用现有作业、锁和账本，不引入分布式调度服务。

## 1. 状态机

状态描述实验进度；EvaluationReceipt 描述证据；InstallationReceipt 描述是否生效，三者不能合为一个 promoted 布尔值。

```text
drafted → admitted → building → screening → evaluating → evaluated
   │          │          │          │           │          ├→ rejected
   │          │          │          │           │          ├→ inconclusive
   │          │          │          │           │          └→ accepted → installing → installed
   └──────────┴──────────┴──────────┴───────────┴→ invalid                 ├→ installation_failed
                                  └→ screened_out                         └→ stale_parent
```

`screening` 可在无 screen 的冻结计划中跳过，但不能跳过必要 preflight；building 包含可选训练，prompt 候选也生成完整 artifact。任意未终结状态可因用户取消进入 cancelled；预算不足进入 inconclusive，reason=budget_exhausted。安装已提交后不能再标 cancelled，只能记录后续停止。基础设施错误按原状态的重试政策处理，次数用尽进入 inconclusive。

`installation_failed` 是本次安装终态，可新建 install operation 重试同 accepted artifact；stale_parent 不可直接重试。终态事件不可改写。每个 operation 保存 `{id, experiment_id, step, input_refs, status, attempts, output_refs, reservation_ids}`，operation_id 由 experiment+step+冻结输入确定，外部尝试有独立 attempt_id。

append journal 和当前状态快照在工作区锁下协调；快照可由 journal 重建。执行前先落 intent，完成后写结果及其引用，再推进状态；恢复对照实际 job、artifact、pointer，不凭最后一条日志猜成功。

## 2. 预算接口与层级

```python
budget.child(scope_id, limits) -> BudgetAccount
budget.reserve(operation_id, upper_bound) -> Reservation
budget.settle(reservation_id, measured, status) -> BudgetReceipt
budget.remaining() -> BudgetRemaining
```

额度含 `usd?, model_calls, input_tokens?, output_tokens?, tool_calls, training_seconds?, wall_seconds, candidates, rounds, train_jobs`；只启用可观测/可约束的维度。每次叶子操作在一个共享账本内同时校验祖先与本 scope 的 `spent + reserved + bound <= limit`，原子预留；settle 只记一次叶子支出，祖先汇总，不能重复计费。两个 research arms 的总额之和不得超过父预算，任何 retry、fallback、离线 label/分支、筛选和失败训练都在此账中。

首版任务/研究比较使用固定子预算，不把一支省下的钱自动转给另一支；公平比较要求两边同额度。候选开始前估算最少完整评测和安装探针开销，划出不可挪给训练的子额度；若不足，输出未运行提案，不启动一个只能训练不能验证的候选。子额度是分配限制，不是另一笔付费 reservation；实际调用仍逐次预留，账本只对叶子操作记一次占用/支出，不能把子额度和其叶子预留相加，也不能把粗估值当真实费用。

## 3. 真实收费与未知状态

费用来自供应商 usage、模型单价快照、训练作业/服务账。本地推理没有 API 账单仍记录 GPU/CPU 时间及估算政策，不能默认零成本。上界根据最大输入/输出 tokens、推理 tokens 上限、fallback 和供应商已知计费推导；隐藏用量不能保守界定时，该调用不可用于严格 max_usd 模式。

当前 Budget.max_usd 在费用未知时不会阻止调用；必须在 provider、serving、训练提交前补上 reserve，不能只在工具 wrapper 里检查。模型调用设置 timeout 和可支持的 token 上限；不支持可执行时间上界的 provider 记录限制，严格时间模式拒绝。取消请求可能仍产生账单，纳入 reconciliation。

响应丢失/进程崩溃时，reservation 转 unknown，保留最坏上界占用预算；优先按 request/job ID 查询。确认未执行才释放；确认已执行则结算；无法确认不自行置零。新的重试视为新外部调用、另行预留并计数。只有供应商确实支持幂等键才能复用外部请求 ID；本地 journal 的幂等不等于付费调用 exactly-once。

实测费用超过预留上界时记录 `budget_breach`，停止后续付费操作并报告偏差；不能撤销已发生费用。没有可靠费用的模式只能声明 token/调用/时间预算，不得输出“严格美元预算内”的结论。

## 4. 调度与恢复

`execute_experiment(experiment, handlers, budget)` 分发已注册 owner 的 build/evaluate，安装共用 M3。`run_worker_round` 只生成/执行 worker 候选。顶层 schedule 在 K 中预先指定 worker 轮数、research slots、patience 和连续基础设施失败上限；slot 只有显式启用 M7 才可提出 research 候选。R 可以建议停止/继续采集，不能增加轮数或自行创建无限嵌套 campaign。

research campaign 内禁用 research slots；R 固定，内部仅改 W。主线可以在若干 worker 轮后执行 research slot，但父版本冻结后必须完成该 slot 再继续主线，首版单控制器避免无意义的晋升竞争。CAS 仍保留用于重启/多进程误启动防护。

恢复规则：已知 ready artifact 直接核验后复用；训练运行中接回 job；配对任务只补计划规定的未完成 attempt；unknown 外部调用按上节处理；accepted 未安装继续安装；installed 直接进入下一轮。无权重新修改 seed、任务清单、阈值或增加 repetitions 来“恢复”。

## 5. 用例与验证

| ID | 场景 | 预期 |
|---|---|---|
| EX-01 | 两个嵌套 arm 同时预留最后额度 | 共享锁只放行合法额度；不重置父预算、不重复统计 |
| EX-02 | 模型响应丢失、控制器重启 | unknown 仍占额度；查询或新预留重试，支出不归零 |
| EX-03 | 预算只够训练、不够完整验证 | 不启动训练，保存未运行提案及原因 |
| EX-04 | provider 无限内部重试/fallback | wrapper 限制重试并逐次扣款；未知隐式 fallback 的配置拒绝严格预算 |
| EX-05 | accepted 后崩溃或 cancelled 恰逢 pointer commit | 用 M3 提交事实恢复；不把已安装写成未安装 |
| EX-06 | research arm 提出 research owner | 拒绝嵌套并记录候选缺陷；不产生第三层预算 |
| EX-07 | USD 计费不可测但调用数可测 | 严格 USD 计划不启动；已声明调用数模式可运行且费用标 unknown |
| EX-08 | 无有效提案、耐心耗尽或连续 infra 失败 | 按固定 reason 停止，保留可用 release，不安装未验证最后一代 |

测试使用 fake clock/provider/job 和故障注入，不能用长 sleep 或实际云费用验证预算。`test_jobs.py`、`test_counting.py` 的既有取消与计数行为必须保留。
