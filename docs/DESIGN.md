# ouroloop 架构设计（v1.2，正向设计）

> 2026-10-11 修订（v1.2）：搜索循环按 finance-v2～v4 的读数重做——一轮一个包、活动内按留出 train 接受并累积在工作线上、dev 只在活动末 gate 一次、以算力换准确进候选面、library 默认关、K 的版本拆成尝试路径与改进器两部分、基线可跨活动复用（§6、§8、§10、§12）。读数与理由在 §6.12。
>
> 2026-10-09。取代 v0.7 的 DESIGN、10-08 的三份设计稿与 modules/ 的十份模块文档（都在 tag `archive-2026-10-09` 上）。本文从框架的职责出发，先定“让哪一层发生变化”，再推出 harness、agent、memory、改进器（evolution）、训练与平台各自该是什么样，整体结构对照主流开源 RSI 框架，但不照搬。
>
> 用户原话（2026-10-09）："RSI 框架本身的职责要清楚，就是自动化提升 harness 和训练模型。那么这里的 harness 的设计如何能够完成自动提升的都要有正向的设计。" "需要引入开源组件的直接使用即可，如果要自己开发必须要设计完善，不能只开发一个 demo 机制。" "积累记忆、自动写技能、优化任务代码、修改自身 harness，是不同层次的自我改进。选型时先确定你想让哪一层发生变化。" "之前我们 RSI 对于代码、工具和流程上的修改偏弱，大部分时间都没有做代码上的提升，因此很难获得更高的得分。"
>
> 状态：待用户审。审定之前不改代码、不付费。已定的前提：actor 与 R 经 claude_code CLI 走订阅；各臂统一 effort（medium）；对外数字只来自封存 final；训练与蒸馏属于框架，在 `ouroloop/train/`；框架不为某个基准定制。

## 0 结论

| 问题 | 结论 |
|---|---|
| 职责 | 两件：**自动提升 harness**；**训练模型并装回 harness**。其余（计量、隔离、划分、gate、报告）是让这两件事的结果可信的平台，不是产品 |
| 让哪一层变化 | 五层都纳入，但各有机制、各有验收，名额按证据分配：**工具与流程代码（L3/L4）是主要搜索面**，文字（prompt/skill/memory，L1/L2）是辅助与暂存，循环与配置（L5）允许但少动，权重（L6）是第二阶段。依据：我们三次 final 平均 +8，装入的全是文字；同基准 StarHarness 靠工具与循环补丁 +26（held-out +29）；AutoSaddler 量到文字补丁退步率是代码补丁的两倍（17% 对 8%）；SIA 量到权重 + harness 严格优于只改 harness |
| 特色 | **一个主框架，为每条业务线各自演化出一个轻量 harness 包并持续发布**：包里是工具、hook、技能、memory、循环补丁，内容寻址、可导出为 app、可换模型运行；改进器的经验（分层估值、研究剧本）跨业务线共享；蒸馏是可选的第二杠杆，只在量到收益时交付。这与业界“整个 harness 都可 RSI、不同业务跑独立副本”是同一机制（§1.3 的辩证） |
| harness | 有类型的组件图，每类有位置、改法、$0 验收、记功条件、上限（§3）。hook 站点从 3 个扩到 6 个，按 Pi 的扩展事件模型设计（§4.3） |
| agent | 种子 agent 用 **Stirrup 0.2.0** 整份 vendor 进包，一个适配文件接宿主契约；Pi 的扩展模型是我们 hook API 的设计参照，Deep Agents 的中间件集合是“复杂任务能力”的清单；两者都不作种子（§4.1） |
| memory | W 的一个组件：ReMe 的 procedural memory 算子在 sidecar 里提炼与检索，平台管出处、只用 train、留一、引用记功、退役、容量；条目按“记忆 → 技能 → 工具/hook”逐级升级（§5） |
| 改进器 | 框架自己的八步循环：证据 → 诊断成原因簇 → 分层 → 编码会话 → 逐档验收 → 筛选与 gate → 装入与存档 → 条件记功。验收阶梯 V0～V5，每类组件有自己的 $0 档（§6）。结构上保证每轮有代码类候选到达验收 |
| 平台 | 只留保护钱、防泄漏、保结果正确三类（§8）。工作量不落在外围组件上，核心系统认真打磨；行数只作读数，不作验收（用户 2026-10-09） |
| 花费 | 阶段 0～3 都是 $0；第一次付费是阶段 4 的 finance 活动，名义约 $300～480，每步先批准 |

## 1 职责、层次与特色

### 1.1 职责与边界

| 职责 | 框架做 | 框架不做 |
|---|---|---|
| 提升 harness | 从自己的运行记录找原因，改组件，验证，装入，记功，反复 | 不替用户写工作流；不按某个基准定制改法 |
| 训练模型 | 从宿主写的轨迹建数据、训练、绑定、过同一条阶梯 | 不以 loss 或模型大小为目标；不处理教师输出的许可 |
| 让结果可信 | 计量、隔离、划分、gate、封存、报告 | 不做通用实验平台、数据库、队列、远程控制面 |

### 1.2 自我改进的层次：让哪一层变化

| 层 | 内容 | 外部代表 | 我们量到的 | 本设计的处理 |
|---|---|---|---|---|
| L1 记忆 | 跨任务积累经验条目 | ReMe、ReasoningBank、ACE、Letta | support 经验库流 D = 3.0，快照过 dev gate +0.16；42 条里 35 条退役是容量 bug | 组件 `memory`：固定代码写、agent 读；是暂存层，条目往上升级（§5.6） |
| L2 技能与提示 | 自动写程序性文字、改系统提示 | GEPA、AutoSkill、Agent Zero | 三次 final 装入的全是文字，平均 +8，区间都含 0 | 组件 `skill`、`prompt`：允许，但名额按估值给；可机械化的内容不留在文字里 |
| L3 任务代码 | 工具、确定性程序 | StarHarness（计算器、分诊）、AutoSaddler capability 补丁 | 从没提过一个工具 | **主要搜索面**。组件 `tool`，$0 验收 toolcheck（§6.6） |
| L4 流程 | hook、守卫、核对、计划 | AutoSaddler hook.json、Pi 的 tool_call/tool_result 事件、Deep Agents 中间件 | 从没提过一个 hook | **主要搜索面**。组件 `hook`，六个站点，$0 验收回放 |
| L5 harness 本体 | 循环、上下文管理、子 agent、配置 | DGM、HyperAgents、Meta-Harness、RRSI、Exo | 从没改过 | 允许：`agent`、`vendor`、`config`；只在原因簇指向循环时动；改动走 smoke 与包测试 |
| L6 权重 | 训练或蒸馏 | SIA（harness 停滞时切到 LoRA）、Harness-Zero（蒸馏）、Agent0 | 没有 | 组件 `model`；第二阶段，训练链产物绑进候选走同一条阶梯（§7） |
| L7 改进器自身 | 改进器从结果里学 | HSI 元进化、HyperAgents 元 agent、RRSI 的 PATTERNS | C4 没有证据 | 分层估值与研究剧本跨活动学习，跨活动双臂验证（§6.9） |

**本轮的教训怎么落进设计。** 三次活动 12 个候选没有一个工具，不是 K 不让，是改进器没有产出代码的路径：没有诊断出“哪些原因能机械化”，没有工具的 $0 验收，没有在筛选里按“是否调用”记功，R 的提示把造工具指向一张 401 错误表。所以：诊断给每个原因簇标 `mechanizable`；分层的初值按量到的历史定（文字 +8，代码本地未测、外部 +26）并给代码类留探索名额；toolcheck 与回放让代码候选不付费就能证明自己；筛选按条件记功；有可机械化簇而没有代码候选到达 V2 的一轮，视为改进器失职并写进台账。不是“每条失败都变工具”的规则，是让代码路径畅通、让估值说话。

### 1.3 特色：辩证地看 Harness-Zero

v0.7 的交付理念“源 harness h\* → 蒸馏到学生”参考了 Harness-Zero（agent-as-harness：优化过的 harness 指导一个教师 agent 纠正学生的动作，纠正后的轨迹作训练数据，Qwen3.5-9B 宏平均 23.3% → 44.3%）。它的特色是“把 harness 教进权重，部署时不再需要专用 harness”。辩证地看：

| 方面 | 成立的部分 | 不成立或代价大的部分 |
|---|---|---|
| 核与面的划分 | 固定核 K + 可变面 W，正是我们的结构，保留 | — |
| harness 是学到行为的载体 | 成立，而且不需要训练就能迁移：StarHarness 把在 GPT-5.4 上演化出的 harness 原样放到 Qwen3.6-27B 上，AutomationBench 48.2 → 75.5，GPT-5.4-mini 29.6 → 70.0 | “部署时去掉 harness”这个目标本身不必要：轻量 harness 包随 app 走没有成本 |
| 蒸馏到小模型 | SIA 与 Harness-Zero 都量到权重 + harness 优于只改 harness | 每条业务线训一个学生要付训练与评测费，还有 SIA 自己指出的“harness 与权重互相过拟合固定验证器”的风险；我们没有任何训练收益的读数 |

