# 跨模块集成验收（v0.6；v0.3 用例于 2026-10-04 加入）

> 首版用例大多已有离线测试，离线通过只证明接线，不是真实结果。IT6-10/19/21/23/26 仍部分实现：
> - IT6-21：活动取消入口不存在；
> - IT6-26：合并/停用分支没有测试（逐题退化记录已写入 gate 收据，2026-10-02）；
> - IT6-23：Docker 中的代码草稿检查链没有测试（3 个 Docker 隔离测试已进入 CI，2026-10-02）；
> - IT6-10：cycle 集成测试都注入脚本研究员；
> - IT6-19：补采批中途崩溃没有测试。
>
> 明细见 `history/V0_6_AUDIT_2026-10-01.md` 与 `history/V0_6_REVIEW_2026-10-02.md`。标为第二版的用例仍是待开发规格。v0.5 历史见 [快照](../history/V0_5_IMPLEMENTATION.md)。首版验收诊断输入、能力包文本/代码演化、隔离/回归/发布与递归；第二版验收教学/训练固化，旧草案用例保留原义。真实实验与 fixture 分开。
>
> v0.3（2026-10-04，[DESIGN v0.7](../DESIGN.md)、[ROADMAP §3](../ROADMAP.md) v0.3 步骤）：第一版交付物改为“可演化的 agent 程序 + 最小运行壳 + pin 的模型”，比较主张改称“同模型”（替代“同 actor”，C1～C4 含义不变）。本文件新增 IT6-29～43（§1a），v0.3 用例各标所属模块与一个 新增 测试，第二版用例不带测试，IT6-35 已撤销（2026-10-04 剪裁，DESIGN §9“考虑过、不做（v0.3）”）；§3 补 v0.1/v0.2 的事实与 v0.3 的效果目标。v0.1/v0.2 的结果与 2026-10-03/04 的记录不删（[RESULTS.md](../RESULTS.md)）。不变并在此写明的：train/dev/test 的信息分离由固定代码执行；final 先封存、提交再运行；带预留的预算账本；R0 对 R1 以下游 W* 比较；Docker 隔离加工具 broker；`claude_code` provider。

## 1. 核心闭环与必须覆盖的场景

首版必做 IT6-08/10/11/13/16/21/22～28，其中 IT6-22 的技能/经验部分在阶段 A 验收、工具/中间件代码部分在阶段 B 验收（[ROADMAP §2](../ROADMAP.md)）；IT6-17/19 的 cycle/补采恢复以 harness_edit/task_feedback 覆盖，不要求蒸馏。IT6-01～07/09/15/18/20 的新增教学/训练场景归第二版，不删用例，也不因未运行它们判首版不完整。各模块教师/SFT/Tinker 用例同样按第二版归属；已有 decision/legacy 回归仍是首版兼容要求。

v0.3 必做 IT6-29～34、36、39～41、43（v0.3，2026-10-04）；IT6-37 可选，发表结果时跑；IT6-35 撤销；IT6-38、IT6-42 与 IT6-30 里的决策点迁移归第二版。v0.3 步骤与用例的对应：步骤 (1) M0/M1 → IT6-29/30/43；步骤 (2) M3/M5 → IT6-33/34；步骤 (3) M4/M6 → IT6-31/32/39/40；步骤 (5) 多域活动 → IT6-36（发表时加 IT6-37）；导出 → IT6-41。v0.6 的用例在 v0.3 下的含义变化只在该行末尾以“（v0.3：…）”注明，原义保留。

离线可重建世界使用“更新记录并通知”，同时包含漏通知和重复发送风险。scorer 读取真实状态；worker/teacher/研究 fixture 依据请求和观察行动，禁止按候选名字返回高分。fake trainer 只验证接线，不能证明真实学习。

