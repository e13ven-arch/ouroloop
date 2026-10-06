# M4：自动发现、实验选择与经验循环

> 首版必需：从任务反馈自动演化能力包代码/材料，并改进和回用 R。复用 `research/agent.py`、`basic.py`、`context.py`、`experiment.py`、`loop.py`、`evolve/experience.py`；第二版再接 actor 蒸馏/controller 新训练，不新增研究服务或调度器。
> v0.3（2026-10-04，[DESIGN](../DESIGN.md) v0.7）：R 的汇总步从“一次 JSON 回复给一个 patch”改为 worker 内对包目录的一次有界编码会话（每轮一次，修订续写同一会话），每轮至多 k 个带声明的候选，先过 train 侧筛选再付 dev gate；引擎自有的简单台账进入 E，准入拒绝重复的 (mechanism, site)；R 的输入增加原始 train 轨迹（只读、限字节）、dev 失败类型直方图与台账投影。失败摘要、逐题笔记、草稿检查、冻结与准入不变。考虑过、不做（v0.3）：成功侧摘要、每段至少一个代码候选、逐题 TRUE FIX/TRUE REGRESSION/STOCHASTIC 标签与预测记分（各在所属小节写明原因）；组件归因与淘汰推迟到第二版（§5）。依据：v0.1/v0.2 装入的两个改动都是文字提醒，13 个候选无一改工具或代码，R 的笔记里 26 条建议 before_tool（[ROADMAP §3](../ROADMAP.md) 第 2a 步第 9 项）；业界报出更大留出增益的框架都改 loop 与工具，且提案者读原始轨迹（[SURVEY §4](../SURVEY_RSI_FRAMEWORKS.md) 第 2、3、5、6、7 项）。本文新增或修改的规则逐条标“（v0.3，2026-10-04）”。

## 1. 输入、输出与启动

用户输入是环境、目标、工具/权限、初始模型、可用训练资源和预算。M7 使用 preset 生成 W0/R0 和默认配方目录；M4 不要求用户事先准备优化工作流。R0 使用已有 provider，默认有限串行搜索；actor、R 和 teacher 可共用供应商，但实际角色、身份和调用用途分别记录。

ResearchContext 由白名单投影得到：当前 W/R/K、包内文件与固定 API 说明、获准 train/fit 任务反馈（含下述固定投影的 train/fit 失败摘要与断言明细）、实际装配/工具/hook 调用、Requirement、实验经验及剩余预算。首版不读任意仓库目录；第二版增加数据/配方目录。不能拿 Runtime、oracle 句柄、凭据或文件句柄；final 不进入研究，研究位的外层池（dev 冻结顺序的后一半，[M6 §2](06_RESEARCH_CAMPAIGN.md)）只允许冻结外层汇总，不给分支。dev 可按既有规则选择候选，包代码/技能/记忆编写重新使用 train 受限上下文，不能把 dev 答案写进共享组件。

首版诊断输入：R 上下文为每个失败的 train/fit 尝试提供失败摘要，含任务文本、逐轮动作（工具名、参数摘要）、工具错误、截断后的长返回（api_search 目录、base64 正文等；截断处写明是摘要截的、agent 当时有全文，免得 R 把它读成 agent 漏读）、终止/完成原因、实际工具/hook/技能读取事件，以及逐条断言结果（每条断言 type/params/passed；评分器排除的断言不给，即作者声明不计分、或初始即成立且结束时仍成立的断言；初始成立但被破坏的断言按失败计分，照常作为失败明细给 R）。dev 只给汇总，final 不进入 R 上下文，dev/final 都不给断言明细和轨迹。这是本版变化：v0.5 的 `task_results_summary` 只给逐题 success/partial/终止/失败类，研究员看不到任何断言明细（`ouroloop/research/context.py:116-146`，docstring 在 `:120`）；首版放开 train/fit 明细，因为定位“漏了哪一步”最直接，且不涉及 dev/test，泛化由 dev gate 把关，包内材料仍只写通用规则；明细里的 params 含具体期望值（邮箱、列表 ID、记录名），草稿检查的字面量检查拒绝把它们或任务特定标识写进包（M0 §3）。

首版新增判分明细存储，与结果分开存：M3 的 `run_tasks`（`ouroloop/evolve/task_eval.py`）在追加结果之后把每次尝试的逐条断言结果（type/params/passed/excluded）另存 `bench/<domain>/grading.jsonl`，不进入 results.jsonl。M7 的结果存储（`ouroloop/benchmark.py`）提供按冻结 split 过滤的读取接口，默认只返回 train，非 train 返回空；失败摘要由 M4 在 `research/context.py` 投影，只经该接口取明细，dev/final 明细即使存在于 grading.jsonl 也不进入上下文；原始判分记录完整保留，供审计与复现。`prompt_state` 的 12,000 字符整体截断（`context.py:50`）不适用于失败摘要；摘要按条目有长度上限，由 preset 固定。

诊断按串行 map/reduce 执行：每个纳入本轮诊断的失败任务一次笔记调用，写失败类别、根因、可泛化机制和建议组件种类，并论证其适用于一类任务，不写单题答案；再一次汇总调用读取 train 血缘笔记、当前包及经验的 train 受限投影（dev 结果只经固定代码投影的通用枚举/统计进入，M0 §4），产出至多一个 patch 或结构化 next_action，patch 保持 train 血缘。汇总请求还列出宿主的基础工具（名称、描述与参数 schema，`tools` 字段），即包工具与 hook 经 `ctx.call_tool` 能调用的工具：汇总看不到失败过程，写代码组件所需的工具名与参数从这里得到；端点、字段名、actor 消息里的标记这类只出现在失败过程中的细节，由笔记带过去（2026-10-03，第二次彩排发现）。笔记是 train 血缘 artifact，计入预算。笔记身份是渲染后的完整笔记请求摘要（system 提示、格式与注入的全部字段：失败摘要 refs 与投影版本、K 允许的组件种类与优先级、长度上限、包/manifest 摘录、笔记 schema 版本）加 provider/模型/推理配置；完全一致才复用、不重复收费，任一变化即重新生成：R1 改了 diagnose，就不能用 R0 的旧笔记证明 R1 已生效；只改 propose 时，身份一致的笔记可以复用；阶段 A 在只允许文本组件的 K 下写的笔记，不会在允许代码的 K 下被复用。M6 两支的共享规则见 [M6 §2](06_RESEARCH_CAMPAIGN.md)。每轮汇总调用读取的笔记数上限（缓存命中与新生成合计）由 preset/K 固定（示例 30），R 不能提高，这样各轮、各支的诊断覆盖可比；失败任务超过上限时，按最近一次被纳入诊断的轮次从久到新选择（从未纳入的最先），同序按冻结 split 顺序，避免排在后面的失败始终得不到诊断。（v0.3，2026-10-06 改）失败跨几个领域时（任务 id 为 `领域.题名`，且不止一个领域），按领域分层轮流选：每个领域内照上面的规则排（从未纳入的在前，再按最近一次纳入从久到新，同序按冻结顺序），领域按其题在冻结顺序里首次出现的先后，每个领域轮流取一题，取满上限为止——上限 30、五个领域时各 6 题（某领域失败不够时由其余领域依次补足）；未纳入的题按同一轮流顺序记录。逐轮轮转因此在每个领域内进行，各轮依次覆盖每个领域的失败。只有一个领域（或任务 id 不带领域）时，选择与以前逐字相同，旧工作区的笔记身份、覆盖记录与摘要不变。多领域时覆盖记录另有 `by_domain`（每领域的失败数与纳入数），R 在 `coverage.json` 里看到。笔记身份不变，所以已缓存的笔记照身份复用。原因：第 5 步第 2 阶段按合并后的冻结顺序取前 30 题，合并顺序 marketing 在前、sales 其次，两轮笔记是 marketing 27 + sales 3 与 sales 30，support、hr、finance 的失败 R 一道没看到，两个候选都在 marketing 上涨、在 sales 上跌（ROADMAP 第 5 步）。实现 `research/diagnosis.select_failures`；测试 `test_diagnosis.py::test_notes_over_five_domains_are_stratified_and_rotate_within_each_domain`、`test_a_single_domain_selects_notes_exactly_as_before`。选中与未纳入的任务都记录，汇总调用与经验写明覆盖：已纳入笔记数/本轮诊断工作集失败数，并注明工作集范围（重跑子集与新增补采），未重跑的 train 任务不在分母内，不能把部分诊断表述为全部失败都已分析。笔记调用使用被 pin 的 `research.diagnose` 配置，汇总调用使用 `research.propose`，两者按 [M6](06_RESEARCH_CAMPAIGN.md) §4 记录实际加载的 R 与提示摘要，R1 的改动因此作用于这两步。两类调用都复用实际绑定 R 的现有 research provider，不建子 agent 平台；这对应 Harness-Zero 的“子 agent 逐题笔记 → 主 agent 汇总”，本项目改为串行（对照见 [技术选型](../TECHNOLOGY.md) §4）。

