# M3：无教师评测、完整发布与部署投影

> 目标：验证部署时真正保留的能力，并让评测通过的完整版本实际生效。复用 `evolve/task_eval.py`、`evolve/task_gate.py`、`release.py`；不新建模型晋升平台。

首版发布对象是现成 actor 加演化能力包；正式 gate/安装/导出不依赖教学来源验证或训练。source_validation 与蒸馏相关流程保留为第二版用途。

v0.3 起（2026-10-04，DESIGN v0.7）发布对象改为**可演化的 agent 程序 + 最小运行壳 + pin 的模型**：能力包含 `agent` 组件（M0 §2（v0.3）：manifest `api_version = "harness_package.v2"`，入口 `agent/<file>.py:run(ctx, task)`，宿主经 `ctx.model`/`ctx.tool` 代它调用 pin 的模型与 broker），可以改循环本身、工具封装、提示、记忆、技能、hook、判断前的证据表、发送前的复核、K 上限内的 effort 与关键决策的多采样。比较主张的措辞由“同 actor”改为“同模型”（业界用法），C1～C4 含义不变。原因：v0.1/v0.2 装入的两处改动都是文字提醒；剩下的失败集中在判断与未读状态，只有循环层机制（复核、证据表、以工具取事实、用算力换准确）够得到；而业界报告更大留出增益的框架都在改循环与工具（[SURVEY_RSI_FRAMEWORKS.md](../SURVEY_RSI_FRAMEWORKS.md) §4、§6）。本模块随之改的是 gate 的统计与 final 的结构（§2.1、§2.2）、preflight 校验的范围（§3）与导出的内容（§4）；不变的是：train/dev/test 的信息分离由固定代码执行、final 先封存并提交再运行、带预留的预算账本、R0 对 R1 由下游 W* 裁决、Docker 隔离与 broker、`claude_code` provider。

## 1. 冻结比较

M0 的 eval template 先固定任务家族、control、工具/harness、重复次数、执行顺序、预算、容忍回归、质量/成本门槛及不确定性方法。构建后只填实际 candidate ref；不因成绩不好临时增重复、换任务或改成功定义。

源方法有两种用途，在 freeze 时区分：

| 用途 | 验证和完成条件 | 后续能力 |
|---|---|---|
| `purpose=source_validation`：只作为教学来源 | 在冻结 train/fit 任务上验证成功、覆盖和回归规则；输出验证 artifact，状态 passed 才生成可用 SourceWorkflow | 立即可进入教学适配；不跑生产 dev gate，不安装，不宣称泛化 |
| `purpose=deployment`：直接发布工作流或模型 | 必须按正式任务计划完成无教师 dev gate，取得 accepted receipt | preflight/CAS 后安装；可导出 app |

两种用途复用相同 TaskBackend、任务运行、评分和 M5 executor；只分派评测模板与终止位置。source validation artifact 至少保存 `plan_ref, source_release_ref, task_result_refs, status, reasons, costs_ref`；status 为 passed/failed/inconclusive/invalid，不能伪造 accepted receipt。预算/配对不足返回 inconclusive，不把少量缺失结果当通过；通过也仅证明冻结 train 任务上的方法有效。

源验证通过后不等待额外生产评测。若随后决定直接发布同一源方法，另建 deployment 实验，复用身份匹配的候选 artifact，执行生产 gate；不能把原 source 实验原地改 purpose，不能用 train 结果抵扣必须的 dev 验证。

actor 蒸馏的主要对照是同底座未训练 actor 和已选 prompt/workflow；controller 的主要对照是同一个蒸馏 actor 的原 stop 策略。教师辅助采集分数单列，不进入候选部署 gate。日后若启用领先模型同题复测，在相同目标 harness 上另立 control；默认只引用公布数字作背景，不能把不同 benchmark 的外部榜单分数输入本地 gate。

正式 task evaluation 启动时必须证实 reviewer 为空、教学材料不可访问、actor/provider 身份匹配 W、controller 开关与 plan 一致。收集 observed inspection，检查实际调用，不能仅比 manifest。TargetHarness 不同的配置可以做系统级比较，但不能作“仅训练带来的收益”的同条件证据。

首版包候选沿用 parent 的 actor 与宿主扩展契约，control/candidate 分别 pin 确切 package ref/有效工具 schema/hook 顺序；二者均在相同运行配置中执行：运行含代码（M0 §2 运行判定）时为同一固定隔离配置，只含 skills/memory 的包由宿主装配（v0.3：带 `agent` 入口的包运行代码；v1 包由默认 agent 在进程内运行，M0 §2（v0.3））。接口与候选自写测试通过只允许开始真实评测，不签发 accepted。dev gate 始终比较完整的冻结 dev 清单，control 是当前已采用版本，candidate 是本轮候选；其中对照版本本次成功的任务也参与比较，不因 train 诊断工作集缩小而移除。首版回归范围就是该 dev 清单，不自动收集历轮 train 成功任务，也不新增回归任务库或另一轮评测；评分器/条件不由包代码定义。只增大预算、换模型或偷偷卸载故障组件的运行，不能归为同模型 harness 收益。

