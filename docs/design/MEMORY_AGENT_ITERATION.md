# Memory 与 agent 架构的下一轮迭代（v6）：设计稿

**状态：** 草案（待用户讨论）
**日期：** 2026-10-08

**范围：** 只读设计，没有改代码，没有跑模型。行号基于 main @ `8434c03`。slim/1 @ `2dbd935` 目前只改了 `tests/`，后续批次合入后行号会变。preset 名暂称 v6（PLAN §3 第 14 项未定）。

**用户的指示（2026-10-08）：**

> "尽快清理代码，清理完成之后尽快开始memory和agent架构的迭代，注意参考开源的实现，不要闭门造车，除非有非常明确的二开理由，需要跟我讨论。但也不要盲目的使用开源框架"

> "需要把memory和agent的架构优化了之后，作为这一个版本开源，这两个做好了成绩应该会更好"

> "……所以harness本身的rsi应该就必须要修改代码和创造工具，通过代码和工具的方式把原本复杂的memory表述变成结构化的流程……修改prompt和memory里面的纯文字虽然简单，但我反而觉得风险比较高，因为很难来衡量梯度……但是创造工具与该流程反而是确定性的，因为代码的执行是确定性的，任务定义，函数的schema是确定的，因此执行之后是否解决了任务相对好衡量一些。"

**依据：** 本稿根据六份只读调研写成，又经过两轮评审（"不要闭门造车"和"不要盲目用开源"）。评审中核实过的意见都已采纳，§10 列出采纳了哪些。

---

## 0 结论

### 0.1 对用户主张的判断

**结论：** 主张成立，但需要一处修正。原来的说法是"文字难量"，更准确的说法是"**没经过执行验证的东西难量**"。代码的优势在于验收梯子的中间几档可以确定地做完，而且不花模型费。

| 主张 | 判断 | 依据 |
|---|---|---|
| 文字 memory 会随着信息量增长而退化，应该把经验变成代码和工具 | 成立 | 我们的数据：VEL 里唯一看得出效果的条目是 TABS（"先列出所有工作表"）。这一步可以机械化，展示过它的 29 次 train 尝试全部做了 metadata 读取，measure 臂只有 56%（S1）。外部：StarHarness 在 finance 上 +26.1，来自工具和循环补丁（arXiv 2608.24804）。 |
| 改文字效果难量；改工具和流程效果好量 | 部分成立，需修正 | ASI 论文表 3（WebArena Shopping，Claude-3.5-Sonnet，arXiv 2504.06821）：未验证的文字 32.6，验证过的文字 39.0，验证过的程序 36.4，作为动作的验证程序 40.1。增益主要来自"执行验证"（+6.4），形式从文字换成程序只多 +1.1。代码的真正优势是可以做 $0 的确定性中间验收：单元测试、在新的 train 世界上执行脚本并按真实断言判分。任务级效果仍然是随机的：actor 调不调工具不确定，dev 上同一题两次读数约 13% 会翻转。所以工具照样要过 dev gate，只是可以先用 $0 的几档筛掉坏候选。 |
| 所有经验都应该升级成代码 | 需要修正 | 失败原因里有一部分是判断：政策允许什么、该找哪里、该发给谁。这部分留在文字里。v0.4 的实测增益恰好来自 2–3 条文字规则（按主题搜 Gmail/Slack、给每个人单独发邮件）。所以层级梯子按"能否机械化"分流：计算、日期、表格比对、要维护的清单、核对自己写过什么，这些变成代码；判断留在文字里。文字也要验证，VEL 的 reflexion 来源（带着失败断言重试，通过后才提炼）就是"验证过的文字"。 |

### 0.2 瓶颈

| 部件 | 瓶颈 | 证据 |
|---|---|---|
| VEL | **容量缺陷，是我们自己的 bug，与记功无关** | 42 条进库，35 条退役，其中 34 条是被挤出满架，30 条从没展示过。模型起了 36 个架名，上限却是 `max_shelves=6`（`ouroloop/research/library.py:54`），所以任何时候库里只有 7–8 条。`library.py:307` 对 set 排序时没有次键，结果取决于 PYTHONHASHSEED。87 条提议里 45 条被丢弃，其中 30 条只是超长。 |
| agent | 写操作没有簿记，提交前不核对，日期取的是真实日历 | 456 次 W0 失败尝试里，84% 是"该写的没写或写错"。有 103 次尝试用了宿主的真实日期（2026-10），其中 76 次失败；沙箱日期在 2026-01 到 2026-04 之间。CLI 会附上真实日期，这一点关不掉（`ouroloop/providers/claude_code.py:5-8`）。`TURN_NOTE` 里写了"忽略它"（:70-73），没有用。 |
| R | 没有产出工具的路径，也没有工具的验收档 | R0 第 (1) 步把造工具指向 `api_errors.json`（`ouroloop/policies.py:720-725`），而这张表主要是 401 服务闸门。12 个候选里没有一个是工具。方法库的回放只读，而且从没在 R 轮次里跑过。 |

### 0.3 本版决策一览

| # | 决策 | 类型 |
|---|---|---|
| 1 | VEL 先修四个 $0 缺陷：容量（按字符打包、新条目观察期）、淘汰确定性、长度上限、失败来源提炼 | 在 VEL 内移植 ReMe 的 α 和 ReasoningBank 的 FAILED_SI |
| 2 | W0-v6 种子只放通用的写入守卫：台账、首次写入前分诊（输出结构化的 `plan_changes`）、提交前两向核对、日期锚。全部做成 hook，`default_agent.py` 一个字节都不改 | 移植 StarHarness、SWE-agent submit review、LangChain Todo 的模式 |
| 3 | 可机械化的具体工具（table_diff、表格写、发送前扫描、calc）**不放进种子**，留给 R 编译，按验收梯子验收 | 让"RSI 增益"与"人写架构增益"按构造分开 |
| 4 | 新增工具验收档 `toolcheck`：照 ASI 的做法，R 为每道它声称能修的 train 题提交一段调用新工具的脚本，宿主在新世界里执行，用真实断言（含 must-not）判分 | 移植 ASI `induce_actions.py::write_tests` |
| 5 | 方法库不整个删，保留回放与验收的核心，推广为 toolcheck | **改变已接受的默认 #1，需要用户定** |
| 6 | 不接 Stirrup，移植它的模式 | **改变已接受的默认 #2，需要用户定** |
| 7 | 不跑 S2（ACE、ReMe 原样作消融臂），把移植的改动直接放进 v6，只在 final 里量 | **改变已接受的默认 #5，选的是 #5 里写着的替代方案，需要用户定** |
| 8 | 实验在 finance 上跑（按默认 #6），support 作备选 | 默认 |

### 0.4 时间：今晚能做到什么

PLAN 规则 13：`code_version` 进 K（`policies.py:806-813`），活动期间不能往运行的检出里合入代码。所以全部代码必须在 freeze **之前**写完、测完、提交。