这改变了 `research.diagnose` 的契约。现有代码中它每轮调用一次，输入 research.context.v1、输出 requirement_list.v1（`ouroloop/policies.py:53-54`、`ouroloop/research/experiment.py:238-242`），默认提示要求只列需求、不提修复（`ouroloop/research/basic.py:231-234`），无提案时按是否有需求回退为 collect 或 stop（`experiment.py:250-252`）。首版登记带版本的逐题契约：输入 failure_summary.v1，输出 failure_note.v1（失败类别、根因、可泛化机制、建议组件种类与泛化论证）；`research.propose` 读取笔记集合与覆盖，产出 Requirement 列表与至多一个 patch 或 next_action，无提案回退改按该 Requirement 判定。默认 R0 的 diagnose 提示随之更新，形成新的 R0 身份，首版活动重新 bootstrap。汇总只看笔记，所以 R0 的 diagnose 提示要求笔记逐字保留改动会用到的接口事实：actor 调用的工具与端点及其方法和字段名、调用返回的错误原文、actor 结束消息里表明没有做完的原话。这些不是任务答案；任务的名字、id 与取值仍不写（2026-10-03，第三次彩排后决定）。旧 requirement_list.v1 路径保留给旧模式，旧 R 候选与经验按其原 schema 解释，不与新笔记混用。

### R 作为有界编码会话（v0.3，2026-10-04）

汇总步换形，其余保留。逐项的替代关系：

| 阶段 | v0.1/v0.2 | v0.3 |
|---|---|---|
| 失败摘要 | 固定投影，train/fit 明细 | 不变 |
| 逐题笔记 `research.diagnose` | failure_summary.v1 → failure_note.v1；身份、缓存、每轮上限、覆盖记录 | 不变；笔记仍是会话的主要输入 |
| 汇总 `research.propose` | 一次 JSON 回复，至多一个 patch 或 next_action | 一次有界编码会话：R 在 worker 内用文件工具编辑当前包目录的副本，产出至多 k 个候选目录与一份声明文件；`research.propose` 的注册目标与 R0/R1 文本保留，文本成为会话的 system 提示，研究位改它的方式不变（[M6 §1](06_RESEARCH_CAMPAIGN.md)） |
| patch 表示 | R 写的路径明确的增改删 patch | 固定代码由候选目录与当前包的 diff 推出 patch（`ouroloop/research/drafts.py` 的 `patch_of`、`payload_of` 增加目录 diff 来源）；R 不再手写 patch |
| 修订 | 再调用一次 propose，带检查输出 | 同一会话续写：检查输出作为新消息进入会话，R 再编辑；次数仍是 `max_draft_repairs`，会话的轮数与编辑数上限同时约束 |
| 草稿检查 → 冻结 → 准入 | package_check → isolated_probe → package_test；冻结；准入 | 不变；准入新增 `repeated_mechanism`（§5） |
| 候选评测 | 冻结后直接 dev gate | 先 train 侧筛选，只有最佳者付 dev gate（§2） |
| 经验 | E 的 experiment 记录 | 另加引擎自有台账（§5） |

会话的运行方式与边界：

- 会话在 M1 的隔离 worker 中运行（Docker；网关与 broker 在宿主，M1 §1（v0.3）：宿主是运行壳，所有真实工具调用经 broker），工作目录 `/work`：`/work/package/` 是当前已采用包的只读副本，`/work/candidates/<i>/` 是 R 建立的候选目录（起始为当前包的完整副本，R 在其中增改删），`/work/inputs/` 是本轮输入的只读投影（笔记、直方图、台账投影、基础工具清单、包 API 说明）。会话每轮一次：修订不另开会话，检查输出作为新消息进入同一会话（上表）。原因：R 写文件比写 JSON patch 少一层转写错误，v0.1/v0.2 的草稿修订多半在修 patch 形状；目录是 Harness-Zero、StarHarness、AutoSaddler 共同采用的候选表示（[SURVEY §6](../SURVEY_RSI_FRAMEWORKS.md)）。
- 会话工具只有：`list_files`、`read_file`、`write_file`、`edit_file`（限 `/work/candidates/` 下写，`/work` 下读）、`read_trajectory`（见下节）、`finish`。不给 shell，不给网络，不给 `ctx.tool` 的业务工具：检查链由固定代码运行，R 要验证就 `finish` 后读检查输出再修订；会话的每次工具调用由宿主记进会话记录，台账据此核对声明。
- 会话的模型调用走现有 research provider 与 `research.propose` 的 pin 配置和计费，是研究调用，不是 `ctx.model`；每次调用和每次工具调用计入研究支出。研究调用现占花费 3.2%（ROADMAP §3 第 2a 步第 9 项），会话多出的轮数在可承受范围内。
- 上限由 preset/K 固定，R 不能提高：`research_session.max_turns`（会话的模型调用次数，含 `finish`，预设 40）、`research_session.max_edits`（`write_file`/`edit_file`/`delete_file` 次数合计，预设 24，跨全部候选目录与修订；2026-10-05）、`research_session.max_trajectory_bytes`（单次 `read_trajectory` 返回上限，预设 65536）、`research_session.max_trajectory_bytes_total`（一次会话累计，预设 524288）。到达上限而未 `finish`：会话结束，本轮按“读不出”处理（下）。
- 会话以 `finish` 结束，参数是声明文件 `candidate_declaration.v1`：`candidates` 列表，每项 `{dir, mechanism, site, carrier, predicted_affected, trigger_condition, differs_from?, notes}`，或没有候选时 `next_action`（none/stop/collect/repair_runtime，语义同 §4）。字段：`carrier` 取 memory/skill/prompt/hook/tool/agent 之一；`site` 按 carrier 取值——hook 为挂点名（before_tool/after_tool/on_finish），tool 为工具名，agent 为 plan/act/context/verify/finish 之一（M0 §2（v0.3）的 `agent` 组件：`agent/<file>.py:run(ctx, task)`，可改 loop 结构、上下文管理、重试、完成协议、判断前证据表、发送前二次核验、任务内 effort——超过 K 的 `max_effort` 时网关按上限执行并记录请求值，不拒绝（M0 §2（v0.3））——、关键决策多采样），prompt 为 system_prompt/finish，memory/skill 为包内路径；`mechanism` 是不超过 12 个词的短语，固定代码规范化（小写、折叠空白）后用于台账与准入比对；`predicted_affected` 是该候选预计修好的 train 任务 id（笔记来源的任务；train 血缘，只进台账，不进包文本）；`trigger_condition` 写改动何时生效（总是，或某条件）；`differs_from` 见 §5；`notes` 是引用的笔记 refs。
- 固定代码对每个候选目录：计算相对当前包的 diff；`notes` 必须都是本轮笔记的 refs；声明的 `carrier`/`site` 必须与 diff 吻合（carrier=hook 而 diff 不含 hooks 文件、carrier=agent 而 diff 不含 agent 入口，均为声明不符）。不吻合的候选记 invalid（reason=declaration_mismatch），其余候选继续。对应 StarHarness/AutoSaddler 的“声明的改动路径等于实际 diff”检查（[SURVEY §4](../SURVEY_RSI_FRAMEWORKS.md) 第 3 项）。
- 读不出（补充 §1 既有清单）：会话未 `finish` 而耗尽轮数或编辑数、声明文件缺失或不合法、`candidates` 为空且 `next_action` 缺失或非法、`candidates` 超过 k（多出的记 dropped，不使本轮 invalid）。全部候选 invalid 或声明读不出：本轮 `outcome=invalid`，计入轮次与 patience，不回退为 collect/stop。截断仍按 [M5 §3](05_EXECUTION_BUDGET.md) 暂停、续跑时重开会话，不计 invalid。
- 会话看不到：评分器、provider 与凭据、对象库句柄、dev/final 的任何逐题内容与轨迹（§1 既有规则）。候选目录的字面量检查（M0 §3）照常拒绝 train 任务特定标识进包。

