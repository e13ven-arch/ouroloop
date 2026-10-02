# M4：自动发现、实验选择与经验循环

> 首版必需：从任务反馈自动演化能力包代码/材料，并改进和回用 R。复用 `research/agent.py`、`basic.py`、`context.py`、`experiment.py`、`loop.py`、`evolve/experience.py`；第二版再接 actor 蒸馏/controller 新训练，不新增研究服务或调度器。

## 1. 输入、输出与启动

用户输入是环境、目标、工具/权限、初始模型、可用训练资源和预算。M7 使用 preset 生成 W0/R0 和默认配方目录；M4 不要求用户事先准备优化工作流。R0 使用已有 provider，默认有限串行搜索；actor、R 和 teacher 可共用供应商，但实际角色、身份和调用用途分别记录。

ResearchContext 由白名单投影得到：当前 W/R/K、包内文件与固定 API 说明、获准 train/fit 任务反馈（含下述固定投影的 train/fit 失败摘要与断言明细）、实际装配/工具/hook 调用、Requirement、实验经验及剩余预算。首版不读任意仓库目录；第二版增加数据/配方目录。不能拿 Runtime、oracle 句柄、凭据或文件句柄；final 不进入研究，research_dev 只允许冻结外层汇总，不给分支。dev 可按既有规则选择候选，包代码/技能/记忆编写重新使用 train 受限上下文，不能把 dev 答案写进共享组件。

首版诊断输入：R 上下文为每个失败的 train/fit 尝试提供失败摘要，含任务文本、逐轮动作（工具名、参数摘要）、工具错误、截断后的长返回（api_search 目录、base64 正文等）、终止/完成原因、实际工具/hook/技能读取事件，以及逐条断言结果（每条断言 type/params/passed；评分器排除的断言不给，即作者声明不计分、或初始即成立且结束时仍成立的断言；初始成立但被破坏的断言按失败计分，照常作为失败明细给 R）。dev 只给汇总，final 不进入 R 上下文，dev/final 都不给断言明细和轨迹。这是本版变化：v0.5 的 `task_results_summary` 只给逐题 success/partial/终止/失败类，研究员看不到任何断言明细（`ouroloop/research/context.py:116-146`，docstring 在 `:120`）；首版放开 train/fit 明细，因为定位“漏了哪一步”最直接，且不涉及 dev/test，泛化由 dev gate 把关，包内材料仍只写通用规则；明细里的 params 含具体期望值（邮箱、列表 ID、记录名），草稿检查的字面量检查拒绝把它们或任务特定标识写进包（M0 §3）。

首版新增判分明细存储，与结果分开存：M3 的 `run_tasks`（`ouroloop/evolve/task_eval.py`）在追加结果之后把每次尝试的逐条断言结果（type/params/passed/excluded）另存 `bench/<domain>/grading.jsonl`，不进入 results.jsonl。M7 的结果存储（`ouroloop/benchmark.py`）提供按冻结 split 过滤的读取接口，默认只返回 train，非 train 返回空；失败摘要由 M4 在 `research/context.py` 投影，只经该接口取明细，dev/final 明细即使存在于 grading.jsonl 也不进入上下文；原始判分记录完整保留，供审计与复现。`prompt_state` 的 12,000 字符整体截断（`context.py:50`）不适用于失败摘要；摘要按条目有长度上限，由 preset 固定。

诊断按串行 map/reduce 执行：每个纳入本轮诊断的失败任务一次笔记调用，写失败类别、根因、可泛化机制和建议组件种类，并论证其适用于一类任务，不写单题答案；再一次汇总调用读取 train 血缘笔记、当前包及经验的 train 受限投影（dev 结果只经固定代码投影的通用枚举/统计进入，M0 §4），产出至多一个 patch 或结构化 next_action，patch 保持 train 血缘。笔记是 train 血缘 artifact，计入预算。笔记身份是渲染后的完整笔记请求摘要（system 提示、格式与注入的全部字段：失败摘要 refs 与投影版本、K 允许的组件种类与优先级、长度上限、包/manifest 摘录、笔记 schema 版本）加 provider/模型/推理配置；完全一致才复用、不重复收费，任一变化即重新生成：R1 改了 diagnose，就不能用 R0 的旧笔记证明 R1 已生效；只改 propose 时，身份一致的笔记可以复用；阶段 A 在只允许文本组件的 K 下写的笔记，不会在允许代码的 K 下被复用。M6 两支的共享规则见 [M6 §2](06_RESEARCH_CAMPAIGN.md)。每轮汇总调用读取的笔记数上限（缓存命中与新生成合计）由 preset/K 固定（示例 30），R 不能提高，这样各轮、各支的诊断覆盖可比；失败任务超过上限时，按最近一次被纳入诊断的轮次从久到新选择（从未纳入的最先），同序按冻结 split 顺序，避免排在后面的失败始终得不到诊断。选中与未纳入的任务都记录，汇总调用与经验写明覆盖：已纳入笔记数/本轮诊断工作集失败数，并注明工作集范围（重跑子集与新增补采），未重跑的 train 任务不在分母内，不能把部分诊断表述为全部失败都已分析。笔记调用使用被 pin 的 `research.diagnose` 配置，汇总调用使用 `research.propose`，两者按 [M6](06_RESEARCH_CAMPAIGN.md) §4 记录实际加载的 R 与提示摘要，R1 的改动因此作用于这两步。两类调用都复用实际绑定 R 的现有 research provider，不建子 agent 平台；这对应 Harness-Zero 的“子 agent 逐题笔记 → 主 agent 汇总”，本项目改为串行（对照见 [技术选型](../TECHNOLOGY.md) §4）。

