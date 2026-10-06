# v0.6 实施路线图

> 2026-09-30 范围修订，代码基线 `b1cb7a7`。首版交付受限 harness 代码演化、应用发布与递归回用；第二版完善训练固化。仅调整相关规格，已有实现、旧 CLI 与历史证据保持原义。详细交接见 [模块目录](modules/README.md)。
>
> 2026-10-04：v0.1、v0.2 两个活动已完成并合并进 main（结果见 [RESULTS.md](RESULTS.md)，记录在 §3 第 2a 步）。同日决定 v0.3 的方向：首版交付物改为“可演化的 agent 程序 + 最小运行壳 + 所 pin 的模型”，设计版本 DESIGN v0.7；步骤、费用、风险与不变项见 §3 末“v0.3（2026-10-04 决定）”，功效预期见 §4。§1～§3 的历史条目不改。

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
        - 2026-10-03 结果（`marketing.social_engagement_response`）：
          - 第一次：7/7 通过，12 轮、26 次工具调用、74 秒。暴露两个问题。
            - 整段历史作一个文本块发送，前缀缓存对不上：写缓存 25.4 万 token、读 4.2 万，API 等价 $2.15。
            - 最后一轮的完整回复因 CLI 多要了一次结构化输出而轮数变多，被误判为截断，最终消息丢失。
          - 改为多块发送、结构化回复不按轮数判截断后重跑：7/7 通过，10 轮、24 次工具调用，正常结束且最终消息完整；写缓存 2.9 万、读 16.7 万、输出 5.1 千，API 等价 $0.37；账本与 CLI 报价逐分相等。
          - 为验证缓存另发了 3 次小调用：第二次起缓存写入从 7 千降到 3 百。
     4. 同一冒烟工作区：`ouroloop freeze` 后把打印的 `[rsi]` 行加进副本，跑 `--phase baseline --limit 2`（dev 与 train 各 2 题）。按实测校正 `[pricing]`，并按实测的每题名义费用和第 4 步的结构重定 `claude.toml` 的 `[freeze.budget]`：现在的 $300/$50 沿用 GLM 的估算，Claude 下研究位的两支与外测都不够。预算根一旦打开就不能改，所以这一步必须在活动工作区打开之前完成。
        - 2026-10-03 结果：
          - 冒烟副本的名义根须不小于一次调用的预留上界（$7.28），final 份额也是；改为 $20，其中 final $8。
          - 4 题 5 分 16 秒，无报错、无暂停、无无效尝试。dev 1/2 通过（`social_engagement_response` 7/7；`lead_enrichment` 17/18），train 0/2（`contact_data_cleanup` 13/15，`campaign_launch_checklist` 4/5）。
          - 失败都是任务判断：邮件正文措辞不合断言、用了旧的政策编号、判定不能上线而没发“ready”邮件。三题需要发的邮件都由模型正常调用 gmail 发出，与附加的账号邮箱无关。
          - 每题 API 等价 $0.22～0.50（均值约 $0.40），48～100 秒；账本 `cost_usd` 与 CLI 报价逐题相等。
          - 按均值 $0.40 估算（第 4 步的结构，研究位两支各最多 3 轮）：基线 77 题约 $31；每轮（dev gate 两支 50 题加 train 失败重跑，另有 R 调用）约 $35～40；完整活动约 $550，研究位每支约 $110，故根约 $800 时每支 15% 份额才够。只跑基线时 $300 足够，但根一旦打开不能再改。
     5. 活动工作区：`--phase cycle --until baseline` 跑 train+dev 基线（77 题），得到得分。2026-10-03 用户决定按 $300/$50 的名义根跑：只跑基线足够，但这个根容不下研究位，完整活动要另开工作区（根约 $800），基线在那里重跑。
        - 2026-10-03 结果（W0，`workspace-claude`，18:33～20:21，共 108 分钟）：
          - train 22/52 通过（42.3%），平均 partial 0.827；dev 9/25（36.0%），平均 partial 0.830。
          - 77 次尝试全部有效，没有暂停或基础设施失败。
          - 670 次模型调用，账本名义 $32.35，与 CLI 报价相等。
          - 失败多扣在最后的汇报上：train 失败断言里 gmail 占 65 条，其余为 google 15、slack 8、buffer 8、linkedin 7、hubspot 3。dev 也是如此：该发邮件时改发 Slack 或没发；新旧规则或追踪编号选错版本；数字、措辞不准。都是任务判断，不是 provider 的问题。
          - 这个根还容得下 segment-0（3 轮约 $110）与 final（约 $37，用 final 份额），研究位放不下。
     6. 同一工作区：`--phase cycle --until segment-0`，至多 3 轮。
        - 2026-10-03～04 进行中：
          - 第 1 轮：R 把失败摘要里的截断标记读成 actor 漏读，提出截断 hook，gate 拒绝（36%→40%，增益 0.04 未过 0.05）。截断标记与包 API 说明随后改正（`29fbc34`），第 3 轮起生效。
          - 第 2 轮：改 `worker.finish` 的结束前核对清单，gate 接受（dev 36%→64%，置信区间 [+0.12, +0.44]，无退步）。装包后重跑 train 失败题，30 题中 13 题通过。
          - 第 3 轮：`before_tool` 的写前核对 hook，gate 拒绝（W* 60% 对 60%，增益 0，退步 2 题）。segment-0 以 rounds_done 结束。
          - W* = W0 + 第 2 轮的结束前核对清单（`release:22cda1b62b97`）。W0 在 dev 上测了三次，都是 36%；W* 测了两次，64% 与 60%。
          - final 计划于 2026-10-04 冻结并先提交（`663f930`，`finalization:sha256:a407a850…`），随后运行。
        - 2026-10-04 final 结果：test 23 题 × 2 次，W0 41.3%、W* 54.3%，配对差值 +0.130，95% 区间 [+0.022, +0.261]，C1 成立；6 题变好、1 题变差、16 题持平；每次尝试的成本 $0.437 对 $0.425。全程 349 次尝试、3,114 次模型调用、名义 $153.30。见 [RESULTS.md](RESULTS.md)。
     7. final（对外数字）：segment-0 结束后在同一工作区跑，研究位不开（这个根放不下）。
        - 在 `claude.toml` 写 `[freeze.final]`：experiment_id；release_refs 为 W0 与 segment-0 结束时的 W*；tasks 为 test（23 题）；repeats 2；统计计划同第 7 步。
        - freeze 后先把计划 hash 提交到 git，再跑 `--phase evaluate --plan <final_plan_ref>`。
        - 规模：约 92 次尝试、约 $41 名义，用 final 份额（$50）。inconclusive 也照实报告。
     8. 版本：
        - final 结果写进结果文档与 README 的结果一节，写明模型与 effort、`claude -p` 的 JSON 工具调用、Marketing 一个领域、n 与 repeats、费用；dev 的 gate 增益有选择偏差，只作过程记录，对外数字是 final 的 test 结果。
        - 全套测试通过后，把 `m1-automationbench` 在本地合并进 main 并打版本标签（不推送，除非用户要求），之后继续在 `m1-automationbench` 开发。
     9. 下一版（v0.2）：另开工作区跑完整活动（含研究位与 segment-1），检验 R 改进自身（C4），并独立重测一次增益。
        - 配置 `examples/automationbench/claude-full.toml`：与 v0.1 同模型、同 preset 与限额；名义根 $900（按 v0.1 实测：基线约 $32、每轮约 $35～40，每支 15% 份额 $135 够三轮），final $50。
        - 运行 `--phase cycle`（不设停止点）：基线、segment-0、研究位（R0 与它提出的 R1 各从同一起点跑一段，在 dev 后半比较）、segment-1。
        - 订阅额度用完时活动暂停；包装脚本只在暂停原因是额度上限时等 30 分钟后用同一命令续跑，其他暂停原因停下等人处理。
        - cycle 结束后照第 7 步先冻结并提交 final 计划（W0 对本活动的 W*），再跑 test。
        - 预计名义约 $550～600、串行 15～20 小时，期间多半会碰到额度暂停。
        - 2026-10-04 运行中途加入一臂内并发（`[bench] concurrency = 4`，M5 §2）：基线、segment-0 与研究位的公共测量是串行跑的，两支起用新代码续跑。并发不进计划身份，每次尝试的限额与预留不变。
        - 2026-10-04 cycle 结果（04:41～14:48，含一次重启）：
          - 基线：train 25/52（48%），dev 9/25（36%，与 v0.1 的 W0 相同）。
          - segment-0：第 1 轮写前规程接受（dev 48%→80%，1 题退步）；第 2 轮发送前审计拒绝（76% 对 76%）；第 3 轮发送前比对的代码 hook 拒绝（-8 点，3 题退步）。
          - 研究位：R0 读自己的实验记录，提出 R1：提案前先说明每次被拒排除了什么，优先只在特定条件下触发的窄改动，不重提已被否定的机制。公共测量 W* 在 train 42/52。两支各两轮，内层 gate 12 题：R0 两轮拒绝（0、-8 点）；R1 两轮拒绝（0、0），其提案确实换了位置（挂在表格读取之后的提醒）。两支 W 相同，判 inconclusive，外测不跑，C4 无证据。
          - segment-1（仍由 R0）：第 1 轮结束前对账的 on_finish hook 接受（dev 68%→84%，无退步）；第 2、3 轮拒绝。W* = W0 + 写前规程 + 结束前对账（`release:6cd37b8e49a4`）。
          - 并发实测：一臂内 4 路，每次尝试墙上时间 88 秒→27 秒（3.2 倍），单次耗时不变，无无效尝试。
          - final 计划冻结并提交（`finalization:sha256:dc0c3261…`），随后运行。
          - 第一次 final（final-1）中止：W* 支 38 次有效（10/19 题通过），W0 支只有 2 次有效，51 次记 budget_exhausted。实际只花了 $23，但 4 路并发同时占住 4 份 $7.28 的预留上界，$50 的份额放不下。计量器已改为预留被拒且有调用在飞时等待（M5 §2）。预算根开了不能改，所以同一比较（同一对 release、同一 23 题、每题 2 次）以 final-2 在对象库的副本工作区（`workspace-claude-full-final`，`claude-full-final.toml`，final 份额 $120）重跑；final-1 的尝试留在记录里，与 final-2 并列报告。test 题在 final-1 里已被读过一次，但计划未因此改动。
          - final-2 结果：W0 47.8%、W* 52.2%，配对差值 +0.043，95% 区间 [−0.109, +0.196]，C1 inconclusive；4 题变好、2 题变差、17 题持平；每次尝试成本 $0.452 对 $0.570。同一个 W0 在 v0.1 的 final 是 41.3%，两次相差 6 个点，是这种 final 的噪声底。两个活动按题平均 +8.7 [−2.2, +20.7]（事后合并，不是冻结计划）。全程名义约 $373（主账本 $303、final-1 $23、final-2 $47）。见 [RESULTS.md](RESULTS.md)。
          - 以 v0.2.0 合并进 main。下一版要让研究位的内层 gate 能分辨小效应（每个 gate 更多尝试，并发之后时间上可承受），否则 C4 测不出来。
        - 2026-10-04 两份分析（离线，不调用模型）：
          - dev–test 差距：test 对 W0 并不更难（合并 44.6% 对 38.4%）。差距在 W* 修的那一类失败（汇报/编号/计数遗漏）在 dev 失败断言里占 58%、test 只占 31%；W* 在两边都消灭了这一类（dev 49→0、test 34→0 条），但 test 从未通过的 10 题里 9 题还带判断类失败。拆分：噪声约 ±5 点、gate 赢家诅咒 5～10 点、失败分布差异 25～30 点。v0.2 的 on_finish 对账 hook 在 test 上引发 2 题多发邮件而失败，并使每次尝试成本 +26%。
          - gate 功效：25 题 × 1 次的配对差值 sd 0.06～0.076，`min_gain` 0.05 不足 1 sd；W0 自身 5 次读数做 A/A，20 组里 4 组会被“接受”；12 题内层 gate 空候选通过率约 1/3；严格小于的退步规则在 A/A 里 20% 触发，加重复反而降低通过率；23 题 final 对 +5～8 点效应无论重复多少次都不显著。
          - 搜索：13 个候选全是文字提醒，无工具、无技能；R 的笔记里 26 条建议 before_tool、4 条建议技能；同一批笔记被反复复用；R1 仍重提 R0 的机制；研究调用只占花费 3.2%。
          - 业界对照见 [SURVEY_RSI_FRAMEWORKS.md](SURVEY_RSI_FRAMEWORKS.md)：增益量级在同类留出结果的 +2～+30 区间内，dev→test 收缩是普遍现象；测量纪律领先（预注册、账本、退步容忍）；落后在搜索宽度（单候选、无档案、无确认复测）、提案者上下文（只有失败摘要）、gate 无成本项。
          - 这两份分析引出的决定是 v0.3，见本节末“v0.3（2026-10-04 决定）”；第 3～7 步是首个活动的协议，保持原文。
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