（实现，v0.3，2026-10-05；`ouroloop/research/session.py`，worker 协议的 `work` 操作在 `agent/worker_runner.py`。）与上文的出入与设计未写明处的取舍：
- **模型循环在宿主，文件在 worker。** 会话的每一回合由宿主调研究 provider（`PolicyResearcher.session_turn`，经 `_Completions.turn`，计量标签 `session`）；`/work` 树在 worker 里（`work` 操作：`init/list/read/write/edit/delete/export`），R 的每次文件调用由宿主转给 worker 执行并记进会话记录。worker 没有轨迹文件，`read_trajectory` 由宿主答（结果存储按 split 过滤）。理由：会话驱动代码是 K，不该随包进镜像；worker 只保管文件就足以让 R 写不到宿主的任何文件。
- **工具多一个 `delete_file`**（删除技能、hook、工具本就是候选的一种，旧 patch 也有 `delete`）；每次 `write_file`/`edit_file`/`delete_file` 计一次编辑。
- **候选目录由第一次写入建立**：对 `candidates/<dir>/...` 的第一次写、改或删把 `<dir>` 建成当前包的完整副本；`<dir>` 匹配 `[A-Za-z0-9_-]{1,32}`。
- **W 的提示文本以文件出现**：候选目录里 `worker/system_prompt.md`、`worker/finish.md` 是 `worker.system_prompt`、`worker.finish` 的当前正文（有正文才有文件）；改它们就是 `carrier=prompt`（site `system_prompt`/`finish`），固定代码从目录读回成 kind=prompt 的 change，照常做字面量检查（`Drafts.check_texts`），不进包、不做草稿检查。设计只写了载体与 site，没写以什么形式出现在目录里，这是落地的选择。
- **到达上限即结束**：第 `max_turns` 次模型调用仍未 `finish`，或编辑数已到 `max_edits` 时再请求一次编辑，会话结束，本轮 invalid（RL6-29）。`finish` 的参数不合法时作为该工具调用的错误返回 R，R 可在上限内再 `finish`；所以“声明读不出”在实现里是“到上限仍没有一份合法声明”。文字回复不带工具调用时宿主回一句提示，占一回合。
- **会话的草稿事件**（`draft_started/draft_check/draft_finished`）带 `session: true` 与候选 id，`Drafts.pending()` 不续它：续跑重开会话（上文“截断”一条），中断前的草稿不复用。
- 一次 `finish` 可同时声明多个候选；修订时的 `finish` 只需带被修订的那个目录（其余不变）。声明超过 k、同 (mechanism, site) 的后者、与 diff 不符的，分别记 dropped / dropped / declaration_mismatch 进台账，不进草稿检查。重复判定在 declaration_mismatch 之后做：一个不合法的声明不占用它的 (mechanism, site)。
- **修订改的是文件，不是声明**（2026-10-05 评审后）：候选的声明在第一次 `finish` 时定下，台账行、声明对象、`declaration_ref`、提案、准入与筛选读的都是这一份；修订的 `finish` 若对同一目录改报了另一组 (mechanism, site, carrier)，该候选 invalid（原因“the revision redeclared …”），不换声明。
- **工具调用的错误都是工具的错误，不是轮次的**（2026-10-05 评审后）：参数不是对象、路径指向目录、父段是文件、文件系统或编码错误（`FileExistsError`、`IsADirectoryError`、`UnicodeEncodeError`、磁盘满……），worker 一律答成该工具的 `ok: false` 结果交给 R，占一回合或一次编辑，从不是 `kind: "package"` 错误（那会被宿主当作包的缺陷、让本轮崩溃）。写入与编辑受 K 的 `max_package_bytes` 约束（`work init` 带着它，单文件与候选目录合计都不得超过）；W 的提示文件 `worker/system_prompt.md`、`worker/finish.md` 不是包文件，不计入这两项（与 `harness_package.check_shape` 量包的方式一致），但计入下一条的协议行预算；删除它们被拒绝（“to clear a prompt, write it empty”：清空提示就写空文本），2026-10-05 再复核后。同一回复里 `finish` 被接受之后的工具调用不执行，答 “not performed: the session finished”，已接受的声明不因它们失效（哪怕其中一次编辑会碰到 `max_edits`）。文件以 `newline=""` 读写，CRLF 原样往返，没改过的文件不会在 diff 里显得改过。`finish` 拒绝空白或非字符串的 mechanism、site、trigger_condition；一个候选的声明只要有一处不合法（`differs_from` 不是 `[{ref, how}]` 列表、`predicted_affected` 或 `notes` 不是字符串列表……）就不再构建它，所有问题一起作为该调用的错误回给 R（2026-10-05 再复核后）。`read_trajectory` 的 offset/limit 不是整数（含溢出）时是该调用的错误。
- **协议行按转义后的长度算**（2026-10-05 复核后）：worker 的回答以 `json.dumps`（ASCII 转义）上线，控制字符成 `\u00XX`、非 ASCII 字符成 `\uXXXX`（基本平面之外的字符成一对，12 个字符），所以候选目录在磁盘上的字节数不是它占协议行的长度。写入与编辑除 K 的字节上限外，还要让候选目录转义后的总长（每个文件的路径——相对 realpath 后的候选目录——与正文各自 `json.dumps` 的长度，加每个文件 4 个字符的分隔）不超过 `LINE_BUDGET` 900000 减去回答外壳 `{"op": "result", "value": {"ok": true, "value": …}}` 的固定长度（宿主的 `MAX_LINE` 是 1000000），否则是该工具的错误，所以一个被接受的目录总能整个导出；宿主侧取回候选目录失败（`ok: false`）时该候选 `invalid` 并带 worker 的错误，不让本轮抛错（2026-10-05 再复核后）；worker 发出 `work` 的回答前再量一次，超过就改答 `{"ok": false, "error": "the answer would exceed the protocol line; read it in pieces"}`，不发出。
- **`read_file` 分页**（2026-10-05 复核后）：一次至多 `READ_CHARS` 131072 个字符，参数 `offset`/`limit` 按字符计（`limit` 不超过它），后面还有内容时结尾写 “[... N characters follow]”；一页转义后仍超行预算时再折半。`inputs/` 的文件（`ledger.json` 随活动增长、没有上限）与候选文件同样分页，所以任何一次读都放得进一行。
- **路径与异常的边界**（2026-10-05 复核后）：路径至多 32 段、1024 个字符；`work` 分支捕获全部 `Exception`（例如过深路径在 `os.makedirs` 里的 RecursionError），都是该工具的错误，只有 `SystemExit`/`KeyboardInterrupt` 这类 `BaseException` 不在其内。
- **严格 UTF-8，两端都查**（2026-10-05 复核后）：宿主在执行任何调用（`finish` 在内）之前，先看它的工具名与参数能否按严格 UTF-8 编码；含孤立代理项（如 `\ud800`）的调用不执行，作为该工具的错误回给 R，会话记录里这一行只写安全化的工具名（`backslashreplace`）与错误，不写原始路径或 ref，助手消息里的这次调用也换成安全化的副本——否则会话事件写盘时以 UnicodeEncodeError 让本轮崩溃。worker 的路径检查也拒绝不是严格 UTF-8 的路径：`surrogateescape` 的 `\udc80`–`\udcff` 会建出原始字节的文件名，它们作为 `list`/`export` 的键再进台账与规范 JSON 就坏了。
- **其余三条**（2026-10-05 复核后）：一次 `finish` 里同一 `dir` 只能声明一次，重复是该调用的错误（R 可在上限内再 `finish`）；正文里写出的工具调用（`{"tool", "args"}` 或 `{"tool_calls": [...]}`）与 provider 的结构化调用一样把参数原样交给工具，不是对象的参数由工具答成错误，不在解析时崩溃；回复自带原生内容的 provider（Anthropic API，有原生工具调用，下一轮原样回放其内容）的正文不读作调用，回以提示语——否则回放的内容里没有 tool_use，紧随的 tool_result 无从对应，API 会拒绝（2026-10-05 三复核后）；`read_trajectory` 的 offset/limit 不合法时也记进读取记录（字节 0，写明拒绝原因），会话记录的 `trajectory_reads` 计入这次调用。