| ID | 场景/涉及模块 | 必须观察的结果 |
|---|---|---|
| IT6-01 | 完整蒸馏，M0/M1/M8/M2/M3/M5/M7 | 有效 source → 自动适配 → reviewed 真实执行 → 合格 SFT → artifact → 无教师 gate → 安装 → 新 session 真正加载新 actor（v0.3：reviewed 执行指教师在网关审阅 act 调用，IT6-38） |
| IT6-02 | 隐私与投影，M0/M1/M2/M8/M3 | 私有参考、被拒草稿、review 理由和 oracle 标记不进入学生输入/训练/部署；审计仍可追溯（v0.3：学生输入就是网关记录的 `messages`，M2 §1a（v0.3）） |
| IT6-03 | 分离决策，M1/M2/M3 | 同 actor 的 controller 开/关；实际 no/pushback 改变行为；误拒和额外费用也报告；标签非 teacher 自评 |
| IT6-04 | 假接线，M1/M3 | actor ref 已变但调用旧 provider，或正式评测存在 teacher，必须验收失败 |
| IT6-05 | 污染，M0/M2/M4/M7/M8 | dev/final 经摘要、前缀或材料生成输入流入 train 被拒，未发训练请求 |
| IT6-06 | 无成功/过长/不支持，M2/M5/M7 | insufficient_data/invalid 明确，无 GPU 请求；可补数据时按限额补采，无进展/缺能力才停止该段，保留报告 |
| IT6-07 | 中断和未知副作用，M5/M8/M2 | open/unknown 不清零，已确认产物复用，工具/optim step 不盲重放 |
| IT6-08 | 并发晋升/崩溃，M3/M5 | 完整 release 原子生效，stale_parent 不覆盖，恢复以 pointer 事实为准 |
| IT6-09 | 多轮 app 改进，M4/M5/M8 | W1 实际参与下一任务/采集，经验/预算衔接；不能单凭此称 R 已改进 |
| IT6-10 | 方法更新，M4/M6/M3 | 同 W/E 起点隔离比较；外测各自 W*；只安装主线 R；下一改进调用实际采用 R1（v0.3：内层 gate 仍在 dev 前一半、不少于 25 题、用合并对照，IT6-40） |
| IT6-11 | 最小 app，M3/M1 | 干净目录仅 runtime/model/tool 依赖即运行；无需 teacher/research/训练数据；托管依赖声明准确（v0.3：app = agent 包 + 运行壳 + 模型服务描述，IT6-41） |
| IT6-13 | 旧用户兼容，M0/M1/M7 | v2 hash、legacy decision CLI、旧任务/研究路径及用例不回归（v0.3：`harness_package.v1` 包仍有效，由默认 agent 运行，IT6-29） |
| IT6-15 | 零人工优化方案启动，M4/M7/M8/M3 | 只有环境、目标、初始模型、权限/预算与默认 preset；自动提出源方法、真实执行验证，再生成参考/训练方案 |
| IT6-16 | 递归伪阳性，M4/M6/M7 | 只改 manifest 或沿用 R0 请求被识别；R1 rejected/inconclusive 时不安装；不把 W 提升写成 C4 |
| IT6-17 | cycle/预算/恢复，M4/M5/M6/M7 | 一次入口跨源搜索、蒸馏和研究 slot；一个总根；重启不重复收费；不隐式跑 final |
| IT6-18 | 更换目标 h，M0/M1/M3/M4/M5/M8 | S0(h0) → 受限 B(h1) 新基线 → S1(h1)；以无环补充许可接续采集/训练；系统 gate 对 S0、学习比较对 B，CAS 仍指 S0；失败不改 active |
| IT6-19 | 自动补采与恢复，M0/M4/M5/M7 | 缺标签或轨迹返回结构化 collect → 有界采集 → 新 context → 下一提案；途中重启不重花钱/重置额度；无进展终止该段 |
| IT6-20 | 教学源与生产用途分开，M0/M3/M4/M5/M8 | train 验证即生成可用 source 并接蒸馏，零源生产 gate/安装调用；以后直接发布同一源必须另过 dev gate |
| IT6-21 | worker 段无候选但有已计划 research slot，M4/M6/M7 | 有预算时仍进入研究比较；分支内部不会因此递归开 slot；取消/全局预算耗尽则结束活动 |
| IT6-22 | 无人工优化方案、无 trainer/teacher，M0/M1/M4/M5/M7 | 从 train 失败自动生成包内工具/中间件代码及技能/经验；真实调用/阻止结束可查；同 actor gate/安装后继续下一包迭代，不只改 prompt（v0.3：可生成的还包括 agent 组件本身的改动，IT6-29） |
| IT6-23 | 候选越界与隔离，M0/M1/M5 | 包外路径/入口被拒；代码不能获得宿主仓库、工作区判分明细（grading.jsonl）、oracle、密钥或未授权基础工具；无合格 worker 时：K 允许代码或当前包运行含代码则 freeze/smoke 拒绝启动；活动中含代码草稿检查记 infrastructure_failed、当前版本运行预检记 repair_runtime，均暂停活动、不耗修订/失败额度；不 in-process exec，运行不含代码的包照常运行（v0.3：带 `agent` 条目的包“运行含代码”、在 worker 里跑；v1 包与不含代码的 v2 包由默认 agent 在宿主进程内运行；ctx 之外无任何宿主句柄，IT6-29） |
| IT6-24 | 包构建/工具/hook 中断，M1/M5/M7 | 确认的检查按输入复用；after_tool 失败保留原结果；未知复合工具副作用不重放，预算/轮次不重置 |
| IT6-25 | 包发布/导出，M1/M3/M5 | 包字节/manifest/API/image 与评测一致；损坏或 stale_parent 不安装；干净 app 保留工具/hook/材料功能与隔离依赖，不含 tests/研究材料（v0.3：导出物含 agent 组件与运行壳，IT6-41） |
| IT6-26 | 固定 dev 比较与组件停用，M3/M4/M6 | 整体收益达标且退化不超冻结上限时可采用并记录退化；超上限则拒绝、保留当前版本；合并/停用也形成候选并经同一 gate；两支代码搜索共享 API/资源但不共享新包/经验（v0.3：对照臂为合并对照、退化规则改为幅度规则、加成本项与确认复测，IT6-33/34；由归因自动提出停用候选归第二版，IT6-42） |
| IT6-27 | 诊断输入，M0/M1/M3/M4/M5/M7 | 真实 train 失败 → 失败摘要与断言明细（type/params/passed）→ 逐题笔记（failure_note.v1）→ 汇总产生 Requirement 与至多一个 patch；笔记为 train 血缘、计费，每轮读取数不超 preset 上限并记录覆盖；评分器排除的断言不给、被破坏的初始成立断言照常给；dev/final 断言明细和轨迹零进入 R 上下文，即使已存于 grading.jsonl（v0.3：R 的输入增加原始 train 轨迹只读工具、dev 失败类型直方图与账本投影，IT6-39；test 仍零进入） |
| IT6-28 | 冻结前试跑修复，M0/M1/M4/M5/M7 | 草稿检查（fixture 或仅 train 构造的世界）失败 → R 只见截断去敏的检查输出（无任务结果/评分）修订，初稿之后最多 max_draft_repairs 次，每稿新包 ref、逐次记录计费；上限内通过才冻结进 Experiment，用尽则本轮无效提案结束并计入轮次；冻结后源码不变；重启/building 按输入摘要复用已确认检查（v0.3：草稿检查对 agent 组件多一步 `run` 的隔离 import/接口 smoke，R 的 run 中抛错属草稿失败，IT6-43） |

