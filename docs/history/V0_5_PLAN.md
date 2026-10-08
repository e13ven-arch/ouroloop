# v0.5 方案（2026-10-06；2026-10-07 评审修订）

> **现状（2026-10-08）：** 已实施（`7e7732e`），未发布；§3 的真实检验没跑完，正文是预注册记录、不改。3a 两臂基线完成（dev：v0.4 臂 26/75，v0.5 臂 27/75）；3b 经验库流 D = 3.0、B = 1，前后两半 0.00 → 0.12（P 0.8895），记功秩相关 0.2，快照轮 gate 首读 +0.24、确认 +0.08、合并 +0.16 [+0.047, +0.287]，装上 `9f07d95032b4`。2026-10-08 用户停止对照：3c、3d、3e 都没跑，H1 与 C1 未测；§3 方法库的两项过程读数（R 能造出可用的方法、学会了何处用）从未读出，即“选择未测”。按瘦身第三批的规则，经验库保留为 preset 开关，方法库拟删（用户可否决）。下文引用的 V0_5_AGENT_RL 已于 2026-10-08 删除（以及若删的 V0_5_METHOD_LIBRARY），H1 脚本 `support_h1.py` 与 `tests/test_support_h1.py` 也在同日移出 HEAD，都在 tag `archive-2026-10-08` 上可读。
>
> 2026-10-07 评审（协调者在隔离副本里跑了脚本化的 v0.5 臂全流程；有效性、公平性与旧工作区身份都核过）：14 条确认，全部按其决定改了——两臂的根都升到 $600（等上限、同日程，各报实花）；经验库的中止规则只看 D；固定代码的轮次可续跑、不计入 R 的轮次；带附注的重试不记功；H1 在一臂没装东西时的配对写死并给了脚本；以及若干记录与测试。改动散在 §1.3、§1.4、§3、§5、§6、§7、§8。

> 本文是 v0.5 的规则来源。三份设计稿 V0_5_METHOD_LIBRARY（tag archive-2026-10-09）（方法库）、V0_5_EVOLVING_SEARCH（tag archive-2026-10-09）（进化搜索）、V0_5_AGENT_RL（agent 内 RL；2026-10-08 删除，tag `archive-2026-10-08` 上可读）是本文的输入，保留作依据；三者与本文不一致处以本文为准。
> 先文档后执行：本文、ROADMAP “v0.5” 一节与 JOURNAL 条目先写，代码与测试随后；全部离线，没有调用模型，没有付费阶段，没有读 `.env`，没有读任何 test 结果。正在付费运行的 `workspace-claude-v4-finance` 不碰。

## 0. 结论

**v0.5 = v0.4 的单领域贪心环（骨干，一字不改）+ 方法库 + 经验库。** 每个新增部件只在新 K 键（preset `automationbench_harness_v5` 的 `methods`、`library` 两张表）下生效；v0.4 的配置与工作区照旧运行。

**为什么是这两样。**

| 部件 | 证据 |
|---|---|
| 方法库：R 把反复出错的搜索、表格分析、版本判断、发送前核对写成只读、确定性的包工具（方法），带卡与触发器 | 158 道失败 train 题里 140 道带方法可修的模式，117 道已确认；按族：核对 50、分析 40、搜索 38、版本判断 12、统计 6（方法库 §1）。v0.3/v0.4 的 12 个候选里 0 个工具 |
| 方法的适用性由固定代码从 train 配对奖励学（每格一个 Beta 后验，冻结成贪心表） | 方法在哪里适用看题目看不出来（关键词只拉开 ±0.1）；宽泛的核对伤人：send_review 修 3、坏 2。方法库的仿真：finance 50 道 train、证据级伤害下，全开 −1.2，学到的情境开关 +2.8（50 次尝试）～+7.4（400 次），每 100 道新题的净题数 |
| 经验库：从 train 尝试里验证过的成功提炼条目，按题相似度检索给 actor，按 train 奖励记功、退役，整库作为候选过筛选与 gate，final 前冻结 | 进化搜索的统一仿真里，四类机制中只有它赢过贪心：$300 时 L+G 对 G 在 finance +0.17 ± .07、support +0.26、sales +0.23 题/25；也只有它随见过的 train 题变好：同 $300，train 题 25/50/100/200 时 G 为 .71/.71/.64/.63，L+G 为 .71/.88/.94/.95；坏安装 0.05～0.15 对贪心 0.20～0.24 |

**照实说的风险。** 两样的增益都小于一次 25 × 3 final 的分辨率（经验库 +0.1～+0.35 题/25；方法库预期 dev +2～+5 题、test 约一半）。经验库的符号取决于三个从未量过的参数（检索命中、无关条目的伤害、提炼命中），取悲观值时 L+G 是 −0.13～−0.27。所以 v0.5 的主张只看预注册的封存 final，过程数字只作过程证据。

**不做什么（考虑过、不做，§4）。** 档案 + 合并、自适应评估（B-Race）、算子老虎机、完整的 agent 内 RL。AGENT_RL 只留下它的“收住伤害”作用，落在方法表的 silent 格上（§1.3）。

## 1. 采纳的部件

### 1.1 方法库（方法库 §2、§4）

