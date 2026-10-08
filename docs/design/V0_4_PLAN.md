# v0.4 迭代方案（2026-10-06）

> **现状（2026-10-08）：** 已实施，是 v0.4 已测结果的规则记录，正文不改。finance 活动三轮与密封 final 已完成（2026-10-07）：W\* 62.7% 对 W0 54.7%，+8.0 [−4.0, +22.7]，不确定；每次尝试成本 −22%，显著；tag `v0.4.0`（`7f65947`），见 [分析](../history/FINANCE_V04_ANALYSIS_2026-10-07.md) §0。support 成了 v0.5 两臂对照里的 v0.4 臂，只跑了基线（dev 26/75），对照 2026-10-08 由用户停止。下文引用的 RSI_PLAN、V0_4_SHARDED_RSI 已于 2026-10-08 删除，在 tag `archive-2026-10-08` 上可读。
>
> 2026-10-06，用户批准。本文取代同目录的 `V0_4_SHARDED_RSI.md`（分片搜索 + 路由）与 `RSI_PLAN_2026-10-06.md`（冻结孪生点火检验）两份设计稿：两者保留作历史，不实现（2026-10-08 删除，tag `archive-2026-10-08` 上可读）。本文是 v0.4 的规则来源，模块文档里标“（v0.4，2026-10-06）”的段落指回这里。
> 先文档后执行：本文、ROADMAP “v0.4” 一节与 JOURNAL 条目先写，代码与测试随后；全部离线，没有调用模型，没有付费阶段，没有读任何 test。
> 依据：[v0.3 五领域三轮的诊断](../history/DIAGNOSIS_FIVE_DOMAIN_2026-10-06.md)（RC1～RC4、P1～P5），RSI_PLAN §1（tag `archive-2026-10-08`；Meta-Harness 与 StarHarness 的数字也见 [SURVEY_RSI_FRAMEWORKS](../SURVEY_RSI_FRAMEWORKS.md) §3、§4）的业界对照（Meta-Harness 只给分数 34.6、给摘要 34.9、给原始轨迹 50.0；StarHarness 在 AutomationBench Finance 留出 +29.3，来自工具与接口修复），[RESULTS](../RESULTS.md) 的 v0.1（单领域 +13.0 [+2.2, +26.1]）。

## 0. 结论

v0.4 不加路由、不加跨领域借鉴、不做冻结孪生。它回到 v0.1 那个奏效的形状——**一个领域一个活动**（自己的工作区、自己的 W\*）——并把诊断找到的五个框架缺陷逐一修掉：R 先看接口错误、读原始轨迹（失败的与通过的）、分清失败方向；字面量检查不再误杀普通词；筛选看得见伤害，对照不再只有一次读数；R 的系统提示换成与 k 个候选一致的方法说明；final 并列报告等成本参照。验证从 finance 开始（约 $250），再到 support。

为什么更简单：诊断的 RC1 是“一个共用包跨异质领域”，单领域活动直接消除它，不需要路由、按领域投影、隔离审计和两阶段门槛；其余缺陷（RC2～RC4）与分片无关，在单领域里同样要修。冻结孪生检验回答的是 C4，而 v0.3 连 C1 都没有新数字；先在单领域上拿到可辩护的 C1，再谈改进器。

## 1. 八项改动

每项写：改什么、证据、预期效果。实现位置与测试见 §4 的对照表。

### 1.1 按领域搜索：每个新领域一个单领域活动

