# M5：统一执行、预算与恢复

> 首版在冻结前加入能力包草稿检查（同一 operation 日志），并将构建/隔离执行加入现有一次候选执行；第二版再加入蒸馏子操作。复用 `evolve/execution.py`、jobs、计量 provider 和事件账，明确恢复已确认工作，不能重复未知付费或副作用。

v0.3（2026-10-04，DESIGN v0.7）在本模块加的是：确认复测的状态与记账（§1、§2）、一轮 k 个候选与 train 侧筛选的子操作（§1）、每次尝试成本进收据、aux 调用的计量、单次尝试硬上限的维度、并发默认 8 与订阅速率上限的处理、逐题笔记并发、final 份额的定法（§2）。§3 的暂停与结束规则不变，只加两条分类。不变的还有：一个活动一个预算根、带预留的账本、续跑的冻结输入、Docker 隔离与 broker、`claude_code` provider。

## 1. 状态机与子操作

deployment 保留已有 drafted → admitted → building → screening（可选）→ evaluating → evaluated → accepted → installing → installed；其他终态保持 rejected/inconclusive/invalid/screened_out/installation_failed/stale_parent/cancelled。installation_failed 仅可重试同一已接受 artifact 的安装。

v0.3 在 evaluating 与 evaluated 之间加 **confirming**（v0.3，2026-10-04）：计划的 `confirmation_repeats > 0`（M3 §2.1(b)（v0.3）的一条规则：第一阶段清过 `min_gain` 0.05 与退步规则的候选，在同一计划下再测一次候选臂；候选全部读数合并对合并对照仍清过 `min_gain`、且复测这一次单独 ≥ 0 才 accepted）时，第一次读数的裁决写成临时 evaluated 事件（verdict 与数字都记，不签发能安装的收据），进入 confirming 跑候选臂的复测，再写最终 evaluated 与收据。收据的 `kind` 只有 `full` 与 `confirmed`：确认通过与确认未通过都是 `kind="confirmed"`，后者 verdict rejected、`first_reading` 保留。第一阶段未清过门槛时不进入 confirming，直接 rejected。confirming 中 worker 不可用或账号错误按 evaluating 的同一规则处理（唯一重试、infrastructure_failed、暂停）；续跑时按 operation 事实判断复测已跑到哪，只补缺项。原因：确认是裁决的一部分而不是新的状态机；一个中间状态加一个收据 kind 就够让续跑与报告分辨它。候选臂每次都跑完整配对（v0.3 不做提前拒绝，M3 §2.1(g) 有说明）。

首版 harness_edit 的包检查为 package_check → isolated_probe → package_test，先在冻结前对草稿包运行（草稿检查）；building 按相同输入摘要引用已确认的草稿检查结果，不重复运行；应用 patch/形成草稿 bundle 属于 M0/M4 的纯规范化，检查结果不得事后改写包。M4 把 patch 规范化成草稿包后，M5 在同一 operation 日志中运行草稿检查：operation 记录 activity、分支 scope、轮次与修订序号；检查输入摘要覆盖草稿 bundle/manifest/test bundle、固定 API/image、测试输入与限额。只含 skills/memory 的草稿只做结构/路径/manifest 检查，不需要 isolated_probe。building 子操作的 operation key 仍包含 experiment、bundle/manifest、固定 API/image、测试输入与限额；复用已确认草稿检查只比较上述输入摘要（不含 experiment）。每份草稿（初稿与每次修订）检查前先做组件种类与字面量检查：种类不在 K 内或包文本含 train 断言 params/任务特定标识时为结构错误（回传 R、占用一次修订）。含代码（M0 §2 检查判定）的草稿需要隔离 worker；K 允许而 worker 不可用时为基础设施故障，按唯一重试策略处理，仍不可用则本轮 `infrastructure_failed` 并暂停活动（M0 §3），不消耗修订次数、不回传 R，不收窄组件种类（M1 §1）。isolated_probe 与 package_test 只在合成 fixture 世界或仅由 train 任务构造的世界中运行，从不绑定 dev/final 任务；回传 R 的检查输出有长度上限并去除任务数据，fixture 身份计入检查输入摘要。

v0.3 的 `agent` 组件（v0.3，2026-10-04）走同一条检查链：package_check 查 manifest 的 `api_version`、`agent` 入口路径与声明的 `ctx.model` 角色（M0 §2（v0.3）：角色只能是 `act`/`aux`，未声明的角色在草稿检查时是错误）；isolated_probe 在 worker 里用假 ctx（固定代码提供，`ctx.model` 返回脚本回复、`ctx.tool` 返回 fixture 输出）调一次 `run(ctx, task)`，接口不符、import 失败或 agent 抛出都是草稿失败，回传 R、占用一次修订；package_test 跑候选自写测试。含 `agent` 的草稿一定含代码，所以一定需要 worker。agent 在正式运行中抛出或 worker 崩溃按基础设施处理（M1 §1（v0.3）），与今日的包 worker 一致；硬上限触顶是有效尝试的 `budget` 终止，不是故障。草稿检查还核对候选声明（M4 §2（v0.3）：mechanism、site、carrier、predicted_affected、trigger_condition、differs_from）与 diff 一致，不一致是结构错误（`declaration_mismatch`）；它不对候选集的组成（例如是否有人改代码）设任何要求。原因：检查链不新增，只是多一种组件的入口；假 ctx 让接口 smoke 在没有模型、没有任务数据的条件下完成。

