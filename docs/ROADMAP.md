# v0.6 实施路线图

> 2026-09-30 范围修订，代码基线 `b1cb7a7`。首版交付受限 harness 代码演化、应用发布与递归回用；第二版完善训练固化。仅调整相关规格，已有实现、旧 CLI 与历史证据保持原义。详细交接见 [模块目录](modules/README.md)。

## 1. 现有基础与缺口

已有 M0～M7：注册策略、真实业务观察、反馈/训练、完整任务配对评测、W/R 发布、单预算根和恢复、研究一轮及 R0/R1 campaign、七个 bench phase。代码基线 `b1cb7a7` 的离线测试为 554 passed, 1 deselected（[实现快照](history/V0_5_IMPLEMENTATION.md)与 JOURNAL 中的 553 为此前记录）。

已有真实 GLM-5.3 在 `marketing.product_adoption` 一次 dev 运行：该任务共 14 条断言，其中 6 条初始即成立且结束时仍成立，被评分器排除、不计入分母；8 条计分断言全部通过（task_completed_correctly=1，partial 1.0）；15 turns、29 次工具调用、229 秒，第 15 轮请求的历史为 27,132 token；当时未配置价格，cost unknown。该任务保持 dev，不得重新标为未见 test。这只证明一次运行可用，没有证明训练或递归收益。[运行日志](JOURNAL.md)

代码基线 `b1cb7a7` 时首版待补的内容如下（阶段 A～E 的工程链路此后已实现，部分验收项仍缺，见 §2 末与 §3）：GLM-5.3 actor pin、能力包内容/manifest 身份、包内代码提案与构建、固定隔离 worker/工具 broker、工具与 hook 实际装配、技能/经验加载、回归/发布/导出，以及自动循环和 R campaign 的接线。R 诊断输入同属首版待补：当前 `task_results_summary` 只给逐题 success/partial/终止/失败类，不含断言明细和轨迹（`ouroloop/research/context.py:116-146`）；首版为每个失败的 train/fit 尝试提供失败摘要与断言明细；dev 只给汇总，final 不进入 R 上下文，dev/final 都不给断言明细和轨迹，这改变了 v0.5 研究员看不到任何断言明细的做法（`context.py:120` 的 docstring）；诊断经逐题笔记→汇总产出至多一个 patch 或结构化 next_action。M3 的 `run_tasks` 把逐条断言结果另存 `bench/<domain>/grading.jsonl`，不进 results.jsonl；M7 的结果存储按冻结 split 过滤读取，默认只返回 train。包草稿冻结前做草稿检查：含代码时 package_check → isolated_probe → package_test，只含 skills/memory 时只做结构/路径/manifest 检查；失败只把检查输出回传 R，在 preset 固定的次数内修订，通过后才冻结进 Experiment。第二版保留 generation 训练绑定、自动教学适配、review 前置、SFT/Tinker 及训练恢复设计。两版的新增能力和收益均未测得。

## 2. 交付顺序

| 阶段 | 工作与模块 | 退出条件 |
|---|---|---|
| A 诊断输入与文本能力包 | M1 pin GLM-5.3 actor、宿主加载 skills/memory；M0 HarnessPackage/worker.harness 身份；M4 train/fit 失败摘要与断言明细、逐题笔记→汇总、harness_edit 文本组件；M4/M5 文本草稿检查（结构/路径/manifest）与有界修订；M3 另存判分明细 `grading.jsonl`、M7 按冻结 split 读取；M7 preset/K 先固定 max_failure_notes、失败摘要条目长度上限与 max_draft_repairs；首轮真实演化前运行 GLM-5.3 train+dev 基线 | 包路径、内容摘要和 manifest 可校验；无人工优化方案即可从真实 train 失败生成文本包，读取/注入事件可查，经 gate 裁决，下一轮沿用已采用包；dev/final 断言明细不进入 R；旧兼容通过；不需要 Docker、trainer 或 teacher |
| B 代码组件与隔离 | M1 固定 package API、Docker worker/工具 broker、hook（先 on_finish）、包工具；M4 harness_edit 代码组件；M4/M5 把冻结前试跑修复扩展到 isolated_probe → package_test（同一 max_draft_repairs）、有界构建/恢复 | 无人工优化方案即可从真实 train 失败生成可执行 tools/middlewares 组件，经草稿检查与 gate 裁决，下一轮沿用已采用包；接口可校验；生成代码只在 worker 执行，不在控制面 import，无 in-process 回退；真实 hook/工具行为可查；不需要 trainer 或 teacher；本机 Docker daemon 可用是真实检查前提 |
| C 验证与 app | M3 同模型固定 dev 配对 gate（含逐题退化记录）、preflight/CAS/导出 | 包被真正执行；有收益才采用；干净部署只需 runtime/包/模型服务/工具绑定 |
| D 递归方法回用 | M4/M6 两支搜索能力包；M3 安装 R | 同 W/E 起点、相同代码 API/镜像/预算；下一改进调用实际加载 R1；失败保留原版本 |
| E 用户入口与真实验证 | M7 harness preset/cycle/报告；完整示例 | 自动代码提案、真实任务、包迭代与有界 C4 留有记录；工程与收益结论分开 |

实现状态（2026-10-01）：A～E 的工程链路已实现，离线测试 703 passed, 1 deselected（Docker 运行时 IT6-23 隔离测试一并通过）。真实模型已跑通两段：一个通过的 train 任务走到研究汇总；一个失败的 train 任务经逐题笔记、汇总提案、草稿检查与一次修订、准入、冻结实验和候选构建，在 gate 前停止。gate 已真实运行到底但未得出结论：比较途中智谱账号余额耗尽，磁盘上 20 条 dev 记录中 19 条无效（gate 收据计入的 19 条中 18 条无效，其中唯一有效的是复用的 1 条筛选候选臂记录），判为 inconclusive，未安装。安装、任务中运行的包、Docker 中的代码草稿检查与 R 递归尚未在真实模型下运行。2026-10-01 的需求审计：首版 354 项中 186 项实现并有测试，23 项实现无测试，136 项部分实现，7 项缺失（明细见 history/V0_6_AUDIT_2026-10-01.md）。E 的退出条件中“真实任务、包迭代与有界 C4”需冻结预算并由用户决定开始付费运行。明细见 [JOURNAL](JOURNAL.md)。