### v0.3（2026-10-04 决定）

接在第 2a.9 步的 v0.2 记录与两份分析之后。设计写成 DESIGN v0.7，模块规格各自标“（v0.3，2026-10-04）”。本节是顺序、费用与止损；规则本身在模块文档里。

**决定了什么。** 首版交付物从“固定 agent 循环 + 能力包”改为“可演化的 agent 程序 + 最小运行壳 + 所 pin 的模型”。agent 程序就是包：它可以改循环本身（规划/执行结构、上下文管理、重试、结束协议）、工具封装与编排、提示、记忆、技能、hook、判断前的证据表、发送前的二次核对、K 上限内的按题 effort（超出上限的请求由网关截顶、记下请求值，不拒绝）、关键决策的多次采样。K 固定的是：模型身份与推理配置上限、计量的模型网关、工具 broker（每个真实工具调用都经它）、权限、评分器、划分、gate、预算根、每次尝试的五个硬限额（`max_steps` = act 调用 50、`max_model_calls` 200、`max_tool_calls` 200、`max_seconds` 900、`max_tokens` = 含 thinking 的输出 token 400000；触顶的尝试以 termination `budget`、`limit=<维度>` 结束）、由宿主从网关与 broker 调用写出的事件/轨迹记录（包写不了）、R 的代码与研究位。比较口径改称“同模型”，C1～C4 含义不变（[M0 §2](modules/00_CONTRACTS.md)（v0.3）、[M1 §1](modules/01_POLICY_RUNTIME.md)（v0.3））。原因：v0.1/v0.2 装上的两个改动都是文字提醒；剩下的失败集中在判断与没读到的状态，只有循环层的机制（核对回合、证据表、用工具取事实、以算力换准确）够得着；Harness-Zero 固定循环是因为它的学生要训练，而训练已移到第二版；综述里报告较大留出增益的框架（StarHarness 在 AutomationBench Finance +29、AutoSaddler +9～10）都改循环与工具（[SURVEY §4、§6](SURVEY_RSI_FRAMEWORKS.md)）。

**步骤（按序；每步先看结果，用户确认后进下一步）。**

0. **本设计（本次提交）。** DESIGN v0.7 与 M0～M7 的 v0.3 规则；不动代码与测试；v0.1/v0.2 的记录与结果保持。费用 0。
1. **M0/M1：agent 组件、网关、默认 agent、彩排。** manifest `harness_package.v2` 加 `agent` 组件（`agent/<file>.py:run`，`run(ctx, task)`，ctx 只有 model/tool/skills/limits/view）；网关与 broker 进宿主，网关把 effort 截顶在 K 的 `max_effort`（记下请求值，不拒绝）、按 act/aux 记角色；宿主今天不问任何决策点：一次尝试在 agent 返回或某个硬限额时结束，tool_gate 留在 broker 作权限（K），今天 `agent/loop.py` 里 stop/retry/compact/route 的决策都在默认 agent 内部；今天的宿主循环移植成 `agent/default_agent.py` 作种子包，v1 包因此等于带默认 agent 的 v2 包（没有代码的包由默认 agent 在进程内运行）；轨迹记录（每次模型调用 {call_id, role, effort, messages, tools, response, usage}，每次工具调用 broker 事件）由宿主写；彩排用假 actor 更新到新边界（REHEARSAL.md 另记）。退出：离线测试通过；v0.1/v0.2 的工作区与配置在新代码下可读；彩排走到 gate 裁决。费用 0（彩排不付 API 费）。
        - 2026-10-04 真实出口检查：`automationbench_harness_v3` preset 下跑 1 道 dev 题（`--phase smoke`，`workspace-claude-v3-smoke`）：7/7 通过，12 次 act 调用、24 次 broker 调用；记录为 task / agent（默认 agent，宿主内运行）/ 12 组 assistant+gateway_call / 1 条 tool_specs / 24 对 tool+broker_event / end；gateway_call 的 prefix/tail 为 (2n, 2)，与“默认 agent 只追加”一致；effort 记为 medium（pin 的值，未截顶）；API 等价 $0.41，与 v0.1 的同题冒烟（$0.37～0.43）相当。
   - **已完成（2026-10-04）**：commits `16bbbf4`（切片 A/B：包 API v2 与 `agent` 条目、五项硬上限、worker 的 run/smoke 协议）、`c44d75f`（切片 C：网关与 broker 作运行壳、默认 agent 为种子包、宿主写记录、硬上限终止为 budget、每个含代码的草稿 smoke、runner 摘要校验）、切片 D（彩排的离线检验走运行壳、v3 preset `automationbench_harness_v3`、R 的包 API 文本改为 v2、报告的网关小节、文档对齐实现；见 JOURNAL 2026-10-04 条）。退出条件已核：全套离线测试通过；彩排的离线检验在新壳下复现 §10 的全部数字，Docker 干跑在 v3 的 K 下走到 gate 裁决与 R1 装入；v0.1/v0.2 的工作区仍可读，其 K 没有 `max_effort`，继续走 v1 循环。
   - worker 镜像已按 `docker/worker.Dockerfile` 重建：宿主在每个会话的第一次 `load` 比对 runner 摘要，不同即 `StaleWorker`（M1 §1），所以 v0.3 的第一次 freeze 前镜像必须重建；v0.1/v0.2 的工作区 pin 的是旧镜像 id，它们已结束，若要 resume 需要旧镜像（不迁移）。
   - 计划 ref 因 per_attempt_budget 带全部限额而改变；没有进行中的活动受影响（2026-10-04）
   - 留给第 2 步：`trajectory_ref`、Inspection 的 `policies_read` 与 effort 逐次分布、PR7-10 的专门用例、v3 preset 的多域（第 5 步）。
2. **M3/M5：统计与速度。** 合并对照（已安装 W 在同一冻结计划与模型身份下的全部有效读数合并，没有时仍新测一次，M3 §2.1(a)（v0.3））；安装前确认复测，是唯一的抗噪规则（第一次读数清过 min_gain 0.05 与退步规则 → 同一计划下再测一次候选臂 → 确认读数单独对合并对照也清过 min_gain 才安装（规则第二版，2026-10-04；第一版写“确认读数本身 ≥0”，回放接受 4/20 后改，M3 §2.1(b)）；收据种类只有 `full` 与 `confirmed`，确认失败的是 `confirmed`、verdict rejected、保留 `first_reading`，M3 §2.1(b)（v0.3））；`min_gain` 仍是 0.05；幅度制退步规则（一题均值下降超过 0.5，`regression_drop` 0.5，M3 §2.1(d)（v0.3））；成本项 β0 0.10、β1 2.0，收据与报告同用 `cost_per_attempt`（M3 §2.1(e)（v0.3）、M5 §2（v0.3））；任何 gate 计划 `min_tasks` 25（M3 §2.1(f)（v0.3））；final repeats 2→3（定值，M3 §2.2（v0.3））；匹配成本的 best-of-k W0 臂可选，只在发表结果时跑（M3 §2.2（v0.3））；五个单次尝试硬限额取 M0 的名字与数值，termination `budget`、`limit=<维度>`，`step_limit` 只见于旧记录；并发默认 8，429/529 作未计费的重试；笔记并发写（[M5 §2](modules/05_EXECUTION_BUDGET.md)（v0.3）、[M7 §1](modules/07_BENCH_REPORT.md)）。数字来自第 2a.9 步的分析（25×1 配对差值 sd 0.06～0.076、A/A 通过 4/20、12 题内层 gate 空候选通过约 1/3、严格退步规则在 A/A 触发、赢家诅咒 5～10 点、W0 test 41.3 对 47.8）。退出：A/A 回放（W0 的 5 次 dev 读数，两两作候选对合并对照再加一次确认）在新规则下 20 组里的接受数写进记录，预期 ≤1 组，多于 1 组就回到 M3 §2.1 改规则再实现；离线测试通过。费用 0。
   - **代码已实现（2026-10-04，未提交；见 JOURNAL “v0.3 第 2 步”）**：合并对照（`Improvement.control_pool`，归属按 RunManifest 与计划条件）、确认复测（`confirm` 臂、`confirming` 状态、收据 `kind`、安装授权核对 kind）、幅度退步（`regression_drop`，按 ER6-19 取“下降超过”）、成本项（`cost_beta0/1`、`cost_per_attempt`）、`min_tasks`（freeze 拒绝）、研究位的 `no_slot_pool`/`no_headroom`（v3 preset `min_headroom_tasks` 6、`worker_round_limit` 3）、v3 preset 下并发默认 8 与 `--concurrency`、进程内在飞上限、`rate_limited` 事件、笔记并发。新字段在旧计划里缺省为 None、读作今日规则，旧计划与旧收据的 ref 不变；v3 preset 下新冻结的计划 ref 与第 1 步的不同（没有进行中的活动受影响）。未做：final 份额的并发上界检查（第 5 步前）、best-of-k 臂（发表时）、final repeats 的改动（配置值，不改代码）。全套离线测试通过。2026-10-05 评审后的 26 条修正见 JOURNAL 同日条：合并对照由“有池就不测”改为按题补齐缺少的读数（M3 §2.1(a)），`confirmation_repeats` 只取 0/1，研究位检查顺序池 → 余量 → 预算，笔记随应答即缓存，研究计量器的 429 也入记录。
   - **第一版规则的回放（2026-10-04）：退出条件未达成**：`examples/automationbench/aa_replay.py`（测试 `test_aa_replay.py`）用 W0 的 5 次 dev 读数回放——每个有向对 =（作候选的读数，作确认的读数），对照为其余 3 次读数合并——新规则（min_gain 0.05、`regression_drop` 0.5、合并对照、确认复测）**接受 4/20**，与旧规则（新测对照、任何下降即退步）的 4/20 相同；加成本项仍 4/20。四组都是 v0.2 工作区那次 48%（12/25）的读数作候选：对 36% 的合并对照第一读数 +0.12，合并后 +0.06 仍清过 0.05，确认读数（36% 对 36%）恰为 0、满足“≥ 0”。test 的 4 次读数（两次 final 各 2 次重复，23 题）12 组里新规则 2 组、旧规则 3 组。结论：确认读数“单独 ≥ 0”对一次偏高的读数没有约束力（同一 W 再读一次的期望正好是 0），合并点估计只把偏高减半；按本步的约定，规则回到 M3 §2.1 修改后再实现，本次没有改规则（M3 §2.1 末有同一记录）。
   - **第二版规则的回放（2026-10-04）：退出条件达成**。M3 §2.1(b) 改为“确认读数单独对合并对照也清过 `min_gain`”（两次独立读数各清过一次 gate；无改动时一次读数 ≥ +0.05 约 0.22、两次约 0.05 ≈ 1/20；真实 +0.10 的功效约 0.64、+0.15 约 0.88、+0.05 约 0.25），其余不动（第一读数清过 min_gain 与退步规则；退步与成本项按合并读数判；合并增益照记；`min_gain` 0.05 不变）。同一回放：**dev 0/20**（第一读数仍清过 4 组，确认读数都是 0 < 0.05），加成本项 0/20；**test 0/12**（第一读数清过 3 组，确认 +0.022、−0.043、+0.022）。第一版的 4/20 留在上一条。代码只改 `judge` 的确认条款与理由文字；安装授权、`confirming` 状态机、续跑路径、收据形状都没有依赖“≥ 0”，未改。全套离线测试通过。