| 时段 | 内容 |
|---|---|
| 今晚 | 写代码 A+B，约 5–6 h，包括测试与和 slim/1 对齐；跑 E0 离线检查（$0） |
| 代码完成后 | 付费部分合计约 4–4.5 h，每一步之后停下等用户批准 |

结论：今晚就出 final 读数是做不到的。

---

## 1 RSI 的层级与阶段：何时改什么，怎么验

### 1.1 层级梯子

| 层 | 载体 | 内容 | 谁写 | 在 v6 里 |
|---|---|---|---|---|
| L0 轨迹 | gateway/broker 事件、`ctx.mark`、`plan_changes` 调用，全部由宿主记录 | 每一步中间输出，带来源（attempt、task、call_id） | 宿主 | 已有。`plan_changes` 让计划也成为结构化的轨迹 |
| L1 条目或规则 | VEL 条目（`skills/library/`）、`memory.md` | 判断类经验：when、做法、avoid | VEL 提炼、R | VEL v6 |
| L2 技能 | `skills/*.md` | 多步的程序性文字 | R | 不变 |
| L3 工具 | `tools/*.py`，可以写，经 broker | 确定性程序：计算、表格操作、查询 | R | **新的主要编译目标** |
| L4a hook | `middlewares/*.py`（before_tool、after_tool、on_finish） | 流程约束：守卫、核对、扫描 | 种子、R | 种子 = `write_guard` |
| L4b agent 程序 | `agent/*.py` | 循环本身 | R（每轮至多一个） | 不变（`default_agent.py`） |

### 1.2 验收梯子

每层的候选从最便宜的一档开始，过了才花下一档的钱。V0–V2 只花 $0 模型费。

| 档 | 内容 | 适用层 | 已有代码 / 新写 |
|---|---|---|---|
| V0 静态 | schema；限额（24 个文件 / 256 KiB，`harness_package.py:75-79`）；字面量检查（`drafts.py:188`）；包内代码不得读 `skills/library/` | 全部 | 已有，读库检查新写 |
| V1 单元 | 包内合成测试，由草稿 smoke 运行 | L3、L4 | 已有（drafts smoke） |
| V2a 脚本验收 | R 为每道声称要修的失败 train 题给一段调用新工具的脚本。宿主在 `suite.episode(task)` 的新世界里，通过**可写**的 broker 执行脚本，再用 `Episode.score()`（`adapters/automationbench.py:615-627`）按真实断言判分，must-not 一起判。要求：每段脚本都调用了新工具（ASI 的 `calc_valid_steps`）；在 ≥2 道声称的失败 train 题上通过。返回给 R 的只有每题通过与否和失败的断言**类别**，不返回值 | L3 | 新写（`research/toolcheck.py`），执行器复用 `reach_check` 的 worker 与新世界机制（`research/methods.py:370-455`，`bench.py:1342-1366`） |
| V2b 读工具的值命中 | 在 V2a 的运行里，统计读工具的输出含有多少该题的断言值（`values_in`，`methods.py:360`）。只计数、只给 R 看，不作准入（同 `methods.py:378-381`） | L3（读） | 已有 |
| V2c 触发回放 | hook 由宿主触发，所以回放全部已记录的 train 尝试：在 ≥2 道失败题上触发；同时报告在**通过题**上的误触发率 | L4a | 已有（`replay_events` :325 加 reach 回放） |
| V3 train 筛选 | 付费。工具和 hook 只在调用过或触发过的题上记修复，文字只在足迹出现的题上记修复 | 全部 | 已有筛选，记功规则新写 |
| V4 dev gate 加确认 | 付费，沿用 | 全部 | 已有 |
| V5 封存 final | 对外的数字只来自这一档（规则 14） | 发布 | 已有 |

**V2a 的局限：** 它只证明"工具正确，而且用它存在可行解"，不证明 actor 会用它。后者由 V3/V4 判断。R 的脚本里必然有该题的字面量，所以脚本只交给检查，不进包，存进研究账本。

### 1.3 升级规则：轨迹 → 条目 → 工具或 hook

1. **轨迹 → L1**：沿用 VEL 规则，`when` 能检索到来源以外 ≥2 道 train 题，并且没有字面量（`library.py:216-224`）。
2. **L1 条目或失败原因 → L3/L4**，同时满足三条才升级：
   - **复现**：同一原因簇覆盖 ≥2 道 train 题（`causes.json`），或者条目 `fixes ≥ 2`。
   - **可机械化**：它是工具输出的纯函数，或者是对本次尝试自身事件的簿记（计算、日期、行操作和两表比对、要维护的清单、核对自己写过什么）。
   - **不是判断**：判断类原因留在文字里，现有 R0 已有这句（`policies.py:756`）。
3. **压缩**（§1.5）：工具装上后，候选里 `subsumes` 列出的条目或 memory 行一起退役。

### 1.4 何时改什么

- 一个候选只改一层；每轮至多一个 L4b 改动。
- L3、L4 先过 V0–V2，才进 V3。V2 不过的候选不花一分钱。
- 文字候选必须声明 `footprint`：用哪个工具事件能看出这条规则被遵守了。足迹只用于交叉核对，不作记功主依据（§3 第 8 行）。
- W0-v6 种子是人写的先验，计入自主性账。R 的编译目标清单（table_diff 等）是写进 R0 的人为先验，同样计入。

### 1.5 Memory 压缩：工具覆盖条目时

- 候选可以带 `subsumes: [entry_id | memory 行]`。
- 前提：V2a 的脚本在该条目的来源题或修复题中至少一道上通过。
- 工具与退役作为**同一个候选**过 V3/V4。gate 通过后，条目以 `subsumed_by:<tool>` 退役。
- 先例：ASI 第 (2) 步"用新函数改写轨迹，再以执行验证"。Voyager 的 `add_new_skill`（`voyager/agents/skill.py:47-85`）只做同名代码的版本替换，与文字退役无关。

### 1.6 工具的生命周期

| 环节 | 规则 | 来源 |
|---|---|---|
| 使用量 | `inputs/tools.json`：每个包工具的调用次数、错误数、调用所在的题、调用题上的修复与弄坏 | 推广 `method_telemetry`（`methods.py:783`） |
| 记功 | 只在调用过该工具的题上记修复或弄坏 | 新写 |
| 退役 | 每次 train 读数后，调用题数低于 `max(2, ⌈log₂₀ n⌉)` 的工具列为退役候选。删除本身也是候选，要过 gate | TroVE `run_trove.py::trim_library`（阈值 `log(n, 20)`；评审读过代码，本稿没有复读） |
| 检索 | 本版不做。包里不超过 24 个文件，所有 schema 都在上下文里。工具超过约 15 个时，按 Voyager `retrieve_skills`（top-k 5）的模式，对描述做 BM25 | Voyager `skill.py:87-100` |