- **方法是包工具。** 名字以 `m_` 开头；在 v5 的 K 下拿只读 broker（写调用由宿主拒绝），每次调用至多 `methods.method_calls = 25` 次基础调用，超过就回错误、不发调用；输出照旧截在 20,000 字符。只返回带出处的证据和 `unclassified`，不写、不替 actor 决定、不调模型。
- **声明在模块里。** 每个方法模块写一个字面量 `METHODS = {"m_x": {family, trigger, kinds, on, footprint, split?, variants?, default_variant?, couples_with?}}`，宿主用 `ast` 读、从不 import；卡就是工具的 description（USE WHEN / INPUT / OUTPUT / FAILS WHEN / NOT FOR）。触发器是事件与 view 的纯函数。
- **kit 由固定代码补齐、R 不能改。** 候选第一次加入 `m_` 工具时，草稿检查之前补上 `middlewares/m_router.py`（kit 字节）、`middlewares/m_table.py`（格的字面量；新方法的格取 R 声明的 `on`，已学到的格保留）与三个路由 hook；R 改过这两个文件就被替换并在检查输出里说明。路由：after_tool 跑触发器、按格 recommend 或 inject；before_tool 对外发与分类写入做 `block_once`；on_finish 做 `pushback`。上限每次尝试 3 次推荐、2 次注入、2 次拦截；核对类只在尝试内日志非空时出手（send_review 的教训）。
- **草稿链多两道免费检查。** kit 检查（路由字节、表能解析且每格单一动作、格指向存在的方法与声明过的种类、变体 ≤ 4、分桶特征 ≤ 1）；触达检查（在 `predicted_affected` 的失败 train 题的录下事件上重放新方法的触发器，第一次触发时在该题的新 train 世界里经只读 broker 跑一次方法；底线：≥ 1 道声称的题触发且无错跑完；另要求触发器在全部 train 回放里碰到 ≥ 2 道不同的题）。只回计数给 R；断言值在宿主端计数，从不进 worker。字面量检查照旧覆盖方法代码、测试与表。
- **R 的输入（全部只来自 train）。** `inputs/methods.json`（每个已装方法的调用、错误、输出长度、`unclassified` 占比、每格的触发题数、动作、后验与 learn 计数、伤害尝试的引用）、`inputs/uncovered.json`（失败 train 尝试按“失败断言类 × 行为特征”聚类，标明有无方法覆盖）、`inputs/method_research.json`（R 自己历次方法候选的去向）、`inputs/method_kit/`（路由的只读参考、表存根、`_mlib.py` 模板、测试模板）。会话说明加方法一段（方法库 §4.2 的原文）。
- **R 的研究剧本。** `finish` 可带 `playbook`：每条教训以 `[<候选 id>: <字段> <值>]` 结尾，宿主逐条对台账，对不上就删并告诉 R；通过的文本存进台账，下一次会话接在方法说明之后，会话记录剧本的哈希。只报过程数字（被删的引用数、条数），不作改进器层的主张。

### 1.2 参考方法：只作夹具与消融，不给 R

方法库 §2.4 预测的头一批（文档存储的扇出搜索 `m_doc_search`、表格的行台账 `m_row_ledger`、政策的版本时间线 `m_policy_timeline`、发送前核对 `m_outbound_lint` 与写后核对 `m_effect_audit`）由我们写成参考方法，各带测试。它们用于机制测试（触达、路由、拟合在真实形状的方法上走通），以及预注册的“人工种子方法库”消融（方法库 §10，只在 R 造不出方法时由用户决定是否跑）。**它们不进 R 的输入，也不进 W0**：方法要由 R 从自己读到的失败里发明，否则自主性账目不成立（方法库 §1.8）。

### 1.3 适用性学习（方法库 §3；AGENT_RL 只留这一层）

- **learn 读数，只在 train。** 固定代码给当前 W\* 造两个孪生包 A、B，只差 `m_table.py`：`mode = "explore"`、盐值 s = (`order_seed`, 阶段)、孪生标签。开关决策 u = H(s, 任务文本, 决策) ∈ {0, 1}，A 取 u、B 取 1 − u；变体 A 取 H mod k、B 取另一个。倾向值精确（开关 0.5，变体 1/k）。
- **只跑会触发的题。** 触发器是纯函数，可以在 W\* 录下的 train 事件上免费重放；两个孪生都不触发的题不带信息（方法库 §3.3），所以 learn 读数只跑重放触发过的 train 题，至多 `methods.learn_max_tasks = 24` 道（2 × 24 次尝试）。这是为等上限做的收缩（§3）。
- **记功与冻结规则照方法库 §3.4～§3.5。** 按方法声明的断言类记功（`footprint`），题级修复与退步另计，“不得做”断言另记；p_c ~ Beta(1 + k + ½k′, 1 + m + ½m′)。转 on：n ≥ 5、P(p > ½) ≥ 0.8、题级修复 − 退步 ≥ 0、不得做 ≤ 1、成本升幅 ≤ 25%；转 silent：n ≥ 5 且 P(p < ½) ≥ 0.8，或不得做 ≥ 2；其余不动。变体与分桶同理（两边各 ≥ 5）。
- **重拟候选走自己的固定轮。** 拟合出的表与已装的不同时，固定代码造一个只改 `m_table.py`（及全静默、自发调用 < 5% 的方法的 `enabled`）的候选，筛选（格变了的触发题 + 6 道通过样本）、gate、确认都照 v0.4。与方法库 §3.6 的差别：它不占 R 的 k 个名额、不与 R 的候选在同一轮里比净翻转；费用相同。
- **先看钱够不够（2026-10-07）。** learn 在第一次孪生尝试之前，检查孪生、重拟筛选的上限（触发题 + 通过样本）与 gate 的核验预留加起来放得下；放不下就以 budget_exhausted 拒绝，一分钱不花。固定轮在第一次筛选尝试之前做同样的检查。
- **可续跑（2026-10-07）。** 固定轮的候选 id 是 `<scope>/f<序号>/<算子>`，实验 id 由候选 id 推出（`x_` + 摘要），所以筛选建出的候选 W、筛选尝试的运行身份、gate 的 Execution 在重跑时都是同一个；没结束的固定轮（有声明行、没有终结的 round 行）沿用它的序号；已写的阶段不再写，已付的尝试不再付。R 的段在续跑时跳过来源为 `fixed` 的实验，它从不算作 R 的一轮，由它自己的阶段收尾。learn 重跑时，只要上次读数的重拟轮还没**结束**（不只是声明过），就收尾那一轮，而不是再买一次读数。
- **冻结的收据。** dev/test 的运行在装配时拒绝 `mode != "greedy"` 的表，不花一分钱；dev/test 的 `method_decision` 标记倾向值必须全为 1，否则该读数作废（报告标出）；探索孪生只出现在 `learn` 阶段的运行里。
- **AGENT_RL 留下的就是这些。** silent 格是学出来的克制；开关不推广到其他已装组件、不加近失候选、不做 DPV 与 RL 取代筛选（§4）。

