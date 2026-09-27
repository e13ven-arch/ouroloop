# M7：Benchmark 编排、最终锁定与结果报告

> 待实现；扩 bench.py、benchmark.py、report.py、cli.py 及示例说明。当前只有 smoke 已实现，下面的入口均须实现后才能标可运行。

## 1. 阶段与命令

保留 `ouroloop bench --config <path> --phase <phase>`；现有 `improve` 名字不改成另一套 evolve 命令。增加 research 阶段并明确状态，未知/未完成阶段继续拒绝，不能静默做 smoke。

| phase | 读取 | 执行与写入 |
|---|---|---|
| smoke | adapter/固定 split/少量 dev | 保留现有契约检查，checks-only 不调模型 |
| baseline | 固定候选模型/合法配置、dev | 合理 base、至少两家 frontier 配置的开发测量、输入审查与失败分布 |
| collect | 明确的 train groups | 当前 W 执行、CompletionProbe 离线反馈和数据审查；不读 dev/test 标签训练 |
| improve | 当前 W/R/E、train/dev | M4+M5 的固定 R worker 循环，可选择训练；形成开发 prompt_best/evolved |
| research | 已冻结 CampaignPlan、research_dev | M6 比较和安装 R，后续 worker round 验证 R1 回用 |
| evaluate | 一次冻结的 FinalizationPlan | 固定 C1～C3 版本及可选 C4 final campaigns，运行独立最终任务；不晋升或调优 |
| report | 已存 manifests/results/receipts | 纯离线聚合与回放，不调模型、不按最终分数选最优版本 |

baseline 的 frontier 选择依据实验启动日目标域的官方资料，保存来源、日期与供应商精确 model/effort/fallback；至少两家是完整 C2 交付要求。缺席一家的记录可以发布，但状态为 C2 不完整，不换弱模型充数。基础配置合理化计入优化预算，不能故意给 base 缺失必要工具说明。

## 2. 配置边界

保留已有 `[bench]` 与 `[llm]` 解析；新选项放 `[rsi]`（由 bench phase 显式读取并校验），不让当前 BenchConfig 静默接受未知键。schema 最小结构为：

```toml
# 拟建 schema 示例；引用必须由实际冻结产物替换后才能运行。
[rsi]
profile = "task"
initial_release_ref = "<bootstrap release ref>"
core_ref = "<core manifest ref>"
schedule_ref = "<bounded schedule ref>"
budget_ref = "<shared budget plan ref>"
task_plan_ref = "<development task plan ref>"
# research_plan_ref = "<research development plan ref>"
# final_plan_ref = "<finalization plan ref>"
```

复用引用避免 TOML 和代码里维护两套 gate/budget 字段。计划生成入口可先由 Python API 完成；CLI 接收引用时验证存在和权限，placeholder 直接报错。运行计划不是研究员可改文件。配置及报告只保存凭证环境变量名称，不保存 API secret。

启动前检查：上游 commit/依赖/工具及 scorer 契约、模型可调用性、输入/输出/推理 token 限制、预算可执行性、数据独立组数、策略绑定和版本权限。模型探针发生的真实支出计入活动预算。检查失败给出具体缺项，不开展一半昂贵实验。

## 3. 最终计划与可恢复封存

`FinalizationPlan` 包含 `experiment_id, core_ref, split_ref, claims, comparisons, release_refs, manifests, task_ids, repeats, statistical_plan, budget_ref, research_campaign_plan_ref?, stop_rules`。comparisons 至少声明 C1 evolved/base、C2 evolved/frontier_a/frontier_b；C3 仅在模型贡献主张启用时要求同最终 prompt 的默认决策对照，以及 prompt_best（相同则复用）。允许候选尚未优于 base，不能由已知 test 分数决定是否公开。

计划在访问最终任务内容/结果前持久化并状态置 sealed；evaluate 首次运行只能执行该内容。结果最初归 evaluator/report scope，不能通过研究 Context 或经验索引回流。可公开一份完整失败或负结果报告；不允许只发布有利子集。

重启使用 `sealed/running/completed/aborted` 记录及 attempt keys 补完同一计划。按预设 infra 重试规则替换 invalid，不能重抽合法失败。aborted 报告已有范围和原因；若已接触 test 后要改变比较版本、任务清单或停止规则，另起开发活动并标记该池已消耗，不能重命名为新独立测试。

## 4. 报告数据与结论

输出机器可读 `report.json` 和 Markdown。report 是原始引用的投影，可重算，不能手填分数。按数据协议版本分别聚合，不把旧 decision replay、snake 或脚本 fixture 与业务任务结果混在一表。

