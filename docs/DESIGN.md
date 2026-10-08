# ouroloop 架构设计（v1.0，正向设计）

> 写于 2026-10-09。取代 v0.7 的 DESIGN（tag `archive-2026-10-09` 上可读）与 2026-10-08 的三份设计稿（OSS_COMPONENTS_PLAN、MEMORY_AGENT_ITERATION、V0_6_DESIGN，同一 tag）。它们是按需求一项项堆起来的；本文从框架的职责出发，推出 harness、改进器、memory 各自该是什么样，再对应到现有代码。
>
> 用户原话（2026-10-09）："我们的核心代码好像很简单，agent，memory，harness 这些核心组件都只有一个文件……大量的开发工作和代码都用来保障无关紧要的外围系统了，有点本末倒置了。……RSI 框架本身的职责要清楚，就是自动化提升 harness 和训练模型。那么这里的 harness 的设计如何能够完成自动提升的都要有正向的设计。"
>
> 状态：待用户审。审定之前不改代码、不付费。已定的前提（不再讨论）：actor 与 R 经 claude_code CLI 走订阅；各臂统一 effort（medium）；对外数字只来自封存 final；memory 与 agent 底座用开源件（ReMe、Stirrup），我们只写平台层与二开；训练与蒸馏属于框架，在 `ouroloop/train/`；框架不为某个基准定制。

## 0 结论

| 问题 | 结论 |
|---|---|
| 框架的职责 | 两件：**自动提升 harness**；**训练模型并把模型装回 harness**。其余一切（计量、隔离、划分、gate、报告）只是为了这两件事的结果可信，是平台，不是产品 |
| 现状为什么本末倒置 | 核心三件（agent 295 行、memory 640 行、包格式 723 行）是最简实现；外围（`bench.py` 5,360 行加 gate、预算、契约、策略、报告约 5,100 行）是精心打磨的壳。壳保护了钱和结果，但里面没有能被自动改进的结构：组件没有类型，改进器没有诊断、分层、验收阶梯和记功，memory 只在开头检索一次。改进器三次活动提了 12 个候选，没有一个是工具或循环改动（RESULTS），是因为它没有产出工具的路径，不是模型不会 |
| 正向设计的推导 | 要自动改进，harness 必须是**有类型的组件图**：每种组件有固定位置、改法、$0 验收、记功方式和上限。改进器围绕组件类型工作：从轨迹诊断出原因簇 → 按学到的估值选一层 → 在我们自己的编码会话里实现 → 逐档验收 → 装入 → 按条件记功。memory 是组件之一：固定代码从 train 奖励写，agent 按题读，反复出现又能机械化的条目升级成工具或 hook。模型也是组件之一：训练链产出的模型绑进候选，走同一条阶梯 |
| 开源怎么用 | agent 底座 **Stirrup 0.2.0** 整份 vendor 进 W（`vendor/stirrup/`），我们写适配层把它的扩展点接到宿主契约 ctx；对上游的改动记成补丁栈，升级 = 换上游、重放补丁、跑适配层测试。memory 用 **ReMe 0.3.1.10** 的 procedural memory 算子（提炼、校验、去重、检索、重排），跑在独立 sidecar 进程里；出处、只用 train、留一、记功、退役、快照过 gate 是我们的平台规则 |
| 平台留什么 | 只留三类：**保护钱**（预算根、预留、续跑、硬限额、计量网关）、**防泄漏**（划分冻结、train-only、字面量检查、留一、封存计划哈希）、**保结果正确**（配对 gate 与确认、内容寻址发布、隔离 worker）。其余删 |
| 规模目标 | 框架 30,113 行 → 约 20k：核心（harness + memory + 改进器）从约 2.9k 升到约 9k，平台从约 18k 降到约 7k，`train/` 4.7k 不动 |
| 花费 | 阶段 0～3 都是 $0；第一次付费是阶段 4 的 finance 活动，名义约 $300～480，每一步先批准 |

## 1 职责与边界

**框架的输入：** 一个任务环境（适配器：任务、工具、评分）、一个起点 harness（种子）、一个 pin 住的模型、预算。
**框架的输出：** 一个改进过的 harness 版本 W\*（可部署的包 + 运行壳 + 模型描述），以及证明它更好的封存读数；在第二版，再加一个训练出的模型。