### 1.4 经验库（进化搜索 §2.4）

- **条目。** `entry_id`（正文的哈希）、`shelf`（工具或意图族）、`kind`（workflow | insight | api_note）、`title` ≤ 80、`when` ≤ 300、`body` ≤ 700（值写成 `<amount>` 这样的占位符）、`avoid` ≤ 200、`source_hashes`（来源题及其 train 近似重复题的题目文本哈希）、`provenance`（train 题号与尝试 id，只在引擎侧）、`origin`（success | contrast | reflexion）、`born_at_n`、`status`、`stats`（`n_shown`、`adv_sum`、`harm`、`fixes`）。只追加，写进台账。
- **流，只在 train。** 冻结排列（种子由 (`order_seed`, 领域) 定，哈希进记录）。走到题 t：(1) 本领域里 t 的已有 train 尝试进提炼队列（成功提炼，成功与失败成对时做对比提炼）；(2) 首遇读数 W_base ⊕ L_n，检索按来源哈希留一；(3) 失败时同一包重试一次，附上失败断言的类型与参数投影（与 R 看到的相同）；(4) 每道有新成功的题调用一次固定提示（代码常量 `library.DISTILL`，合并用 `library.MERGE`；两者的哈希记进流的记录；不进 R0，R 改不了它，改进器层改提炼提示推迟），至多 2 条。
- **固定检查。** 模式与长度；字面量检查（单题的值、单题的断言数字）带一次自动占位替换，仍不过就丢；`when` 必须在来源之外检索到 ≥ 2 道 train 题；与已有条目 TF-IDF 余弦 ≥ 0.8 就做一次 MERGE（逐条，从不整库重写）；每架 ≤ 6 条，满了挤掉效用最低的。
- **检索是宿主代码。** 在组系统提示处（`start` 位点），任务文本对条目 `title + when` 做 TF-IDF，过阈值、按相关度再按效用排序，至多 3 条插在系统提示之后；来源哈希包含本题文本哈希的条目不检索（train 上即留一；dev/test 题从来不是来源，同一条规则下不排除任何条目）。每次检索写一条 `library_retrieval` 事件（条目、分数、字符数）。包里没有库文件时，系统提示与原来逐字节相同。
- **记功与退役。** 有条目被检索出的每次首遇尝试：`n_shown + 1`，`adv_sum += r − b(t)`（b(t) 是 W_base 在该题上全部有效 train 读数的均值），b(t) = 1 而 r = 0 时 `harm + 1`。`harm ≥ 2`，或 `n_shown ≥ 3` 且 `adv_sum / (n_shown + 2) ≤ −0.2`，就退役。**重试不记功**（2026-10-07）：重试带着失败断言的附注，它的成败不只是条目的功劳；它只作 reflexion 提炼的来源。
- **流可续跑（2026-10-07）。** 每道题结束、以及首遇之前的提炼之后各存一次检查点，重跑从检查点继续，已付的提炼与尝试都不再付；提炼或合并调用遇到研究模型的错误（ProviderError，含截断与拒答）时，流在上一个检查点暂停，不当作崩溃。流的记录（`library_entry`、`library_stream`）只写一次；快照轮被打断后，重跑接着同一个固定轮。
- **快照进 W。** 流走完（n = 50）编译一次快照：每架一个 `skills/library/<shelf>.md`，带段标记 `<!-- entry: <id> shelf=<shelf> -->`，在 manifest 里列为**停用**的技能（不进技能索引、`read_skill` 读不到，只由宿主检索）。快照是固定代码声明的候选（算子 `library`），在自己的固定轮里过草稿检查、筛选（W 失败的 train 题 + 6 道通过样本，按留一检索）、gate、确认，与 v0.4 逐字节相同的规则。装上之后库冻结为 W 的组件；之后 R 的轮次在它之上，R 读 `inputs/library.json`（条目、书架、记功计数、退役原因，只来自 train）。
- **中止规则（预注册；2026-10-07 修订）。** 流走完时 D = Σ(r_lib − b) ≤ 0：不给快照付 gate，钱回到 R 的轮次，如实报告。B（W_base 在 train 上通过、首遇失败的题数）只作过程数字报告，不决定去留：伤害由快照轮自己的筛选（样本退步 ≤ 1）、dev gate 与确认读数挡。

## 2. 与三份草稿的差异（为等上限与单领域实测）

| 草稿 | 本文 | 原因 |
|---|---|---|
| 快照在 n = 25 与 n = 50 各过一次 gate（进化搜索 §2.4.3）| 只在 n = 50 一次 | 等上限（§3）：少一次筛选 + gate + 确认约 $40 |
| 每题一次同期 W0 对照（§2.4.2 第 3 步）| 对照取基线的 W0 train 读数 | 库的轨道紧接基线、同一个 W_base；省 $23.30。配对差不按失败选题，没有回归均值的偏差；漂移的风险照实记下 |
| learn 读数跑全部 50 道 train（方法库 §3.3）| 只跑在录下事件上重放触发过的题，≤ 24 道 | 两孪生都不触发的题不带信息；省约 $26 |
| 重拟候选是下一轮的第 k + 1 个候选（方法库 §3.6）| 自己的固定轮 | 不占 R 的名额、不与 R 的候选混排；费用相同 |
| S1 finance 机制检验、S1-L finance 库探针 | 不跑 | finance 的 test 已被 v0.4 用掉；本版的真实检验只在 support（§3）|
| 跨领域端口、R 的研究动作表跨活动累积 | 推迟 | v0.5 只有一个领域在跑 |
| 剧本存成 R 的新版本 R_g（方法库 §4.5 (b)）| 剧本经台账核对后作为会话输入，哈希进会话记录 | 改进器层本版只报过程数字；不新增研究集版本，R0 的身份不动 |