3. **M4：R 作为编码会话、台账、k 候选、筛选、输入。** R 是一轮一个的有界编码会话，在 worker 里以文件工具改包目录（`research_session.max_turns` 40、`max_edits` 24，一轮内含修订合计；修订继续同一会话；工具只有文件工具、`read_trajectory` 与 `finish`，没有 shell），产出至多 k=3 个候选目录，草稿检查（形状 → 隔离导入/接口 smoke → 测试）、有界修订、冻结、准入不变；k 由 preset `max_candidates_per_round` 定（原 `bench.py` 拒绝 ≠1，2026-10-05 已改为 1..preset），每个候选声明 mechanism、site、carrier、predicted_affected、trigger_condition、differs_from，固定代码对照 diff 核对；dev gate 前先做 train 侧筛选（重跑候选笔记来源的失败 train 题加 `screen.passing_sample` 5 个固定的通过 train 题，净翻转 ≥ `screen.min_net_flips` 1 的最好候选付 dev gate，否则本轮 `screened_out`；失败集（笔记任务 ∪ predicted_affected，∩ 最新 train 测量仍失败）少于 3 题；通过样本不计的标 `thin_screen`；筛选不读 dev）；引擎自有的简单台账在 E 里（每个候选的声明、筛选结果、gate 收据 ref，以及从收据逐题差值读出的 fixed/regressed/still_failing）；准入拒绝 (mechanism, site) 与已拒候选相同而 `differs_from` 不说明差别的候选（`repeated_mechanism`）；R 的输入是原始 train 轨迹（只读、`read_trajectory` 单次 65536 字节、累计 524288）、dev 失败类型直方图（只有枚举类型与计数；无参数、无任务 id；test 永不）与台账投影；研究位：内层 gate 仍是 dev 合并清单的前一半、外层是后一半，每一半 ≥25 题，所以 dev ≥ 2 × min_tasks（50 题）才开位，不够记 `no_slot_pool`、C4 not_run（只含 Marketing 的活动如此）；`worker_round_limit` 2→3；开位前余量检查（起点 W 从未通过的内层任务少于 `campaign.min_headroom_tasks` 6 时拒绝，记 `no_headroom`）；裁决只看下游 W*（[M4 §1、§2、§5](modules/04_RESEARCH_LOOP.md)（v0.3）、[M6 §2～3](modules/06_RESEARCH_CAMPAIGN.md)（v0.3））。退出：彩排在新 R 路径上走到安装与回用检查；离线测试通过。费用 0。
   - **实现完成，退出条件部分达成（2026-10-05，未提交；见 JOURNAL “v0.3 第 3 步”及其评审、复核两条）**：`research/session.py`（会话驱动、声明核对、筛选、轮次驱动）、worker 协议的 `work` 操作（`/work` 树在 worker，模型循环在宿主）、`evolve/experience.py` 的台账行与投影、准入的 `declaration_reasons`（`repeated_mechanism`、train 限定）、`ExperimentProposal.declaration_ref`、v3 preset 的 `research_session`/`screen`/`max_candidates_per_round`（`bench.py` 的 ≠1 拒绝改为 1..preset）、报告的台账与会话小节、E0 去 dev 题号、`_reuse_fact` 认 `session` 步。实现与设计的出入写进 M4 §1/§2/§5 的“实现”段（工具多一个 `delete_file`、候选目录由首次写入建立、W 的提示文本以 `worker/*.md` 出现、`differs_from` 的形状、`thin_screen`（失败集（笔记任务 ∪ predicted_affected，∩ 最新 train 测量仍失败）少于 3 题；通过样本不计）、平局取低成本再按声明顺序、会话草稿不续跑）。**退出条件部分达成**（2026-10-05）：“彩排在新 R 路径上走到安装与回用检查”里，经 CLI、由 manual provider 作答的那次彩排**没有跑**；代之的是离线干跑（`examples/rehearsal/dry_run.py`，脚本研究员）：进程内注册 `rehearsal_v3` preset（v3 preset 按 16 题的世界调低 `min_tasks` 8、`min_headroom_tasks` 2，`[bench] concurrency = 1`，`rehearsal.toml` 本身未改），走到 segment-0 装入包工具（技能被成本项拒绝：读技能多一次模型调用）、C 的 memory 被筛出、研究位 accepted、R1 装入、segment-1 在 R1 下三轮各装一个 memory、回用 verified、两次补跑、Docker 修订回路一次；全套离线测试通过（见 JOURNAL）。阻塞点：`rehearsal.toml` 用 `marketing_harness_v1` preset，其 K 不带 `research_session`，不进会话路径；换 `automationbench_harness_v3` 则 `min_tasks` 25 容不下 16 题的 dev（两半各 8）。补齐它需要：一个按规模配好的 v3 彩排 preset（或在 preset 表上允许彩排配置调低 `min_tasks`/`min_headroom_tasks`——这是放宽，今天 `_beyond_preset` 拒绝），以及 manual provider 的回复写成工具调用（`{"text", "tool_calls": [{"name", "args"}]}`，或正文里一个 `{"tool", "args"}` JSON）。`_verify_bound` 不加 k 份筛选：筛选在冻结前，第一次筛选之前一次性查全部筛选尝试加 gate 预留（M5 §1）。