| 职责 | 框架做 | 框架不做 |
|---|---|---|
| 提升 harness | 从自己的运行记录里找原因，改组件，验证，装入，记功，反复 | 不替用户写工作流；不按某个基准定制改法 |
| 训练模型 | 从宿主写的轨迹建数据、训练、绑定、过同一条阶梯 | 不以 loss 或模型大小为目标；不处理教师输出的许可 |
| 让结果可信 | 计量、隔离、划分、gate、封存、报告 | 不做通用实验平台、数据库、队列、远程控制面 |

系统仍记作 S = (W, R, E; K)：W 是 harness（可演化的包），R 是改进器，E 是宿主写的证据，K 是固定核心。四者的关系没变；变的是 W 有了类型结构，R 有了完整机制，K 收缩到三类。

```
                 ┌──────────── K：平台（固定）────────────┐
 环境适配器 ──→  │ 网关·broker·worker·预算·划分·gate·发布 │ ──→ E：轨迹、事件、台账（宿主写，包与 R 写不了）
                 └──────────────┬───────────────┬────────┘
                                │ 运行          │ 证据
                       W：harness 包            R：改进器
                 组件图：agent/tool/hook/…       诊断→分层→实现→验收→装入→记功
                 memory 组件 ⇄ memory sidecar    ↑ 自己的策略也从台账学（C4）
                 model 组件 ⇄ train/ 训练链 ─────┘
```

## 2 Harness（W）：有类型的组件图

### 2.1 组件类型

一个 harness 是一个包目录，内容寻址，每种组件有固定位置。改进器只能按类型改；K 规定每种类型允许与否和上限。

| 类型 | 位置 | 内容 | 谁写 | $0 验收（§4.6） | 记功条件 | K 的上限 |
|---|---|---|---|---|---|---|
| `agent` | `agent/*.py` | 循环本身：回合、完成协议、上下文管理、子 agent。种子里是 Stirrup 的适配层 | 种子、R | smoke（脚本化模型，找确定性崩溃）、包测试 | 全部尝试 | 文件数、字节数 |
| `vendor` | `vendor/<name>/**` | 原样嵌入的开源程序，带 `PROVENANCE.json` 与 LICENSE | 上游；R 可改，改动记成补丁 | 适配层测试 | 同 agent | 字节数；LICENSE/PROVENANCE 不可改 |
| `tool` | `tools/<name>.py` + manifest 的 schema | 确定性代码，模型可调用：计算、表格比对、核对清单、查询封装。可写（经 broker） | R | 单元测试；**toolcheck**：R 为声称能修的 train 题各给一段调用脚本，宿主在新世界里执行，按真实断言判分 | 该次尝试调用过它 | 个数、schema 深度 |
| `hook` | `middlewares/<name>.py`，站点 before_tool / after_tool / on_finish | 流程约束：写前分诊、提交前核对、发送前扫描、日期锚 | 种子、R | **回放**：对已记录的 train 轨迹回放，在声称的失败题上触发 ≥ 2 道，并报告在通过题上的误触发率 | 该次尝试触发过它 | 个数 |
| `prompt` | `prompt/system.md`、`prompt/finish.md` | 系统提示与完成提示 | 种子、R | 字面量检查 | 全部尝试 | 字数 |
| `skill` | `skills/<name>.md` | 多步程序性文字，按需读 | R | 字面量、可达性（能被 ≥ 2 道来源外 train 题检索到） | 该次尝试读过它 | 个数、字数 |
| `memory` | `memory/`（宿主写）| 验证过的经验条目，按题检索（§3） | 固定代码，只从 train | 出处、字面量、可达性、去重 | 该次尝试引用过该条 | 条数、字节 |
| `model` | manifest 的 `model` 项 | 模型身份与推理配置（effort 钉死） | 种子；训练链（§5） | 训练链收据 | — | effort 上限 |
| `config` | manifest 的 `config` 项 | 数值旋钮：回合数、摘要阈值、检索条数 | R | 在 K 的范围内 | 全部尝试 | 每个键的范围 |
| `test` | `tests/*.py` | 包自己的测试，草稿检查时跑 | R | — | — | 个数 |