## 3. 真实检验：support 上的两臂，等上限、同日程

**形状。** 两个活动，各自的工作区、各自的 W\*，同一个划分（support `3da11232f5d5ff38`，test 25 道从未读过）、同一个根 $600、同一个 final 份额 $160、同一个日程（一段 3 轮 R、patience 2）、同样的单次尝试硬限额、同一个 actor 与 K 的其余部分：

- **v0.4 臂**：`examples/automationbench/claude-v4-support.toml`（基线 → 3 轮、patience 2 → 有确认的安装才有 final）。它自己的路径约 $203，用不完 $600。
- **v0.5 臂**：`examples/automationbench/claude-v5-support.toml`：基线 → `--phase library`（流 + n = 50 的快照轮）→ `--phase cycle`（R 的轮次，带方法；3 轮、patience 2）→ 有方法装上时 `--phase learn`（learn 读数 + 拟合 + 重拟轮；钱不够就拒绝，一分不花）→ final（**总是跑**；没装上任何东西时 W\* = W0）。

**等上限，不是等花费（2026-10-07 的决定）。** 两臂的根都是 $600：v0.5 臂能跑完它的整套设计，v0.4 臂按自己的日程停下、用不完。所以比较的是“同样的上限、同样的日程下，两套框架各自做到哪里”；报告在 H1 旁边并列两臂的实际花费（`support_h1.py` 读各自的预算账本）。

**final 的顺序（写死）。** (1) v0.4 臂若有确认的安装：填 `[freeze.final]`（W\* 与 W0），`ouroloop freeze` 封存并打印计划哈希；(2) v0.5 臂同样封存；(3) 两份哈希（或 v0.4 臂没有 final 的说明）随 JOURNAL 提交；(4) 然后才各自 `--phase evaluate`：support 的 25 道 test × 3 次，W0 对 W\*，并列 W0 的 pass@k；(5) `python examples/automationbench/support_h1.py --v5 <v0.5 工作区> --v4 <v0.4 工作区>` 算 H1。任何一份 final 运行之前两份计划都已封存（test 不用来挑选任何东西）。

**预注册的读法（在第一笔付费之前随 JOURNAL 提交，之后不改）。**

| 命题 | 成立 | 否定 | 不确定 |
|---|---|---|---|
| H1（v0.5 的主张）：等上限下 v0.5 的 W\* 胜过 v0.4 的 W\* | 两份 final 按题配对（每题 3 次的均值），W\*_v5 − W\*_v4 的 95% 自助区间（对题重抽 2000 次，种子 1）下界 > 0 | 下界 ≤ 0 且点估计 ≤ 0 | 下界 ≤ 0、点估计 > 0（预期最常见）|
| H1 的对照在一臂没装东西时 | v0.5 臂没装东西：它的 W\* 就是 W0，final 照跑，H1 照算 | v0.4 臂没装东西：它没有 final（V0_4_PLAN §3），W\*_v4 = W0；H1 改用 v0.5 那份 final 里自己的 W0 臂作对照 | — |
| C1（每臂）| W\* − W0 的 95% 区间下界 > 0 | 下界 ≤ 0 且点估计 ≤ 0 | 其余 |
| 经验库在 train 上有效（过程）| D ≥ +2 | D ≤ 0（快照不付 gate）| 0 < D < 2（照常付 gate）；B 只报告 |
| 经验库的学习曲线（过程）| 后 25 道的配对效应 − 前 25 道 > 0，自助法 P ≥ 0.8 | ≤ 0 | — |
| 记功有信息（过程）| n = 25 时条目的效用与它们在 n = 26～50 的优势秩相关 > 0 | ≤ 0：报告“退役规则没有信息” | — |
| R 能造出可用的方法（过程）| v0.5 臂里 ≥ 1 个方法候选过了筛选 | 0 个：如实报告“在这套协议下 R 没有造出有用的方法” | — |
| 学会了何处用（过程）| learn 跑了，≥ 1 格以 0.8 离开默认，且重拟候选过了 gate | 重拟没过 gate | learn 因预算或无方法安装没有跑：报“选择未测” |

**功效，照实写。** 25 × 3 的 final，单臂 95% 区间半宽约 ±12 个点；H1 是两臂之差，噪声更大。仿真预期的差是 +0.15～+0.3 题/25（经验库）加上方法库的不确定增益，真实增益 +8 个点时 H1 的功效约 30%。所以 H1 预期多半“不确定”；这一点先写在这里，结果照报，不追加读数、不按 dev 挑快照。

**学习曲线怎么画。** 横轴 n（流里的序号），纵轴每 10 道题一个窗口的配对效应 mean(r_lib − b)，自助区间；分 W0 稳定失败题上的修复、稳定通过题上的伤害、全部三条。报告另画配对剂量—反应（同一道题首遇与末快照筛选读数之差，对“首遇时库还差多少经验”）。曲线是“学习确实发生了”的过程证据，不是主张。

## 4. 考虑过、不做