- **改什么。** 每个新领域跑自己的活动：自己的工作区、自己的 W\*，v0.1 的形状。本版不写路由代码。配置 `examples/automationbench/claude-v4-finance.toml` 与 `claude-v4-support.toml`：provider、pricing、K 的表照抄 `claude-v3.toml`；`domains = ["finance"]` / `["support"]`；划分与 `workspace-claude-v3` 冻结的 finance/support 划分**完全相同**（分层规则、seed 1），由 `[bench.frozen_splits]` 钉住 hash（finance `4fd91eaf9fb8daf5`、support `3da11232f5d5ff38`），所以 test 仍是那 25 道从未读过的题；v3 preset；一段 3 轮 worker、patience 2、`research_slots = 0`；k = 3；K 的 `max_effort` medium；final 模板为该领域 25 道 test × 3 次，W0 对 W\*。
- **钉住的规则（新）。** v0.3 的 `frozen_splits` 只按 hash 规则重算被钉住的领域；分层规则的划分钉不住。现在被钉住的领域按“能重现所钉 hash 的那条规则”重算：先用配置的 `split_rule`，不符再用另一条；两条都不符就拒绝（与 v0.3 同一条错误）。规则与 seed 都进 manifest 的 hash，所以至多一条规则能重现一个 hash，没有歧义。Marketing 的 v0.2 钉子（hash 规则）照旧成立。
- **证据。** 诊断 RC1：三轮的净增益全在已 spent 的 Marketing（+6、+4、+3 题），四个主张领域净 −2、−1、−2 题；同一机制（“多读、多做”）在该克制的领域有害。单领域的 v0.1 是唯一显著的留出结果。
- **预期效果。** 候选只针对一个领域的失败，gate 读这个领域的 25 道 dev，没有“在 A 涨、在 B 跌”的交换。代价是每领域 dev 只有 25 题：单次读数噪声约 ±7～9 个点，靠 1.6 的三次对照读数与确认复测压住。

### 1.2 先修工具与接口

- **改什么。** (a) R 的新输入 `inputs/api_errors.json`：固定代码只从本活动（即本领域）的 **train** 轨迹投影，按端点/工具计调用数、涉及题数、错误响应（状态码与归一化的错误信息：数字、引号内的值、邮箱、路径里的 id 都替换掉）、紧跟在错误后的重试、还有下一页却没再读的响应、空结果；没有任务 id，没有参数值。端点是 `METHOD host/path`，路径段含数字（版本号 `v1` 之类除外）或只在一道题里出现时写成 `{id}`；`urlsplit` 解析不了的 URL（如 `https://[your-domain]...` 这种带方括号的占位主机）记在工具名下。(b) R 的方法说明（1.7）让它先看这里：失败集中在某个端点时，先做包工具或该工具接口上的 hook，再考虑行为文字。(c) 测试保证一个 `tools` 候选（包工具 + manifest 条目）在会话里走通整条草稿链并被筛选。
- **证据。** StarHarness 在 AutomationBench Finance 留出 +29.3，来自工具与接口修复（RSI_PLAN §1.1）；v0.3 的 9 个候选没有一个改工具（诊断 RC4）；把本表的固定代码用在 v0.3 工作区 W0 基线的 train 读数上（只读）：finance 50 次尝试 1,203 次调用、72 次错误响应（401“该服务没有连接账号”54、404“没有该处理器”16、400 两次）、25 次紧跟错误的重试、76 次空结果，错误集中在 QuickBooks 的端点（agent 在找一个这个世界里不存在的服务）；support 1,099 次调用、17 次错误、4 次重试、91 次空结果；两个领域都没有检出“有下一页却没读”。
- **预期效果。** 至少一部分候选落在接口上：一个包工具对用到该端点的每道题都起作用，不依赖 agent 记住一条指令；比“多读一点”的文字更不容易在别的题上过度作用。

### 1.3 R 读原始轨迹，失败的与通过的

- **改什么。** `TrajectoryReader` 的清单（`inputs/trajectories.json`）在本轮纳入诊断的失败 train 尝试之外，列出至多 6 个通过的 train 尝试（含“不得做”断言——类型名含 `_not_`——的题优先），每项标 `outcome`。准入新增一条固定检查（K 的 `research_session.min_failing_reads` / `min_passing_reads`，v3 preset 都是 1）：每个候选须在本会话里至少读过一次其 `predicted_affected` 中某题的失败轨迹，且本会话至少读过一次通过的轨迹；拒绝理由写明缺哪一样。清单里没有通过的尝试时第二项不要求。读数以宿主的读取记录为准（字节 > 0、未被拒），不是 R 的自述。（评审 2026-10-06：）同一检查先在会话里第一次 `finish` 时做——缺读数是这次 `finish` 的错误，R 可在上限内补读再 `finish`；修订的 `finish` 不再查（修订只改文件，读数只增不减）；准入照旧查同一记录，作兜底。安装之后测量只重跑上一版的失败题，所以清单（以及 1.2 的接口错误表、1.6 的样本）从本活动更早的测量里补上这次没重跑的 train 题（同一 K 下 `measure` 臂的最近有效读数，安装后都是通过）；测量、诊断与 train 分数不变。
- **证据。** Meta-Harness 的消融：只给分数 34.6、给摘要 34.9、给原始轨迹 50.0；v0.3 的 R 每轮读 2～4 条失败轨迹、0 条通过的，support 与 hr 一条没读（诊断 RC4）。
- **预期效果。** 每个候选都落在 R 真看过的失败上，并且 R 看过“做对了的样子”，知道哪些习惯不能破坏。