这改变了 `research.diagnose` 的契约。现有代码中它每轮调用一次，输入 research.context.v1、输出 requirement_list.v1（`ouroloop/policies.py:53-54`、`ouroloop/research/experiment.py:238-242`），默认提示要求只列需求、不提修复（`ouroloop/research/basic.py:231-234`），无提案时按是否有需求回退为 collect 或 stop（`experiment.py:250-252`）。首版登记带版本的逐题契约：输入 failure_summary.v1，输出 failure_note.v1（失败类别、根因、可泛化机制、建议组件种类与泛化论证）；`research.propose` 读取笔记集合与覆盖，产出 Requirement 列表与至多一个 patch 或 next_action，无提案回退改按该 Requirement 判定。默认 R0 的 diagnose 提示随之更新，形成新的 R0 身份，首版活动重新 bootstrap；旧 requirement_list.v1 路径保留给旧模式，旧 R 候选与经验按其原 schema 解释，不与新笔记混用。

输出沿用 ProposalResult，区分合法候选与结构化 next_action。无候选时 `collect` 是可继续的准备请求，只有 none/stop 或无法补齐的原因才结束当前 worker 段；repair_runtime 表示明确的工程能力缺口，不授权自动改代码。R 返回假设、证据、变更意图、预期和放弃条件；固定代码负责 refs、实验身份、权限、预算、评测和安装。`origin=manual/scripted/llm` 由调用入口写入，模型不能自报或改写。

读不出的回复不是 R 的决定（2026-10-02）。“读不出”包括：空回复、拒答（供应商返回 `stop = "refusal"`；manual 回复写 `"stop": "refusal"`）、非 JSON、缺少必需字段，或提案形状不合法（目标或 kind 不在注册范围内、change 形状不对）。按步骤处理：

- 逐题笔记（`research.diagnose`）：
  - 单条读不出时，记入 coverage.invalid（含 task_id 与原因），不缓存，跳过这一条，本轮继续，用其余笔记汇总。
  - 全部笔记都读不出时，不调用汇总，本轮记 `outcome=invalid`。
- 汇总（`research.propose`）：
  - 读不出，或提案形状不合法，本轮记 `outcome=invalid`，计入轮次与 patience（不计入基础设施失败上限），原因写明是哪一种。
  - 不得回退成 collect 或 stop，也不得静默丢掉不合法的提案后再解释剩下的内容。
- 修订：读不出的修订算一次失败的修订，占用一次 `max_draft_repairs`；用尽后按 §2 记 invalid。
- 研究位：
  - 读不出或提案不合法，记 invalid，不记 `no_proposal`。研究位的提案不合法指：proposals 不是列表、某项不是对象、目标不是 `research.diagnose` 或 `research.propose`、没有新文本，或文本与当前提示相同。每项都校验，排在后面的不合法提案同样使研究位 invalid；多出来的合法提案按上限记为 dropped。
  - 没有提案时，next_action 须为 none 或 stop，缺失或取其他值算读不出。只有读得出、没有提案且 next_action 合法的回复，才记 `no_proposal`。
  - 提案文本不截断：超过 K 的提示长度上限时构建失败，研究位记 invalid（stage=build）。
  - `rsi/cycle.json` 中该步记 outcome、reason 与 stage：reply（R 的回复）、admission（准入拒绝）或 build（构建失败）。
  - 报告的 C4 保持 not_run，detail 写“研究位回复无效”及原因，不写“R 没有提案”（[M7 §4](07_BENCH_REPORT.md)）。