草稿检查失败时，只把检查输出回传 R，不含任何任务结果或评分；R 可修订，修订次数上限 `max_draft_repairs` 由 preset/K 固定（默认 3，初稿之后最多修订 3 次、最多 4 份草稿），R 不能提高。每次修订形成新草稿包 ref，草稿与检查逐次记录并计费，重启不重置修订计数。只有检查通过的草稿才冻结进 Experiment；次数用尽则本轮以无效提案结束（RoundResult `outcome=invalid`，M0 §3）并计入 schedule 轮次与失败上限，不冻结 Experiment。冻结后源码不变；building 按相同输入摘要复用已确认的草稿检查，不重复运行。冻结后仍出现的路径/接口/候选代码错误判 invalid；隔离设施故障独立记录，不能卸载包继续评测：building/evaluating 中 worker 不可用同样按唯一重试策略，仍不可用则实验记 infrastructure_failed（评测侧 inconclusive，不当候选失败）并暂停活动。修复只在冻结前、同一轮内有界进行，不另开 proposal，也不占用额外 schedule 轮次；冻结实验内不更换文件。

v0.3 一轮有 k 个候选（M4 §2（v0.3）：preset `max_candidates_per_round` 3，`bench.py:1083-1086` 今日拒绝 ≠1 的值，这是要改的地方）时（v0.3，2026-10-04）：k 个候选目录出自同一轮的一个 R 编码会话（M4 §1（v0.3）；修订继续同一会话），每个候选各有自己的草稿、检查与修订计数（各自 ≤ `max_draft_repairs`），都在同一轮的 operation 日志里，按候选序号记；train 侧筛选（M4 §2（v0.3）：重跑该候选笔记来源的失败 train 题加一份固定的通过题样本，只有筛选最好的候选付 dev gate）是 screening 之前的一个子操作 `train_screen`，每个候选一个，operation key 含候选 ref 与筛选任务清单；只有被选中的候选冻结进 Experiment 走 building → evaluating，其余候选以 `screened_out` 结束并保留草稿、检查与筛选结果。续跑按候选序号补缺项。原因：k 个候选是 k 份草稿与 k 次筛选，不是 k 个实验；dev gate 仍只付一次。

代码检查、候选自写测试与任务评测都不在控制面 import 包。M1 提供固定隔离 worker（只含 skills/memory 的包不启动 worker），M5 用现有 operation/run/deadline 管理生命周期、资源输出、取消与清理；不新增代码编排服务。已确认检查只在全部输入摘要一致时复用，已存在文件不等于检查完成。未知工具副作用按下节核对，不能因 worker 重启重放复合工具。

第二版 distill_actor 在 building 内依次记录 collect/dataset/train/probe 子操作；沿用 experiment + step + 冻结 input refs 的身份与日志。子操作结果落盘后才能推进，不新增顶层训练状态机或让 collect 自行发布。

v3 `purpose=source_validation` 复用同一 executor 的 build/evaluate：drafted → admitted → building → evaluating → evaluated → **validated**。只新增 validated 这个非发布终态；校验/执行故障仍走 invalid/inconclusive，源验证未达门槛走 rejected。evaluate 返回源验证 artifact，结果为 passed 时写 SourceWorkflow 并结束，receipt/installation 为空；不得进入 accepted/installing。可选 screen 仅用于便宜的预检，不代替源验证。旧 v2/deployment 状态解释不变。

build/evaluate operation 身份包含 purpose、parent、execution base 和冻结输入；恢复时按原 purpose 加载 source 结果或正式 receipt，不能把 validated 解释成待安装。相同内容的纯构建 artifact 可复用，但改变用途/比较条件必须新建 experiment 和评测操作。

harness preparation、首版草稿检查/修复、M8 教学适配和补采使用同一 operation 日志：输入先冻结、权限/预算先检查、产物落盘后才能生成依赖它的 proposal。harness preparation 记录兼容检查、基线 release/permit、baseline attempts 和结果；期间不移动 active。材料准备不是第二实验状态机，也不能绕过 activity schedule 隐藏无限搜索。

补采由持久化 CollectionRequest 驱动，operation key 包含 activity、分支 scope、请求 ref、冻结任务/attempt 身份；事件记录请求、pending、已完成 refs、计数和后续 round 的关联。恢复优先完成/核对 pending，再用实际结果重建 context。只复用已确认数据，未知 API/副作用仍按下节处理；不重置补采上限，不重跑完成批次。

