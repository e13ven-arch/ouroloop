# 开源 memory 与 agent 底座：回答与精简试验计划

> **状态：** 草案。2026-10-08 用户审过：§9 第 1 项的 key、第 2 项、第 4 项已定；第 1 项的预算和第 3 项（Stirrup）待定。**日期：** 2026-10-08。
>
> **范围：** 只读阅读代码、文档与公开网页写成。没改其他文件，没跑模型、付费阶段或测试，没读 `.env`、任何 test 内容，也没读 `workspace-claude-v4-support`、`workspace-claude-v5-support` 的记录；support 经验库的状态只取自 docs/JOURNAL.md:25-35。下文 file:line 相对仓库根；外部事实都是 2026-10-08 抓取，另注日期的除外。初稿经过一轮评审，§10 列出改了哪些地方。

## 用户原话（2026-10-08）

> 推理阶段的RSI，不进行训练的情况下已经做到最好了吗？memory部分效果不好的话，有没有可能是我们memory组件设计的有问题呢？可以用开源的memory组件来做实验，直接使用，不用造出更好的memory，除非我们在试验过程中发现了明确需要解决的问题，再基于开源的做二开。包括agent框架也是，我们虽然会rsi agent和harness，但是也可以基于一个不错的开源框架再进行rsi对吧？

> 也有专注于做跨任务的memory吧，如果实在没有的话，那不就是我们的特色吗？也可以参考轻量级的memory设计，来二开我们自己的memory

> 其他人的rsi框架，应该也都是直接引入的别人的agent吧类似引入pi直接作为agent架构，或者在这上面做二开，而不是完全自己开发出来一套agent

> 看一下这两个comment，站在巨人的肩膀上比自己闭门造车更快

## §0 结论一览

| 问题 | 结论 | 关键证据 |
|---|---|---|
| 不训练的推理期 RSI 做到头了吗 | **没有** | 我们三次封存 final 是 +13.0、+4.3、+8.0，区间都宽（docs/RESULTS.md:7-10；docs/history/FINANCE_V04_ANALYSIS_2026-10-07.md:17）。同基准的 StarHarness 在 finance 上 +26.1。官方榜上光 effort 从 XHigh 到 Max 就有 +6.7（§1） |
| memory 效果差是设计问题吗 | **memory.md 是。经验库（下称 VEL）有具体缺口**。另外，外部证据显示，规则或工作流类记忆在 Claude 上不涨甚至掉分；有正证据的是"成功和失败都提炼"以及"检索具体经验"这两类（§2） | finance 那份 memory.md 约 15 条规则，增益只来自 2～3 条，R 还把功劳记错了规则（FINANCE_V04_ANALYSIS:30-33、:282）。VEL 的 42 条入库条目退役了 35 条，每次尝试平均只展示 1.24 条（JOURNAL:28） |
| 有专做跨任务记忆的开源项目吗 | **有，十几个，但没有一个能直接拿来当我们的经验库**。所以跨任务 memory 作为我们的特色，以 VEL 为底座二开，借开源的设计 | 没有项目同时做到：只从打分器验证过的 train 轨迹学、留一读数、逐条记功和退役、过配对 dev gate 并做确认、最后封存 final。每一项单独都有人做过（§3） |
| 别人的 RSI 是不是都引入现成 agent | **是，大多如此**。只有改自己源码的那一支自己写 agent，而且故意写得很小 | Harness-Zero 用 deepagents，StarHarness 用 Stirrup，Live-SWE-agent 用 mini-swe-agent，AutoSaddler、RRSI、Meta-Harness 都从各自基准的参考 agent 起步（§4） |
| 能在开源框架上做 RSI 吗 | **能，首选 Stirrup**。但换底座本身大概率不涨分，收益要看 R 能改哪些东西 | StarHarness 里，Stirrup 原样是 57.1%，加补丁后 83.2%。在 pi 上只做提示词优化的 GEPA (Pi) 比它低 17.6 个点（§4） |