2026-10-02 复核（11 个范围逐一审计并对抗复核，结论见 JOURNAL 同日条目）发现 4 个高危缺陷：
- 准入在 K 未冻结 methods 时放行 R 提供的 `spec.backend`，使控制面 import 任意对象（阶段 3 已修）；
- 研究比较中两支 W* 相同时仍可判 accepted（研究位前已修）；
- 外层比较不按任务配对（研究位前已修）；
- 续跑不读 Experiment 冻结的评测模板与预算 scope（阶段 3 已修）。

其余两项（研究裁决）已在 §3 的“研究位前”修完（2026-10-03）。

另有以下问题：
- 暂停只在 worker 轮循环里写入；collect、续跑、基线、研究位提案、campaign 公共测量与外测遇到账号错误或截断时以异常退出（阶段 3 已修：cycle 的每一步按同一规则暂停或结束）。
- 示例研究模型的输出上限 16384 不够用（阶段 1 已改为 32768）。
- 所有 improve/cycle/research 集成测试都注入脚本研究员。
- 研究收据的结果对象记下两支工作区里的 release：各支最后一次安装的 release，以及没有安装的轮次记下的起始 release。它们不在 W* 的闭包里，没有复制回活动对象库。R1 装入后，凡是闭包经过该 R 安装 release 的 `copy_closure` 都以 KeyError 崩溃：第二个研究位，或在同一工作区里开研究位的后续活动。首个活动只有一个研究位（cycle 拒绝更多），不受影响。
- M6 §3 要求报告 research_dev 的候选次数和访问历史，报告里还没有。
- 第二次彩排发现：汇总请求只有笔记、包与 targets，没有失败摘要，也没有基础工具的名称与参数。R 要写包工具或 hook，所需的接口细节（端点、字段名、工具名、actor 消息里的标记）只能由笔记带过去，而 R0 的 diagnose 提示没有要求笔记带这些。真实运行中，代码组件的提案可能因此写不对。2026-10-03 决定：在汇总与修订请求里加入基础工具清单（方案 2），已实现（`1c44670`）。第三次彩排（REHEARSAL.md §13）表明，方案 2 加上“笔记写下本条失败过程里的接口细节”就够了。但 R0 的 diagnose 提示没有要求笔记写下这些细节（端点、方法、字段名、错误原文、actor 结束消息里可作判断依据的原话）。2026-10-03 决定加进 R0 的 diagnose 提示（新的 R0 身份；真实活动尚未开始，没有工作区受影响），已实现（`ef6e865`）。第四次彩排（REHEARSAL.md §15）显示两项合起来足以只凭请求写出有效的包工具与 hook。仍未检验的是端点从未被调用过的情形。
- IT6-10/19/21/23/26 仍部分实现：活动取消入口尚无代码（首个活动靠停止点人工停下）；逐题退化记录已在阶段 3 写入 gate 收据，IT6-26 的合并/停用分支仍没有测试。

修复前不进入付费运行。修复与真实运行的顺序见 §3 的首个真实活动协议。

A～E 属于首版，递归方法比较与实际回用仍为必做。第二版接 M8 教学适配/采集、M2 SFT、M1 sampling/teacher hook 及独立 controller 新训练验证，沿用已写规格；不把这些账户或依赖放入首版预检。按 [分版验收](modules/08_INTEGRATION_CASES.md)执行，未来用例不删除也不算首版缺项。真实试验需模型服务与冻结预算；不具备时真实验证标未完成。

补采请求经同一 driver 自动续跑（purpose=task_feedback）属首版，IT6-19/21 首版验收，覆盖补采恢复及 worker 段与活动的停止区别；更换 h 时创建 baseline release/新比较身份与 source_validation（只验证教学来源）属第二版，IT6-18/20 第二版验收。

新增 cycle 只是按固定 schedule 调用现有 handlers；不另造编排器。R 使用已有 provider/策略对象，逐题笔记与汇总串行调用同一 provider，不建子 agent 平台；M6 复用已有 campaign。scope 首版允许能力包目录内工具/中间件代码、技能/经验与注册 W/R 策略；框架源码、任务数据/评分、权限、预算、依赖锁/镜像和发布核心不可演化。固定 package API 的变化仍由工程开发接入，不让候选自行注册新的宿主入口。

## 3. 首条真实实验路径