collect 可引用已有完整轨迹；dataset 身份固定选择规则/投影；train 身份固定数据/配方/底座；probe 固定实际 artifact/runtime。任何上游输入变化生成新操作，不覆盖旧结果。同一 plan 的未完成采集只补计划允许的缺口；不能挑最好轨迹无限重试。

## 2. 预算所有权

一个 activity 一个预算根；源工作流实验、材料适配、collect/train/eval、研究提案、campaign 两支及外层验证都用其子额度。计入 actor/R/teacher API、训练 tokens/时间、环境费用、失败与重试。子额度是限额不是额外消费，实际叶子操作只收费一次；不得因开始研究比较另开同额根。

首版还计入逐题失败笔记与汇总的 R 调用、每份草稿的检查及 R 修订调用（含未通过的草稿）、代码生成、隔离构建/测试、worker 资源时间及 broker 内部的每次基础工具调用；不能只计复合工具外层调用。笔记是 train 血缘 artifact，每轮笔记数上限 `max_failure_notes` 与修订上限 `max_draft_repairs` 由 preset/K 固定，R 不能提高；已落盘笔记只在完整笔记身份（M4 §1：渲染后的笔记请求摘要与模型/推理配置）一致时复用，已确认检查在全部输入摘要一致时复用，均不重复收费；max_failure_notes 限制每轮汇总调用读取的笔记数（缓存加新生成）。诊断笔记、草稿检查/修复与构建前均预留正式 gate/回归/安装探针及已安排的 R 比较额度，包代码不能改 timeout/call cap 或另开收费 client。第二版 teacher/训练费用在相应方法启用后计入，不因保留设计就要求首版预留训练账户额度。

**单次尝试的硬上限与 aux 调用（v0.3，2026-10-04）。** 今日的单次尝试限额是 `Budget(max_steps, max_tool_calls, max_seconds, max_usd)`（`ouroloop/benchmark.py:240-257`），由冻结的 task plan 带着（`per_attempt_budget`）。v0.3 的维度：
v0.3 的五个硬上限，名字与数值与 M0 §2（v0.3）相同：
- `max_steps`：role `act` 的模型调用数，含义不变（一步 = 一次 act 调用）；preset 50。
- `max_model_calls`（新增）：全部模型调用数，act 加 aux；preset 200。
- `max_tool_calls`：经 broker 的工具调用数，含义不变；preset 200。
- `max_seconds`：墙上秒数，含义不变；preset 900。
- `max_tokens`（新增）：本次尝试全部模型调用的**输出** token 合计（含计费的 thinking）；preset 400,000，是天花板不是旋钮。它是每次尝试的上限，与 `[llm] max_tokens`（单次回复的输出上限）不是同一个东西。
- `max_usd`：可选，含义不变。
全部在宿主执行：网关在每次 `ctx.model` 前、broker 在每次 `ctx.tool` 前检查并拒绝超限的调用，agent 看到的 `ctx.limits` 是余量（M0 §2（v0.3）），改不了上限；触顶的尝试以 termination `budget` 结束，`limit` 记触顶的维度（`act_calls`/`model_calls`/`tool_calls`/`seconds`/`tokens`），是有效的任务失败，不是故障，不补跑。`step_limit` 只出现在旧记录里，读旧结果时与 `budget, limit=act_calls` 同义，新记录不再写它。aux 调用（`role="aux"`：核对、证据表、投票等不产生动作的调用）与 act 调用一样经网关预留与结算、计入 `max_model_calls`、`max_tokens` 与费用，计入每次尝试成本；网关记录按角色记它们（第二版的训练投影只读 role act 且教师 PASS 的记录，M2 §1a；第二版采集时 aux 计入 collect limits）。`effort` 由网关按 K 的 `max_effort` 截顶，不拒绝：超出的请求按截顶值发出，网关记录里记下请求值。原因：可改循环的包能自己决定调多少次模型；上限不在包手里，“同模型”才成立；aux 单独有名字，是因为成本项与第二版的训练投影都要分辨它。冻结后再改 `[bench.budget]` 不影响已冻结的比较（§5 既有）。新增 `test_paid_run_safety.py::test_an_aux_call_is_metered_and_counted_against_the_attempt_by_role`、`test_a_token_limit_ends_the_attempt_as_budget_not_infrastructure`、`test_the_gateway_caps_effort_at_k_and_records_the_requested_value`。

**每次尝试成本进收据（v0.3，2026-10-04）。** 每个 TaskResult 已带 `cost_usd`；gate 收据加 `metrics.cost_per_attempt = {"subject", "control", "allowed"}`（两臂有效尝试的平均，`allowed` 是 M3 §2.1(e)（v0.3）成本项的上限），final 的每臂 summary 同样加 `cost_per_attempt`，报告按臂列出同名的 `cost_per_attempt`（M7 §4（v0.3）；收据与报告只用这一个名字，它指每次有效尝试的平均成本）。成本缺失（无计价或 usage 不全）记 `cost_missing` 计数，不填 0；成本项按 `missing_cost_policy` 处理。原因：成本项要用它裁决，报告要用它说明“更贵还是更好”，两处读同一个数。