## §1 推理期还有多少余量

| 证据 | 数字 | 读法 |
|---|---|---|
| 我们的 final | v0.1 +13.0 [+2.2, +26.1]；v0.2 +4.3 [−10.9, +19.6]；v0.4 finance +8.0 [−4.0, +22.7]，部分得分 0.843→0.915 | 三次互相在噪声内，平均约 +8 个点（FINANCE_V04_ANALYSIS:220） |
| StarHarness（arXiv 2608.24804；GPT-5.4 medium；Finance-100） | 目标得分 57.1→83.2（+26.1），部分得分 67.3→86.1（+18.8），held-out 上 +29.3。补丁内容：写前先分诊、日期锚定到沙箱时钟、算术和表格交给确定性工具 | 口径不同（目标完成率、单次重复），不能直接比。但能说明一点：大增益来自工具和循环补丁 |
| 官方榜（zapier.com/benchmarks v1.0.6） | Opus 5.5 XHigh 35.77%（$0.89/题），Max 42.47%（$1.44/题）。榜首 Gemini 4 Argon High 51.29% | Medium 29.53%、High 33.03% 是第三方汇总（benchlm，2026-10-01）的数字，官方页这次只列了 XHigh 和 Max 两行，没复核 |
| 我们的天花板实验（全程 effort high，外加 4 个循环机制） | W\* 每次都失败的 8 道题里修好 1 道、弄坏 2 道（docs/RESULTS.md:28-40） | 只靠 effort 加循环检查修不动难题 |
| 失败构成 | 一个完美的"别写"闸门，最多救回 train 失败的 8.1%、dev 失败的 16.0%（docs/design/V2_TRAINING_PIPELINE.md:61） | 其余失败是该写没写或写错。所以收益要靠"写全、写对"，也就是结构化写工具和工作流记忆管的事 |
| R 提了什么 | v0.3/v0.4 的 12 个候选里没有一个是工具或 agent（docs/design/V0_5_PLAN.md:16、:191） | 带写权限的包工具 K 已经允许：普通包工具拿到的是 `read_only=False`（ouroloop/agent/default_agent.py:283），只有 hook（:170）和 `m_` 方法（:281）是只读的，worker 也只在只读时才拒绝写（ouroloop/agent/worker.py:476）。是 R 从没提过，不是 K 不让 |

**不是差距来源，不用做：** 上下文压缩只认 read/bash 两个工具（default_agent.py:42-45），在本基准上从没触发过；20k 的输出截断从没触发过；没有 oracle 的 best-of-n 余量很小（finance pass@1 0.547，oracle pass@3 也只有 0.640）。

## §2 memory：我们的设计和成熟方案差在哪

| 维度 | memory.md | VEL（v0.5 经验库） | 成熟方案 |
|---|---|---|---|
| 单位 | 一段文字，≤8,000 字（ouroloop/agent/harness_package.py:77），同一领域每题都给，不检索 | 条目，最多 6 架 × 6 条（ouroloop/research/library.py:53-54） | ACE：逐条 bullet，带 helpful/harmful 计数；ReMe：条目带"何时用" |
| 写入 | 只有 R 整段改写；一轮最多装 1 个 | 每道有新成功的题提炼 ≤2 条。来源只有成功、对比、重试三种（library.py:33），**失败本身不提炼** | ReMe、ReasoningBank 都会从失败里单独提炼 |
| 检索 | 无 | 只在开头检索一次：用 TF-IDF 在库里自己约 36 条的 title+when 上算分，取 top 3 且分数 ≥0.2。这个阈值没校准过（library.py:121-126、:450；ouroloop/agent/runner.py:139-147；V0_5_PLAN.md:188） | ReMe：嵌入检索 + 可选的 LLM 重排和改写；ReasoningBank：嵌入检索 |
| 记功 | 没有单条记功 | 同一次展示的条目平分 r − b(t)，每题只有一次读数（library.py:274-284）；42 条里退役了 35 条 | ACE 由生成者标出实际用了哪几条；ReMe 被检索 ≥5 次且平均效用 ≤0.5 就删 |
| 学习预算 | 每个活动 1～2 次安装 | 50 题跑一遍后冻结 | ACE 的离线模式可以跑多轮（`--num_epochs`） |
| gate 分辨率 | 25 题 dev | 预期增益 +0.1～+0.35 题/25，远低于 gate 能分辨的量（V0_5_PLAN.md:20） | 多数没有 held-out gate |