| 做法 | 来源 | 不做的原因 |
|---|---|---|
| 档案 + 帕累托 + 机械合并 | 进化搜索 §2.1 | 统一仿真里对贪心 −0.05～−0.20；合并把样本内过拟合的修复和两份伤害一起并进来；近失候选的成功改由经验库提炼 |
| 自适应评估 B-Race（知识梯度、隐藏留出、送 gate 规则）| 进化搜索 §2.2 | 统一仿真 ≤ 0；完美挑选的天花板只有 +0.06～+0.13；优势依赖还没量过的 ρ 异质性 |
| 算子老虎机（Thompson、盲切片奖励、强制名额）| 进化搜索 §2.3 | 统一仿真 −0.12～−0.16；单领域功效 0.10～0.12，学不动 |
| 完整的 agent 内 RL（OAB、DPV 快照分支、进化的路由程序、RL 取代筛选、近失候选开关、所有已装组件转开关）| AGENT_RL §0、§5 | 仿真里贪心在每个标定世界领先（搭车形式 −0.4～+0.3 pp）；只保留“收住伤害”的部分，落在方法格上（§1.3）|
| 库跨领域合并、方法跨领域端口 | 进化搜索 §9、方法库 §2.9 | 库跨领域在仿真里每领域约 −0.2；v0.5 只有一个领域，端口没有来源 |
| 分页方法、裸计算器 | 方法库 §1.4 | 22,130 个输出里 0 例下一页；算术失误 0 例 |
| 种子方法库作为起点 | 方法库 §10 | 发明会变成我们的；参考方法只作夹具与事后消融（§1.2）|
| 第二批 top-two Thompson 读数、按目标连线的记功、变体学习的单领域版本 | 方法库 §3 | 样本不够；先看第一次 learn 读数 |
| 在 dev/test 流上在线学习、LLM 判官给条目或方法打分、会写入的方法、R 写表或写库条目 | 三份草稿的“不做” | 留出证据不成立；不确定、回放不了；学到的部分会被 R 的叙述左右 |
| final 加“W\* 去库”“W\* 表全开”两臂 | 进化搜索 §5.4、方法库 §7.3 | 各 $37.50，放不进等上限的 $160 份额；需要时由用户另批 |

## 5. 预算（v0.5 臂；v0.4 臂见它自己的配置；2026-10-07 修订）

单价：W0 $0.466/次；带库的尝试 $0.47，带方法的 $0.50；提炼 $0.14/次（finance 第 1 轮笔记实测）；一次调用的预留上界 $7.2768。

| 步 | 内容 | 预计 |
|---|---|---|
| 基线 | train 50 + dev 25 × 3 = 125 × $0.466 | $58.25 |
| 库的流 | 首遇 50 × $0.47 $23.50；重试 ≤ 25 × $0.47 $11.75；提炼 ≤ 75 × $0.14 $10.50 | $45.75 |
| 快照轮（n = 50；D > 0 时）| 筛选约 33 × $0.47 $15.51；gate 25 × $0.47 $11.75；确认 $11.75；装上后重跑 W0 的约 27 道 train 失败题 $12.69 | $51.70 |
| R 的 3 轮 | 3 × $39.89 $119.67；一次确认 $12.25；一次安装后的重跑 $13.23 | $145.15 |
| learn + 重拟（有方法装上时）| 孪生 ≤ 48 × $0.50 $24；重拟筛选 ≤ 12 × $0.50 $6；gate $12.50；确认 $12.50 | $55.00 |
| final 之前合计 | | $355.85 |
| final | 25 × 3 × ($0.466 + $0.50) | $72.45 |

**根 $600 的算式。** 预留：8 路在途 $58.21；final 之前约 655 次尝试 × 8.6 次调用 + 约 300 次研究与提炼调用 ≈ 5,900 次调用，按约 1/2,250 卡住，3 次，$21.83。$355.85 + $58.21 + $21.83 + final 份额 $160 = $595.89，余 $4.11（learn 付钱之前自己核对余额）。final 份额 $160 放得下 final：$72.45 + 8 路 $58.21 + 1 次卡住 $7.28 = $137.94。v0.4 臂同样是 $600 的根，它自己的路径 $203.40 加预留只用 $436.16 的量，余下不花；两臂的实际花费在报告与 `support_h1.py` 里并列。

## 6. 身份：哪里变、哪里不变

- **不变：** 已有工作区（v1、v3、v4-finance、v4-support）的 K、W0、R0、划分、运行身份与回放；v0.4 配置在本版代码下的行为（新行为只在 K 有 `methods` / `library` 表时出现，测试逐项钉住）。`m_` 前缀的只读与调用上限只在 view 带 `methods` 时生效，而 view 只在 K 有 `methods` 时带它。
- **会变的，只在新 bootstrap：** preset `automationbench_harness_v5` 冻结的 K（多 `methods`、`library` 两张表）；v5 bootstrap 的 R0：`research.propose` 是 `PROPOSE_SESSION` 加方法一段（新常量 `PROPOSE_SESSION_V5`），研究集的其余策略与 v0.4 相同（提炼提示是代码常量，不在 R0 里）。所以 `claude-v5-*` 的 K 与 R0 都不是 `claude-v4-*` 的。
- **对所有活动、但不改变任何已有身份的改动：** 宿主代码的哈希（`code_version`，每次提交都变）；`default_agent.py` 的字节（每次运行由宿主附上，不进任何身份；无 `methods` 时行为逐字节等价，测试钉住）；`gateway.MARK_KINDS` 多 `method_decision`；`PackageAgent` 多一个可选的检索块与 `library_retrieval` 事件（包里没有库时不出现）与一个可选的附注（只有库的重试给）；`contracts.ORIGINS` 多一个允许值 `fixed`；`run_arm` 在 dev/test 上拒绝探索表（只有 learn 的孪生带它，旧包没有）；报告的 `v05` 段只在有 v0.5 记录时出现；`train_screen` 多可选的 arm、notes、repeat（缺省时与原来同一个运行身份）；（2026-10-07）每条 `broker_event` 多一个 `read_only` 字段（转录里的记录，不进任何身份）；台账折叠后的条目对固定代码的候选多一个 `fixed_code`；R 的段续跑时跳过来源为 `fixed` 的实验（旧工作区里没有这种实验）。worker 镜像不需要重建：触达检查用现有的 `hook` 协议，`worker_runner.py` 未改。v0.4 尝试的第一条系统提示与本版之前逐字节相同（金样测试钉住，摘要取自 0b9b475 的代码）。