### R 的输入（v0.3，2026-10-04）

会话开始时 `/work/inputs/` 有、并在 system 提示里列出的输入，按血缘分列。原有输入（当前包与 manifest、包 API 说明、基础工具清单、笔记与覆盖、train 受限的经验投影）不变，新增三项：

- **原始 train 轨迹，经 `read_trajectory`。** 参数 `{attempt_ref, offset, limit}`，返回宿主写的轨迹记录的字节片段：每次模型调用的 `{call_id, role, effort, messages, tools, response, usage}` 与每次工具调用的 broker 事件（M0 §2（v0.3）：轨迹由宿主从网关与 broker 调用写出，包不能写）。只读；只接受 train/fit 尝试的 ref，split 过滤在固定代码（M7 的结果存储按冻结 split 过滤，非 train 返回 AccessDenied，调用照记）；单次与累计字节上限见上；每次调用记录 ref、offset、字节数。失败摘要仍是默认入口，轨迹是摘要不够时的补读。原因：Meta-Harness 的消融是本调研里唯一的硬证据——只给分数 34.6、分数加 LLM 摘要 34.9、全轨迹 50.0（[SURVEY §4](../SURVEY_RSI_FRAMEWORKS.md) 第 7 项）；v0.1 第 1 轮的错误结论正来自误读摘要的截断标记（[RESULTS](../RESULTS.md)）。
- **dev 失败类型直方图。** 固定代码从 `grading.jsonl` 投影：当前已采用 W 在 dev 上合并对照读数（M3 §2.1(a)（v0.3）：合并对照 = 同一冻结计划与模型身份下该 W 的全部有效读数）里，失败断言按断言 `type` 枚举的计数，另给读数次数与 dev 任务数。只有枚举与计数：没有 params、没有任务 id、没有逐题行、没有通过/失败的题数分布之外的统计；test 永不投影。它是 R 对 dev 唯一的断言级信息，补充 M0 §4 的“dev 只给汇总”：汇总从“逐题 success/partial 的合计”扩到“失败类型的合计”，仍不含任务事实（M0 §4（v0.3））。原因：2026-10-04 的差距分析发现 W* 修的失败类在 dev 占 58%、test 只占 31%，R 若只看 train 失败分布会一直修同一类；类型计数不足以定位任何一题。
- **台账投影。** §5 的引擎自有台账的 train 受限投影：每个历史候选的声明（mechanism、site、carrier、trigger_condition、differs_from）、筛选结果（净翻转数、`thin_screen`）、裁决，以及 gate 后 fixed / regressed / still_failing 三类的**题数**；train 任务 id 出现（筛选结果），dev 任务 id 不出现。（v0.3，2026-10-06）多领域时投影的 gate 项另有 `by_domain`：每个领域的 fixed / regressed / still_failing 题数（由台账里的逐题列表按任务 id 的领域前缀计，两支 E0 去题号时先换成同样的计数），以及该领域两臂的 dev 均值与差值（收据的 `metrics.per_domain`）；只有计数与均值，没有 dev 任务 id。R 由此看得到“在 marketing 上修好、在 sales 上弄坏”，不必从总增益猜。实现 `experience.ledger_projection(rows, objects=…)`、`experience.strip_dev_task_ids`；测试 `test_research_round.py::test_the_ledger_projection_shows_each_domains_gate_outcome_without_dev_task_ids`。v0.2 的 R1 在提示里要求的“先说明每次被拒排除了什么”由此成为引擎产物，R0 与 R1 读同一份（[SURVEY §4](../SURVEY_RSI_FRAMEWORKS.md) 第 2 项）。

（实现，v0.3，2026-10-05）三项输入进 `/work/inputs/`（`notes.json`、`coverage.json`、`histogram.json`、`ledger.json`、`tools.json`、`experience.json`、`trajectories.json`、`package_api.md`），同一内容也是会话的第一条用户消息（`research.session.run_session_round` 的 `state`）。`trajectories.json` 列本轮纳入诊断的失败 train 尝试的 `attempt_ref`（即 attempt id）与字节数，`read_trajectory` 的参数 `attempt_ref` 取它；返回的是转写文件的字节片段（UTF-8 解码，末尾注明剩余字节数）。直方图是 `session.dev_failure_histogram`：从 gate 的合并对照规则（`Improvement.control_pool`）取已装 W 的 dev 读数，按 attempt 读 `grading.jsonl` 的 dev 行，计失败且未排除的断言 `type`；字段只有 `split/types/readings/tasks/attempts`。台账投影是 `experience.ledger_projection`（§5）。

v0.3 考虑过成功侧摘要（`success_digest`：对通过的 train 尝试再做一次模型摘要，写“不能破坏的习惯”），不做：筛选的通过样本已直接测退步，再加一次摘要调用是过度设计。

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

（v0.3，2026-10-05，第 0/1 阶段评审）会话的记录：除每次调用一行（回合、工具、路径、ok）之外，整段会话存为一个对象（`CodingSession.transcript()`，`transcript:sha256:…`）：按顺序的每条消息——输入与之后的用户消息、R 的回复文本与它的工具调用及参数、每个工具的结果或拒绝原因——每项截到 8,000 字符、全部严格 UTF-8 可编码；`research_session` 事件记 `transcript_ref`，报告的会话行列出它；被拒调用的那一行也带错误文本（截到 500 字符）。原因：被拒的写入、读取错误与 R 的回复原来只在会话的内存里，会话一结束就没了；第 0 阶段的继续/停止规则要读它。测试 `test_research_session.py::test_the_sessions_turns_are_stored_with_refusals_and_the_event_names_them`、`test_a_transcript_item_is_cut_and_strict_utf8`。

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
| 读了状态仍判错、遗漏核对、发错渠道、选错版本（判断类失败；v0.3，2026-10-04） | `agent` 组件（carrier=agent）：判断前证据表、发送前二次核验、关键决策多采样、任务内按需提高 effort（超过 K 的 max_effort 时按上限执行并记录请求值），或改 loop 结构 | 声明 site 与 trigger_condition；成本项通过（M3 §2.1(e)（v0.3）：候选每次有效尝试的均值成本不超过合并对照的 (1 + β0 + β1·gain) 倍，预设 β0 0.10、β1 2.0）；多采样与 effort 在匹配成本下比较 |
| 上下文溢出、长返回后漏读（v0.3，2026-10-04） | `agent` 组件改上下文管理（compact 策略、证据保留规则） | 同上；v0.1 的“context_guard 类组件不支持”取舍在 v0.3 撤销，上下文算法随 agent 组件可变 |
| 同一机制在同一位置再次被提出（v0.3，2026-10-04） | 准入 `repeated_mechanism` 拒绝，除非声明 `differs_from` | 台账中该规范化 (mechanism, site) 已有 rejected/screened_out 记录（§5） |