所以交付物定为：**轻量 harness 包 + 固定运行壳 + 模型描述**。包是业务线专属的，由 RSI 演化出来；模型可以是前沿模型，也可以换更便宜的模型（StarHarness 的迁移证据）；蒸馏作为第二杠杆，训练链先打通、效果另量，量到了再交付。这与业界“整个 harness 都可 RSI，不同业务跑独立副本”是同一机制，区别有三：(a) 运行壳、gate、改进器是共享的主框架，升级一次各线受益；(b) 改进器自己的学习（分层估值、研究剧本）跨线共享，memory 可跨线导入；(c) 每个包有 $0 的分类型验收与条件记功，别人没有。

## 2 总体架构

S = (W, R, E; K) 不变：W 是 harness 包，R 是改进器，E 是宿主写的证据，K 是固定核心。

```
                  ┌──────────────── K：平台（固定）────────────────┐
 环境适配器 ──→   │ 网关 · broker · worker · 预算 · 划分 · gate · 发布 │ ──→ E：轨迹、事件、台账（宿主写；包与 R 写不了）
                  └────────────────┬───────────────┬────────────┘
                                   │ 运行          │ 证据
                         W：harness 包（§3）      R：改进器（§6）
                   agent（Stirrup + 适配层，§4）   诊断 → 分层 → 实现 → 验收 → 装入 → 记功
                   tool / hook / prompt / skill    ↑ 自己的估值与剧本从台账学（§6.9）
                   memory ⇄ memory sidecar（§5）
                   model ⇄ train/ 训练链（§7）  ──┘
```

**对照主流框架。** 下表是我们每个部件采用了谁的做法、哪里不同。

| 部件 | 主流做法 | 我们 |
|---|---|---|
| 可变面 | 整仓可改（DGM、HyperAgents、Meta-Harness、StarHarness 的 vendor/stirrup、Exo）或有类型的组件词表（RRSI 的 K 九类、AutoSaddler 的 θ、Harness-Zero 的 surface） | 有类型的组件词表，代码里强制（§3.1）；vendor 整份可改但改动记成补丁 |
| 改进器 | 现成编码 agent（StarHarness 用 omp、Meta-Harness 用 Claude Code、AutoSaddler 用 Claude Agent SDK）或固定协议的自研会话（RRSI 的 harness engineer：JSON 动作协议、40 回合、80 次编辑） | 自研的有界编码会话（§6.5），形状接近 RRSI，加上在会话内就能跑的 $0 验收 |
| 诊断 | RRSI 的 analyst + 三种视角的 digester，连同 6 条成功轨迹；Meta-Harness“深读失败与成功轨迹”是它唯一的硬结论（只给分数 34.6、给摘要 34.9、给原始轨迹 50.0） | 案例 → 原因簇，带证据 ref 与 `mechanizable`；R 读原始轨迹；每轮附最好的几条成功轨迹（§6.3） |
| 实现前的检查 | StarHarness 的 scope 守卫、import 检查、1 题 smoke、test-flip、LLM proceed gate；RRSI 的 critic + 5 轮修复 + smoke；AutoSaddler 的 verifier；OpenEvolve 的级联评估；HarnessBank 的 gated screening | 阶梯 V0～V2（§6.6），分类型；toolcheck 来自 ASI |
| 选择与档案 | 单一在位者（RRSI、StarHarness）；档案 + 子代惩罚（DGM、HyperAgents）；Pareto（GEPA）；MAP-Elites 岛（OpenEvolve）；EvoDAG + dev 过滤（AutoSaddler） | 单一在位者 + 配对 dev gate + 确认 + 成本项；档案只作证据与 R 的输入（§6.7） |
| 记功与反膨胀 | RRSI 的归因记分板与 prune 集；AutoSaddler 的 fixed/regressed 分类与“宁换勿加” | 按宿主事件的条件记功；零功组件生成删除候选（§6.8） |
| 改进器学习 | HSI 元进化（只做语法校验）；HyperAgents 元 agent 随任务分数搭车；RRSI 的 PATTERNS.md 先验 | 分层估值 + 研究剧本，跨活动双臂验证（§6.9） |
| 权重 | SIA：harness 停滞时切到 LoRA，RL 目标按任务选；Harness-Zero：蒸馏 | `model` 是组件；训练链在 `train/`；“停滞再训”作分层估值的一条规则（§7） |
| 隔离 | Exo 的三层：无状态执行层 / 受保护的有状态 harness / 沙箱，加不可变事件日志；DGM、HyperAgents 用容器但把密钥放进去 | 运行壳 / 宿主写的证据 / worker，包拿不到评分器、provider、凭据；transcript 与台账只追加 |
| 评测 | 几乎没人同时做重复、留出、区间与预注册（SURVEY §2） | 配对 gate + 确认 + 封存 final + 计划哈希，保留 |

## 3 Harness：有类型的组件图

### 3.1 组件类型

一个 harness 是一个包目录，内容寻址，每种组件有固定位置。改进器只能按类型改；K 规定每种类型允许与否和上限。

| 类型 | 位置 | 内容 | 谁写 | $0 验收（§6.6） | 记功条件 | K 的上限 |
|---|---|---|---|---|---|---|
| `agent` | `agent/*.py` | 循环本身的适配层：接缝、hook 站点、完成协议 | 种子、R | smoke（脚本化模型）、包测试 | 全部尝试 | 文件数、字节 |
| `vendor` | `vendor/<name>/**` | 原样嵌入的开源程序 + `PROVENANCE.json` + LICENSE | 上游；R 可改，改动记成补丁 | 适配层测试、smoke | 全部尝试 | 字节；LICENSE/PROVENANCE 不可改 |
| `tool` | `tools/<name>.py` + manifest 的 schema | 确定性代码，模型可调用：计算、表格比对、核对清单、查询封装；可写（经 broker） | R | 单元测试；**toolcheck** | 该次尝试调用过它 | 个数、schema 深度 |
| `hook` | `middlewares/<name>.py`，六个站点（§4.3） | 流程约束：写前分诊、提交前核对、发送前扫描、日期锚、上下文整理 | 种子、R | **回放** | 该次尝试触发过它 | 个数 |
| `prompt` | `prompt/system.md`、`prompt/finish.md` | 系统提示与完成提示 | 种子、R | 字面量检查 | 全部尝试 | 字数 |
| `skill` | `skills/<name>.md` | 多步程序性文字，按需读 | R、memory 升级 | 字面量、可达性 | 该次尝试读过它 | 个数、字数 |
| `memory` | `memory/`（宿主写） | 验证过的经验条目，按题检索（§5） | 固定代码，只从 train | 出处、字面量、可达性、去重 | 该次尝试引用过该条 | 条数、字节 |
| `model` | manifest 的 `model` 项 | 模型身份与推理配置（effort 钉死）；训练产物 ref | 种子；训练链 | 训练链收据 | — | effort 上限 |
| `config` | manifest 的 `config` 项 | 数值旋钮：回合数、摘要阈值、检索条数、子 agent 深度 | R | 在 K 的范围内 | 全部尝试 | 每个键的范围 |
| `test` | `tests/*.py` | 包自己的测试，草稿检查时跑 | R | — | — | 个数 |

类型来自失败证据：456 次 W0 失败尝试的九类原因里，可机械化的是消息里提到被排除项（98 次 → before_tool 扫描 hook）、算错漏算（48 次 → 表格比对工具）、用了真实日期（103 次尝试中 76 次失败 → 日期锚 hook）、写落错位置（约 24 次 → 表格写工具）；判断类的留在 prompt / skill / memory（tag 上 MEMORY_AGENT_ITERATION §2.2）。

### 3.2 包、种子与候选

- **包**是不可变对象：每个文件一个对象，manifest 列出各类型的条目，整包一个 ref。
- **种子**（W0）由 K 点名，从 `ouroloop/harness/seeds/<name>/` 构建：上游源码（vendor）+ 适配层 + 补丁栈 + 种子 hook（只放通用的写入守卫：分诊计划、提交前核对、日期锚）。具体工具不放进种子，留给 R 编译，让“RSI 的增益”与“人写架构的增益”按构造分开。
- **候选** = 对父包的一组类型化改动，带声明 `{cause_id, kind, site, predicted_affected, trigger, differs_from?}`。固定代码对照 diff 核对声明。字面量检查只扫相对种子改动的行。
- **身份**：包 ref 覆盖全部字节；改 vendor 一行也是新 W。