- 截断：回复在输出上限处被截断，任何一步都属于配置问题，按 [M5 §3](05_EXECUTION_BUDGET.md) 暂停活动；续跑时用改正后的配置重新询问同一步，不计 invalid。

这些规则对由 manual provider 作答的回复同样适用。付费前彩排（[ROADMAP §3](../ROADMAP.md) 首个真实活动协议）逐步注入这些回复形态来验收。

worker 轮的部分已在阶段 3 实现（2026-10-02）：
- 汇总回复空、拒答、非 JSON、无提案也无 next_action、或带形状不合法的提案时，本轮 invalid（`test_research_round.py::test_an_unreadable_reply_is_an_invalid_round`）。
- 逐题笔记全部读不出时不调用汇总（`test_diagnosis.py::test_no_readable_note_makes_an_invalid_round`）；单条笔记读不出时，coverage.invalid 记下真实原因（拒答、空回复、非 JSON 或缺字段，`test_an_unreadable_note_records_why_it_could_not_be_read`）。
- 汇总回复里的每个提案都校验，排在上限之后的不合法提案同样使本轮 invalid；上限之外的合法提案记为 dropped。
- 截断在任何一步都按 M5 §3 暂停。

研究位的部分已实现（2026-10-03）：
- 研究位回复空、拒答、非 JSON、无提案也无合法 next_action，或带不合法的提案时，研究位 invalid，不记 no_proposal（`test_cycle.py::test_an_unreadable_slot_reply_is_an_invalid_slot`）；R 明确回复 stop 或 none 且没有提案时，仍记 no_proposal（`test_a_slot_decision_r_states_is_still_no_proposal`）。
- 准入拒绝与构建失败分别记 stage=admission 与 stage=build（`test_a_slot_that_admission_or_the_build_refuses_says_which`）。
- 研究位回复被截断时暂停，续跑时重问同一步，每次调用只记一次账（`test_a_slot_reply_cut_at_the_output_limit_pauses_and_is_asked_again`）。

研究位记 invalid 后不自动重问，这一步就此结束，cycle 照常进入下一段；重问要在下一段跑完之后，会让 R1 的回用检查读到安装前的段。

## 2. 首版可执行的候选空间

| 失败证据 | 候选/处理 | 必须具备的条件 |
|---|---|---|
| 漏步骤、重复动作、工具顺序不当 | 改 `worker.system_prompt` 或 `worker.finish` 中的技能指令/检查流程 | 改动可由现有运行时执行；任务反馈能检验 |
| 反复分页/漏读状态/工具使用繁琐 | harness_edit 生成或修改包内工具封装 | 只组合获准基础工具；固定 API 与隔离测试通过；真实调用可查 |
| 写入前缺检查、工具失败未处理、过早结束 | harness_edit 修改 before_tool/after_tool/on_finish 中间件 | 不改硬评分/权限；真实 hook 触发与结果可查 |
| 多任务重复失败、已有组件重叠/无效 | 修改、合并、停用技能/经验/组件 | 通用规则来自 train；变更仍走 gate，不能运行中偷偷卸载 |
| 已验证方法需模型固化（第二版） | SourceWorkflow 适配后 distill_actor | 源方法可用、目标动作可映射、训练后端通过检查 |
| 有限决策判断错误（第二版新增训练） | decision_train；首个复用 worker.stop | 状态真值、校准数据和同 actor 任务比较 |
| 工具缺失、反馈缺失、actor 未真正加载 | repair_runtime/collect/stop，并报告缺口 | 不把工程故障交给模型训练来掩盖 |
| 反复选错实验、重复失败、浪费采集额度 | research owner 的注册策略候选，交 M6 | 在已配置 research slot 中执行，共同资源边界 |

首版默认方法是 policy_edit/harness_edit，新增训练方法不进入菜单或账户预检；旧 trainer/API 不删除。每轮至多一个候选，以当前已采用包为起点，优先修已有机制；没有收益时保留原版本、分析失败或停止。包文件数/大小、hook/工具数、冻结前草稿修复次数、每轮失败笔记数与测试时间由 preset/K 固定，不允许 R 提高上限。

组件种类优先级为 memory/skills → on_finish 守卫 → before_tool/after_tool 守卫 → 包工具；逐题笔记的建议组件种类与汇总 patch 按此顺序优先选择。包工具置后的依据是 Harness-Zero 三个学生库最终共 12 个中间件、3 个 skill、3 个工具，工具只出现在 USPTO（化学校验），SpreadsheetBench 与 AppWorld 没有工具；memory/skills 置前是因为纯文本包无需隔离 worker、可在阶段 A 先交付（[ROADMAP](../ROADMAP.md) §2），并非来自 Harness-Zero 的组件数量。改上下文的组件（context_guard 类）按设计不支持，是有意取舍（TargetHarness 固定上下文算法）；其余挂点覆盖见 [M1](01_POLICY_RUNTIME.md) §1。