4. **天花板实验（约 $10～20 名义）。** 手写一个 agent 包（全文读取工具、判断前的证据表、发送前核对、effort 到上限），只跑 W* 仍失败的 4 道 dev 题与 train 失败题，不跑 test。它回答的是 v0.3 的重点放在搜索还是训练准备：手写包修掉大半，说明循环层机制够得着，v0.3 的搜索值得做；修不掉，说明剩余失败是模型能力，重点转到第二版的训练准备。结果只作决策记录，不进任何主张；包不安装。
   - **已运行（2026-10-05）：结论为“重心转向训练准备”（training）。** 52 次尝试全部读到（无效 0、硬限额截断 0、包崩溃 0），名义费用 $45.28（W* 臂 $17.82、每次 $0.69；手写包臂 $27.46、每次 $1.06，贵 54%）。R（W* 在本次两次都失败）= 8 题，X（手写包至少过一次）= 1 题（`featured_snippet`），B（W* 过、手写包两次都不过）= 2 题（`voice_of_customer`、`google_ads_pause_low_ctr`），E = 5；搜索需要 |X| ≥ 5 且 |X| > |B|，不满足。两臂通过率都是 23.1%（6/26）。v0.2 三次读数都失败的 4 道 dev 题本次两臂四次都失败。机制确实在起作用：手写包臂 27 份记录里 aux 调用 53 次（约每次尝试两次：证据表与完成前核对）。按失败类型：missing_write 8 题里修 1 破 1，content/count/forbidden_write 都是修 0 破 1。读法：在 W* 剩下的失败上，循环层的读取、证据表、核对与 effort 上限够不着；这些失败是同一模型的能力边界（或任务与评分的歧义），要靠第二版的训练准备。边界：一个手写包、每臂每题 2 次，题数 13；只说明 Marketing 上 RSI 之后的残余失败，不说明新领域从 W0 起步时可达的增益（v0.1 在 Marketing 上从 W0 起 +13.0）。结果与逐题表在 `workspace-claude-ceiling/ceiling/result.md`（工作区不入库）。见 JOURNAL “2026-10-05 v0.3 第 4 步：天花板实验的结果”。
   - 准备（2026-10-05，commit `882fed5`）：见 JOURNAL “2026-10-05 v0.3 第 4 步：天花板实验的准备”，做法与数字的细节在 [ceiling/README](../examples/automationbench/ceiling/README.md)。
   - 任务集（运行前冻结于 `examples/automationbench/ceiling/tasks.json`，从 v0.2 的记录读出，带来源：工作区、release、run hash、attempt id）：
     - W* = `release:6cd37b8e49a4`：`rsi/active.json` 与 `rsi/cycle.json` 的 segment-1 一致，由 `install-cb19d2b77cb2` 按 segment-1 第 1 轮的接受收据装入。
     - W* 的 dev 读数共三次：84%（装入它的那次 gate 的候选臂）、76%、76%（segment-1 第 2、3 轮 gate 的对照臂）。
     - dev 取三次里至少两次失败的题，共 **6 题**，与最近一次读数失败的题恰好相同：三次都失败的 4 题即上文的“4 道”（ad_performance_review、event_registration_sync、industry_event_tracking、seo_competitor_gap，记 `persistent`）；后两次失败、装入那次通过的 2 题（campaign_calendar、featured_snippet）。上文写 4 道，是按装入那次的读数数的；按多数读数或最近读数都是 6。
     - train 取 W* 唯一一次 train 测量（run `130aa1d0657a8f35`）失败的 **7 题**。安装后只重跑上一版失败的 train 题，所以 W* 只跑过 11 道 train 题；另 41 题最后一次是在更早的版本下通过的，W* 从未跑过。
     - 不跑 test。每题的失败按断言类型名分成 missing_write、forbidden_write、content、count；文件里只有类型与计数，没有断言参数。
   - 两臂：`wstar`（W* 的包逐字节照搬，v1 manifest 由默认 agent 运行）与 `ceiling`（手写包）。同一工作区 `workspace-claude-ceiling`，同在 v3 运行壳下，同一 pin 的 actor。两臂 effort 都是 high：W* 臂由 actor 的 pin 给出，手写包每次请求 high；v0.2 是 medium，所以 W* 在本次里重测。每题每臂 2 次，共 52 次尝试。
   - 手写包：W* 的 memory、on_finish 审计 hook 与其测试原样带上，加 `agent/ceiling_agent.py`。四个机制：
     - (a) 全文读取：`read_full` 一次至多 20 个只读 GET，全文分页返回，每页不超过 `max_output_chars`；结果被截断、带下一页标记或只有 id/摘要时，附一条读取提示（嵌套的下一页标记只在有值时算，HubSpot 每次列表/搜索都带的 `{"next": {"after": null}}` 不算）。
     - (b) 第一次写之前，用一次 `aux` 调用列出证据表，写操作对着表重发。对 search/query 路由的 POST（路径末尾为 `/search`、`:search`、`:searchStream`、`/query`、`:find`，如 Google Ads `googleAds:search`、HubSpot `contacts/search`）算读，照常执行；`:findOrCreate…` 仍算写。
     - (c) 结束前用一次 `aux` 调用逐条核对已执行的写与发送，至多两次。
     - (d) 每次调用都请求 effort high。
   - 手写包守在 K 内：五项硬限额不动；`max_output_chars` 不动（离线重放 W* 在这 13 题上的 576 次工具调用，最长输出 7,689 字符，宿主的 20,000 截断从未触发）。包 6 个文件、约 45 KB。草稿检查与 R 的候选同路径，2026-10-05 已在 Docker worker 里离线通过（评审修正后在按 Dockerfile 重建的镜像 `c7005dd5c421` 上再过一次）：形状、train 字面量、隔离导入、agent smoke、包内 9 个测试。
   - 判定规则（运行前写定）：
     - 记号：F 为冻结的 13 题，每题每臂 2 次。每个（题、臂、重复）按其最后一行读：有效尝试照其得分；因本臂自己的包崩溃而无效（运行时 agent 程序抛错，运行壳记为 `harness: AgentRunError`、failure_class infrastructure、照常补跑；或候选装不上，`candidate_defect`）算该臂失败一次——那是包的行为；因环境无效（provider、worker、预算）不算读数，补跑至多 3 次。两臂每次重复都有读数的题算“读到”。R = 读到的题中 W* 臂 0/2 的（本次仍失败）；X = R 中手写包至少 1/2 通过的（修掉）；B = 读到的题中 W* 臂至少 1/2、手写包 0/2 的（弄坏，单列）；E = W* 臂本次至少 1/2 的。
     - 判“重心在搜索”：F 全部读到、|R| ≥ 4、|X| > |R|/2（过半，|R| = 13 时为 7），且 |X| > |B|。
     - 判“重心转向训练准备”：F 全部读到、|R| ≥ 4，但不满足上一条。
     - 判 inconclusive：F 全部读到且 |R| < 4，即 W* 本次自己就过了大部分。
     - 判 incomplete：有题没读到（预算硬停，或环境失败补跑 3 次仍未成），只记数字、不下结论。判定顺序：先 incomplete，再 inconclusive，再搜索/训练准备。
     - 理由：
       - 手写包取“至少 1 次”，因为它是天花板，问的是够不够得着。
       - W* 取同一次运行里“0/2”，去掉挑失败题带来的运气成分和偶发失败，并让壳、effort、时间相同。
       - 取过半，是“修掉大半”的字面意思。
       - 要求 |R| ≥ 4：少于 4 题时，过半只是一两题，在噪声之内。
       - 要求 |X| > |B|：得失相抵的修复与噪声分不开。
       - 包自己崩溃算失败、环境失败不算读数：比较的就是两臂的代码，崩溃的尝试就是没做对的尝试；provider 或 worker 的失败与两臂无关，补跑后仍不成就不读。
     - 结论按第 5 步末的止损执行：结论为“模型能力”时，第 5 步只跑基线与 final。
     - **用户决定（2026-10-05，结论出来之后）：第 5 步照完整设计跑，含研究位，不执行上一条止损。** 理由：天花板实验只读了 Marketing 在 RSI 之后剩下的失败，新领域从 W0 起步（v0.1 在 Marketing 上从 W0 起 +13.0）；发布要的主数字是 RSI 在留出 test 上的提升，止损会让 v0.3 没有这个数字；第 3 步的 R 递归只在研究位里得到检验。天花板结论照记为“剩余失败的重心在训练准备（第二版）”。
   - 费用：
     - 估算：v0.2 的 W* 在这 13 题上每次约 $0.50（effort medium，25 次）。按 W* 臂 $0.65、手写包 $0.90 每次估算，52 次约 $40（$34～47）。
     - 根：$150 名义、硬停（2026-10-05 评审后定；初稿 $30、随后 $60 都太小）。根按预留定，不按花费定：每次调用先预留最坏情形 bound = 1M 输入 × $4/M + 163,840 输出 × $20/M = $7.2768，结算为 unknown（被杀的 `claude` 子进程、没回来的应答）的调用继续占着 bound，所以同时在飞的调用数 = floor((根 − 已花 − unknown 数 × bound) / bound)，等待预留的调用在其尝试的 900 秒里等。$60 时最后几条线（W* 的第 2 轮重复总在最后）只剩 2～3 个槽位给 8 个尝试，被 seconds 上限截断的尝试会记成 W* 失败，偏向“搜索”；一次 Ctrl-C 就可能让结论成为 incomplete。$150 时花到约 $40 仍保有全部 8 个槽位，并容得下几次 unknown（需要 150 − 7.28 × 9 = $84.5 > $40）。runner 运行前的提示按“根 − bound × (1 + 并发)”比较估算。真正的钱的护栏是关掉 extra usage 的订阅；预计花费仍约 $40（$34～47），高于本步原写的 $10～20：任务按记录是 13 题而不是 4 题，两臂各 2 次，effort high。根一开就不能改。
   - 运行：
     - 先跑 `.venv/bin/python examples/automationbench/ceiling_run.py --config examples/automationbench/claude-ceiling.toml --checks-only`。它做冻结、bootstrap、全部启动检查、任务表对划分、两个包的草稿检查与候选 W 构建；不调模型、不开预算根、不跑尝试。结果里每臂列出被硬上限截断的尝试（按维度，如 seconds）、算作失败的包崩溃与因环境未读的尝试。
     - 去掉 `--checks-only` 即真实运行。同一命令可续跑。结果写进 `workspace-claude-ceiling/ceiling/result.json` 与 `result.md`。