**同模型比较的条件（v0.3，2026-10-04）。** 包可以改循环之后，“相同运行配置”指两臂共用的是 K 而不是循环：
- 两臂 pin 的 package ref 覆盖 `agent` 代码（M0 §2（v0.3）：包身份含 agent 代码），都经同一运行壳运行（M1 §1（v0.3）：episode → worker 会话 → `agent.run`，网关与 broker 在宿主）；模型身份、推理配置上限（含 `max_effort`）、网关、broker、权限、评分器、split、gate、预算根、单次尝试硬上限（M5 §2（v0.3）：act 调用、模型调用、工具调用、秒、token）对两臂相同，由冻结计划带着。原因：这些是“同模型”主张的全部内容；少任一项，差异就可能来自条件而不是包。
- 候选在 K 上限内提高 effort 或多采样是合法的包内容，不是违规；它的代价由 §2.1(e) 的成本项显现（公布结果时另跑 §2.2 的同成本对照臂），不由 gate 禁止。原因：用算力换准确正是循环层机制的一种，禁止它等于把可变面收回到文字；但不计成本地接受它，会把“更贵”错当“更好”。
- 两臂的事件与轨迹记录由宿主从网关与 broker 调用写出（M0 §2（v0.3）），包不能写；gate 读的实际工具/hook 事件与模型调用计数来自这些记录。原因：可改循环的候选也能学会伪造“已复核”的事件；证据必须出自包够不到的层。
- 候选崩溃或 agent 在运行中抛出按基础设施处理并按 retry_policy 补跑一次（如今日），硬上限触顶的尝试是有效的失败（termination `budget`），不补跑（M1 §1（v0.3））。原因：补跑只修环境，不修候选。
- gate 的统计规则（合并对照、确认复测、退步幅度、成本项、最少题数）在 §2.1（v0.3），final 的结构在 §2.2（v0.3）；其他模块引用的写法是“M3 §2.1(x)（v0.3）”，早先写作“M3 §1（v0.3）(x)”或“M3 §2（v0.3）(x)”的引用指同一条款。

### 更换目标 harness 的准备与基线

本节只处理宿主执行契约变化；首版包内工具/hook 演化使用原契约的扩展 API，直接进入 worker 候选，不为每个包版本创建 B 或重复新基线。第二版若需不同目标宿主，沿用以下准备流程。

`prepare_harness(parent_release_ref, target_harness_ref, baseline_plan_ref, scope, budget) -> preparation_ref 或明确失败` 由 M3 固定代码提供，使用 M1 兼容检查与 M5 operation，不新增安装入口。R 只从 K 预授权的已注册目标中选择；不提交任意 runtime 代码。

1. 校验 target 在 K 允许集合内，工具权限/任务评分语义不越界，actor renderer、消息/动作 schema、controller observation、上下文/完成协议能装配；失败不改 active、不训练。
2. baseline_plan_ref 先冻结 parent 的 W/目标 h、任务、条件和预算，不引用尚未创建的 B，保持 plan → permit → release 的单向关系。从主线 parent 创建基线 release B：保留 W/R/K，只改变 target_harness_ref 及计算得到的 required_capabilities。它有 parent/provenance 和限定任务/mode 的执行许可，不伪装已安装版本。
3. 按已冻结计划运行 B 的无教师基线；记录完整 release/harness/model/任务/预算/比较身份。所需基线未完成则 preparation 非 ready，不生成依赖它的训练实验；重启只补缺项。
4. M4 用 B 构造 DistillationPlan.student_release_ref 及 proposal.execution_base_ref。计划冻结后，M3 针对确切 B/plan 签发 reviewed_collection 的补充 v3 permit；它从既有 B 派生而不改写 B，避免“plan 引 release、release 又引该 plan 的 permit”的哈希环。基线许可本身不能拿来执行任意采集任务。冻结后的 Experiment.parent_release 仍是原主线 parent；candidate 在 B 的目标 h 下构建、采集和验证，运行中不换 h。
5. 要安装最终 candidate，按冻结协议与原主线 parent 比较完整系统效果，并对原 parent 做 CAS。不能因 candidate 比 B 好，就忽略它比当前生产版本更差；训练本身的贡献另外比较 B 与 candidate，二者同 h。旧 h 的成绩不能充当新 h 的同条件基线。

换 h 的实验可只改 harness，也可后续蒸馏；h-only 候选以明确 execution_base_ref 表示真实变化，不要求伪造 prompt 改动。若原 parent 已被另一安装替换，本实验保持旧输入，安装返回 stale_parent，不能偷偷继承新 W/R。若新 h 超出 K，需要另行授权活动，不让 R 修改 K。没有选新 h 时 execution base 就是 parent，不增加准备或额外基线开销。

第二版换学生时的 harness–模型共同适应是一项成本（M2 §3a（v0.3）：在 Opus 上演化出的循环未必迁移到 9B 学生，学生的 W0 是更简单的种子）；本节的 B 基线就是把它测出来的地方，不是回避它的地方。

## 2. 评测流程与结果

deployment 沿用 permit → 候选隔离运行 → 配对 TaskResult → EvaluationReceipt；source_validation 在同一运行/评分层输出源验证 artifact，不能进入安装分支。配对键仍为 `(plan_ref, task_id, repeat_index)`；attempt retry 不增加独立样本，补跑保存 supersedes。任务世界每次按同一输入复建；容器或服务故障没有有效得分时不要强行记成任务失败。

首版判分明细分存：`run_tasks`（`evolve/task_eval.py`）在追加 TaskResult 之后，把每次尝试的逐条断言结果（type/params/passed/excluded）另存 `bench/<domain>/grading.jsonl`，不进入 results.jsonl，TaskResult 与既有结果读取不变；明细只经 M7 结果存储按冻结 split 过滤的接口读取（M0 §4、M4 §1）。

主要报告完整任务成功、partial credit、任务/家族数、配对增减与不确定性、回归、延迟、实际推理费用、工具调用。训练/教师/搜索费用归总项目成本，推理节省单列。质量优先还是同预算质量比较由 K 的 gate 提前选择；没测费用就 unknown，不当 0。

M4 的失败 train 子集重跑是诊断采集，不替代正式 dev gate。未重跑的 train 成绩保留原版本/attempt 身份，仅作为历史记录；不能与新包的子集结果拼成当前版本的完整 train 成绩，或宣称这些任务已通过当前版本回归。测量复用仅限 M4 §4 的实际任务执行身份与冻结计划条件匹配，保留原 TaskResult refs；新候选仍须完成同一冻结 dev 清单的配对评测，缺项沿用 inconclusive，不因诊断子集涨分跳过 gate。