**外部证据：在 Claude 上什么样的记忆有用。**

| 来源 | 设置 | 结果 |
|---|---|---|
| ReasoningBank（arXiv 2509.25140，表 1） | WebArena，Claude-3.7-Sonnet，在评测流上在线学习 | 无记忆 41.7，AWM 40.8，ReasoningBank 46.3 |
| Evo-Memory（arXiv 2511.20857，表 2） | 4 个多轮环境，Claude 3.7，在评测流上在线学习 | ReAct 0.57，Mem0 0.50，AWM 0.49，Dynamic Cheatsheet 0.49～0.50，ExpRAG 0.63，ReMem 0.78。增益和题目相似度相关（r = 0.563） |
| MemEvolve（arXiv 2512.18746，表 3） | Flash-Searcher + GPT-5-Mini | 相对无记忆：ExpeL 在 GAIA −3.0、xBench −5.0；AWM −1.8～+2.0；Dynamic Cheatsheet −4.0～+1.8 |

**读法。** 把整段规则或工作流塞给强模型，没有用，甚至有害；这正是 memory.md 的形态。有正证据的是三种做法：同时从成功和失败里提炼、检索具体的过去经验、在尝试中途反复整理记忆。但这些正证据都来自在评测流上在线学习，协议偏乐观；而且 Claude 3.7 比 Opus 5.5 弱。所以我们能期待的增益仍然小，只能靠对照臂来分清是设计问题还是模型上限（§5）。

## §3 跨任务 memory：开源现状与我们的定位

| 项目（版本或日期） | 学习信号 | 记功或删除 | 防泄漏 / held-out | 证据 | 判定 |
|---|---|---|---|---|---|
| ACE（ace-agent/ace，Apache-2.0，1.3k★，2025-11） | 离线模式：在 train 上用 ground truth 学，测试时冻结 | helpful/harmful 计数。官方 Curator 只执行 ADD，UPDATE/MERGE/DELETE 都丢掉（ace/core/curator.py `_add_operations_only`） | 有 train/test 划分，没有 gate | 依赖 sentence-transformers（会带进 torch）、faiss-cpu、together、sambanova（pyproject.toml）。playbook 预算默认 80,000 token | **借设计**；S2 直接跑它的写入路径 |
| ReMe task memory（`reme-ai==0.2.0.6`，2026-01-07） | 从成功、失败、对比三种来源提炼 | 被检索 ≥5 次且平均效用 ≤0.5 就删（α=5，β=0.5） | AppWorld 90 道 train / 168 道 test | Qwen3-8B AppWorld Avg@4 14.97→24.70，BFCL-V3 40.33→45.17（arXiv 2512.10696） | **借设计**；S2 直接跑它的写入路径。注意：0.3.0.0（2026-02-28）起已改成文件记忆，最新 0.4.1.13（2026-10-04） |
| ReasoningBank（google-research/reasoning-bank，Apache-2.0，605★） | 成功和失败都提炼 | 无 | 在评测流上在线学 | Claude-3.7 +4.6（见 §2）。研究脚本，绑定 WebArena/SWE-bench 和 Vertex，README 写明"仅作演示" | **借设计**（从失败提炼） |
| Evo-Memory 的 ExpRAG、ReMem | 在评测流上在线学 | — | 无 | 见 §2 | ExpRAG 要试的话，作为 VEL 的一个模式（这次没核到可直接用的官方代码） |
| AWM | 工作流归纳 | — | — | 两处都低于无记忆 | 不选 |
| ExpeL（EvolveLab 重实现） | 洞见加成功轨迹 | — | — | MemEvolve 表 3 为负；EvolveLab 的版本没有投票操作，不忠于原版 | 暂缓 |
| Dynamic Cheatsheet | 整份改写 | 没有单条来源 | — | Claude 3.7 0.49～0.50 | 不选 |
| AutoGen Task-Centric Memory（实验性） | `train_on_task(task, expected_answer)`，对着答案学 | — | — | 没有任务基准上的数字 | 不选 |
| Memento-Skills（1.5k★） | 从失败重写 skill | 保存前自动生成测试再检查 | — | 和它自己的 agent 框架绑在一起 | 不选 |
| Hermes skill_manage、memU、Acontext、MemOS skill memory、EverOS | 由 agent 自己判断成败 | — | — | 要么是对话基准，要么没有数字（初稿调研） | 不选 |
| Mem0、Zep/Graphiti、Letta、LangMem | 对话和用户画像记忆 | — | — | Mem0 0.50，低于 ReAct 0.57 | 不选 |

