# v0.5 方法库：R 发明方法，train 奖励学会何处用（2026-10-06）

> **（2026-10-06 定稿说明）本文是 v0.5 的输入，保留作依据；v0.5 的规则以 [V0_5_PLAN.md](V0_5_PLAN.md) 为准**：采纳方法库（§2、§4）与适用性学习（§3），learn 读数只跑重放触发过的 train 题、重拟候选走自己的固定轮、参考方法只作夹具与消融、S1 finance 不跑（V0_5_PLAN §1–§2）。两者不一致处以 V0_5_PLAN 为准。
>
> 设计稿，待批准。用户方向（2026-10-06）：agent 可以学会“在某些任务上用某种分析方法、统计方法或搜索方法能更好地完成任务”，这也是 RSI 的能力，要仔细设计。连同此前的方向：不训练权重的 RL（选项加上从 train 奖励学出的策略，在 dev/test 上冻结）；R 发明选项，RL 学它们在哪里适用；agent 见过的题越多就越好。
> 先文档后执行：本文只写设计。实现与任何付费阶段都排在 v0.4 finance 活动（三轮加封存 final）结束并提交之后；在那之前 `ouroloop/` 下的代码一行不动。本文与同时撰写的 [V0_5_AGENT_RL.md](V0_5_AGENT_RL.md)（agent 内 RL）、[V0_5_EVOLVING_SEARCH.md](V0_5_EVOLVING_SEARCH.md)（框架层进化搜索）之间的接口写在 §5，三份文档对齐之后才写代码。
> 依据：158 道失败 train 题的逐题证据（§1；来自 v3 五领域基线、v4-finance W0、Marketing 全量工作区的 train 读数；dev 只用汇总，test 一行没读）；v0.4 finance 第 1 轮的台账（send_review、finish_deferral、source_memory）；[V0_4_PLAN](V0_4_PLAN.md)；[五领域诊断](../history/DIAGNOSIS_FIVE_DOMAIN_2026-10-06.md)；[RSI_PLAN §1](RSI_PLAN_2026-10-06.md) 的业界对照。

## 0. 结论先行

**做什么。** 把“方法”做成包里的一等选项。一个方法是只读、确定性的代码模块，以包工具的形式存在，并带两样东西：一张方法卡（何时用、输入、输出、何时失效、不用于什么），和一个触发器（选项理论里的起始集合）。方法只返回带出处的证据：它不写，也不替 agent 做决定。整个设计由三个循环组成，各有各的速度：

1. **R 造方法（每轮，LLM）。** R 从 train 失败里找出 agent 手工做、做错、又反复出现的事情，写成方法。这些事情包括搜索、表格分析、统计判断、选政策版本和发送前核对。每个方法都带测试。之后 R 再根据遥测去改进方法、收窄触发器或让方法退役。草稿链之外多一道免费的**触达检查**：方法必须在它声称要修的失败 train 题的真实世界里触发并跑通。
2. **固定代码学何处用（每个学习读数，统计）。** 每个“方法 × 触发种类 × 情境格”的动作（静默或启用）和变体（例如 as-of 日期取哪种估计）都从 train 的配对奖励里学出来。具体做法：
   - 只在 train 上探索：用两个互为对偶的孪生包，按题目文本的哈希分配动作，所以倾向值是精确的；
   - 按方法自己声明的断言类记功；
   - 用 Beta 后验，别的领域的计数以半权并入；
   - 学完冻结成一张贪心的表，放进包里，作为一个只改这张表的候选，照常筛选和 gate。

   dev 和 test 只见到冻结后的表。
3. **R 的研究方法从结果中学（改进器层）。** 固定代码把 R 自己历次方法候选的去向整理成表交给 R：触达、筛选、gate、安装、退役、遥测。每轮开头 R 可以修改自己的研究剧本，但每条教训都要引用台账行，由宿主核对。v0.5 在这一层只报过程数字，改进器层的主张留给冻结孪生检验（RSI_PLAN §4）。

**为什么要这样做（证据，§1）。**

- 158 道失败 train 题里，140 道带有方法可修的模式，其中 117 道已确认。按方法族分：核对 50、分析 40、搜索 38、版本判断 12、统计 6。
- 但方法在哪里适用，看题目是看不出来的。题目关键词和简单状态特征只能把失败率拉开大约 ±0.1。
- 同一个选项在不同题族里对错相反：系统日期对 wave_client_statement 是对的，对 ap_aging_report 是错的；一个自造的阈值过严，另一个过宽。
- 第 1 轮里那个宽泛的核对 hook send_review 修好 3 道，却在 6 道通过样本里弄坏 2 道。

所以“R 发明选项、train 奖励学何处用”不是点缀，而是让最大的一族（核对）由负转正的那一步。

另有两个反面发现，决定了不做什么：

- 分页出现 0 次：22,130 个 api_fetch 输出里没有一个 `has_more: true`。所以不做分页方法。
- 算术失误出现 0 次：每个错数都能追溯到输入。所以不做裸计算器。

**改动面。** 宿主上的改动很小：
- 不新增包种类，K 的组件不变；
- `m_` 前缀的包工具拿到只读 broker，并且每次调用最多 25 次基础调用；
- 多一种 mark；
- 新增模块 `research/methods.py`；
- bench 多一个只在 train 上跑的 `learn` 阶段；
- R 多几份输入（方法遥测、未覆盖的失败簇、研究动作表、方法 kit）和一份剧本。

**怎么测。** 分三步：

| 阶段 | 内容 | 花费 |
|---|---|---|
| S0 | 离线回放，决定去留 | $0 |
| S1 | finance 上的机制检验，接着 v0.4 的 W\* 继续。finance 的 test 会被 v0.4 的 final 用掉，所以这一步不读 test | 约 $290 |
| S2 | 在 support 上做主张，封存 final 有三臂：W0、W\*、W\*（表全开） | 约 $410 |

**预期。**
- 每个领域 3 轮装上 2～3 个方法，dev 上 +2～+5 题/25；test 上大约是它的一半；
- 选择表本身的增量是 +0～+2 题，主要来自避开退步。25 题上单独测不出来，所以只作为次要假设预注册；
- “学会了何处用”的判据是：至少 1 个格以后验 ≥ 0.8 离开默认，重拟候选过了 gate，并且交叉拟合的 train 价值随着见过的题数上升。

**保留、推迟、删：**

| 部件 | 决定 | 原因 |
|---|---|---|
| 方法契约（只读、证据、卡、触发器、调用上限、日志）| 保留，S1 | 增益靠它承载；只读才让 hook 内执行和跨领域借用成立 |
| 路由 kit（after_tool / before_tool / on_finish）| 保留，S1 | 只靠卡不够：175 次带非消息写入的尝试里只有 16 次读回，agent 很少自己做系统化的步骤 |
| 草稿链里的触达检查 | 保留，S1 | 免费挡住“写了却永不触发或跑不通”的方法 |
| 方法遥测、研究动作表交给 R | 保留，S1 | R 改进与退役方法的依据；免费 |
| R 的研究剧本（带引用的教训）| 保留，S1 第 2 轮起 | 改进器层最小的可核对版本，几乎不花钱 |
| 选择表的学习（learn 读数、按断言类记功、冻结、重拟候选）| 保留，第一次装上方法之后 | 让宽泛的核对不再伤通过题；前提是先有装上的方法 |
| 变体学习（DATE 锚点、STAT 规则、LEDGER 严格度）| 保留，受门槛约束 | 每格至少 5 道触发题才能离开默认；单领域多半学不到，要靠跨领域合并 |
| 第二批 top-two Thompson 读数 | 推迟 | 只有 ≥3 个格仍未决时才值 $50 |
| 按目标对象连线的记功 | 推迟 | 先按断言类记功；S0 回放显示太吵再做 |
| 跨领域端口 | S2 起（finance → support）| 第一次测迁移 |
| 分页方法、裸计算器、按题号的策略、卡片向量检索、dev/test 上在线学习、LLM 判官、会写的方法 | 删 | §10 |

## 1. 证据：train 失败里哪些能靠方法解决

### 1.1 读了什么

| 来源 | 内容 | 用途 |
|---|---|---|
| `workspace-claude-v3` | 五领域基线 train 读数（252 题，148 道失败）、78 条 R 笔记 | 断言行、轨迹、笔记 |
| `workspace-claude-v4-finance` | finance W0 train 读数（50 题，25 道失败）、25 条笔记；三次 dev W0 读数只取汇总 | 同上 |
| `workspace-claude-full` | Marketing 基线 train 读数（52 题，27 道失败）、45 条笔记；dev 只取汇总，test 行从未打开 | 同上 |

三份读数合起来，失败的 train 题共 158 道：finance 27、hr 38、marketing 33、sales 33、support 27。

每道题读了这些：
- 失败且未排除的断言；
- 有笔记的读笔记（约 103 道）；
- 叙述化的轨迹：助手文本、每个 api_fetch 的参数与正文、解码后的外发邮件；
- 没有笔记的约 55 道，以及笔记含糊的题，轨迹全读。

然后给每道题标一个或多个模式。**C** 表示在轨迹里或在引了调用的笔记里确认过；**P** 表示看到了信号但没有核实。

### 1.2 模式

题数是失败的 train 题（C/P）。领域列按 C+P 计。