采用现有 GatePlan 和家族聚合方法。首版 quality 模式按完整配对结果判断：整体任务成功率增益达到 min_gain，且 regression_rate 不超过 max_regression_rate，再检查计划中已有的其他条件；regression_rate 沿用现有定义，即各任务有效重复的平均 success 比对照下降的任务数 / 全部配对任务数。它是本次观察到的退化比例，不是所有历史成功任务的失败比例。收据的 `metrics.regressions` 逐题列出比例计入的任务（任务、家族、两臂有效重复的平均 success）；其中含 dev 任务 id，经验日志不保存该键，只保留比例和收据 ref，所以不会进入 R（M0 §4，2026-10-02）。GatePlan 现有默认 max_regression_rate=0.2 只是工程默认，preset 明示并冻结实际值，不把它当成已经验证的最优值；后续根据实跑调整只作用于新活动。未超过冻结上限的退化如实报告，不一律拒绝。以下情形必须明确：候选协议错误为 invalid；样本/完整配对/预算不足为 inconclusive；达不到既定门槛为 rejected；符合冻结门槛才 accepted。deployment 的可选 screen 沿用现有规则：仅在显式冻结的 screen_ids 上发现对照成功、候选失败时提前筛掉候选；首版不为回归新增 screen，未被筛掉仍需完整 dev gate；source_validation 的独立终止路径不产生发布资格。

首个小规模实验可只报告方向和案例，不以训练 loss 或单任务成功降低正式发布门槛。演示用脚本 fixture 的 accepted 只证明控制路径，不能合并到真实成绩。

### 2.1 gate 统计（v0.3，2026-10-04）

依据是 2026-10-04 的离线分析（[ROADMAP §3](../ROADMAP.md) 第 2a.9 步）：25 题 × 1 次的配对差值 sd 为 0.06～0.076，`min_gain` 0.05 不足 1 个 sd；W0 自身的 5 次读数做 A/A，20 组里 4 组会被接受；12 题的内层 gate 空候选通过率约 1/3；“严格小于即退步”的规则在 A/A 里 20% 触发；gate 的赢家诅咒 5～10 点；同一个 W0 两次 final 41.3% 与 47.8%。以下规则按此改 `judge`（`ouroloop/evolve/task_gate.py`）的输入与判定，上段的 invalid/inconclusive/rejected/accepted 分类不变。新增字段都进 GatePlan/TaskEvalPlan 的 JSON 并随计划冻结，所以旧收据按旧计划读，不被重判；preset 的值是起点，`[freeze.task_plan]` 与 `[freeze.task_plan.gate]` 只能加严（`bench._beyond_preset` 的方向检查沿用，v0.3 起也查 `control_pooling` 与 `confirmation_repeats`）。

**(a) 合并对照（pooled control）。** 对照臂是**已安装 W 在同一冻结计划（任务清单、重复数、单次尝试上限、顺序种子）与同一模型身份下的全部有效读数的合并**，每题取全部有效重复的平均；对照不足时补齐缺少的读数（只读缺的题：某题的有效读数少于 `repeats` 时，对照臂只跑这一题缺的那几次配对，补上的读数与合并读数一起作对照，并进下一个 gate 的池；没有任何读数就是全部补齐，走同一条路径）。（v0.3，2026-10-04 评审后改：原写“没有任何读数时仍取一次新读数”，池非空就不跑对照臂——一次基线尝试坏了、或新测对照中途崩溃再续跑，这题此后每个 gate 都会 inconclusive。）
- TaskEvalPlan 新增 `control_pooling: str | None = None`（缺省读作 `"fresh"`，与 §2.3 一致），v0.3 preset 为 `"pooled"`；`"fresh"` 是今日行为（每个 gate 重跑对照臂，`Improvement._evaluate`）。
- 读数的归属由固定代码按 (release_ref 的 W 身份, template 的条件摘要) 判定，与 M4 §4 测量复用的 `_runs_like` 同一口径：只改 R 的 release 视为同一 W。研究位两支各自可见的读数范围由 M6 §2（v0.3）定：公共输入加该支自己的读数，另一支的永不可见。
- 配对：候选每题每次重复仍须有效（配对键不变）；对照每题至少有 `repeats` 次有效读数（合并后），缺的由对照臂补齐（上段）；补齐后仍缺（补跑也失败）才是该题缺项，按 inconclusive 规则处理。bootstrap 仍按家族重采样，每题用两臂的平均。
- 收据 `metrics.control_readings`（合并的读数数）与 `metrics.control_attempt_refs`（读数的 TaskResult refs），让复核者能重算。
- 实现（v0.3，2026-10-04，`Improvement.control_pool`）：读数的归属按 RunManifest 判——去掉 candidate/role 标签、计划 ref、预算 scope、release 与 R（R 不参与运行，只改 R 的 release 是同一 W）之后与本 gate 对照臂的 manifest 相同（模型身份、包、单次尝试上限、重复数、split、宿主契约都相等），且该读数自己的计划（有的话）冻结了同一条件（任务清单、重复数、顺序种子、单次尝试上限，`TaskEvalPlan.conditions()`）；基线的 dev 运行没有计划、按冻结模板的条件跑，所以在池里；候选装入后，它在自己 gate 里作为候选臂与确认臂的读数就是已安装 W 的读数。`control_readings` 按运行计数（一次读数 = 一个 run）。筛选臂（`screen_subject`/`screen_control`：只跑 screen 题、attempt 0，不是完整读数）不入池；在本 gate 自己的计划 ref 下记录的早先对照臂读数在池里（主线各 gate 共用同一个计划 ref，它们的对照读数正是池该有的）。缺少的配对由 `task_gate.shortfall` 算出、对照臂只跑这些（`evaluate_task`），续跑时同一运行身份找回已记录的尝试，不重付。字段缺省（None）读作 `fresh`，且不进计划身份，所以在字段出现之前冻结的计划 ref 不变。
- 原因：对照每轮重测是 v0.1/v0.2 每轮一半的花费，而 W0 在 dev 的三次读数都是 36%；合并后对照的 sd 随读数数下降，候选的噪声才是剩下的主项。对照臂不再被重测的代价是“对照漂移”看不到；它由 (b) 的确认复测和 §2.2 的 final 兜住，不在 gate 里补。
- 测试：新增 `test_task_gate.py::test_the_control_arm_pools_every_valid_reading_of_the_installed_version`、`test_the_control_arm_reads_only_the_pairs_the_pool_is_short_of`（原名 `test_a_fresh_control_reading_is_taken_only_when_none_exists`，评审后改为补齐规则）、`test_a_pooled_control_counts_only_readings_under_the_same_plan_conditions_and_model`（含筛选臂不入池与 `_verify_bound` 的缺项数）、`test_cycle.py::test_a_control_arm_cut_short_is_topped_up_on_restart_and_nothing_is_paid_twice`（首版活动在第一个 gate 之前只有 train 读数，池是空的，对照臂中途崩溃后续跑只补缺的两题）。