首版默认方法是 policy_edit/harness_edit，新增训练方法不进入菜单或账户预检；旧 trainer/API 不删除。v0.1/v0.2 每轮至多一个候选；v0.3（2026-10-04）每轮至多 k 个，k 由 preset 的 `max_candidates_per_round` 固定（预设 3），冻结进 schedule 的 `candidates_per_round`（`bench._cycle_tables`）。`bench.improvement_settings` 接受 1～preset 值、缺省取 preset，`bench._beyond_preset` 拒绝高于配置的 schedule（2026-10-05 已实现；此前的代码拒绝 ≠1）。k 个候选来自同一会话、同一组笔记，须在 (mechanism, site) 上互不相同——相同者后者记 dropped；原因是 k 的意义在于并行试不同机制而非同一机制的三个措辞，单候选三轮串行是 v0.2 的搜索宽度不足（[SURVEY §7](../SURVEY_RSI_FRAMEWORKS.md)）。候选都以当前已采用包为起点，优先修已有机制；没有收益时保留原版本、分析失败或停止。包文件数/大小、hook/工具数、冻结前草稿修复次数、每轮失败笔记数、每轮候选数、会话轮数与编辑数、筛选样本数与测试时间由 preset/K 固定，不允许 R 提高上限。

组件种类优先级为 memory/skills → on_finish 守卫 → before_tool/after_tool 守卫 → 包工具；逐题笔记的建议组件种类与汇总 patch 按此顺序优先选择。包工具置后的依据是 Harness-Zero 三个学生库最终共 12 个中间件、3 个 skill、3 个工具，工具只出现在 USPTO（化学校验），SpreadsheetBench 与 AppWorld 没有工具；memory/skills 置前是因为纯文本包无需隔离 worker、可在阶段 A 先交付（[ROADMAP](../ROADMAP.md) §2），并非来自 Harness-Zero 的组件数量。改上下文的组件（context_guard 类）按设计不支持，是有意取舍（TargetHarness 固定上下文算法）；其余挂点覆盖见 [M1](01_POLICY_RUNTIME.md) §1。

以上优先级与取舍是 v0.1/v0.2 的规则，保留为历史。v0.3（2026-10-04）起：

- 组件种类优先级不再作为规则引导候选选择；笔记仍写“建议组件种类”，但会话按声明的 carrier 自选。原因：v0.1/v0.2 的 13 个候选全是文字提醒，而笔记里 26 条建议 before_tool、4 条建议技能，优先级把 R 推向文本面；剩余失败是判断与漏读状态，文本提醒够不到（ROADMAP §3 第 2a 步第 9 项）。
- v0.3 考虑过“每段至少一个候选改代码”（`require_code_candidate`，由草稿检查 shape 阶段强制），不做：取消优先级后由 R 自选载体，强制项另加检查链分支。候选的载体分布照常进台账与报告（M7 §4）。
- `agent` 组件是可变层的一部分（M0 §2（v0.3）：manifest `harness_package.v2` 的 `agent` 入口，`run(ctx, task)`；ctx 只暴露 `model`、`tool`、`skills`、`limits`、`view`）；默认 agent `agent/default_agent.py` 把现行宿主循环搬进包，v1 包即带默认 agent 的 v2 包，hook 挂点作为默认 agent 遵守的约定保留。K 不变的部分：模型身份与推理配置上限、网关、broker、权限、评分、split、gate、预算根、单次尝试硬上限、宿主写的事件/轨迹记录、R 的代码与研究位。

### train 侧筛选（v0.3，2026-10-04）

冻结后的候选先在 train 上筛，只有最佳者付 dev gate：

- 筛选任务集：(a) 该候选声明 `notes` 所指笔记来源的 train 失败任务，与 `predicted_affected` 的并集；(b) `screen.passing_sample`（预设 5）个 train 通过任务，从当前已采用 W 最近一次 train 测量的通过题中按冻结 split 顺序与 `order_seed` 取，一轮内所有候选用同一组。每题 1 次。筛选计划的任务 id 只来自 train，split 过滤在固定代码，筛选永不读 dev。
- 分数：净翻转 = 转通过数 − 转失败数，与候选每次有效尝试的均值成本一并记入台账。付 gate 的条件：净翻转 ≥ `screen.min_net_flips`（预设 1），取最高者，同分取均值成本低者；都不满足则本轮 `outcome=screened_out`，没有 gate、没有安装，计入轮次与 patience，每个候选的筛选结果进台账。筛选只决定谁付 gate，不能接受任何候选：接受仍只由 dev gate 的冻结门槛决定（M3 §2.1（v0.3）：合并对照、min_gain 0.05、幅度退步规则、成本项，再经确认复测）。
- 费用：k × (|笔记任务| + 5) 次尝试，按 v0.2 实测约 k × 10 × $0.4 ≈ $12，比一次 50 次尝试的 gate（约 $20）便宜，且 v0.2 为 −8 点的候选付了完整 gate。
- 复用：筛选尝试是候选在 train 上的有效读数。候选被安装后，§4“重跑上一诊断工作集中失败的 train 任务”一步按执行身份复用这些读数，不再跑；未被安装的候选的筛选读数留在台账，不进入任何版本的 train 分数。
- 失败集（笔记任务 ∪ predicted_affected，∩ 最新 train 测量仍失败）少于 3 题；通过样本不计的候选照常筛，但台账标 `thin_screen`（定义 2026-10-05 统一；原写“筛选任务数少于 3”）；它的净翻转分母小，台账投影与报告引用时带此标记。

（实现，v0.3，2026-10-05；评审后同日修正见括号）筛选在 `research/session.py` 的轮次驱动里，由 `Improvement.train_screen` 跑候选臂（arm `train_screen`，split train，一次尝试，自己的 permit 与 TaskEvalPlan，`installs=False`，父 release 取当前 active——与会话输入、Experiment 同一来源，不取上一次 gate 留下的 parent；基础设施失败的尝试按计划的 `retry_policy` 经 `refill` 补跑一次，与 gate 的臂相同）；它是轮次级的子操作，记 `train_screen` 事件（`rsi/events.jsonl`，含候选 id、任务、净翻转、均值成本、`thin_screen`、尝试 id），不是 Experiment 的状态；只有被选中的候选冻结进 Experiment，其 id 在筛选时就定下（`screen_build(proposal, experiment_id)`），所以筛选跑的 W 与 gate 跑的 W 是同一个内容地址的候选（policy set 的 provenance 含 experiment id）。取舍与定义：(1) 候选的失败任务集 = （其声明 `notes` 所指笔记的任务 ∪ `predicted_affected`）∩ 本轮最新 train 测量里**失败**的任务（2026-10-05 评审后定：声明里写了却已通过的 `predicted_affected` 留在声明里，不进失败集，只可能经固定通过样本进入筛选）；`thin_screen` 看这个集合的大小 < 3，通过样本不计入；(2) 通过样本从本轮最新测量的通过题中按冻结顺序取，`random.Random(order_seed)` 打乱后取前 `passing_sample` 个，再按冻结顺序排列，一轮内所有候选同一组；(3) 平局：净翻转最高者；同分取均值成本低者（成本未知者排在已知者后）；再同则按声明顺序（`session.pick_best`）；(4) `before` 取本轮测量里该任务的最新有效结果，`after` 取筛选臂的有效结果，只在两边都有读数的任务上算翻转。本轮最新 train 测量里没有任何失败题时，会话不开（没有候选能有失败集可筛），本轮以 `needs_collection`、`next_action=collect` 结束，走既有的采集路径（受 `collection_batches` 限制，不计 idle；2026-10-05 再复核后定）；会话格式说明里写明“候选在当前失败的 train 题上筛选”。净翻转只在**失败集**有有效 after 读数时才算（2026-10-05 复核后定）：失败集为空的候选不付筛选；失败集没有一题有有效 after 读数的候选净翻转为 None——单凭通过样本算出的数只能 ≤ 0，记成 `screened_out` 会让它成为重复机制的触发项，而它其实没被测到。没有读数时按原因分：失败集为空、构不出、任一尝试是 `candidate_defect`（候选自己的代码出错，gate 也判 invalid）、或失败集的尝试以其他原因无效，结局 `invalid` 并写明原因（如 “no valid screen reading on the failing set …”）；失败集的尝试（补跑之后）只因 `infrastructure`/`evaluator` 而无效，结局 `lost`，筛选行记 `lost_to`（这些丢失尝试的 failure class；只有 `lost` 的筛选带它，补跑成功被取代的尝试不算，2026-10-05 再复核后），不是 R 的错、不触发重复机制；任一尝试因 `budget_exhausted` 无效，本轮以 `budget_exhausted` 结束。一轮里**没有任何候选**有有效筛选读数、且其中有 `lost` 的，本轮以 `inconclusive` 结束，原因以 “infrastructure:” 开头——与 gate 的完整性损失同样处理：重跑、计入基础设施失败上限，不算完成的轮次、不算 idle、不占一支的 `worker_round_limit`——在运行中也是如此（`loop.run_worker_rounds`：`infrastructure_round()` 认得的一轮在同一个轮位上重跑，不占 `schedule.worker_rounds`，不加 idle，连续基础设施失败数加一，既有上限 `max_infrastructure_failures` 照旧停下运行；2026-10-05 再复核后落实到代码；续跑时 `loop.counters_of` 也这样数：丢给基础设施的轮次计入连续失败数、不计 idle，暂停记号（停止点以外的 stopped）把失败数归零，三复核后）；只有 `invalid` 的才是 `invalid` 轮。至少一个候选有读数时，本轮按已有的读数挑选或 `screened_out`。筛选行的结局共六种：`selected`（付 gate 的那个）、`not_selected`（清过 `min_net_flips` 但没被选中）、`screened_out`（低于 `min_net_flips`）、`invalid`（候选自己读不出）、`lost`（尝试丢给基础设施或评分器）、`interrupted`（挑选之前的占位：轮次在付过的筛选之后中断时以此补写，折叠为 pending，轮次重跑）；`REPEAT_OUTCOMES` 仍只有 rejected 与 screened_out（2026-10-05 评审后定）；(5) 第一次筛选之前查一次预算：全部已准入候选的筛选尝试加 gate 的验证预留（`verify_bound`）须一起容得下，否则本轮在付任何筛选之前以 `budget_exhausted` 结束；(6) 复用：`Improvement.screen_readings` 以运行身份（RunManifest 去掉标注臂的字段 `RUN_LABELS` 后相等）找回筛选读数，`train_measurement` 对这些任务不再跑，`last_measurement` 也把它们计入；未安装候选的筛选读数与任何已装 release 的身份都不相等，自然不被读。