| 模式 | 失败的样子 | 题数 C/P | 领域 | train 例子 | 方法族 |
|---|---|---|---|---|---|
| LIT | 决定对了，外发正文缺了必需的字面写法：'<N> <名词>' 计数、全名、源数字格式、算出的总额、否定通知里出现肯定词 | 20/9 | sales 8、marketing 8、support 7、hr 4、finance 2 | social_comment_response_queue：6 条升级都对，正文写成 '6 comments escalated'，检查 '6 escalat' 失败 | 核对 |
| LEDGER | 聚合前的行处理错：把噪声行算进去、悄悄丢掉真实行、无视行备注里的 skip/exclude、把测试记录算进去 | 16/7 | marketing 13、support 5、finance 4、sales 1 | budget_reallocation：23 行里有 'Channel-Noise-N'，邮件写 10 个渠道，丢了 TikTok | 分析 |
| DOC | 政策、先例或事实的搜索太窄或停得太早；Confluence、Notion、已发邮件从没查过 | 15/2 | marketing 6、sales 5、finance 4、hr 2 | project_profitability：Drive 搜 'guideline' 为空，Notion/Confluence 端点找到了却没调用 | 搜索 |
| EFFECT | 写入成功但效果不对，或预期的写入根本没做 | 13/6 | support 8、sales 6、hr 3、finance 2 | calendly_multi_stakeholder_scheduling：后两位被邀请人进了 event_guests，没读回就结束 | 核对 |
| SCRUB | 已正确排除或被取代的项，仍出现在外发正文里 | 13/1 | hr 6、finance 5、sales 2、marketing 1 | commission_calculation：最终数字旁边又写了只按档位算的 '9% = $4,500.00' | 核对 |
| VER | 用错了政策版本：旧的或最先找到的文档、把补充条款当成整套规则、忽略后来的解除 | 12/5 | marketing 10、finance 3、hr 2、sales 2 | xero_bill_entry：VP 后来说 'lift the hold'，被忽略 | 版本判断 |
| SRC | 401 之后试 1～3 个同类服务就放弃，从不列 Drive/Sheets 或另一个 CRM | 10/1 | hr 6、sales 4、finance 1 | comp_review_preparation：BambooHR 三次 401，一次 Drive 或 Sheets 调用都没有 | 搜索 |
| JOIN | 连接方向错，或没处理匹配不上的行 | 10/1 | finance 4、sales 3、support 3、hr 1 | zendesk_escalation_waterfall：tkt_605 不在历史表里，遍历了历史表 | 分析 |
| VOCAB | 分类写入用了自造的状态或标签串 | 10/1 | hr 5、support 3、sales 2、finance 1 | xero_bank_reconcile：写 'Matched'，政策规则 3 用的是 'Reconciled'，4 条行检查全失败 | 核对 |
| API | 能力/端点搜索太窄，用了最近的那个端点 | 9/3 | sales 5、support 3、marketing 2、finance 1、hr 1 | sheets_multi_channel_campaign_router：发了私信，政策要的是连接请求 | 搜索 |
| DATE | 业务上的“今天”和相对日期窗口取错；沙盒时钟（2026-10）不是业务日期 | 9/1 | hr 4、finance 3、sales 2、support 1 | ap_aging_report：按 2026-10-06 计账龄，6 张单全进 90+ | 分析 |
| STAT | 对含糊标准自造阈值、指标定义随意 | 6/1 | sales 2、support 2、marketing 2、finance 1 | landing_page_alert 的阈值过严；event_followup_outreach 的过宽 | 统计 |
| TAB | 工作簿没逐页列：不带页名读数值只拿到第一页、页名靠猜、单元格备注没取 | 5/0 | finance 2、其余各 1 | intercompany_balance_recon：从汇总页对账，各对手方的页从没列出 | 搜索 |
| DUP | 近似重复和键匹配：冲突的重复键被悄悄处理、在该精确匹配处用了模糊匹配 | 5/0 | sales 2、其余各 1 | invoice_reconciliation：同一发票号两种金额，没发提醒 | 分析 |
| CALC | 计算没按规程：年度估计没年化、为配平硬加一笔 | 2/0 | finance 2 | quarterly_tax_estimate；fiscal_year_rollover 加了 $15,000 的补差 | 分析 |
| JUDG | 只是判断或安全问题，确定性方法修不了 | 仅此一项的 18 道 | hr 12、finance 5、marketing 5、sales 3、support 2 | candidate_rejection_followup：照着附加请求，在每封拒信里抄送 CEO | 不是方法 |

### 1.3 按方法族汇总

| 族 | 已确认的题（可重叠）| 模式 |
|---|---|---|
| 核对 | 50 | LIT、SCRUB、EFFECT、VOCAB |
| 分析 | 40 | LEDGER、JOIN、DUP、DATE、CALC |
| 搜索 | 38 | SRC、DOC、TAB、API |
| 版本判断 | 12 | VER |
| 统计 | 6 | STAT |

用户说的“分析、统计、搜索”三类都在里面。除此之外还有两类：核对（数量最多）和版本判断（它是 SCRUB 的上游）。

### 1.4 两个反面发现

- **分页：0 例。** train 与 dev 轨迹里共 22,130 个 api_fetch 输出，没有一个 `has_more: true`，也没有非空的游标；770 个游标字段全是空的。1,695 次 Gmail 列表调用里只有 2 次被截断。要补的是搜索的**广度**（SRC、DOC、TAB、API），不是翻页。
- **算术失误：0 例。** 每个错数都能追溯到输入（行错、来源错、as-of 日期错、政策过时），或者追溯到规程与定义的选择（3 道）。光有计算器什么也修不了，价值在“选输入”的方法上。这一点和 PAL / PoT 的场景相反：那里卡在“算”，这里卡在“取什么来算”。

### 1.5 适用处为什么必须学

1. **题目看不出来。** 日期词、“最新/当前”词、对账/匹配词、存在噪声行、存在备注列、读过 ≥2 封政策邮件：这些特征下的失败率都在基率 ±0.1 之内。
2. **触发本身也不说明该不该出手。** 下表用的是与触发种类同形的行为检测器（单次读数，一题一次尝试）。宽的触发在一个领域里就能碰到 ≥5 道题，但触发时的失败率接近基率。所以真正要学的是“触发之后出手是否有利”，不是“触发了就出手”。

| 观测（触发种类的雏形）| v3 五领域 train：触发的尝试 / 252，触发时失败率（基率 0.59）| finance W0 train：触发 / 50，失败率（基率 0.50）|
|---|---|---|
| 出现 401 | 72，0.67 | 21，0.57 |
| 有非消息写入但没读回 | 159，0.56 | 20，0.45 |
| 读了表格数值却没读元数据（不知道有几页）| 70，0.56 | 8，0.50 |
| 拿系统日期当“今天” | 17，0.65 | 3，1.00 |

3. **对的选项随题族而变。**
   - 系统日期：wave_client_statement 用它通过了，ap_aging_report 用它失败了。qb_void_stale_invoices 用消息日期通过，hr 有几道题用邮箱日期通过。
   - 阈值：landing_page_alert 的自造阈值过严，event_followup_outreach 的过宽。
   - 所以 DATE 和 STAT 是“在几个变体里选一个”的问题，不是“开或关”的问题。
4. **宽泛的核对会伤人。** v0.4 finance 第 1 轮的筛选：
   - send_review（每次尝试第一次外发前的核对清单）修 3、在通过样本里坏 2；
   - finish_deferral 修 2、坏 1；
   - source_memory（一条文字规则，覆盖 TAB、SRC、DATE、VER）修 9、坏 1，之后 dev 上 34.7% → 46.0%（合并三次读数，+11.3）。

   这些模式是真的、也修得动；但最大的一族（核对）一旦处处出手，净值会被退步吃掉。

### 1.6 一题多模式

140 道里有 91 道只带一个模式，45 道带两个，4 道带三个。最常见的组合：

| 组合 | 题数 |
|---|---|
| API + EFFECT | 4 |
| LEDGER + SCRUB | 4 |
| DOC + VER | 3 |
| LEDGER + VER | 3 |
| LEDGER + LIT | 3 |
| LIT + VER | 3 |

这推出两条设计：
- 方法之间要共享一份尝试内的**日志**：分析方法排除了什么，核对方法就查什么；
- 记功要细到**断言类**：只修好一半的题，在题级奖励里看不见。

### 1.7 finance 与 support

- **finance。** 25 道失败的 W0 train 题里有 20 道带方法模式：SCRUB 5、JOIN 4、DOC 4、DATE 3、VER 3、LEDGER 2C+2P、LIT 2、TAB 2、EFFECT 2、CALC 2，VOCAB、DUP、API、SRC、STAT 各 1。
  - 只涉及判断的 5 道：bad_debt_writeoff、capex_approval、financial_ratio_analysis、payment_fraud_detection、slack_receipt_capture。
  - 三次 W0 读数的 dev 失败断言（只看汇总）：数值错 42、没写到目标 39、泄漏已排除值 16、过度行动 14。这是 LEDGER、JOIN、DATE、VER、SCRUB 的地盘。
  - 已装上的 source_memory 已经部分覆盖了 TAB、SRC、DATE、VER。
- **support。** v3 单次读数里有 27 道失败的 train 题，其中 26 道带方法模式（19 道已确认）：LIT 4C+3P、EFFECT 3C+5P、LEDGER 3C+2P、JOIN 3、API 2C+1P、STAT 2、VOCAB 2C+1P，TAB、DATE、DUP 各 1。support 偏核对，正是选择表最要紧的地方。

### 1.8 注意

- 模式是人工标的；P 没有核实；各类计数有重叠。
- v3 与 Marketing 的 train 每题只有一次读数；finance 三次 dev 读数里有 1 题不稳，所以少数单次失败可能是噪声。
- 隐藏的判分约定（正确的串在题目、政策和现有数据里都看不到）不算作方法失败。
- **本节的模式表不进 R 的输入。** 它只用于本设计，以及 §7.5 的事先登记预测（“发现召回”）。原因：方法要由 R 从它自己读到的失败里发明出来；把这张表交给 R，发明就成了我们的，自主性账目也就不成立了（RSI_PLAN §4.3 ①）。

## 2. 方法库：方法作为包工具

### 2.1 什么是方法

方法就是选项（Sutton、Precup、Singh 1999）：
- 起始集合 = 它的触发器；
- 内部策略 = 一段确定性代码；
- 终止 = 一次调用后返回。

“选项之上的策略”就是 §3 的选择表。R 是造工具的人，actor 是用工具的人，选择表是调度者。这对应 LATM 的分工，只是调度者从奖励里学出来，而不是靠提示。方法只交证据、由 actor 拍板，这对应 CREATOR 把工具创建与决策、执行分开的做法。

### 2.2 契约

每条后面写了原因。

1. **名字以 `m_` 开头的包工具拿到只读 broker**：POST/PUT/PATCH/DELETE 由宿主拒绝。
   - 原因：方法只取证据，写入留给 actor。这一条让三件事成立：(a) 路由可以在 hook 里直接运行方法（hook 本来就是只读的）；(b) 借给别的领域的方法不可能直接造成过度写入（诊断 RC1：sales/hr 的退步约一半是“不得做”断言）；(c) 方法可以在 train 的夹具世界里离线跑，不留下痕迹。