**定位。** 能说是我们特色的只有**组合**：只用打分器验证过的 train 轨迹，留一的首遇读数，配对 dev gate 加确认，最后封存 final。单看每一项，都有人做过：ACE 离线学再冻结，ReMe 按效用删除，AutoGen TCM 对着答案学，Memento-Skills 先测试再保存。

**底座选 VEL，不选 ACE。**

- **VEL 已经具备这套协议框架。** library.py 640 行，只用标准库（:22-28），自带留一、记功和退役、literal 检查、快照 gate。
- **ACE 做底座不合适：**
  - 依赖 torch 和 faiss，进不了只依赖 httpx 的宿主核心，也进不了 worker。
  - 默认预算 80k token，而 memory.md 上限是 8,000 字，压缩到这个规模已经不算"直接使用"。
  - 拿 ACE 二开，等于把 VEL 的协议在 ACE 上重写一遍。

**借设计，不搬代码。下表各项都要等 §6 的条件触发后才动：**

| 借自 | 借什么 | 对准 VEL 的哪个缺口 |
|---|---|---|
| ACE | 逐条 bullet；生成者标出实际用了哪几条，计 helpful/harmful；增量 delta 更新 | 同一次展示的条目平分功劳；退役 35/42 |
| ReMe | "何时用"索引；从失败和对比里提炼；α/β 删除规则 | 失败本身不提炼；退役规则 |
| ReasoningBank / ExpRAG | title/description/content 格式；检索具体经验 | 只有短规则。注意示例轨迹里带有 train 世界的字面值，`literal_pass`（library.py:194）会把它们删掉 |

接口方面，EvolveLab 的 `BaseMemoryProvider`（`take_in_memory`、`provide_memory`）只作参考。等某个库胜出、需要自己的读取路径时再定。

## §4 agent 底座

**别人怎么做（初稿调研，2026-10-08）：**

| 系统 | 被进化的 agent | 改进器 |
|---|---|---|
| Harness-Zero | LangChain deepagents 0.7.6。学生核心冻结，只进化 bank | 编码 agent |
| StarHarness | Stirrup（Artificial Analysis）整个 vendor 进来改 | oh-my-pi（pi 的变体） |
| AHE / AutoSaddler / RRSI / Meta-Harness / Live-SWE-agent | 依次是 NexAU；Meta-ARE、SWE-agent、Terminus 2；Terminus-2；Terminus-KIRA；mini-swe-agent | 现成编码 agent |
| DGM、SICA、ADAS、HyperAgents | 自己写，故意写小，好让模型整份读完 | 自身 |

**我们的情况。**