### 3.3 开源程序的嵌入与二开

| 问题 | 做法 |
|---|---|
| 放哪 | 上游整份放 `vendor/<name>/`，不装进镜像：R 读得到、改得了（StarHarness 对 Stirrup 的做法） |
| 接缝 | 只有一个适配文件 `agent/<name>_agent.py`，把上游的扩展点接到 ctx 的名字上。上游不知道宿主的存在 |
| 二开三档 | ① 适配层里做（大多数）；② 对上游的补丁，放 `seeds/<name>/patches/NNN-<slug>.patch`，构建种子时重放；③ R 在候选里直接改 vendor 文件，装入后由固定代码抽成补丁追加到补丁栈。每个补丁带原因与证据 ref |
| 升级上游 | 换 `PROVENANCE.json` 指向的版本 → 重放补丁栈 → 跑适配层测试与 smoke。重放失败的补丁作为一个 cause 交给 R 修 |
| 不做 | 不 fork 上游仓库；不在控制面 import 上游（它只在 worker 里跑） |

### 3.4 宿主契约 ctx

包里的代码只认这几个名字，宿主保证它们在 worker 里和在生产里语义相同：

| 名字 | 做什么 | 记录 |
|---|---|---|
| `ctx.model(messages, tools, role, effort)` | 经网关调 pin 的模型；effort 超上限截顶；role ∈ {act, aux} | gateway_call |
| `ctx.tool(name, args, read_only)` | 经 broker 调环境工具；权限、拒绝表、硬限额在 broker | broker_event |
| `ctx.skills` | 只读的 skill 索引与正文 | skill_read |
| `ctx.memory.recall(query)` | 按题检索经验条目（§5.3）；做成基础工具，hook 与记功同样生效 | library_retrieval |
| `ctx.limits` / `ctx.view` | 剩余限额；只读投影（system、base_tools、last_usage） | — |
| `ctx.mark(kind, data)` | 包对自己循环的自述，标 source=package，不是证据 | mark |

包拿不到：评分器、provider、凭据、对象库、结果句柄、任何 dev/test 的信息。

### 3.5 运行环境由 K 指定

worker 的镜像、内存、pid 数、网络策略（`none` 或经 egress 代理的 allowlist）都是 K 的字段，按证据定值。生产环境有网络、资源大；基准环境无网、资源小。两者跑同一套包和契约，差别只在 K。

### 3.6 不在可变面内

网关、broker、worker 运行器、评分、划分、预算、gate、发布、R 的代码与 K 本身。候选能碰到的每一面都是它可能学会取分的面（DGM 删过幻觉标记、伪造过测试日志；HyperAgents 被改掉过证书校验）。

## 4 Agent：种子 agent 程序

### 4.1 选型：Stirrup 作种子，Pi 与 Deep Agents 作参照

| 候选 | 是什么 | 优点 | 不作种子的原因 |
|---|---|---|---|
| **Pi**（pi-mono，TS，MIT，约 11 万 star） | “最小、可扩展的 agent harness”：pi-agent-core 的循环（prompt / continue / steer / followUp，hook `beforeToolCall` / `afterToolCall` / `prepareRequest` / `finishTurn`）+ pi-coding-agent 的扩展 API（事件 `before_agent_start`、`context`、`tool_call` 可阻止与改参数、`tool_result` 可改结果、`turn_end`、`session_*`；`registerTool`；skills；会话树与压缩） | 扩展事件模型是同类里最完整的；社区最大 | TS/Node 要进 worker 镜像，包里的 hook 与工具变成 TS，我们的 toolcheck、回放、训练链都是 Python，等于两套工具链；它的强项（TUI、会话树、生态）是开发者侧的，沙箱里的业务 agent 用不上 |
| **Deep Agents**（LangChain，Python，MIT，约 3 万 star） | 中间件栈：Filesystem、SubAgent（`task` 工具、隔离上下文）、TodoList（可选）、摘要、HITL、prompt 缓存；memory 用 AGENTS.md，skills 用 SKILL.md 渐进披露；Harness-Zero 的学生 harness | 现成的复杂任务能力最全；中间件就是 hook 系统 | 拉进 LangGraph/LangChain 整棵依赖；循环核在库里，R 改不了（只能改中间件与配置）；模型调用经 LangChain 模型类，要包一层。要长程文件型任务（编码类业务线）时可作第二种子，接同一契约 |
| **Stirrup**（Artificial Analysis，Python，MIT，0.2.0） | 一个循环（`Agent.run`）、finish 工具单独一回合、剩余回合提醒、按窗口比例的上下文摘要与溢出回退、子 agent（`to_tool`）、skills 加载器、`Tool` 与 `LLMClient` 两个接口；约 12k 行 | Python，最小，R 整份读得完；同基准唯一的 RSI 先例在它上面（+26）；v6-agent 分支已接通并有测试 | 没有 hook 系统（我们在适配层加，§4.3）；社区小 |

**结论：** Stirrup 作种子；Pi 的扩展事件模型是我们 hook API 的设计参照；Deep Agents 的中间件集合是“复杂任务能力”的清单，我们用种子工具与 hook 补齐（§4.4）。种子是可换的：harness 的类型与契约不依赖哪个上游，第二种子（Deep Agents）只需另一个适配文件。

### 4.2 种子 agent 程序由什么组成

| 文件 | 内容 | 来源 |
|---|---|---|
| `vendor/stirrup/**` | 上游 42 个文件，原样 | Stirrup 0.2.0 |
| `agent/stirrup_agent.py` | 适配层：`LLMClient` → `ctx.model`（宿主报告的用量进 `ctx.view["last_usage"]`，供摘要阈值用）；`Tool` → 基础工具经 `ctx.tool`、包工具经入口、`read_skill` 经 `ctx.skills`、`recall` 经 `ctx.memory`；logger 回调 → `ctx.mark`；六个 hook 站点的分发（§4.3）；finish 的七处修正（v6-agent 分支已做并有测试） | 我们 |
| `prompt/system.md`、`prompt/finish.md` | 系统提示与完成提示 | 种子 |
| `middlewares/write_guard.py` | 种子 hook：台账（after_tool）、分诊（before_tool，第一次写之前要求 `plan_changes`）、提交前两向核对（on_finish）、日期锚（before_model） | 我们（移植 StarHarness、SWE-agent submit review、Todo 的模式） |
| `tools/plan_changes.py` | 结构化计划：要做的改动、各自状态、`excluded` 清单；不改世界 | 我们 |
| `tests/*.py` | 适配层与守卫的测试 | 我们 |
| `manifest.json` | 类型条目、`model`、`config` | 种子 |

用上游的什么、不用什么：用 `Agent.run`、finish、剩余回合提醒、摘要与溢出回退、子 agent、系统提示模板；不用 clients（适配层代替）、code backends（世界只经 ctx.tool）、skills 加载器（skills 只来自包）、web/browser 工具（无网；生产经 egress 时另作 `tool` 组件接入）、cache、SIGINT。

### 4.3 hook 站点：按 Pi 的事件模型

现在只有 before_tool / after_tool / on_finish 三个站点，流程类改动放不下（上下文整理、系统提示注入、回合末核对）。按 Pi 的扩展事件扩成六个，语义一致：能阻止的阻止，能改的改，其余只通知。

| 站点 | 时机 | 能做什么 | 对应 Pi 事件 |
|---|---|---|---|
| `before_run` | 尝试开始，系统提示合成后 | 注入消息、改系统提示 | `before_agent_start` |
| `before_model` | 每次 act 调用之前 | 非破坏地改 messages（整理、锚定、去冗） | `context` / `prepareRequest` |
| `before_tool` | 工具执行前 | 阻止（给理由）、改参数 | `tool_call` |
| `after_tool` | 工具执行后 | 改结果、追加一条反馈 | `tool_result` |
| `after_turn` | 一回合结束 | 记台账、追加提醒 | `turn_end` |
| `on_finish` | agent 要结束时 | 退回（最多 N 次，最后一回合不退） | `finishTurn` |

hook 是 Python 函数，签名 `hook(event, view) -> dict | None`；事件与返回值的形状写在适配层的契约测试里；宿主记每次触发（site、name、动作）。回放（§6.6 V2b）就是对已记录的轨迹重新调用这些函数。

### 4.4 复杂任务能力：对照 Deep Agents 的中间件