## 7. 实现对照

行号以本版提交时为准。测试文件：`tests/test_methods.py`（方法库单元与 attempt 内行为）、`tests/test_library.py`（经验库单元）、`tests/test_v05.py`（K、R0、会话、草稿链、配置）、`tests/test_v05_phases.py`（learn 与 library 两个阶段、报告）。

| 项 | 代码 | 测试 |
|---|---|---|
| v5 preset 与 K 的两张表（只由 preset 设，配置不能改；方法需要 tools 与三个 hook 位点）| `bench.py:1910` `_v5_preset`、`:1921` `PRESET_ONLY_TABLES`、`:2143` `harness_settings` 的 v0.5 段；`research/methods.py:73` `MethodSettings`；`research/library.py:47` `LibrarySettings` | `test_v05.py::test_the_v5_preset_freezes_the_method_and_library_tables_into_k_and_bootstraps_r0_with_the_methods_text`、`test_a_config_cannot_set_the_preset_only_tables` |
| v5 的 R0（方法一段）| `policies.py:745` `METHODS_PARAGRAPH`、`:761` `PROPOSE_SESSION_V5`、`:776` `default_research_set(methods=)`；`bench.py` bootstrap 处 | 同上 |
| 方法的只读与调用上限（view 带 `methods` 时）| `agent/default_agent.py:101` `_Refused`、`:137` `_PackageContext._mark`（只在 methods K 下挂成 `mark`）、`:278` `_execute`；`agent/runner.py:274` `release_harness_table`、`:287` `k_methods`、`attempt_view` 的 `methods`；`evolve/task_eval.py:389` | `test_methods.py::test_an_m_tool_gets_a_read_only_broker_and_a_post_is_refused`、`test_an_m_tool_past_its_call_cap_gets_an_error_not_a_call`、`test_a_hook_context_has_mark_only_under_a_methods_k`；`test_v05.py::test_a_v5_ks_attempts_carry_the_methods_view_and_a_v4_ks_do_not` |
| `method_decision` 标记 | `agent/gateway.py:52` `MARK_KINDS` | `test_methods.py::test_the_router_decides_once_marks_the_decision_and_recommends_within_its_caps` |
| kit：路由、表、`_mlib` 模板、触达 harness、测试模板 | `research/method_kit/m_router.py`、`m_table.py`、`mlib.py`、`reach.py`、`template_test.py` | `test_methods.py::test_the_router_decides_once…`、`test_a_silent_cell_marks_its_decision_and_says_nothing`、`test_the_router_blocks_a_body_once_and_lets_the_unchanged_resend_through`、`test_the_journal_does_not_survive_an_attempt` |
| kit 补齐、kit 检查、`METHODS` 用 ast 读 | `research/methods.py:183` `kit_complete`、`:222` `kit_check`、`:103` `literal_of`、`:136` `methods_of`；`research/drafts.py:103` `Drafts.complete`、`:191` kit 检查 | `test_methods.py::test_kit_completion_gives_a_method_candidate_the_router_the_table_and_the_hooks`、`test_r_edits_to_the_router_or_table_are_replaced_and_reported`、`test_a_candidate_without_methods_is_untouched_and_one_that_drops_them_loses_the_kit`、`test_the_kit_check_refuses_a_table_or_declaration_outside_the_contract`、`test_the_router_is_the_kits_bytes_and_the_table_is_parsed_not_imported`、`test_the_literal_check_covers_method_code_and_the_kit_check_runs_in_the_draft_chain` |
| 触达检查（≥ 2 道 train 题触发；声称的失败题上触发并跑通；断言值只在宿主计数）| `research/methods.py:370` `reach_check`、`:325` `replay_events`；`research/drafts.py:211`；`bench.py:1340` `reach_fn`、`:1329` `recorded_attempts`；`research/session.py:1404` `_reach_counts` | `test_methods.py::test_the_reach_check_replays_recorded_train_events_through_the_trigger_and_runs_the_method_on_a_fresh_train_world`、`test_a_method_that_never_fires_on_its_predicted_tasks_is_refused_with_counts`、`test_a_trigger_that_fires_on_one_train_task_only_is_refused`、`test_a_method_that_raises_in_the_reach_check_is_reported_with_its_error`；`test_v05.py::test_a_method_candidate_is_completed_checked_reached_and_screened`、`test_a_method_that_never_fires_goes_back_to_r_with_the_reach_counts` |
| R 的会话：方法输入、方法说明、带剧本的 finish | `research/session.py:199` `SESSION_METHODS`、`:211` `session_format`、`:152` `FINISH_V5`、`:1082` 起的 v0.5 段；`bench.py:1425` `method_inputs`；`research/methods.py:783` `method_telemetry`、`:890` `uncovered_clusters`、`:919` `research_moves`、`:1015` `method_kit_inputs` | `test_v05.py::test_the_session_inputs_carry_the_method_library_inputs_from_train_only`、`test_the_v4_preset_keeps_its_k_r0_and_session_text` |
| 剧本的引用核对 | `research/methods.py:955` `playbook_check`；`research/session.py:1125` | `test_methods.py::test_a_playbook_lesson_without_a_valid_ledger_citation_is_dropped`；`test_v05.py::test_a_playbook_lesson_is_kept_only_with_a_matching_ledger_citation_and_shown_to_the_next_session` |
| 适用性学习：孪生、观测、拟合、冻结规则、重拟 | `research/methods.py:465` `assign`、`:475` `twin_files`、`:547` `observations`、`:619` `cell_rule`、`:681` `fit_table`、`:737` `refit_files`；`bench.py:3001` `learn`、`:3113` `_refit_round`、`:1383` `trigger_replay`、`:1367` `package_candidate` | `test_methods.py::test_the_explore_twins_flip_every_gate_between_a_and_b_with_exact_propensities`、`test_fit_table_recovers_a_planted_cell_and_keeps_the_default_below_five_tasks`、`test_a_split_needs_five_tasks_on_each_side`、`test_other_domains_and_pre_install_counts_enter_at_half_weight`、`test_observations_pair_the_twins_by_decision_and_skip_a_task_where_neither_fired`、`test_the_refit_candidate_changes_only_the_table_and_the_enabled_flags`；`test_v05_phases.py::test_the_learn_phase_runs_antithetic_twins_on_train_fits_the_table_and_gates_the_refit`、`test_an_interrupted_refit_round_is_finished_on_the_next_learn_without_another_reading` |
| 冻结的收据（dev/test 拒绝探索表；倾向值审计）| `bench.py:727`（`run_arm`）；`research/methods.py:484` `frozen_table_problems`、`:509` `propensity_problems`；`report.py:126` `v05_evidence` 的 `non_greedy_decisions` | `test_methods.py::test_a_dev_or_test_reading_refuses_an_explore_table_and_a_propensity_below_one_is_invalid`；`test_v05_phases.py::test_the_learn_phase_refuses_a_k_without_methods_and_a_dev_run_refuses_an_explore_table` |
| 固定代码的候选轮（library、refit）| `research/fixed.py:70` `run_fixed_round`、`:57` `fixed_candidate`；`evolve/contracts.py` `ORIGINS` 加 `fixed` | 两个阶段的测试 |
| 参考方法（夹具与消融，不给 R、不进 W0）| `research/method_kit/reference/m_search.py`、`m_tables.py`、`m_policy.py`、`m_verify.py`；`research/methods.py:1070` `reference_files` | `test_methods.py::test_the_row_ledger_…`、`test_the_doc_search_…`、`test_the_policy_timeline_…`、`test_the_outbound_lint_…`、`test_the_effect_audit_…` |
| 足迹断言类 | `research/methods.py:515` `assertion_class`；`adapters/automationbench.py:482` | `test_methods.py::test_assertion_classes_cover_the_five_footprints` |
| 经验库：条目、检查、去重合并、记功退役、快照、检索 | `research/library.py`：`:162` `make_entry`、`:228` `check_entry`、`:195` `literal_pass`、`:217` `reach_beyond_sources`、`:245` `duplicate_of`、`:261` `merged`、`:275` `credit`、`:288` `retire_reason`、`:297` `shelf_overflow`、`:347` `with_library`、`:381` `keep_parents_library`、`:451` `retrieve`、`:472` `retrieval_block` | `test_library.py` 全部 12 个 |
| `start` 位点的检索（宿主代码，留一，记录 `library_retrieval`）与重试的附注 | `agent/runner.py:142`（`PackageAgent.run`）、`:148`；`evolve/task_eval.py` `extra_system` | `test_library.py::test_with_the_start_site_off_the_system_prompt_is_byte_identical_to_the_package_without_a_library`；`test_methods.py::test_a_package_without_m_tools_runs_and_records_as_before` |
| 经验库的流、快照轮、中止规则、学习曲线 | `bench.py:3138` `library_track`；`research/library.py:565` `stream_readout`、`:587` `bootstrap_halves`、`:607` `rank_correlation`、`:506` `distill_request`、`:532` `retry_note` | `test_v05_phases.py::test_the_library_stream_reads_train_only_distils_credits_and_gates_its_snapshot`、`test_a_library_entry_with_a_single_tasks_literal_is_refused_and_the_stream_continues`、`test_a_failed_first_encounter_is_retried_with_its_failed_checks_and_a_library_without_effect_is_not_gated`、`test_an_interrupted_stream_continues_where_it_stopped_and_refuses_another_w`、`test_the_library_phase_refuses_a_k_without_the_library` |
| 两个阶段的入口 | `bench.py:41` `PHASES`、`run_phase`；`cli.py:421` | 同上 |
| 报告的 v0.5 段 | `report.py:126` `v05_evidence`、`:983` `_v05_lines` | `test_v05_phases.py::test_the_library_stream_…`（报告部分）、`test_an_activity_without_v05_records_reports_as_before` |
| 真实检验的配置 | `examples/automationbench/claude-v5-support.toml` | `test_v05.py::test_the_two_support_arms_share_the_split_the_root_the_limits_and_differ_only_in_the_preset` |