### 1.7 与定位的对应

| 定位 | 本版落点 | 下一版 |
|---|---|---|
| (a) 轨迹 memory | 宿主记录每一次 broker 调用和 mark；`plan_changes` 让计划结构化；VEL 条目带 `src`/`source_hashes` 来源 | 条目可以直接引用 L0 事件 id |
| (b) 像应用商店一样持续发布 | 每次 release 都是一个包版本，附 gate 记录；发布说明按层列出改了哪些工具、hook、文字 | 跨版本的工具注册表 |
| (c) 平台：一个主框架为多条业务线产出 harness，R 自身也能演化 | 一个域。R 的递归沿用 C4（R1 = R0 + playbook） | 共享工具规则：一条线的已验证工具进入另一条线时，只能作为那条线 R 轮次里的候选，并且要在那条线自己的 train 上重过 V2a，再过 V3/V4。ASI 报告过程序换到新站点会失效（`sort_by_listings` 的例子） |

HyperAgents 是"可编辑的改进器"的先例。我们的差异在平台形态和产品生命周期，不在"R 能改自己"这一点。

---

## 2 证据

### 2.1 S1：VEL 诊断（support 3b，只读 train/dev 记录）

| 项 | 读数 | 修法（§3） |
|---|---|---|
| 容量 | 35 条退役中 34 条是被挤出满架；30 条从没展示；任何时候库里只有 7–8 条（本稿用 `library_stream.json` 复核：42 条、36 个架名、退役 35 条、退役时从没展示的 30 条、丢弃 45 条） | 按字符打包，`max_active=40`；展示满 3 次之前不淘汰 |
| 确定性 | `library.py:307` 用 `sorted(set, key=…)` 排序，同分时的顺序取决于哈希；种子 0–5 得到 6 种不同的存活集合 | 排序键改为 `(-weight, name)`，对所有 preset 生效 |
| 长度丢弃 | 87 条提议中丢弃 45 条：33 条因 schema（其中 30 条只是超长），12 条因 reach 不足 | 上限改为 body 1,200、avoid 300、title 100，并在提示里写明上限 |
| 检索 | 52 对 dev 人工标注上：TF-IDF 的 AUC 0.73，BM25（title+when）0.90。样本小，偏向当前方法自己命中的题，而且用的是 dev | 先在 S1 已判的 62 对 train 首次命中上重算，在 train 上也胜出才换（E0d） |
| 显示数 | 显示 0、1、2、3 条时，lib − control 分别为 +0.10、+0.125、0、−0.167（混杂因素多，样本小） | `shown_k=2`（ReasoningBank 的 k 消融里 k=1 最好，k=3 更差） |
| 记功 | b(t) 是单次 0/1 读数；D=3 约 1.2 个标准差；3 次伤害全部来自同一道题（winback） | 引用记功（ACE）；伤害按不同题计数 |
| 失败 | 6 个位置首次和重试都失败，什么都没留下 | 新增 failure 来源（ReasoningBank FAILED_SI） |
| 冗余 | GMAIL 条目占了显示位，但所有臂的 Gmail 发送错误都是 0 | 前几项修好后交给记功淘汰 |

### 2.2 失败原因（456 次 W0 失败尝试，train/dev）与归属

| # | 原因 | 失败尝试数 | 能否机械化 | v6 由谁来修 |
|---|---|---|---|---|
| 1 | 处理过的条目上漏了某个动作 | 139 | 簿记可以，内容是判断 | 种子：分诊计划 + 提交前核对 |
| 2 | 对被排除的实体做了禁止的动作 | 119 | 判断 | 种子：分诊里写明 `excluded`；R：文字规则 |
| 3 | 漏了收件人或渠道 | 103 | 判断 | R：文字（v0.4 已证实）；种子核对 |
| 4 | 消息里提到了被排除的项或中间数（25 次是唯一原因） | 98 | 可以：拿声明过的 `excluded` 做子串扫描 | **R 编译目标**：before_tool 扫描 hook，读 `plan_changes.excluded` |
| 5 | 需要的值在世界里，但从没读过 | 72 | 部分可以 | R：文字（v0.4 已证实） |
| 6 | 算错或漏算（finance 33 次） | 48 | 可以 | **R 编译目标**：`table_diff`（两表按键连接，报告缺失与不一致）。calc 是否需要由 E0e 决定 |
| 7 | 声称有阻碍，就停了 | 35 | 部分可以 | 种子核对：台账为空时追问 |
| 8 | 用了真实日期 | 103 次尝试中 76 次失败 | 可以 | 种子：日期锚 |
| 9 | 写落错位置或报错 | 约 24 | 可以 | R 编译目标：表格写工具，照上游 `row.py` 的语义 |

**为什么 calc 不进种子：** finance 里"算术"一类多半不是算术本身失手。
- invoice_reconciliation：漏了 INV-3002，是两表比对漏行。
- sales_tax：CA 用错了税率规则。
- timesheet：选错了费率。
- xero_credit：错的是日期。

StarHarness 的计算器增益是在 GPT-5.4 上测的，不能直接搬到 Opus。

**日期的事实：**
- 上游把沙箱时钟放在 `WorldMeta.current_time`（`AutomationBench/automationbench/schema/world.py:66`），没有任何工具暴露它。
- 上游有意让噪声数据的日期不晚于 `current_time`，注释里说未来日期会成为 "a false date anchor"（`domains/sales/_noise.py:3490-3496`）。所以"从数据推断今天"符合基准的意图，读 `meta` 才算泄题。
- 局限：finance 里数据中最新日期距沙箱日期 ≤3 天的只有 31/75 题，≤14 天的有 52/75 题。所以日期锚只能给下界。即便如此，也比真实日期（差约 8 个月）好得多。
- oss-agent 说"actor 拿不到任何日期"，这是错的：CLI 会附上真实日期。

---

## 3 Memory：VEL v6 的逐项改动

"原样"指原样接入开源件，"移植"指在 VEL 内按它的设计实现，"不做"指本版不做。所有新设置都是 int 或 float，默认值保持 v5 的行为（`LibrarySettings.from_json` 只做类型转换，`library.py:72-79`）。v5 preset 的 library 表冻结成字面量 `V5_LIBRARY`，这样 v5 的 K 哈希不会变。