冻结前试跑-修复：patch 规范化成草稿包后，M5 在同一 operation 日志里做 package_check → isolated_probe → package_test（草稿检查）；只含 skills/memory 的草稿只做结构/路径/manifest 检查，不需要 isolated_probe。失败时只把检查输出（不含任何任务结果或评分）回传 R，连同父包与同一份基础工具清单，R 可修订；修订次数上限由 preset/K 固定（默认 3，指初稿之后最多修订 3 次、最多 4 份草稿），R 不能提高。每次草稿都有新包 ref，逐次记录并计费；只有检查通过的草稿才冻结进 Experiment；用尽次数则本轮以无效提案结束并计入轮次。冻结后源码不变；building 按相同输入摘要复用已确认的草稿检查，不重复运行。草稿修复不形成新 proposal、不另占 schedule 候选轮次，也不把无限修复藏在一个 build 中。

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

v0.3（2026-10-04）替换上面从 `one summary call (research.propose)` 到 `execute through M5` 的部分；其余步骤不变：

```text
dev histogram: fixed projection of failed-assertion types over the pooled control readings of the installed W (counts only)
ledger projection: train-restricted digest of the engine-owned ledger
one coding session per round in the worker over /work (package copy, candidate dirs, inputs), bounded by
    research_session.max_turns / max_edits / max_trajectory_bytes(_total); tools: files, read_trajectory, finish
    finish -> candidate_declaration.v1: up to k candidates {dir, mechanism, site, carrier, predicted_affected,
        trigger_condition, differs_from?, notes} or a next_action
    unreadable (no finish, bad declaration, no candidates and no next_action): outcome=invalid, the round counts
for each declared candidate: diff against the current package; declaration must match the diff (else invalid)
    duplicates on normalized (mechanism, site) within the round: later ones dropped
if collect / none / stop / repair_runtime: as before
draft check per candidate: kind and literal checks -> isolated_probe -> package_test, revisions within max_draft_repairs
    (revisions continue the same session; turn and edit caps still bind); a candidate that fails its last draft: invalid
freeze each passing draft; admit each (core checks, duplicate_experiment, repeated_mechanism); write the ledger entry
train-side screen of the admitted candidates: notes' tasks + predicted_affected + the fixed passing sample, 1 repeat
    best net flips >= screen.min_net_flips pays the dev gate; none: outcome=screened_out (the round counts)
dev gate for the one screened candidate: pooled control, min_gain 0.05, magnitude regression rule, cost term,
    confirmation re-run before install (M3 §2.1, v0.3)
    then the receipt ref and per-task fixed / regressed / still_failing into the ledger (§5)
execute through M5 (unchanged); if deployment is accepted: M3 may install against parent
```

每轮至多一个候选进 dev gate、至多一次安装，这一点与 v0.1/v0.2 相同：k 加宽的是提案与筛选，不是 gate 的次数，所以 §1 的 patience 与轮次计数的含义不变。

每轮 measure 区分诊断与正式评测：新包安装后，默认只重跑上一诊断工作集中失败的 train 任务，以定位剩余问题；未重跑的成功任务保留原版本历史，不视为新包已通过，更不能把不同版本结果拼成新包完整 train 分数。正式 dev gate 仍对当前已采用版本与候选执行完整固定 dev 配对比较，逐题结果及退化比例按 M3 判断；它只说明该 dev 范围的表现，不证明未重跑的具体 train 任务仍成功。完整且匹配的候选 gate dev 结果可用于下一轮 measure，但只进入 R 的上下文，并标明是促成安装的那次抽样（受选择影响偏乐观）；gate 每次都在自身 plan 内重跑对照臂（`ouroloop/bench.py:910`），从不复用它；也不能以 train 诊断子集代替。（v0.3，2026-10-04）前一句是 v0.1/v0.2 的规则：v0.3 的对照臂改为合并对照（M3 §2.1(a)（v0.3）），已安装 W 在同一冻结计划与模型身份下的全部有效读数合并为对照，没有任何读数时才取一次新读数；`bench.py:910` 的每轮重跑成为 `control_pooling="fresh"` 的旧行为。“促成安装的那次抽样偏乐观”照旧，确认复测（M3 §2.1(b)（v0.3））在安装前已纠一次。

复用按实际任务执行身份检查，至少包含 W/package、实际模型与推理配置、宿主/隔离配置与工具权限、环境及评分版本、任务/attempt、冻结计划与运行限额；保留原 release/attempt/TaskResult refs，不重写身份或计作新的独立样本。托管模型标 mutable=true 时，复用限本活动内并在报告注明。上一轮未安装（rejected/invalid/infrastructure_failed/needs_collection/no_proposal 等）只是可能复用的条件：上述条件一致且结果已确认有效，才无需新增任务运行；缺项或条件变化时，按冻结计划/预算补必需尝试，无法补齐则报告证据不足或能力缺口。仅 R 改变且任务执行配置未变时可复用业务测量；R 调用产物（逐题笔记）只在 §1 的完整笔记身份一致时复用。补采产生的新合格 train 反馈仍按实际身份进入诊断上下文，不能因复用 measure 被丢弃。

准入按 method 分派：首版 harness_edit 检查包路径/结构/API/隔离能力（仅对含代码的包，M0 §2）/完整评测预算，不要求决策标签或教学 plan；准入时隔离 worker 不可用按 M0 §3 的基础设施故障处理（重试后 infrastructure_failed 并暂停活动），不记 invalid、不计失败上限；第二版 decision_train 检查事实反馈，distill_actor 检查教学计划/能力/预算，再由 M2 检查成功轨迹。固定核心检查不能由 R 的辅助预检删除。

每轮至多一个可执行候选或一批补采（v0.3，2026-10-04：至多 k 个候选进草稿检查与筛选，仍至多一个进 gate），候选至多一次 training 条目；沿用现有 schedule 并添加补采计数，限制轮数/失败上限/耐心。冻结前草稿修复与逐题笔记属于本轮，次数按冻结上限，不另占候选轮次；修订用尽的轮次计入候选轮次与失败上限。源方法实验和蒸馏实验分别占用轮次/子操作额度；不能把无限源搜索藏进一次 build。R 没有直接开作业、设置门槛或安装的权限。