5. **多领域活动（根 $1,800 名义，并发 8～16 时 1～2 天）。** `[bench] domains` 列出 Marketing 与四个新领域（sales、support、hr、finance，各 100 题；上游另有 operations，本活动不用，2026-10-04 定），共五个；新领域按计分断言构成与工具族分层划分为 train/dev/test 三个分区，比例 0.5/0.25/0.25（2026-10-04 定：多给 train 无用，每轮最多读 30 条笔记；多给 test 直接换来更窄的 final 区间）；Marketing 划分原样、其 test 对新主张已耗尽（读过三次：v0.1 final、final-1、final-2），只给 train/dev 用与报告单列（[M7 §2](modules/07_BENCH_REPORT.md)）。结构同第 4 步：基线 → segment-0 → 研究位 → segment-1 → 封存的 final；研究位的内层 gate 是合并 dev 的前一半、外层是后一半（各约 62 题，≥25），余量检查在开位前；final 在新领域的 test 上，W0 对 W*，repeats 3（定值，2026-10-04 定：100 题以上题间异质性主导区间宽度，× 2 半宽约 ±6.0、× 4 约 ±5.5，取 3 作折中），计划 hash 先提交；匹配成本的 best-of-k W0 臂可选，发表结果时另加（M3 §2.2（v0.3））。估算（按 v0.2 每次尝试 $0.45～0.57；四个新领域按 0.5/0.25/0.25 得 train 200、dev 100、test 100，加 Marketing 的 train 52、dev 25，合并 train 252、dev 125、test 100）：基线 train + dev 约 377 题约 $190；每轮一个候选臂 125 题约 $60，加筛选约 $5、确认测量约 $60（只对第一次读数过门槛的）、k 个候选的编码会话与笔记约 $5～10，两段六轮约 $500；研究位两支各约 $220（三轮、内层约 62 题、含确认测量，份额按主线轮实测费用重估，M6 §2（v0.3）），外测约 $50；final 两臂 × 100 题 × 3 次约 $300。合计约 $1,500～1,700，根按 $1,800 名义开、final 份额 $400；预算根开之前按本步实测的每题费用重算（第 2a.4 步的做法）。墙上时间：8 路并发时每次尝试约 12～15 秒；多域合并 dev 的 gate 一臂约 30 分钟，一轮约 45～60 分钟（单域 25 题时约 10～15 分钟）；研究位两支并行跑；全程约 9 小时的尝试加 R 会话与限流暂停，1～2 天。并发先在 16 路试基线的头 50 题，429 重试数超过尝试数就退回 8（M5 §2（v0.3））。预算根按估算上界加一份预留上界开，开了不能改（M5 §2）。止损沿用第 6 步的 K1～K3；另加：第 4 步结论为“模型能力”时，本步只跑基线与 final，不开研究位（2026-10-05 第 4 步判为 training 后，用户决定不执行这一条，照完整设计跑，见第 4 步）。
   - **状态：已运行到第 2 阶段之后的续跑第 1 轮；无安装、未跑 final（2026-10-06）。** 第 0 阶段试点、第 1 阶段基线、第 2 阶段两轮与续跑第 1 轮的数字见下文各条与 [RESULTS v0.3](RESULTS.md)；三轮的 gate 增益 +3.2、+2.4、+0.8（125 道 dev），marketing 分别 +6、+4、+3 题，四个主张领域合计 −2、−1、−2 题；累计名义 $424.61，新领域的 test 未读，final 份额 $390 未动。原因的分析见 [history/DIAGNOSIS_FIVE_DOMAIN_2026-10-06.md](history/DIAGNOSIS_FIVE_DOMAIN_2026-10-06.md)（一个共用的包跨异质领域、只有一次对照读数的噪声、看不到伤害的筛选、R 沿用旧的“一轮一个改动”提示与误拒 20 份草稿中 14 份的字面量检查）；诊断也指出，续跑第 1 轮那条里“要过 gate 必须至少在两个领域都起作用”与“sales 总退步”约一半是单次对照读数的噪声。下一步见下文“v0.4 方向”。
   - **实现完成（2026-10-05），待运行**（未提交；见 JOURNAL “2026-10-05 v0.3 第 5 步：多领域的实现”）。全程离线：没有调用模型、没有付费阶段、没有读 `.env`。代码与模块文档的“实现”段：
     - 领域列表与划分（[M7 §2](modules/07_BENCH_REPORT.md)“实现”）：`[bench] domains`（单值 `domain` 读作一个元素的列表，旧配置的路径与 hash 不变）、`split_rule = "stratified"`、`spent`、`[bench.frozen_splits]`；每领域一份划分在 `bench/<domain>/`，合并划分（`benchmark.pool_manifests`，记下每个领域的 hash 与 spent）与结果在合并名 `marketing+sales+support+hr+finance` 下的一个目录（与 M7 原文“结果按领域存放”不同，原因写在 M7）；分层的层键是计分断言的种类集合 × 工具族（`ab.stratum`；种类集合而不是类型多重集，因为每领域 100 题有 56～98 种多重集）。
     - 启动检查：每个领域都从上游装载（装不上即点名拒绝），各领域的划分 hash 记在合并划分里并与磁盘核对，核心的 `split_ref` 等于合并 hash，每个领域一道 dev 题做评分器契约检查；v3 preset 去掉 Marketing 锁。
     - 研究位：合并 dev 在**每个领域内**对半分（按合并顺序直接切会让前两个领域全在内层），两半都覆盖每个领域、各 ≥25；余量与两半的按领域计数记进研究位（[M6 §2](modules/06_RESEARCH_CAMPAIGN.md)（v0.3，2026-10-05））。
     - final：`tasks = "test"` 只解析为新领域的 test（100 题），含 Marketing test 的计划冻结与运行都拒绝；final 份额放不下 `concurrency` × 单次上界即拒绝冻结并打印预期花费与建议份额；`--phase evaluate` 付费前打印每臂“还要跑的尝试数 × 实测每次成本”与在飞预留（[M5 §2](modules/05_EXECUTION_BUDGET.md)（v0.3，2026-10-05））。
     - 报告：每 run 按领域的通过率与 `cost_per_attempt`、final 按领域与主张行（Marketing 标 spent、不进区间）、best-of-k 未跑写 not_run、多领域 gate 收据按领域的增益、台账与余量按领域（[M7 §4](modules/07_BENCH_REPORT.md)）。
     - 测试：M7 §5 列为“第 5 步，待实现”的八个与 IT6-36 的两个、M5 的两个、报告的三个、研究位对半分一个，共 16 个新测试；全套离线测试 1112 通过（HEAD `a469dd8` 时 1096）。
   - 划分（2026-10-05 离线核对，真实上游 `4a8e106`，seed 1）：上游六个领域各 100 题（marketing、sales、support、hr、finance、operations）。Marketing 按 `frozen_splits` 复现 v0.2 的 `aa7ff7ec189c0cbb`（52/25/23，test spent）；sales `d3922dc767306d14`、support `3da11232f5d5ff38`、hr `8adf8ae514393116`、finance `4fd91eaf9fb8daf5` 各 train 50 / dev 25 / test 25，分别 8、4、7、10 层；合并 train 252、dev 125、test 123（新主张可读 100），合并 hash `5e68b39fca26a675`；研究位内层 62（各领域 12/13/12/13/12）、外层 63。
   - **费用重算（2026-10-05）：$1,800 不够，中央估计的根是 $2,800。**（已被下文“分阶段运行”取代：同日用户改为分阶段运行、K 的 effort 上限降到 medium、研究位移到另一个工作区，根按 $1,500 重算；本条留作记录。） 单价（实测）：W0（effort medium，M0 §2 的 pin）每次 $0.45（v0.2 W0 train+dev $0.409、test $0.452）；候选与 W* 每次 $0.69（天花板实验 W* 在 effort high 下 $0.685：v3 的包可按调用请求到 K 的 high）；单次预留上界 $7.2768（1M 输入 × $4/M + 163,840 输出 × $20/M），8 路在飞占 $58.21。
     - 基线：(252 + 125) × $0.45 = $170。
     - 一轮：R 的笔记与编码会话 $10（会话从未用真实模型跑过，试点量它）+ train 筛选约 36 次 × $0.69 = $25 + dev gate 125 × $0.69 = $86 = $121；第一次读数清过门槛时加确认 $86；安装后重跑上一版失败的 train 题约 131 × $0.69 = $90。
     - 主线两段六轮：6 × $121 + 2 次确认 $173 + 2 次安装 $180 = $1,080。
     - 研究位：公共输入（起点 W 在 252 道 train 与 62 道内层 dev 上的读数）$217 + 两支各（内层 62 题三轮 $233 + 一次确认 $43 + 一次安装 $90）$366 × 2 + 外测（两个 W* × 63 题）$87 = $1,036。
     - final：100 题 × 3 × ($0.45 + $0.69) = $342。
     - 合计花费约 $2,628。根要同时容下花费、一开始就预留的 final 份额、以及任何时刻 8 路在飞与两次 unknown 的预留（$58.21 + $14.55）：final 之前的花费 $2,286 + $72.8 + final 份额 $420（$342 + $72.8 = $414.8）= $2,778，取 **$2,800**；final 份额 **$420**（原定 $400 放得下 8 份上界、freeze 不拒，但放不下预期加上界）。v3 preset 的 `arm_share` 0.16（$448）、`outer_share` 0.06（$168）按这个根定（算式在 `bench.PRESETS` 与 M6 §2）。
     - 原估算 $1,500～1,700 偏低的原因：没算安装后的 train 重跑（每次约 $90）与研究位公共输入（$217）、k = 3 的筛选按 $5 而不是约 $25、候选按 $0.45～0.57 而 v3 的包可跑在 high、没算在飞预留。低情形（W* 维持 v0.2 medium 下的 $0.57、每轮一个候选、主线一次安装、研究位不安装）花费约 $1,590、根约 $1,740，$1,800 够；高情形（循环层的包像天花板手写包那样每次 $1.06）花费约 $4,400。
     - 配置 `examples/automationbench/claude-v3.toml` 按 $2,800 / $420 写（注释里有同一张表）；**开根前由用户确认根**：若定 $1,800，须同时把 `arm_share`/`outer_share` 改为 0.25 / 0.093，并接受中央情形下活动在研究位或 segment-1 因预算结束。根一开就不能改。
   - 并发试跑（手动一步，用现有的 `--limit` 与 `--concurrency`，不加新参数）：`ouroloop freeze` 之后、cycle 之前跑 `ouroloop bench --config examples/automationbench/claude-v3.toml --phase baseline --limit 25 --concurrency 16`，即基线的头 50 次尝试（25 道 dev + 25 道 train；这一步打开预算根）；报告里该 run 的 `rate_limited` 超过 50 就留在 8，否则 cycle 加 `--concurrency 16`，但只在根的余额高于 16 × $7.2768 = $116 加剩余花费时用，segment-1 与 final 回到 8（份额按 8 路定）。
   - （已被下文第 0 阶段取代：试点改为 k = 3、约 $52、根 $130。）试点（开根之前，约 $40 名义）：`examples/automationbench/claude-v3-pilot.toml`，独立工作区 `workspace-claude-v3-pilot`；领域 operations（本活动不用）+ Marketing（test 已 spent），各按分层 6/13/81 划分，合并 train 12、dev 26（gate 的 `min_tasks` 25 不能调低，所以 dev ≥ 25；< 50 时研究位本来就是 `no_slot_pool`，配置干脆 `research_slots = 0`，也免去研究位份额对小根的检查）；k = 1、K 的 `max_effort` 调低到 medium 以省钱，都不碰要验的会话路径；根 $120 硬停（4 路在飞 $29.1 + 一次 unknown 之外可花 $83）。命令：`ouroloop bench --config examples/automationbench/claude-v3-pilot.toml --phase cycle --until segment-0/round-0`；第 0 轮若 `screened_out`（没有候选进 gate），用 `--until segment-0/round-1` 再跑一轮。必须看到：`research_session` 事件（回合、编辑、轨迹读取、结束方式）与每回合带工具调用的记录（write_file/edit_file、read_trajectory、finish）；台账的 declared（mechanism、site、carrier、predicted_affected、trigger_condition）、drafted、admitted、screened（train 题、净翻转、thin_screen）行；筛选选中时有 gated 行与 gate 收据（`evaluation_completed` 事件，kind full 或 confirmed）。预计：基线 $17 + 笔记与会话约 $6 + 筛选约 $4.5 + gate $11.7 ≈ $40；第一次读数过门槛再加确认 $11.7；需要第二轮约再加 $22。它量到的每轮会话费用替换上面的 $10。
   - （已被下文的分阶段运行取代。）开根前的顺序：试点 → `ouroloop freeze --config examples/automationbench/claude-v3.toml`（打印每个领域的 hash、计数与分层，加 `[rsi]` 行）→ 16 路试跑 → `--phase cycle` → 填 `[freeze.final]`（W* 的 release）、冻结、提交计划 hash → `--phase evaluate`。
   - **分阶段运行（2026-10-05 用户决定）：每个阶段结束都停下来分析，不一次跑完约 $2,000、16 小时。** 配置 `examples/automationbench/claude-v3.toml`：活动的 schedule 改为一个 segment、6 轮、`research_slots = 0`、patience 2（`[freeze.schedule]`；`_beyond_preset` 只拒绝比 preset 多的研究位与候选数，不查轮数，preset 本身不用改）；研究位移到另一个工作区（第 5 阶段），本工作区报告的 C4 为 not_run，理由“deferred to a separate workspace”（cycle 记 `research_deferred`，报告据此写）。K 的 `max_effort` 降到 medium（W0 的 pin 也是 medium；`[harness_evolution]` 只能调低，冻进 K，`test_cycle.py::test_max_effort_is_frozen_into_k_with_a_default_of_high_and_narrows_only`）：候选不能用更高的 effort 换增益，C1 读的是 harness 而不是算力，每次尝试成本约 $0.50。运行手册（每段的命令、预计费用与时间、继续/停止的规则）：
     - **第 0 阶段：试点**（`claude-v3-pilot.toml`，约 $52，约 1 小时，开根之前）：operations + Marketing 的小子集上一轮真实主线轮，`max_candidates_per_round = 3`，使会话与活动的一样（评审第 6 条），`research_slots = 0`，根 $130。命令 `ouroloop bench --config examples/automationbench/claude-v3-pilot.toml --phase cycle --until segment-0/round-0`（第 0 轮 screened_out 时再跑 `--until segment-0/round-1`）。继续：有带工具调用的真实 `research_session`（事件的 `transcript_ref` 指向整段会话：R 的回复、每个工具调用及其参数、每个工具的结果或拒绝原因）、候选的声明、筛选行与 gate 收据，R 路径可用。停止：会话无效或崩溃、没有声明、草稿过不了检查——离线修好，不开根。按比例推到活动：R 每轮 = 试点每条笔记的费用 × 30 + 试点在 k = 3 下的会话费用；筛选每个候选 =（失败题 + 5 个通过样本）× 每次成本；gate = dev 题数 × 每次成本（试点 26、活动 125）。
       - **第 0 阶段已运行（2026-10-05）：继续。** 名义 $47.11（基线 $15.27：W0 在 12 道 train 与 26 道 dev 上，每次 $0.39～0.42；一轮 $31.84：7 条笔记 $0.90、会话 3 回合约 $0.3、筛选 11 次 $5.65、gate 26 题 $12.41、确认复测 26 题 $12.67），848 次 act 调用全在 medium，硬限额截断 0，预留全部结算。R 的会话（`transcript_ref` 已存）：读 manifest、finish 检查与包 API，写一个 memory 候选（“写之前先拿全证据”的清单：读完列表里每条记录、找最新政策、逐实体判断、解析真实 id、更新已有资源），声明 carrier memory、site memory.md、predicted_affected 为 7 道失败 train 题；k = 3 但只交了 1 个，没有读轨迹。筛选净翻转 4；gate 收据 kind confirmed：dev 0.423 → 0.692（第一读数与复测都是 +0.269），无退步，Marketing +0.31、operations +0.23，成本 $0.48 对 $0.39（允许 $0.64）；已安装。暴露的小问题：`list_files("package/")` 带尾斜杠被拒、浪费一回合——已修（读与列目录接受尾斜杠，写仍须指文件），worker 镜像按 Dockerfile 重建（runner `77b280fb…`）。按比例推到活动：R 每轮约 $0.13 × 30 条笔记 + 会话 ≈ $4（低于估计的 $10）；单价与估计相符（W0 约 $0.40～0.45、候选约 $0.48）；段 1～4 的估计不变，约 $1,200。另：试点第一次是被 `--phase cycle --checks-only` 误启动的（cycle 不读这个参数，照常付费跑），中断时 248 笔预留全部结算、花 $11.95，续跑未重付；`--checks-only` 现对 smoke 以外的阶段直接拒绝。
     - 第 1～3 阶段的操作规则（第 0/1 阶段评审，2026-10-05）：在普通终端里跑，不用有会话时限的环境（时限截断会留下 open 的预留）；只用 `--until` 停；任何中断之后、下一阶段之前，数一下 `workspace-claude-v3/rsi/budget/book.json` 里 status 为 `open` 与 `unknown` 的预留（每个占着根的 $7.2768，直到结算）。`claude` 子进程现在各在自己的 session 里，终端的 Ctrl-C 只到父进程，在飞的调用跑完并结算；超时由父进程杀掉子进程的整个进程组。
     - **第 1 阶段：基线**（约 $170，8 路约 1.5 小时；根在这里打开）：先 `ouroloop freeze --config examples/automationbench/claude-v3.toml`、把打印的 `[rsi]` 行写进配置；16 路试跑是这一段的头 50 次尝试（`--phase baseline --limit 25 --concurrency 16`，见上文“并发试跑”）；再 `ouroloop bench --config examples/automationbench/claude-v3.toml --phase cycle --until baseline`。继续：按领域的 W0 train/dev 通过率与实测每次成本；用实测成本重算第 2～4 阶段，并对照根的余额（根已开、不能改）：装不下第 3 阶段就只跑第 2 与第 4 阶段。停止：某个新领域无效尝试多（基础设施）、或实测成本让第 2 阶段加 final 都装不下。
       - **第 1 阶段已运行（2026-10-05）：继续。** 8 路并发约 1 小时 5 分钟，377 次尝试全部有效（无硬限额截断、无基础设施失败），名义 $175.97，每次 $0.466（估计 $0.45）。W0：train 104/252 = 41.3%，dev 51/125 = 40.8%。按领域 train / dev：marketing 48.1% / 28.0%，sales 34.0% / 48.0%，support 46.0% / 36.0%，hr 24.0% / 60.0%，finance 54.0% / 32.0%（每域 dev 只有 25 题，域内 train 与 dev 的差距是小样本的噪声，不说明域间难度）。账本：3 笔调用结算为 unknown（各占 $7.2768，共 $21.83），约每 1,200 次调用 1 笔，比按 v0.1/v0.2 估的每 2,250 次 1 笔多一倍。重算：第 2～3 阶段一轮约 R $4 + 筛选约 36 × $0.49 = $18 + gate 125 × $0.49 = $61 ≈ $83，第一次读数过门槛再加确认 $61，安装后 train 重测第一次约 148 × $0.49 = $72（W0 在 train 上失败 148 题）；第 2 阶段约 $370～490，第 3 阶段相近；final 100 × 3 × ($0.466 + $0.49) ≈ $287，在 $390 的份额内。根的 final 以外部分 $1,110 − 已花 $175.97 − unknown $21.83 = $912；第 2 + 3 阶段约 $800 加 8 路在飞 $58 与按新比率再约 7 笔 unknown $51 ≈ $909，刚好装下——第 2 阶段跑完按实花再定第 3 阶段跑不跑、跑几轮。
       - **第 2 阶段已运行（2026-10-05）：两轮都被拒，patience 2 停在第 2 轮，没有安装。** 名义 $170.30（累计 $346.27；中途碰订阅会话限额暂停一次，9 笔调用不计费释放，限额重置后续跑）。第 1 轮：R 13 回合、读轨迹 4 次、交 3 个候选（sendcov 净翻转 0、finishgate 草稿未过、rulesmem 净翻转 5）；rulesmem 进 gate：dev 40.8% → 44.0%（+3.2 < 0.05，拒），按领域 marketing +24、hr +8、finance +4、support −4、sales −16，退步 10 题。第 2 轮：5 回合、读轨迹 2 次、3 个候选（writemem 4、finishgap 3、unconnected 1）；writemem 进 gate：+2.4（拒），marketing +16、finance +4、hr 0、support 0、sales −8，退步 6 题。**查到的原因：** R 的笔记按合并后的冻结顺序取失败题前 30 道、逐轮轮转（`diagnosis.select_failures`），合并顺序里 marketing 在前、sales 其次——第 1 轮的 30 条笔记是 marketing 27 + sales 3，第 2 轮全是 sales；support、hr、finance 的 148 道失败里 R 一道没看到。候选于是照着 marketing（与 sales）的失败写，在 marketing 上涨得多，在别的领域持平或退步。这是第 5 步多领域实现的缺陷（单领域时不存在），不是 RSI 够不着的证据。按事先的停止规则（没有安装就停、不跑 final），本阶段到此。
       - **续跑第 1 轮（整体第 3 轮，2026-10-06）：被拒。** 笔记按领域分层（每域 6 条，12 条复用缓存）、R 看得到各领域的 gate 得失；R 7 回合、读轨迹 3 次、交 3 个候选（withhold 净翻转 1、ledger 0、sendcheck 未进筛选）；withhold 进 gate：dev 40.8% → 41.6%（+0.8，拒），marketing +12、finance 0、support 0、hr −4、sales −4，退步 7 题。名义 $78.34，累计 $424.61；final 以外的根余额约 $663。三轮的 gate 增益 +3.2、+2.4、+0.8，三次都是 marketing 涨（+24、+16、+12）、别的领域持平或退步：一个只在 marketing 上起作用的改动在合并 dev 上最多约 +2.4～4.8，过不了 0.05，要过 gate 必须至少在两个领域都起作用。
     - **第 2 阶段：前三轮**（`--phase cycle --until segment-0/round-2`，约 $400，3～4 小时）：停止：三轮都没有安装——停，不跑 final（主张会是 W0 对 W0）。
     - **第 2 阶段之后：修正与续跑（2026-10-06，用户决定）**：修好多领域的笔记选择后，在同一工作区续跑至多 4 轮（基线复用、根不变、test 不动），偏离冻结 schedule（worker_rounds 6、patience 2，segment-0 已因 patience 在第 2 轮结束）的做法如实记录。
       - 修正（离线，见 JOURNAL “2026-10-06 v0.3 第 5 步：笔记按领域分层与续跑”）：(1) 失败跨几个领域时笔记按领域轮流选，每个领域内照旧“从未纳入的在前、再按最近一次纳入从久到新、同序按冻结顺序”，上限 30、五个领域时各 6 题；单领域时选择与以前逐字相同（M4 §1）。对本工作区离线核对：W0 的 148 道 train 失败为 marketing 27、sales 33、support 27、hr 38、finance 23，第 2 轮的 30 条笔记各域 6 条，其中 12 条（marketing 与 sales 各 6，最久以前纳入的）按身份复用缓存，18 条新写。(2) R 读的台账投影里 gate 多出每个领域的 fixed/regressed/still_failing 题数与两臂 dev 均值及差值（只有计数与均值，没有 dev 任务 id）：前两轮现在读作 rulesmem marketing +24、hr +8、finance +4、support −4、sales −16，writemem marketing +16、finance +4、hr 0、support 0、sales −8（M4 §1 台账投影）。(3) 显式续跑：`--phase improve --rounds N --reason "…"` 对已结束的段起作用，在 `rsi/events.jsonl` 记 `schedule_extended`（段、by、理由、轮数、从第几轮起、冻结的 worker_rounds 与 patience、累计加了几轮），重新打开段的轮次记录，在同一任务计划、预算根与 K 下跑 N 轮，轮次、诊断序号与候选 id 接着（第 2 轮、`d2`）；合并对照照旧读已有读数，不补测；patience 在每次续跑内重新计数，所以 `--rounds 1` 恰好跑一轮；报告的能力包一节列出每次续跑（M7 §1）。
       - 每轮的命令（一次一轮，跑完停下分析）：`.venv/bin/ouroloop bench --config examples/automationbench/claude-v3.toml --phase improve --rounds 1 --reason "2026-10-06: R's notes are now stratified by domain (stage 2 noted only marketing and sales); continue segment-0 after its patience stop, one round at a time (user decision)"`。中断后同一命令续这次续跑（不新开、不再记事件）；碰订阅限额暂停时同上。每轮跑完：看 gate 收据的按领域增益、台账、会话记录与账本里 open/unknown 的预留，再决定下一轮。
       - 费用（按本工作区第 2 阶段的实测，`rsi/budget/book.json`）：两轮的笔记与会话共 $10.70（每轮约 $5.35）、筛选 $35.94（每轮约 $18）、gate 候选臂 250 次 $123.67（每次 $0.495，每轮约 $62）。续跑一轮约 **$85**；第一次读数清过门槛时加确认复测约 $62（约 $147）；装上之后下一轮开头重测上一版失败的 train 题约 135 × $0.49 ≈ $66。根的余额：$1,500 − final 份额 $390 − 已花 $346.27 − 3 笔 unknown $21.83 = $741.90，扣 8 路在飞 $58.21 后约 $684 可用；4 轮在常见情形（2 次确认、1 次装入）约 $534，装得下；4 轮都过第一次门槛且装两次约 $728，超出——那时停在第 3 轮。
       - 停止规则不变：续跑的 4 轮里没有安装就停、不跑 final；有安装就按第 4 阶段封存 final（W0 对 W*）。
     - **第 3 阶段：第 4～6 轮**（`--until segment-0/round-5`，约 $400，3～4 小时，可选）：第 2 阶段有安装且余额装得下本阶段与 final 才跑；patience 2 可能让 segment 提前结束。
     - **第 4 阶段：封存的 final**（约 $285，约 2.5 小时）：`[freeze.final]` 填 W* 的 release（`rsi/active.json`），`ouroloop freeze`（封存前把计划的预期花费 + 8 路在飞 + 2 个卡住的预留与 final 份额比较，放不下就拒绝封存，评审第 3 条），提交计划 hash，`--phase evaluate`（付费前再比一次份额余额，放不下就拒绝，评审第 2 条）；100 道新领域 test × 3 次，W0 对 W*；结果照报。
     - **第 5 阶段：研究位（C4，可选，之后再定）**：照 v0.2 的 final-2，在工作区的一份拷贝里用自己的根跑（新的 `scope_id`、`research_slots = 1`、`--until slot-0`）；公共输入复用主线对起点 W 的读数（评审第 1 条，W0 时 train 与内层 dev 全部复用），开位前的钱检查含公共输入的估算。份额按那个根重算（`bench.PRESETS` 注释：一支 $346、外测 $142，$1,000 的根时 0.35 与 0.15）。离线没有跑过这条路径，开始前再核对。
   - **费用重算（2026-10-05，effort medium）**：单价 W0 每次 $0.45（v0.2 W0 在 medium 下 train+dev $0.409、test $0.452），候选与 W* 每次 $0.50（medium；v0.2 的 W* 为 $0.565）；单次预留上界 $7.2768。
     - 第 1 阶段（基线）：(252 + 125) × $0.45 = $169.65。
     - 一轮：R 的笔记与 k = 3 的会话 $10 + train 筛选约 36 × $0.50 = $18 + dev gate 125 × $0.50 = $62.5 = $90.5；第一次读数过门槛时加确认 125 × $0.50 = $62.5；安装后重跑上一版失败的 train 题约 131 × $0.50 = $65.5。
     - 六轮：6 × $90.5 + 2 次确认 $125 + 2 次安装 $131 = $799（第 2、3 阶段各约 $400）。final 之前的花费 $968.65。
     - 第 4 阶段：100 × 3 × ($0.45 + $0.50) = $285。合计花费约 $1,254。
     - 预留：8 路在飞 $58.21；结算不回来的调用（被杀的 `claude` 子进程、没回来的应答）一直占着预留——v0.1/v0.2 与天花板实验约 9,900 次调用里留下 4 个，约每 2,250 次一个。按 v0.2 基线每次尝试约 8.6 次调用，第 1～3 阶段约 1,855 次尝试加约 420 次 R 调用，约 16,400 次调用 → 8 个卡住的预留 $58.21；final 约 5,160 次调用 → 3 个 $21.83。
     - final 份额 **$390**（第 0/1 阶段评审后由 $370 改）：预期情形是 $285 + 8 路 $58.21 + 3 个卡住 $21.83 = $365.04；$390 还容得下更贵的 W*：390 − 8 路 58.21 − 2 个卡住 14.55 = $317.24，两臂各 300 次，即 W0 + W* 每次 ≤ $1.057，W0 $0.45 时 W* 可到 $0.607——高于 v0.2 实测的 W* $0.565，也高于 gate 在增益 0.10 时的成本上限 $0.45 × (1 + 0.10 + 2.0 × 0.10) = $0.585。封存前的检查按“预期 + 8 路 + 2 个卡住”判。根 = $968.65 + 8 路 $58.21 + 8 个卡住 $58.21 + final 份额 $390 = $1,475.07，取 **$1,500**。`[estimate] cost_per_attempt = {base = 0.45, evolved = 0.50}` 是封存检查在臂还没有计价读数时用的计划数字，不是限额。
   - **评审修正（2026-10-05，见 JOURNAL “v0.3 第 5 步：分阶段运行与评审修正”）**：(1) 研究位开位的钱检查加上公共输入的估算（`bench.common_inputs_estimate`），公共输入复用主线对起点 W 的有效读数（同一执行身份；W0 时全部复用，已装 W 时复用它已有的 train 读数；研究位的 release 带上主线的宿主契约，执行身份才相同）；(2) final 的估算按执行身份（W 与 K）找读数，找得到 W* 在 gate 里的读数；份额余额放不下时 `--phase evaluate` 在付费前拒绝；(3) `ouroloop freeze` 封存 final 计划前打印并检查估算（读数优先，否则 `[estimate]`）；(4) 构建前的验证预留加美元项：尝试数 × 父版本实测每次成本 × (1 + β0)，筛选同价，付不起的 gate 不开始；(7) 多领域 gate 收据的 `metrics.per_domain` 有了收据级测试；(8) 启动后改了领域列表的活动在启动检查里拒绝。