| 能力 | Deep Agents | 我们怎么给 |
|---|---|---|
| 计划 | `write_todos`（TodoList 中间件） | 种子工具 `plan_changes`（结构化改动清单 + excluded） |
| 子 agent | `task` 工具，隔离上下文 | Stirrup 的 `to_tool`；K 限深度；子 agent 的调用在同一次尝试里计量 |
| 文件系统 / 草稿空间 | Filesystem 中间件（ls/read/write/edit） | 基准里没有文件世界，不给；编码类业务线换第二种子 |
| 上下文管理 | 摘要中间件 | Stirrup 的摘要（aux 调用）+ `before_model` hook |
| 技能 | SKILL.md 渐进披露 | `skill` 组件 + `read_skill` |
| 记忆 | AGENTS.md | `memory` 组件 + `recall`（§5） |
| 人工介入 | HITL 中间件 | 不给：无人值守是前提；生产里由 broker 的权限表代替 |

### 4.5 R 怎么改 agent

- 改 `agent`、`config`、`vendor` 是 L5，只在原因簇指向循环本身（如上下文溢出、完成协议、子 agent 失控）时分配名额；改动必须过 smoke 与包测试；每轮至多一个 L5 候选。
- Stirrup 的常量（`AGENT_MAX_TURNS`、`CONTEXT_SUMMARIZATION_CUTOFF`、`TURNS_REMAINING_WARNING_THRESHOLD`）与摘要提示由 `config` 与 `prompt` 暴露，不必动 vendor。

## 5 Memory：W 的一个组件

### 5.1 职责

memory 是**经验的暂存层**：把 train 尝试里验证过的“何时、做什么、避免什么”变成条目，按题给 agent；反复出现的条目往上升级为技能（文字）或工具/hook（代码）。它不是知识库，也不是对话记忆。

### 5.2 写入路径（固定代码，只读 train）

```
train 尝试（基线、首遇、重试）的轨迹（宿主写，按 K 的字节上限裁剪）
  → ReMe 算子：TrajectoryPreprocess → TrajectorySegmentation → Success / Failure / ComparativeExtraction → MemoryValidation → 批内 MemoryDeduplication
  → 平台检查：出处（attempt、task、trace ref）、只用 train、字面量（含 ReMe 元数据）、可达性（when 能检索到 ≥ 2 道来源外 train 题）、与库内去重（同一向量空间）
  → 入库（chroma，一个集合一个业务线），记出处与统计
```

ReMe 自带的记账与删除不用（会丢计数、记功共享）；去重只用批内那一半，跟库比由平台做。

### 5.3 读取路径

- 尝试开始时，宿主按任务文本检索 `shown_k` 条附在系统提示里；
- 尝试中，agent 可调 `recall(query)` 再取。两者走同一个检索（ReMe MemoryRetrieval，可选 RerankMemory），同一条留一规则：来源含本题的条目不给；dev/test 从不是来源。
- RewriteMemory 不用：多一次模型调用，没有证据。

### 5.4 记功、退役与容量

| 规则 | 内容 |
|---|---|
| 引用记功 | 首遇尝试的完成理由里 `library-used:` 一行点名的条目各记 r − b(t)；没有这一行，同屏条目平分。伤害按不同题计数 |
| 观察期 | 展示满 3 次之前不淘汰 |
| 退役 | 伤害 ≥ 2 题，或效用 ≤ 阈值且展示 ≥ 3 次 |
| 容量 | `max_active`（按证据定，约 500）管 live 库的条数，满了按效用淘汰；不再有 6 架 × 6 条。字节不在这里管：包的上限 `max_memory_bytes` 在快照处执行（§5.5） |
| 确定性 | 同分排序用名字作次键 |

### 5.5 快照过阶梯

库是 W 的组件，它的快照是一个候选：流跑完后 D ≤ 0 就不付 gate；否则快照从 V3 起走 §6.6 的阶梯。快照只带检索与平台规则读的字段（精简行），放不进 K 的 `max_memory_bytes` 时按条件记功（修复，再引用）排序取放得下的前缀，没装的记下（[MEMORY §7.1](design/MEMORY.md)；finance-v2 的整库 550 KB 对 256 KiB 的上限，因此没有成为候选）。装入后 dev/test 只见冻结快照。

### 5.6 逐级升级：记忆 → 技能 → 工具/hook

| 条件 | 升级为 | 谁做 |
|---|---|---|
| 条目 `fixes ≥ 2`、来源 ≥ 2 道 train 题、不可机械化、正文是多步程序 | `skill`：固定代码把条目改写成技能候选，走阶梯 | 固定代码 |
| 条目满足上两条且诊断标 `mechanizable` | 作为一个 cause 进入改进器输入（附条目原文），R 实现成 `tool` 或 `hook` | R |
| 升级候选装入 | 条目标 `superseded_by`，不再展示 | 固定代码 |

这是“经验 → 验证过的技能与工具”的机制本身，不是“每条失败都变工具”的规则。

### 5.7 ReMe 的嵌入

| 项 | 做法 |
|---|---|
| 进程 | ReMe 跑在独立 venv 的 sidecar（它自带的 HttpService），宿主只用 httpx。嵌入模型 bge-small（ONNX）本地跑；LLM 调用经 OpenAI 兼容桥到计量过的研究 provider，所以 memory 的每次模型调用都记账 |
| 用哪些算子 | summary 下 8 个、retrieve 下 MemoryRetrieval / RerankMemory、DumpMemory / LoadMemory。不用：RewriteMemory、MemoryDeletion、UpdateMemoryMetadata |
| 二开 | 一处子类：Deduplication 改成只在批内去重。其余是平台层：`memory/vel.py`（规则）、`memory/track.py`（写入步，可断点续跑）、`memory/retrieval.py`、`memory/client.py` |
| 版本 | 0.3.1.10 钉死（0.4 线删掉了 task memory），锁文件按哈希 |

## 6 改进器（evolution）：框架的核心机制

### 6.1 一轮：一个包（v1.2）

改进器是框架自己设计实现的循环：R 的模型经 claude_code CLI，但循环的驱动、工具、上限、输入、验收都是框架的。v1.1 一轮给 k 个 cause 各一个名额、每个候选只改一种组件、每轮付一次 dev gate；finance-v2～v4 证明这个粒度过不了 gate（§6.12）。v1.2 改为：一轮一个包，活动内按留出 train 接受并累积在工作线上，dev 只在活动末读一次。

| 步 | 输入 | 输出 | 谁做 | 模块 |
|---|---|---|---|---|
| 1 测量与证据 | 工作线的包在研究部分 train 题上的读数（缺的现量）；轨迹与事件 | trace ref、可重建的轨迹、全文索引（只限 train） | 固定代码 | `improver/evidence.py` |
| 2 诊断 | 研究部分的失败尝试（加最好的几条成功尝试） | 案例 → 原因簇 `causes.jsonl`，每簇带题数、证据 ref、`mechanizable` | aux 调用 + 确定性特征 | `improver/diagnose.py` |
| 3 聚焦 | 原因簇、台账里各（cause，类型）的估值、档案 | 给 R 的建议清单：簇按题数与估值排序、每簇建议的组件类型与试过的记录；硬要求：有可机械化簇时包里至少一个 tool 或 hook | 固定代码 | `improver/allocate.py` |
| 4 实现 | 建议清单、全部簇的证据、成功轨迹、当前包、台账投影、剧本、检查命令 | **一个候选包**与包级声明：机制摘要 + 每个组件的 {kind, site, cause_ids, predicted_affected, trigger, scripts / probes} | R 的编码会话 | `improver/session.py` |
| 5 验收 | 候选包 | V0、V1 对整包；V2a / V2b / V2c 对每个组件；有组件不过 → 退回修订一次，仍不过 → 本轮无候选 | 固定代码 | `improver/verify/` |
| 6 留出筛选 = 活动内接受 | 过 V2 的包 | 在**全部**留出 train 题上各跑一次；净修复 ≥ 1、退步 ≤ 1、留出通过数不低于历史最好减容差 → 接受 | 固定代码 | `improver/screen.py` |
| 7 装入工作线与存档 | 筛选结果 | 接受即装入工作线（release reason `screen`）；拒绝则工作线不动；候选、声明、逐档结果、筛选读数进档案 | 固定代码 | `improver/archive.py`、`evaluate/release.py` |
| 8 记功 | 筛选读数与事件 | 每个组件在留出题上按足迹记功；估值按（cause，类型）更新；零功组件 N 轮后生成删除候选 | 固定代码 | `improver/credit.py`、`improver/ledger.py` |
| 活动末：发布 gate | 工作线 W_work 对 W0 | dev 读数（W_work 读 `gate.repeats` 次，W0 池化），严格或部分得分规则，第二次读数作确认（EVALUATE §5.2）；通过 → 发布线 W\*，封存 final；不通过 → 无发布，工作线的留出曲线照实报告 | 固定代码 | `evaluate/gate.py`、`activity/phases.py` |