设计依据：
- 类型来自失败证据，不是凭空列的。456 次 W0 失败尝试的九类原因里（tag `archive-2026-10-09` 上 MEMORY_AGENT_ITERATION §2.2），可机械化的是：消息里提到被排除项（98 次，before_tool 扫描 hook）、算错漏算（48 次，表格比对工具）、用了真实日期（103 次尝试中 76 次失败，日期锚 hook）、写落错位置（约 24 次，表格写工具）；判断类的留在 prompt / skill / memory。
- 别的改进器怎么分：RRSI 的 K 把 harness 分成 prompt / control_flow / config / context_mgmt / client_tool / skill / memory / subagent 九类并在代码里强制；AutoSaddler 的 θ = prompts + tools + middleware + loop；两者是仅有的在代码里强制类型的系统（SURVEY §6）。我们与之对齐，并给每类配 $0 验收，这是别人没有的。

### 2.2 包、种子与候选

- **包**是不可变对象：每个文件一个对象，manifest 列出各类型的条目，整包一个 ref（现有 `harness_package.py` 的做法保留）。
- **种子**（W0）由 K 点名，从 `ouroloop/harness/seeds/<name>/` 构建：上游源码（vendor）+ 我们的适配层 + 补丁栈 + 种子 hook（只放通用的写入守卫：分诊计划、提交前核对、日期锚；具体工具不放，留给 R 编译，这样"RSI 的增益"与"人写架构的增益"按构造分开）。
- **候选** = 对父包的一组类型化改动，带声明 `{cause_id, kind, site, predicted_affected, trigger}`。固定代码对照 diff 核对声明。字面量检查只扫相对种子改动的行（上游自己的行不扫）。
- **身份**：包 ref 覆盖全部字节；改 vendor 一行也是新 W。

### 2.3 开源底座的嵌入与二开

| 问题 | 做法 |
|---|---|
| 放哪 | 上游整份放 `vendor/<name>/`，不装进镜像：R 读得到、改得了，和 StarHarness 对 Stirrup 的做法一样 |
| 接缝 | 只有一个适配文件 `agent/<name>_agent.py`，把上游的扩展点接到 ctx 的六个名字：Stirrup 的 `LLMClient` → `ctx.model`，`Tool` → `ctx.tool` / 包工具入口 / `ctx.skills`，logger 回调 → `ctx.mark`，用量 → `ctx.view["last_usage"]`。上游不知道宿主的存在 |
| 用上游的什么 | Stirrup：`Agent.run` 循环、finish 工具（必须单独一回合）、剩余回合提醒、按窗口比例的上下文摘要与溢出回退、子 agent（`to_tool`）、系统提示模板。不用：它的 clients（由适配层代替）、code backends（世界只经 ctx.tool）、skills 加载器（skills 只来自包）、web / browser 工具（基准 worker 无网；生产经 egress 代理时另作 tool 组件接入）、cache、SIGINT |
| 二开 | 三种，按侵入程度排序，先用轻的：① 适配层里做（大多数）；② 对上游的补丁，放 `seeds/<name>/patches/NNN-<slug>.patch`，构建种子时重放；③ R 在候选里直接改 vendor 文件，装入后由固定代码抽成补丁追加到补丁栈。每个补丁带原因与证据 ref |
| 升级上游 | 换 `PROVENANCE.json` 指向的版本 → 重放补丁栈 → 跑适配层测试与 smoke。重放失败的补丁作为一个 cause 交给 R 修（框架用自己的改进器维护自己 vendor 的代码） |
| 不做 | 不 fork 上游仓库；不在控制面 import 上游（它只在 worker 里跑） |

### 2.4 宿主契约 ctx（运行壳的接缝）

包里的代码只认这六个名字，宿主保证它们在 worker 里和在生产里语义相同：

| 名字 | 做什么 | 记录 |
|---|---|---|
| `ctx.model(messages, tools, role, effort)` | 经网关调 pin 的模型；effort 超上限截顶；role ∈ {act, aux} | 每次一条 gateway_call |
| `ctx.tool(name, args, read_only)` | 经 broker 调环境工具；权限、拒绝表、硬限额都在 broker | 每次一条 broker_event |
| `ctx.skills` | 只读的 skill 索引与正文 | skill_read |
| `ctx.memory.recall(query)` | **新**：按题检索经验条目（§3.3）。做成一个基础工具，所以 hook 与记功对它同样生效 | library_retrieval |
| `ctx.limits` / `ctx.view` | 剩余限额；只读投影（system、base_tools、last_usage） | — |
| `ctx.mark(kind, data)` | 包对自己循环的自述，标 source=package，不是证据 | mark |