首个业务后端采用 AutomationBench 直连。首版 actor 原定 GLM-5.3（已配置并计价的 OpenAI 兼容接口），2026-10-03 起先跑 Claude Opus 5.5（经订阅额度，见 §3 第 2a 步），冻结底座、权重身份和推理配置；R 可共用供应商。Docker 用于能力包代码隔离，不管理任务集群，也不要求接 Harbor；只含 skills/memory 的包由宿主装配，不启动 worker。现有决策 trainer 保持兼容，但首版实验不训练新模型。第二版生成训练首选 Tinker LoRA SFT，候选学生 Qwen3.5-9B；届时验证实际模型/SDK/费用，不提前构成首版依赖。换 Qwen3.5-9B 学生时，h* 需在学生上重新演化：[Harness-Zero](TECHNOLOGY.md#4-参考设计如何转为独立实现) 的 h* 是在学生上验证的（其 base+h* 为学生带 h* 的成绩），首版在 GLM-5.3 上演化的包不直接作为学生的 h*。

先测所 pin actor 的无教师起点（原定 GLM-5.3，2026-10-03 起为 Claude Opus 5.5，见 §3 第 2a 步）。真实基线尚未运行：2026-09-30 一次启动在第一个任务中途中止（4 次模型调用，无完成尝试）；train+dev 基线在首轮真实 h* 演化就绪时、演化开始前运行，既有单任务 dev 成绩不能作为 before。从 train/fit 失败摘要与断言明细经逐题笔记→汇总生成共享能力包；先交付 skills/memory 文本组件，再交付代码组件。代码草稿冻结前完成隔离接口 smoke（package_check → isolated_probe → package_test）及有界修复，再真实执行和 dev gate；Docker 隔离测试已在本机 daemon 上通过（2026-10-01），但 CI 尚未构建 worker 镜像，代码草稿在 Docker 中的检查链也还没有经过真实模型。诊断可聚焦失败 train 任务；候选采用前仍比较完整固定 dev 清单，不另建历史成功任务池或回归作业。至少观察一个工具封装或中间件改变真实执行；静态检查通过或文件变化不能作为 C1 证据。

任务家族和失败覆盖先统计，不预设一定涨分。首版按 purpose=task_feedback 的 CollectionRequest 补 plain train attempt；第二版训练缺口使用对应 purpose，同一 driver 不变。不能把 test 迁入 train，或通过代码编写读取 dev/final 答案。预留 gate/递归额度，计入 R/actor、逐题笔记、草稿检查/修复、代码构建/隔离运行、失败候选与全部评测。

在基础 app 链路可运行后，安排一次冻结的 R 方法比较及其下一轮回用检查。R1 无收益则保留 R0，报告 C4 未成立；不为展示递归而强制安装。以后可增加实验规模或新后端，但不能把这条核心能力永远留在设计里。

### 首个真实活动协议（2026-10-02）

本节在代码修复之前写定首个付费活动的结构、顺序与止损。修复或运行若要偏离本节，先改本节再动手。成本与工期都是按 §4 单价的估算。

括号内是落实该条的测试：标“新增”的随对应修复一起写；只改配置的条目不另配测试。

1. **付费前必修。**
   - 基线前（2026-10-02 已完成，经对抗审查后修正，见 JOURNAL 同日条目）：
     - 基线的 train 尝试直接作为第一轮测量：一个活动里 W0 的 train 只测一次（新增 `test_cycle.py::test_the_baseline_train_is_the_first_measurement`）。
     - 单独跑的基线与之后 cycle 里的基线互相复用，不重付（`test_cycle.py::test_baseline_attempt_ids_are_stable`，在与默认值不同的 task plan 下验证）。
     - 运行身份由运行时守卫锁定，没有采用字面量金样（运行身份含环境信息，换机器就会变）：`measurement_planned` 与 `collection_booked` 记下运行身份；用改过的代码补完被中断的测量或补采批时，身份不一致就拒绝续跑，不按新身份重付（`test_cycle.py::test_a_measurement_booked_under_another_run_is_not_finished_under_this_one`）。
     - 测量不完整时不复用，按记账的任务补齐（`test_cycle.py::test_an_interrupted_measurement_is_finished_not_reused`、`test_a_limited_baseline_is_finished_by_the_first_round`）。
     - 补采先记账再运行：中断的批次在重启时按原序号补完、只计一次额度，补完的结果照常判断是否有进展；重建测量时纳入同一版本的补采结果（`test_an_interrupted_collection_batch_is_finished_at_its_own_index`、`test_improve_finishes_an_interrupted_batch`）；补完时遇到账号错误按暂停处理（`test_an_account_refusal_while_finishing_a_batch_pauses`）。
     - 示例配置中，研究模型的 `max_tokens` 与 `[pricing] max_output_tokens` 提到 32768。
     - 启动检查按 pin 的 actor 与 `[research.llm]` 核对输出上限，并确认 key 存在；`type = "manual"` 免检 key（新增 `test_cycle.py::test_startup_checks_the_pinned_models`）。
     - 付费阶段在计划未命名时拒绝启动，并提示先 freeze（新增 `test_cycle.py::test_a_paid_phase_refuses_an_unfrozen_budget`）。具体是：
       - 配置里有 `[freeze.X]`，而 `[rsi]` 没有对应的 ref；
       - 或者活动带 `[improvement]`，而 `[rsi]` 没有同时命名 task_plan、schedule、budget 三个 ref。
       - 原因：以空限额建立的预算根，以后无法再用冻结的限额打开；不在冻结计划下跑的基线，也不能被 cycle 复用。
       - 有 `[improvement]` 时，`ouroloop freeze` 也冻结 preset 的 task plan 与 schedule，与 cycle 自己冻结的是同一组对象（`test_cycle.py::test_baseline_attempt_ids_are_stable`）。
     - cycle 拒绝多于 preset 的 `research_slots`（新增 `test_cycle.py::test_more_research_slots_than_the_preset_are_refused`）。
     - cycle 加停止点 `--until`（新增 `test_cycle.py::test_cycle_stops_at_the_named_step`）：
       - 取值为 `baseline`、`segment-N`、`segment-N/round-K` 或 `slot-N`，与 `rsi/cycle.json` 的步名一致。
       - 停下时：该段不标 finished，不进入下一个付费步骤，cycle 以 stopped 结束（不是 paused）。
       - 再次运行时从下一步续跑，已完成的部分不重复记账，也不重新冻结任何计划。
       - 停止点是优先级最低的停因：调度规则在同一时刻触发时，报调度规则自己的原因（`test_a_stop_point_does_not_hide_the_schedules_own_end`）。
       - 停在请求补采的那一轮时，这批不付费；续跑时先补这一批，再进入下一轮（`test_a_collection_a_stop_point_left_unanswered_runs_before_the_next_round`）。
       - 续跑保持 patience 与失败计数；被重跑的轮（暂停的、因基础设施失败的）不计入（`test_a_resumed_segment_keeps_its_patience_and_failure_counts`、`test_a_segment_paused_on_infrastructure_resumes_after_the_repair`）。
       - 回用检查只读研究位之后那一段，在它非暂停地结束时计算；停止点、暂停或重启都保留待查（`test_the_reuse_check_is_read_from_the_segment_after_the_slot`、`test_a_paused_segment_after_an_install_keeps_the_reuse_check_pending`、`test_until_slot_stops_before_the_next_segment`）。
     - 活动开始时冻结的计划记在 `rsi/cycle.json`；之后的 cycle 若会冻结出别的计划（preset 或 `[freeze.*]` 变了，或 `[rsi]` 换了 ref），就拒绝运行（`test_an_activity_does_not_freeze_its_plans_again`）。
     - 预算根里划出一笔 `final` 分配（`[freeze.budget.final]`），final 按该分配预留，cycle 不能花这部分（`test_cycle.py::test_cycle_cannot_spend_the_final_allocation`、`test_paid_run_safety.py::test_the_final_spends_the_share_set_aside_for_it`）。
       - 分配在 freeze、启动检查与打开预算根时都校验：必须限制根所限的维度、大于零、给其余部分留有余额，且放得下一次调用（`test_a_share_that_cannot_work_is_refused_before_a_root_is_opened`）。
       - 分配在第一次打开根时设定；根打开后不能改、不能撤、也不能在花钱之后再加（`test_an_open_roots_allocations_do_not_change`）。
       - 显示给 R 和人的有效剩余，与预留实际允许的一致，不含未花的 final 份额。
   - worker 段前（2026-10-02 已完成，经对抗审查后修正，见 JOURNAL 同日条目）：
     - 准入一律拒绝 R 提供的 `spec.backend`，以及不带同一目标训练条目的 `kind=model` 改动（含 worker.actor 换模型身份），不依赖 K 是否冻结了 methods；构建对修复前已冻结的实验同样拒绝（`test_research_round.py::test_a_proposal_cannot_name_a_backend`）。
     - 续跑按 [M5 §3](modules/05_EXECUTION_BUDGET.md) 的“续跑的冻结输入”处理（新增 `test_real_run_findings.py::test_resume_refuses_a_changed_frozen_plan`）：
       - 评测模板或预算 scope 与 Experiment 冻结的不一致：拒绝续跑并暂停。
       - 筛选与评测的 control、permit 与候选 release，都建在冻结的 parent_release 上。
       - 安装仍按冻结的 parent_release 做 CAS；active 已移动时记 stale_parent，不暂停。
       - 续跑途中遇到暂停类或耗尽类故障，单独运行的 improve 与 campaign 臂也按同一规则停下，不以异常退出。
       - campaign 臂冻结并记录自己的评测模板（原计划在研究位前，随本项完成）。
     - 暂停、结束与 invalid 按 M5 §3 和 [M4 §1](modules/04_RESEARCH_LOOP.md) 覆盖全部路径，新增：
       - `test_real_run_findings.py::test_every_cycle_step_pauses_on_an_account_error`
       - `test_real_run_findings.py::test_a_research_call_that_keeps_failing_pauses_the_activity`
       - `test_real_run_findings.py::test_a_spent_budget_ends_every_cycle_step_without_pausing`
       - `test_research_round.py::test_an_unreadable_reply_is_an_invalid_round`（worker 轮的汇总回复；研究位的回复见下面“研究位前”）
       - 账本拒绝分两类：额度耗尽（`BudgetExhausted`）是有界结束；预算与冻结的不符是配置问题，暂停（`test_harness_loop.py::test_budget_exhaustion_in_the_note_map_stops_the_round_cleanly`）。
     - 逐题笔记全部读不出时，不调用汇总，本轮记 invalid（新增 `test_diagnosis.py::test_no_readable_note_makes_an_invalid_round`）。
     - prompt 改动也做 train 字面量检查（新增 `test_research_round.py::test_a_prompt_change_gets_the_literal_check`）。
     - gate 收据写入逐题退化清单（新增 `test_task_gate.py::test_the_receipt_lists_each_regressed_task`）。
     - 包工具的 name/schema 按 provider 约束校验（`test_harness_package.py::test_a_tool_name_the_provider_refuses_fails_the_check`、`test_a_tool_schema_the_provider_refuses_fails_the_check`）；`enabled` 只取布尔，停用的工具不暴露（`test_enabled_is_a_boolean_and_a_disabled_tool_is_not_offered`）。
     - 另加：CI 构建 worker 镜像，并在 `OUROLOOP_TEST_REQUIRE_DOCKER` 下运行 3 个 Docker 隔离测试；缺镜像就失败，不再跳过（`.github/workflows/tests.yml`）。
     - 对抗审查后补充：
       - 冻结的 task plan 带全部单次尝试上限，`[bench.budget]` 冻结后再改，不影响任何已冻结的比较；研究位的两支与公共测量也从计划读上限（`test_cycle.py::test_a_frozen_plan_keeps_its_attempt_limits_when_the_config_changes`）。
       - 计费层重试用尽后，任何供应商异常（含 Anthropic SDK 的连接或超时错误）都以 ProviderError 离开计费层，按暂停处理；网关返回 200 却没有 choices 也是供应商故障（`test_a_research_call_that_keeps_failing_pauses_the_activity` 的 sdk 用例、`test_a_gateway_that_answers_200_without_choices_is_a_provider_failure`）。
       - 账号拒绝只从供应商的故障里读，内部错误的文本里带 "billing" 不算；预算不符单列为 `BudgetMismatch`，其余账本误用直接上抛（`test_only_a_model_providers_failure_is_read_as_a_refused_account`）。
       - 构建与续跑都按准入的同一规则检查 model 改动；训练条目只能指向核心开放的决策目标（`test_a_resumed_experiment_that_names_a_backend_is_not_evaluated`）。
       - 逐题笔记读不出时记下真实原因（`test_diagnosis.py::test_an_unreadable_note_records_why_it_could_not_be_read`）；已完成的段不被后续步骤的故障记成暂停（`test_cycle.py::test_a_finished_segment_is_not_recorded_as_paused_by_a_later_failure`）；campaign 臂的续跑有测试（`test_rsi_integration.py::test_a_campaign_arm_resumes_its_open_experiment_under_its_frozen_inputs_or_pauses`）。
   - 研究位前（2026-10-03 已完成，经对抗审查后修正，见 JOURNAL 同日条目；彩排的研究位部分在此之后重跑）：
     - 研究裁决按任务配对；配对不全、或两支 W* 相同时，判 inconclusive；外测补跑一次，额度从预算根预留。规则见 [M6 §3](modules/06_RESEARCH_CAMPAIGN.md)：
       - 配对：同一 pair 内，外层任务的每次重复在两支都有有效尝试才计入；补跑后仍缺任一，判 inconclusive。
       - 两支 W* 是同一个 worker ref 时不跑外测，判 inconclusive。
       - 补跑按外测计划冻结的 retry_policy，只补基础设施失败，至多一次。
       - 预留：preset 的 `outer_share`（0.08）乘冻结限额，写入 campaign plan 的 `outer_budget`。它是外测 scope 的上限，并在公共测量与两支开跑前以不结算的预留占住。
       - 两支等额：研究位开始时，根须能覆盖外测份额与两支份额（由 cycle 自己的研究员作答时，再加 R 这一次调用），否则研究位不问 R，记 budget_exhausted。公共测量之后、两支开跑之前，两支的份额也以不结算的预留占住，各支开跑时释放给自己；占不住时两支都不运行，外测份额退回，判 inconclusive：否则后跑的一支（R1）会被前面的花费饿死，预算不等的比较说明不了 R。重启找回这些预留，已跑过的支不再检查。
       - 新增 `test_campaign.py::test_identical_arms_are_inconclusive`、`test_identical_arms_stop_the_campaign_before_a_later_pair`、`test_outer_results_are_paired_by_task`、`test_a_task_missing_on_either_arm_leaves_the_pair_incomplete`、`test_a_task_with_a_repeat_lost_is_not_counted_as_paired`、`test_the_outer_share_is_set_aside_before_the_common_inputs_and_the_arms`、`test_an_arm_the_root_cannot_cover_leaves_the_comparison_inconclusive`、`test_a_restart_holds_and_checks_nothing_again_for_arms_that_already_ran`；`test_rsi_integration.py::test_an_outer_attempt_lost_to_infrastructure_is_refilled_once`、`test_an_outer_attempt_lost_again_on_its_refill_leaves_the_comparison_inconclusive`；`test_cycle.py::test_a_slot_whose_outer_share_the_root_cannot_hold_asks_r_nothing`、`test_common_inputs_that_leave_the_arms_short_end_the_slot_inconclusive`。
       - 调整现有测试：`test_rc02_an_arm_that_installed_nothing_returns_w0_with_its_costs` 的断言翻转为 inconclusive；rc05、rc09、rc10、it6_16 与 `test_an_arm_with_no_outer_result_is_inconclusive_rather_than_a_win` 中两支 W* 相同的测试改夹具，让两支不同，断言不变；`test_real_run_findings.py::test_a_spent_budget_ends_every_cycle_step_without_pausing` 的 campaign 参数相应调整：在公共测量处花光时两支份额占不住、两支都不运行；在某一支处花光时两支 W* 相同、外测不运行；两种情况下外测预留都退回活动，下一段开始时再花光一次。
     - E0 交给两支：研究位开始时主线经验冻结在配对里，两支的经验日志以它原样开头，第一轮读到同一个 experience_ref。实验记录去掉原始 gate 理由，因为理由可能写出开发题号；汇总指标保留。E0 引用的对象不复制进分支，分支里没有读取它们的步骤（新增 `test_cycle.py::test_rc6_01_both_arms_start_from_the_same_e0`）。
     - worker 段装过包之后，研究位的公共测量与两支复制 W 时以 KeyError 崩溃：嵌套记录的 `ref` 被 `refs_in` 当作对象引用，`copy_closure` 去复制一个不存在的对象（阶段 3 调研发现，见 JOURNAL）。修法：整条嵌入的记录自带的 `ref` 等于其余字段的摘要，`refs_in` 不把它当作引用。不改任何对象的身份，修复前写下的工作区不用迁移。新增 `test_cycle.py::test_a_slot_after_a_worker_install_runs` 与 `test_contracts.py::test_an_embedded_records_own_ref_is_not_a_reference`；`test_real_run_findings.py::test_every_cycle_step_pauses_on_an_account_error` 去掉绕开它的停止点。
     - 研究位的 system 提示保留 R 自己的 propose 文本（R 正是用它提出对自身的修改），由研究位格式声明：前文是 worker 轮的改包指令，本次调用改的是这些指令本身，以本格式为准。只改代码里的格式，不改 R0 文本：R0 已冻结进 release，活动内改它不会生效（新增 `test_cycle.py::test_the_slot_keeps_rs_propose_text_and_its_format_governs`）。
     - 研究位读不出的回复记 invalid，不记 no_proposal；没有研究收据时，报告的 C4 按 `cycle.json` 中研究位的结局写 detail。规则见 [M4 §1](modules/04_RESEARCH_LOOP.md)：每个提案都校验；`cycle.json` 记 stage（reply、admission 或 build）；提案文本不截断，超过 K 的提示长度上限时构建失败。新增 `test_report.py::test_c4_says_how_a_slot_without_a_campaign_ended`、`test_cycle.py::test_an_unreadable_slot_reply_is_an_invalid_slot`、`test_a_slot_decision_r_states_is_still_no_proposal`、`test_a_slot_that_admission_or_the_build_refuses_says_which`、`test_a_slot_reply_cut_at_the_output_limit_pauses_and_is_asked_again`。
     - 研究位安装 R1 之后，回用检查只在紧随其后的 worker 段非暂停地结束时计算；该段暂停、崩溃或停在停止点时，保留待查。已随停止点在阶段 1 完成（`test_cycle.py::test_a_paused_segment_after_an_install_keeps_the_reuse_check_pending`）。
2. **付费前彩排（生产路径，不付 API 费用）。** 2026-10-03 已完成，结果与覆盖缺口见 [REHEARSAL.md §9](REHEARSAL.md)；补技能、包工具与补跑的第二次彩排见 §10，结果见 §11。
   - 目的：经 CLI `ouroloop bench --phase cycle` 走一遍生产路径，不注入研究员，也不替换 `cycle_pairs`，R 由 Claude 作答。它证明接线与故障协议，不证明任何模型的效果。
   - 环境：
     - R：`[research.llm] type = "manual"`（`ouroloop/providers/manual.py`）。Claude 只依据请求文件作答，不使用仓库、评分器或任务答案这些带外知识。
     - 任务世界：以 `marketing` 域名导出的 fixture 上游，满足 preset 的域锁（`bench.py:923-925`）。规模小：train 中固定失败的题不超过 5 道；dev 至少 5 个家族，满足 gate 的 min_groups。
     - actor：`[llm] type = "<模块>:<类>"` 的确定性假 actor，在 bootstrap 时被 pin 为 W0。它对包内容作出反应：包中出现某类规则（memory 或 skill 文本、on_finish hook 的回推）时，在指定的 dev 题上改做对，让 gate 增益可以达到 0.05；在 train 失败题上留下能被失败摘要捕捉、并指向该规则的线索。假 actor 的规格在阶段 3 开始时先写，再实现：见 [REHEARSAL.md](REHEARSAL.md)。
       - 缺口不止一类：segment-0 装包之后，仍要有缺口同时出现在开发家族冻结顺序的前一半（两支的内层 gate）与后一半（外层比较），两支才可能装包，外测才会运行。两支 W* 相同时研究位判 inconclusive、不跑外测；若如此，外测、补跑与 R1 装入后的回用如实记为覆盖缺口。
     - 研究位部分的重跑从 `--until segment-0` 时复制的工作区开始：已记下研究位的工作区不再重问，同一 R1 的 campaign 有收据就不再运行，研究位前修复之前冻结的计划也没有外测与两支的预留。
     - 组件：允许代码组件（本机 Docker 可用），以便覆盖 Docker 中的草稿检查链。
     - 隔离：独立工作区与 scope，名称带 rehearsal。系统不会给这些记录打 synthetic 标记：收据仍记 discovery=automatic，提案仍记 origin=llm。synthetic 由工作区名与 JOURNAL 标注；该工作区的报告与 C1～C4 不得引用，也不与正式工作区合并。
     - 规模：用停止点分段推进；需要时只调低 `max_failure_notes`。
   - 故障注入：R 侧通过 manual 回复文件注入。“非 JSON”写在合法回复文件的 `text` 字段里；回复文件本身解析不了时，manual 会一直等待。

     | 步骤 | 注入 | 预期 |
     |---|---|---|
     | 任一步 | `stop = "max_tokens"` | 暂停；续跑时重问同一步，不计 invalid，不重复记账 |
     | 逐题笔记 | 空回复、非 JSON、缺字段 | 该条记入 coverage.invalid，本轮继续；全部读不出时本轮 invalid |
     | 汇总 | 空回复、拒答（`stop = "refusal"`）、非 JSON、形状非法的提案 | 本轮 invalid，不回退成 collect 或 stop |
     | 修订 | 空回复或读不出 | 占用一次修订；用尽则本轮 invalid |
     | 研究位 | 读不出、拒答或形状非法 | 研究位 invalid，不记 no_proposal |
     | 任一步 | manual 判为非法的回复（例如带 tool_calls） | ProviderError，暂停；只验证逃逸路径，不代表真实供应商的行为 |
     | actor | 假 actor 调用未提供的工具 | 任务内的工具错误，尝试继续 |

     研究位只有一次，记 invalid 后不重问。所以研究位“读不出”一行在另一个彩排工作区里注入（同样跑到 segment-0 再进研究位），主彩排工作区的研究位用来走到裁决。截断与 ProviderError 只让研究位暂停，续跑时重问，可以在主工作区注入。

   - 通过条件：
     - 至少一次走完笔记 → 汇总 → 草稿检查 → 准入 → 构建 → gate 裁决，accepted、rejected、inconclusive 都算。
     - Docker 草稿检查链：
       - 至少一份含 hook 或包工具的草稿，在 Docker 中走完 package_check → isolated_probe → package_test；结果为 ready，或经有界修订后为 invalid（基础设施失败不算）。
       - gate 候选臂的 transcript 中出现该 hook 或工具的执行事件。
       - 另注入一份首检失败的代码草稿，覆盖 Docker 内的修订回路。
     - 安装，且下一轮满足 K3a。若假 actor 的设计没能让 gate 接受，如实记为覆盖缺口：生产 CLI 路径上的安装与回用，要到第一次真实运行才首次执行。
     - 研究位走到裁决，accepted、rejected、inconclusive 都算；accepted 时，下一段核验 R1 被加载。
     - 故障注入表逐行得到预期；每次暂停后续跑，都不重复记账。
   - 产物：作答的请求与回复存档，作为回放 fixture 进入测试。
   - Claude 作答 actor 的检查不在这个 cycle 里做，因为 actor 在 bootstrap 时已被 pin：
     - 另开一个以 `[llm] type = "manual"` bootstrap 的工作区；
     - 在真实 Marketing 上跑 `ouroloop bench --phase baseline --limit 1`，即 dev 与 train 各第一题；
     - 用来检验 pin 路径上工具调用的往返；不用 test 题。
   - 边界：彩排替代不了第 3 步的计价校准，也说明不了 GLM-5.3 的表现。
2a. **Claude 运行（订阅额度，2026-10-03 决定）。** 先看本框架下 Claude 的任务得分；GLM-5.3 与第 3 步的计价校准延后，不删除。
   - 模型：actor 与 R 都用 Claude Opus 5.5（`claude-opus-5-5`），effort 为 medium（省额度；要改须在 bootstrap 之前，因为 W0 会把它 pin 进推理配置）。经新的 `claude_code` provider 调用无头 Claude Code（`claude -p`），用用户的 Max 订阅登录；启动检查拒绝任何不是 Claude 订阅的登录。规格见 [M1 §1](modules/01_POLICY_RUNTIME.md)。
   - 请求里有什么：每次调用在一个空目录里运行，关闭全部工具与本地定制（CLAUDE.md、记忆、技能、插件、hook、MCP、设置文件、附件），不保存会话，系统提示换成框架给的那段，CLI 自己的重试与换模型都关闭；调用不经任何交互会话转发。订阅登录下 CLI 仍会附上三样，关不掉：系统提示前一行 Agent SDK 身份、账号邮箱、当天日期。actor 的回复格式说明写明这些不属于任务。冒烟时核对模型实际看到的内容。
   - 与原生 API 的差别，随结果写明：
     - 工具调用经结构化输出（`--json-schema`）写在回复里，不是原生 tool use；
     - 每次调用重发整段历史，上下文缓存命中多少由冒烟实测；
     - 请求带有上面三样附加内容；
     - 得分是“Claude Opus 5.5（effort medium）经 `claude -p`、JSON 工具调用”在本框架下的得分，不与原生 tool use 的公开成绩直接比较。
   - 计价：订阅没有逐次账单。`[pricing]` 按 claude-opus-5-5 的 API 标价填写（输入 $4、输出 $20、缓存读 $0.20、缓存写 $8，每百万 token；CLI 写的是 1 小时缓存），作名义上界，`source` 注明；每次调用把 CLI 报告的 API 等价费用记进 usage（`cli_cost_usd`），用来核对账本。一次 CLI 调用最多发 5 次模型请求（输出触顶后 CLI 自行续写至多 3 次，加一次结构化输出重试），所以 `max_output_tokens` 至少是 `max_tokens` 的 5 倍，启动检查会核对。预算是名义美元，用来限住额度的消耗。
   - 额度用完：CLI 报使用上限属账号类错误，活动暂停，额度恢复后同一命令续跑（[M5 §3](modules/05_EXECUTION_BUDGET.md)）。CLI 实际作答的模型不是所配模型时同样暂停。若账号开了 extra usage，CLI 在套餐额度用完后改用付费额度继续，不报错，名义预算就成了真钱：第 2 步之前由用户在 claude.ai 设置里确认 extra usage 已关闭。
   - 顺序（每一步先看用量与稳定性，用户确认后再进下一步）：
     1. 实现 provider 与离线测试（假 `claude` 程序），不耗额度。
     2. 探针：经 provider 直接发两次调用，一次普通问答、一次带一个假工具的结构化回复。核对 CLI 的输出形状（`structured_output`、`num_turns`、`stop_reason`）、用量与缓存、`modelUsage` 里实际作答的模型、`cli_cost_usd` 与 `[pricing]` 是否相符、工作目录是否仍为空，以及模型看到的请求之外的内容。
        - 2026-10-03 结果（`examples/automationbench/claude_probe.py`）：两次都成功，普通问答 `num_turns` 1、4.5 秒，结构化回复 `num_turns` 2、2.6 秒，正确调用了假工具；`modelUsage` 只有 claude-opus-5-5；工作目录仍为空；账本与 CLI 报价逐分相等（缓存写按 1 小时价 $8/M，已改进 `[pricing]`）。模型看到的请求之外的内容只有一段 userEmail 上下文（CLI 自带“不属于用户消息”的说明），没有看到日期。effort medium 下普通问答的 thinking 为 117 token。
     3. 冒烟：复制 `claude.toml` 为 `claude-smoke.local.toml`，改 `[workspace] root` 与 `[rsi] scope_id`，`[freeze.budget]` 压到几美元；跑 `--phase smoke`（1 道 dev 题），看一道题的调用次数、token、缓存命中与耗时。
     4. 同一冒烟工作区：`ouroloop freeze` 后把打印的 `[rsi]` 行加进副本，跑 `--phase baseline --limit 2`（dev 与 train 各 2 题）。按实测校正 `[pricing]`，并按实测的每题名义费用和第 4 步的结构重定 `claude.toml` 的 `[freeze.budget]`：现在的 $300/$50 沿用 GLM 的估算，Claude 下研究位的两支与外测都不够。预算根一旦打开就不能改，所以这一步必须在活动工作区打开之前完成。
     5. 活动工作区：`--phase cycle --until baseline` 跑 train+dev 基线（77 题），得到得分。
     6. 额度允许，再按第 4～7 步跑完整活动。
3. **计价校准（独立于正式活动）。**
   - 2026-10-03 起延后：先跑第 2a 步；GLM-5.3 恢复为 actor 时再做本步。
   - 继续使用 open.bigmodel.cn。`[pricing]` 按该平台价目填写，注明汇率与日期；账本必须等于账单，否则预算上限没有意义。
   - 充值后，在一个独立的校准工作区运行 `ouroloop bench --phase baseline --limit 5`：5 个 dev 加 5 个 train，共 10 次尝试，约 $1.4～4.5。
   - 用账本对比平台账单：偏差 ≤10% 即通过；超限则修正 `[pricing]`，在新的校准工作区重测。
   - 校准的尝试不复用，不计入正式活动的预算根。正式活动用最终的 `[pricing]` 开始，活动内不改计价（M5 §2）。
4. **活动结构与冻结。**
   - 结构：一个活动 = train+dev 基线 → segment-0（至多 3 轮；patience=2，连续两轮不安装即结束）→ 一个研究位 → segment-1（检查回用）。
   - 启动顺序：
     1. `ouroloop freeze --config …` 冻结 task plan、schedule 与预算，把输出的 `[rsi]` 行写进配置；
     2. `--phase cycle --until baseline`，检查 K1；
     3. `--until segment-0/round-0`，检查 K2；
     4. `--until segment-0`，检查 K3，并由人决定是否进入研究位；
     5. 去掉停止点，跑完。
   - 预算：活动根 $300，其中 `final` 分配 $50，cycle 可花 $250；每支按 0.15 份额约 $45；外测按 0.08 份额约 $24（外层约 13 题、两支，含一次补跑的上限），campaign 开始时自动预留；两支份额在公共测量之后预留。研究位开始时根须空出两支与外测共 38%（约 $114），由 cycle 的研究员作答时再加 R 一次调用的上界。先充值 ≥$150 等值，进入研究位前补足。
   - 进入研究位前（segment-0 停下时）人工核对：根的有效剩余不少于“两支份额 + 外测份额 + 公共测量（当前 W 的 train 测量，约 $7～23）+ segment-1 首轮估算”；不够就不开研究位，直接跑 final。根覆盖不了外测份额与两支份额时，研究位不问 R，以 budget_exhausted 结束，活动随之结束；公共测量之后两支份额占不住时，研究位判 inconclusive。
   - 活动内不重新冻结 task plan、schedule、预算或计价；首版 preset 不开 screen。
5. **gate 口径。**
   - 沿用点估计门槛（min_gain 0.05，repeats=1）。worker gate 的 accepted 只是开发采纳，C1 只由 final 裁决。
   - 基线 dev 与 round-0 gate 的对照臂，是同一个 W0 的两次测量，相当于免费的 A/A：结果不一致的家族 ≥4/25 时，下一个活动冻结 repeats=2。
6. **检查点与止损。** “停止”指停在该检查点所在的步，不进入下一个付费步骤。
   - K1（baseline 后）：有效尝试 ≥74/77；open 与 unknown 预留为 0；W0 的 dev 成功率在 15%～85% 之间。不满足就停止，不进入 worker 段。
   - K2（segment-0/round-0 后）：
     - 通过条件：提案通过准入；gate 经基础设施补跑一次后 25 对完整，裁决为 accepted 或 rejected（不是 invalid 或 inconclusive）；unknown 为 0。
     - 不满足就不续跑 round 1。基础设施原因：离线修复后从 round 1 续跑。提案或准入原因：由人决定续跑，还是以负结果收尾。
   - K3（首次安装后的下一轮）：
     - K3a 装配，不过就停止：下一轮 RunManifest 的 package_ref 等于新包，会话的 harness 记录与包内容一致——
       - memory 在 system 中；
       - skills 有索引，且有 read_skill 工具；
       - 包工具在 tools 中；
       - hook 在 manifest 中启用；需要 worker 的包，至少有一条 worker 会话事件。
     - K3b 使用，只记录、不止损：read_skill、hook、包工具的事件次数；为 0 时写入复盘。
   - segment-0 正常结束（不是暂停）且没有任何安装：停止付费，不开研究位，不跑 final；离线复盘后以负结果收尾，或调整下一个活动。
   - 连续两轮 invalid（round 0、round 1）：视为接线或提示故障，不按负结果处理；离线修好后用新 scope 重开活动。
   - 同一天出现两次基础设施暂停：不续跑，先排除供应商的问题。
7. **final。**
   - 研究位结束（或决定不开）之后只跑一次，花 `final` 分配：W0 对 W*，23 族 × repeats 2，约 $13～41；计划 hash 先提交到 git。inconclusive 也算完成。
   - 同预算的 prompt-only 或 GEPA 类对照臂，等出现含代码组件的安装后再决定是否加入。

## 4. 效果验收与最小比较

保留 C1 同底座整体迭代收益、C2 领先模型位置、C3 独立决策贡献、C4 改进方法效果。首份报告包含起点、有效 prompt/workflow、演化能力包和有界 R0/R1 比较；新训练/蒸馏及 C3 效果验证归第二版，未运行明确 not_run。包的装配、实际行为、固定 dev 比较中的退化与总成本必须可查。首版沿用现有 GatePlan 的整体收益与退化比例门槛，不把任何单题退化一律判为拒绝；进一步的回归能力等真实运行暴露具体问题后再设计，不成为本轮开发依赖。

C2 默认只引用官方榜单和上游公开结果，记录来源/日期/任务集/effort；不运行本地领先模型，不混成配对差。若将来需要同题结论，另行冻结范围和预算。

工程测试证明机制，不保证模型涨分。真实 C1、C4 仍是待证明的效果目标：未提升就记未达目标，不用“完整 RSI 不再验收”回避。最终 final 在方案锁定后按冻结协议运行；用其结果继续优化后需新的独立任务支持泛化主张。

功效与成本按 GLM-5.3 串行预期：单任务约 $0.14–0.45、约 4 分钟；train 52 + dev 25 基线约 $11–35、约 5 小时；每轮（dev gate 对照臂与候选臂各 25 题，gate 每次重跑对照臂，`ouroloop/bench.py:758-780`；另重跑 train 失败题）约 $10–40、约 5–6 小时；逐题笔记诊断每轮约 $1–2。Harness-Zero 的 h* 相对 base 增益为 SpreadsheetBench +8.0、AppWorld +21.4、USPTO +26.0 个百分点；dev 25 族时约 +20 个百分点才可能显著，SpreadsheetBench 量级在 25 族上测不出。gate 按现行规则装包：点估计 ≥ 冻结任务计划的 min_gain 且回归在容忍内即 accepted，显著性另记（`ouroloop/evolve/task_gate.py:253-269`）；GatePlan 默认 min_gain 为 0.05。preset 的 `task_plan.gate` 给出 0.05（`ouroloop/bench.py:887`），冻结任务计划（`[freeze.task_plan.gate]`）可以提高、不能低于它（`ouroloop/bench.py:2128`）；研究比较的分支没有冻结 worker gate 计划时回退 0.01（`ouroloop/bench.py:1842`、`:2316`），cycle 总是传入冻结计划，不会走到这个回退。C1 结论按“只能识别约 +20 个百分点的效应”预期；锁定后的 final（23 族）同样只看得出大效应。

## 5. 明确后置的功能

Harbor 仅在需要其原生任务/沙箱时接入，DeepAgents 仅在 source 原语确实需要时接入。首版不做新决策点大全、新训练后端、专用研究权重训练、多 agent 研究群、市场、多租户、数据库/队列、框架全仓代码自修改或 RL/DPO 平台。受限能力包代码演化属于首版，不能以此条禁止其实现。

每个模块提交说明复用位置、新增文件/依赖的必要性、错误恢复语义和对应验证。保留包名、CLI、旧身份；不复制第三方代码。完整方法链与递归核心必须完成，外围 feature 按实际缺口逐步加。