**验证预留（v0.3，2026-10-04）。** 构建前的验证预留（`bench.py:842` 的 `_verify_bound`）改为：候选臂 dev 任务 × repeats × (1 + `confirmation_repeats`)，加对照臂（仅当该计划下没有已安装 W 的合并读数，M3 §2.1(a)（v0.3）），加安装探针；k 个候选时另加 k 份 train 筛选（各自的失败题数加固定样本）与一份 dev gate，不是 k 份 dev gate。原因：预留的用途是“开工前确认验得起”，确认复测与筛选是新的验证步骤，必须进上界。新增 `test_paid_run_safety.py::test_the_verify_bound_counts_the_confirmation_and_the_screens_and_one_dev_gate`。

按用途预留验证额度：deployment 预留正式 gate 和安装探针；source_validation 预留冻结的源验证，不要求预留并执行无关生产 gate；补采预留该批采集及下一次提案/准入；换 h 预留兼容探针、新基线及所计划后续实验的最低验证。各预留属于同一总根；source 的轻量验证不豁免活动级已冻结的后续发布/递归验证保留额度，不能把钱全花在源搜索、教师或训练。campaign 的外测额度（含一次补跑）冻结为 plan 的 `outer_budget`，是外测 scope 的上限；它在公共测量与两支开跑前以不结算的预留占住，按上界计入每一级直到根，其余步骤花不到；外测开始时释放给外测尝试，外测不运行时退回活动；重启找回原预留，不重复占用。两支各以份额为上限，但都从根花钱：公共测量之后、两支开跑之前，两支的份额同样以不结算的预留占住，各支开跑时释放给自己，后跑的一支因此不会被前面的花费饿死；占不住时两支都不运行，比较判 inconclusive。重启找回这些预留，已跑过的支不再检查。预算维度和计价版本固定：调用/tokens/train_jobs/training_seconds/环境时间/费用按实际后端能力选。未知成本不能算 0，也不能声称严格 USD 限额已保证。

**R 作为编码会话的研究调用（v0.3，2026-10-04）。** M4 §1（v0.3）把 R 的提案改成在 worker 里对包目录的有界编码会话（文件工具、`read_trajectory`、`finish`，没有 shell；回合上限 `research_session.max_turns` 40、编辑上限 `research_session.max_edits` 24 由 preset 固定）；**每轮一个会话**，产出至多 `max_candidates_per_round`（3）个候选目录，草稿检查失败后的修订继续同一会话，回合与编辑计数跨整轮含修订累计；会话的每一回合是一次研究调用，经同一计量器预留与结算，计入研究子额度；会话读原始 train 轨迹的只读工具（`read_trajectory`，单次 `research_session.max_trajectory_bytes` 65536、累计 `max_trajectory_bytes_total` 524288）不收费，但读取次数与字节数记进 operation。今日研究调用占花费 3.2%（ROADMAP §3 第 2a.9 步），翻几倍也付得起；研究子额度耗尽按 §3 的子 scope 耗尽处理（本轮 invalid，活动继续）。新增 `test_paid_run_safety.py::test_every_turn_of_the_research_session_is_metered_on_the_research_scope`。

并发：`[bench] concurrency` 让一支（或一个 gate、final 的一臂）里的 N 次尝试同时跑（`task_eval.run_tasks`）。它是机器的设置，不进任何冻结计划的身份：每次尝试的限额与每次调用的预留都不随并发变化，账本仍按每次调用预留与结算。并发尝试各有自己的 episode、agent 与会话；它们共用的计量器、绑定 release 的计数与各类追加写入都加锁；结果、判分与调用方回调仍由主线程按完成顺序记录，返回列表按计划顺序。中断时已在跑的尝试跑完并记录，未开始的不再开始。两支之间、gate 的两臂之间仍是串行。并发下在飞的调用各占一份预留上界：某次预留被拒而本计量器还有调用在飞时，等其中一次结算后再预留，不算预算耗尽；只有没有调用在飞仍被拒，才是预算用尽（v0.2 的第一次 final，2026-10-04：花到 $21 时 4 份 $7.28 的上界放不进 $50 份额，78 秒内 51 次尝试被记成 budget_exhausted）。因此一个份额要容下预期花费加一份上界；final 份额按两臂预期花费加一份上界定。2026-10-04 加入（v0.2 运行中途，用新代码续跑），为缩短墙上时间；订阅额度的滚动窗口不变，并发只是更快用到它。

