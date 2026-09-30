# v0.5 实现快照

> 取自代码基线 `b1cb7a7` 的模块文档。以下是原时点记录，不是 v0.6 的新验收结果；新契约以 [开发入口](../modules/README.md)为准。完整历史和真实模型运行见 [JOURNAL](../JOURNAL.md)。原测试用例定义可从该提交的 Git 历史读取。

## 截至 2026-09-28 的证据

已有工程测试历史记录为 553 passed, 1 deselected；本次文档更新没有重跑它们。GLM-5.3 在 `marketing.product_adoption` 的一次 dev 运行 success=1、partial=1.0，15 turns、29 次工具调用、229 秒。该次运行未配置计价，费用为 unknown；不能当作 100% 通过率。真实训练提升与研究策略收益尚未验证。


## 00_CONTRACTS

已实现于 `ouroloop/evolve/contracts.py`：规范 JSON 与 `type:sha256:<64hex>` 身份、分区合并（派生记录继承最严格来源）、schema 注册表（含各视图的 `required` 字段）、原 v0.5 契约表中的全部记录类型、`Objects` 对象库（原子落盘、写入即去重、读取校验哈希）、`Events` 追加日志、`copy_closure`（跨工作区复制且身份不变）。`CandidateDefect` 在此定义：候选缺陷与基础设施故障必须分开，否则坏候选会被无限重试到通过。

`RunManifest v2` 与 `TaskResult v2` 在 `ouroloop/benchmark.py` 扩展：v2 字段未设置时不进入哈希载荷，旧运行的 hash 不变；`RunManifest.v2()` 明确回答"这是否是完整 v2 运行"，缺字段的旧记录不能冒充。

验收：`python -m pytest tests/test_contracts.py -q` → 13 passed（CT-01、CT-02、CT-04 及对象库/事件日志）；CT-03 见 `tests/test_policies.py`、CT-05/CT-06 见 `tests/test_policies.py` 的 profile 与 permit 用例。

已知缺口：`ArtifactRef` 只记录摘要，不校验大文件字节；短 ID 仅用于显示，没有解析路径；旧 `Candidate` 仍由 `ExperimentProposal` 包装，未提供自动迁移工具。


## 01_POLICY_RUNTIME

已实现：`ouroloop/policies.py`（`TargetRegistration` 与原 v0.5 的七个目标、内置配置解析且拒绝未知字段、`validate_policy_set` 要求完整集合、`descriptor`/`replace_policies`、`StopDecider`、`Activity`、`BoundRuntime`、`bind_release`、`SessionBinder`、`fixed_defaults`、`build_candidate`）、`ouroloop/research/inspection.py`（`inspect`、字段覆盖率、`public` 投影）、`ouroloop/points/__init__.py` 的 `stop.business.v1` 视图与 `business_stop_point`、`ouroloop/runtime.py` 的 `propose`（route `policy`，记录 proposed/executed/executor/fallback_reason）、`ouroloop/stop.py` 的 `policy=` 接线与 override 事件、`ouroloop/agent/loop.py` 的业务观察、工具事件与 Observation/DecisionTrace 落盘。

`worker.stop` 只在 `on_finish` 生效：`per_turn` 记录观察但不询问策略，行里 `proposed` 为空、`executor` 为 `default`。模型输出非法类别抛 `CandidateDefect`（尝试作废），声明的弃权、低于阈值和服务不可用走回退并记录原因。

验收：`python -m pytest tests/test_policies.py tests/test_policy_runtime.py -q` → 25 passed，覆盖 PR-01～PR-05、PR-07、ER-03、ER-09 及 CT-03/CT-05/CT-06。PR-06 在 `tests/test_rsi_integration.py::test_it03_*` 以真实请求的 prompt hash 验证。

已知缺口：`worker.stop` 的 `spec` 载体通过"同 model_ref、新 spec"表达，没有独立的 spec kind；`retry/route/compact/tool_gate` 未注册为可改目标；校准阈值只支持 `argmax` 与预先给定的阈值来源，没有接入自动 calibration 产出；进程级隔离未实现，首版只执行声明式配置。


## 02_EVIDENCE_TRAINING