2. **只返回证据。** 每个值带出处（来源、记录、字段）；分不了类的放进显式的 `unclassified`。
   - 原因：一个自信但错了的方法比没有方法更误导人。噪声行出现在 252 条 v3 train 轨迹中的 102 条里，而这些题的失败率接近基率，说明过度过滤是真实的风险。
3. **确定性，不调用模型。** 方法拿到的上下文只有 `view` 和 `call_tool`（现行的 `_PackageContext`）。
   - 原因：能在录下的事件上原样回放，能在 hook 里跑，能离线评估。成本也可预期。
4. **每次调用至多 25 次基础调用**，由宿主计数，超了回错误；输出不超过 `max_output_chars`（20,000）。
   - 原因：一次尝试的工具预算是 `max_tool_calls = 200`。一个方法用一次工具调用替代 3～6 个 act 回合的手工搜索，但不能吃掉整个预算。
5. **参数不对不抛异常**，回 `is_error` 和用法说明。
   - 原因：草稿冒烟会用最小参数调用每个工具；在运行时抛异常算基础设施失败。
6. **日志。** 方法把排除的实体、被取代的值、算出来的必需记号（总额、计数）、计划中的写入效果追加到 `/tmp/m_journal.json`。路由在 after_tool 把每次写入（目标、字段）也记进去。
   - 原因：LEDGER→SCRUB、VER→SCRUB、API→EFFECT 都要求核对方法知道分析方法做了什么决定（§1.6）。
   - 为什么用文件：worker 按路径加载模块，同一文件可能被加载两次，模块级状态靠不住。每次尝试一个容器，`/tmp` 是 64 MB 的 tmpfs，日志不会活过这次尝试（§8 有测试）。
7. **触发器是纯函数**：输入是事件 `{tool, args, result[:4000], is_error}` 和 `view`（含任务文本与最近 20 个 broker 事件），不发任何调用；返回 `None` 或 `{kind, args, context}`。
   - 原因：它必须能在录下的 train 事件上原样重放，触达检查（§2.7）和离线评估（§3.8）都靠这一点；也让两个孪生包里“是否触发”可以比。
8. **只写一般规则。** 词表里的词（噪声模式、备注指令词、同义词）每个都要在 ≥2 道 train 题里出现；不按题目或实体分支。
   - 原因：这就是 v0.4 字面量检查的同一条规则（只出现在一道 train 题里的值一律拒绝）。方法代码、方法测试、`METHODS` 字面量都在包里，检查自动覆盖。
9. **回放时触发器要在 ≥2 道不同的 train 题上触发**（全部 train 尝试，失败的与通过的都算）。
   - 原因：只在一道题上触发的方法是换了形式的按题策略。

### 2.3 方法卡与声明

卡就是工具的 `description`，上限 1,024 字符（`MAX_TOOL_DESCRIPTION`），字段固定为 USE WHEN / INPUT / OUTPUT / FAILS WHEN / NOT FOR。卡是给 actor 看的运行时文字，所以用英文。例子：

```
[method] USE WHEN you will count, total or act on rows of a table that may hold filler or test rows, per-row
notes, or rows outside a date window. INPUT source (sheet id + tab, or a records URL), key column, amount columns,
window. OUTPUT one line per row: include|exclude + reason; counts and totals from included rows only; an
`unclassified` list. FAILS WHEN notes use words outside its directive list (see unclassified). NOT FOR single-record
lookups. Read-only evidence: you still decide.
```

每个方法模块写一个字面量 `METHODS`，宿主用 `ast` 解析、从不 import：

```
METHODS = {"m_row_ledger": {"family": "analysis", "trigger": "row_ledger_trigger", "kinds": ["table_read"],
           "on": "recommend", "footprint": ["value", "not_contains"], "split": "noise_rows",
           "variants": ["notes_only", "notes_and_noise", "notes_noise_empty"], "default_variant": "notes_and_noise"}}
```

各字段的含义：
- `on`：启用时的动作，取值见 §2.5；
- `footprint`：这个方法应当推动的断言类，共五类：`body_contains`、`not_contains`、`value`、`exists`、`categorical`，由 `adapters.automationbench` 从断言类型映射；
- `split`：R 提议的一个情境特征，至多一个；
- `variants`：至多 4 个。

这些信息写在方法模块里，声明格式就不必加新键。R 的声明仍用现有字段：carrier `tool`，site 写 `m_` 工具名，trigger_condition 写触发种类与条件，再加 predicted_affected。

### 2.4 预期 R 会写出的方法（证据预测，不交给 R）

下表是 §1 推出来的预测，用于 §7.5 的“发现召回”，**不是**给 R 的清单。

| 方法 | 族 | 模式（C）| 载体与触发种类 | 决策类型 | 优先级 |
|---|---|---|---|---|---|
| m_outbound_lint | 核对 | LIT 20、SCRUB 13、VOCAB 10 | before_tool：外发且日志非空，或分类写入 | 开关（启用 = 拦一次）| finance、support 的第一批 |
| m_effect_audit | 核对 | EFFECT 13 | on_finish：有非消息写入而没读回 | 开关（启用 = 追问一次）| support 第一批 |
| m_vocab_for | 核对 | VOCAB 10 | after_tool：即将写分类字段 | 开关 | support |
| m_row_ledger | 分析 | LEDGER 16 | after_tool：读到 ≥8 行的表 | 开关 + 变体（严格度）| finance 第一批 |
| m_join_report | 分析 | JOIN 10 | after_tool：两个来源的记录集共享键 | 开关 | finance 第一批 |
| m_dupe_scan | 分析 | DUP 5 | 同 JOIN | 开关 | 合并到 JOIN |
| m_as_of / m_date_calc | 分析 | DATE 9 | after_tool：任务含相对日期词，且数据日期远离系统时钟 | 变体（5 种估计）| 跨领域合并后才学得动 |
| m_threshold | 统计 | STAT 6 | after_tool：含糊标准 + 数值列 | 变体（给定的 / 最大间隙 / 中位数 ± k·MAD / 标准定义）| 只在合并时学 |
| m_policy_timeline | 版本判断 | VER 12 | after_tool：同一主题命中 ≥2 份文档 | 开关 + 变体（替换 / 合并补充）| marketing、finance |
| m_source_sweep | 搜索 | SRC 10 | after_tool：http 401 | 开关（启用 = 注入摘要）| hr、sales |
| m_doc_search | 搜索 | DOC 15 | after_tool：文档搜索为空 | 开关 | finance 次一批 |
| m_read_workbook | 搜索 | TAB 5 | after_tool：读数值时没带页名 | 开关 | finance（source_memory 已部分覆盖）|
| m_capability_search | 搜索 | API 9 | after_tool：调用了 api_search | 开关 | sales、support |
| （finance_calc）| 分析 | CALC 2 | — | — | 推迟：只有 finance 的 2 道 |

### 2.5 路由与选择表（kit 文件，R 不能改）

**`middlewares/m_router.py`** 是框架的 kit 文件，字节须与 kit 一致。它注册在三处：

- **after_tool。** 每次基础调用之后，依次运行每个方法的触发器。第一次触发时做决策，并打一个 `method_decision` 标记；之后同一（方法，种类）沿用这次的动作。
  - `recommend`：一行反馈，点名方法和建议的参数。
  - `inject`：路由自己用 hook 的只读上下文运行方法，给出不超过 1,800 字符的摘要，并告诉 agent 调用 `m_x` 可拿到全文。hook 消息本来就截在 2,000 字符，after_tool 反馈合起来截在 4,000。
- **before_tool。** 拦外发（Gmail send、Slack chat.postMessage、备注写入）和分类写入。启用中的核对方法检查正文：
  - 日志里已排除或已被取代的值（SCRUB）；
  - 日志里的必需记号（LIT：算出来的总额、'<N> <名词>' 计数、记录里的全名）；
  - 分类串是否在 vocab_for 给出的集合里（VOCAB）。

  `block_once` 的规则：同一正文哈希只拦第一次，原样重发就放行；每次尝试至多拦 2 次。
- **on_finish。** `pushback`：有非消息写入而没读回时，运行 effect_audit，把差异作为一次追问。这次追问计入现有的 `MAX_PUSHBACKS = 2`。

上限：每个方法每次尝试至多 1 次 recommend 或 inject；每次尝试合计至多 3 次 recommend、2 次 inject、2 次拦截。原因：send_review 的教训是，拦得太多会伤通过题；上限把一个格学错的代价封住。

**核对类的触发要求日志非空。** 外发 lint 只在日志里有排除项、被取代的值或必需记号时才触发，分类写入除外。原因：send_review 在每次尝试的第一次外发都出手，伤了 2/6 通过题；日志为空时 lint 没有一个“依据数据”的东西可查，出手只会制造噪声。这个条件把核对绑在“分析方法真的做过决定”的那些题上。

**`middlewares/m_table.py`** 只放一个字面量，例子：

```
TABLE = {"mode": "greedy",
         "cells": {"m_row_ledger|table_read|noise_rows=1": {"action": "on", "variant": "notes_and_noise"},
                   "m_row_ledger|table_read|noise_rows=0": {"action": "silent"},
                   "m_as_of|date_task|gap_gt_14": {"action": "on", "variant": "newest_seeded_message"},
                   "m_outbound_lint|outbound_send|*": {"action": "on"}}}
```

规则：
- 宿主和路由都用 `ast.literal_eval` 读它，从不 import。
- 只有固定代码写它：新方法的格取 R 声明的 `on`；重拟候选写学到的格（§3）。
- 探索孪生的表 `mode = "explore"`，带一个盐值和孪生标签 A/B（§3.3）。
- 动作只有两种：`silent`，或这个方法声明的 `on`（`recommend` / `inject` / `block_once` / `pushback` 之一）。
- 静默的格里卡片照样在工具列表上，actor 仍可自己调用；表学的是**路由**该不该出手。方法该不该在包里，由加入它的那个候选的筛选与 gate 决定。