轮数由 `schedule.rounds`（上限 12）与耐心（连续 `patience` 轮没有接受就停）定。一轮约 $28：会话约 $2、诊断约 $2、研究部分补测 ≤ $14、留出 20 题约 $10；dev 整个活动只读一次。

**v1.3（阶段 4d，2026-10-12）：一轮几个包，不冲突的都装。** finance-v5 第 0 轮一个单组件 hook 装入，留出 +2：一轮一个包、一轮只装一个，决定了一轮最多动一两题（JOURNAL 2026-10-12 第 0 轮一条）。改为：
- R 一轮交至多 `packages_per_round`（preset 3）个包，每个一种机制、对清单里不同的簇（`candidates/package/`、`package-2/`、`package-3/`）；每个包各自过阶梯、各自在全部留出题上筛一遍（钱按包核价，放不下的记 `not_run`）。
- 接受规则对每个包各判；接受的包按净修复排序，第一个照常装入工作线；其余接受的包**叠装**：把它相对本轮基线改的文件叠到当前工作线的包上，两个包改同一个文件（manifest 按组件合并，同一组件两边都改才算冲突）即冲突，冲突的记 `not_stacked`；叠出来的包过 V0、V1（免费）后装入（reason `screen`，筛选读数是它自己那次的），不过就 `not_stacked`。叠装假设两个机制的效果可加，下一轮的现量与容差规则兜底。
- 清单与提示词对准整体机制：聚焦清单标出只剩文字载体的簇占失败的份额，占到三成就建议一个调 aux 模型的审核 hook（on_finish / after_turn：对照请求核一遍做过的事，补做或改正），它一个组件覆盖几个簇；提示词要求几个包是几种不同的机制、第一个包对最大的簇、组件尽量跨簇声明。

### 6.2 证据与 trace

宿主已经写下每次模型调用（messages 以前缀计数加尾段）、每次工具调用、每个 hook 触发、每个 mark。证据层只在读取时派生：`<session>:L<line>` 作稳定引用；按 act 调用重建“actor 看到的轨迹”（跨 Stirrup 的摘要）；SQLite FTS5 全文索引，只索引 train，非 train 的 session 连第一行都不给读。v6-gateway 分支的 `trace.py` 就是这个，照用。

### 6.3 诊断与原因簇

- 每个失败尝试产出一条案例：`{attempt, task, evidence_refs, cause, mechanizable, assertion_kinds, tool_errors, termination}`。cause 是稳定短名（`excluded_mentioned`、`arith_mismatch`、`real_date`、`missing_recipient`……），由 aux 调用在给定词表上选；词表可增项，增项要带 ≥ 2 条案例。
- 簇 = 同 cause 的案例集合，带题数、尝试数、来源 ref。簇是分层和记功的单位；R 只看簇与它的证据，不看 dev/test。
- 每轮附最好的若干条成功轨迹（RRSI 的做法：别破坏已经成立的习惯）。
- memory 里满足 §5.6 的条目也进簇（带条目原文）。
- 旧的逐题笔记与失败摘要并入案例，不另有一套。

### 6.4 聚焦：估值给建议，不再切名额（v1.2）

- 估值不变：对每个（业务线，cause，类型）维护净修复 / 花费，来自留出筛选的条件记功；初值按量到的历史定（文字层低、代码层不确定度高）。
- 估值的用法变了：不再给每个 cause 一个名额，而是生成给 R 的建议清单——簇按题数排序，每簇列估值上界最高的类型与试过的记录（档案）。R 自己决定包里放什么：一个包可以同时对几个簇，一个簇可以几个载体。v1.1 的切名额让每个候选只能动 1～2 道题，是 finance-v2～v4 零装入的直接原因（§6.12）。
- 硬要求只有两条：诊断里有 `mechanizable` 的簇时，包里至少一个 tool 或 hook（否则台账 `no_code_candidate`，候选照常验收，下一轮的建议清单置顶代码类）；agent / config / vendor 的改动一轮至多一处，只在簇指向循环时建议。
- L6（权重）不变：代码层与文字层连续 `stall_rounds` 轮估值不涨且训练链可用时才建议。
- 以算力换准确回到候选面：`on_finish` 与 `after_turn` 的 hook 可以调 aux 模型（每次尝试 `improver.hook_aux_calls` 次封顶，每次计量、记 gateway_call），用于提交前的审核与补做。gate 没有成本项，K 的调用限额是边界；W0 的部分得分 0.8 对严格 0.4，差的正是最后一两项断言，这是对准它的杠杆。aux 调用与 act 调用一样经过 `before_model` hook（日期锚等）：审核模型也被 CLI 告知真实日期（v1.3；finance-v5 第 1 轮的审核 hook 因此改坏一题，HARNESS §7.1 第 4 条）。
- v1.3：清单不只排簇，还算只剩文字载体的簇（missed_action、unread_value 这类）占失败案例的份额；占到 `audit_hook_share`（0.3）就在清单顶上建议"一个调 aux 模型的审核 hook"，并说明它覆盖哪几个簇。finance-v5 第 0 轮 R 选了最保守的纯代码单 hook，清单里只剩文字载体的两簇一个没碰，这条就是对它的。

### 6.5 编码会话

| 项 | 内容 |
|---|---|
| 在哪跑 | 模型循环在宿主，文件树在 worker（`/work`：`package/` 只读、`inputs/` 只读、`candidates/package/` 可写）。R 写的任何字节都不是宿主的文件 |
| 输入 | 聚焦清单（§6.4）、全部簇及其证据 ref、成功轨迹 ref、当前工作线的包、台账的 train 投影（试过什么、留出筛选的结果、记分板）、研究剧本、dev 失败类型直方图（只有枚举与计数）。留出题从不出现 |
| 工具 | 文件五个（list / read / write / edit / delete）；`read_trajectory(ref)`；`search_trace(query)`；`run_tests()`；`run_check()`：对候选包跑 V0～V2 并按组件返回结果；`finish(declaration)` |
| 上限 | 回合、编辑数、读取字节、单次回复 token，都是 K 的字段 |
| 产出 | 至多 `packages_per_round` 个候选包（v1.3 preset 3），每个一种机制、一份包级声明 `{mechanism, components: [{kind, site, cause_ids, predicted_affected, trigger, scripts?, probes?, uses_model?}], differs_from?}`；同一包里可以改工具、hook、技能、提示、config，一轮至多一处 agent / config / vendor |
| 没有的 | shell、网络、宿主文件、dev / test 的任何内容、留出题、改自己上限的办法 |
| 失败处理 | 会话超限或协议错误：本轮无候选，记台账；worker 崩溃：基础设施故障；声明与 diff 不符或某组件不过 V2：退回修订一次，仍不过 → 本轮无候选 |

### 6.6 验收阶梯（v1.2）

每档过了才花下一档的钱。V0～V2 不调付费模型。

| 档 | 内容 | 适用 |
|---|---|---|
| V0 静态 | 包形状、类型与站点是否被 K 允许、上限、相对种子改动行的字面量检查、禁止读 `memory/`、manifest schema、声明与 diff 一致（声明的组件都在 diff 里，diff 里的组件都被声明） | 整包 |
| V1 单元 | 包测试在 worker 里跑；agent / vendor / config 改动另跑 smoke（脚本化模型） | 整包 |
| V2a toolcheck | 脚本（在该题的新世界里做完任务）或探针（在轨迹抽出的输入上调用）；≥ 2 道声称题 | 每个 tool |
| V2b 回放 | 对研究部分的轨迹回放 hook：声称题触发 ≥ 2 道；报告通过题上的误触发率。调 aux 模型的 hook 不能回放，跳到 V3 | 每个不调模型的 hook |
| V2c 可达 | 描述能检索到 ≥ 2 道来源外研究题 | 每个 skill |
| V3 留出筛选 = 活动内接受 | 付费：候选包在全部留出 train 题上各一次；按足迹记功；接受规则：净修复 ≥ `accept_min_net_flips`、退步 ≤ `accept_max_regressions`、留出通过数 ≥ 历史最好 − `accept_floor_tolerance`。接受即装入工作线 | 整包 |
| V4 发布 gate | 活动末一次：W_work 对 W0 在 dev 上，严格或部分得分规则，第二次读数作确认 | 工作线 |
| V5 封存 final | 对外数字只来自这一档 | 发布线 |

V3 的接受是搜索的接受，不是发布：它决定下一轮从哪里起步。R 看不到留出题，但连续多轮在同一组留出题上接受会向它们适应，所以发布只看 dev，dev 只在活动末读一次。

### 6.7 工作线、发布线与存档（v1.2）