冻结前试跑-修复：patch 规范化成草稿包后，M5 在同一 operation 日志里做 package_check → isolated_probe → package_test（草稿检查）；只含 skills/memory 的草稿只做结构/路径/manifest 检查，不需要 isolated_probe。失败时只把检查输出（不含任何任务结果或评分）回传 R，R 可修订；修订次数上限由 preset/K 固定（默认 3，指初稿之后最多修订 3 次、最多 4 份草稿），R 不能提高。每次草稿都有新包 ref，逐次记录并计费；只有检查通过的草稿才冻结进 Experiment；用尽次数则本轮以无效提案结束并计入轮次。冻结后源码不变；building 按相同输入摘要复用已确认的草稿检查，不重复运行。草稿修复不形成新 proposal、不另占 schedule 候选轮次，也不把无限修复藏在一个 build 中。

新增包内 Python 是首版支持的候选，不限于选择已有组件。R 返回路径明确的增改删 patch、manifest 变更、证据与假设；固定代码在对象库的新包副本应用/冻结，禁止修改工作区仓库或当前 active 的文件。包内入口经 M0 结构准入和 M1 隔离装配后可执行；草稿要求新增宿主 API、基础工具权限或依赖时，草稿检查按结构/接口错误失败，检查输出回传 R 并占用一次修订；确属工程能力缺口时 next_action=repair_runtime，属于后续工程接入。候选自写测试只能辅助接口/机制检查，最终效果由固定任务 verifier/gate 决定。

失败 train 工作集用于定位机制；候选采用前仍评测完整固定 dev 清单，不从 dev 移除本次对照版本成功的任务。沿用 M3 的冻结门槛决定采用与否；首版不另外维护历史成功任务池或设计回归修复流程，已有评测无法解决的具体问题在实跑后再判断。未装配/未调用的工具先修可发现性或删除重复项，不通过继续加组件掩盖问题。停用/删除也生成新包候选并走 gate；只记录组件使用与整体效果，不为首版建设全面自动消融搜索。

## 3. 工作流发现与教学材料的衔接

首版以 deployment 用途（purpose 字段保持 None，M0 §3）直接构建能力包并走完整任务 gate，成功后发布；不依赖 SourceWorkflow、教师适配或训练。以下 source_validation → 教学材料的接线归第二版，原规格保留，不能成为首版默认必经阶段。

第一步由 R 在 train/fit 受限上下文中产生可执行 worker changes 和方法说明。仅用于教学时，固定代码冻结 `purpose=source_validation` 实验；M1 真正绑定候选，M3 在 train/fit 任务验证，M5 以 validated 结束成功分支。SourceWorkflow 引用确切候选 release/harness、guide 和验证 artifact，随后立即可用于适配；不必先跑 dev gate，也不改变 active。

如果目标是直接发布该工作流，则使用 `purpose=deployment`，必须完整生产 gate 后才能安装。已做源验证的方法后来决定发布时创建新 deployment 实验，复用候选构建产物，不重做已确认工作，也不继承不存在的安装权。用途由已校验的意图在 freeze 时写入，不由执行器事后猜测。

source validation 的任务清单、尝试次数和成功/回归要求在运行前冻结。证据不足可以保留为 hypothesis，再在预算内补一次声明过的采集；不得把未跑的方法标 validated。成功方法才能进入默认自动蒸馏路径。所有失败保留在经验中，重复相同输入的实验由既有签名检查拒绝。

第二步为材料准备：R 选择已验证 source 与目标 h/配方 key。若目标 h 不同于当前 parent，先按 M3 `prepare_harness` 创建受限基线 release 并运行新基线；完成后才以它作为 execution base/student release，调用 M8 `adapt_workflow`。目标不变则直接沿用 parent。固定代码校验每个选择的 scope、能力和余量，R 不能自行拼装一个未经检查的执行起点。M8 在 train 受限上下文中自动生成 TeachingReference；固定校验通过后生成 DistillationPlan，再构造完整 v3 proposal 并按原 admission/freeze 流程执行。LLM 返回的选择意图是内部临时值，不是缺失 ref 的可执行 ExperimentProposal。