诊断区分：teacher 也失败时查工作流/目标能力；teacher 成功而无教师 actor 失败时查可学数据、表示和训练；controller 未调用时查接线；有调用但更差时查真值、误拒和费用。依据证据选择下一动作，不自动加模型、专家或工具。

## 5. 经验与递归接口

Experience 记录 proposed/built/evaluated/validated/installed 的事实，引用 source、plan、dataset、模型身份、任务结果、成本、实际 R 和失败阶段。hypothesis 与事实分开；prompt 候选不能记 trained=true。后续 context 通过现有检索规则选择获准经验，不复制所有轨迹。

### 引擎自有的实验台账（v0.3，2026-10-04）

台账是 E 中 `kind="ledger"` 的记录，只由固定代码写：包不能写（它在 worker 里没有 E 的句柄），R 不能写（会话没有写 `/work/inputs/` 的工具），R 只读 §1 的投影。原因：自写的经验教训不可信——HSI 的 meta-evolver 因提案者“把 harness 的错归给模型、声称从未触发的机制有效”而存在，v0.1 第 1 轮的截断 hook 就是这样一条假教训（[SURVEY §5](../SURVEY_RSI_FRAMEWORKS.md) 第 9 项）；台账里的每条结论都带读数 ref，可对宿主写的事件记录核对。

每个候选一条，随阶段追加：

- 提案时：声明全文（mechanism 原文与规范化形式、site、carrier、predicted_affected、trigger_condition、differs_from、notes refs）、候选 package ref、diff 摘要（改动路径与字节数）、所属轮次/段/R 身份。
- 草稿检查后：每次草稿的 ref 与结果。
- 准入后：通过/拒绝与原因（含 `duplicate_experiment` 与 `repeated_mechanism`）。
- 筛选后：筛选任务 id（train）、每题前后结果、净翻转、均值成本、`thin_screen`。
- gate 后：裁决、M3 收据的 ref（`kind` 为 `full` 或 `confirmed`；确认复测未过的候选也是 `confirmed`、verdict rejected，`first_reading` 保留，M3 §2.1(b)（v0.3））及其 `gain`、`control_readings`、`cost_per_attempt`，以及**逐题三类**，从收据的逐题差值直接读出、不另算：fixed（候选该题的均值 success 高于合并对照）、regressed（低于合并对照，即计入 `regression_rate` 分子的题，M3 §2.1(d)（v0.3）的幅度规则）、still_failing（两臂均值都为 0）。三类带收据 ref，可凭 `control_attempt_refs` 重算；合并对照增加读数后不回头重算旧条目。

（实现，v0.3，2026-10-05；`evolve/experience.py`。）E 是只追加的 jsonl，所以“每个候选一条、随阶段追加”落成每个阶段一行 `kind="ledger"`（`stage` 为 declared/drafted/admitted/screened/gated，`candidate` 为候选 id，定义见下），`ledger_rows` 折叠成每个候选一条并给出 `outcome`（dropped / declaration_mismatch / invalid / blocked——准入答 collect 或 repair_runtime / screened_out / not_selected / lost——筛选尝试丢给基础设施或评分器 / gate 裁决 / installed / pending；2026-10-05 评审后定，`lost` 为复核后加）；`screened` 行带候选的 `experiment_id` 与 `proposal_ref`（id 在筛选前已定），`proposal_ref` 从 `admitted` 行读；一轮被中断（额度耗尽、worker 崩溃）时已付的筛选行在收尾处补写（结局 `interrupted`，折叠为 pending，轮次重跑）；续跑里完成的 gate 由 `research/loop.resume_unfinished` 经同一 helper（`session.gate_ledger_fields`）补 `gated` 行，所以中断不丢台账；续跑开始时还会补一种缺口：筛选结局 `selected`、带 experiment id、没有 gated 行、而其 Execution 已在终态的条目（实验跑完、轮次在写 gated 行之前中断），从终态记录的收据经 `gate_ledger_fields` 补写 gated 行（2026-10-05 再复核后）；它按 experiment id 找候选（筛选行记着），只有不带 experiment id 的旧条目才按 `proposal_ref` 找——提案 ref 是内容地址，同一 R 输出、缓存的笔记会让不同轮的候选有同一个 ref，按首个匹配会把 gated 行落到更早一个从未付 gate 的候选上（2026-10-05 复核后）。候选 id 是 `<scope>/d<诊断序号>/<dir>`，诊断序号即 `Diagnosis.round_index`（本 scope 的第几次诊断），跨段、跨重跑唯一；R 读的是 `ledger_projection`（声明、`declaration_ref`、草稿次数、准入状态、筛选的 train 题号与净翻转、裁决、gate 后三类的**题数**）。gated 行的三类由 `task_gate.receipt_differences` 从收据的结果对象读出（候选 = subject 加 confirm 臂，对照 = 合并对照；regressed 用计划的 `regression_drop`），收据的 `kind` 在行里叫 `reading_kind`（行自己的 `kind` 是 `ledger`）。声明存成 `declaration:sha256:…` 对象（`ExperimentProposal.declaration_ref`，M0 §3）。两支的 E0 经 `experience.strip_dev_task_ids` 把 gated 行的三类列表换成题数（M6 §2）。报告的台账一节见 M7 §4。

v0.3 考虑过逐题 TRUE FIX / TRUE REGRESSION / STOCHASTIC 标签（阈值 `ledger.min_control_readings`/`stable_fail`/`stable_pass`、重算与 superseded）与预测命中/未中记分，不做：单次读数分不出噪声，标签给不出收据之外的事实。

**准入 `repeated_mechanism`。** 在 `ouroloop/research/experiment.py` 的 AUXILIARY 检查（`check_duplicate_experiment`，`:116-160`，比对的是改动字节的签名）之外，新增一条比对规范化 (mechanism, site)：本活动台账中存在同一 (mechanism, site) 且结局为 rejected 或 screened_out 的记录时，候选须带 `differs_from`，指向该记录并用一句话写出不同之处（非空、不与被指记录的 mechanism 原文相同）；否则准入拒绝，候选记 invalid（stage=admission，reason=repeated_mechanism），本轮其余候选继续。固定代码不判断“不同之处”是否成立，只要求它被写出并记进台账；判断留给筛选与 gate。原因：v0.2 的 R1 在研究位里仍重提 R0 已被否定的机制，只在提示里写“不重提”没有约束力（[SURVEY §6](../SURVEY_RSI_FRAMEWORKS.md)“Anti-repeat”一栏：只有 RRSI 与 AutoSaddler 把它做成结构）。被 infrastructure 结束、或 inconclusive 的记录不触发此检查，与 `frozen_signatures` 的取舍一致。

（实现，v0.3，2026-10-05）`research/experiment.py` 的 `declaration_reasons`，由 `admit(..., declaration=, ledger=, train_tasks=)` 在核心检查之后固定运行——不在 R 可配置的 preflight 清单里。`differs_from` 的形状是 `[{"ref": <台账行的 declaration_ref>, "how": "<一句话>"}]`；规范化后的 `how` 等于被指机制原文时视同未写。同一检查也拒绝 `predicted_affected` 含非 train 任务 id（M0 §3 CT7-08）。本轮内相同 (mechanism, site) 的后者在进入草稿检查前就记 dropped，不走这条。

**组件归因与淘汰——第二版。** 已安装组件按其 gate 的 fixed − regressed 记归因增益、若干轮后对无增益的组件由固定代码生成删除候选（占 k 的一个名额，照常过检查、筛选与 gate），是第二版的计划，v0.3 不记归因增益、不生成删除候选（累积无益组件的代价见 AutoSaddler Iter 46、Decagon，[SURVEY §5](../SURVEY_RSI_FRAMEWORKS.md) 第 7 项；v0.3 靠成本项约束）。