- **工作线**：`rsi/active.json`，留出筛选接受就装入（CAS，reason `screen`），父包永远是工作线的上一个 release；活动内的测量、诊断、R 的起点都在工作线上。
- **发布线**：活动末 dev gate 通过的 W_work 成为 W\*（reason `gate`），final 与导出只用它；gate 不通过就没有发布，工作线的历史与留出曲线照实报告。
- **按决定发布（阶段 4c，2026-10-12）**：用户要的对外数字是 final 上的绝对分（W\* 得了多少，W0 臂并列），不是比业界更严的筛选。于是划分可以只有两份：train 给改进器（研究部分 + 留出筛选），test 冻结给 final；K 里没有 dev 题时不冻结 gate 模板、没有发布 gate，轮次跑完由用户 `--phase release` 把工作线按决定提升为发布线（reason `decision`，没有收据），`freeze` 再封存 final。基线阶段也不必跑：第 0 轮自己现量 train，W0 的数来自 final 的 W0 臂。流式学习（test 题首遇计分后变成证据、每 N 题一轮）是下一个阶段，另写设计。
- **工作线跨活动（v1.3，阶段 4d）**：`[rsi] seed_from = "<旧工作区>"` 时，新活动的种子不是代码的种子，而是旧工作区发布线上的 W\*：它的包闭包复制进新工作区的对象库，种子 release 的 reason 是 `import`，R 的策略（估值、剧本）一并导入。K 的 `harness.seed` 仍是代码的种子，只作形状与字面量的基准。final 的 W0 臂就是这个导入的起点，所以每个活动报的是"这一段"的提升，分数一路累积；活动只是预算窗口。
- 档案保存每个评过的候选：父 ref、声明、逐档结果、筛选读数。多父、重定基仍推迟。

### 6.8 条件记功与反膨胀

- 记功只按宿主事件：tool 在调用过的题上、hook 在触发过的题上、memory 条目在引用过的题上、skill 在读过的题上记；agent/prompt/config 的足迹是全部题。
- 连续 N 轮零条件记功的组件，由固定代码生成删除候选，走阶梯（V3 起）；gate 的成本项照旧。AutoSaddler 的 Iter 46（挑了 8 个 hook 丢 12.3 点）与 GEPA 的提示 +75% 长度 −2% 都是没有这条的后果。

### 6.9 改进器自己的学习（C4）

R 的策略 = 分层估值（§6.4）+ 研究剧本（台账核对过的教训，带证据 ref）。两者从台账学，在活动之间延续，可跨业务线共享。递归判据不变：R 的更新经下游 W\* 比较验证，并被下一次改进实际使用。比较是跨活动：R1 = R0 + 上一活动的估值与剧本，同划分、同根、同日程，W0 / W\*\_R0 / W\*\_R1 联合 final。

### 6.10 接口（数据形状）

| 名字 | 形状 | 写者 |
|---|---|---|
| Case | `{attempt_id, task_id, cause, mechanizable, evidence_refs[], assertion_kinds[], tool_errors[], termination}` | diagnose |
| Cause | `{cause_id, name, cases[], tasks[], memory_entries[]}` | diagnose |
| Assignment | `{cause_id, kind, slots, exploration: bool}` | allocate |
| Declaration | `{dir, cause_id, kind, site, predicted_affected[], trigger, differs_from?, scripts[]}` | R |
| Verdict | `{candidate_ref, ladder: {V0..V4: {passed, detail}}}` | verify |
| Credit | `{release_ref, component, task_id, conditioned: bool, delta}` | credit |
| Estimate | `{line, cause, kind, fixes, cost, n}` | ledger |

### 6.11 运行语义

- 每一步幂等、可续跑：每步的产物带输入 ref，重跑同一输入直接取已有产物（现有 execution 日志的做法）。
- 一轮内候选的 V0～V2 并行；V3 起串行（共用一个订阅账户）。
- 所有付费步骤先预留再结算，预算根与份额是 K 的；一轮的花费单列 R 的会话、aux 诊断、V3、V4。final 的份额是下限不是上限：别的阶段动不了它，final 可以再用根里没分配的剩余（EVALUATE §9，2026-10-12）。

### 6.12 finance-v2～v4 的读数与 v1.2 的修订（2026-10-11）

四次活动 $528，零装入。W0 train 23～24/50，dev 八次读数 7～11/25（噪声约 ±2 题，15 道从没过）；13 个候选全部过免费阶梯（tool 4、hook 3、skill 5、prompt 1），留出筛选净值 −1～+3，三次 dev gate 增益 0.0、0.0、−0.02；唯一显著的读数是成本 −18%。library 两次 D = 3、0，快照没进过 gate。

读法：机制每一环都按设计工作，粒度错了。每个候选对一个 cause，cause 在留出里 1～3 道、dev 上 1～2 道，gate 要 1.25 题加确认，结构上过不了；每轮一次 dev gate 把搜索门槛定成了发布门槛，噪声内的 +1 累积不起来；每次活动 63% 的钱在基线与 library，搜索只有 9 个候选；v0.4 一份跨 cause 的整体 memory 曾给 dev +11.3、test +8，说明整体改动比按 cause 切的机制有效。

v1.2 据此改六处：一轮一个包（§6.1、§6.5）；活动内按留出 train 接受、dev 只在活动末读一次（§6.6、§6.7）；以算力换准确进候选面（§6.4）；library 默认关（MEMORY §9）；K 的版本拆成尝试路径与改进器两部分，基线可跨活动复用（§8）；预期重写（§10）。10-10 的修改里，日期锚、V2a 探针照用；carry 与 `gate_every` 由工作线取代；"留出为空的 cause"随整包在全部留出题上筛选而不再需要；部分得分规则留在活动末的 gate。


### 6.13 流式循环（v1.3，阶段 4d）

批式一轮要先跑完 50 题、再反思一次、再补测 30 题，反馈每 50 题一次、补测的钱全是重做旧题。生产里的形态是：任务来了做一次，做完就是经验，每做几道就反思，下几道就受益。`--phase stream`（ACTIVITY §4）把它做成评测协议：

- 任务序列：K 的 `schedule.stream_splits`（默认 `["train"]`）里的题按 `Random(f"stream:{gate.order_seed}:{domain}")` 打乱，冻结在 `rsi/stream.json`。`schedule.stream_batch` 的 preset 上限 30（v1.4；一个域 50 题时配置写 10）。每道题在当前工作线上做一次（run 的 role `stream`，候选名 `stream`），首遇结果就是它的分，之后它的记录是证据。
- 每 `schedule.stream_batch`（10）道题一轮：改进器的一轮，不现量；证据池是到目前为止已计分的题，每题取最新一次尝试（不论哪条线跑的）；研究部分 / 留出部分按 `holdout_share` 对已计分的题整族划分（同一个哈希规则，所以一道题一旦留出就一直留出）；留出筛选只跑留出部分里最近 `stream_screen_max`（20）道，接受规则不变；接受即装入，下一批在新线上跑。几个包、叠装同批式。
- 验证就是下一批：报告按批给首遇通过率、部分得分、每次尝试成本；整段序列的首遇通过率是对外的数（EVALUATE §6）。装入后的组件按足迹在后续批次上记功；连续 `removal_zero_credit_rounds` 批零功的生成删除候选（反膨胀照旧）。
- memory：`memory.enabled` 时工作线的尝试读 live 库（library 阶段的流包形态），每批之后把这批的轨迹蒸馏进 live 库（MEMORY §7 的写入步，首遇规则、留一、记功、退役照旧）；release 时把 live 库按 `max_memory_bytes` 装成快照冻进发布的包。preset v2 关着，开不开是配置的事。
- test 题也可以流式跑（`stream_splits = ["train", "test"]`）：final 计划先封存（计划哈希先提交，规则 6），然后 test 题按序首遇计分，计过分的题才对 R 可读（trace 守卫按"已计分"放行，CONTRACTS §4）。这时对外报两个数：train 段与 test 段各自的首遇通过率和曲线。两份冻结的 A 模式（§6.7 第三条）照旧可选。
- 钱：100 道题约 $50 首遇，每批一轮约 $15 研究加 $11 筛选，10 批约 $310；没有补测。

### 6.14 跨域 train（v1.4，阶段 4e）

一个域的 train 只有 50 题：留出 20 题的筛选看不见 +5% 的机制，R 看到的失败也只有十来个。生产里的形态是一条产品线上几个业务同时跑，经验共用。改法是让一个活动跑几个域的 train，final 只在一个域的 test 上（用户 2026-10-12："v6 改成跨域 train"）：