6. **v0.3.0。** 全套测试通过，`m1-automationbench` 本地合并进 main 并打标签（不推送，除非用户要求），之后继续在 `m1-automationbench` 开发。final 结果写进 RESULTS.md 与 README：模型与 effort、`claude -p` 的 JSON 工具调用、领域列表、n 与 repeats、两臂（发表时加跑 best-of-k 臂则三臂）、`cost_per_attempt`、Marketing test 的 `spent` 标注。
   - **v0.3.0：结果写入 RESULTS/README，合并进 main（2026-10-06）。** 第 5 步没有安装，所以没有 final 可写：RESULTS 的 v0.3 一节写的是天花板实验、试点、五领域活动（基线与三轮）、诊断与费用（三项实验合计名义 $517.00），总表加一行“v0.3：无 final”，v0.1/v0.2 的数字不变；两份 README 的结果段与能力清单加 v0.3。见 JOURNAL “2026-10-06 v0.3.0 发版”。

### v0.4 方向（2026-10-06）

v0.3 的五领域活动三轮都没有安装（第 5 步；[诊断](history/DIAGNOSIS_FIVE_DOMAIN_2026-10-06.md)）。下一版的方向是把搜索按领域分片并加路由，设计在 v0.3.0 合并之后于 `m1-automationbench` 上写进 `docs/design/`；顺序、费用、止损与规则都以那里为准，本节只作指针。（2026-10-06 更新：用户批准的是更简单的方案——每个新领域一个单领域活动，不写路由；见下一节“v0.4（2026-10-06 决定）”与 [design/V0_4_PLAN.md](design/V0_4_PLAN.md)。分片与路由的设计稿和冻结孪生方案都已标为被取代。本节下面的风险、不变项与推迟项仍是 v0.3 的记录。）