| # | 机制 | 来源（已读代码） | 处理 | 理由 | 改动位置与量 |
|---|---|---|---|---|---|
| 1 | 按字符预算打包；新条目要展示满 α 次才能被淘汰 | ReMe @v0.2.0.6 `reme_ai/vector_store/delete_memory_op.py`（`freq >= α`） | 移植 α=3。不采用 β：它是绝对通过率，通过率约 0.4 时会误杀 | 34/35 的退役来自架位挤出 | `library.py:47-66` 新增 `max_active=40`、`probation_shown=3`、`packed=1`；`shelf_overflow` :296-310 只在超过 `max_active` 时淘汰，先淘汰 `n_shown≥3` 中 utility 最低的，按 `(utility, born_at_n, entry_id)`；`compile_shelves` 写成 `skills/library/part-NN.md`，每块 ≤ `max_skill_chars`，块数 ≤ `max_skills − own`，保证流和快照一致（现在 :339-340 会悄悄截掉条目）。约 60 行 |
| 2 | 淘汰确定性 | — | bug 修复，所有 preset 都改 | 不同哈希种子得到不同的存活集合 | :307，1 行 |
| 3 | 长度上限，并在提示里写明 | ReasoningBank `WebArena/prompts/memory_instruction.py` 的条目格式 | 移植 | 30 条只因超长被丢 | :55-58；新常量 `DISTILL_V6`：去掉 shelf 字段，`when` 写成使用场景，写明"超过上限会被丢弃，请数字符"；`bench.py:3175` 的 prompt 摘要按实际使用的 prompt 计算 |
| 4 | 失败来源提炼 | ReasoningBank `FAILED_SI`（同一文件，Apache-2.0） | 移植。只在首次和重试**都**失败时调用一次，输入两次失败的轨迹摘要和失败的断言类别；至多 1 条，`kind=insight`，`avoid` 必填；新条目进观察期，单独记功 | 6 个位置什么都没留下。ReMe 表 3 中"只加成功"最好，所以这条按来源单独看 D，不好就关 | `ORIGINS` :33 加 `"failure"`；`failure_request()` 放在 :505 旁；`bench.py` :3293-3303 的 else 分支；约 40 行 |
| 5 | 显示 k=2 | ReasoningBank `WebArena/run.py` 的 `select_memory(n=1)` 与 k 消融 | 原样采用这个数值 | 外部消融和我们的显示数读数方向一致 | `shown_k=2`，不改代码 |
| 6 | BM25 检索 | Lucene BM25Similarity 的公式（idf `log(1+(N−df+.5)/(df+.5))`，k1=1.2，b=0.75），与 S1 `replay.py:36-48` 一致 | 有条件采用：E0d 在 train 上也胜出才开（`bm25=1`）。用标准库内联约 15 行，**不加** rank_bm25（它要 numpy，用的是 k1=1.5 加 epsilon 托底，和 S1 测的不是同一个公式）。BM25 只负责排序，分数门槛按"占本查询最高可能分的比例"归一 | 宿主只依赖 httpx（`pyproject.toml:14`）；绝对阈值在库从 0 涨到 40 条的过程中不稳定 | `retrieve` :450-468 |
| 7 | actor 引用自己用了哪些条目 | ACE `ace/prompts/generator.py`（`bullet_ids`）、`ace/ace.py::_train_single_sample`（`extract_playbook_bullets`） | 移植，放在 `cite=1` 后面。每条前缀 `[lib <id8>]`，`INTRO_V6` 要求最后一行写 `library-used: <id8,…>`；宿主从记录里的最终消息解析，不改 runner | 最终消息不参与判分：`Episode.score` 的注释写着 "Nothing the agent wrote in a message is an input here"（`adapters/automationbench.py:618`），上游断言签名都是 `(world, assertion)`。v0.4 里 R 事后归因错了，actor 自己报更可靠 | `retrieval_block` :471-485；新增 `cited_ids()`；`credit(cited=…)` :274-293；`bench.py` :3239-3245 从 transcript 取最终消息 |
| 8 | 记功：被引用的条目记 r − b；没有引用行时按 1/len(shown) 均分；伤害按不同题计数，≥2 道题且 `n_shown≥5` 才退役 | ExpeL 的计票删除（`agent/expel.py`，只取思路） | 新写，属工程选择 | 3 次伤害全部来自一道噪声题。证据只有 1 道题，所以保留这一改动，但不作为改进来报 | :274-293，约 20 行 |
| 9 | 流分批 | — | 新写；`stream_batch=4`，交用户定（§8） | 墙钟时间从约 2 h 降到约 50 min；代价是每批更新一次库 | `bench.py:3272-3320`，约 80 行，用 `work.train_screen` 按批跑 |
| 10 | 提炼时给出已有条目，允许返回 `update_of` | ACE Curator（只做 ADD）；ReMe v0.3 `procedural_summarizer.py` | 不做 | 容量修好后这个理由不成立；现有 `duplicate_of`/`merged`（`library.py:244-271`）已经覆盖 | — |
| 11 | 常驻的通用条目层（`reach ≥30%` 编进常驻块） | — | 不做 | GMAIL 这类条目恰好是冗余的；没有效果证据 | — |
| 12 | 嵌入检索与去重、LLM 校验、重排与改写、MaTTS、多轮 epoch、ReMem | ACE `bulletpoint_analyzer.py`；ReMe `memory_validation_op.py`、`rerank_memory_op.py`；ReasoningBank `induce_scaling.py` | 不做 | ReMe 去重要嵌入端点（`text-embedding-v4`），网关上没有；ACE 去重要下载模型；在网关上未声明的角色会被拒（`gateway.py:276`）；外部消融的增益在噪声范围内 | — |
| 13 | AWM、Dynamic Cheatsheet 整体重写 | `agent-workflow-memory webarena/induce_prompt.py`；`dynamic-cheatsheet prompts/curator_prompt_for_dc_cumulative.txt` | 不做 | 在 Claude-3.7 的 agent 任务上是负效果。DC 的大增益来自存下来的**代码**，这一点对应到 L3 | — |

---

## 4 Agent：W0-v6 种子（写入守卫 hook）与 Stirrup 的结论

### 4.1 为什么做成 hook，而不是新的 agent 程序

现有的站点已经够用：
- before_tool 返回 block 时，结果是 "Stopped before execution"，同一轮剩下的调用写成 "Not executed"（`default_agent.py:243-257`）。
- on_finish 的 pushback 受 `MAX_PUSHBACKS=2`（:38）和 `_turns_left` 限制，最后一轮不触发（:225-236）。
- 模块在一次尝试里只加载一次（`_Bundle.entry`，:72-86），所以 hook 模块里可以保存状态。
- `ctx.view["events"]` 包含包工具内部发出的 broker 调用，在 worker 里也会就地刷新（`worker_runner.py:20-21`）。

做成 hook 的好处：
- `default_agent.py` 逐字节不变，slim/1 的 `tests/fixtures/attempt_behaviour_v3.json` 不受影响。
- 每个组件可以用 manifest 的 `enabled` 单独开关。
- R 可以按 L4a 编辑它们。

要注意的限制：
- view 只保留最近 20 个事件（`gateway.py:45`），所以台账要在每次 after_tool 按 `call_id` 累积。
- hook 只看到结果的前 4,000 字符（`default_agent.py:36`），事件摘录只有 2,000 字符（`gateway.py:44`）。E0 要模拟同样的截断。