**并发 8 与速率上限（v0.3，2026-10-04）。** `[bench] concurrency` 默认由 1 改为 8；仍是机器设置，不进计划身份，运行中可以调低再续跑。订阅的速率上限（CLI 报 429/529/overloaded/rate limit）由 `claude_code` provider 判为未计费、可重试的 ProviderError（`ouroloop/providers/claude_code.py:48-49`、`:297-303`），由计费层的重试吸收，不计入账本；重试用尽才成为尝试的基础设施失败，按 retry_policy 补跑一次，连续失败轮由 `max_infrastructure_failures` 升级为暂停（§3 既有）。一轮里未计费的速率拒绝次数记进 operation（`rate_limited`），报告列出；一轮里它超过尝试数时，停下来调低并发再续跑是人的决定，不自动调。原因：8 路是 v0.2 实测 4 路 3.2 倍加速的延长，让一臂 25 题的读数约 5 分钟（每题约 90 秒）；速率上限表现为拒绝而不是账单，所以它是重试问题而不是预算问题，自动调并发会让墙上时间不可预期，不如记下来由人定。新增 `test_claude_code_provider.py::test_a_rate_limit_refusal_is_unbilled_retryable_and_counted`、`test_paid_run_safety.py::test_concurrency_is_a_machine_setting_the_frozen_plan_does_not_carry`（若既有测试已覆盖后者，只改默认值的断言）。

**逐题笔记并发（v0.3，2026-10-04）。** 逐题失败笔记是互相独立的 R 调用（M4 §1），按同一 `[bench] concurrency` 并发发出；笔记身份（渲染后的请求摘要与模型/推理配置）与复用规则不变，所以并发不改任何缓存命中。汇总请求里笔记的顺序是冻结的覆盖顺序（久未纳入的失败优先，M4 §1），不是完成顺序，所以汇总请求的摘要不随并发变化。每条笔记仍逐次预留与结算；`max_failure_notes` 仍限制读取数。中断时在飞的笔记写完落盘，未开始的不开始，续跑只补缺项。原因：v0.2 一轮 30 条笔记串行约 10 分钟，是每轮墙上时间的大项；并发后笔记约 2 分钟。新增 `test_diagnosis.py::test_notes_written_concurrently_enter_the_summary_in_the_frozen_order`。

**并发的范围（v0.3，2026-10-04 补）。** `[bench] concurrency` 是一个进程内同时在飞的尝试总数，不是“每臂”：一个 gate 里候选臂的尝试与（没有合并读数时的）对照新测共用同一个池；k 个候选的 train 侧筛选共用同一个池；研究位的两支各自是一个进程、各自的工作区（今天就是），同时跑，各按自己的 `concurrency`，订阅的速率上限由两支共担。所以“批量跑”已经是默认：串行只剩 R 的会话、草稿检查与轮次之间的依赖。并发能开多大由订阅的速率上限定：先在 16 路试一次基线的头 50 题，429 的重试数超过尝试数就退回 8。新增 `test_paid_run_safety.py::test_the_in_flight_cap_is_shared_by_the_arms_and_screens_of_one_gate`。

**每轮墙上时间的目标（v0.3，2026-10-04）。** 并发 8、合并对照（对照臂不重跑）、笔记并发与 train 筛选合起来，单域（25 题 dev）一轮的目标约 10～15 分钟：笔记约 2 分钟、R 会话约 2～3 分钟、k 份筛选各几题约 2 分钟、候选臂一次读数约 5 分钟、确认复测再 5 分钟（只对清过门槛的候选）。多域合并 dev（约 125 题）时候选臂一次读数约 20～25 分钟，一轮约 45～60 分钟。这是目标不是上限，报告按轮写实际墙上时间。原因：第 5 步的多领域活动要在 1～2 天内跑完（ROADMAP §3（v0.3）），每轮的墙上时间是它的算术前提。

**final 份额的定法（v0.3，2026-10-04）。** final 份额须容下两臂（W0 × repeats、W* × repeats；repeats 3）按合并对照读数的每次尝试平均成本的预期花费，计划含公布结果时加跑的同成本 best-of-k W0 臂（M3 §2.2（v0.3），可选）时再加该臂（W0 × k × repeats），加 `concurrency` 份单次调用的预留上界（并发在飞的调用各占一份；final-1 的 $50 份额容不下 4 份 $7.28 就是这个账）。freeze 时的既有检查（份额限制根所限的维度、大于零、给其余部分留有余额、放得下一次调用）加一条：份额放不下配置的 `concurrency` 份上界时拒绝冻结，并打印按当前已知成本算出的预期花费与建议份额；运行 `--phase evaluate` 前再算一次（此时合并读数更多），只打印估算，不拒绝。预算根开了不能改，所以份额定在根打开之前（ROADMAP §3 第 2a.4 步的教训）。原因：份额不够的 final 不是便宜，是作废（final-1）。新增 `test_paid_run_safety.py::test_a_final_share_that_cannot_hold_the_concurrent_bounds_is_refused_at_freeze`、`test_the_final_prints_its_expected_spend_from_the_pooled_readings_before_running`。

外部收费前 reserve；返回后按实际 usage settle。Tinker 需分别记录训练和 sampling 的计价维度，不能直接用聊天 API 输入/输出价格计算梯度训练。后端提供可执行上界时才能使用严格美元/时间模式；否则启动前要求选择可执行的调用/token/job 限额并报告费用限制，不能静默降级。