- **W0 已经是这一类的单循环。** 三个基础工具，50 步（examples/automationbench/claude-v5-support.toml:89），没有工具调用就当作结束（default_agent.py:1-24）。上游 runner 默认也是 50 步。
- **W 的载体对得上 Harness-Zero 的 bank。** memory、skills、hooks、tools 四样，和 Harness-Zero"冻结核心 + 可进化 bank"的结构一致。
- **真正自造的部分：**
  - K，这是每个系统都要自己搭的。
  - claude_code 的 JSON 工具传输：`claude -p --tools ""` 下用 `--json-schema` 结构化输出代替原生 tool use（ouroloop/providers/claude_code.py:14、:279-288）。

| 候选 | 判定 | 理由与接法 |
|---|---|---|
| W0 走 API（原生 tool use） | **不做**（用户 2026-10-08："这个还是通过cli的方式来跑"） | actor 继续经 claude_code CLI 走订阅，不开要 API key 的臂。初稿的理由仍然成立：现成的 `ouroloop/providers/anthropic.py` 发原生 `tool_use`/`tool_result`（:17-40、:88），只改传输一个因素，不用改代码。但它要 API key、按 API 真实计费（约 $50），所以删去 |
| **Stirrup**（MIT，0.2.0，2026-08-04，Python ≥3.12） | **第二，由你决定** | 同基准唯一的自改进先例就是在它上面做的。接口薄：`LLMClient.generate`、`Tool`。适配层 `agent/stirrup_agent.py` 放在 W 里，估 150–250 行：`generate` 接 `ctx.model`，`Tool` 接 `ctx.tool`，复用 default_agent 的 `_execute`/`_run_hooks`/`_PackageContext`，所有 ctx 调用用一把锁串行。**需要改代码：**① 配置里不能换镜像，必须新建 preset（ouroloop/bench.py:2016-2019）；② worker 的 256m 内存是默认值，不从 K 读（worker.py:192、:255）。Stirrup 可能要 512 MB，这需要加参数。③ Stirrup 自带的 skill 加载器不能去读 `skills/library/`，否则绕过留一（V0_5_PLAN.md:191）。它自身大概率带来约 0 的增益（StarHarness 的原样基线就是 57.1%），价值在于 R 能改的面更大 |
| Claude Code 原生 | 暂缓，support 两份 final 之后再评估 | 不走 API，原生 tool use 就只能经这一臂测：CLI 照旧走订阅，把我们的三个基础工具做成 MCP server 暴露给 Claude Code，server 把调用转发给我们的 broker。代价：循环、系统提示、传输三样同时变，分不出是哪个起作用；Claude Code 要联网，进不了无网络的 worker；计量只能按会话算，不能按调用；闭源循环，可能有旁路调用；要做的最多（宿主侧新 runner 加计量代理） |
| pi（v1.0.4，TypeScript） | 不选 | 要装 Node 并写一座桥，worker 的 256 MB / 64 pid 很紧。**更正初稿：**StarHarness 的基线里有 "GEPA (Pi)"，也就是在 pi 上做提示词优化，结果比进化后的 Stirrup 低 17.6 个点。可见决定结果的是 R 能改工具和循环，不是选哪个底座 |
| smolagents / mini-swe-agent / OpenHands、Agno、CrewAI、AG2 / deepagents / GEPA | 依次是：暂缓；不选（和 W0 同类）；不选（太重）；留给 v2；作 R 侧对照，暂缓 | — |

## §5 精简试验计划

**原则：**

- 开源库原样跑它的写入路径，读取路径用我们自己的；
- 臂由 S1 的 $0 诊断来挑，不预先定死；
- 每一步都停下来分析；
- 所有代码改动都等 support 两份 final 和 H1 出来之后，在独立 worktree 里做。