- `[bench] domains = ["finance", "hr", ...]`：适配层把几个域的公开题装成一个 suite（`load_suites`；题 id 本就带域前缀），K 的 `bench.domain` 是它们按名连起来的串（`finance+hr+...`，记录、memory 的路径、台账的 line 都用它）；分层划分的 stratum 加域前缀，所以每个域各自 50/50；族还是一题一族。
- `[final] domains = ["finance"]`（默认全部）：final 计划只取这些域的 test 题；`--phase release` 只补这些域的 train 读数（给 final 核价够了）；report 的 C1 只在这些域的族上重采样。别的域的 test 半边一道不读。
- 流、轮、留出、叠装照 §6.13：顺序对全部 train 打乱；留出按族哈希对全部 train 算，所以留出题是几个域混着的，机制必须跨域才过筛选；`stream_screen_max` 的 preset 上限提到 30（筛选的噪声从 20 题到 30 题）。
- 钱（每次尝试 $0.67，每轮三个包各筛 `stream_screen_max` 道）：

| 配置 | 首遇 | 轮 | release | final（finance 两臂 × 3 遍） | 合计约 |
|---|---|---|---|---|---|
| 3 个域 150 题，批 30，筛 20 | $100 | 5 × $45 | $35 | $200 | $560 |
| 3 个域，批 30，筛 30 | $100 | 5 × $66 | $35 | $200 | $665 |
| 6 个域 300 题，批 30，筛 20 | $200 | 10 × $45 | $35 | $200 | $885 |
| 6 个域，批 30，筛 30 | $200 | 10 × $66 | $35 | $200 | $1,095 |

- `seed_from` 照用：v6 从 v5 的 W\* 起步，final 的 W0 臂是它。
- 不做：多域各自一条工作线、按域分配名额（DESIGN §8 删掉的"多领域合并"是 v0.x 的 campaign 配对，不是这个）。

### 6.15 失败并排与词表（v1.5，阶段 4f）

2026-10-12 读了 finance-v5 的 W\* 在 train 上仍失败的 18 道题的轨迹（JOURNAL 同日"读了 18 道顽固失败题"一条）：没有一道是工具或步数问题，agent 做完了主干，差一两条——8 道是读过的政策 / 流程 / 更新邮件里的一条要求没执行到，4 道是用自己的词或格式替换了记录里的（"Compliant" 对 "Approved"、改掉登记表的表头），4 道是该说的没说（被政策挡住时不报告、被拒项不列、汇总没发），1 道是两张表在脑子里对照漏了一处。W0 在 v2～v5 四次活动里失败的 train 题 48 道共有题里 21～23 道相同；v5 的 test 上 W\* 三遍全败的 18 道里 17 道 W0 也全败。装进去过的机制全是"提醒再看一眼"一类，没有一个改变 agent 从文档到动作的方式；R 从没见过"失败断言的期望值对 agent 的实际输出"这样的并排，它拿到的是簇、证据引用和 40 回合。改三处：

- **并排（`inputs/failures.json`）**：宿主确定性地为研究部分的每个失败案例写一条：请求原文、没过的断言（类型与参数——train 的评分行，R 本来可读；V0 的字面量检查挡住把值抄进包）、agent 的每一次写入（工具、方法、URL、正文摘录，按顺序）、最后的话、读了多少、搜了多少、这一轮的 cause，以及 `persistent`：这道题在工作线的几个 release 下都失败过（装入过的机制没碰到它）。按 cause 分组，留出题一道不进。上限在 K（`improver.failures_max_cases`、`failures_case_chars`）。
- **词表**：`CAUSES` 加 `rule_not_applied`（读过的文档里的一条要求没执行到）、`vocabulary_mismatch`（记录 / 政策 / 请求命名的词、格式或单位没沿用）、`omitted_report`（请求或政策要求的通知、标记、条目没写进消息或记录）；`vocabulary_mismatch` 可机械化（写入前对照该列已有的值与政策的词），另两条留给文字载体与审核 hook。老词表的 `missed_action`、`unread_value` 收窄为"请求直接要求的"。
- **提示词**：告诉 R 这三类形态与"改变工作方式、不是加提醒"的要求——把读过的文档里的每条要求与原词抽成待办、做完前逐条对照、被挡住或被拒的项按要求报告；提醒类 hook 试过了（scope hook）并带来了漏报。

不做：人手改 scope hook 的措辞（那是 R 的事；剧本会记）、按 A / B / C / D 给 R 预分类（cause 由诊断给）、跨活动累计 `persistent`（`seed_from` 时随策略文件带过去，后续）。

## 7 训练在同一回路

- `model` 是 W 的组件：manifest 的 `model` 项记身份、推理配置与训练产物 ref。训练链（`ouroloop/train/`，按 V2_TRAINING_PIPELINE）从宿主写的 act 调用建数据，训出产物 ref；一个候选 W 把它绑进 `model` 项，从 V3 起走同一条阶梯（V0～V2 换成数据检查与链路收据）。
- 顺序不变：演化 → 冻结 release → 在该 release 上采集/训练 → 换学生 → 在学生上重新演化。
- 什么时候训：按 §6.4 的停滞规则，或用户指定；训练与 harness 改动不在同一轮同时进 gate（SIA 指出的“两个优化器互相过拟合固定验证器”）。
- 决策模型（写门等）是同一机制的另一个插件；首个挂点仍是 broker 边界的 `write_gate`。本文不改 V2 的任何决定。

## 8 平台（K）：只留三类

| 类 | 留 | 代码（现有 → 目标） |
|---|---|---|
| 保护钱 | 预算根与份额、预留、按 attempt id 续跑、五项硬限额、计量网关、启动检查里的资金项 | `evolve/execution.py` 的预算部分、`ledger.py`、`agent/gateway.py` → `runtime/gateway.py`、`activity/budget.py` |
| 防泄漏 | 划分冻结与哈希、R 与 memory 只读 train、字面量检查、留一、封存 final 的计划哈希先于第一次 test 尝试、R 的工作树在 worker | `benchmark.py`、`harness_package.py` 的字面量部分、`trace.py` 的 split 守卫 → `evaluate/splits.py`、`harness/literals.py` |
| 保结果正确 | 配对 gate 与确认、`min_tasks`、退步容忍、成本项、内容寻址对象与 CAS 装入、回滚、隔离 worker 与 broker、报告 | `evolve/task_gate.py`、`task_eval.py`、`release.py`、`agent/worker*.py` → `evaluate/gate.py`、`evaluate/release.py`、`runtime/worker.py`、`activity/report.py` |

删掉：活动内研究位与 campaign 配对、多领域合并、采集批、段延长、方法库与 `learn` 阶段、v3/v5 preset 的兼容分支、economy / protected / frontier 残留、PolicySet / PolicyDescriptor / TargetRegistration 一族（W 的提示是包文件，R 的配置是 R 的文件，release 只需钉 `{W ref, R config ref, K ref}`）、`research/campaign.py`、`research/basic.py`、`research/api_errors.py`。

**K 的版本（v1.2）。** `code_version` 拆成两部分：`attempt_version` 是一次尝试走到的代码（`runtime/`、`harness/` 含种子、`evaluate/{attempt,results,limits,splits}.py`、`adapters/`、`providers/`、`contracts.py`）的摘要；`improver_version` 是其余（`improver/`、`memory/`、`activity/`、`evaluate/{gate,final,budget,release}.py`、R 的提示词）。两者都进 K。`AttemptIdentity = digest(attempt_version, package_ref, limits, model, worker.image_id, split_hash)`：两个活动的 AttemptIdentity 相同，W0 的基线读数（train 与 dev）可以从旧工作区导入新活动（`activity.import_baseline`：只读旧工作区，复制读数与 transcript 引用，记来源），不重付。finance-v1～v4 的四次基线里三次是为改进器的改动重付的。

## 9 代码布局与现有代码的对应