包拿不到：评分器、provider、凭据、对象库、结果句柄、任何 dev/test 的信息。

### 2.5 运行环境由 K 指定

worker 的镜像、内存、pid 数、网络策略（`none` 或经 egress 代理的 allowlist）都是 K 的字段，按证据定值，不写死在代码里（现在是 256 MB / 64 pid 写死，v6 分支已改成从 K 读，照用）。生产环境有网络、资源大；基准环境无网、资源小。两者跑同一套包和同一套契约，差别只在 K。

### 2.6 不在可变面内

网关、broker、worker 运行器、评分、划分、预算、gate、发布、R 的代码与 K 本身。这条和 v0.7 相同，原因也相同：候选能碰到的每一面都是它可能学会取分的面（SURVEY §5 第 3、6 条）。

## 3 Memory：W 的一个组件

### 3.1 职责

memory 是**经验的暂存层**：把 train 尝试里验证过的"何时、做什么、避免什么"变成条目，按题给 agent；反复出现、又能机械化的条目，交给改进器升级成工具或 hook。它不是知识库，也不是对话记忆。

### 3.2 写入路径（固定代码，只读 train）

```
train 尝试（基线、首遇、重试）的轨迹（宿主写，按 K 的字节上限裁剪）
  → ReMe 算子：TrajectoryPreprocess → Segmentation → Success/Failure/ComparativeExtraction → Validation → 批内 Deduplication
  → 平台检查：出处（每条带 attempt、task、trace ref）、只用 train、字面量（含 ReMe 元数据）、可达性（when 能检索到 ≥ 2 道来源外 train 题）、与库内去重（同一向量空间）
  → 入库（chroma，一个集合一个业务线），记出处与统计
```

ReMe 自带的记账与删除不用（会丢计数、记功是共享的）；去重只用批内那一半，跟库比由平台做。原因见 V0_6_DESIGN §5.7（tag 上）。

### 3.3 读取路径

- 尝试开始时，宿主按任务文本检索 `shown_k` 条附在系统提示里（现有做法）；
- 尝试中，agent 可调 `recall(query)` 再取（新增）。两者走同一个检索（ReMe MemoryRetrieval，可选 Rerank），同一条留一规则：来源含本题的条目不给（train 上是留一；dev/test 从不是来源）。
- 重写（RewriteMemory）不用：多一次模型调用，没有证据。

### 3.4 记功、退役与容量

| 规则 | 内容 |
|---|---|
| 引用记功 | 首遇尝试的完成理由里有 `library-used:` 一行，点名的条目各记 r − b(t)；没有这一行，同屏条目平分。伤害按不同题计数 |
| 观察期 | 展示满 3 次之前不淘汰 |
| 退役 | 伤害 ≥ 2 题，或效用 ≤ 阈值且展示 ≥ 3 次 |
| 容量 | `max_active`（按证据定，约 500），满了按效用淘汰；不再有 6 架 × 6 条（S1 查出的容量 bug：42 条进库 35 条退役，其中 34 条是被挤出满架） |
| 确定性 | 同分排序用名字作次键，不依赖哈希种子 |

### 3.5 快照过阶梯

库是 W 的一个组件，所以它的快照是一个候选：流跑完后 D ≤ 0 就不付 gate；否则整库快照走 §4.6 的阶梯从 V3 起（V0～V2 在写入时已做）。装入后 dev/test 只见冻结快照。

### 3.6 升级成代码

条目 `fixes ≥ 2`、来源 ≥ 2 道 train 题、诊断标为可机械化 → 作为一个 cause 进入改进器的输入（§4.3），附条目原文。R 实现成 tool / hook 并装入后，条目标 `superseded_by`，不再展示。这是"经验 → 验证过的工具"的机制本身，不是"每条失败都变工具"的规则。

### 3.7 ReMe 的嵌入