| 阶段 | 内容 | 花费（名义） | 停下来分析的问题 |
|---|---|---|---|
| S0（现在） | 本计划交你审；不改任何代码 | $0 | 选的臂对不对 |
| S1（final 和 H1 之后） | ① 读 3b 的检索记录：命中分布、无关命中的比例；抽样读退役的 35 条，看原因。② 算 support train/dev 题目文本的相似度。③ 在 scratch venv 里，用录下的 train 题文本重放检索：TF-IDF 对 MiniLM，命中率零成本可得。④ 写设计文档；合入瘦身第一批。⑤ 核对：冻结的 evaluate 计划能不能对包外构建的包做 dev 读数，不能的话补一条小代码路径 | $0 | 3b 的退役主要是记功噪声，还是条目真的有害；问题出在检索还是内容 |
| S2 memory（1～2 个臂） | 共同输入：3b 流里全部 train 轨迹（对照、带库、重试），已经付过费。每个库在独立 venv 里原样跑写入步骤（ACE 的 Reflector/Curator；ReMe 0.2.0.6 的 task memory 总结），LLM 调用经一个小 shim 走计量过的研究 provider，每个库 $5～25。结果导出成 W 现有的载体：ACE 的 playbook 把预算压到能放进 8,000 字，作为 memory.md，或者作为 VEL 的书架；ReMe 的条目作为 VEL 书架条目，检索不变，这样测的是提炼。留下的臂各跑一次 dev gate，同时并发重读 VEL 快照和 W_base | $50～180 | 有没有臂胜过 VEL；据此决定 §6 的哪一项二开 |
| S3 agent | 你同意 Stirrup 的话（§9 第 3 项），W0′-Stirrup 臂对 W0（claude_code），并发读，dev 25 × 3。原来的 ① W0-API 臂已删（用户 2026-10-08，§4）；原 S3′ 的 Max effort 读数也删了（§9 第 4 项） | 名义约 $33～65 | 换不换底座 |
| S4 RSI | 在选定的底座上跑 R：3 轮，patience 2。R 能改的面里加上可写的结构化工具；底座切换的增益和 RSI 的增益分开报。final 可以放在任何一个开了活动的域（§9 第 2 项，已定） | $300～350 | 底座增益和 RSI 增益各有多少 |
| S5 二开 | 只在 §6 的条件触发时做 | 视情况 | — |

S4 之前合计约 $50～245 名义，没有真实花费：S2 $50～180，你同意 Stirrup 时再加 S3 的 $33～65。用户 2026-10-08 删去 W0-API 臂（约 $50 真实 + $33 名义）和 S3′（约 $59）；删之前是 $200～350 名义加约 $50 真实，初稿是 $670～800。这些都是新的活动，要新开预算根，开之前需要你批准。v0.5 臂的根是 $600，已经用了 $138.83（JOURNAL:31）。如果每个新活动都必须先跑一遍自己的 W0 基线，每个活动再加约 $56（JOURNAL:40-41）。

**不用改代码、现在就能排的杠杆（零成本或很便宜）：**

1. **可写的结构化工具。** K 已经允许（§1）。3c 和 v0.4 臂的 R 轮次正好排在接下来的日程里（JOURNAL:32），先看 R 在那里会不会提出写工具。
2. **effort 杠杆不做（用户 2026-10-08 否决："不要，就保持统一的档位"）。** 原来的第 2 项"自适应 effort，最高到 high"和第 3 项"不做 High 的读数"一并撤下：所有臂保持同一个 effort（现行 medium），K 不让 W 买 effort（§9 第 4 项）。原第 2 项的接法是新配置写 `max_effort = "high"`，R 经 `ctx.model(effort=)` 按调用请求（ouroloop/agent/worker_runner.py:212-213），网关按上限截断（ouroloop/agent/gateway.py:72-77、:279）。

## §6 什么情况下才二开

**总规则：** 至少要有 3 道题的记录指向同一个原因，才二开。能改适配层就先改适配层，改不了再改库。