**已知风险。**
- 目标劫持面变大：包能改循环，就能学会在 on_finish 里短路核对、在证据表里编造。对策是评分器与 provider 对包不可达、事件/轨迹由宿主写、包拿不到 objects/results 句柄（M0 §2（v0.3））；gate 证据只读宿主记录。
- 以算力换准确：按题 effort 与多次采样会抬成本。对策是 gate 的成本项；发表结果时加跑匹配成本的 best-of-k 臂（M3 §2.2（v0.3）），W* 不优于它时 C1 的解释写明“增益可由算力解释”。
- R 作为编码会话花更多研究调用：今天研究调用占花费 3.2%，翻几倍也可承受；上限由 preset 的轮数与编辑次数固定。
- harness 与模型共适应：在 Opus 上演化出的循环未必迁到 9B 学生；第二版学生的 W0 是更简单的种子，h* 在学生上重新演化（[M2 §1～3](modules/02_EVIDENCE_TRAINING.md)（v0.3））。
- 多领域的分层划分不能按成绩调：划分在基线前冻结，只用任务定义的静态特征。

**不变的部分（须说明）。** train/dev/test 的信息分离在固定代码里（判分明细按冻结 split 过滤，dev 只给枚举直方图，test 不进 R）；final 封存、计划 hash 先提交；预算账本与预留（M5 §2，含并发下的预留上界）；R0 对 R1 由下游 W* 裁决（M6 §3）；Docker 隔离与工具 broker（M1 §1）；`claude_code` provider 与计量（M1 §1、第 2a 步）。

**第二版的训练兼容（计划，不实现）。** v0.3 实现的只有网关记录里的 `role` 字段。其余是第二版计划：训练投影只读网关日志里 role=act 且教师 PASS 的调用；教师在网关对 act 调用介入（PASS/REPLACE，回复到包之前），对包透明；aux 调用计采集限额但不进训练数据；顺序为 演化 → 冻结 release → 在该 release 上采集/训练 → 换学生 → 在学生上重新演化（[M2 §1～3](modules/02_EVIDENCE_TRAINING.md)（v0.3））。

**推迟到第二版（v0.3 只留一段说明，无字段表、无测试）。**
- 把决策点（stop/retry/compact/route/tool_gate）移到网关/broker 边界供 C3 使用；v0.3 的宿主不问任何决策点，tool_gate 留在 broker 作权限（K）。
- 网关处的教师、从网关日志出发的训练投影、演化→冻结→训练→换学生→再演化的 schedule 与共适应说明（上段）。
- 组件归因与弃用规则。

**考虑过、不做（v0.3）。**
- 候选臂的序贯早停拒绝（`early_rejection`、`rejected_early`、`stopped_early`、min_pairs/net_deficit）：只省候选臂的钱，多一套规则与收据。
- 台账的逐题 TRUE FIX / TRUE REGRESSION / STOCHASTIC 标签、`ledger.min_control_readings/stable_fail/stable_pass` 与预测命中/落空计分板：台账只记声明、筛选、收据 ref 与收据读出的 fixed/regressed/still_failing。
- 成功透镜摘要（`success_digest`）。
- `require_code_candidate`（及 `require_code_change_per_segment`）：不强制每段改代码。
- 台账数字作研究位的次要指标（`metrics.ledger.*`）：裁决只看下游 W*。
- 收据的 `null_acceptance`、`noise_sd`、`acceptance_rule`/`two_stage`/`stage1_min_gain` 与 `min_gain` 0.12 预设：`min_gain` 保持 0.05，确认复测是唯一的抗噪规则。
- `research_dev` 分区与 `no_outer_pool`：外层仍是合并 dev 的后一半，dev <50 题记 `no_slot_pool`。
- 部分得分作为新列项：它已在报告里，不另说。
- best-of-k 臂“成本比 <1.5 时不需要”的规则：臂本身可选，只在发表时跑。
- 提案者的组件种类优先顺序作为规则。