本地 CPU/GPU 资源也记录时间/估算依据。教师无外部副作用的文件读取无需假造 API 费用，但读取权限和工具次数受 collect limits 约束。实际费用超上界记 budget_breach 并停止后续付费；无法撤回已发生账单。

## 3. 重试与不确定结果

仅一层负责外部重试。禁用 SDK 隐式重试，或将每次尝试显式计入预算；Harbor 负责它的 trial 内环境恢复时，ouroloop 不再嵌套重试同一动作。供应商幂等只有确实受支持才使用，本地 journal 不等于外部 exactly-once。

响应丢失、进程死亡或提交后未保存 ID：保留 open/unknown reservation 的最坏占用，不直接释放；按 request/client/job/checkpoint ID 核对。确认未执行可重试，确认已执行复用，不能确定则停候选并标 inconclusive，等待可核对事实。新请求必有新预留，不能把相同 operation ID 当作免费重放。

Tinker optim_step 若状态不明，不能重复 apply。只有已确认训练 checkpoint、optimizer 状态及数据游标可恢复时继续；否则终止该候选或另建有明确预算的新训练，历史费用保留。Harbor 是否可 attach/resume 由能力声明决定，不能默认存在稳定恢复 API。

首版的暂停与结束规则（2026-10-02）按故障在哪一层被处理来划分。

**actor 单次尝试内**（基线、测量、补采、gate 两臂、公共测量与外测中的每次尝试）：
- 账号类错误（余额、配额、被拒的 key，订阅的使用上限）上抛，活动以 repair_runtime 暂停。
- 其余 ProviderError，包括计费层重试用尽的：
  - 记该次尝试 invalid（failure_class=infrastructure），按冻结的 retry_policy 补跑一次；仍缺则所属比较判 inconclusive，不暂停活动。
  - 连续的基础设施失败轮由 `max_infrastructure_failures` 升级为暂停。
- 尝试内的账本预算拒绝：记 budget_exhausted，所属比较判 inconclusive。
- （v0.3，2026-10-04）速率上限的拒绝（429/529/overloaded）不是账号类错误：未计费、可重试，由计费层吸收；只有重试用尽才落到上一条。
- （v0.3，2026-10-04）单次尝试硬上限（act 调用、模型调用、工具调用、秒、token）触顶：有效尝试，termination `budget`（`limit` 记维度；`step_limit` 只在旧记录里），failure_class 为任务结果，不补跑、不暂停；agent 在运行中抛出或 worker 崩溃：failure_class=infrastructure，按上面第二条处理（M1 §1（v0.3））。

**尝试之外的调用**（逐题笔记、汇总、修订、研究位提案、R 编码会话的每一回合，以及 collect、续跑、基线、研究位、公共测量与外测的编排步骤）：
- 触发暂停的故障：账号类错误、研究回复在输出上限处被截断（ReplyTruncated）、计费层重试用尽后的 ProviderError。
- 暂停时：
  - 写入 paused 及原因，不以未捕获的异常退出；
  - 已结算的费用保留，open 预留按上文处理；
  - 不消耗轮次、修订次数或失败额度。
- 排除问题后续跑：从记录的状态继续，不重付已确认完成的尝试。

**账本预算（BudgetError）分两类：**
- 额度耗尽：reserve 会超出某级上限，或 budget_breach 之后 scope 已停止。这不是故障，是有界结束：
  - 子 scope（campaign 的某一支、外测预留）耗尽：只结束该子步骤，所属比较判 inconclusive，活动继续。
  - 研究位开始时根覆盖不了外测预留与两支份额（由 cycle 自己的研究员作答时，再加 R 这一次调用）：按活动根耗尽处理，研究位不问 R。
  - 活动根耗尽：活动以 budget_exhausted 结束并出报告，不写 paused。
  - 上面列出的编排步骤遇到额度耗尽时，同样按此记录，不以异常退出。
- 配置不一致：scope 已以其他限额存在、scope 不存在、未知维度。按下文“续跑的冻结输入”暂停并写明原因；能在 freeze 时发现的，在 freeze 时拒绝启动。

供应商账号的余额或配额耗尽属于账号类错误，充值（订阅则等额度恢复）后续跑即可，与账本预算耗尽不是一回事。订阅没有逐次账单，`[pricing]` 按 API 标价作名义上界，预算的美元数是名义值；账号开了 extra usage 时，套餐额度用完后 CLI 改用付费额度继续，不报错，名义预算就成了真钱，所以运行前要确认已关闭（ROADMAP §3 第 2a 步）。