已实现：`ouroloop/evolve/feedback.py`（`FeedbackBuilder` 注册表与 `completion_snapshot.v1`，内容决定的记录 id、env/human 冲突判定、`supersedes`）、`ouroloop/evolve/build.py` 的 `build_task_dataset`（按冻结 family map 分组、一次性按家族划分 fit/validation/calibration 并记录 seed 与排序组 ID、每任务上限、排除原因、`DataManifest`）与 `check_lineage`、`ouroloop/evolve/pipeline.py` 的 `train_artifact`（提交前预留预算、失败仍结算、加载探针校验 model id 与输出类别、`attach` 查询未知远端作业、不动任何指针）、`ouroloop/research/context.py`（`make_context` 白名单投影、`task_results_summary`）。`CompletionProbe` 现在把无法评分的状态写成 label=None，于是反馈为 `unknown` 而不是静默缺失。

视图可声明 `requires`；历史观察缺该字段时拒绝重渲染并要求重新采集，不从最终状态补齐。

验收：`python -m pytest tests/test_evidence_training.py -q` → 17 passed，覆盖 DT-01～DT-08 与 Context 权限。

已知缺口：分支比较 builder（第二条信号路径）未实现，需另附重放等价性证明；calibration 组可生成但尚未接入阈值拟合；`prepare_model` 仍保留旧校准与门流程，未共用新的构建函数（旧路径的回归测试照旧通过）。


## 03_EVALUATION_RELEASE

已实现：`ouroloop/evolve/task_gate.py`（`GatePlan`/`TaskEvalPlan`、交替顺序、按任务聚合、家族层配对 bootstrap、回归率、`integrity`、`judge`、`screen`、`evaluate_task`）、`ouroloop/release.py` 的 `Releases`（bootstrap/permit/candidate_release/accepted_release、`authorize`、`register_receipt`、`install`→preflight→锁内 CAS→原子替换、`recover`、`revoke`、`last_good`、`rollback`、`resolve_profile`）、`ouroloop/evolve/task_eval.py` 的统一资源生命周期与 failure_class 分类。

开发接受与显著性分开：`uncertainty.significant` 显式写 true/false，区间含零时 reason 里说明"不是显著提升"。economy 模式下未测成本一律 inconclusive。

验收：`python -m pytest tests/test_task_gate.py -q` → 23 passed，覆盖 ER-01～ER-08、ER-10 及统计定义；ER-03/ER-09 见 `tests/test_policy_runtime.py`，崩溃恢复矩阵见 `tests/test_rsi_integration.py::test_it04_*`。

基础设施补跑按计划的 `retry_policy` 执行（`task_gate.refill` / `refill_attempts`）：只补 `failure_class == infrastructure` 的配对，原尝试与费用留在记录里，补跑结果在写入结果库时就带上 `supersedes`（只追加的结果库不能事后补字段）；agent 自己失败的任务、候选缺陷和预算用尽（`budget_exhausted`，比较记 inconclusive）都不重抽。评测处理器按尝试 id 续跑，中断后不重复付费。

已知缺口：`cache_policy` 记录在计划里但不复用 screen 结果（总是重跑）；旧 decision profile 的 GateRecord 路径未改动。


## 04_RESEARCH_LOOP

已实现：`ouroloop/research/experiment.py`（`ProposalResult`、`normalize`、三项辅助检查、`core_checks`/`admit`、`freeze`、`propose_experiment`、`retrieve`）、`ouroloop/research/loop.py` 的 `run_worker_round` 与 `run_worker_rounds`（旧 `run_round` 原样保留）、`ouroloop/research/basic.py` 的 `PolicyResearcher`（用已加载 R 的 prompt 发请求，记录 prompt hash）、`ouroloop/evolve/experience.py` 的 `experiment_record`/`summary_record`/`snapshot`/`requirement_status`。

辅助检查 block 时按原因区分 `collect` 与 `repair_runtime`；核心检查无论辅助列表如何都执行，且默认反馈构造器取注册表（空默认会让所有训练提案因错误原因作废）。`verdict` 与 `installed` 在经验记录里是两个字段。

验收：`python -m pytest tests/test_research_round.py -q` → 11 passed，覆盖 RL-01～RL-04、RL-06、RL-08 及实验冻结；RL-05 见 `tests/test_rsi_integration.py::test_it01_*`，RL-07 见 `test_it03_*`。

已知缺口：`research.diagnose` 的输出仍是字典而非持久化 `Requirement` 对象（requirement id 只落在经验记录里）；`abandon_if` 记录但未自动执行；经验检索只做标签过滤与稳定排序，没有语义检索。