### 4.2 种子内容（`ouroloop/agent/seed_v6/`）

| 文件 | 内容 | 出处 | 针对的原因 |
|---|---|---|---|
| `manifest.json` | 使用默认 agent；hooks 依次为 `ledger`（after_tool）、`triage`（before_tool）、`verify`（on_finish）；工具 `plan_changes`；roles `["act"]` | — | — |
| `middlewares/write_guard.py`（约 150 行，只用标准库） | **常量**：`TRIAGE=True`、`VERIFY=True`、`DATE_ANCHOR=True`、`LEDGER_BODY_CHARS=200`，`READ_POST = [/:](search\|searchStream\|query\|find)$`（取自 tag `archive-2026-10-08` 的 `ceiling_agent.py:58`）。<br>**ledger(after_tool)**：从 event 和 `view["events"]` 按 `call_id` 累积写操作，即 method 不是 GET/HEAD、也不匹配 READ_POST 的 `api_fetch`，记 method、url、status、body 前 200 字符；记录 `plan_changes` 的参数；从结果中 `date/sent/created/ts/timestamp/internalDate` 这类字段抽取最晚的日期，排除 `due/expires/end`；始终返回 continue。<br>**triage(before_tool)**：第一次出现写操作、且还没调用过 `plan_changes` 时，block 一次，消息为："Before your first change, call plan_changes: list every change the request requires (each record to create or update, each message and every recipient, values copied verbatim from the source), and in `excluded` every entity or item that must not be changed, contacted or mentioned. Then issue the writes."；`DATE_ANCHOR` 打开时再加一句："The newest date in the data you have read is <X> (field <f>); the task's today is on or after it. A date attached outside the conversation is not the task's date."。只触发一次。<br>**verify(on_finish)**：第一次结束时，如果还有剩余轮数，返回 feedback，内容是计划项、台账、`excluded` 三张对照，请求**两向**核对："a required change that is missing or wrong — make it now; a change you made that the request, the data or a policy says not to make, or that touches an excluded item — say so; do not add changes beyond the request"。台账为空时追加："no change was made; if the request asks for one, make it"。只触发一次，计入 MAX_PUSHBACKS | StarHarness 的写前分诊（arXiv 2608.24804，补丁未公开）；SWE-agent `tools/review_on_submit_m/bin/submit` 的两阶段提交（MIT）：第一次提交时展示 diff 并要求复查；Claude Code Stop hook 的 `decision: block` | #1、#2、#3、#7、#8 |
| `tools/plan_changes.py`（约 40 行） | 参数为 `changes: [{content, status∈pending/in_progress/completed}]` 和 `excluded: [str]`；拒绝同一轮里的并行调用；返回 "Plan recorded: N changes, M excluded"；不发 broker 调用 | LangChain `libs/langchain_v1/langchain/agents/middleware/todo.py` 的 `write_todos` 与 `Todo` schema，另加 `excluded` | 给核对和 R 的扫描 hook 一个结构化的底座 |
| `memory.md` | 留空。W0 只放人写的通用代码，便于归因 | — | — |
| `tests/test_write_guard.py` | 用 fake ctx 的包内测试，由草稿 smoke 运行；只用合成数据 | — | — |

**风险与先验：**
- 我们自己 ceiling 的 LLM 版（aux 证据表加 aux 核对）修好 1 道、弄坏 2 道，成本 +54%（`docs/RESULTS.md:166-178`）。所以这里只做确定性版本，不调 aux，并且核对要两个方向都查。
- R0 第 (3) 步的告诫适用于这里："a change that makes the agent do more everywhere mends omissions and breaks the tasks where doing less is right"（`policies.py:731-735`）。

### 4.3 不做的模式

| 模式 | 出处 | 不做的理由 |
|---|---|---|
| 上下文压缩 | Stirrup `constants.py`（0.7）；deepagents `middleware/summarization.py` | 步数中位 7–9，最多 18，上限 50 |
| 子 agent | Stirrup `Agent.to_tool()`；deepagents `middleware/subagents.py` | 要 aux 角色，任务短 |
| 工具错误重试 | Stirrup `run_tool`；mini-swe-agent `agents/default.py` | 错误率 1.4–3.2%，约 85% 是服务闸门 |
| 剩余轮数提示、finish 工具 | Stirrup | 一次都没碰到上限 |
| LLM 评分核对 | deepagents `middleware/rubric.py` | 要多一次模型调用，ceiling 上是负效果 |

### 4.4 Stirrup：不接，移植模式

| 项 | 接 Stirrup | 移植（本稿） |
|---|---|---|
| 底座本身的增益 | 约 0：原样 Stirrup 是 57.1%；StarHarness 被采纳的 5 个补丁都在工具、提示和 `agent_setup.py` 层 | 同样的模式直接放进 W |
| 成本 | 新镜像（11 个依赖，256 MB 内存有风险，`worker.py:192`）；async 桥 150–250 行；`core/agent.py` 约 2,100 行，会超出 24 文件 / 256 KiB 的限额，R 能改的面反而变小；v2 的学生在 W0 下运行，也会被打破 | 约 190 行，放在 2 个文件里 |
| 何时重新考虑 | R 的候选需要我们循环里没有的能力时 | — |

---

## 5 R 的配套

### 5.1 R0 文本：新常量 `PROPOSE_SESSION_V6`

位置：`policies.py`，放在 :761 之后。v3、v5 的常量逐字节不动。`default_research_set(..., v6=True)`（:774-797）在 v6 下选用它，`session_format(..., v6=True)`（`research/session.py:211`）。

第 (1) 步替换为下面这段，(2)(3)(5)(7) 沿用：