**续跑的冻结输入（2026-10-02）：**
- 续跑一个未完成的实验时，评测计划与预算 scope 只取 Experiment 冻结的 `eval_template_ref` 与 `budget_scope`。
- 当前活动的计划或 scope 与之不一致时不续跑：实验保持打开并记录原因，活动暂停，由人决定关闭该实验还是恢复原计划。不得用新计划或新预算完成旧实验。
- 筛选与评测的 control、permit 与候选 release，都建在冻结的 `parent_release` 上。
- 安装仍按冻结的 `parent_release` 做 CAS；active 已移动时记 stale_parent，不暂停。
- 首版活动内不重新冻结 task plan、schedule、预算或计价（[ROADMAP §3](../ROADMAP.md) 首个真实活动协议）。
- （v0.3，2026-10-04）confirming 的续跑按同一 `eval_template_ref`：复测的尝试与第一次读数在同一计划身份下，只是 `retry_index`/读数序号不同；合并对照的读数归属也按冻结计划的条件摘要判（M3 §2.1(a)（v0.3）），不按当前配置。

## 4. episode 与副作用恢复

采集记录草稿 → review → accepted → execution 各事实；写入采用已有原子对象与追加事件机制。不对失败的草稿执行任何工具。动作执行后、结果落盘前崩溃形成不确定副作用：优先查询环境真实状态，不能直接重发营销通知等操作。

首版 benchmark 可将未完成 episode 标 incomplete，并在全新可重建世界中按冻结 retry policy 补一次；旧轨迹和费用保留，不进入 SFT。真实业务环境不能 reset 时，必须有工具幂等/查询契约或人工处理；没有该能力就停止，不把 benchmark 的重建能力承诺给生产。

v0.3 的事件与轨迹记录由宿主写（v0.3，2026-10-04）：网关记录每次模型调用 {call_id, role, effort, messages, tools, response, usage}，broker 记录每次工具调用事件；包经 ctx 看不到这两份记录的写入句柄（M0 §2（v0.3））。episode 的 incomplete 判定、补跑与 supersedes 读这两份记录，不读包自己的任何笔记；中断时在飞的尝试也按这两份记录结算。原因：可改循环的包能写出任何“已完成”的说法，恢复与裁决只信宿主写的事实。

cancel 停新付费请求并尽力取消后台作业；取消仍可能收费，保留对账。已完成 artifact 核验后复用；已评测配对只补缺项；accepted 未安装继续同一次安装，CAS 已提交以实际 pointer 为准。

## 5. 当前已知差距与用例

当前已有逐调用预算、未知占用、活动预算根和实验续跑。待补：自动 wall time 计量、SDK 隐式重试覆盖、外部请求对账，以及教师/Tinker/Harbor 新边界。不能把这些设计写成当前严格保证。

§3 中 2026-10-02 写定的规则，已在阶段 1 与阶段 3 实现（2026-10-02）：
- 尝试之外的故障由 `research/loop.py` 的 `halt_of` 统一分类：
  - worker 轮由 `run_worker_rounds` 落在 repair_runtime 或 budget_exhausted；
  - 续跑未完成的实验时，由 `resume_unfinished` 按同一规则停下；
  - cycle 的每一步（基线、段、研究位）把结局与原因写进 `rsi/cycle.json` 该步的 outcome/reason 以及活动的 status。
- 账本区分额度耗尽（`BudgetExhausted`，有界结束）、预算不符（`BudgetMismatch`，暂停）与其他账本误用（直接上抛，不当作暂停）。
- 计费层重试用尽后，供应商抛出的任何异常都以 ProviderError 离开计费层，带上是否可重试与 HTTP 状态，所以 SDK 自己的连接或超时错误同样按暂停处理。
- 冻结的 task plan 带全部单次尝试上限；冻结后再改 `[bench.budget]` 不影响任何已冻结的比较。
- 续跑比对 Experiment 冻结的 `eval_template_ref` 与 `budget_scope`：不一致就不续跑，记 `resume_refused` 事件并暂停。筛选与评测建在冻结的 parent 上。
- 2026-10-04（v0.2 运行中途）：一臂内并发（`run_tasks` 的 `concurrency`），以及计量器在预留被拒且有调用在飞时等待结算再预留（`evolve/execution.py:800-830`）。

尚未覆盖：
- 单独运行的 `--phase baseline` 与 `--phase research`，遇到尝试之外的故障时仍以异常退出（单独运行的 `--phase improve` 已按规则停下）。
- 尝试内的预算不符，仍按 budget_exhausted 记录。
- v0.3 本模块的全部新增（confirming、`train_screen`、`max_model_calls`/`max_tokens`、termination `budget` 的 `limit`、aux 计量、`cost_per_attempt`、验证预留的新算法、并发默认 8 与 `rate_limited` 计数、笔记并发、final 份额检查）：设计已写，随 ROADMAP §3（v0.3）第 1～3 步实现，实现前不是保证。

首版按 [ROADMAP §2](../ROADMAP.md) 阶段补：阶段 A 补文本草稿检查（结构/路径/manifest）与有界修订、笔记调用计量；阶段 B 把同一修复循环扩展到 isolated_probe/package_test，并补包 worker 生命周期/资源与 broker 内部工具调用计量。教师/Tinker 的新恢复路径归第二版，已有旧 job/serving 兼容不删除。