**kit 补齐。** R 的候选第一次加入 `m_` 工具时，固定代码在草稿检查之前补上三样：`m_router.py`（kit 的字节）、`m_table.py`（这个候选的格）、manifest 里的三个路由 hook 条目。若 R 改过这两个文件，它的改动会被替换，并在检查输出里说明。原因：路由是策略的执行器，表是从奖励学出来的统计量；如果 R 能写，学到的部分就会被 R 的叙述左右。

### 2.6 包的布局

方法相关文件最多 16 个，在 K 允许的 24 个文件、256 KiB 之内：

| 文件 | 谁写 |
|---|---|
| `tools/_mlib.py`：取数、Sheets/Drive/Gmail/Slack 解析、base64 邮件解码、日志读写的模板 | kit 给模板，R 可以扩展 |
| `tools/m_search.py`、`m_tables.py`、`m_time.py`、`m_stats.py`、`m_policy.py`、`m_verify.py`（按族分组，一个文件多个入口）| R |
| `middlewares/m_router.py` | kit，字节固定 |
| `middlewares/m_table.py` | 固定代码（学习器）|
| `tests/test_m_<族>.py` | R |
| manifest：`m_*` 工具条目，加三个 hook 条目 `m_router_after` / `m_router_before` / `m_router_finish` | R 写工具条目，kit 补 hook 条目 |

按族分组的原因：24 个文件的上限会随方法库变大而收紧；共享的解析与日志逻辑写一遍就够了。

### 2.7 测试

| 层 | 跑在哪 | 内容 | 给 R 什么 |
|---|---|---|---|
| R 的测试 | 草稿链里的 package_test，worker 内 | R 自造的合成数据：噪声行、备注、多页工作簿、401、重复键 | 失败输出 |
| 冒烟与探针 | 草稿链（现有）| 每个工具用最小参数调用一次；hook 在夹具事件上跑一遍 | 失败输出 |
| kit 检查 | 草稿链（新）| 路由字节、表能解析、格指向存在的 `m_` 工具与声明过的种类、`METHODS` 能解析 | 违例清单 |
| 触达检查 | 草稿链（新），免费 | 见下 | 每题计数 |

**触达检查的做法。** 对候选 `predicted_affected` 里每道失败 train 题，取它录下的尝试，把工具事件依次喂给新增或改过的方法的触发器。第一次触发时，在这道 train 题的一个新世界里（`suite.episode(task_id)`，通过只读的夹具 broker）运行一次这个方法。

报告给 R 的只有每题的计数：是否触发、是否跑完、有无错误、输出长度、题目的计分断言值里有几个出现在输出中（只给个数，不给是哪些值）。

**底线：** 在 ≥1 道声称的失败 train 题上触发并且无错跑完。达不到底线，就作为检查错误回给 R，消耗一次修订。

**不当目标的东西：** 出现了几个断言值从不参与排序和准入。原因：触达是一个便宜的过滤器，不是评估器；一旦当作目标去优化，方法就会向 train 的答案靠（AlphaEvolve 一类工作被批评的正是可以被钻空子的评估器）。

**为什么 R 的测试只用合成数据：** 真实 train 数据里只属于一道题的值会被字面量检查拒掉，而且测试文件也在包里。真实世界只由宿主的触达检查来跑，结果只以计数的形式返回。

### 2.8 一次尝试里怎么运行

1. `attempt_view` 把启用的 `m_*` 工具和三个路由 hook 放进 `view.package`。默认 agent 把它们的卡附在基础工具之后。卡约增加 2～3k 输入 token，大多是缓存读。
2. 模型调用 `m_row_ledger` 时，`_execute` 在 worker 里加载 `tools/m_tables.py:row_ledger`，上下文是只读的 `_PackageContext`，带 25 次上限。它发出的 GET 照常成为 broker 事件，计入这次尝试的 200 次。返回的 JSON 截到 20,000 字符；`ctx.mark("package_tool")` 记下这次调用。
3. 每次基础调用之后，`m_router.after` 跑所有触发器。某一格是 `on` 时给推荐，或者注入摘要（§2.5）。
4. 外发之前，`m_router.before` 解码正文并对照日志做 lint；第一次违例拦下这次调用和这一回合剩下的调用，模型改了再发。
5. 结束时，`m_router.finish` 查 `pushback` 格；若有，做一次有界的追问。
6. 包里没有任何东西能区分 train、dev 或 test：表是常量数据；探索只以不同包的形式存在，并且只在 train 上由宿主运行。

成本：方法用一次工具调用替代多轮手工搜索（例如 source_sweep 在一次调用里做 8～15 次 GET，而 actor 手工要 3～6 个 act 回合），省下的 act 调用和重读上下文可以抵消卡片的 token。预期每次尝试的成本在持平到 −10% 之间；gate 现有的成本项兜底。

### 2.9 跨领域借用为什么更安全

“安全”在这里是**比借文字规则更安全**，不是没有风险：

1. **只读。** 借来的方法不会写入；最常见的跨领域伤害——在该克制的领域里多写多发——没有直接通道。
2. **有条件。** 方法只在触发器触发、且这一格为 `on` 时出手；文字规则则一直在上下文里。v0.3 的 rulesmem 在 train 样本里 0/5 退步，到了 dev 退步 10 题（sales 占 6），因为“多读多做”的文字在每道题上都起作用。
3. **给证据，不给决定。** 输出带出处和 `unclassified`，决定仍在 actor 手里。
4. **付钱之前就能量。** 在接收领域里，触达检查会在它的 train 世界里免费回放；先验来自来源领域的计数，以半权并入；接收领域照样有自己的筛选与 gate。
5. **模式确实会复现。** 下表是每个模式出现失败 train 题的领域数（共 5 个领域，C 或 P）：

| 模式 | LIT | API | DOC | TAB | DATE | LEDGER | JOIN | DUP | STAT | VER | EFFECT | VOCAB | SCRUB | SRC | CALC |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 领域数 | 5 | 5 | 4 | 4 | 4 | 4 | 4 | 4 | 4 | 4 | 4 | 4 | 4 | 3 | 1 |

**仍然存在的风险：** 推荐文字和错的输出依然会误导人。所以：
- 端口进入接收领域时，格的动作取来源领域学到的动作，默认是 `on`；在接收领域的第一个 learn 读数之前，只靠接收领域的筛选来挡伤害；
- 来源领域的计数只当先验，权重 0.5（§3.5）；
- 端口由固定代码提供（文件 + 卡 + 合并计数，没有任务 id），由 R 决定是否拿来，作为普通候选（§4.3）。

## 3. 学会何时用哪个方法：方法选择上的 RL

### 3.1 问题的形式

一次尝试里，每个（方法，触发种类）第一次触发就是一个**决策点**。决策有两种：

- **开关**：动作是 `silent` 或 `on`；
- **变体**：在 2～4 个子程序里选一个，例如 as-of 日期的估计方式。

所谓策略，就是一张表：π(动作 | 方法, 种类, 情境格)。奖励来自这次尝试的 train 判分。

这是批量的情境老虎机：一个批次里探索，然后冻结成贪心表交付；也就是选项层面的单步 RL。权重不训练。

为什么不做完整的序列 RL：一次尝试里只有几个决策点，奖励在最后才有；决策之间的耦合用组合决策（§3.4）和断言类记功来处理，就已经够用。要学跨步的价值函数，那是 V0_5_AGENT_RL 的范围（§5）。

对照的做法：
- 按实例选算法（Rice 1976；SATzilla 用探测特征而不是实例描述）；
- 情境老虎机（LinUCB；Thompson 抽样）；
- 离线策略评估（双稳健估计，Dudík 等 2011）。

### 3.2 情境特征

格的键是：方法 | 触发种类 | 至多一个分桶特征。

- **触发种类**由方法声明，常见的种类由 `_mlib` 提供检测器：`http_401`、`tabless_read`、`table_read`、`doc_miss`、`capability_search`、`date_task`、`vague_criterion`、`policy_hits`、`categorical_write`、`outbound_send`、`write_no_readback`。
- **分桶特征**由 R 为每个方法提议一个，来自方法自报的情境，例如 `noise_rows`、`n_tabs`、`clock_gap_days`、`journal_size`、`unmatched_count`。固定代码把它切成至多两个桶。
- **领域**不进键，而是通过先验合并进来（§3.5）。

原因：
- 题目关键词预测不了失败（±0.1）；运行时观测到的东西才是 SATzilla 式的探测特征。
- 格越多，每格的题越少：50 道 train 题撑不起两个以上的特征。
- 桶名是通用名（如 `gap_gt_14`），能过字面量检查。

### 3.3 探索：只在 train，对偶孪生

**learn 读数。** 固定代码给当前 W\* 造两个孪生包 A 和 B。它们和 W\* 只差 `m_table.py`：`mode = "explore"`，带一个盐值 s（由 (`order_seed`, 阶段) 定）和孪生标签。

**分配规则。**
- 开关：对每个决策 d，路由算 u = H(s, 任务文本, d) ∈ {0, 1}。A 取 u，B 取 1 − u。
- 变体：A 取 v = H mod k，B 取 A 之外的另一个变体，也由哈希决定。
- 每道 train 题在 A、B 下各跑一次，共 100 次尝试。每个开关在每道题上恰好开一次、关一次（foldover）；不同决策的符号相互独立。

原因：
- 包看不到题号，但看得到任务文本；按文本哈希做分配，结果确定、可复现，而且倾向值精确：开关 0.5，变体 1/k。
- 对偶配对把题与题之间的差异消掉。v0.4 finance 三次读数里只有 1/25 题不稳，所以不一致的配对大多是真效应。
- 所有格都随机化，包括看起来已经定了的格：train 上的奖励不是目标，随机化的代价只是 train 分数，换来的是没有偏差的估计。

**孪生只在 train 上跑。** `--phase learn` 拒绝 dev 和 test；dev/test 的运行在装配时拒绝 `mode != "greedy"` 的表（§3.7）。

**“是否触发”两边都看得见。** 触发器是纯函数，在 A 和 B 里都会运行并打标记，所以对比可以限定在“至少一个孪生里触发了”的题上。两个孪生都没触发的题，A 和 B 的行为相同，不带任何信息。

**变体的等价共享。** 方法每次都算出全部变体，并标出所选的那个。两个变体给出同一输出的题，算作两者共享；只有输出不同的题才进比较。