### 1.4 失败方向

- **改什么。** 逐题笔记多一个 `direction` 字段：`omission`（漏读、漏写、漏步）| `over_action`（做了任务、数据或政策说不该做的写入、发送、修改）| `other`；笔记提示（新 R0 的 `research.diagnose` 文本）要求它；覆盖记录 `coverage.by_direction` 给各方向的笔记数。R 的方法说明：over_action 的失败靠克制修（一个“不写、不发、不改”的条件），不是靠多读多写。
- **证据。** 诊断 RC1：sales/hr 的退步事件约一半是“不得做”断言失败；R 看到的笔记里每轮约 5/30 条是过度行动，没有一个候选处理它们；9 个候选全是“多做”。
- **预期效果。** 克制型候选出现，或至少不再用“多做”的文字去修“做多了”的失败。

### 1.5 字面量检查的精度

- **改什么。** `harness_package.literal_findings` / `describe_hits`：报告文件、行号与字面量本身；**区分大小写**匹配（邮箱除外，邮箱不分大小写）；豁免纯数字——但 train 断言 params 里的数字（判分器查的值）不豁免——、JSON 键与表头（train 数据里作字典键出现，或在 `headers`/`columns` 列表里出现的串）、以及在 ≥2 道不同 train 题里出现的值；只出现在一道 train 题里的值照旧拒绝。（评审 2026-10-06：finance 的 train 断言 params 里有 98 个数字，90 个只属于一道题、分布在 50 道题中的 35 道，如 '13,650'；全豁免数字会让候选把一道题的金额写进包、在筛选里修好那道题。所以 `round_inputs` 在并入初始状态之前取断言 params 的字面量作 `answers`，交给 Drafts：一道题的断言数字照旧禁止，只在初始状态或代码里出现的数字（`[:3000]`）仍豁免。）字面量的来源从集合改为“字面量 → 出现它的 train 题”的索引（断言 params 与初始状态各自按题记）。检查输出里的字面量是 train 数据，R 本来就读得到（失败摘要与轨迹），所以写出来不扩大 R 能看到的东西；它只回给 R，不进包。
- **证据。** v0.3 的 20 份草稿被拒 14 份，全是普通词或常量的误拒（'Not Sent'、'Event Types'、`[:3000]` 里的 '3000'）；25 个回合里 14 个花在盲改上；finishgate 的触发条件被删成“总是”，rulesmem 的例子被删掉（诊断 RC4）。回放复现了每份草稿的命中数：14 份里 10 份只因草稿里的小写普通短语（'not sent'、'legal hold'、'tracking code'……）对上了表格里的大写值或表头，3 份只因 '3000'（纯数字，也在 6 道题里出现），1 份两者都有。
- **预期效果。** 回放 `workspace-claude-v3` 的 20 次检查：14 次误拒归 0；一个只属于一道 train 题的值（原样大小写）和一道题的断言金额照旧被拒（数字规则改后重放仍是 0/20）。回合花在分析上，带具体触发条件的 hook 与带例子的技能能留下来。

### 1.6 筛选看得见伤害，对照少一点噪声