适配调用属于活动预算下的准备 operation，输入先冻结，结果按身份复用；失败保留费用，不开始训练。source 验证、适配与蒸馏实验形成 ref 依赖，不用虚构未来 artifact，也不新建一套计划状态机。最终蒸馏 build 可复用完整匹配的 reviewed collection，或调用 M8/M2 采集训练。

dev 汇总可以提示“完成检查不充分”，但包含 dev 任务事实的上下文不能生成 train guide。选择意图仅投影注册目标、通用失败类别和已有合规 source ref；参考编写重新加载 train/fit 内容，记录实际输入 parents。若无法证明该投影不含任务事实，拒绝训练材料准入，回到 train 诊断。不是把同一对话改名为 train。

## 4. 一轮逻辑与准入

### 补采请求的分派

M4 将模型请求或 admission 的 `collect` 结果规范化为 M0 CollectionRequest，校验后返回 outcome=needs_collection，并通过 RoundResult.next_action/action_ref 交还同一个循环驱动。M7 仅提供 collector handler，M5 执行 operation；不让用户再敲一次 collect 命令。`improve`、cycle 和 M6 分支共用这段驱动，避免只有 CLI 快捷路径支持补采。

| 情况 | 固定处理 | 继续条件 |
|---|---|---|
| 缺少可获得的完成状态真值 | plain 采集，调用已注册 snapshot/feedback builder，仅 train | 新增合格反馈后重建 context，再提案/准入 |
| actor 轨迹不足且已有合法教学 plan | reviewed 采集，pin plan 的学生/harness，扩充冻结任务选择允许的缺口 | 新增合格轨迹后重建 context，重新冻结训练 proposal |
| 缺教学来源/plan | 先提出 source_validation 候选；不能把 plain 日志假装成 actor 模仿数据 | 源验证/适配完成后才能 reviewed 采集 |
| 首版任务反馈覆盖不足 | purpose=task_feedback 的 plain attempt，只选许可 train 任务，不生成决策标签 | 新增合法任务结果后重建 context，再提出包候选 |
| 缺工具、状态评分能力或兼容运行时 | repair_runtime，记录具体 capability gap；不反复采集同样的无效数据 | 本版停止受影响 worker 段，不自动写新工具代码 |
| 无值得试的方法，或数据池/次数/预算已耗尽 | none/stop，保留原因和已完成工作 | 按 M7 的活动 schedule 判断是否还有已计划 research slot |

默认 preset 允许每个预先分配的 worker 段最多 2 批补采；segment_id 及限额在活动启动前冻结，不由 R 自增。重新调用 phase 不产生新段或新额度；research slot 后的下一 worker 段只能使用启动时已分配的额度。每批都有 task/attempt/token 限额，属于原预算根，并预留一次后续提案/准入的轮数与费用；无法预留则不启动补采。每批占用一个 schedule 轮次，阶段切换/重启不重置计数。

采集结束以新获准 evidence refs 重建 context。出现新增合格反馈/轨迹才记 data_progress；重复已有 artifact、仅生成新文件名或失败轨迹不算进展。无新增合格数据就结束该 worker 段，避免采集空转；需要改变工作流时由另一次有证据的候选尝试处理。补采本身不增加候选晋升计数，不作为模型改进收益，也不清零原候选失败次数。

同一请求输入和 task attempt 身份相同就复用完成结果，只有冻结池内未跑的任务/attempt 才可产生新批次。中途重启先恢复 pending collection，不能重新向 R 询问后把旧请求丢掉。具体状态与预算见 M5。

### 一轮执行

复用 run_worker_round，在既有 normalize/admission/build 按 method 分派；首版包路径与第二版训练路径共用以下驱动：