> (1) Start from inputs/causes.json: the failing train attempts grouped by cause (assertion class × what the agent did), each cause with its attempt ids and a `mechanizable` flag. Read raw trajectories for each cause you act on (step 2). A cause that recurs on two or more train tasks and is mechanizable — a computation, a date, a row operation or a comparison of two tables, a list to keep, a check of what this attempt itself wrote — is fixed in code: a package tool (it may write through call_tool), a hook, or a change to middlewares/write_guard.py. For each new or changed package tool, give `checks`: for each failing train task you predict it fixes, the tool calls a correct attempt would make, using the tool. The host runs each script on a fresh world of that task and grades it with the task's own checks, must-not checks included; the check output tells you, per task, pass or fail and the kinds of checks that failed, never their values. A tool needs scripts that call it and pass on at least two failing train tasks. Scripts are checks, not package files. Judgment causes (what a policy allows, whom to tell, where to look) are fixed in memory or skills; for each text rule, name the tool call that shows it was followed (`footprint`). (4') One layer per candidate, at most one change to the agent program per round. A candidate that adds a tool may retire library entries or memory lines it covers (`subsumes`). (6') inputs/tools.json shows each package tool's calls, errors, tasks, and fixes and breaks on the tasks it was called in; inputs/library.json lists the library's entries with their credit; an entry credited with fixes on two or more train tasks whose procedure is mechanizable is a candidate to compile into a tool. plan_changes records the agent's plan and its `excluded` list; a hook may read them from the view.

### 5.2 R 的输入

| 文件 | 内容 | 代码 |
|---|---|---|
| `inputs/causes.json` | 失败的 train 尝试按（断言类 × 行为）聚类；每个簇列出 attempt id、题数和 `mechanizable`。只当索引用，第 (2) 步"读原始轨迹"的要求保留。R0 自己引用的数字（34.6、34.9、50.0）说明只看摘要并不比只看分数好 | 新写 `research/causes.py`，约 120 行，移植 failure-evidence 的 `classify.py`，复用 `assertion_class`（`methods.py:515`）、`behaviour_of` :846、`failure_class` :877 |
| `inputs/tools.json` | 每个包工具的使用、记功和退役候选（§1.6） | 推广 `method_telemetry` :783 |
| `inputs/library.json` | 加上 `when`、`body`、`fixes`、`cited`、`n_uncited`，以及"可编译候选"标记 | `library.py:546-563` 的 `telemetry(full=True)` |
| `inputs/api_errors.json` | 保留，但不再作为第 (1) 步的起点 | `session.py:1071-1080` |

### 5.3 验收档的接线

- `research/drafts.py`：在 kit_check（:192）的位置，K 带 `toolcheck` 表时：
  - 对新增或改动的包工具跑 `toolcheck.script_check`；
  - 对 hook 跑触发回放；
  - 新增静态检查：包内代码不得读 `skills/library/`。
- 脚本检查的次数计入 `max_draft_repairs=3`（`bench.py:1835`）。
- 筛选沿用 FINANCE §5.2、§5.5 中已采纳的项（默认 #11），另外按 §1.2 V3 的规则记功（`research/session.py` :811、:884-894）。

---

## 6 文件级改动清单

从 slim/1 分出 `v6/1`。合入点放在 slim 第 3 批（P3 删除）之前，因为第 3 批要按决策 D2 修改删除清单。

### 6.1 新增

| 文件 | 约多少行 |
|---|---|
| `ouroloop/agent/seed_v6/{manifest.json, middlewares/write_guard.py, tools/plan_changes.py, memory.md, tests/test_write_guard.py}` | 250 |
| `ouroloop/research/toolcheck.py`：`script_check(worker, files, tools, scripts, *, episode_of, attempts)` → `(errors, report)`。新世界 + `Broker(world, read_only=False)` + `Episode.score()`；检查工具是否被调用；读工具做 values_in 计数 | 130 |
| `ouroloop/research/toolcheck_harness.py`：由 `method_kit/reach.py` 迁移，在 worker 里执行脚本与触发回放 | 80 |
| `ouroloop/research/causes.py` | 120 |
| `examples/automationbench/claude-v6-finance.toml`：preset v6，冻结划分 `4fd91eaf9fb8daf5`（同 `claude-v4-finance.toml:60`），effort 为 medium | — |
| `tests/test_seed_v6.py`、`test_preset_v6.py`、`test_library_v6.py`、`test_toolcheck.py`、`test_causes.py` | 400 |

### 6.2 修改

| 文件 | 改动 |
|---|---|
| `ouroloop/research/library.py` | §3 第 1–8 项；`V5_LIBRARY` 字面量 |
| `ouroloop/bench.py` | 1. :1919 之后加 `_v6_preset()`：基于 v3，`library = LibrarySettings(shown_k=2, max_active=40, probation_shown=3, packed=1, title_chars=100, body_chars=1200, avoid_chars=300, failure_distill=1, cite=1, bm25=<E0d>, stream_batch=<D5>)`，加上 `toolcheck` 表、`seed: "agent/seed_v6"`、`max_effort: "medium"`，不带 `methods` 表和 `cost_beta`。<br>2. `PRESET_ONLY_TABLES`（:1921）加 `"toolcheck"`。<br>3. 启动处（:2254）有 seed 时调用 `hp.seed_package`。<br>4. `library_track`（:3138-3320）：分批、failure 分支、引用记功，`shelf_overflow(..., max_chars=…)`（:3233）。<br>5. 在 `reach_fn`（:1342-1366）旁加 `toolcheck_fn()`。<br>6. 参照臂（约 40 行）：在 v6 活动里登记空包的 release（`hp.empty_package`），供 E1 的 dev 读数和 final 的第三臂使用（`FinalizationPlan.release_refs`，:4170、:4281 已经支持每臂一个 release） |
| `ouroloop/agent/harness_package.py` | 在 :601 附近加 `seed_package(objects, directory) -> ref`，用 `build_package(..., Limits())` |
| `pyproject.toml` | package-data 加 `ouroloop/agent/seed_v6/**` |
| `ouroloop/policies.py` | `PROPOSE_SESSION_V6`；`default_research_set(v6=…)` |
| `ouroloop/research/session.py` | `session_format(v6=…)`；R 输入增加 causes、tools、library(full) |
| `ouroloop/research/drafts.py` | toolcheck 接线（:115、:192、:212）；读库的静态检查 |
| `ouroloop/research/methods.py`（D2） | 保留 `replay_events` :325、`assertion_values` :339、`values_in` :360、`reach_check` :370-455（改成 hook 回放用）、`assertion_class` :515、`footprint_score` :531、`must_not_failures` :541、`method_telemetry` :783、`behaviour_of` :846、`failure_class` :877。删除 router、table、learner（:457-514、:547-780）和 `method_kit/` 的其余部分 |
| **不动** | `ouroloop/agent/default_agent.py`、`ouroloop/agent/runner.py` |

### 6.3 测试

| 范围 | 测试 |
|---|---|
| VEL | 1. `shelf_overflow` 在 `PYTHONHASHSEED` 为 0、1、2 的子进程里结果相同。<br>2. 40 条分布在 40 个架名上，全部保持活跃；第 41 条只淘汰已展示 ≥3 次中 utility 最低的；从没展示的条目不被淘汰。<br>3. 打包往返：`library_of(with_library())` 拿回全部活跃条目；块数和每块大小不超限；v5 设置下字节不变。<br>4. `from_json(V5_LIBRARY)` 的行为与 v5 相同。<br>5. 1,100 字符的 body 在 v6 下通过、在 v5 下被拒。<br>6. failure 来源只在两次都失败时调用一次；v5 下不调用。<br>7. `cited_ids` 忽略没展示过的 id；没有引用行时回退到均分。<br>8. 伤害按不同题计数。<br>9. BM25 分数与 S1 的 `replay.py` 在同一组 fixture 上一致。<br>10. 分批：每批写一次 checkpoint；中断后续跑不重复付费；`stream_batch=1` 时与现状相同 |
| 种子 | 1. 分诊只触发一次；被挡的调用没有执行；重发后执行。<br>2. GET 和搜索类 POST 从不被挡。<br>3. 已经调用过 `plan_changes` 时不挡。<br>4. 核对只触发一次，带上计划、台账（含包工具内部的写）和 `excluded`；最后一轮不触发。<br>5. 台账在 view 只剩 20 个事件时仍然完整。<br>6. `plan_changes` 拒绝并行调用。<br>7. 日期抽取跳过 `due/expires`。<br>8. 种子不超过包限额，smoke 通过，seed ref 有金标 |
| toolcheck | 1. 脚本通过时保留；在少于 2 道失败题上通过时拒绝。<br>2. 脚本没有调用该工具时拒绝。<br>3. 写工具新增 must-not 违规时，该题判为失败。<br>4. 返回给 R 的报告里没有断言值。<br>5. hook 的误触发率能算出来。<br>6. 包代码读 `skills/library/` 时被拒 |
| preset | v6 的 K 与 R0 ref 是新金标；v3、v5 的不变 |

---

## 7 实验（finance，划分 `4fd91eaf9fb8daf5`）

**前提：**
- 全程走 claude_code CLI，effort 统一为 medium。
- 开新活动和新预算根。
- 每个付费步骤先得到用户批准，跑完停下分析（规则 3、5）。

**单价按以下数字估：** 每次尝试约 $0.50（v6 多 1–2 轮），8 路并发，每次尝试约 1.5 min。

| 步 | 内容 | 读法与事先登记的停止规则 | 名义费用 | 墙钟时间 |
|---|---|---|---|---|
| 代码 A+B | §6 全部；`pytest -q`；付费前跑全量（含 Docker，规则 8）；提交；在独立检出上 freeze | v3、v5 的金标不变 | $0 | 5–6 h |
| E0 离线 | 只读 train 记录，按 hook 的截断长度模拟。<br>(a) 日期锚相对 `current_time` 的差距分布（finance、support）。<br>(b) 台账重放与 broker 写记录逐条对齐。<br>(c) 用 v6 规则重放 3b 的流：活跃条目数、从没展示就被淘汰的条数、跨种子是否一致。<br>(d) 在 62 对 train 首次命中上比较 BM25 与 TF-IDF 的 AUC。<br>(e) 用 agent 自己选的操作数重算 finance 的"算术"类失败，数出纯算术失手的题数 | (a) 中位差距超过 14 天时，去掉日期锚那句，交用户定是否由宿主告诉日期。(b) 必须 100% 对齐。(c) 活跃条目 ≥25，从没展示就被淘汰的为 0。(d) 在 train 上胜出才开 `bm25`。(e) 纯算术失手 ≥3 道题时，把 calc 写进 R 的编译目标清单 | $0 | 30 min |
| E1 | W0-v6：train 50×1、dev 25×3；同一时间窗跑 W0-旧（空包）dev 25×3。同一时间窗是必须的：CLI 附上的真实日期随运行日变化 | **过程读数**：分诊触发率、`plan_changes` 调用率、核对之后又写了什么以及这些写对应的断言、核对之后新增的 must-not 违规、写操作里出现真实日期的比例（`extract.py` 的 `wallclock_date_in_writes`）、每次轮数与成本。<br>**护栏**：dev 均值 W0-v6 ≤ W0-旧 − 3 题/25 时停下，关掉 triage 和 verify 再读一次 dev（+$38）。3 次对 3 次读数时，差值的 sd 约 1.27 题，所以"低 1 题"不作为停止规则 | ≈ $100 | 40 min |
| E2 | `--phase library`：W_base = W0-v6，VEL v6 流（50 次首次 + 约 28 次重试 + 约 6 次失败提炼 + 约 60 次研究调用），然后快照 gate（37 次 train 筛选 + 25 次 dev + 25 次确认） | 活跃条目 ≥25；从没展示就被淘汰的为 0；丢弃率 <10%；failure 来源的 D 单独报；`cited_rate`；引用记功的秩相关。D 和 P **不与 3b 比**，因为不是同一个域 | ≈ $90 | 分批约 70 min，不分批约 2.5 h |
| E3 | R 跑 1–2 轮，用 `PROPOSE_SESSION_V6`，走 V0–V2a/c，再到筛选、gate、确认 | 第 0 轮如果没有任何工具或 hook 候选到达 V3，就停下，先和用户一起改 R0。读数：V0–V2 各筛掉几个；V2a 通过的题与 V3 实际修复是否一致（检验"中间档有用"） | ≈ $45/轮 | 35 min/轮 |
| E4 | 先提交计划哈希，再在 test 上 25×3 跑三臂（W0-旧、W0-v6、W\*-v6），然后 report | 分开报两种增益：架构增益（W0-v6 − W0-旧）和 RSI 增益（W\* − W0-v6）。finance 的 test 已读过 1 次，在报告里披露 | ≈ $112 | 45 min |
| **合计** | | | **≈ $345–390** | 付费约 4–4.5 h，另加停顿 |

**费用：** 合计略高于已接受的预算默认 #4（每个域 $300–350），多出的部分来自 W0-旧这一臂。

**现实预期：**
- dev 上 3 次对 3 次读数的半宽约 ±2.5 题/25。
- 架构增益估计 +1 到 +3 题/25，主要来自日期和核对。
- 能否做到"像样的成绩"（别人从 45–57% 做到 80% 以上），取决于 E3 里 R 能不能产出过了 V2a 的工具。这一点不作保证。

---

## 8 需要与用户讨论的二开和已定事项的改变

| # | 事项 | 理由与证据 | 原样用开源的替代 | 成本 |
|---|---|---|---|---|
| D1 | **把写入守卫做成 hook，不接 Stirrup**（改变默认 #2） | 见 §4.4：底座增益约 0，接入反而缩小 R 能改的面 | Stirrup 适配层加新镜像 | 移植约 2 h；接 Stirrup 约 1 天，外加镜像 |
| D2 | **方法库不整个删，保留回放与验收核心，推广为 toolcheck**（改变默认 #1） | 用户的主张需要确定性的中间验收。ASI 靠执行验收；我们的新世界加 broker 正好对应 ASI 的 `run_demo.py --action_path`，这是最便宜的接法，不是唯一的接法 | ASI `induce_actions.py` 原样跑不了，它依赖 WebArena 和 BrowserGym；TroVE 的 `select_best_solution` 只选解，不验收工具 | 保留约 400 行，删掉约 1,000 行；P3 的删除清单要改 |
| D3 | **不跑 S2，把移植的改动直接放进 v6**（改变默认 #5，选的是 #5 里写着的替代方案） | ReMe 原样跑要嵌入端点；ACE 发布的 Curator 只做 ADD，去重要下载模型；S1 已经证明 VEL 的问题是我们自己的容量 bug | S2：在宿主侧用各自的 venv 离线跑写入步骤 | 省下 S2 的 $50–180 |
| D4 | VEL 内的容量、记功、引用改动 | 34/35 的退役来自我们自己的 bug | — | 约 150 行 |
| D5 | 流分批（`stream_batch=4`） | 墙钟时间约降为 1/2.5；语义变成每批更新一次库 | 不分批（约 2.5 h） | 约 80 行 |
| D6 | 日期用数据锚定，不由宿主直接告诉 actor 沙箱日期 | 上游 runner 不告诉 agent 日期，直接告诉会偏离基准；数据锚只给下界 | 宿主在提示里写日期（属于基准保真度的问题，由用户定，默认不写） | — |
| D7 | `toolcheck` 的脚本验收、`causes.json`、`subsumes` 压缩、工具记功规则是新写的 | 这是主张的核心机制；ASI 只给了"改写轨迹加执行判分"这个模式，没有可直接调用的库 | 不做压缩，让工具和条目并存 | 约 300 行 |
| D8 | 预算略超默认 #4 | 多了 W0-旧这一臂 | 去掉 W0-旧臂，代价是架构增益无法单独报 | +$40–60 |

---

## 9 风险

| 风险 | 缓解 |
|---|---|
| 分诊和核对弄坏通过的题（ceiling 的教训） | 只用确定性版本、不调 aux；核对两个方向都查；E1 的护栏和过程读数 |
| 每次多 1–2 轮，成本上升 | E1 记录每次成本；超过 +25% 时报告 |
| 日期锚给出错误的下界 | E0a 先用数据决定去留 |
| R 仍然不造工具 | R0 起点换成 `causes.json`；E3 第 0 轮的停止规则 |
| R 写出"按题定制"的工具 | V2a 要求在 ≥2 道失败题上通过；字面量检查；脚本不进包 |
| 把 V2 的计数当成优化目标 | 计数只给 R 看，筛选和准入不按它排序 |
| 与 slim/1 冲突 | slim/1 目前只改了 `tests/`；P3 开工前由用户定 D2 |
| 成绩仍然不够好看 | 分开报两种增益；不承诺 80% |

---

## 10 评审意见的采纳情况与来源

### 10.1 已核实并采纳

**"不要闭门造车"评审：**
- V2 改用 ASI 式脚本验收：已核对 scratch 副本 `asi_induce_actions.py:222-262` 和 :310-335 的判分与回滚逻辑，以及 arXiv 2504.06821 表 3。
- 改正了对 ASI 和 TroVE 的描述。
- `subsumes` 改为由验收决定，不由 R 声明。
- 工具退役用 TroVE 规则。
- 核对的出处改为 SWE-agent。
- 结构化计划 `plan_changes`。
- 表格写工具照上游 `row.py`：已核对 `automationbench/tools/zapier/google_sheets/row.py:174`、:247。
- BM25 改用 Lucene 公式，不加依赖。
- ACE 引用：最终消息不参与判分，已核对 `adapters/automationbench.py:618`。
- `causes.json` 只当索引用。
- 方法库保留 :515-546。
- 对用户主张的修正。

**"不要盲目用开源"评审：**
- 规则 13 要求代码先行，已核对 `PLAN.md:76`、`policies.py:806-813`。
- 标出三项改变已定默认的事项，已核对 `PLAN.md:298`。
- W0-旧臂的代码。
- CLI 会附上真实日期，已核对 `claude_code.py:5-8`。
- calc、表格写工具、发送前扫描移出种子：已抽查 `dig_fin.txt` 里的 invoice_reconciliation、sales_tax、timesheet。
- 删掉 `update_of`。
- BM25 先在 train 上验证。
- 分批每批不超过 4 道。
- 记功改动不作为改进来报。
- 种子做成 hook，已核对 `default_agent.py` :72-86、:225-257，`gateway.py:44-45`，`worker_runner.py:20-21`。
- 核对两个方向都查。
- 种子不占掉可机械化的原因。
- 护栏改为 −3 题。
- E2 不与 3b 比。

### 10.2 驳回或改写

- oss-agent 说"actor 拿不到日期"，与事实不符，已驳回。
- oss-agent 提议的 memory 文字 "There is no system clock"，已删掉。
- oss-memory-b 说退役时从没展示的有 28 条，复核结果是 30 条。

### 10.3 来源

**本仓库**（main @ 8434c03，只读）：
- `ouroloop/agent/{default_agent.py, gateway.py, runner.py, worker_runner.py, harness_package.py}`
- `ouroloop/research/{library.py, methods.py, drafts.py, session.py, method_kit/reference/m_verify.py}`
- `ouroloop/{policies.py, bench.py}`
- `ouroloop/providers/claude_code.py`
- `ouroloop/adapters/automationbench.py`
- `pyproject.toml`
- `docs/PLAN.md`
- tag `archive-2026-10-08` 下的 `examples/automationbench/ceiling/package/agent/ceiling_agent.py`
- `examples/automationbench/workspace-claude-v5-support/rsi/library_stream.json`（只读 train）

**上游**（`~/workspace/AutomationBench/automationbench/`）：
- `schema/world.py:66`
- `domains/sales/_noise.py:3488-3500`
- `tools/zapier/google_sheets/row.py`
- `rubric/assertions/*.py`

**开源**：
- ASI：github.com/zorazrw/agent-skill-induction `asi/induce/induce_actions.py`；arXiv 2504.06821
- TroVE：github.com/zorazrw/trove `run_trove.py`、`utils/select.py`
- Voyager：github.com/MineDojo/Voyager `voyager/agents/skill.py`
- ACE：github.com/ace-agent/ace `ace/prompts/generator.py`、`ace/ace.py`、`ace/core/curator.py`
- ReMe：github.com/agentscope-ai/ReMe @v0.2.0.6 `reme_ai/vector_store/delete_memory_op.py`
- ReasoningBank：github.com/google-research/reasoning-bank `WebArena/prompts/memory_instruction.py`、`WebArena/run.py`
- ExpeL：github.com/LeapLabTHU/ExpeL `agent/expel.py`
- SWE-agent：github.com/SWE-agent/SWE-agent `tools/review_on_submit_m/bin/submit`
- LangChain：github.com/langchain-ai/langchain `libs/langchain_v1/langchain/agents/middleware/todo.py`
- Stirrup：github.com/ArtificialAnalysis/Stirrup `src/stirrup/core/agent.py`、`constants.py`
- StarHarness：arXiv 2608.24804；github.com/ServiceNow/StarHarness

**调研与评审的 scratch**：`/private/tmp/claude-502/-Users-leon-workspace-ouroloop/c9591706-bc88-4f6c-9900-8380bdb28477/scratchpad/mem-agent/`（s1-diagnostics、failure-evidence、oss-memory-a、oss-memory-b、oss-agent、oss-lens）