IT6-14（此前草案“首版不得调用研究员”）撤销，不实现为当前要求；它来自已纠正的人工流水线范围。manual 入口仍可独立运行单次实验，不能据此宣布本版自动 RSI 验收完成。旧 ID 不复用成新语义。

### 1a. v0.3 新增用例（2026-10-04）

每条写所属模块、必须观察的结果，以及验收它的测试；测试都尚不存在，均标 新增。agent 组件的测试与 M1 PR7 的放同一个文件 `tests/test_package_worker.py`；其余放各模块既有文件。所有 fixture 的 fake 模型都在网关之后、作为 `ctx.model` 背后的模型出现，不再直接塞给 Agent（ROADMAP §3 彩排协议的假 actor 随之改到网关后面，§3）。

| ID | 场景/涉及模块 | 必须观察的结果 | 测试（新增） |
|---|---|---|---|
| IT6-29 | agent 组件在 worker 中经网关/broker 运行，M0/M1/M5 | manifest `api_version = "harness_package.v2"` 带 `agent/<file>.py:run`；episode → Docker worker 会话 → `agent.run(ctx, task)`，返回值即最终消息；包里每次模型调用都经 `ctx.model` 到宿主网关再到 pin 的模型，`effort` 超过 K 的 `max_effort` 被截到上限、不拒绝，请求的值与实际用的值都记录，manifest 未声明的 role 在草稿检查判结构错误；每次真实工具调用都经 `ctx.tool` 到 broker，拒绝表与权限在 broker 执行，与 agent 代码无关；ctx 之外拿不到评分器、provider、凭据、对象库、结果（探针逐项尝试并失败）；网关日志每条 `{call_id, role, effort, messages, tools, response, usage}` 与 broker 事件由宿主写，包往自己目录写的文件不出现在记录里；一个 `harness_package.v1` 包由默认 agent（`agent/default_agent.py`，今天的宿主循环移植成包）运行，在同一 fixture 上发出与 v0.6 宿主循环相同的工具调用序列；包身份覆盖 agent 代码，只改 agent 文件得到新 package/W ref | `test_package_worker.py::test_it6_29_an_agent_component_runs_in_the_worker_and_every_model_and_tool_call_goes_through_the_host`；`test_package_worker.py::test_it6_29_a_v1_package_under_the_default_agent_makes_the_same_calls_as_the_host_loop` |
| IT6-30 | 宿主不问决策点，M1 | 今天 `agent/loop.py:1-10` 列的五个决策点在 v0.3 都不由宿主问：compact 与 route 是默认 agent 内部的事，网关原样记录 agent 发来的 `messages` 与 `tools`，不压缩、不改写、不选 tier；tool_gate 留在 broker，作为 K 的权限执行（规则否决记 veto），与 agent 代码无关；stop 只有两条：agent `run` 返回即完成（termination `finished`），或任一硬限额到（`max_steps`=act 调用 50、`max_model_calls` 200、`max_tool_calls` 200、`max_seconds` 900、`max_tokens` 输出 token 含 thinking 400000）由宿主在边界计数并结束尝试，termination=`budget`、`limit=<维度>`；默认 agent 的 StopGuard 回推、turn cap、pushback cap 是它的内部事件（记 `pushback`），不是 termination；hook 的三个 site 作为默认 agent 遵守的约定保留，默认 agent 之外的 agent 不调用 hook 时，hook 事件为 0 并如实记录，不填假事件。第二版：把 stop/retry/compact/route 的决策点放到网关/broker 边界以验证 C3，v0.3 不做 | `test_package_worker.py::test_it6_30_the_host_asks_no_decision_point_the_gateway_records_what_the_agent_sent_and_stop_is_return_or_a_hard_limit` |
| IT6-31 | 引擎自有的实验账本，M4/M3/M5 | 引擎在 E 里为每个候选写一条：声明（mechanism、site、carrier、predicted_affected（train 题）、trigger_condition、differs_from）、与 diff 的核对结果、筛选结果、gate 收据 ref，以及从收据的逐题差读出的每题 fixed/regressed/still_failing（dev 题只以三者的计数进入 R 的账本投影，题号留在收据，M3 §2 的 dev 题号规则不变；train 侧筛选的题给题号）；R 每轮先读账本投影；准入拒绝 (mechanism, site) 与已被拒或 screened_out 候选相同而 `differs_from` 未写明差异的提案（`repeated_mechanism`），写明差异的放行并记下；账本不由 R 写，R 的 lessons 带证据 ref 另存 | `test_research_round.py::test_it6_31_the_engine_writes_the_ledger_from_the_declaration_the_screen_and_the_receipt_and_refuses_a_repeated_mechanism_and_site` |
| IT6-32 | 每轮 k 候选与 train 侧筛选，M4/M5/M7 | `max_candidates_per_round` 取 3（今天 `ouroloop/bench.py:1083-1086` 拒绝 ≠1，这是要改的那处）；一轮一个编码会话产出至多 3 个候选目录，各自声明 IT6-31 的六项；每个候选过草稿检查后先做 train 侧筛选：重跑它的笔记所来自的那几道失败 train 题，加一组固定的、通过的 train 题小样本（`screen.passing_sample` 5）；筛选只读 train，dev 零访问；净翻转 ≥ `screen.min_net_flips`（1）且最高的一个付 dev gate 的钱，其余记 `screened_out` 与筛选成绩，筛选题少于 3 的标 `thin_screen`，都不满足则本轮 `screened_out`；筛选费用逐候选入账；逐题笔记并发写、按冻结顺序进入汇总，上限不变 | `test_harness_loop.py::test_it6_32_three_candidates_are_screened_on_their_own_failing_train_tasks_and_only_the_best_pays_the_dev_gate` |
| IT6-33 | 合并对照与确认复测，M3/M5 | gate 的对照臂是“同一冻结计划、同一模型身份下已安装 W 的全部有效读数”合并而成（M3 §2.1(a)（v0.3）），没有时才新测一次对照，复用的对照读数保留原 TaskResult ref、不计新样本；任何 gate 不少于 `min_tasks` 25 题；一条反噪声规则（M3 §2.1(b)（v0.3））：第一次读数清过 `min_gain`（0.05 不变）与退化规则 → 同计划再测候选一次 → 候选全部读数合并对合并对照仍清过 `min_gain`、且确认那次单独的增益 ≥ 0 才安装；第一次没清过的不复测、直接 rejected；确认失败的收据 `kind="confirmed"`、verdict rejected、`metrics.first_reading` 保留第一次数字；收据种类只有 `full` 与 `confirmed` | `test_task_gate.py::test_it6_33_the_control_arm_pools_every_valid_reading_of_the_installed_version`；`test_task_gate.py::test_it6_33_a_candidate_that_clears_once_is_measured_again_and_installed_only_if_the_pooled_estimate_still_clears_and_the_confirmation_is_not_negative` |
| IT6-34 | 成本项与幅度退化规则，M3 | 候选每次有效尝试的平均费用（`cost_per_attempt`）不得超过合并对照的 (1 + β0 + β1·gain) 倍，preset β0 = 0.10、β1 = 2.0（+10%，再加每个通过率点 2%，M3 §2.1(e)（v0.3））；超出则 rejected，reason 写 cost 与两边数值；费用 unknown 按 `missing_cost_policy`，不当 0；退化规则改为幅度（M3 §2.1(d)（v0.3））：某题均值下降 ≥ `regression_drop` 0.5（1 次重复时即对照 1.0 → 候选 0.0）才计入退化，严格小于的旧规则不再用（它在 A/A 里 20% 触发）；退化比例上限与逐题清单不变 | `test_task_gate.py::test_it6_34_a_candidate_that_costs_more_than_the_control_allows_is_rejected_with_the_cost_as_the_reason`；`test_task_gate.py::test_it6_34_a_regression_is_a_drop_of_at_least_half_a_point_not_any_decrease` |
| IT6-35 | 撤销（2026-10-04） | 原草案的序贯提前拒绝已从 v0.3 剪掉（DESIGN §9“考虑过、不做（v0.3）”）；ID 不复用 | — |
| IT6-36 | 多域活动与分层划分，M7 | `[bench] domains` 取列表（上游 marketing 之外还有 sales、support、hr、finance、operations，各 100 题）；划分按域各自做再合并；新域只分 train/dev/test，按计分断言组合 × 工具族分层；Marketing 的划分原样冻结且其 test 标为已耗尽（被 v0.1 final、final-1、final-2 读过三次），新主张不得再用；并发默认 8，429/529 是未计费的重试、不是暂停；报告按域与合并各列 `cost_per_attempt`、账本、headroom（起始 W 从未通过的内层题数）；墙上时间如实记：单域 25 题 gate 约 10～15 分钟，多域合并 dev（约 125 题）约 45～60 分钟，只作记录 | `test_bench_phase.py::test_it6_36_domains_are_split_per_domain_pooled_and_stratified_while_marketings_split_is_frozen_and_its_test_is_spent`；`test_report.py::test_it6_36_the_report_lists_cost_per_attempt_the_ledger_and_headroom_per_domain` |
| IT6-37（可选） | 匹配成本的 best-of-k W0 臂，M3/M7 | 发表结果时 final 计划多一臂 best-of-k W0（M3 §2.2（v0.3）；[arXiv 2607.12227](https://arxiv.org/abs/2607.12227) 的批评）：k 由固定代码按 `k = max(2, round(cost_W* / cost_W0))` 从合并对照与 W* 的每次尝试平均费用算出并写入封存计划；语义是 oracle：W0 每题 k 次独立尝试、任一通过即通过，是同成本测试时算力的上界、不可部署；报告与 W* 并列给出 pass@k（标为上界）与各臂每题费用；永不进入 C1 的裁决；repeats 从 2 到 4 在预算允许时 | `test_paid_run_safety.py::test_it6_37_when_the_final_carries_the_optional_best_of_k_arm_its_k_is_sealed_in_the_plan_and_it_is_reported_beside_w_star_outside_c1` |
| IT6-38（第二版） | 教师在网关，M1/M2/M8 | 采集模式下只有 `role="act"` 的 response 先到教师；PASS 原样返回、REPLACE 以替换响应返回，包收到的是被接受的那条，拿不到 proposal 与理由；该条网关记录多一个 `review_ref?`；aux 不经教师；训练投影只取 PASS 的 act（M2 §2 第 5 步）；生产与 gate 模式配置了教师即启动拒绝（IT6-04 不变）。v0.3 只记 `role`，不建教师 | 第二版，无测试 |
| IT6-39 | R 作为有界编码会话，M4/M5 | 一轮一个编码会话：R 在 worker 里对包目录用文件工具编辑，≤ `research_session.max_turns`（40）轮、≤ `research_session.max_edits`（24）次编辑，上限覆盖整轮含修订，修订在同一会话续写；产出至多 k=3 个候选目录与声明文件（`finish`）；工具只有文件工具、`read_trajectory`、`finish`，无 shell、无网络；会话拿不到宿主仓库、评分器、dev/test；之后的草稿检查（形状 → 隔离 import/接口 smoke → tests）、冻结、准入不变；会话的模型调用按研究调用计费（今天占花费 3.2%）；R 的输入增加：原始 train 轨迹经只读 `read_trajectory`（单次 ≤ `research_session.max_trajectory_bytes` 65536，累计 ≤ `max_trajectory_bytes_total` 524288）；dev 失败类型直方图（只有枚举类型与计数，无 params、无题号）；账本投影；test 任何形式零进入 | `test_research_round.py::test_it6_39_r_edits_the_package_in_one_bounded_worker_session_per_round_and_the_draft_check_is_unchanged`；`test_research_round.py::test_it6_39_r_reads_raw_train_trajectories_through_a_byte_capped_tool_and_a_dev_type_histogram_without_task_facts` |
| IT6-40 | 研究位的开位条件、内层 gate 与 headroom，M6/M7 | 内层 gate 仍是 dev 的前一半、外层是后一半（如今天），但任何 gate 不少于 `min_tasks` 25 题，所以 dev ≥ 2 × 25 = 50 题才开研究位；只含 Marketing（dev 25）的活动不开位，outcome `no_slot_pool`、C4 `not_run`；内层 gate 用合并对照与确认复测（IT6-33）；`campaign.worker_round_limit` 2 → 3；开位前做 headroom 检查：起始 W 从未通过的内层题少于 `campaign.min_headroom_tasks`（6）时不问 R、不开两支，记 `no_headroom`（v0.2 的 W* 在内层 67～80% 时两支都加不出东西）；裁决只看下游 W*，两支 W* 相同仍判 inconclusive（M6 §3 不变） | `test_cycle.py::test_it6_40_a_slot_without_headroom_is_refused_before_r_is_asked`；`test_campaign.py::test_it6_40_a_slot_opens_only_when_dev_holds_two_gates_of_min_tasks_and_a_marketing_only_activity_records_no_slot_pool` |
| IT6-41 | 导出 app = agent 包 + 运行壳 + 模型服务描述，M3/M1 | 干净目录只放运行壳、已安装 release 的 agent 包（运行 bundle）与模型服务描述符，即可跑一个合法任务；运行壳仍在部署时写网关日志与 broker 事件；导出物不含 R、评分器、tests、研究材料、凭据；包字节与评测时相同，隔离镜像 digest 一致 | `test_export.py::test_it6_41_the_exported_app_is_the_agent_package_the_runner_shell_and_a_model_descriptor_and_runs_a_task_from_a_clean_directory` |
| IT6-42（第二版） | 组件归因与删除规则，M4/M3 | 第二版：以账本的逐题 fixed/regressed 把增益归到组件，连续两轮没有归因增益的组件由固定代码提出删除候选、同过筛选与 gate；v0.3 不做归因，停用候选仍由 R 自己提出（IT6-26） | 第二版，无测试 |
| IT6-43 | 失败语义，M1/M5 | worker 崩溃或 agent 在 `run` 中抛错 = 基础设施（尝试 invalid，按冻结 retry_policy 补一次，连续超过 `max_infrastructure_failures` 暂停）；在草稿检查中抛错 = 草稿失败（回传检查输出，占一次修订）；硬限额结束 = termination=budget（尝试有效，按最终状态评分）；三者各自计数，不互相冒充；`claude_code` provider 与计费不变 | `test_package_worker.py::test_it6_43_a_crash_at_run_time_is_infrastructure_a_crash_in_the_draft_check_is_a_draft_failure_and_a_hard_limit_is_budget` |

## 2. 可选接入

| ID | 条件 | 预期 |
|---|---|---|
| IT6-12 | 显式启用 Harbor，M7/M1/M5 | 原生 verifier 与结果映射一致；缺 review/snapshot 时禁相应模式；未选择时不要求其依赖 |

包内工具/中间件代码演化是首版核心；新增基础服务、宿主 API/依赖、其他 trainer 和研究权重训练按实际需求后接。actor/controller 分离与已有训练兼容保留，新增训练和 C3 效果验证归第二版。v0.3 把 agent 程序也放进可演化面，但固定的仍是：模型身份与推理配置上限、网关、broker、权限、评分器、划分、gate、预算根、每次尝试的硬限额、宿主写的记录、R 的代码与研究位（v0.3，2026-10-04）。

## 3. 真实验证与效果目标

首版 actor 原定 GLM-5.3，2026-10-03 起先跑 Claude Opus 5.5（ROADMAP §3 第 2a 步）；首轮真实 h* 演化前先运行所 pin actor 的 train+dev 无教师基线，2026-09-30 在第一个任务中途中止、无完成尝试的启动不算基线。随后先以真实 train 失败经失败摘要/逐题笔记生成文本包（技能/经验），读取/注入事件可查并经 gate 裁决；再做真实 provider 的包内代码提案、冻结前草稿试跑修复、固定隔离 worker import/接口 smoke、工具/hook 实际执行、同 actor gate/安装/干净部署与下一包迭代。真实 Docker 隔离检查不能由 fake worker 冒充，本机 Docker daemon 可用是其前提；scripted R 只能证明流程接线。

### 3a. 已经发生的（v0.1、v0.2，2026-10-04 记）

两次完整活动，各有一个自动演化的 W* 与同一 W0 在封存并提交的计划下比较（test 23 题 × 2 次，[RESULTS.md](../RESULTS.md)）：

| 活动 | W0 test | W* test | 配对差与 95% 区间 | 结论 | 费用 |
|---|---|---|---|---|---|
| v0.1（只有 segment-0） | 41.3% | 54.3% | +13.0 [+2.2, +26.1] | C1 成立 | $153 名义 |
| v0.2（完整活动，含研究位） | 47.8% | 52.2% | +4.3 [−10.9, +19.6] | C1 inconclusive | $373 名义 |
| 两次按题平均（事后，非封存计划） | 44.6% | 53.3% | +8.7 [−2.2, +20.7] | — | — |

事实，不加解释地记下：
- 工程链路在真实模型下全程走通：基线、逐题笔记、汇总、草稿检查与修订、准入、gate、安装、下一轮回用、研究位、封存 final、导出。IT6-22 的代码部分有真实记录：v0.2 segment-0 第 3 轮的代码 hook 被 gate 拒绝（−8 点、3 题退步），segment-1 第 1 轮的 on_finish 对账 hook 被接受。
- 被安装的改动都是文字提醒：v0.1 的结束前核对清单、v0.2 的写前规程与结束前对账 hook。13 个候选没有一个工具、没有一个技能。
- 同一个 W0 在两次 final 分别 41.3% 与 47.8%，相差 6 点，什么都没变：这是 23 题 × 2 次 final 的噪声底。
- C4 未测得、未被反驳：v0.2 研究位两支各两轮、内层 gate 12 题，R0 与 R1 都没有装进任何东西，两支 W* 相同，inconclusive。
- gate 的功效（2026-10-04 分析，ROADMAP §3 第 2a.9 步）：25 题 × 1 的配对差 sd 0.06～0.076，min_gain 0.05 不足 1 sd；W0 五次读数做 A/A，20 组里 4 组被“接受”；12 题内层 gate 空候选通过率约 1/3；严格小于的退步规则在 A/A 里 20% 触发；赢家诅咒 5～10 点。
- dev–test 差距：W* 修的那类失败在 dev 失败断言里占 58%、test 只占 31%；test 从未通过的 10 题里 9 题带判断类失败。
- 两个运行缺陷已修：一臂内并发（88 s → 27 s 每次尝试）；预留被拒且有调用在飞时等待（final-1 因此中止，final-2 在副本工作区重跑，test 被读过三次）。
- 业界位置（[SURVEY §3、§7](../SURVEY_RSI_FRAMEWORKS.md)）：增益在同类留出结果的 +2～+30 区间内；测量纪律领先（预注册、账本、退步容忍）；落后在搜索宽度、提案者上下文、gate 无成本项。

### 3b. v0.3 的效果目标（2026-10-04）

目标按步骤分开写，每一步说清“达到”与“未达到”各写什么；没有效果就报告未达目标，不以 fixture 或彩排补位。

1. **M0/M1（agent 组件、网关、默认 agent）：** 工程目标，不是效果目标。退出条件是 IT6-29/30/43 的测试通过，且默认 agent 在 v0.2 的 W*（`release:6cd37b8e49a4`）上重放 fixture 时发出与 v0.6 宿主循环相同的调用序列；真实模型下跑 `--phase smoke` 一题，网关日志每条可对账到 `cli_cost_usd`。
2. **M3/M5（统计与速度）：** 工程目标。用 v0.1/v0.2 已有的 W0 读数（dev 五次、test 四次）做离线 A/A 复算：新规则下（`min_gain` 0.05 不变、确认复测、幅度退化、合并对照）20 组 A/A 的“接受”数预期 ≤ 1（今天是 4）；多于 1 就回到 M3 §2.1 改规则再实现，复算得到的数字写进 ROADMAP。并发 8 下的墙上时间如实记录、不止损：单域 25 题 gate 约 10～15 分钟，多域合并 dev（约 125 题）约 45～60 分钟。
3. **M4（R 作为编码会话、账本、k 候选、筛选、输入）：** 工程目标加一个过程指标：一个活动里至少一个被接受的候选改了代码（工具封装、agent 或逻辑组件）；账本记每个候选的声明、筛选结果、gate 收据 ref 与逐题 fixed/regressed/still_failing。它不是 C1 证据。
4. **天花板实验（≈ $10～20 名义）：** 手写一个 agent 包（全量读取工具、判断前的证据表、发送前的核对、effort 到上限），在 v0.2 W* 仍失败的 4 道 dev 题与 train 失败题上跑。它回答一个问题：循环级机制能不能够到剩下的失败。能（这些题里过半被修），v0.3 的重心是搜索；不能，重心是训练准备（第二版）。两种结果都写进 ROADMAP，不预设答案。手写包不是候选，不装、不计 C1。
5. **多域活动与封存 final（根 $1,500 名义：两支各约 $220，外测约 $50；并发 8 下 1～2 天）：** 这是 v0.3 唯一的效果目标。在 Marketing 之外的新域（`[bench] domains`）上跑完整活动，final 在新域的 test 上封存、提交、运行，W0 与 W* 两臂，repeats 4 只在实测费用容得下时保留、否则写明用了 2；发表结果时另加可选的匹配成本 best-of-k W0 臂（IT6-37）。
   - C1 成立：W* 对 W0 的配对差区间不含 0；跑了可选臂时再看 W* 对 best-of-k W0：区间含 0 则写明“与同成本并行采样无法区分”，这不否定 C1，但否定“演化优于算力”的解读。
   - C1 不成立：如实报 inconclusive，列出 dev 增益、test 增益与两者的差，不并入 v0.1/v0.2 的数字做事后合并主张（事后合并只能作为探索性结果另列）。
   - C4：合并 dev ≥ 50 题才开研究位；内层 gate 在 dev 前一半（≥ 25 题）、用合并对照与确认复测、三轮；两支 W* 不同且外测（dev 后一半）配对完整时才有裁决。两支 W* 相同仍 inconclusive，写“未测得”，不写“未成立”；没开位则 `not_run`。
   - Marketing 的 test 不再用于新主张。
6. **v0.3.0：** 全套测试通过，合并进 main，开发继续在 `m1-automationbench`。

已知风险随目标一起写：可演化面变宽后 objective hacking 的面也变宽（评分器/provider 不可达、记录由宿主写，IT6-29；gate 证据从不来自包）；以算力换准确率必须过成本项（IT6-34），发表时在匹配成本下比较（IT6-37，可选）；R 作为编码会话多花研究调用（今天 3.2%，可承受），每次会话仍逐次入账。

### 3c. 付费前彩排

付费前彩排（[ROADMAP §3](../ROADMAP.md) 首个真实活动协议，2026-10-02）介于离线测试与真实运行之间：
- 做法：
  - 经 CLI 走生产 cycle，不注入研究员，也不替换 `cycle_pairs`；
  - R 由 manual provider 作答，即 Claude 读请求文件回复；
  - actor 是对包内容作出反应的确定性假 actor；
  - 任务世界是以 marketing 域名导出的小型 fixture 上游。
- v0.3 的改动（v0.3，2026-10-04）：假 actor 改为网关后面的假模型，默认 agent 作为 W0 的包运行它；另备一个 fixture agent 包（在默认 agent 上加一次 `role="aux"` 的发送前核对调用，并在指定 dev 题上据此改对），让 gate 候选臂的网关日志出现 agent 代码发出的 aux 调用与 broker 事件；注入表加三行：agent 在 `run` 中抛错 → 尝试 invalid（基础设施）；agent 在草稿检查中抛错 → 占一次修订；agent 调用未声明的 role → 草稿检查结构错误。其余做法、验收与边界不变。
- 验收：
  - 至少一次走到 gate 裁决；
  - 至少一份含 hook 或包工具的草稿在 Docker 中走完 package_check → isolated_probe → package_test，并在 gate 候选臂中被执行；
  - 安装，且下一轮满足 K3a；
  - 研究位走到裁决，accepted 时 R1 被加载；
  - 按协议中的注入表，各步骤的截断、读不出、形状非法回复分别得到暂停、coverage.invalid、本轮 invalid、占用修订或研究位 invalid；
  - 续跑不重复记账。
  - 研究位只有一次，记 invalid 后不重问：研究位读不出的注入在另一个彩排工作区里做（同样跑到 segment-0 再进研究位），主工作区的研究位用来走到裁决；截断与 ProviderError 只让研究位暂停，可以在主工作区注入。
  - 若假 actor 没能让 gate 接受，如实记为覆盖缺口：生产 CLI 路径上的安装与回用要到第一次真实运行才首次执行。
- 边界：它证明生产路径与故障协议，不证明任何模型的效果。系统不会给这些记录打 synthetic 标记，synthetic 由独立工作区（名称带 rehearsal）与 JOURNAL 标注；该工作区的报告不计入真实跑通，也不计入 C1～C4。

首个真实活动按该协议的检查点 K1～K3 与止损执行。“停止”指停在该检查点所在的步，不进入下一个付费步骤；任一检查点未过时记录原因，不以 fixture 或彩排结果补位。成本与功效预期见 [ROADMAP §4](../ROADMAP.md)：dev 25 族约 +20 个百分点才可能显著；v0.3 的 gate 口径见 IT6-33/34（M3 §2.1（v0.3））。第二版再增加适配/teacher/短 SFT/checkpoint/sampling smoke，模型与调用来源可查。

首版用冻结家族/预算比较同 actor 起点、有效 prompt/workflow、演化包；完整固定 dev 比较保留本次对照版本成功的任务，按 M3 冻结门槛判断，不要求零退化或另建回归任务池。安排有界 R0/R1 方法比较，通过才安装/回用；拒绝逻辑同样验收。第二版再比较蒸馏 app/controller。领先模型默认引用公布数字，不混作本地成绩。v0.3 起“同 actor”一律读作“同模型”：模型身份与推理配置上限不变，循环可以变（v0.3，2026-10-04）。

工程完成、真实跑通、C1/C3/C4 有效分别记录。没有效果就报告未达效果目标，不能让 fixture 必涨掩盖真实失败；没有账户/预算时将真实验证标未完成。完整 RSI 路径属于首版实现与验收，不取消为未来事项。

首版最小可复现实物：模型/运行核心身份、包源文件与 manifest/API/镜像/依赖、任务划分、失败摘要/逐题笔记与 patch 证据、草稿检查/修复记录、隔离检查、实际工具/hook/材料事件、冻结评测/回归、全部尝试/费用、安装/导出凭据、下一任务/包迭代和下一改进调用的真实装配。v0.3 另加：agent 组件源码、网关日志与 broker 事件、账本（候选声明、筛选结果、gate 收据 ref、逐题 fixed/regressed/still_failing）、筛选记录、合并对照的读数清单与确认复测收据、每次尝试费用、headroom 检查记录、各域划分的分层依据，以及跑了可选 best-of-k 臂时封存计划里的 k（v0.3，2026-10-04）。第二版另加教学/采集/数据/训练证据；单条成功轨迹或截图不能替代。