**(b) 装入前确认复测（confirmation re-run）。** 一条规则：第一阶段清过 `min_gain`（0.05）与 (d) 的退步规则的候选，按同一计划再测一次候选臂（对照臂仍是合并读数）；**复测这一次单独对合并对照也清过 `min_gain`**，才 accepted——候选要在两次独立读数上各清过一次 gate。候选全部读数合并后对合并对照的点估计照记（`metrics.gain`；两次各清过则合并必清过），退步规则 (d) 与成本项 (e) 按合并读数判。第一阶段没清过的候选不复测，直接 rejected。
- 规则第二版（v0.3，2026-10-04）。第一版写的是“复测单独 ≥ 0”；实现后的 A/A 回放（本节末）在 20 组里接受 4 组，都是那次 48% 的读数作候选、一次 36% 的读数作确认。同一个 W 再读一次，对合并对照的期望正好是 0，所以“≥ 0”约束不了任何东西；合并点估计只把偏高减半（+0.12 → +0.06），仍清过 0.05。改为复测单独也要 ≥ `min_gain` 的依据：合并对照之下每次读数的噪声约等于单次读数的 sd（25 × 1 时 0.06～0.076），无改动时一次读数 ≥ +0.05 的概率约 0.22，两次独立读数都 ≥ +0.05 约 0.05 ≈ 1/20；真实 +0.10 时两次都清过的功效约 0.64，+0.15 约 0.88，+0.05 约 0.25——放弃的是 test 分区本来也证实不了的小增益（ROADMAP §4、第 2a 步的功效估算），换的是误接受率。`min_gain` 仍是 0.05（(c)）。第一版规则与它的 4/20 留在本节末的回放记录里。
- TaskEvalPlan 新增 `confirmation_repeats: int | None = None`（缺省读作 0），只取 0 或 1——一次确认读数，别的值在构造计划时拒绝；economy 模式的 gate 不与确认同用（确认条款在 quality 分支里，economy 计划会为一次什么都约束不了的读数付费却拿到 `confirmed`）——v0.3 preset 为 1（再读一次）。
- 状态：evaluating → evaluated（临时）→ confirming → evaluated（M5 §1（v0.3））；只有最后的 evaluated 产生能安装的收据。
- 收据：EvaluationReceipt 新增 `kind: str | None = None`（缺省读作 `"full"`），取值只有 `"full"`（完整配对、单次读数）与 `"confirmed"`（经本条确认）；确认未通过的收据同样是 `kind="confirmed"`、verdict rejected。`metrics.readings = 1 + confirmation_repeats`，`metrics.first_reading = {gain, verdict}` 保留第一次的数字，`metrics.confirmation_gain` 是复测单独的点估计，`metrics.gain` 是合并后的点估计。安装授权只认 verdict accepted 且 `kind` 符合计划：`confirmation_repeats > 0` 的计划只认 `"confirmed"`。
- 复测的尝试计入候选所在实验的费用；验证预留（`Improvement._verify_bound`）按候选臂 × (1 + confirmation_repeats) 计，对照臂只计合并读数补不齐的那些配对（M5 §2（v0.3））。
- 实现（v0.3，2026-10-04）：确认读数是候选臂的另一个运行身份（candidate `confirmation`、arm `confirm`；`task_gate.confirmation_arm`），续跑按它自己的 attempt id 只补缺项，不会把第一次读数的尝试误当确认；确认读数本身也须完整（每个配对键有效），否则 inconclusive。`kind` 字段缺省（None）读作 `full`，新收据都显式写 `full`/`confirmed`；安装授权在 `Releases.install` 按收据所引计划的 `confirmation_repeats` 核对 kind。`confirmation_repeats` 缺省（None）读作 0，不进计划身份。
- 原因：赢家诅咒来自“挑了最大值还按它报数”；复测是最便宜的去偏，每个接受的候选多付一次读数（约 $11，25 题 × $0.45），比接受一个空候选再在 final 上发现它便宜。它是 v0.3 唯一的去噪规则，门槛本身不动（(c)）。
- 测试：新增 `test_task_gate.py::test_an_accepted_first_reading_is_confirmed_before_it_installs`、`test_a_candidate_that_clears_the_gate_on_both_readings_is_accepted`、`test_a_confirmation_whose_pooled_gain_falls_below_min_gain_rejects`、`test_a_confirmation_reading_under_min_gain_rejects_even_when_the_pool_clears_it`（第二版：复测恰为 0 的那种对，第一版会接受）、`test_a_rejected_first_reading_is_not_confirmed`、`test_a_failed_confirmation_keeps_kind_confirmed_and_the_first_reading`。