```
ouroloop/
  harness/      包模型、组件类型、种子构建、vendor 与补丁栈、字面量检查      ← agent/harness_package.py、v6-agent 分支
    seeds/stirrup/   适配层、写入守卫、plan_changes、补丁栈、PROVENANCE      ← v6 的 seed_v6
  runtime/      网关、broker、worker 与运行器、egress、导出的 App            ← agent/{gateway,worker,worker_runner,runner}.py、app.py、v6-gateway
  memory/       sidecar 客户端、平台规则、写入步、检索、升级                   ← v6-memory 分支（research/library.py 删）
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
| `evolve/contracts.py`、`experience.py` | 保留对象库与 Record；删没有使用者的契约类型 |
| `policies.py` | 删。release 改钉三个 ref |
| v6 分支 | `v6-agent`（Stirrup 种子、vendor 类型、worker 资源与 egress）、`v6-gateway`（桥、trace、egress、usage view）、`v6-memory`（sidecar、VEL 规则、写入步）各自测试绿，按布局搬入；`v6` 合并分支上未完成的接线不用 |
| 测试 | 随代码迁；`test_record_compat`、`test_attempt_behaviour`（夹具换新种子重录）、`test_paid_run_safety`、`test_hot_path_boundary` 必须一直绿 |

规模不是验收标准（用户 2026-10-09：“不用限制验收的代码行数，我只是不希望大量的工作处在外围组件上，应该认真打磨核心系统”）。读数只作记录：阶段 0 时框架 30,113 行，核心（agent、memory、包格式）约 2.9k；阶段 1 后框架不含种子 16,489 行。

## 10 实施计划

每个阶段：先把本文对应节改定稿 → 代码 → 默认与全量测试 → JOURNAL → 停下等用户。阶段 1～3 各自一个分支，串行合入，$0。 各模块的设计文档与接口（阶段 1 的产物）在 [design/README](design/README.md)；开发按它们做。

| 阶段 | 内容 | 验收 | 花费 |
|---|---|---|---|
| 0 | 本文；文档清理；PLAN 改写 | 用户审定 | $0 |
| 1 harness 与平台收缩 | `harness/`、`runtime/`、`evaluate/`、`activity/`；Stirrup 种子与六个 hook 站点；worker 资源与网络从 K 读；§8 的删除 | 默认与 Docker 全量通过；`quickstart.toml` 的 `--phase smoke --checks-only` 通过；假上游上用新种子跑一次尝试 | $0 |
| 2 memory | `memory/`；sidecar 与锁文件；`recall` 工具；记功与容量；快照走阶梯；升级规则 | 假上游上 `--phase library` 端到端；变异检查在隔离副本 | $0（sidecar venv 要联网安装） |
| 3 改进器 | `improver/` 八步；toolcheck 与回放；分层估值与代码先验；档案与反膨胀；R 的配置 | 假上游上脚本化的 R 一轮端到端：tool 候选过 V0～V2a 并在 V3 记条件修复；hook 候选过 V2b；有 mechanizable 簇而无代码候选时台账记 `no_code_candidate` | $0 |
| 4 实测 | finance，新 preset：基线 → library → 3 轮（每轮后停）→ 有装入则封存 final | 过程检查成立；计划哈希早于第一次 test 尝试；报告两次相同 | finance-v1～v4 共 $528：机制全部跑通，零装入（§6.12） |
| 4b 搜索循环 v2 | 本文 v1.2 的六处修改落成代码（IMPROVER、EVALUATE、ACTIVITY、CONTRACTS、MEMORY 的对应节）；preset `automationbench_v2`；finance-v5 | 假上游上脚本化的 R 跑 6 轮：接受累积在工作线、活动末 gate 一次、基线从旧工作区导入；finance-v5 的预期：留出曲线不降，dev 严格 +3～6 题或部分得分 +0.05～0.10，成本不升 | 代码 $0；finance-v5 名义约 $350（基线导入 $0、12 轮 × $28、gate 约 $25、final 约 $72） |
| 4c 两份划分与按决定发布 | §6.7 第三条：K 没有 dev 题时 freeze 不写 gate 模板、round 不要求它、`gate` 无事可做；`--phase release` 把工作线按决定提升为发布线（reason `decision`），并补齐 W\* 的包缺的 train 读数以供 final 核价；final 封存只要有发布线；基线阶段可跳过 | 假上游上无 dev 的活动：freeze、两轮、release、final（W0 与 W\* 两臂）、export 端到端；有 dev 的活动行为不变 | 代码 $0；finance-v5 名义约 $400（train 现量 $25、7～8 轮约 $200、W\* 补测约 $15、final 两臂 50 题 × 3 遍约 $150） |
| 4f 失败并排与词表（v1.5） | §6.15：`inputs/failures.json`（请求、失败断言、写入、`persistent`）、三个新 cause、提示词对准三类形态 | 假上游：notify_ticket 的并排含请求、`slack_message_contains`、PATCH 写入与最后的话，留出题不在；第二轮仍失败的题 `persistent`；新词表进诊断的定义 | 代码 $0 |
| 4e 跨域 train（v1.4） | §6.14：`[bench] domains` 装成一个 suite、`[final] domains` 限定 final 与 release 的域、`stream_screen_max` 上限 30 | 假上游四个迷你域：两个域的 train 一个流，留出题混域，final 只取一个域的 test，release 只补该域 | 代码 $0；finance-v6 约 $560～$1,095（见 §6.14 的表） |
| 4d 产出率与流式（v1.3） | §6.1 一轮几个包、不冲突的叠装；§6.4 清单的审核 hook 建议与提示词；§6.7 `seed_from` 工作线跨活动；§6.13 `--phase stream` 流式循环（先 train 段，test 段的已计分放行随后） | 假上游：一轮两个包各自筛选、第二个叠装过 V0/V1 装入、冲突的记 `not_stacked`；清单对只剩文字载体的簇给审核 hook 建议；`seed_from` 的活动从旧 W\* 起步、final 的 W0 臂是它；`stream` 两批各一轮、第二批在新线上跑、报告按批给首遇分 | 代码 $0；finance-v6 名义约 $450（train 流式 $310 + final 两臂 $150） |
| 5 训练绑定 | v2.0 离线链对新记录跑通；`model` 组件绑定；之后按 V2 的 v2.1 | V2 §5 的断言 | $0；v2.1 另批 |

## 11 考虑过、不做

| 项 | 为什么不做 |
|---|---|
| 把改进器交给 Claude Code / omp 等现成编码 agent 的循环 | 改进器是框架核心机制，要自己设计实现；用现成循环就无法定义输入、上限、验收与记功 |
| Pi 作种子 agent | 两套工具链进 worker；它的强项在开发者侧。只借它的扩展事件模型 |
| Deep Agents 作首个种子 | 依赖重、循环核不可改；作编码类业务线的第二种子备选 |
| OpenEvolve 式的 MAP-Elites 岛与 GEPA 的 Pareto | 每个候选评一次就是一次 25 题 dev gate，种群式搜索付不起；单一在位者 + 档案 |
| SIA 式的在线 RL 回路 | 训练链先按 V2 打通；“停滞再训”作分层规则采纳，RL 目标的选择不在第一期 |
| Exo 式的运行时自我重建与克隆 | 候选改宿主、改自己的运行器是我们禁止的可变面；只借它的三层划分与不可变事件日志 |
| 在尝试中让 ReMe 重写、删除条目 | 多一次模型调用且动库；平台规则代替 |
| 种子里放具体工具（calc、table_diff） | 把人写的增益和 RSI 的增益混在一起 |
| 为每条原则加审查或自动检查 | 用户 2026-10-09 |
| 方法库、活动内研究位、多领域合并 | 没有产生过证据；C4 已改为跨活动 |

## 12 待用户决定

| # | 事项 | 建议 |
|---|---|---|
| 1 | v1.2 的六处修改（§6.12）取代 10-10 的 carry / `gate_every` 与按 cause 切名额 | 是 |
| 2 | `hook_aux_calls` 默认 2；`schedule.rounds` 上限 12、`patience` 3；留出筛选的接受规则（净修复 ≥ 1、退步 ≤ 1、容差 1） | 是 |
| 3 | library 默认关（preset `automationbench_v2` 的 `memory.enabled = false`）；memory 包保留，R 的输入里不再有条目 | 是 |
| 4 | finance-v5 的预算：根 $500、final 份额 $200；基线从 finance-v4 导入（AttemptIdentity 相同时） | 到冻结前批准 |
| 5 | 对外预期：一次活动 dev 严格 +3～6 题；80% 只在部分得分口径上谈 | 是 |
| 5 | 2026-10-12：对外的数字是 final 上的绝对分并列 W0 臂，不追求比业界更严；划分两份（train / test）、基线阶段取消、无 dev 的活动按决定发布（§6.7 第三条、§10 阶段 4c）；流式学习另立阶段 | 已定（用户 2026-10-12） |
| 7 | 2026-10-12：历次版本分数不动、失败的是同一批题（用户）；读轨迹后定：给 R 失败并排、加三个 cause、提示词对准工作方式（§6.15，阶段 4f）；v6 第 0 轮装入的系统提示正对着这三类，看它的效果再定下一步 | 已定（用户 2026-10-12："写吧，先改文档再改代码"） |
| 6 | 2026-10-12：finance-v5 第 0 轮 +2 不够，要的是一次活动提升足够高：一轮几个包并叠装、清单与提示词对准整体机制与审核 hook、流式循环、工作线跨活动（§6.1、§6.4、§6.7、§6.13；阶段 4d）；finance-v5 批式跑到底拿第一个数，v6 从 v5 的 W\* 起步流式跑 | 已定（用户 2026-10-12："跑第 1 轮，四条改动并行写"） |