### v0.4（2026-10-06 决定）

规则在 [design/V0_4_PLAN.md](design/V0_4_PLAN.md)；本节是顺序、费用与止损。v0.4 回到 v0.1 的形状——每个新领域一个单领域活动——并修掉诊断找到的五个框架缺陷（R 的输入与旧提示、字面量误拒、看不见伤害的筛选、只有一次读数的对照），final 并列等成本参照。

**步骤（按序；每步先看结果，用户确认后进下一步）。**

0. **文档（$0）。** V0_4_PLAN、本节、JOURNAL 条目；两份被取代的设计稿顶端加一行说明；受影响的模块文档（M0 §3、M3 §2.1–2.2、M4 §1–2、M5 §1、M7 §2、§4）各加一段“（v0.4，2026-10-06）”。
1. **代码与测试（$0，离线）。** V0_4_PLAN §1 的八项，每项有测试（去掉改动测试就失败）；字面量检查回放 `workspace-claude-v3` 的 20 次草稿检查（只读），14 次误拒归 0、单题的值照旧被拒；两份配置 `claude-v4-finance.toml`、`claude-v4-support.toml` 的划分 hash 与 v3 工作区冻结的相同（finance `4fd91eaf9fb8daf5`、support `3da11232f5d5ff38`）。退出：全套离线测试在前台通过。
2. **finance 活动（约 $250，可选，用户决定开始）。** `examples/automationbench/claude-v4-finance.toml`，工作区 `workspace-claude-v4-finance`（新根 $450，其中 final 份额 $160）。付费前：`claude auth login`；按 `docker/worker.Dockerfile` 重建 worker 镜像；`ouroloop freeze --config examples/automationbench/claude-v4-finance.toml` 打印划分 hash（须为 `4fd91eaf9fb8daf5`）与要加的 `[rsi]` 三行。
   - **2a 基线**（`--phase cycle --until baseline`，约 $58）：W0 的 train 50 + dev 25 × 3 次读数（`[bench] control_readings = 3`，无条件，在任何 gate 之前）。
     - **finance 2a 已运行（2026-10-06）：继续。** 125 次尝试全部有效，名义 $57.92（dev 每次 $0.43～0.45，train $0.50），2 笔限流调用不计费释放，没有卡住的预留。W0：train 25/50（50%）；dev 三次读数 9、8、9 道（36%、32%、36%）。逐题看，25 道 dev 里 16 道三次都失败、8 道三次都通过、只有 1 道不稳（2/3）——finance 的重跑噪声远小于五领域的估计（不一致率约 12%），对照几乎不抖，gate 能看清 +2～3 题的真实变化；16 道稳定失败的题就是余量。
   - **2b 3 轮**（`--phase cycle`，约 $120；一次确认加 $12、一次安装加 $11）：一段 3 轮、patience 2、k = 3。每轮之后读过程检查（免费）：字面量误拒为 0；每个候选都有准入认可的轨迹读取（失败的在其 `predicted_affected` 里，加一条通过的）；3 轮里至少一个工具或 hook 候选到达筛选。不成立就停下离线修，不付下一轮。
   - **2c 封存 final**（只在有确认的安装时，约 $72）：`[freeze.final]` 填 W\* 的 release，`ouroloop freeze` 封存并提交计划 hash，`--phase evaluate`：finance 的 25 道 test × 3 次，W0 对 W\*；报告并列 W0 的 pass@k（k = 1～3）作等成本参照。
3. **support 活动（约 $250，同样的形状）。** `claude-v4-support.toml`，划分 hash 须为 `3da11232f5d5ff38`。
4. **读法（预注册）。**
   - 某领域有确认的安装 → 该领域封存 final，结果照报。
   - 3 轮没有安装 → 该领域停止，如实报告“R 以 k = 3、接口优先的方法在 3 轮内没有在该领域得到确认的安装”；不续跑同一条管线。
   - 某领域的 final 显著（95% 区间下界 > 0）→ 加 sales 与 hr，各自一个单领域活动，同样的配置形状。
   - 不做：不重判 v0.3 的三张收据；不在边缘读数后追加读数或重复；不给 R dev 题号或 dev 轨迹；不读 Marketing 已 spent 的 test。
5. **v0.4.0。** 有结果（含负结果）后写 RESULTS 与 README，合并进 main。

**功效（如实写）。** 单领域 gate 读 25 道 dev：`min_gain` 0.05 约 1.25 题，单次读数噪声约 ±7～9 个点；抗噪靠三次对照读数的合并池与确认复测（复测单独也须清过 `min_gain`）。final 25 × 3 次时最小可检出效应约 15 个点（§4 的口径）；StarHarness 在 Finance 上报的 +29.3 若有一半留下来就看得出，+5～8 个点看不出。

## 4. 效果验收与最小比较

保留 C1 同底座整体迭代收益、C2 领先模型位置、C3 独立决策贡献、C4 改进方法效果。首份报告包含起点、有效 prompt/workflow、演化能力包和有界 R0/R1 比较；新训练/蒸馏及 C3 效果验证归第二版，未运行明确 not_run。包的装配、实际行为、固定 dev 比较中的退化与总成本必须可查。首版沿用现有 GatePlan 的整体收益与退化比例门槛，不把任何单题退化一律判为拒绝；进一步的回归能力等真实运行暴露具体问题后再设计，不成为本轮开发依赖。

C2 默认只引用官方榜单和上游公开结果，记录来源/日期/任务集/effort；不运行本地领先模型，不混成配对差。若将来需要同题结论，另行冻结范围和预算。

工程测试证明机制，不保证模型涨分。真实 C1、C4 仍是待证明的效果目标：未提升就记未达目标，不用“完整 RSI 不再验收”回避。最终 final 在方案锁定后按冻结协议运行；用其结果继续优化后需新的独立任务支持泛化主张。

功效与成本按 GLM-5.3 串行预期：单任务约 $0.14–0.45、约 4 分钟；train 52 + dev 25 基线约 $11–35、约 5 小时；每轮（dev gate 对照臂与候选臂各 25 题，gate 每次重跑对照臂，`ouroloop/bench.py:910`；v0.3 起对照改为合并读数，M3 §2.1(a)（v0.3）；另重跑 train 失败题）约 $10–40、约 5–6 小时；逐题笔记诊断每轮约 $1–2。Harness-Zero 的 h* 相对 base 增益为 SpreadsheetBench +8.0、AppWorld +21.4、USPTO +26.0 个百分点；dev 25 族时约 +20 个百分点才可能显著，SpreadsheetBench 量级在 25 族上测不出。gate 按现行规则装包：点估计 ≥ 冻结任务计划的 min_gain 且回归在容忍内即 accepted，显著性另记（`ouroloop/evolve/task_gate.py:253-269`）；GatePlan 默认 min_gain 为 0.05。preset 的 `task_plan.gate` 给出 0.05（`ouroloop/bench.py:1041`），冻结任务计划（`[freeze.task_plan.gate]`）可以提高、不能低于它（`ouroloop/bench.py:2552`）；研究比较的分支没有冻结 worker gate 计划时回退 0.01（`ouroloop/bench.py:2141`、`:2774`），cycle 总是传入冻结计划，不会走到这个回退。C1 结论按“只能识别约 +20 个百分点的效应”预期；锁定后的 final（23 族）同样只看得出大效应。以上是 v0.1/v0.2 的口径，实测见 §3 第 2a 步。

（v0.3，2026-10-04）功效按 v0.1/v0.2 的实测重算，单位是通过率的百分点：
- 噪声底：25 题 × 1 次的配对差值 sd 0.06～0.076，即每题配对差值的 sd 约 0.30～0.38；同一 W0 两次 23 题 × 2 次的 final 相差 6 点。
- 单领域（Marketing）：dev 25 × 1 的最小可检出效应（双侧 95%，功效 80%）约 20 点；test 23 × 2 约 15 点；+5～8 点的效应无论重复多少次都不显著（第 2a.9 步）。这是 v0.1 的 +13 显著、v0.2 的 +4 不显著的原因，不是两次活动找到的改动差了三倍。
- 多领域（§3 “v0.3” 第 5 步）：100 题 × 2 次时配对均值的 sd ≈ 0.34/√2/√100 ≈ 0.024，最小可检出效应约 7 点；100 题 × 4 次约 5 点。v0.3 的 C1 结论按“能识别约 7 点的效应”预期；主张区间只用新领域的 test，Marketing 的 test 单列为 `spent`。
- gate：`min_gain` 仍是 0.05（25 × 1 时约 1 个净任务，不足 1 sd）；抗噪靠安装前的确认复测（确认读数单独也过门槛，第二版 2026-10-04；第一版的“≥0”回放接受 4/20）；合并对照；幅度制退步与成本项（β0 0.10、β1 2.0）（M3 §2.1(a)(b)(d)(e)（v0.3））。dev 上的增益仍有选择偏差（赢家诅咒 5～10 点），只作过程记录，对外数字是 final。
- 匹配成本的对照臂（可选，发表结果时跑，M3 §2.2（v0.3））：W0、W* 之外加 best-of-k W0（oracle 语义，k = max(2, round(W* 与 W0 的 `cost_per_attempt` 之比))）。W* 对 best-of-k 的差值是“演化找到的方法比同样多算力的多次采样强多少”，W* 对 W0 的差值仍是 C1；两个区间都报，W* 不优于 best-of-k 时 C1 的解释写明“增益可由算力解释”。
- 研究位：内层 gate 是合并 dev 的前一半（≥25 题，dev ≥50 才开位）、合并对照、开位前余量检查；两支的差异先要在内层 gate 上分得出（25 题时最小可检出效应约 20 点，多域约 62 题时约 12 点），否则 C4 仍是“未测出”而不是“否定”。

规则以 [M3 §2.1～2.2](modules/03_EVALUATION_RELEASE.md)（v0.3）与 [M6 §2～3](modules/06_RESEARCH_CAMPAIGN.md)（v0.3）为准，报告字段见 [M7 §4](modules/07_BENCH_REPORT.md)。

## 5. 明确后置的功能

Harbor 仅在需要其原生任务/沙箱时接入，DeepAgents 仅在 source 原语确实需要时接入。首版不做新决策点大全、新训练后端、专用研究权重训练、多 agent 研究群、市场、多租户、数据库/队列、框架全仓代码自修改或 RL/DPO 平台。受限能力包代码演化属于首版，不能以此条禁止其实现。

每个模块提交说明复用位置、新增文件/依赖的必要性、错误恢复语义和对应验证。保留包名、CLI、旧身份；不复制第三方代码。完整方法链与递归核心必须完成，外围 feature 按实际缺口逐步加。