- **改什么。** (a) 通过样本从本领域通过的 train 题里抽 **6** 道，**每轮重抽**，种子由 (`order_seed`, 轮次序号) 定，含“不得做”断言的题优先。(b) 资格：**≥1 个修复且通过样本里 ≤1 个退步**，再按净翻转排序（平手按成本、再按声明顺序）。(c) `[bench] control_readings = 3`：基线无条件（在任何 gate 之前）读 3 次 W0 的 dev，合并对照用全部读数；第一次读数的运行身份不变，另外两次是同一执行身份下的两个新运行（`notes` 标签区分）。安装之后样本与它的“before”覆盖整个 train 划分（这次没重跑的通过题从更早的测量补上，见 1.3），否则样本只能从 W0 先前的失败题里抽（评审 2026-10-06）。(a)(b) 由 K 的筛选表打开：`screen.max_sample_regressions`（v3 preset 1，上限）与 `screen.min_fixes`（1，下限）在 K 里时用 v0.4 的抽样与资格；v3 preset 同时把 `min_net_flips` 降到 0（修复 ≥1 且退步 ≤1 时净翻转本就 ≥0）、`passing_sample` 升到 6。
- **证据。** 诊断 RC3：失败集 63/63 都是 R 有笔记的题，样本内修复率 27～44%，dev 上只有 11～19%；7 次筛选的通过样本都是同 5 题、没有 hr；rulesmem 在 train 样本上 0/5 退步，到 dev 上退步 10 题；空候选以 0.43～0.57 的概率清过 `min_net_flips = 1`。RC2：三次 gate 共用一次 W0 读数，噪声约 ±3.9 题/125（单领域 ±7～9 个点）；三次读数把现行规则下的空候选误收从 1.0～1.6% 降到 0.2～0.4%（诊断 P3）。
- **预期效果。** 宽而总在作用的文字在筛选里就露出伤害，少付几次注定被拒的 gate；R 不再追 ±2～3 题的波动。

### 1.7 换掉 R 的旧系统提示

- **改什么。** 会话核心（K 有 `research_session`）新 bootstrap 的 R0：`research.propose` 换成 k 个候选的方法说明（`policies.PROPOSE_SESSION`：先看 api_errors、读失败与通过的轨迹、分方向、每个候选一个原因并写明何时不作用、优先有条件的改动、不写任务答案），`research.diagnose` 换成带 `direction` 的笔记提示（`DIAGNOSE_SESSION`）。协议（工作树、输入清单、工具、上限、声明格式、准入规则）留在 `SESSION_FORMAT`；方法（做几个、怎么选、先看什么）只在 propose 文本里。v1 核心（单次汇总调用）的 R0 不变。
- **证据。** 诊断 RC4：v0.3 的会话系统提示是旧的 `research.propose`（“Propose at most one change… Prefer… memory notes”），与会话要求的 k 个候选矛盾。
- **预期效果。** R 读到的是一份自洽的方法说明，而不是两份互相打架的指令。

### 1.8 final 并列等成本参照

- **改什么。** 报告从 final 自己的 W0 重复（每题 3 次）算 W0 的 pass@k（k = 1～3，按每题 n 次中 c 次通过的无偏估计，oracle 上界），与 W\* 的通过率并列，标为“等成本参照”，另写两臂的每次成本之比。不另付费；不进 C1 的裁决。
- **证据。** Wang 等的再评估：同预算的并行采样（72.3）胜过演化出的 harness（67.4）（RSI_PLAN §1.1）。
- **预期效果。** 读者能看到 W\* 的增益是否超过“同样的钱让 W0 多试几次再挑最好的”的上界。

## 2. 兼容与身份

- 已有工作区不动：K、W0、R0 都是存下来的对象，bootstrap 只在没有 active release 时发生。`workspace-claude-v3` 的 K 没有新键，继续 v0.3 的筛选与准入；它的划分已冻结在盘上。
- **身份会变的地方，只在新 bootstrap：** v3 preset 下新冻结的 K（筛选表多 `min_fixes`、`max_sample_regressions`，`passing_sample` 6、`min_net_flips` 0；会话表多 `min_failing_reads`、`min_passing_reads`）；会话核心新冻结的 R0（`DIAGNOSE_SESSION`、`PROPOSE_SESSION`）。所以 `claude-v4-*` 的 K 与 R0 都不是 `claude-v3` 的。
- **不变的身份：** 笔记的身份（`NOTE_FORMAT` 不变；方向由 R0 的文本要求）；运行身份（第一次基线读数不变，`control_readings` 缺省 1；新读数是新运行）；划分 hash；冻结计划的结构。
- **对所有活动、只影响新会话的改动：** 字面量检查（1.5）、`api_errors.json` 与轨迹清单里的通过尝试（1.2、1.3）、`SESSION_FORMAT` 与 `read_trajectory` 的说明文字（会话的 `prompt_hash` 记录随之变）、笔记方向的解析与计数（1.4）、报告的等成本参照（1.8）。