```text
pin current W/R/K and permitted experience snapshot
preflight the current application's complete package and required runtime capabilities:
    if the package runs code (M0 §2 runtime predicate) and the isolated worker is unavailable:
        record repair_runtime and suspend the activity (no further segments or research slots);
        resume this round after smoke re-confirms the worker;
        do not remove inherited code, switch packages, or run in-process
measure current application under the permitted development plan:
    reuse only confirmed results whose actual task execution identity and plan conditions match;
        keep their original release/attempt/result refs, not a new measurement
    if the previous round installed nothing and required results are complete and match:
        reuse results (no new task runs)
    elif a candidate was installed:
        dev (R's context only): no per-round dev run; R reads the gate's aggregate figures (gain,
             regression rate, pair and family counts) through the experience projection, labelled as the
             draw that led to acceptance; every gate still reruns its own control arm
        train diagnosis: rerun the train tasks that failed in the latest diagnostic set;
             unretested passing results remain historical, not current-version passes
    else (no install occurred but execution/plan conditions differ or results are incomplete):
        do not use the old measurement for the missing scope; run only permitted required attempts,
        or report insufficient evidence/runtime capability
merge new valid train collection results into diagnostic context using their actual identities
build failure summaries for failed train/fit attempts (split-filtered assertion detail for train/fit only;
    dev: aggregate summary only; final: never enters R context)
diagnose and retrieve using the actually bound R:
    select failures for this round, capped by the frozen per-round note limit (cached + new):
        least recently included first (never included first), ties in frozen task order;
        record selected and excluded tasks and the diagnostic working-set scope
    one note call per selected failure (research.diagnose: failure_summary.v1 -> failure_note.v1);
        notes are train-lineage and budgeted; reuse only on full identity
        (rendered note request digest + model/inference config)
    one summary call (research.propose) over notes, note coverage, current package and train-restricted
        experience: Requirement list plus at most one patch or a structured next_action
propose a bounded intent: at most one patch or a structured next_action
if collect is requested (including admission's data-gap result):
    validate/freeze CollectionRequest; persist next_action and action_ref
    dispatch collector through M5 within the frozen schedule
    if new valid evidence exists: rebuild context and use the next allowed round
    else: end this worker segment with a specific reason
elif no method or missing runtime capability: end this worker segment
else:
    if harness_edit is selected:
        draft = normalize bounded patch into a complete draft package, inherited entries/files included
        for the first draft and each revision (at most max_draft_repairs revisions after the first):
            kind and literal checks on the complete draft (kinds within K; no train assertion literals)
            if the draft needs the worker for checks (M0 §2 check predicate) and it is unavailable after
                the single retry policy: record outcome=infrastructure_failed (no revision consumed, nothing
                returned to R, counts toward max_infrastructure_failures only); repair_runtime: suspend the
                activity; on resume re-run this draft's check with the same draft ref and revision index
            draft check through M5: package_check -> isolated_probe -> package_test on a fixture or
                train-only world (skills/memory-only draft: structure/path/manifest only)
            if the check passes: stop revising
            elif revisions remain: return capped, redacted check output only to R; revise into a new draft
        if the last allowed draft still fails: record outcome=invalid (the round counts); skip freeze/execute
        else: freeze only the passing draft into the package proposal; source is unchanged after freeze
    if target h changes: prepare the new execution base and baseline through M3
    if distillation is selected: adapt validated source and freeze teacher plan
    normalize proposal; admit; freeze Experiment(purpose, parent, execution_base)
    execute through M5
    if source_validation passes: record SourceWorkflow, no install
    if deployment is accepted: M3 may install against parent
append facts, failures, costs and actual loaded refs; obey the fixed schedule
```

每轮 measure 区分诊断与正式评测：新包安装后，默认只重跑上一诊断工作集中失败的 train 任务，以定位剩余问题；未重跑的成功任务保留原版本历史，不视为新包已通过，更不能把不同版本结果拼成新包完整 train 分数。正式 dev gate 仍对当前已采用版本与候选执行完整固定 dev 配对比较，逐题结果及退化比例按 M3 判断；它只说明该 dev 范围的表现，不证明未重跑的具体 train 任务仍成功。完整且匹配的候选 gate dev 结果可用于下一轮 measure，但只进入 R 的上下文，并标明是促成安装的那次抽样（受选择影响偏乐观）；gate 每次都在自身 plan 内重跑对照臂（`ouroloop/bench.py:758-780`），从不复用它；也不能以 train 诊断子集代替。

复用按实际任务执行身份检查，至少包含 W/package、实际模型与推理配置、宿主/隔离配置与工具权限、环境及评分版本、任务/attempt、冻结计划与运行限额；保留原 release/attempt/TaskResult refs，不重写身份或计作新的独立样本。托管模型标 mutable=true 时，复用限本活动内并在报告注明。上一轮未安装（rejected/invalid/infrastructure_failed/needs_collection/no_proposal 等）只是可能复用的条件：上述条件一致且结果已确认有效，才无需新增任务运行；缺项或条件变化时，按冻结计划/预算补必需尝试，无法补齐则报告证据不足或能力缺口。仅 R 改变且任务执行配置未变时可复用业务测量；R 调用产物（逐题笔记）只在 §1 的完整笔记身份一致时复用。补采产生的新合格 train 反馈仍按实际身份进入诊断上下文，不能因复用 measure 被丢弃。