| ID | 条件 | 预期 |
|---|---|---|
| EX6-01 | 工作流搜索+适配+actor/teacher+训练+campaign+外评记账 | 共享一个根，叶子无重复累计；子额度不增加总可花预算 |
| EX6-02 | 钱够采集训练但不够最低评测 | admission 不启动付费构建 |
| EX6-03 | teacher 或 optim_step 响应丢失 | 额度继续占用；核对或 inconclusive，不盲重试 |
| EX6-04 | collect/dataset/train/probe 各完成后崩溃 | 已确认产物按身份复用；只恢复缺失部分 |
| EX6-05 | 工具已执行但回执未保存 | 不重复副作用；隔离世界按冻结策略重建，旧 episode incomplete |
| EX6-06 | SDK 自动重试或 Harbor 内外双重重试 | 启动配置阻止，或每次外部收费可追溯 |
| EX6-07 | 取消恰逢安装提交 | 读取安装事实，不把已安装记成未安装 |
| EX6-08 | source validated 后重启，installer 设失败哨兵 | 恢复已完成源产物，零安装调用；不进入 accepted/installing |
| EX6-09 | 补采已返回数据、后续提案前崩溃 | 已完成批次不重跑，计数和证据保留，下一轮从新 context 继续 |
| EX6-10 | 新 h 基线部分完成后中断 | 保留 preparation/parent，补缺项且共用原预算，不将 B 设为 active |
| EX6-11 | 包测试完成后重启，或复合工具执行后 worker 失联 | 同输入复用已确认检查；未知工具副作用先核对、不重放；计数/费用/资源占用不清零 |
| EX6-12 | 草稿检查失败、R 修订后通过；期间重启，随后冻结并进入 building | 每份草稿、检查与修订调用逐次记录计费，修订计数不重置；重启复用已确认检查；building 按输入摘要复用，不重跑 |
| EX6-13 | 含代码草稿检查时 worker 失联；另一次 building/evaluating 中失联 | 唯一重试后 infrastructure_failed，修订计数与失败上限不变，已发生费用保留，活动暂停；恢复后以原草稿 ref/修订序号或原 operation 续跑，不重复收费，不 in-process 执行 |
| EX6-14（v0.3） | 第一次读数清过门槛，confirming 中途重启 | 续跑按同一 `eval_template_ref` 只补复测缺项；第一次读数的临时 evaluated 事件与费用保留；只有最终 evaluated 产生能安装的收据 |
| EX6-15（v0.3） | 确认复测通过（合并 ≥ 0.05、复测单独 ≥ 0）与未通过（合并 < 0.05，或复测单独 < 0）各一个候选 | 两者收据都是 `kind="confirmed"`、`first_reading` 保留；前者 verdict accepted 可安装，后者 rejected；状态机都经 confirming → evaluated，没有别的收据种类 |
| EX6-16（v0.3） | agent 一次尝试里发 60 次 act 调用、或 act+aux 共 201 次、或输出 token 合计超 400,000 | 网关在超限的那次调用前拒绝，尝试以 termination `budget` 结束，`limit` 分别为 `act_calls`/`model_calls`/`tokens`，有效、不补跑；aux 调用全部计费并计入每次尝试成本，网关记录里 role 为 aux |
| EX6-17（v0.3） | 一轮 3 个候选，各有草稿与修订；2 个通过筛选 | 每个候选各自的修订计数 ≤ `max_draft_repairs`；3 份 `train_screen` 子操作各记一次；只有筛选最好的候选冻结进 Experiment 付 dev gate，其余 `screened_out` 并保留记录 |
| EX6-18（v0.3） | 8 路并发下一轮里 CLI 报 429 共 20 次，重试后全部成功 | 20 次未计费、不进账本、记 `rate_limited=20`；无 invalid 尝试、不暂停；报告列出该计数 |
| EX6-19（v0.3） | 30 条逐题笔记并发写，完成顺序与覆盖顺序不同 | 汇总请求里笔记按冻结的覆盖顺序排列，请求摘要与串行时相同；笔记各自计费一次；中断后只补未落盘的笔记 |
| EX6-20（v0.3） | `[freeze.budget.final]` 份额放不下 `concurrency` 份单次调用上界 | freeze 拒绝并打印预期花费与建议份额；`--phase evaluate` 前按合并读数打印估算，不拒绝 |
| EX6-21（v0.3） | 一轮的 R 会话产出 3 个候选目录，其中一个草稿检查失败 | 修订继续同一会话，`research_session.max_turns`/`max_edits` 跨整轮累计；会话每一回合各计费一次；`read_trajectory` 不收费但次数与字节数记进 operation |

复用 `test_execution.py`、`test_paid_run_safety.py`、`test_jobs.py`、`test_claude_code_provider.py`、`test_diagnosis.py`；使用 fake provider/job/clock 故障注入，不用真账单证明预算正确。本节标“新增”的测试随 ROADMAP §3（v0.3）第 1 步（agent 组件与硬上限）、第 2 步（统计与速度）、第 3 步（k 候选与筛选）一起写。