**(c) 门槛不变：`min_gain` 0.05，仍是比例。** 多领域合并 dev（M7 §2（v0.3））时 `min_gain` 不按题数换算，仍是成功率增益 0.05；题数增多带来的是更小的 sd，不是更高的门槛。v0.3 考虑过按噪声定门槛（把 preset 提到 0.12、两阶段规则、在收据上写空候选的通过概率），不做：(b) 的确认复测已是唯一的去噪规则，再提门槛会让 25 题 gate 几乎不接受任何候选。

**(d) 退步按幅度判。** 一题计入 `regression_rate` 的分子，须其有效重复的平均 success 比合并对照**下降超过 `regression_drop`**（repeats 1 时即对照 1.0 → 候选 0.0；repeats 2 时 1.0 → 0.5 不计、1.0 → 0.0 计，与 ER6-19 一致；repeats 3 时 1.0 → 1/3 计、1.0 → 2/3 不计）。（v0.3，2026-10-04 实现时把“≥”改为“超过”：ER6-19 的例子要求 1.0 → 0.5 不计，“≥ 0.5”会把它计入。）
- GatePlan 新增 `regression_drop`，缺省（None）即今日的严格规则（任何下降计入），v3 preset 0.5；取值在开区间 (0, 1)：1.0 会让“超过”永不触发。`max_regression_rate` 含义不变，分子换成上面的定义；`metrics.regressions` 仍逐题列出计入的任务。
- 原因：“严格下降即退步”在 A/A 里 20% 触发，且重复数越多越容易触发（任一重复失手就算），与“更多重复更可靠”相悖；幅度门槛让退步指“这题确实不会了”。
- 测试：新增 `test_task_gate.py::test_a_regression_is_a_drop_of_at_least_the_plans_magnitude`；现有 `test_regression_rate_counts_tasks_whose_mean_fell` 不改——它断言的是字段缺省（None）时的严格规则，幅度规则由新测试单独断言。

**(e) 成本项。** 候选每次有效尝试的平均成本最多超出合并对照的 β0 + β1·gain：`cost_subject ≤ cost_control × (1 + cost_beta0 + cost_beta1 × gain)`，gain 为成功率增益的点估计（分数）。
- GatePlan 新增 `cost_beta0`、`cost_beta1`，缺省（None）即没有成本项，v3 preset 0.10 / 2.0（+10%，再加每 1 个百分点增益 2%）；两个 β 同设同缺。现有 `max_runtime_cost_ratio`（固定比值）保留，两者都设时都检查。只在 quality 模式判（v0.3，2026-10-04）。
- 收据 `metrics.cost_per_attempt = {"subject": ..., "control": ..., "allowed": ...}`（`allowed` 在算得出时填）；超出时 reasons 记 `cost per attempt X above the allowed Y (control C × (1 + β0 + β1 × gain))`，verdict rejected。成本缺失按现有 `missing_cost_policy`：任一臂有未计价的有效尝试即算缺失，`refuse` 时 inconclusive，`report_unknown` 时不检查成本项并在 reasons 记明。
- 原因：v0.2 的 W* 每次尝试 $0.452 → $0.570（+26%），gate 里没有任何条款看见它；可改循环后“多复核几次”的候选更会如此，成本项让“更贵但没更好”被拒，“更贵且更好”按比例放行。
- 测试：新增 `test_task_gate.py::test_a_candidate_whose_cost_outgrows_its_gain_is_rejected_with_the_cost_reason`、`test_unknown_cost_follows_the_missing_cost_policy_in_the_cost_term`。

**(f) 任何 gate 不少于 25 题。** GatePlan 新增 `min_tasks`，缺省（None）即没有下限，v3 preset 25；freeze 时任务数少于它的 gate 计划拒绝冻结（与 `min_groups` 并列；检查在 `bench.freeze_task_plan`，主线 `ouroloop freeze` 与研究位两支的内层计划都经它，v0.3，2026-10-04），主线 gate 与研究位的内层 gate 都受此约束。研究位的内层 gate 仍用 dev 的前一半、外层验证用后一半（如今日），所以研究位只在 dev ≥ 2 × `min_tasks`（50 题）时开；只有 Marketing（25 题 dev）的活动不开研究位，研究位记 `no_slot_pool`，C4 不运行（M6 §2（v0.3））。原因：12 题时一题是 8 个点、空候选通过率约 1/3，裁决不出任何 R 的差别。测试：新增 `test_paid_run_safety.py::test_a_gate_plan_under_min_tasks_is_refused_at_freeze`。

**(g) 没有序贯提前拒绝，也没有提前接受。** 候选臂每次都跑完整配对。v0.3 考虑过序贯提前拒绝（候选臂净落后到一定数目即停止，省下明显差的候选的钱），不做：省下的钱有限，却给状态机、收据与预留各加一条路径。提前接受则会把赢家诅咒再放大一次，同样不做。

**(h) 收据的裁决顺序。** integrity（invalid/inconclusive，`task_gate.integrity`）→ protected requirements → 增益 ≥ `min_gain` → 退步比例 → 成本项 → 其他已有条件 → 确认复测（复测单独 ≥ `min_gain`；合并增益照记，第二版，2026-10-04）→ accepted。每个未通过的条款都写进 reasons，不止写第一条。