| 项 | 做法 |
|---|---|
| 进程 | ReMe 跑在独立 venv 的 sidecar（它自带的 HttpService），宿主只用 httpx。嵌入模型 bge-small（ONNX）本地跑；LLM 调用经我们的 OpenAI 兼容桥到计量过的研究 provider（claude_code），所以 memory 的每次模型调用都记账 |
| 用哪些算子 | summary 下 8 个、retrieve 下 Retrieval / Rerank、dump / load。不用：RewriteMemory、MemoryDeletion、UpdateMemoryMetadata（由平台规则代替） |
| 二开 | 只有一处子类：Deduplication 改成只在批内去重。其余都是平台层：`memory/vel.py`（规则）、`memory/track.py`（写入步，可断点续跑）、`memory/retrieval.py`、`memory/client.py`（sidecar 生命周期） |
| 版本 | 0.3.1.10 钉死（0.4 线删掉了 task memory），锁文件按哈希 |

## 4 改进器（R）：框架的核心机制

### 4.1 一轮的八步

改进器是我们自己设计、自己实现的循环，不交给任何现成编码 agent 的循环。R 的模型经 claude_code CLI，但循环的驱动、工具、上限、输入、验收都是框架的。

| 步 | 输入 | 输出 | 谁做 | 模块 |
|---|---|---|---|---|
| 1 证据 | 本轮 train 尝试的轨迹与事件 | trace ref、可重建的轨迹、全文索引（只限 train） | 固定代码 | `improver/evidence.py` |
| 2 诊断 | 失败的 train 尝试 | 失败案例（证据 ref、原因标签、可机械化与否）→ 跨题聚成**原因簇** `causes.jsonl` | aux 模型调用 + 确定性特征（断言类型、工具错误、终止原因） | `improver/diagnose.py` |
| 3 分层 | 原因簇、台账里各类型的估值 | 本轮每个簇指派的组件类型与名额 | 固定代码（§4.4） | `improver/allocate.py` |
| 4 实现 | 指派、当前包、轨迹读取、检查命令 | ≤ k 个候选目录，各带声明 | R 的编码会话（§4.5） | `improver/session.py` |
| 5 验收 | 候选 | 逐档结果；过 V2 的候选才付费 | 固定代码（§4.6） | `improver/verify/` |
| 6 筛选与 gate | 过 V2 的候选 | train 筛选最优者付 dev gate + 确认 | 固定代码 | `evaluate/` |
| 7 装入与存档 | gate 收据 | 装入主线或拒绝；候选、声明、逐档结果、收据进档案 | 固定代码 | `improver/archive.py`、`evaluate/release.py` |
| 8 记功 | 装入后的 train 读数与事件 | 按条件给组件记功；无功组件生成删除候选；台账更新各类型估值 | 固定代码 | `improver/credit.py`、`improver/ledger.py` |

### 4.2 证据与 trace

宿主已经写下每次模型调用（messages 以前缀计数加尾段）、每次工具调用、每个 mark。证据层只在读取时派生：`<session>:L<line>` 作稳定引用；按 act 调用重建"actor 看到的轨迹"（跨 Stirrup 的摘要）；SQLite FTS5 全文索引，只索引 train，非 train 的 session 连第一行都不给读。v6-gateway 分支的 `trace.py` 就是这个，照用。

### 4.3 诊断与原因簇

- 每个失败尝试产出一条案例：`{attempt, task, evidence_refs, cause, mechanizable, assertion_kinds, tool_errors, termination}`。cause 是稳定的短名（如 `excluded_mentioned`、`arith_mismatch`、`real_date`），由 aux 调用在给定的词表上选，词表可增项但增项要带两条以上案例。
- 簇 = 同 cause 的案例集合，带题数、尝试数、来源 ref。簇是分层和记功的单位；R 只看簇与它的证据，不看 dev/test。
- memory 里满足 §3.6 的条目也进簇（带条目原文）。
- v0.4 的逐题笔记与失败摘要并入案例，不再另有一套。

### 4.4 分层：学出来的，不是写死的

对每个（业务线，cause，组件类型）维护一个估值：装入后在该 cause 的题上的净修复数 / 名义花费，来自台账。本轮给每个簇选估值上界最高的类型；每轮留一个名额给该 cause 上从没试过的类型（探索）。初值一律相同，R 的提示里没有"先做工具"的倾向。估值随活动累积，是 R 自己学习的一部分（§4.8）。

### 4.5 编码会话