**探索的来源与功能分工。**
- 只有 learn 读数进拟合。
- 筛选臂（候选对父版本）和装上后的重跑也是配对数据，但卡片和路由动作在里面是一起变的，所以只进遥测给 R 看，不进拟合。这样表的估计是干净的。
- 第二批读数（推迟）：只有在 ≥3 个格有 ≥5 道触发题却仍未决时才跑。分配概率用 top-two Thompson（Russo 2016），写进孪生的表；它的目标是找出最好的臂，而不是压低 train 上的遗憾。

### 3.4 奖励与功劳分配

每道题、每个孪生记下三样：
- 题级奖励 r_task ∈ {0, 1}；
- 断言类得分 r_fp：在这个方法声明的 `footprint` 类里，未排除断言的通过比例；
- “不得做”记账：一条 `_not_` 断言在 `on` 这边失败、在 `silent` 这边通过，就记在这个方法这一格上。

对格 c 里触发过的每道题，比较 `on` 与 `silent` 两边：
- k_c 是 r_fp 上升的题数，m_c 是 r_fp 下降的题数，持平的题丢掉；
- 题级的修复数和退步数另外计。

为什么按断言类记功：
- 140 道里有 49 道是一题多模式，只修好一半的题在题级奖励里看不出来。
- 离线模拟里（假设同 §3.9，低伤害设定），按断言记功比按题记功更省样本：finance 100 次尝试时 10.9 对 8.6（每 100 道新题的净题数，oracle 15.8）；合并后 SCRUB 的决策准确率 0.90 对 0.67。
- S2 用的是按**类**记功，不连到具体目标；目标级连线推迟。原因：按类不需要把断言和写入一一配对，实现量小。S0 会在录下的数据上量一下按类记功的误记率。

**组合决策。** 如果一个方法依赖另一个方法的日志（如 SCRUB lint 依赖 LEDGER），R 可以在 `METHODS` 里写 `couples_with`，两者作为一个组合决策来随机化。原因：互补的方法单独估计都会被低估。

**成本守卫。** 某格要转为 `on`，触发题的平均尝试成本不能上升超过 25%。原因：gate 的成本项只看整体，守卫能挡住单格的膨胀。

### 3.5 更新与冻结规则

**后验。** 每格 p_c ~ Beta(1 + k_c + ½·k′_c, 1 + m_c + ½·m′_c)。k′、m′ 是同一方法在别的领域的计数，以及本领域在上次安装之前（W\* 的底子不同）的计数，都取半权。原因：只有两三个领域时估不了层级超先验，固定半权简单而保守；底子变了，旧证据就该打折扣。

**开关格的规则。** 一格要离开当前动作，所有条件都要满足：
- **转 on**：触发题 n_c ≥ 5；P(p_c > ½) ≥ 0.8；题级的修复 − 退步 ≥ 0；“不得做”记账 ≤ 1；成本守卫成立。
- **转 silent**：n_c ≥ 5，且 P(p_c < ½) ≥ 0.8；或者“不得做”记账 ≥ 2。
- 其余情况保持当前动作。新方法的初始动作是 R 声明的 `on`。

**变体格的规则。** 变体 v 要取代当前变体，须满足：在两者输出不同的题里 n ≥ 5，P(v 更好) ≥ 0.8，加上同样的守卫。

**分桶。** 只用 R 为这个方法提议的那一个特征，阈值取在粗网格上（天数取 7 的倍数，计数取整）。只有两边各 ≥5 道触发题，并且两边按规则得出的动作不同，才保留这次切分。

为什么取这些数：
- **5 道题**是匿名性的底线：一格不可能编码某一道题。它比字面量检查的 ≥2 更严，因为格直接控制行为。
- **0.8 加上题级不为负**，是在 25 道题的 dev 噪声（单次读数 ±7～9 个点）之下仍站得住的门槛。

### 3.6 重拟候选：改表也要过筛选和 gate

只要拟合出的表和已装的表不同，固定代码就在下一轮加一个第 k+1 个候选：
- carrier `hook`，site `after_tool`，机制写“refit method table from paired train rewards”；
- `predicted_affected` 是格变了的那些触发题；
- 文件上只改 `m_table.py`，再把满足两个条件的 `m_` 工具设为 `enabled: false`：所有格都已静默，并且 actor 在卡片可见时自发调用的比例 < 5%（TroVE 式修剪；工具留在包里，重新启用要另做一个候选）。

它和其他候选一样走筛选（格变了的题 + 6 道通过样本）、gate、确认读数。原因：学到的表也是一次改动，它在 dev 上有没有用要单独量；拟合本身不能白拿 dev 分数。

### 3.7 冻结的收据

1. `bench` 在装配 dev/test 运行时，若包的 `m_table.py` 不是 `mode = "greedy"`，或有任何一格不是单一动作，就拒绝运行，不花一分钱。
2. dev/test 运行结束后，所有 `method_decision` 标记的倾向值都必须是 1.0；否则这次读数作废。
3. 探索孪生的包引用只出现在 `learn` 阶段的运行记录里。

这三条保证 dev/test 用的是冻结的贪心策略。它们靠构造和检查成立，而不是靠 agent 守规矩。

### 3.8 学习曲线：见得越多越好，体现在三处

1. **表。** 用 5 折交叉拟合：在部分折上拟合 π_n，用 learn 读数的精确倾向值，在留出折上做双稳健估计，得到 V̂(π_n)。取 n = 已记录 train 题的 25%、50%、100% 各报一次。另报每轮仍停在默认动作的格数。全部免费。
2. **方法库。** 每轮报：已装方法数、退役数，以及“已装方法触发且出手了”的失败 train 题占全部失败 train 题的比例。
3. **迁移。** 在同一份 support 记录上做一次免费的反事实：带 finance 先验和不带先验，各需要多少道题才能让共享格达到 0.8。

预注册的曲线形状：V̂(100%) − V̂(25%) > 0，自助法 P ≥ 0.8；support 带先验时所需的题数少于冷启动。

### 3.9 样本量的现实

**单领域。** 宽的触发能在一个领域里碰到 ≥5 道题，见 §1.5 的表：finance 50 道 train 里，401 碰到 21 道，写后不读 20 道，不带页名的读 8 道。所以每个领域大致能学到这些：
- 每个方法的开关；
- 最常见的几个触发种类，再各加一次切分。

窄的触发凑不够：拿系统日期当“今天”在 finance 只有 3 道，统计类在五个领域合起来也只有 6 道确认题。这些格要靠跨领域合并才学得动。

**离线模拟。** 模拟基于证据表，单位是每 100 道新题的真实净题数。假设：方法适用时修复概率 0.5；伤害按第 1 轮筛选校准；单次重跑的翻转率 0.03。S0 会换成回放实测的参数重跑。

| 设定 | 全开 | 学到的情境开关 | 每方法一个开关 | oracle |
|---|---|---|---|---|
| finance，50 道 train，证据级伤害 | −1.2 | 50 / 100 / 200 / 400 次尝试：+2.8 / +4.2 / +5.9 / +7.4 | +0.5～+2.4 | +14.4 |
| 五领域合并，300 道 train | −3.1 | 300 / 600 / 1200 / 2400 次：+5.3 / +6.4 / +8.1 / +9.6 | — | +11.8 |

as-of 日期这一项单独看：
- finance 约 17 道日期题，学 1 / 2 / 4 轮的准确率提升是 +1.8 / +3.8 / +5.4 个点（oracle +8.4），P(有增益) 分别为 0.69 / 0.82 / 0.90；
- 合并 85 道日期题时，1 轮就有 +5.9，P 0.96。

**读法：**
- 在证据级伤害下，全开是负的；学到的表把它翻成正的。这就是本设计里 RL 那一半的价值。
- 瓶颈在“方法触发且起作用的**不同**题”有多少，不在重跑噪声。所以设计里用了：只比触发过的题、按断言类记功、跨领域合并。
- 在 finance 一个领域里，这一步是**机制检验**，不是头条。

## 4. R 的角色：发明与改进方法，并改进自己的研究方法

### 4.1 分工

| 角色 | 做什么 | 不做什么 |
|---|---|---|
| R（LLM，每轮）| 决定哪种反复出现的失败值得做成方法；写代码、触发器、卡、测试；提议一个分桶特征；根据遥测改进、收窄或退役方法；决定是否接受端口 | 不写表、不定格的动作、不改路由；看不到 dev 的逐题结果和 dev 轨迹，看不到任何 test |
| 固定代码 | 补齐 kit、跑触达检查、出遥测、拟合表、出重拟候选、提供端口 | 不发明方法 |
| actor | 用卡、推荐、注入的摘要和拦截信息完成任务 | 看不到划分 |

### 4.2 R 的新输入（全部只来自 train）

- **`inputs/methods.json`**：每个已装方法一份，包括：
  - 版本哈希、调用数、错误率、平均输出长度、`unclassified` 的占比；
  - 每格的这些数：触发题数、动作；推荐之后真被调用的比例；注入、拦截次数；后验均值与 90% 区间；learn 读数里的修复、退步和“不得做”记账；
  - 伤害案例和“带错调用”案例的 train 尝试引用，R 可以用 `read_trajectory` 去读。
- **`inputs/uncovered.json`**：当前 W\* 下失败的 train 尝试，按两个维度聚类：失败断言的类（数值错、泄漏排除值、没写到目标、过度行动、分类串错）× 行为特征（401 之后没查文档或表格、文档搜索为空就停、读数值不带页名、写后不读、拿系统时钟当锚点）。每个簇标明是“没有方法覆盖”，还是“被方法 m 覆盖但后验 ≤ 0.5”。
- **`inputs/method_research.json`**：研究动作表，见 §4.5。
- **`inputs/method_kit/`**：路由（只读参考）、表的存根、`_mlib` 模板、测试模板。R 像复制 `default_agent.py` 那样复制它们。
- **`inputs/method_ports/`**（S2 起）：其他领域已装方法的文件、卡和合并计数，不带任务 id。

`PROPOSE_SESSION` 加一段方法说明（英文，R 的提示）：