2026-10-07 评审的 14 项（编号是评审的）：

| 项 | 代码 | 测试 |
|---|---|---|
| 4 两臂的根 $600、final 份额 $160、同日程，实花并列 | `claude-v4-support.toml`、`claude-v5-support.toml` 的头注与 `[budget]`；§5 | `test_v05.py::test_the_two_support_arms_share_…`；`test_bench_phase.py` 的根按配置（finance $450、support $600）|
| 4 learn 与固定轮付钱之前核预算（孪生 + 重拟筛选 + gate 的核验预留）| `bench.py:3064`；`research/fixed.py:191` | `test_v05_phases.py::test_learn_and_a_fixed_round_refuse_before_paying_what_the_budget_cannot_hold` |
| 5 中止规则只看 D，B 只报告 | `research/library.py:589` `snapshot_abort`；`bench.py:3366` | `test_v05_phases.py::test_the_snapshot_is_held_back_by_d_alone_and_b_is_only_reported` |
| 6 + 11 + 12 固定轮可续跑、不进 R 的段 | `research/fixed.py:26` `round_ended`、`:34` `next_ordinal`、`:49` `fixed_experiment_id`、`:96`（已结束的轮答回记录）、`:117`（读回已写的阶段）、`:191`、`:250`（续 Execution）、`:263` `_resume_execution`；`research/loop.py:374` `resume_unfinished(skip_fixed=)`、`:500`；`evolve/experience.py:124`；`bench.py:2893`（R 的段跳过固定实验）、`:3040`（learn 收尾没结束的重拟轮）、`:3374`（快照轮的序号进检查点）| `test_v05_phases.py::test_a_fixed_round_keeps_its_ordinal_and_experiment_id_until_it_ends`、`test_an_interrupted_snapshot_round_resumes_where_it_stopped_and_pays_nothing_twice`（在筛选、gate 首读、确认三处打断）、`test_a_snapshot_round_that_ended_answers_with_its_recorded_outcome`、`test_r_still_gets_its_three_rounds_when_a_fixed_rounds_gate_is_open`、`test_an_interrupted_refit_gate_is_finished_by_the_next_learn` |
| 7 带附注的重试不记功 | `bench.py:3301` | `test_v05_phases.py::test_a_failed_first_encounter_is_retried_…` |
| 8 H1 的配对（一臂没装东西时）与脚本 | `examples/automationbench/support_h1.py:41` `final_outcomes`、`:62` `spend`、`:74` `h1`、`:91` `support_h1`；§3；v5 配置的读法。（2026-10-08：对照停止，H1 没有算；脚本与测试移出 HEAD，tag `archive-2026-10-08` 上可读）| `tests/test_support_h1.py` 4 个（同上，已移出） |
| 10 流把近似重复题记进来源 | `bench.py:3216` | `test_v05_phases.py::test_the_stream_records_an_entrys_near_duplicate_train_tasks_among_its_sources` |
| 1 `broker_event` 的 `read_only` 与报告的 `m_tool_writes` | `agent/gateway.py:370`；`report.py:129` `m_tool_writes`、`:221`、`:1070` | `test_methods.py::test_an_m_tool_gets_a_read_only_broker_and_a_post_is_refused`；`test_v05_phases.py::test_the_learn_phase_…`（v5 臂写 0 次）|
| 2 合并回复是单个条目对象 | `research/library.py:527` | `test_v05_phases.py::test_a_merge_reply_of_one_bare_entry_is_read` |
| 3 + 9 首遇前提炼的检查点；模型错误是暂停 | `bench.py:3278`、`:3311`、`:3320` | `test_v05_phases.py::test_an_interruption_after_the_pre_attempt_distillation_does_not_pay_it_again`、`test_a_distillation_failure_pauses_the_stream_and_its_resume_pays_no_distillation_twice` |
| 13 v0.4 尝试的第一条系统提示金样 | `agent/runner.py:137` | `test_v05.py::test_a_v04_attempts_first_system_prompt_is_the_same_bytes_as_before_v05`（摘要用 0b9b475 的代码在同一夹具上算出）|
| 14 两臂一致性测试略去 `[rsi]` 的引用与 `[freeze.final]` | — | `test_v05.py::test_the_two_support_arms_share_…` |