| 项 | 内容 |
|---|---|
| 在哪跑 | 模型循环在宿主，文件树在 worker（`/work`：`package/` 只读、`inputs/` 只读、`candidates/<dir>/` 可写）。R 写的任何字节都不是宿主的文件 |
| 输入 | 指派（簇 + 类型 + 名额）、簇的证据 ref、当前包、台账的 train 投影（哪些机制试过、结果如何）、dev 失败类型直方图（只有枚举与计数） |
| 工具 | 文件五个（list / read / write / edit / delete）；`read_trajectory(ref)`；`search_trace(query)`；`run_tests(dir)`；`run_check(dir)`：对候选跑 V0～V2 并返回结果，**R 在 finish 之前就能自己验**；`finish(declaration)` |
| 上限 | 回合、编辑数、读取字节，都是 K 的字段 |
| 产出 | ≤ k 个候选，每个声明 `{cause_id, kind, site, predicted_affected, trigger, differs_from?}`；toolcheck 用的脚本随声明交，只给检查，不进包 |
| 没有的 | shell、网络、宿主文件、dev/test 的任何内容、改自己上限的办法 |

这就是现有 `research/session.py` 的形状，差别是：输入从"失败笔记 + 摘要"换成簇与指派；多了 `search_trace` 与 `run_check`；删掉 playbook 段落以外的方法库相关输入。

### 4.6 验收阶梯

每档过了才花下一档的钱。V0～V2 不调付费模型。

| 档 | 内容 | 适用类型 |
|---|---|---|
| V0 静态 | 包形状、类型是否被 K 允许、上限、相对种子改动行的字面量检查、禁止读 `memory/`、manifest schema | 全部 |
| V1 单元 | 包测试在 worker 里跑；agent 改动另跑 smoke（脚本化模型） | agent、vendor、tool、hook |
| V2a toolcheck | R 为每道声称能修的失败 train 题交一段调用新工具的脚本；宿主在该题的新世界里经可写 broker 执行，用真实断言（含 must-not）判分；要求脚本调用了新工具；≥ 2 道声称题通过。只回传通过与否和失败断言的**类别** | tool |
| V2b 回放 | 对已记录的 train 轨迹回放 hook：声称的失败题上触发 ≥ 2 道；报告通过题上的误触发率 | hook |
| V2c 可达 | when / 标题能检索到 ≥ 2 道来源外 train 题 | skill、memory |
| V3 train 筛 | 付费：声称的失败题 + 固定通过样本；**按条件记修复**（tool 只在调用过的题上记，hook 只在触发过的题上记，文字在足迹出现的题上记）；净翻转最优者进 V4 | 全部 |
| V4 dev gate | 配对、确认复测、`min_tasks`、退步容忍、成本项（现有） | 全部 |
| V5 封存 final | 对外数字只来自这一档 | 发布 |

V2a 只证明"工具正确，而且用它存在可行解"，不证明 actor 会用；后者由 V3/V4 判断（ASI 论文表 3：执行验证带来的增益 +6.4，形式从文字换成程序只多 +1.1）。

### 4.7 装入、存档与反膨胀

- 装入仍是 dev gate 后的 CAS（现有 `release.py`）；父包永远是主线，回滚退到好的祖先。
- 档案保存每个评过的候选：父 ref、声明、逐档结果、收据。它是记功和 R 输入的来源，不是第二条主线（多父、重定基推迟，见 §9）。
- 反膨胀：连续 N 轮零条件记功的组件，由固定代码生成删除候选，走同一条阶梯（V3 起）；gate 的成本项照旧。AutoSaddler 的 Iter 46（挑了 8 个 hook 丢 12.3 点）与 GEPA 的提示 +75% 长度 −2% 都是没有这条的后果。

### 4.8 R 自己的学习（C4）

R 的策略 = 分层估值（§4.4）+ 研究剧本（playbook，台账核对过的教训）。两者都从台账学，都在活动之间延续。递归判据不变：R 的更新经下游 W\* 比较验证，并被下一次改进实际使用。比较仍是跨活动：R1 = R0 + 上一活动的估值与剧本，同划分、同根、同日程，W0 / W\*\_R0 / W\*\_R1 联合 final。

### 4.9 接口（数据形状）

| 名字 | 形状 | 写者 |
|---|---|---|
| Case | `{attempt_id, task_id, cause, mechanizable, evidence_refs[], assertion_kinds[], tool_errors[], termination}` | diagnose |
| Cause | `{cause_id, name, cases[], tasks[], memory_entries[]}` | diagnose |
| Assignment | `{cause_id, kind, slots, exploration: bool}` | allocate |
| Declaration | `{dir, cause_id, kind, site, predicted_affected[], trigger, differs_from?, scripts[]}` | R |
| Verdict | `{candidate_ref, ladder: {V0..V4: {passed, detail}}}` | verify |
| Credit | `{release_ref, component, task_id, conditioned: bool, delta}` | credit |
| Estimate | `{line, cause, kind, fixes, cost, n}` | ledger |