> Methods. When the actor did the same search, table analysis, date or threshold choice, policy-version choice or pre-send check by hand and got it wrong in two or more failing train tasks, you may write a method: a read-only package tool named m_<name>, built from inputs/method_kit/. A method returns evidence with provenance and an `unclassified` part; it never writes and never decides. Declare its trigger (a pure function of the tool event and the view), its `on` action, its footprint, at most one context feature and at most four variants in the module's METHODS literal, and test it on synthetic data. Do not edit middlewares/m_router.py or middlewares/m_table.py: the router is fixed and the table is fitted from train rewards. inputs/methods.json and inputs/uncovered.json show where your methods fired, were used, helped or hurt; refine, narrow or retire them from it. Judgment failures (what a policy allows, what not to do) belong in memory or skills, not in a method.

方法说明里点到的几个大类（搜索、表格分析、日期或阈值、政策版本、发送前核对），是写进 R0 的人为先验，在自主性账目里记明。具体的方法、模式和例子不交给 R（§1.8）。

### 4.3 R 的五种动作

每轮仍是 k = 3 个候选。

| 动作 | 什么时候做 | 怎么核对 |
|---|---|---|
| 新方法 | 同一种手工搜索或计算在 ≥2 道失败 train 题里出错 | 草稿链 + 触达底线 + 筛选 + gate |
| 改进 | 方法被调用过的轨迹里看得到失败（解析错、`unclassified` 太多、漏了来源）| 同上；改过的方法算新的臂，旧计数取半权 |
| 收窄触发或重写卡 | 遥测显示“推荐了却没调用”，或“调用时参数错” | 同上 |
| 退役 | 两个 learn 读数里都全格静默，或者从没被调用 | 一个移除候选，照常筛选（看通过题会不会因此坏）|
| 接受端口 | 别的领域的方法在本领域的 `uncovered` 簇里有对应 | 本领域的触达检查、筛选、gate |

这些动作对应的做法：LATM、CRAFT、ToolMaker（带测试的工具制造与自我修正）、TroVE（按使用量修剪），以及 Voyager 式的可组合方法库（共享 `_mlib`）。

### 4.4 准入

现有的检查都照旧：形状、K 允许的组件、字面量、探针与冒烟、包测试、`declaration_mismatch`（carrier `tool`，site 是 `m_` 工具名）、重复实验、重复机制。新增四条，都免费：
1. kit 检查（§2.7）；
2. 触达底线（§2.7）；
3. 触发器在全部 train 回放里 ≥2 道不同的题（§2.2 第 9 条）；
4. `METHODS` 合乎上限：变体 ≤ 4，分桶特征 ≤ 1。

### 4.5 改进器层：R 的研究方法也从结果中学

方法库让 W 变好，这是 W 层的学习。下面三样让 R 怎么造方法本身随结果改变：

**(a) 研究动作表（固定代码统计，S1 起）。** `inputs/method_research.json` 记录 R 自己做过的每一种动作，按动作 × 族列出：
- 提出了几个、准入了几个、第一稿就过触达底线的比例、平均修订几次；
- 筛选的修复与退步；
- gate 的通过与否（每个候选只有一位）；装上与否；后来是否退役；
- 装上之后，学习器把它的格转 on 和转 silent 各多少。

这张表跨轮、也跨活动累积：support 的 R 能看到 finance 时期自己的记录。原因：R 要知道哪类动作真正带来过安装、哪类在筛选里总伤通过题（例如“核对方法如果触发得比 outbound_send 还宽，就会退步”），才能调整自己的优先次序。

**(b) R 的研究剧本（S1 第 2 轮起）。**
- 存储：R 的研究集里多一个文本组件 `research.playbook`，上限 4,000 字符，bootstrap 时为空。
- 编辑：每轮会话开头，R 可以改工作树里的 `playbook/method_playbook.md`。每条教训必须以引用结尾，格式为 `[<候选 id>: <字段> <值>]`，例如 `[finance-claude-v5/d1/m_ledger: fixes 3, sample_regressions 0]`。
- 核对：宿主逐条对台账。引用的行不存在或数字对不上，这条教训就被删掉，并把原因回给 R。
- 生效：通过核对的文本存成 R 的一个新版本 R_g = R0 + 剧本_g，下一次会话的系统文字里，它接在 `PROPOSE_SESSION` 之后；每次会话都记录剧本的哈希。
- 字面量检查同样适用。

原因：这是 ExpeL“从成败对比中提炼洞见、不改权重”的做法搬到改进器层；要求引用，是为了让每条教训都能追到账目，而不是 R 的自述（RSI_PLAN §4.3 的“引用核对”）。剧本里每个字节都出自 R 的会话，自主性账目得以成立。

**(c) RL 结果反过来给 R 指方向。** `uncovered.json` 把“学习器判定某方法在某簇上无效”（后验 ≤ 0.5）和“没有方法的簇”交给 R。R 下一轮的发明就落在选择学习还没拿下的地方。这样三个循环是耦合的：R 扩大方法空间；学习器判断这些新增在哪里有用；下一个 R 读到哪些还没覆盖。

**(d) 怎么量，以及不主张什么。** v0.5 只报过程数字：
- 每轮方法草稿第一稿过触达的比例；
- 每个准入方法的修订次数和 R 的回合数；
- 每个方法候选在筛选里的净翻转；
- 退役比例；
- 剧本条数，以及被删掉的引用数。

“R 因为剧本变得更会改进了”是改进器层的主张（L4/L5），需要 RSI_PLAN §4 的冻结孪生协议：同一领域、同样预算、对 R 隐藏的 dev 面板，比较 R_g 与 R0 的每候选产出。本版不跑，留到用户另行决定（§10）。措辞上限照 RSI_PLAN §4.4：W 层的增益对外叫“自动 harness 工程”加“学到的方法选择”，不叫“改进器变强”。

## 5. 与 agent 内 RL、框架层进化搜索的关系与组合

### 5.1 分层

| 层 | 学什么 | 谁来学 | 用什么数据 | 多久一次 | 什么时候冻结 |
|---|---|---|---|---|---|
| W 的内容：方法库（§2、§4）| 有哪些方法、方法本身怎么写 | R（LLM）| train 失败、遥测、触达 | 每轮 | 装上即冻结（内容寻址的包）|
| W 内的选择：方法选择表（§3）| 每个方法在哪里出手、用哪个变体 | 固定代码（统计）| learn 读数的配对奖励 | 每个 learn 读数 | 进 dev/test 之前贪心冻结 |
| W 内的一般选项策略（V0_5_AGENT_RL）| 更一般的选项（如果那份文档这样定义）及其策略 | 固定代码 | train | 由那份文档定 | 同一条冻结规则 |
| 包的选择：进化搜索（V0_5_EVOLVING_SEARCH）| 哪些包或候选进入下一代 | 框架（筛选、gate 或种群选择）| train 筛选 + dev gate | 每轮或每代 | 安装或发布时 |
| R 的研究方法（§4.5）| 怎么做研究 | R 自己（带引用）| 自己候选的台账 | 每轮 | 每次会话记录哈希 |

### 5.2 与 agent 内 RL 的接口

本文的选择表应当是 AGENT_RL 那套“选项策略”的一个实例：选项就是方法，策略就是 `m_table.py`。对那份文档，本文只提四点要求：
1. **同一个估计器和冻结规则**：配对的不一致计数、Beta 后验、半权合并、n ≥ 5、0.8、题级不为负、“不得做”记账。如果 AGENT_RL 选了别的估计器，两份文档择一，不并存。原因：两套规则会在同一份 learn 数据上给出相互矛盾的表。
2. **同一个只在 train 上跑的 learn 阶段，同一种对偶孪生分配。** 一次 learn 读数应当同时服务两份设计的全部决策点（各决策点的符号相互独立），而不是各跑一次。原因：$50 一次，数据可以共用。
3. **同一种 mark 和同一份收据。** 都用 `method_decision`，或者一个更一般的 `option_decision`；dev/test 上倾向值全为 1。
4. **方法类选项有三个特殊约束**：只读、只给证据、触发器是纯函数。AGENT_RL 若有会写入或会调用模型的选项，就不享有 §2.9 的借用论证，也不能在 hook 里执行。

### 5.3 与进化搜索的接口

- **方法是基因材料。** R 在方法上的动作（新增、改进、加变体）就是变异；变体相当于 AlphaEvolve / FunSearch 式的程序变异，子臂继承父臂计数的半权。
- **便宜的评估器。** 草稿链、触达检查、在录下 train 事件上的回放都免费，搜索可以在付钱之前用它们过滤。它们是底线，不是适应度：理由与 §2.7 相同。
- **表不是搜索对象。** 表的格由拟合产生，不交给 LLM 变异。原因：在有噪声的筛选结果上用变异去搜格，会对选择偏差过拟合；用随机化日志去拟合更便宜，也没有偏差。
- **种群成员共享遥测。** 同一领域的 learn 数据和方法遥测，对所有种群成员都可见（只限 train）。

### 5.4 合成一个活动、一次 final

每个可以主张的领域，test 只读一次。support 的 test 至今没读过；如果三份设计各开一个根，support 的 test 就会被争用。所以建议：
- v0.5 在每个主张领域里只跑**一个活动**，三份设计各自作为其中的组件；
- 每份设计的增量用**预注册的消融臂**来量，所有臂放在**同一次封存 final**里；
- 本文要求的臂是“W\* 的表全开”（§7.3）；
- 三份文档在冻结 support 计划之前，对齐好臂的清单和总预算。

### 5.5 冲突时怎么办

三份文档中先定稿的那份，定下共享的名字：表文件、mark 的种类、learn 阶段的名字、K 里那张表的键。另外两份在写代码之前改过来。这遵循“先文档后执行”。

## 6. 有效性

每条后面写了原因。

1. **dev/test 不回流到学习。**
   - 学习器只读 train 的断言行和 train 轨迹，按运行的划分清单过滤（测试里会故意放进 dev 行，验证它们被忽略）；
   - R 的方法输入（`methods.json`、`uncovered.json`、`method_research.json`）只来自 train；
   - R 能看到的 dev 信息，只有 v0.4 就有的那一位 gate 结论（接受或拒绝）。

   原因：dev 是 gate 的面板；如果把 dev 的逐题或逐方法数字喂给学习器或 R，几轮之后 dev 就变成了 train。