准入按 method 分派：首版 harness_edit 检查包路径/结构/API/隔离能力（仅对含代码的包，M0 §2）/完整评测预算，不要求决策标签或教学 plan；准入时隔离 worker 不可用按 M0 §3 的基础设施故障处理（重试后 infrastructure_failed 并暂停活动），不记 invalid、不计失败上限；第二版 decision_train 检查事实反馈，distill_actor 检查教学计划/能力/预算，再由 M2 检查成功轨迹。固定核心检查不能由 R 的辅助预检删除。

每轮至多一个可执行候选或一批补采，候选至多一次 training 条目；沿用现有 schedule 并添加补采计数，限制轮数/失败上限/耐心。冻结前草稿修复与逐题笔记属于本轮，次数按冻结上限，不另占候选轮次；修订用尽的轮次计入候选轮次与失败上限。源方法实验和蒸馏实验分别占用轮次/子操作额度；不能把无限源搜索藏进一次 build。R 没有直接开作业、设置门槛或安装的权限。

诊断区分：teacher 也失败时查工作流/目标能力；teacher 成功而无教师 actor 失败时查可学数据、表示和训练；controller 未调用时查接线；有调用但更差时查真值、误拒和费用。依据证据选择下一动作，不自动加模型、专家或工具。

## 5. 经验与递归接口

Experience 记录 proposed/built/evaluated/validated/installed 的事实，引用 source、plan、dataset、模型身份、任务结果、成本、实际 R 和失败阶段。hypothesis 与事实分开；prompt 候选不能记 trained=true。后续 context 通过现有检索规则选择获准经验，不复制所有轨迹。

安装 W1 后下一轮任务运行/采集 pin W1；旧 W0 轨迹按其身份保留。W 自己的业务能力更新不自动证明 R 改进。在固定 research slot 中，当前 R 根据实验产出提出注册 R 候选，由 [M6](06_RESEARCH_CAMPAIGN.md)比较；安装后下一次真实诊断/提案必须加载 R1。

研究位的请求（`ouroloop/research/basic.py` 的 `RESEARCH_FORMAT` 与 `PolicyResearcher.propose_research`）：system 提示是 R 自己的 `research.propose` 文本，后接研究位格式。保留这段文本，因为 R 正是用它提出对自身的修改。格式声明：前文是 worker 轮的改包指令；本次调用改的是 `research.diagnose` 或 `research.propose` 本身，不改包；两者在读什么、改什么、回复什么上不一致时，以格式为准。输入是目标、预算、经验投影（最近 20 条）与两段提示的当前文本。格式是代码，不属于 R 的身份；R0 文本冻结在 release 中，不随代码改动。

当前代码在 `run_worker_round` 把无 proposal 的 collect 请求折叠为 no_proposal，`run_worker_rounds` 随即停止。本版必须修改这两个分支，保留结构化 action 并先分派补采；仅在文档或日志里写 collect 不能通过验收。旧 v2 模式维持原行为；只有新 driver 启用该协议，并写入带 next_action/action_ref 新字段的 RoundResult。

首版无研究员专用训练、自治专家群或全仓任意代码改写；包内代码演化是明确支持的范围。人工 proposal 用同一执行器调试，scripted 用于接线；二者不能作为真实自动发现的成绩证据。训练/教学相关分支在第二版启用，未启用时不可隐式采样教师或探测 Tinker 账号。

## 6. 用例