## 05_EXECUTION_BUDGET

已实现于 `ouroloop/evolve/execution.py`：状态机（`STATES`/`NEXT`，终态不可改写，`installation_failed` 可重试同一 accepted artifact）、`Execution`（journal + 快照，快照可由 journal 重建，operation id 由 experiment+step+冻结输入决定）、`Budgets`/`BudgetAccount`（共享账本、子树汇总、祖先校验、`allocate` 双向限制、`settle` 的 settled/unknown/released、`budget_breach` 后停止付费操作）、`Pricing`/`check_pricing`、`MeteredProvider`（调用前预留、失败按 unknown 结算、重试有上限且逐次计费）、`Schedule` 的固定停止理由、`execute_experiment`。

`remaining()["used"]` 报告被触碰过的维度即使实测为 0：跑得比时钟分辨率快的作业不是没跑的作业。

验收：`python -m pytest tests/test_execution.py -q` → 19 passed，覆盖 EX-01～EX-04、EX-06～EX-08 与状态机；EX-05 见 `tests/test_rsi_integration.py::test_it04_*`，跨支预算见 `test_it06_*` 与 `tests/test_campaign.py`。

2026-09-28 补齐：`MeteredProvider` 已接到所有阶段——agent 的每次调用与研究员的每次调用都先预留、再按实测结算，额度用尽时尝试记为 `budget_exhausted`（不补跑，比较记 inconclusive）。一个活动只有一个预算根（`[rsi] scope_id`，额度来自冻结的 budget 对象），collect、improve、campaign（含各支与外层评测）、final 都是其子额度，不存在第二个根；此前 campaign 另开根会使预算翻倍，已修。`settle` 只在预留时真正设了上界的维度上判定越界（未声明 token 上限时，用量照记、不与虚构的 0 比较）。`normalize_usage` 统一 Anthropic 与 OpenAI 兼容接口的用量词汇，`Pricing` 增加缓存读/写单价，缓存无价则费用 unknown。`execute_experiment` 可从记录的状态续跑，`unfinished`/`resume_unfinished` 在新一轮前完成中断的实验。验收：`python -m pytest tests/test_paid_run_safety.py -q` → 18 passed。

已知缺口：`wall_seconds` 维度可声明但没有自动计时预留；供应商幂等键未使用（每次重试都是新外部调用）；进程崩溃时未结算的预留保持 open 并按上界占用（与 unknown 同样保守），尚无按 request ID 的自动对账；SDK 自带的内部重试（如 Anthropic 客户端默认 2 次）在计量层看作一次调用。


## 06_RESEARCH_CAMPAIGN

已实现：`ouroloop/research/campaign.py`（`CampaignPair` 构造时拒绝内外层重叠、`CampaignPlan`、`validate` 的血缘与独立组检查、`arm_workspace`、`ArmOutcome`、`select_w`、`run_campaign`、`judge_research` 以 independence_group 为统计单位）、`ouroloop/bench.py` 的 `_run_campaign_arm`（每支独立工作区、独立 active.json 与经验、固定子预算、research slots 关闭）、`_evaluate_outer`（同一外层任务与重复由评测者运行）、`research` 阶段与 `research_slot`（R0 提出 R1 → 冻结 campaign → 比较 → 安装 R）。

分支 W* 只经 `copy_closure` 回到评测者的对象库，主线 W 不变；`discovery` 字段区分 automatic/scripted/manual_seed 并进入 receipt。

验收：`python -m pytest tests/test_campaign.py -q` → 13 passed，覆盖 RC-01～RC-10；完整链条见 `tests/test_rsi_integration.py::test_it03_*`。

已知缺口：内层任务改进只跑到 `worker_round_limit`，没有经济模式（按达到质量阈值的时间/成本比较）；arm 隔离是目录与 scope 级，不是进程隔离，开放生成代码前必须升级；C4 的独立数据不足，fixture 的 1 个 independence group 只能出探索性结论。


## 07_BENCH_REPORT