preset/K 新增参数汇总（v0.3，2026-10-04；均冻结进 K，R 不能提高；2026-10-05 已进 `automationbench_harness_v3`，`bench.PRESETS`；`[harness_evolution.research_session]` 的四项与 `screen.passing_sample` 只能调低（各自 ≥ 1，`passing_sample` ≥ 0），`screen.min_net_flips` 是下限、只能调高（2026-10-05 复核后写明），`[improvement] max_candidates_per_round` 取 1～3、缺省 3，冻结进 schedule 的 `candidates_per_round`，`_beyond_preset` 拒绝高于配置的 schedule）：`max_candidates_per_round` 3；`research_session.max_turns` 40、`research_session.max_edits` 24、`research_session.max_trajectory_bytes` 65536、`research_session.max_trajectory_bytes_total` 524288；`screen.passing_sample` 5、`screen.min_net_flips` 1。新 schema：`candidate_declaration.v1`；新轮次结局：`screened_out`；新 invalid 原因：`declaration_mismatch`、`repeated_mechanism`。

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
| RL6-28（v0.3） | 会话声明 3 个候选，其中两个 (mechanism, site) 相同，一个声明 carrier=hook 而 diff 只改 memory | 相同者后者记 dropped；声明不符者 invalid（declaration_mismatch）；剩下的候选照常进草稿检查；本轮不 invalid |
| RL6-29（v0.3） | 会话耗尽 `max_turns` 或 `max_edits` 而未 finish；或 finish 的声明读不出 | 本轮 invalid，计入轮次与 patience，不回退 collect/stop；会话的全部调用照常计费 |
| RL6-30（v0.3） | 本轮三个候选都是文本（memory/skill/prompt） | 照常进草稿检查、筛选与 gate；候选载体进台账与报告，不因没有代码候选判结构错误 |
| RL6-31（v0.3） | 三个候选通过准入；筛选净翻转分别为 +2、+2、0，前两者均值成本 $0.50 与 $0.42 | 只有第二个候选付 dev gate；三者的筛选结果都进台账；筛选计划的任务 id 全在 train |
| RL6-32（v0.3） | 所有候选筛选净翻转 < `screen.min_net_flips` | 本轮 screened_out，无 gate、无安装，计入 patience；台账记各候选 |
| RL6-33（v0.3） | 被筛选的候选经 gate 安装，进入下一轮 | 下一轮的 train 诊断对筛选已跑过的失败题按身份复用，不再付费；未被安装的候选的筛选读数不进任何版本的 train 分数 |
| RL6-34（v0.3） | 候选的 (mechanism, site) 与台账中一条 rejected 记录相同 | 无 `differs_from`：准入拒绝，记 invalid（stage=admission, repeated_mechanism），其余候选继续；带非空且不同于原文的 `differs_from`：准入通过，差异写进台账 |
| RL6-35（v0.3） | gate 后某题候选通过、合并对照 3 次读数通过 0 次；另一题候选失败、对照通过；第三题两臂都失败 | 台账从收据的逐题差值读出 fixed / regressed / still_failing 各一，带收据 ref 与 `control_readings`；不给标签、不重算 |
| RL6-36（v0.3） | 会话调用 `read_trajectory` 读 dev 尝试的 ref；或累计字节超过 `max_trajectory_bytes_total` | 前者 AccessDenied，调用记录在会话记录里，会话继续；后者该调用拒绝并说明累计上限，会话继续 |
| RL6-37（v0.3） | R 读到的 dev 直方图与台账投影 | 直方图只有断言 type 枚举与计数、读数次数、题数；台账投影里 dev 结果只以 fixed/regressed/still_failing 的题数出现，train 任务 id 可出现；test 不出现 |
| RL6-38（v0.3） | 确认复测的读数没清过门槛 | 收据 `kind="confirmed"`、verdict rejected、`first_reading` 保留；台账记该候选 rejected 并引用收据；不安装 |
| RL6-40（v0.3） | 包代码或会话尝试写台账、写 `/work/inputs/`、写 `/work/package/` | 写被拒绝并记录；台账只有固定代码的条目 |

扩展 `test_research_round.py`、`test_roles.py`；复用当前 admission 与 executor，接口变化必须有旧模式兼容测试。v0.3（2026-10-04）的验收：
- `pytest tests/test_research_round.py tests/test_research_session.py tests/test_diagnosis.py tests/test_bench_phase.py`
- 会话：`test_research_session.py::test_a_session_stops_at_its_turn_and_edit_caps`（新增，RL6-29）、`test_the_declared_carrier_and_site_must_match_the_diff`（新增，RL6-28）、`test_a_session_sees_no_scorer_dev_or_final`（新增）、`test_a_revision_continues_the_same_session`（新增；一轮一次会话）、`test_a_session_cannot_write_outside_its_candidate_dirs`（新增，RL6-40）。
- k 候选：`test_research_round.py::test_a_round_declares_up_to_k_candidates_and_drops_a_repeated_mechanism_site`（新增，RL6-28）、`test_a_text_only_candidate_set_is_checked_like_any_other`（新增，RL6-30）、`test_bench_phase.py::test_max_candidates_per_round_up_to_the_preset_is_accepted`（新增，`bench.improvement_settings` 的改动）。
- 筛选：`test_research_round.py::test_the_screen_reruns_the_notes_tasks_and_a_fixed_passing_sample_from_train_only`（新增，RL6-31）、`test_only_the_best_screened_candidate_pays_the_gate`（新增，RL6-31）、`test_a_round_with_nothing_worth_the_gate_is_screened_out`（新增，RL6-32）、`test_screen_readings_of_the_installed_candidate_are_reused_as_its_train_diagnosis`（新增，RL6-33）。
- 台账与准入（台账是 `research/experiment.py` 旁的固定代码，不另立模块）：`test_research_round.py::test_the_ledger_reads_fixed_regressed_and_still_failing_from_the_receipt`（新增，RL6-35）、`test_a_failed_confirmation_is_a_confirmed_receipt_rejected_in_the_ledger`（新增，RL6-38）、`test_the_package_and_the_session_cannot_write_the_ledger`（新增，RL6-40）、`test_admission_refuses_a_rejected_mechanism_at_the_same_site_without_differs_from`（新增，RL6-34）。
- 输入：`test_diagnosis.py::test_read_trajectory_is_train_only_read_only_and_byte_capped`（新增，RL6-36）、`test_the_dev_histogram_has_assertion_types_and_counts_only`（新增，RL6-37）、`test_the_ledger_projection_shows_dev_results_as_three_counts_only`（新增，RL6-37）。
- 兼容：`test_research_round.py::test_a_v1_package_round_still_runs_with_the_default_agent`（新增；M0 §2（v0.3）：v1 包是带默认 agent 的 v2 包）。

（v0.3，2026-10-05）上列测试全部已实现，名字以此为准；另有 `test_research_round.py::test_a_declaration_naming_a_dev_task_is_refused_at_admission`（CT7-08）、`test_it6_31_…`、`test_it6_39_…`（两条，08 §1a）、`test_research_session.py::test_a_memory_candidate_from_the_session_is_drafted_screened_gated_and_installed`（整轮）、`test_harness_loop.py::test_it6_32_…`、`test_package_worker.py::test_the_work_tree_seeds_a_candidate_from_the_package_and_writes_under_candidates_only` 与 `test_a_work_action_is_never_a_package_error_and_runs_no_code`（worker 的 `work` 操作）、`test_report.py::test_the_ledger_is_listed_per_candidate_without_dev_task_ids`、`test_cycle.py::test_e0_strips_dev_task_ids_from_ledger_rows`、`test_paid_run_safety.py::test_every_turn_of_the_research_session_is_metered_on_the_research_scope`、`test_rsi_integration.py::test_the_segment_after_an_install_runs_its_session_under_r1s_propose_text`。测试里的研究员是 `tests/rsi.py` 的 `SessionScript`：按计划用工具调用写文件再 `finish`，只读会话交给它的内容。会话路径只在 K 带 `research_session`（package_api v2 的核心）时启用：v1 preset 与第 3 步前冻结的工作区仍走汇总调用（`bench.Improvement.round_inputs` 的第三个返回值为 None）。