2. **包看不到划分。** 表是常量；探索只以孪生包的形式存在，只由 `learn` 阶段在 train 上运行；§3.7 的三条收据把贪心和冻结钉死。原因：如果 agent 自己能分辨在不在 dev，“冻结”就只是一句约定。
3. **方法不能内嵌答案。**
   - (a) 现有字面量检查覆盖方法代码、测试、`METHODS` 和表，只属于一道 train 题的值一律拒绝，包括断言里的数字；
   - (b) R 的测试只用合成数据；
   - (c) 触达检查只回计数，而且从不当作目标；
   - (d) 触发器至少碰到 2 道 train 题；
   - (e) 格至少 5 道触发题，格名是通用的桶名，阈值落在粗网格上；
   - (f) lint 要求的记号全部来自运行时数据（任务文本、政策、记录、日志），代码里不写任何具体的记号。

   原因：方法是代码，比文字更容易藏进一个具体的值；(a)～(f) 分别堵住代码、测试、检查输出、触发、格、lint 这六条路。
4. **R 不写学到的东西。** 表和格的动作由固定代码从奖励拟合；R 改了表会被替换。原因：学到的那一部分必须是 R 的叙述左右不了的统计量。
5. **基准的规律要标出来。** 如果方法依赖的是 AutomationBench 特有的规律（如噪声行的命名 'Channel-Noise-N'、沙盒时钟与业务日期之间的差），在报告里标明。原因：这类方法能在本基准的 dev/test 上推广，却未必能推广到真实系统；对外的表述要把两者分开。
6. **统计上的守卫。** n ≥ 5、0.8、题级不为负；重拟候选照常筛选和 gate；最终主张只看封存 final。原因：train 上每格的提升是过程证据，dev 上的增益有选择偏差（v0.3：筛选内修复率 27～44%，到 dev 只剩 11～19%）。
7. **归因。**
   - 封存 final 的轨迹带 `method_decision` 标记，所以可以按“这道 test 题里有没有方法出手”把增益拆开来报。原因：回答“声称的那条路径是不是真的走了”。
   - “方法有用”和“表有用”只有加了“表全开”这一臂才分得开；不加，就只主张 W\* 对 W0。
8. **“学会了何处用”的操作定义。** 同时满足三条：至少 1 个格以后验 ≥ 0.8 离开默认；重拟候选过了 gate；交叉拟合的 train 价值随见过的题数上升（§3.8）。原因：这句话不能从 R 的叙述里推出来，只能从格的去向和曲线里读出来。

## 7. 真实测试计划

### 7.1 S0：离线，$0，决定去留

1. 实现 §8 和其中的测试。全部离线通过之后，在任何付费阶段之前跑 `-m 'not live'`。
2. **在录下的 train 轨迹上回放**（v3 252、v4-finance 50、Marketing 52；不读 dev 逐题，不读 test）：
   - 每个 `_mlib` 检测器在每个领域的触发题数，以及触发时的失败率与通过率；这些数决定哪些格可能达到 5 道；
   - 按类记功的误记率：在一题多模式的 49 道里，一个失败断言被记到几个方法上。
3. **机制演练**用玩具方法，它们只是测试夹具，永不进包：
   - 触达检查在一个植入了多页工作簿的 train 夹具世界里触发并报告计数；
   - `m_` 工具发出的 POST 被拒；
   - 拟合能找回一个植入的格和一次植入的切分，不足 5 道时保持默认；
   - 孪生的分配是对偶的，倾向值精确；
   - 路由被改会被替换；
   - lint 对同一正文只拦一次。
4. **换上实测参数重跑模拟**（入库为 `examples/automationbench/method_sim.py`），事先登记 S1 的读法。
5. **去留标准：**
   - 机制演练全过；
   - finance 至少有 3 个检测器种类在 ≥5 道 train 题上触发（否则一个领域里的格学不动，S1 的选择学习部分改成只报遥测）；
   - 不满足就先修，再付钱。

### 7.2 S1：finance 机制检验（约 $290，不读 test）

- **根。** `examples/automationbench/claude-v5-finance-methods.toml`，新工作区，在 v0.4 finance 活动及其 final 提交之后才开。
  - 划分钉在 `4fd91eaf9fb8daf5`（与 v3、v4 相同）；
  - 起始版本 = v0.4 finance 最终装上的 W\*，按包引用导入，并把来源记进 manifest；
  - preset v5 = v3 preset + `[improvement.methods]` + 方法 kit + 剧本组件。
- **为什么接着 W\*。**
  - 现实中的问题是“在一条文字规则已经拿到的增益之上，方法还能加多少”；
  - finance 的 test 会被 v0.4 的 final 用掉，这里本来就做不了 C1；
  - 不用重新花钱去买 source_memory 已经拿到的那一轮。
- **W\* 的对照。** train 50 + dev 25 × 3 = 125 次 × $0.37 = **$46**。
- **3 轮。** 每轮：R 约 $12～15（写代码和测试比写文字多 5～10 个回合）；筛选 36 × $0.50 = $18；gate 25 × $0.50 = $12.50。合计约 $43～46 一轮。另加一次确认 $12.50 和一次装上后的重跑约 $12。3 轮约 **$155～165**。
- **learn 与重拟。** 第一次装上方法之后，在下一个轮次边界做 learn 读数：100 × $0.50 = **$50**。重拟候选在下一轮作为第 k+1 个候选：筛选约 12 × $0.50 = $6，gate $12.50，确认 $12.50，约 **$31**。如果第 1 轮就装上了，第 3 轮之后可以选做第二个 learn 读数，为学习曲线多一个点（+$50，单独决定）。
- **免费的过程检查**（每轮之后、付下一轮之前，从记录里读）：
  - 3 轮内至少 1 个方法候选到达筛选；
  - 每个筛选过的方法都过了触达底线；
  - `m_` 工具的写调用为 0；
  - 方法错误率 < 10%；
  - 方法的基础调用 < 工具预算的 15%；
  - 报告推荐到调用的转化率；
  - 剧本里被删掉的引用数。

  任何一条不成立，先离线修，再付钱。
- **读法。** dev 的合并 gate 对比 W\* 的三次对照读数，量的是“在 source_memory 之上加方法”的增量。这是过程数字，有选择偏差，不进主张。安装规则不变：筛选，然后 gate，然后确认。

### 7.3 S2：support 主张（约 $410）

- **根。** `examples/automationbench/claude-v5-support-methods.toml`，从 W0 开始；划分钉在 `3da11232f5d5ff38`；test 未读。
- **建议：** 这个活动就当作 support 的活动，替代 V0_4_PLAN §3 里原定的 support 活动，让 support 的 test 只在本活动预注册的 final 里读一次（§5.4）。这一条由用户决定。
- **步骤与费用。**

| 步骤 | 费用 |
|---|---|
| 基线：125 × $0.466 | $58.25 |
| 3 轮。第 1 轮起提供 finance 的端口（`inputs/method_ports/`），R 的研究动作表和剧本也从 S1 带过来 | 约 $160 |
| learn 与重拟 | 约 $81 |
| 封存 final：W0 25 × 3 × $0.466 = $34.95；W\* 25 × 3 × $0.50 = $37.50；W\*（表全开）$37.50 | $109.95 |

  “表全开”这一臂把所有格都设为方法声明的 `on`。只有当 W\* 的表至少有一格与全开不同时才跑这一臂；否则它和 W\* 是同一个包。
- **合计约 $410，根取 $450。**

### 7.4 S3 以后（由用户决定）

- hr 与 sales 的 test 也还没读，可以照 S2 的形状做。（2026-10-08，V2_TRAINING_PIPELINE §7 第 2 项：sales、hr 的 test 留给 v2，v1 活动在这两域不跑 final；本条与下一条的五领域 test 计划不再适用。）
- 五领域合并的方法库：领域作为合并的先验，test 25 × 3 × 2 臂 × 5 个领域，约 $800 量级。
- 改进器层的冻结孪生检验（§4.5 (d)）：费用参照 RSI_PLAN §5。

### 7.5 事先登记的判定

在第一笔付费之前，随 JOURNAL 一起提交，之后不改。

| 命题 | 成立 | 否定 / 停止 | 不确定 |
|---|---|---|---|
| S1 的过程：R 能造出可用的方法 | 3 轮内 ≥1 个方法过了筛选 | 3 轮没有一个方法过筛选 → 停止 S1，如实报告“在这套协议下，R 没有造出有用的方法”；S2 是否做、或者先做一次人工种子方法库的消融（§10），由用户决定 | — |
| S1、S2：学会了何处用（§6 第 8 条）| 三条全满足 | 重拟候选从未过 gate → 报“选择未证实”，附格的计数 | 有格离开了默认，但重拟没过 gate |
| S2 的 C1（主假设）| support test 上 W\* − W0 的 95% 区间下界 > 0；同时并列 W0 的 pass@k | 下界 ≤ 0 且点估计 ≤ 0 | 下界 ≤ 0 但点估计 > 0（预期最常见的结局之一）|
| S2 表的增量（次要）| W\* − W\*全开 的单侧区间下界 > 0 | — | 预期大多落在这里（25 题，功效低）|
| 迁移 | support 带 finance 先验时，共享格达到 0.8 所需的题数少于冷启动（§3.8 第 3 条）| 不少于冷启动 | — |
| 发现召回（过程）| 报告 R 已准入的方法覆盖了 §2.4 预测中的哪些。不评判，只报 | — | — |

- **不做：** 边缘读数之后追加读数或重复（不搞可选停止）；给 R dev 题号或 dev 轨迹；读 finance 的 test。
- **功效：** 25 × 3 的 final，95% 区间半宽约 ±12 个点；若真实增益为 +8 个点，功效约 40%。这一点预先写明。

### 7.6 预算汇总

| 阶段 | 预计 | 根 |
|---|---|---|
| S0 | $0 | — |
| S1 finance | 约 $290（第二个 learn 读数另加 $50）| $320 |
| S2 support | 约 $410 | $450 |
| 合计 | 约 $700 | $770 |

单价：W0 $0.466；带方法的候选按 $0.50 计（v0.3 的候选是 $0.49，方法再加工具调用和卡片 token，估计 +0～+10%）；已装 W\* 是 $0.37（v0.4 finance 第 1 轮）。

### 7.7 预期效果

校准用的历史数字：
- v0.3：筛选内修复率 27～44%，dev 上 11～19%；
- v0.4 finance 第 1 轮：一条文字规则预测 13 道，筛选里修好 9 道，dev 合并 +11.3。