**A/A 回放，第一版规则（v0.3，2026-10-04，实现后）。** `examples/automationbench/aa_replay.py` 用 W0 的 5 次 dev 读数（v0.1 工作区 3 次、v0.2 工作区 2 次，各 25 题）与 4 次 test 读数（两次 final 各 2 次重复，23 题）回放本节规则：每个有向对 = (作候选第一读数的读数, 作确认读数的读数)，对照是其余读数的合并；旧规则（新测对照、任何下降即退步）在同一记录上并列回放。结果：dev 20 组里旧规则接受 4 组，新规则第一读数清过 4 组、确认后仍接受 **4 组**，加成本项仍 4 组——都是 v0.2 工作区那次 48%（12/25）的读数作候选：对合并对照（36%）第一读数 +0.12，合并后 +0.06 仍清过 0.05，确认读数（36% 对 36%）恰为 0，满足“≥ 0”；test 12 组里旧规则 3 组、新规则 2 组。五次 dev 读数的每次尝试成本在 $0.366～$0.390 之间，成本项分辨不出噪声。退出条件（≤ 1 组）**未达成**；按 ROADMAP 第 2 步的约定，规则回到本节修改后再实现，本次实现没有改规则。回放说明的是：对一次偏高的读数，确认读数“单独 ≥ 0”没有约束力（同一 W 的另一次读数期望恰为 0），合并点估计只把偏高减半，仍能清过 0.05。测试：`test_aa_replay.py::test_the_replay_judges_each_pair_as_a_confirmed_candidate_against_the_pooled_rest`、`test_the_example_workspaces_hold_five_dev_and_four_test_readings_of_w0`。

**A/A 回放，第二版规则（v0.3，2026-10-04）。** 同一脚本、同一批读数，(b) 改为“复测单独也清过 `min_gain`”之后：dev 20 组里第一读数仍清过 4 组（那次 48% 的读数作候选，+0.12），确认读数（36% 对合并的 36%）都是 0 < 0.05，**接受 0/20**；加成本项 0/20；test 12 组里第一读数清过 3 组（52% 的读数作候选），确认读数 +0.022、−0.043、+0.022 都不清过，**接受 0/12**（第一版 2/12）。退出条件（≤ 1 组）达成。实现：`judge` 的确认条款（`ouroloop/evolve/task_gate.py`），理由写两个数（`the confirmation reading alone has gain X, below the plan's min_gain 0.05`）；安装授权、`confirming` 状态与续跑、收据的形状都不依赖第一版的“≥ 0”，没有改。测试另加 `test_aa_replay.py::test_one_high_reading_among_equals_is_accepted_by_the_old_rule_and_by_no_confirmation`（真实记录的形状：四次 36% 加一次 48%，旧规则 4、第一版 4、第二版 0）。

### 2.2 final 与同成本对照臂（v0.3，2026-10-04）

final 的协议不变：计划（`bench.FinalizationPlan`）先封存并提交，再运行；结果只报告，不选版本；inconclusive 也算完成。v0.3 改两处：