**学习曲线的口径。** 曲线读的是 train 上每道题的首遇读数（库里来自这道题及其近似重复题的条目一律不检索，所以对这道题是留出的），对照是 W_base 自己的 train 读数；dev 只有快照轮 gate 的那一个读数，作为 n = 50 处的稀疏点（过程数字）。不另买 dev 快照（进化搜索 §5.8：25 题上差约 0.5 题，读数噪声 ±1 题）。

**突变检查（离线，在隔离副本里）。** 24 处关键改动逐一回退或打坏，每处对应的测试都失败；共享工作树前后快照一致。2026-10-07 评审的修正另做 22 处：首轮 2 处存活（固定轮“已结束就答回记录”这一分支、gate 续跑时用已冻结的实验而不是再冻结一次），各加一条断言后全部被抓到。

## 8. 未完成与开放项

- **库的检索阈值 `min_score = 0.2` 未标定**：S0 应先在已录下的 train 题文本上读出分数分布再定；它决定检索的命中与无关条目的伤害（进化搜索 §3.8 的未知参数之一）。
- **变体与分桶的学习只在开关之上做了最小实现**：变体按“两孪生都出手且变体不同”的题比较；单领域里多半学不到（方法库 §3.9），本版不期待它们离开默认。
- **跨领域端口、研究动作表跨活动累积、改进器层的冻结孪生检验**：推迟（§2）。
- **R 写的 agent 程序可以从磁盘读到整库**：默认 agent 只经宿主检索看到条目；一个 R 自写的 agent 若去读 `skills/library/` 文件，就绕过了留一。本版不加检查（v0.3/v0.4 的 12 个候选里 0 个 agent）；若出现 agent 候选，报告里核对其读文件的行为。
- **流的记录在两次追加之间崩溃（2026-10-07）**：`library_entry` 各行与 `library_stream` 行之间没有付费调用，但崩在这中间时，重跑会再写一遍条目行（以 `library_stream` 行是否存在判断“已写”）。不花钱、不改读数；报告（`report.py:169`）与 R 的 `inputs/library.json`（`research/library.py:551`）按 `entry_id` 读这些行，重写的同内容行互相覆盖，计数不变。
- **固定轮的 gate 在 Execution 记下实验引用之前被打断（2026-10-07）**：重跑用同一个实验 id、同样的输入再冻结一次再走 M5，与续跑等价；测试覆盖的是记下引用之后的三处打断。
- **等上限不是等花费（2026-10-07）**：v0.4 臂按自己的日程约花 $203，v0.5 臂约 $356 + final；H1 读的是“同上限、同日程下各自做到哪里”，两臂实花并列报告，不作等花费的主张。
- **“W\* 去库”“W\* 表全开”两臂**：放不进等上限的 final 份额，需用户另批（§4）；代码有 `methods.all_on_files` 与 `library.without_library` 可直接造这两个包。