已实现：`ouroloop/bench.py` 的七个阶段（smoke 原样保留；baseline 含 frontier 配置与缺席记录；collect 含反馈与数据审查；improve 接 M4+M5；research 接 M6；evaluate 运行密封的 `FinalizationPlan`；report 纯离线聚合）、`Activity`/`open_activity`、`freeze_task_plan`、`Improvement`（四个 handler、run_arm、context、training_options）、`startup_checks`（上游漂移、scorer 契约、模型表、每次尝试上限、预算可执行性、独立组数、release 可装配与授权）、`pricing_of`、`seal`、`research_slot`；`ouroloop/report.py`（身份与范围、任务效果、运行成本、改进成本、改进证据、RSI 证据、C1～C4 状态、Markdown 渲染、可重算写出）；`ouroloop/cli.py` 的 `--phase`、`--rounds`、`--repeats`、`--plan`（每个阶段只接受对它有意义的参数）。

阶段在调用模型之前拒绝缺失的冻结引用；`[rsi]`、`[pricing]`、`[frontier.*]` 三张表都拒绝未知键与占位符。

验收：`python -m pytest tests/test_report.py tests/test_bench_phase.py tests/test_paid_run_safety.py -q` 覆盖 BR-01～BR-08 与启动检查；`examples/automationbench/marketing.toml` 给出各表的示例。

更正（2026-09-28）：此前把 BR-05 记为已覆盖是写过头了——当时 `evaluate` 恢复时会重跑全部尝试，只是不重复写状态文件。现已按验收实现：尝试 id 由计划、臂、任务、重复与重试决定，再次运行时已记录的尝试原样保留（含费用），只运行缺失的，基础设施失败按 `stop_rules.max_infrastructure_retries` 补跑并在写入时记下替代关系。计划冻结不再只能用 Python API：`ouroloop freeze --config <path>` 读取 `[freeze.task_plan]`、`[freeze.schedule]`、`[freeze.budget]`、`[freeze.final]`，写入对象并打印要填进 `[rsi]` 的引用；同样的表冻结出同样的引用，同一 experiment_id 下的不同最终计划被拒绝。

C1～C3 的 `supported` 只能来自密封最终计划：报告用该计划的统计参数在 final 记录上做家族层配对 bootstrap，区间全大于零才写 supported，含零写 inconclusive，全小于零写 not_supported；开发记录无论多好都只写 inconclusive 并说明需要最终计划。

已知缺口：C3 需要调用方显式声明 `prompt_best` 与默认决策对照两条运行；多重比较校正只记录在 `statistical_plan` 里，没有自动施加；真实 benchmark 成绩、frontier 实测与 C1～C4 结论都未运行。


## 08_INTEGRATION_CASES

夹具：`tests/fake_upstream` 新增 `rsi` 域（读取并更新、更新后通知且要求写在请求末尾、合法 no-op、重复发送即回归，另有 dev 与 final 任务），`tests/rsi.py` 提供显式 family 与 split、脚本 worker（读请求、系统提示与工具结果）、`PhraseDecider`/`PhraseTrainer`、`ScriptedResearcher`、`ResearchPolicyProposer` 与 `collect` 辅助。marketing 域仍是两个任务，既有测试不受影响。

验收：`python -m pytest tests/test_rsi_integration.py -q` → 10 passed。

| 场景 | 状态 | 用例 |
|---|---|---|
| IT-01 不训练完成改进 | 通过 | `test_it01_*`、`test_the_installed_version_makes_the_failing_dev_task_pass` |
| IT-02 训练得到的决策器影响执行 | 通过 | `test_it02_*` |
| IT-03 真正回用更新后的研究方法 | 通过（scripted 发现） | `test_it03_*`、`tests/test_campaign.py::test_rc03_*`/`test_rc07_and_rc08_*` |
| IT-04 完整安装的崩溃恢复 | 通过 | `test_it04_*`、`tests/test_task_gate.py::test_er06_*`/`test_er08_*` |
| IT-05 阻止结果与权限污染 | 通过 | `test_it05_*`、`tests/test_evidence_training.py::test_dt05_*` |
| IT-06 严格总预算与未知收费 | 通过 | `test_it06_*`、`tests/test_execution.py` |
| IT-07 报告不能反向选冠军 | 通过 | `test_it07_*`、`tests/test_report.py::test_br07_*`/`test_br08_*` |

全仓：`python -m pytest -q` → 553 passed, 1 deselected（既有测试全部保留通过；贪吃蛇相关 15 项已随场景包删除）。

不能由此推出：夹具的数字是夹具定义的，脚本研究员的提案不是自动发现，也没有任何真实模型的业务成绩。IT-03 标 `scripted`；真实自动递归与 C1～C4 需要冻结的付费实验。