## 5 模型训练在同一回路

- `model` 是 W 的一个组件：manifest 的 `model` 项记身份与推理配置。训练链（`ouroloop/train/`，按 V2_TRAINING_PIPELINE）从宿主写的 act 调用建数据，训出产物 ref；一个候选 W 把它绑进 `model` 项，从 V3 起走同一条阶梯（V0～V2 换成数据检查与链路收据）。
- 顺序不变：演化 → 冻结 release → 在该 release 上采集/训练 → 换学生 → 在学生上重新演化。
- 决策模型（写门等）是同一机制的另一个插件；首个挂点仍是 broker 边界的 `write_gate`。
- 本文不改 V2 的任何决定；只规定它怎么接到 W 上。

## 6 平台（K）：只留三类

| 类 | 留 | 代码（现有 → 目标） |
|---|---|---|
| 保护钱 | 预算根与份额、预留、按 attempt id 续跑、五项硬限额、计量网关、启动检查里的资金项 | `evolve/execution.py` 的预算部分、`ledger.py`、`agent/gateway.py` → `runtime/gateway.py`、`activity/budget.py` |
| 防泄漏 | 划分冻结与哈希、R 与 memory 只读 train、字面量检查、留一、封存 final 的计划哈希先于第一次 test 尝试、R 的工作树在 worker | `benchmark.py`、`harness_package.py` 的字面量部分、`trace.py` 的 split 守卫 → `evaluate/splits.py`、`harness/literals.py` |
| 保结果正确 | 配对 gate 与确认复测、`min_tasks`、退步容忍、成本项、内容寻址对象与 CAS 装入、回滚、隔离 worker 与 broker、报告 | `evolve/task_gate.py`、`task_eval.py`、`release.py`、`agent/worker*.py` → `evaluate/gate.py`、`evaluate/release.py`、`runtime/worker.py`、`activity/report.py` |

删掉（都在 `bench.py`、`policies.py`、`research/` 里）：活动内研究位与 campaign 配对、多领域合并、采集批、段延长、方法库与 `learn` 阶段、v3/v5 preset 的兼容分支、economy / protected / frontier 的残留、PolicySet / PolicyDescriptor / TargetRegistration 一族（W 的提示是包文件，R 的配置是 R 的文件，release 只需钉 `{W ref, R config ref, K ref}`）、`research/campaign.py`、`research/basic.py`、`research/api_errors.py`。

## 7 代码布局与现有代码的对应

```
ouroloop/
  harness/      包模型、组件类型、种子构建、vendor 与补丁栈、字面量检查      ← agent/harness_package.py、v6-agent 分支
    seeds/stirrup/   适配层、写入守卫 hook、补丁栈、PROVENANCE               ← v6 的 seed_v6
  runtime/      网关、broker、worker 与运行器、egress、导出的 App            ← agent/{gateway,worker,worker_runner,runner}.py、app.py、v6-gateway
  memory/       sidecar 客户端、平台规则、写入步、检索                        ← v6-memory 分支（research/library.py 删）
  improver/     evidence、diagnose、allocate、session、verify/、archive、credit、ledger、policy  ← research/{session,drafts,diagnosis,experiment,inspection,context}.py 拆分重写
  evaluate/     划分、gate、final 计划与封存、发布                            ← benchmark.py、evolve/{task_gate,task_eval}.py、release.py
  activity/     阶段编排、预算、config 与 preset、报告                         ← bench.py（5,360 行）拆成约 1,200 行
  adapters/     automationbench                                              不动
  train/        训练链                                                       不动
```