| 触发条件 | 动作（在 VEL 上） |
|---|---|
| S1 发现 ≥30% 的尝试没检索到条目，或检索到的和题无关；或者 S2 里换检索的臂胜出 | 换嵌入检索。嵌入模型放在无网络的边车容器里跑，属于核心改动 |
| S1 抽样发现，多数退役条目和导致失败的原因无关 | 用 ACE 的"实际用了哪条"标记，取代同屏平分 |
| S2 里 ReMe 臂胜出，或者失败题提炼不出条目 | 加上失败本身的提炼，以及 ReMe 的 α/β 删除规则 |
| 书架超出预算，或者近重复（≥0.8）超过 20% | 合并和删除 |
| 某个开源库做不到只用 train、记来源、过 literal 检查，适配层又补不上 | 二开那一处 |
| W0′-Stirrup 不如 W0，并且原因能定位 | 先改适配层；还是不如，就留在 default_agent |
| R 的候选需要改框架内部 | 把那一个模块搬进包。包放不下（24 个文件 / 256 KiB，harness_package.py:75-76）时，再讨论在 K 上放宽 |

## §7 功效与公平

- **功效。** 按每题差值 sd ≈ 0.44 估算，近似且偏乐观，因为没扣掉题与臂的交互：
  - 95% 半宽：1 次读数约 ±4.3 题，3 次对 3 次约 ±2.5 题。
  - 判定规则"胜过 VEL"：点估计 ≥ +2 题/25，且确认读数同号。各真实效应下通过的概率：

    | 真实效应 | 通过概率 |
    |---|---|
    | 0 | 0.03 |
    | +1 题 | 0.15 |
    | +2 题 | 0.41 |
    | +3 题 | 0.72 |

  - 强模型相对 VEL 的合理增益最多约 +1 题，所以多半是"不确定"。dev 只用来挑，不作主张。
- **对照一致。** 每个臂都和 VEL 快照、W_base 的并发重读比，不复用 3b 的合并对照。3b 用的是 3 次读数 27/75（JOURNAL:29、:41），复用会带进时间漂移。
- **输入一致。** 所有臂用同一批轨迹。ACE 报的增益来自多轮离线学习，而我们只跑 50 题一遍，偏弱；所以零结果不能当作否定了 ACE。
- **S3 的非劣读法要写明。** 规则是点估计 ≥ −1 题。真实差 −1 题时有 50% 会被放过，差 −2 题时有 22%。之所以能接受，是因为封存 final 里 W0 和 W0′ 两个臂都在。

## §8 与当前进度的衔接，以及风险

**衔接**

- **顺序：** 主检出正在跑 support（接下来是 3c 和 v0.4 臂的 3 轮）→ 两份 final 和 H1 → 瘦身第一批 → 本计划的 S1。任何 `.py` 改动都会改 `code_version`，所以在那之前什么代码都不动（JOURNAL:61）。
- **v2：** V2 §7 第 4 项是用户已经接受的设计，规定采集时学生跑在 W0 下（docs/design/V2_TRAINING_PIPELINE.md:663）。换底座就改了这项设计，需要你决定。建议 v2 第一期仍用现在的 W0：第一期只搭链路、不设效果指标（V2_TRAINING_PIPELINE.md:20）。

**风险**

- **泄漏：** 开源库的在线模式要关掉；适配层拒绝非 train 的写入；导出前每条过 `literal_pass`；嵌入只在 train 上用。
- **ReMe 的旧版本：** 0.2 是旧线，要钉死版本、自己 vendor 一份。
- **供应链：** 据初稿调研，litellm 1.82.7 和 1.82.8 在 2026-03-24 被投毒；依赖树里只要有它，就按哈希钉死。同时关掉遥测，HF 设为离线。
- **订阅条款**（初稿调研，随时间变化）：2026-02-19 起，订阅的 OAuth 只能用于 Claude Code 和 claude.ai；2026-06-15 起，程序化使用走单独的月度额度。这对现在的路径同样适用。
- **gate 的成本项：** 检索时额外的 LLM 调用会被成本项惩罚。ReMe 的重排和改写是宿主侧调用，不在 manifest 声明的角色里，网关会拒绝未声明的角色（gateway.py:276）。所以 S2 先关掉重排和改写。