| 部分 | 必需内容 |
|---|---|
| 身份与范围 | K/W/R/E、底座/effort/fallback、上游版本、任务与家族数、split、时间、缺失配置 |
| 任务效果 | 完整通过率、partial_credit 单列、配对差值与区间、合法失败、infra 无效/补跑、实际样本覆盖 |
| 运行成本 | 所有模型/服务/工具、验证和重试成本、延迟、预算耗尽；unknown 保留 |
| 改进成本 | 采集、研究、训练、失败实验、dev/campaign/最终评测分列；总额与摊销任务量假设 |
| 改进证据 | 候选 diff、Requirement、触发/覆盖、gate、安装与回退；区分提案/接受/已生效 |
| RSI 证据 | R0→R1 来源、比较、安装、真实 R1 请求与 W2；manual/scripted/automatic 三类标识 |
| 结论状态 | 每项 C1～C4 为 supported/not_supported/inconclusive/not_run；工程验收状态另列 |

正式 supported 按预先指定比较和方向判断：质量差的区间支持提升；经济性满足非劣与成本降低；多项比较/多个主张的校正策略或探索性标识写在 statistical_plan。不得把开发 accepted 当成最终 supported，或拿局部分类准确率代替任务成功率。

C1 支持而 C3 不支持时，可以报告框架迭代有效，不能声称训练小模型是增益来源。没有训练模型也可以有 C1/C4，不应为了演示塞入一个无用模型。首要交付是端到端效果；仅在声明模型或研究策略贡献时启用必要对照，不要求展开全组件消融矩阵。

公开本地 AutomationBench 任务与官方私有榜单不同。C2 只能说明本地同协议下的相对位置，不能直接宣称刷新官方榜单。

## 5. 用例与验证

| ID | 场景 | 预期 |
|---|---|---|
| BR-01 | 当前尚未实现某 phase 或引用不存在 | 明确拒绝且不调用模型；smoke 原入口保持可用 |
| BR-02 | base 与 prompt_best 相同，模型默认对照也相同 | 复用完全相同的配置身份，避免重复费用；不同身份不合并 |
| BR-03 | 比较 frontier 时隐藏 fallback 或缺第二家 | 启动校验/报告暴露缺项；不生成完整 C2 支持结论 |
| BR-04 | 报告 cost=null、success=0、partial_credit 高 | cost 保持 unknown，完整通过率为零，不互换指标 |
| BR-05 | final 运行一半崩溃后恢复 | 只补同计划缺失/允许重试的 attempts，原结果和费用保留 |
| BR-06 | test 上看到退步后想改 prompt 或挑代 | 本 final 计划不变；新研究不得继续声称同池独立 |
| BR-07 | 从同一原始目录重新生成 report | 数值与引用一致；无额外模型请求，筛选规则可追溯 |
| BR-08 | prompt 有效、小模型无增益或 C4 未运行 | 各结论分别显示，不将整体有效夸大为所有组件有效 |

复用 `test_bench_phase.py`、`test_benchmark.py`，新增 finalization 与纯离线报告测试。真实 benchmark 运行是单独的付费实验交付，不是每次单元测试的前置要求。

## 6. 实现状态（2026-09-27）

已实现：`ouroloop/bench.py` 的七个阶段（smoke 原样保留；baseline 含 frontier 配置与缺席记录；collect 含反馈与数据审查；improve 接 M4+M5；research 接 M6；evaluate 运行密封的 `FinalizationPlan`；report 纯离线聚合）、`Activity`/`open_activity`、`freeze_task_plan`、`Improvement`（四个 handler、run_arm、context、training_options）、`startup_checks`（上游漂移、scorer 契约、模型表、每次尝试上限、预算可执行性、独立组数、release 可装配与授权）、`pricing_of`、`seal`、`research_slot`；`ouroloop/report.py`（身份与范围、任务效果、运行成本、改进成本、改进证据、RSI 证据、C1～C4 状态、Markdown 渲染、可重算写出）；`ouroloop/cli.py` 的 `--phase`、`--rounds`、`--repeats`、`--plan`（每个阶段只接受对它有意义的参数）。

阶段在调用模型之前拒绝缺失的冻结引用；`[rsi]`、`[pricing]`、`[frontier.*]` 三张表都拒绝未知键与占位符。

验收：`python -m pytest tests/test_report.py tests/test_bench_phase.py -q` → 42 passed，覆盖 BR-01～BR-08 与启动检查；`examples/automationbench/marketing.toml` 给出三张表的完整示例（引用位留空并说明由 Python API 冻结）。

已知缺口：`FinalizationPlan` 的统计计划只被记录，配对区间由 report 的 receipt 投影给出，尚无独立的最终配对统计入口；C3 需要调用方显式声明 `prompt_best` 与默认决策对照两条运行；真实 benchmark 成绩、frontier 实测与 C1～C4 结论都未运行。