- **重复 2 → 3**（`[freeze.final] repeats = 3`，定值，2026-10-04 定）。原因：区间宽度由题间异质性和同题随机性两部分组成，100 题以上前者占主导，重复只压后者——100 题 × 2 次半宽约 ±6.0，× 4 约 ±5.5，费用翻倍只窄不到 10%；取 3 是在费用与第三次读数能暴露“2 次恰好相同”的假确定性之间的折中。更多的任务（M7 §2（v0.3）多领域）比更多重复有效得多，题数优先。
- **可选的同成本 best-of-k W0 臂**（[arXiv 2607.12227](https://arxiv.org/abs/2607.12227) 的批评：演化花费是额外的搜索算力，部署时的匹配算力对照应当在场）。它在**公布结果时**运行，不是每次 final 的必备部分：封存前由固定代码从合并对照与 W* 的每次尝试平均成本算出 `k = max(2, round(cost_W* / cost_W0))`，写进 FinalizationPlan.comparisons 的一项 `{"claim": "C1", "role": "matched_cost", "release_ref": <W0>, "best_of": k}`；W0 对每题跑 k 次独立尝试（各自重建世界），**任一通过即记通过**——这是 oracle 选择，是 W0 在同成本下测试时算力的**上界**，不是可部署策略，报告里与 W* 并列并写明这一点。它从不进入 C1 的裁决：C1 仍是 W* 对 W0（同 effort、同配置）的配对差；同成本臂回答的是“这笔增益有多少只是算力”，W* 不高于 best-of-k 上界时报告如实写“增益不超过同成本的测试时算力上界”。计划含该臂时，final 份额按三臂定（M5 §2（v0.3））。
- Marketing 的 test 已被读过三次（v0.1 final、final-1、final-2），对新主张宣布为已耗尽（M7 §2（v0.3）：Marketing 的 split 原样冻结，新领域的 split 按计分断言种类与工具家族分层）；新领域的 final 才能支撑新主张，Marketing 的 test 只用于与历史数字对照。（v0.3，2026-10-05 实现）`[bench] spent` 记在合并划分里，`[freeze.final] tasks = "test"` 只解析为新领域的 test，显式列出 spent 领域的 test 题拒绝冻结，`--phase evaluate` 对含这些题的计划在付费前拒绝，报告的主张区间不含它们（M7 §2、§4）。
- 测试（可选，发表时，待实现）：`test_paid_run_safety.py::test_the_final_runs_the_matched_cost_arm_when_it_is_sealed_in_the_plan`、`test_report.py::test_the_matched_cost_arm_is_reported_beside_w_star_and_not_as_the_claim`。

### 2.3 field 清单（v0.3，2026-10-04）

| 对象 | 新增字段 | 缺省（字段不写，v0.3，2026-10-04 实现） | v0.3 preset | 条款 |
|---|---|---|---|---|
| GatePlan | `min_gain`（既有，不变） | 0.05 | 0.05 | (c) |
| GatePlan | `regression_drop` | None：任何下降即退步（今日规则） | 0.5 | (d) |
| GatePlan | `cost_beta0` / `cost_beta1` | None：没有成本项 | 0.10 / 2.0 | (e) |
| GatePlan | `min_tasks` | None：没有下限 | 25 | (f) |
| TaskEvalPlan | `control_pooling` | None，读作 `"fresh"` | `"pooled"`：合并读数不足的题由对照臂补齐，不是“有池就不测” | (a) |
| TaskEvalPlan | `confirmation_repeats` | None，读作 0 | 1（只取 0 或 1；不与 economy gate 同用） | (b) |
| EvaluationReceipt | `kind`（`"full"` / `"confirmed"`） | None，读作 `"full"`；新收据显式写 | — | (b) |
| EvaluationReceipt.metrics | `control_readings`、`control_attempt_refs`、`readings`、`first_reading`、`confirmation_gain`、`cost_per_attempt` | — | — | (a)(b)(e) |
| FinalizationPlan.comparisons | 可选 `role = "matched_cost"`、`best_of` | — | 公布结果时加入，k 由成本算出 | §2.2 |

`confirmation_repeats` 的判定是 (b) 的第二版（v0.3，2026-10-04）：复测单独也清过 `min_gain`。缺省都是 None 而不是设计稿写的数值，原因（v0.3，2026-10-04）：`Record.payload` 不写 None，`GatePlan.to_json` 也不写缺省的 v0.3 字段，所以在字段出现之前冻结的计划与收据的 ref 不变、按旧规则读，同一张 `[freeze.task_plan]` 表再冻结得到同一个 ref；只有 v3 preset 与写了字段的表拿到新值。`[freeze.task_plan.gate]` 对 v3 preset 只能加严：`regression_drop`、`cost_beta0/1` 不得高于、`min_tasks` 不得低于 preset，缺省也算放宽（`bench._beyond_preset`）。

## 3. 发布、恢复与回退

保持完整 `SystemRelease`，安装同时 pin W/R/K。训练器不触碰 active；M3 根据 accepted receipt 做 artifact/服务装载、schema、权限和依赖 preflight，再 CAS 更新 expected_parent。模型、prompt、controller 校准与 runtime config 不可分别移动冠军指针。

首版 preflight 还校验包字节/manifest/API（运行含代码时另校验 image）与被评测内容相同，测试与正式运行均使用规定的运行方式；包文件损坏、运行含代码的包 worker 不可用、schema 不兼容不能回退旧包后安装。停用/删除组件同样是新的评测候选；回退恢复完整旧 release，不只替换个别 .py 文件。

v0.3 的 preflight 另校验（v0.3，2026-10-04）：manifest 的 `api_version` 是 `harness_package.v2` 或 v1（v1 包由随框架交付的默认 agent 运行，M0 §2（v0.3）），`agent` 入口可解析且与草稿检查通过的字节相同，manifest 声明的 `ctx.model` 角色集合在 K 允许的 `{"act", "aux"}` 内；收据的 `kind` 符合计划（§2.1(b)）。原因：agent 代码是新的可执行面，评测时跑的与安装的必须是同一份字节；一个角色没声明就能调的 aux 调用，会让成本项多算，也会让第二版从网关记录出发的训练投影（M2 §1a：只读 role act 的记录）漏掉或多算。

安装过程中崩溃，按原 installation operation 与 active pointer 事实恢复；同一 accepted artifact 可重试安装，不能自动换新模型。stale_parent 不覆盖其他已安装版本；另起评测或明确结束。旧 session 继续其 pin，新 session 读取新 release。

回退选择已有有效 release，记录操作并 preflight；撤销模型不删除账本。托管 checkpoint 过期或服务不可用时不能声称历史 release 可部署；状态清楚标明，不能暗换别的底座。

M6 的 research receipt 沿用同一 preflight/CAS，只变主线 R；分支 W* 不自动成为主线 W。下一研究步骤实际绑定的 R 由 M4/M6 记录。一次 release 不混装两个未经共同授权的 W/R 候选。

## 4. 业务流程 app 的最小导出

新增窄函数 `export_deployment(installed_release_ref, destination) -> DeploymentManifest`，归 `release.py`（`release.py:471`）；CLI 接线由 M7 在本版交付。首版只支持已安装且未撤销的 release。导出是选择运行所需内容，不能递归复制整个 release 对象闭包。

白名单包括：W 的运行策略、release pin 的 TargetHarness、所需模型与 inference 配置、runtime/依赖版本、工具绑定 schema、运行权限与预算、可公开验证的发布凭据摘要。`required_core_ref` 指一个固定的运行核心投影（权限/limits/合法目标），保留源 K 身份摘要，不包含 oracle、任务答案或研究访问权限。发布凭据只提供验证所需结果，不导出原始受限评测记录。

首版额外导出 HarnessPackage 的运行 bundle/manifest、启用组件、package API、运行含代码时的隔离镜像 digest、broker 的业务工具绑定与资源要求；评测和部署挂载相同运行字节。候选 tests/失败轨迹/研究笔记分开保存，不进入运行 bundle。不能只导出 prompt 而漏掉真正产生效果的工具/中间件；有生成代码的 app 仍使用隔离 worker，不因已接受就改为控制面 import。容器镜像属于声明的依赖，不把构建集群或训练环境装入 app。

v0.3 的 app 是三件东西（v0.3，2026-10-04）：**agent 包**（运行 bundle 含 `agent/` 代码、manifest、启用组件、package API 版本）、**运行壳**（宿主运行时的投影：网关与 broker 的绑定、权限、单次尝试硬上限、默认 agent 的字节以运行 v1 包、隔离镜像 digest）、**模型服务描述**（模型身份、推理配置上限含 `max_effort`、provider 类型与服务绑定；凭据由使用环境注入）。DeploymentManifest 新增 `agent_entrypoint`、`runner_shell_ref`、`model_service` 三个键。网关日志、轨迹记录、实验台账（M4 §5（v0.3）的引擎自有台账）、合并对照的读数都不导出：它们是研究工作区的材料，不是运行所需。原因：部署端复现的是“同一程序在同一上限下调同一模型”，三件缺一不可；多一件就把研究材料带进生产。

排除 R、teacher、TeachingReference、训练数据、私有原草稿/理由、验证答案、云密钥、本地绝对路径和训练恢复凭据。外部依赖清单区分文件权重与托管模型服务；服务凭据由使用环境注入。artifact 下载后校验字节摘要；不可导出服务模型仍可输出服务绑定，但明确不能离线运行。

导入只检查兼容性与绑定，不能把未知包自动安装成改进冠军。用干净工作目录运行一个合法任务，证明不需要研究/训练进程；不要顺手实现远端目录、搜索、签名平台、市场或多租户授权系统。

## 5. 用例

| ID | 条件 | 预期 |
|---|---|---|
| ER6-01 | 教师辅助高分，无教师仍失败 | 高分仅列采集；候选按无教师结果裁决 |
| ER6-02 | control 与 candidate 底座/工具不匹配 | 同底座声明被拒绝或明确成为另一比较，不复用旧分数 |
| ER6-03 | 一侧 infra 故障或预算用尽 | 不伪造完整配对；按计划补跑或 inconclusive |
| ER6-04 | actor 装载失败、controller inference 不兼容 | preflight 失败，active 不动 |
| ER6-05 | CAS 前后分别崩溃、另一个安装先完成 | 原子恢复；stale_parent 不覆盖 |
| ER6-06 | 导出物内埋私有参考/密钥标记 | 白名单检查阻止；干净部署输入中找不到该标记 |
| ER6-07 | exported runtime 未启动 teacher/research 服务 | 合法任务可运行，实际加载模型与 release 一致 |
| ER6-08 | 源方法仅作为教学来源且 train 验证通过 | SourceWorkflow 立即可用；生产 dev evaluator/installer 设失败哨兵也不被调用 |
| ER6-09 | 新 h 下候选优于新基线 B，却劣于旧生产 parent | 可报告同 h 学习收益，但生产 gate 拒绝，active 不变 |
| ER6-10 | 新 h 基线中断或依赖不兼容 | preparation 不就绪，不采集训练；恢复复用已确认结果，不回退旧 h 冒充新基线 |
| ER6-11 | 经验证 source 随后请求直接发布 | 新 deployment 实验做 dev gate；旧 train 结果无安装权限 |
| ER6-12 | B 基线完成后冻结采集 plan 并请求执行 | 正确的补充许可可执行，不重写 B；旧基线许可或错 plan/任务/mode 的许可拒绝，引用图无环 |
| ER6-13 | 完整 dev 比较整体收益达标、其余条件满足且未被 screen 拦截；退化比例分别在冻结上限内和超出上限 | 上限内可 accepted，并保留退化记录；超出则 rejected、保留当前版本；不能仅报告失败子集涨分 |
| ER6-14 | 导出包含运行包代码但无 tests/研究材料，或包字节被改写 | 前者以评测时同字节/同隔离配置运行；后者 preflight 拒绝，不暗换代码或运行模式 |
| ER6-15 | train 与 dev 尝试各完成一次 | 两者逐条断言结果都写入 grading.jsonl；results.jsonl 行与旧结果读取不变 |
| ER6-16 | 新包只重跑失败 train 子集，旧包有其他 train 成功记录 | 两版本结果分别保留身份与覆盖范围；不拼成新包全量 train 成绩，正式 dev gate 仍完整执行 |
| ER6-17（v0.3） | 已安装 W 在同计划下有两次有效读数，第三个 gate 开始 | 对照臂不重跑，合并两次读数；收据列出 `control_readings=2` 与读数 refs；只改 R 的 release 视为同一 W |
| ER6-18（v0.3，规则第二版 2026-10-04） | 候选第一次读数 +0.08，确认复测 −0.02；另一候选第一次 +0.12，复测 +0.04（合并 +0.08）；第三个第一次 +0.08、复测 +0.06（合并 +0.07） | 前两者都不装入：复测单独未清过 `min_gain` 0.05，收据都是 `kind=confirmed`、verdict rejected，reasons 写 `the confirmation reading alone has gain 0.04, below the plan's min_gain 0.05`，`first_reading` 保留第一次的数字，`metrics.gain` 仍记合并点估计；第一次 +0.04 的候选不复测，直接 rejected；第三个两次都清过，accepted（第一版规则会接受第二个：复测 +0.04 ≥ 0） |
| ER6-19（v0.3） | 对照 1.0 的题候选两次重复各 1/2；另一题 1.0 → 0.0 | 前者不计退步（降 0.5 以下），后者计入；`metrics.regressions` 只列后者 |
| ER6-20（v0.3） | 候选 +0.12，每次尝试成本 $0.60 对合并对照 $0.45 | 允许上限 0.45 × (1 + 0.10 + 2.0 × 0.12) = $0.603，通过；成本 $0.62 时 rejected，reasons 记成本条款 |
| ER6-22（v0.3） | 活动的 dev 只有 25 题（Marketing）；另一活动的 gate 计划只有 12 题 | 前者不开研究位，记 `no_slot_pool`，C4 不运行，主线 gate 照常；后者 freeze 拒绝（`min_tasks` 25） |
| ER6-23（v0.3） | 公布结果时的 final：W* 每次尝试 $0.57，W0 $0.45 | k = max(2, round(1.27)) = 2，best-of-2 W0 臂封存进计划，与 W0、W* 并列报告，写明 oracle 上界；C1 仍只比 W* 对 W0 |
| ER6-24（v0.3） | 导出 v2 包的 app | 导出物含 agent 代码、运行壳投影与模型服务描述；网关日志、账本、合并对照读数不在其中；干净目录可运行一题 |
| ER6-25（v0.3） | 安装时 manifest 声明的 `ctx.model` 角色含 K 之外的名字，或 agent 字节与草稿检查通过的不同 | preflight 拒绝，active 不动 |

复用 `test_task_gate.py`、`test_gate_bypass.py`、`test_integrations.py`、`test_export.py`、`test_paid_run_safety.py`；故障注入沿用既有 release 测试风格。§2.1～2.2 与 §4 标“新增”的测试随 v0.3 第 2 步（统计）与第 1 步（agent 组件、导出）一起写。