| 现有 | 处理 |
|---|---|
| `agent/default_agent.py` | 删。种子 agent 是 Stirrup 适配层；旧包不再在 HEAD 上跑（规则 15） |
| `research/library.py`、`methods.py`、`method_kit/`、`fixed.py`、`campaign.py`、`basic.py`、`api_errors.py`、`loop.py` | 删 |
| `evolve/contracts.py`、`experience.py` | 保留对象库与 Record；删训练/决策以外没有使用者的契约类型（随 `policies.py` 一族） |
| `policies.py` | 删。release 改钉三个 ref |
| v6 分支 | `v6-agent`（Stirrup 种子、vendor 类型、K 的 worker 资源与 egress）、`v6-gateway`（桥、trace、egress、usage view）、`v6-memory`（sidecar、VEL 规则、写入步）三条都在各自的测试下是绿的，按上面的布局搬入；`v6` 合并分支上未完成的接线（WIP）不用 |
| 测试 | 随代码迁；`test_record_compat`、`test_attempt_behaviour`（夹具换成新种子重录）、`test_paid_run_safety` 与 `test_hot_path_boundary` 必须一直绿 |

## 8 实施计划

每个阶段：先文档（本文的对应节改定稿）→ 代码 → 默认与全量测试 → JOURNAL → 停下等用户。阶段 1～3 各自一个分支，串行合入，$0。

| 阶段 | 内容 | 验收 | 花费 |
|---|---|---|---|
| 0 | 本文；文档清理；PLAN 改写 | 用户审定 | $0 |
| 1 harness 与平台收缩 | `harness/`、`runtime/`、`evaluate/`、`activity/`；Stirrup 种子；K 的 worker 资源与网络；§6 的删除 | 默认与 Docker 全量通过；`quickstart.toml` 的 `--phase smoke --checks-only` 与假上游上的一次尝试（新种子）通过；框架行数 ≤ 22k | $0 |
| 2 memory | `memory/`；sidecar 与锁文件；`recall` 工具；记功与容量规则；快照走阶梯 | 假上游上 `--phase library` 端到端：写入、检索、记功、快照；变异检查在隔离副本 | $0（sidecar venv 要联网安装） |
| 3 改进器 | `improver/` 八步；toolcheck 与回放；分层估值；档案与反膨胀；R 的配置 | 假上游上脚本化的 R 一轮端到端：产出一个 tool 候选，过 V0～V2a，V3 筛选记条件修复；一个 hook 候选过 V2b | $0 |
| 4 实测 | finance，新 preset：基线 → library → 3 轮（每轮后停）→ 有装入则封存 final | 过程检查成立；计划哈希提交早于第一次 test 尝试；报告两次相同 | 名义约 $300～480，每步先批准 |
| 5 训练绑定 | v2.0 离线链对新记录跑通；`model` 组件绑定；之后按 V2 的 v2.1 | V2 §5 的断言 | $0；v2.1 另批 |

## 9 考虑过、不做

| 项 | 为什么不做 |
|---|---|
| 把改进器交给 Claude Code 自己的循环 | 改进器是框架的核心机制，要自己设计实现；用现成循环就无法定义输入、上限、验收与记功（用户 2026-10-09） |
| 多父档案、重定基到任意祖先（AutoSaddler EvoDAG、DGM 档案） | 单领域 3 轮的活动里没有用武之地；先记档案，分支策略等有证据再加 |
| 在尝试中让 ReMe 重写、删除条目 | 多一次模型调用且动库；平台规则代替 |
| 自适应 effort、Max 读数 | 用户已定统一档位 |
| 种子里放具体工具（calc、table_diff） | 把人写的增益和 RSI 的增益混在一起；finance 的"算术"失败多半是比对漏行、规则用错，不是算术本身 |
| 为每条原则加审查或自动检查 | 用户 2026-10-09："不要因为一个原则的加入从而引入过多的无关审查" |
| 方法库、活动内研究位、多领域合并 | 没有产生过证据；C4 已改为跨活动 |

## 10 待用户决定

| # | 事项 | 建议 |
|---|---|---|
| 1 | 本文取代 DESIGN v0.7、OSS_COMPONENTS_PLAN、MEMORY_AGENT_ITERATION、V0_6_DESIGN；modules/ 的十份文档描述现有代码，随阶段 1～3 的代码一起删 | 是 |
| 2 | 阶段 1 的分支从 `m1-automationbench` 起，v6 的三条分支按 §7 搬入，不合并 `v6` 的 WIP | 是 |
| 3 | 第一个实测域 finance（噪声最小，有 v0.4 作参照） | 是 |
| 4 | 阶段 4 的预算根，名义约 $300～480 | 到阶段 4 前批准 |
| 5 | §2.1 的组件类型表与 §4.6 的阶梯是否有要加减的 | — |