| ID | 条件 | 预期 |
|---|---|---|
| RL6-01 | 人工、scripted、LLM 提同一合法候选 | 同一 admission/executor；origin 由入口分别记录，模型不能自改 |
| RL6-02 | prompt/工作流已满足目标，无训练价值 | 可以发布/停止，零 trainer 请求 |
| RL6-03 | dev 答案进入参考或候选修改 gate | 固定准入拒绝，保留来源和原因 |
| RL6-04 | teacher 采集成功，SFT 或无教师评测失败 | 不安装；经验区分失败阶段及费用 |
| RL6-05 | W1 安装后继续采集 | 实际用 W1，新 plan/ref；旧数据不冒充新 W 产物 |
| RL6-06 | 固定 R0 多轮，W 提升 | 报告业务迭代；不虚报 R 更新或 C4 |
| RL6-07 | 仅给任务/工具/初始模型/preset，无人工 source | 自动产生候选并真实执行验证，能继续适配与蒸馏 |
| RL6-08 | source 尚未验证或目标工具缺失 | 不进入默认训练；source 未验证返回待验证，目标工具缺失时 M8 映射标 unsupported、round 以 repair_runtime 报告能力缺口，无 GPU 请求 |
| RL6-09 | 相同 source/目标的适配完成后重启 | 复用匹配产物，不重花材料生成费用 |
| RL6-10 | 数据池/补采限额/预算耗尽、候选无效或耐心耗尽 | 有界结束，无隐藏实验/无限训练；基线和失败报告完整 |
| RL6-11 | 无 proposal、next_action=collect，预算/任务充足 | 自动补采并重建 context，下一轮提出候选，无人工命令；不提前 no_proposal 退出 |
| RL6-12 | admission 要求补标签；或只有最终 reward 不支持状态标签 | 前者进入同一补采路径，后者 capability gap 停止；不盲采/伪造标签 |
| RL6-13 | 已验证 source 仅作教师来源 | 不调用生产 gate/installer，下一轮可适配和蒸馏 |
| RL6-14 | 补采无新增数据或同请求重启 | 无数据时结束；重启复用 pending/done 事实，限额不重置 |
| RL6-15 | 没有人工组件，仅有 train 失败轨迹和 package API（代码组件，阶段 B） | 自动提出 tools/middlewares 文件 patch，固定代码冻结新包；实际执行后才评测，零 trainer/teacher 请求 |
| RL6-16 | 提案包含具体 dev 答案、越界文件或重复组件 | 内容来源/路径准入拒绝前两者；后者优先合并或提出删除候选，不直接改 active |
| RL6-17 | train 尝试失败；grading.jsonl 同时存有 dev/final 尝试的断言明细 | train 失败摘要与断言明细进入 R 上下文；dev/final 的断言明细和轨迹永不进入；dev 只见汇总，final 不进入 |
| RL6-18 | 草稿包检查失败 | R 只见检查输出并修订，初稿之后最多 max_draft_repairs 次（默认 3，最多 4 份草稿）；每次草稿新包 ref、逐次记录计费；次数内通过则冻结；用尽则本轮以无效提案结束、计入轮次；冻结后源码不变 |
| RL6-19 | 多个 train 任务失败，失败数超过新笔记上限 | 逐题笔记 → 汇总产生至多一个 patch；笔记为 train 血缘 artifact 并计费；从未笔记过的失败优先，其余按冻结顺序补足；选中/未笔记任务与覆盖率写入汇总输入和经验；上限不可由 R 提高 |
| RL6-20 | 上一轮候选经完整 gate 安装后进入下一轮，执行身份和计划条件匹配 | 复用该候选已确认的 gate dev 结果；train 诊断只重跑上次失败子集，未重跑成绩标为旧版本历史，不生成新包全量 train 分数 |
| RL6-21 | 上一轮未安装，但任务执行身份/计划条件变化，或所需结果缺失/未确认 | 不直接沿用旧测量；仅按冻结计划和预算补必需尝试，不能补齐则报告证据不足/能力缺口，不虚报零运行的有效测量 |
| RL6-22 | 上一轮未安装，完整匹配测量已存在；collect 又产生合格 train 反馈 | 不重复测量已确认尝试，原结果身份保留；新反馈进入诊断上下文，不因复用而丢失 |
| RL6-23 | K 只允许文本组件时 R 提出含代码草稿；或 K 允许代码但运行中隔离 worker 不可用 | 前者为结构错误，检查输出回传 R、占用一次修订；后者按唯一重试策略重试后记 infrastructure_failed 并暂停活动（不再调度后续段与 research slot），恢复后以原草稿 ref 与修订序号续跑；不耗修订/失败额度、费用照计，不中途收窄组件种类，不 in-process 执行 |
| RL6-24 | R1 只改 propose；另一次 R1 改了 diagnose | 前者复用完整身份一致的旧笔记，汇总调用按 R1 的 propose 配置运行并记录配置摘要与笔记 refs；后者在同一笔记上限内重新生成并记录覆盖，旧笔记不能作为 R1 生效的证据 |
| RL6-25 | train 尝试同时含作者声明不计分的断言、初始即成立且保持的断言、初始成立但被 agent 破坏的断言 | 前两者不进入 R 上下文；第三者作为失败明细出现；grading.jsonl 原始记录三者齐全 |
| RL6-26 | 汇总 patch 把 train 断言中的邮箱/记录名写进 memory 或技能 | 字面量检查判结构错误，检查输出回传 R 并占用一次修订；通过的包不含 train 任务特定标识 |
| RL6-27 | 首版活动 bootstrap 与旧模式活动各跑一轮 | 首版 diagnose 逐题产出 failure_note.v1，propose 产出 Requirement 与至多一个 patch；旧模式仍走 requirement_list.v1，旧 R 经验不与新笔记混用 |

扩展 `test_research_round.py`、`test_roles.py`；复用当前 admission 与 executor，接口变化必须有旧模式兼容测试。