## 3. 验证运行（写进 ROADMAP，本版不跑）

单价用 v0.3 实测：W0 每次 $0.466，候选 $0.49；一次调用的预留上界 $7.2768。

**finance 先（约 $250）：**
1. 基线：train 50 + dev 25 × 3 次读数 = 125 次 × $0.466 = **$58.25**（其中两次额外的 dev 读数 $23.30）。
2. 3 轮：每轮 R（笔记与 k = 3 的会话）约 $10 + 筛选约 3 × (6 + 6) = 36 次 × $0.49 = $17.64 + dev gate 25 × $0.49 = $12.25，约 **$40**；第一次读数清过门槛时加确认 $12.25；装上后重跑上一版 train 失败题约 23 × $0.49 = $11.27。3 轮约 $120，含一次确认与一次安装约 $143。
3. 封存 final（只在有确认的安装时）：25 × 3 × ($0.466 + $0.49) = **$71.70**。
4. 合计预期约 $273；根与 final 份额的算式写在配置里（final 份额 $160，根 $450）。

**然后 support**，同样的形状与费用。

**预注册的读法：**
- 某领域有确认的安装 → 该领域封存 final（W0 对 W\*，test 25 × 3，并列 W0 pass@k）；结果照报。
- 3 轮没有安装 → 该领域停止，如实报告：R 以 k = 3、接口优先的方法在 3 轮内没有在该领域得到确认的安装。不续跑同一条管线。
- 某领域的 final 显著（95% 区间下界 > 0）→ 加 sales 与 hr，各自一个单领域活动，同样的配置形状。（2026-10-08，V2_TRAINING_PIPELINE §7 第 2 项：sales、hr 的 test 留给 v2；在这两域开 v1 活动时不跑 final，只报 dev 读数，或不开。）（同日用户问“不能全跑吗？”，改为全部域可跑 final：sales、hr 的活动照常可以封存 final，按有条件复用规则（构建学生的任何产物都不读之前 final 的逐题记录，代码强制）披露读取次数；只有 operations 整域留出。一个单领域活动名义约为基线 $56 + 3 轮约 $120 + final 约 $72（订阅），按域分批开。）
- **过程检查（免费，从记录读，每轮后、付下一轮之前）：** 字面量误拒为 0；每个候选都有准入认可的轨迹读取；3 轮里至少一个工具或 hook 候选到达筛选。不成立就先离线修，再付钱。
- 不做：不重判 v0.3 的三张收据；不在边缘读数后追加读数或重复（可选停止）；不给 R dev 题号、dev 轨迹或逐题 dev 翻转；不读 Marketing 已 spent 的 test。

## 4. 实现对照