## §9 待你决定

1. **预算和 key。** 预算待定：是否批准新根，约 $50～245 名义（订阅），没有真实花费。key 已定（用户 2026-10-08："这个还是通过cli的方式来跑"）：actor 继续经 claude_code CLI 走订阅，W0-API 臂删去，不需要 API key。原生 tool use 只能经 Claude Code 原生臂测（CLI 加上把三个基础工具暴露给 Claude Code、转发到我们 broker 的 MCP server），暂缓，support 两份 final 之后再评估（§4）。
2. **final 放哪个域。** 已定（用户 2026-10-08 问"不能全跑吗？"，采纳）：finance、support、sales、hr、marketing 都可以跑密封 final，一律按有条件复用规则：构建学生的任何产物都不读之前 final 的逐题记录（由结果库按 split 过滤、代码强制），每份报告披露这份 test 被读过几次。只有 operations 整域从未读过，留作 v2 的 LODO（V2_TRAINING_PIPELINE.md §7 第 2 项）。这取代了原来"sales 和 hr 的 test 留给 v2、marketing 已经用尽"的限制，S2 的赢家和 S4 的封存 final 不必再在"finance/support 上复用"和"只报 dev 或 not_run"之间二选一。费用：在一个新域上开活动，名义约为基线 $56 + 3 轮约 $120 + final 约 $72（订阅），按域分批开。
3. **agent 底座。** 要不要让 R 在 Stirrup 上进化？这需要改代码（新 preset 和镜像、worker 内存参数），在 final 之后、在 worktree 里做。另外请确认：Claude Code 臂暂缓、pi 不选；v2 第一期仍用现在的 W0。待用户决定（已解释影响与实现）。
4. **effort。** 已定（用户 2026-10-08："不要，就保持统一的档位"）：所有臂保持同一个 effort（现行 medium）；不做 Max 读数，不做自适应 effort，K 也不让 W 按成本项买 effort。S3′ 和 §5 的 effort 杠杆随之删去。

## §10 相对初稿的更正

**结论和定位**

- **跨任务 memory 的底座：** ACE 改为 VEL。ACE 依赖 torch/faiss，默认预算 80k token；改为借它的设计。
- **"特色"的说法：** 收窄为"组合"。
- **pi：** 补上 GEPA (Pi) 的结果。

**事实**

- **原生 tool use：** 不需要 Claude Code 臂，现有的 anthropic provider 就能测。（用户 2026-10-08 不要 API 臂，这条不再适用：原生 tool use 只能经 Claude Code 原生臂测，暂缓，§4。）
- **effort：**（用户 2026-10-08 定为统一档位，下面几条都不做，§9 第 4 项）
  - 自适应 effort（最高到 high）不需要改网关，只要一份新配置；
  - Max 读数要改 preset；
  - High 读数不做；
  - 费用约 $59。
- **Stirrup：** 不只是 K 配置的变更，要改代码（新 preset 和镜像、worker 内存参数）。
- **失败构成：** 改用定稿后的写门上限 8.1% / 16.0%。

**范围和预算**

- **S4 的 final 域：** 去掉"候选 sales"（sales 的 test 留给 v2），改为待你决定。（用户 2026-10-08 已定：全部域可跑 final，§9 第 2 项。）
- **memory 臂：**
  - AWM 删去，ExpeL 暂缓（外部证据为负）；
  - S2 去掉 memory.md 臂和 VEL 臂，VEL 改为每个臂的并发对照；
  - 通用边车改为离线跑写入路径，臂按 S1 的诊断来挑。
- **费用：** 补上基线、ReMe 的宿主调用和 S3 的费用，合计从 $670～800 降到 $200～350 名义加约 $50 真实。用户审后删去 W0-API 臂和 S3′，再降到约 $50～245 名义、没有真实花费（§5）。