| 量 | 预期 |
|---|---|
| 一个针对 5～8 道失败 train 题的方法候选 | 筛选里约修 30%，dev 上约 15%，相当于每装一个方法 +1～+2 道 dev 题 |
| 每领域 3 轮装 2～3 个方法 | dev +2～+5 题/25（+8～+20 个点）；test 约一半，+4～+12 个点 |
| finance，在 W\* 之上 | dev +1～+3 题；dev gate 确认的概率约 0.25～0.45 |
| 表本身（相对全开）| +0～+2 题，主要来自避开退步：第 1 轮 send_review 的 3 修 2 坏，如果坏的那几格静默，净值是 +3 而不是 +1 |
| 每次尝试的成本 | 持平到 −10% |

## 8. 代码改动概要

全部在 v0.4 finance 活动提交之后才动。`default_agent.py` 的字节一变，新的 bootstrap 就会有新的种子哈希；已有工作区的记录不受影响（下表最后一条测试会验证）。

| 模块 | 改动 |
|---|---|
| `agent/default_agent.py` | `_execute`：名字以 `m_` 开头的工具拿 `_PackageContext(read_only=True, max_calls=25)`；`_PackageContext.call_tool` 超过上限时回错误，不发调用 |
| `agent/harness_package.py` | `PackageRuntime.call_tool` 用同一条规则；`methods_of(files)` 解析 `METHODS`；`method_table_check(files)`（能被 literal_eval、结构对、greedy 时每格单一动作、变体 ≤ 4、格指向存在的 `m_` 工具与种类）。不新增包种类 |
| `agent/gateway.py` | `MARK_KINDS` 加 `method_decision`（字段：方法、种类、格、动作、变体、倾向值、触发回合、情境桶、注入字符数、是否拦截）|
| `research/methods.py`（新）| `METHOD_KIT`、`kit_complete`、`reach_check`、`method_telemetry`、`uncovered_clusters`、`explore_twins`、`fit_table`（后验、切分、冻结规则）、`refit_files`、`ope_curve`（交叉拟合的双稳健估计）、`research_moves`、`playbook_check` |
| `research/drafts.py` | `Drafts.check`：diff 里加了或改了 `m_` 工具时，跑 kit 检查和触达检查；新增 train 世界的工厂 `train_world(task_id)` |
| `research/session.py`、`policies.py` | `SESSION_FORMAT` 列出新输入；`PROPOSE_SESSION` 加方法段；kit 补齐；第 k+1 个重拟候选；剧本的加载、编辑与引用核对；`default_research_set` 加 `research.playbook` |
| `bench.py` | `--phase learn`（只在 train 上跑）；K 加 `[improvement.methods]`：`min_cell_tasks = 5`、`p_decide = 0.8`、`pool_weight = 0.5`、`max_variants = 4`、`max_must_not = 1`、`max_cost_rise = 0.25`、`method_calls = 25`、`trigger_min_tasks = 2`；`[workspace] start_release`；dev/test 拒绝非 greedy 的表；final 的“全开”臂 |
| `adapters/automationbench.py` | `assertion_class(type)`，映射到五个断言类 |
| `report.py` | 每轮的方法表（触发、调用、每格的修复与退步）、方法库的增长、学习曲线、final 按“有没有方法出手”拆开的增益 |
| `examples/automationbench/` | `claude-v5-finance-methods.toml`、`claude-v5-support-methods.toml`、`method_sim.py` |

新增测试（约 19 个，`test_methods.py` 等）：
- `an_m_tool_gets_a_read_only_broker_and_a_post_is_refused`
- `an_m_tool_past_its_call_cap_gets_an_error_not_a_call`
- `the_router_is_the_kits_bytes_and_the_table_is_parsed_not_imported`
- `r_edits_to_the_router_or_table_are_replaced_and_reported`
- `the_reach_check_replays_recorded_train_events_through_the_trigger_and_runs_the_method_on_a_fresh_train_world`
- `a_method_that_never_fires_on_its_predicted_tasks_is_refused_with_counts`
- `a_trigger_that_fires_on_one_train_task_only_is_refused`
- `telemetry_and_fit_read_train_attempts_only`
- `the_explore_twins_flip_every_gate_between_a_and_b_with_exact_propensities`
- `the_learn_phase_refuses_dev_and_test_and_a_dev_run_refuses_an_explore_table`
- `fit_table_recovers_a_planted_cell_and_keeps_the_default_below_five_tasks`
- `a_split_needs_five_tasks_on_each_side`
- `other_domains_and_pre_install_counts_enter_at_half_weight`
- `the_refit_candidate_changes_only_the_table_and_enabled_flags_and_is_screened_like_any_candidate`
- `the_router_caps_recommendations_and_blocks_a_body_once`
- `a_dev_or_test_reading_with_a_propensity_below_one_is_invalid`
- `a_playbook_lesson_without_a_valid_ledger_citation_is_dropped`
- `the_journal_does_not_survive_an_attempt`
- `a_package_without_m_tools_runs_and_records_as_before`（已有工作区的回放不变）

工作量：离线约 1～1.5 周。

## 9. 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| R 造不出方法 | R 以前的候选全是文字和 hook | 方法说明、kit、`uncovered.json`；预注册的停止规则（§7.5）；人工种子库只作为事后消融 |
| 方法没人用 | 175 次带写入的尝试只有 16 次读回，actor 很少自己做系统化的步骤 | 路由的推荐、注入、拦截；R 根据“推荐了没调用”的计数重写卡 |
| 推荐带来退步 | send_review 修 3 坏 2 | 每格学 silent/on；核对类要求日志非空；上限；只拦一次 |
| 自信但错的方法 | ledger 丢掉一行真实数据，比没有方法更糟 | 出处、`unclassified`、R 的测试、触达检查、通过样本、gate |
| 样本少 | 每领域 50 道 train；窄的格学不动 | 只比触发过的题、按断言类记功、跨领域半权合并；接受“finance 只是机制检验” |
| 记功被钻空子 | lint 可能把记号硬塞进正文；scrub 可能删掉必需的内容 | 记号只取自运行时数据；“不得做”记账；题级不为负；dev gate 看整道题 |
| 方法之间的相互作用 | LEDGER→SCRUB、DOC→VER、API→EFFECT 是互补的 | `couples_with` 作为组合决策；方法改过之后重新拟合 |
| 底子在变 | 新的 W\* 会让旧后验过时 | 安装前的计数取半权；每次装上之后的下一个 learn 读数重新拟合 |
| 基准过拟合 | 噪声行命名、沙盒时钟 | 在报告里标明（§6 第 5 条）|
| 上限 | 24 个文件、256 KiB、200 次工具调用、20,000 字符 | 按族分组；每方法 25 次调用；修剪 |
| 宿主改动影响身份 | `default_agent.py` 的字节变了 | 只在新的 bootstrap 里生效；有回放不变的测试；不在 finance 付费运行期间合入 |
| 与另两份设计冲突 | 估计器、learn 阶段、mark、test 的使用 | §5 的接口；先定稿者定名字；三份文档对齐之后再写代码 |
| 判断类失败 | 18 道（hr 占 12）不归任何方法管 | 在方法说明里写明交给 memory/skills；不指望方法去修“该克制”的失败（避免重蹈 v0.3“多做”的覆辙）|

## 10. 考虑过、不做

| 做法 | 不做的原因 |
|---|---|
| 分页 / 翻页方法 | 22,130 个输出里 0 例 |
| 裸计算器 | 算术失误 0 例；`finance_calc` 只有 finance 的 2 道（CALC），推迟 |
| 按题号或按题目关键词定的策略 | 关键词 ±0.1；题号在包里看不到，也等于记答案 |
| 卡片的向量检索 | 方法不超过 15 个，全放进工具列表就行 |
| 在 dev/test 流上在线学习（AWM 的在线模式）| 那就不是留出证据了；dev/test 只用冻结的表 |
| 方法里调用模型、LLM 判官式核对 | 不确定、回放不了、成本不可控，也不能在 hook 里跑 |
| 会写入的方法 | 破坏 §2.9 的借用论证和 hook 内执行；写入留给 actor |
| 让 R 写表或写概率 | 学到的部分会被 R 的叙述左右 |
| 包内用随机数做类别抽样来探索 | 换成按任务文本哈希的对偶分配：确定、可复现、不用在 view 里传种子 |
| 新增包种类 `policy/*.json` | 用 `middlewares/m_table.py` 纯字面量代替，不加种类、不加上限 |
| 把筛选臂的配对放进拟合 | 卡片和路由动作在里面一起变；只给遥测，保证表的估计干净 |
| 按目标连线的记功（默认开）| 实现量大；先按类，S0 显示太吵再做 |
| 带任务固定效应的 logistic 交叉校验 | 和“题级不为负”的守卫作用重复 |
| on 动作的分级晋升（recommend → inject → 拦截）| 臂数翻倍，样本不够；on 的等级由 R 声明 |
| 默认就跑第二批 Thompson 读数 | $50 一次，只在 ≥3 个格未决时才跑 |
| 每轮都做翻转读数 | 成本高；每个阶段一个 learn 读数就够 |
| 把本文的模式表或方法清单交给 R | 发明会变成我们的；自主性账目不成立 |
| 人工写好的种子方法库作为起点 | 理由同上；只在 S1 得出否定结论之后，作为“瓶颈在 R 的发明还是在方法这条通道”的消融（约 $150）|
| 固定代码按研究动作的后验分配 R 的候选名额 | 3 轮 × k = 3 太少，学不出来；会盖过 R 对失败的阅读；交给剧本 |
| v0.5 里跑改进器层的冻结孪生检验 | 要另开一笔预算（RSI_PLAN §5）；本版只报过程数字 |
| 在 support 上另跑一个“无方法”的孪生活动作为框架级消融 | 多花约 $200；25 × 3 时功效约 40%；如果 v0.4 的 support 活动照原计划跑，可以把它的 W\* 作为同一次 final 的第四臂（需在冻结前登记）|
| 在 finance 上读 test | finance 的 test 会被 v0.4 的 final 用掉 |
| 单领域内的变体学习（STAT）| 五个领域合起来只有 6 道确认题，只做合并 |