| 项 | 代码 | 测试 |
|---|---|---|
| 1.1 | `bench.freeze_splits`（钉住的规则）；`examples/automationbench/claude-v4-finance.toml`、`claude-v4-support.toml` | `test_bench_phase.py::test_a_pinned_split_is_reproduced_under_whichever_rule_made_it_and_a_wrong_pin_is_still_refused`、`test_the_v04_configs_are_single_domain_activities_on_the_v3_workspaces_frozen_splits`、`test_the_v04_configs_reproduce_the_v3_workspaces_finance_and_support_splits_offline`（需要上游检出） |
| 1.2 | `research/api_errors.py`（`_url_parts` 接住 `urlsplit` 的 ValueError）；`research.session` 的输入与 `SESSION_FORMAT`；`bench.Improvement.session_inputs`、`carried_train_readings` | `test_diagnosis.py::test_the_interface_table_counts_where_the_agent_fights_the_api_from_train_attempts_only`、`test_a_url_urlsplit_refuses_is_counted_under_its_tool`；`test_research_round.py::test_the_session_lists_passing_attempts_must_not_first_and_reads_the_interface_table`、`test_a_package_tool_candidate_passes_the_draft_chain_and_reaches_the_screen` |
| 1.3 | `research.session.TrajectoryReader`（`listing`、`read_tasks`）、`CodingSession._ungrounded`（第一次 finish）、`grounding_inputs`；`research.experiment.grounding_reasons`、`declaration_reasons`（兜底）；`adapters.automationbench.has_must_not`；`bench.Improvement.carried_train_readings` | `test_research_round.py::test_grounding_needs_a_failed_read_among_the_predicted_tasks_and_one_passing_read`、`test_an_ungrounded_finish_is_refused_in_the_session_and_admission_is_the_backstop`、`test_read_tasks_counts_only_the_reads_the_host_served`、`test_after_an_install_the_session_inputs_see_the_whole_train_split` |
| 1.4 | `research.diagnosis`（`note_direction`、`direction_counts`、`by_direction`）；`policies.DIAGNOSE_SESSION` | `test_diagnosis.py::test_a_note_s_direction_is_read_normalised_and_counted_in_the_coverage` |
| 1.5 | `agent.harness_package`（`add_literals`、`header_names`、`forbidden_literals`、`literal_findings`、`describe_hits`）；`research.diagnosis.train_literal_index`；`bench.Improvement.train_state_index`、`round_inputs`（`answers`）；`research.drafts.Drafts.forbidden`、`answers`；`examples/automationbench/literal_replay.py` | `test_harness_package.py::test_the_check_output_names_the_file_the_line_and_the_literal`、`test_matching_is_case_sensitive_except_for_an_e_mail_address`、`test_only_a_value_of_one_train_task_is_forbidden`、`test_drafts_built_from_an_index_refuse_only_single_task_values`、`test_a_single_task_amount_the_grader_checks_stays_forbidden`、`test_the_twenty_recorded_v03_draft_checks_replay_to_no_false_refusal`（需要 v3 工作区与上游检出）；`test_research_round.py::test_the_round_hands_the_draft_check_the_literal_index_its_answers_and_the_header_exemptions` |
| 1.6 | `research.session`（`passing_sample_v4`、`screen_counts`、`eligible`、`pick_best`、筛选行的 `fixes`/`sample_regressions`）；`evolve.experience` 的台账与投影；`bench` 的 v3 preset、`HARNESS_TABLES`；`bench.baseline` 与 `[bench] control_readings` | `test_research_round.py::test_the_v04_passing_sample_is_redrawn_each_round_with_must_not_tasks_first`、`test_a_v04_screen_needs_a_fix_and_at_most_one_sample_regression_then_ranks_by_net_flips`、`test_the_baseline_reads_w0s_dev_three_times_and_the_gate_pools_all_three`、`test_after_an_install_the_session_inputs_see_the_whole_train_split` |
| 1.7 | `policies.PROPOSE_SESSION`、`DIAGNOSE_SESSION`、`default_research_set(session=...)`；`bench.open_activity`；`research.session.session_format`、`SESSION_READS` | `test_research_round.py::test_a_session_core_bootstraps_the_v04_r0_and_its_sessions_run_under_the_method_text` |
| 1.8 | `report.final_reference`、`pass_at_k`；`render_markdown` | `test_report.py::test_the_final_reports_w0s_pass_at_k_from_its_own_repeats_beside_w_star` |

## 5. 不做（两份被取代的设计里的部分）

路由（`worker.harness` 下的 routes）、按领域投影与隔离审计、借鉴库与端口提议、两阶段 gate 与按 W0 标定的门槛、结构零 final、冻结孪生点火检验（C4′）与盲账目臂、主张人群 gate（P5，单领域活动里没有意义）、留出切片、台账里的噪声带与“触发/未触发”拆分。等 finance 与 support 的结果出来再决定哪些值得做。
