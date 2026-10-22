# ouroloop 架构设计（v2.0，正向设计）

> 2026-10-22 修订（v2.0，阶段 4o）：最后一版——按 Harness² 真正出效果的路径做"逐题采用"，评测改成贴近真实工作的**重复流**：同一请求再次到来时，agent 用自己上次的经验（不带标签）修正过的 harness 去做；对外报"首次通过率 → 自我修正后的稳态通过率"，W0 原样重复作对照臂。用户 2026-10-22："实际的工作场景里大部分都是重复的工作……一千次请求都是一样的模式，只是参数换了换""还是学学 Harness² 吧……最后改一版，包括我们的测试方法也可以调整到更符合真实情况的，这样也可以对外讲"（§6.23）。
>
> 2026-10-21 修订（v1.12，阶段 4n）：松开接受规则、爬坡到 final——用户："我们不需要一切都卡得非常严格……我们也要做到类似的效果就好了，这一阶段的任务需要尽快收尾掉。"可复现退步上限 0 → 3、留出容差 1 → 3、循环改动每轮 1 → 3；只跑 finance 一个域，一次活动 8 轮爬坡，退步题照旧进前沿由下一轮条件化；然后发布、封存 final 出数（§6.22）。
>
> 2026-10-21 修订（v1.11，阶段 4m）：逐题 probe——R 的会话里多一个 `try_task(task)`：当场把候选包在那道研究失败题上跑**一次**，宿主把这次执行与 W 那次执行的并排对比交回（失败断言前后、出手的 hook 与它说的话、写入按路由、最后的话）；提示词改成 Harness² 的固定形状（读 W 的轨迹找最早分叉 → 一处追溯到时刻的改动 → try 一次 → 看对比 → 同一道题至多再试一次 → 下一道），通过与否是一次 rollout、不作确认，出没出手与说了什么才是 debug 的依据；before_tool 的 hold 进所有簇的载体表、审核不再是默认建议；`scope` 进声明字段清单；`trial_max_attempts` 默认 40、`try_per_task` 2。finance-v10 r0 的阶段 A 看到 R 又交了审核 + 词表技能（框架的清单把它带回老路），用户："既然已经知道如何改了，就不用等跑完再改，持续 debug 然后修复"（§6.21）。
>
> 2026-10-20 修订（v1.10，阶段 4l）：向 Google Research 的 Harness²（arXiv 2610.14224，2026-10-09）学四处能稳定起效的机制——V2b 回放给 hook 真跑同形的 view（`events` 窗口按记录重建）并把一个包的全部 hook 按事件顺序一起回放；R 的证据按"分歧 > 顽固失败 > 未兑现的风险"排序，分歧题（W 自己有过有败）带通过轨迹并排；一轮的候选分成"通用（agent 怎么做事）"与"领域（从业者知道什么）"两种 scope；筛选后按组件裁决（helped / hurt / no_effect / unobserved）进台账与剧本。不学的：逐题的测试时递归与在匿名答案里挑一个（那是测试时算力，不是冻结的 W\*）（§6.20）。
>
> 2026-10-13 修订（v1.9，阶段 4j）：回路改成"R 对着失败题改、改完真跑、通过了再筛退步"——样本内试跑作为 R 的反馈与留出筛选的入场门槛（接受仍只看留出）、只含代码 / 技能组件的包退步按足迹计、前沿条目带修复证据且合并版筛选前先回放、导入 stream 的首遇、V0 提示未挂的 hook、词表信号去误报、编辑额度提醒、核价与 V2b 样本（§6.19）。
>
> 2026-10-13 修订（v1.8，阶段 4i）：不退步的累积——可复现退步的证据回灌 R（三档粒度）、只因退步被拒的包进前沿并由 R 合并成条件化版本、文字组件只能增量、靠词表触发的机制记软信号；参数都是 K 的超参（§6.18）。
>
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
| 5 验收 | 候选包 | V0、V1 对整包；V2a / V2b / V2c 对每个组件；有组件不过 → 退回修订一次，仍不过 → 本轮无候选；v1.9：过了免费阶梯的包先在它声明的研究失败题上试跑，结果回 R 修订，有样本内修复才进筛选（§6.19） | 固定代码 | `improver/verify/`、`improver/trial.py` |
| 6 留出筛选 = 活动内接受 | 过 V2 的包 | 在**全部**留出 train 题上各跑一次，翻转的题再读一次确认、W 补到两次（§6.17）；可复现修复 − 可复现退步 ≥ 1、可复现退步 0、足迹是全部题的包可复现修复 ≥ 2、留出通过数不低于历史最好减容差 → 接受；只含 tool / hook / skill 的包，退步只在组件出手的题上算（v1.9，§6.19） | 固定代码 | `improver/screen.py` |
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
| V2b 回放 | 对研究部分的轨迹回放 hook：声称题触发 ≥ 2 道；通过题样本（`replay_sample` 道，12；v1.9 从 6 提到 12）上的触发率 ≤ `replay_max_false_trigger`（0.25；v1.7，§6.17——此前只报不拦）。调 aux 模型的 hook 不能回放，走 V2d。v1.10（§6.20）：回放保真——每个事件带真跑同形的 `view["events"]`（该时刻之前的 broker 调用窗口）与 `turn`，一道题的全部站点事件按记录顺序一起回放，包的全部不调模型的 hook 都跑（模块状态共享，每道题重载），只有候选在声明站点改过的 hook 出手才计 | 每个不调模型的 hook |
| V2c 可达 | 描述能检索到 ≥ 2 道来源外研究题 | 每个 skill |
| V2t 样本内试跑（v1.9，§6.19） | 付费但便宜：包在它声明的研究失败题上各跑一次（≤ `trial_max_tasks`），每道的结果与 session 回给 R 修订（≤ `trial_max_rounds` 次）；翻转 ≥ `trial_min_fixes` 道才进 V3。是入场门槛，不是接受 | R 的包 |
| V3 留出筛选 = 活动内接受 | 付费：候选包在全部留出 train 题上各一次，翻转的题再读一次确认（v1.7，§6.17）；按足迹记功；接受规则：可复现修复 − 可复现退步 ≥ `accept_min_net_flips`（1）、可复现退步 ≤ `accept_max_regressions`（0）、足迹是全部题的包可复现修复 ≥ `accept_min_whole_fixes`（2）、留出通过数 ≥ 历史最好 − `accept_floor_tolerance`。接受即装入工作线；只含 tool / hook / skill 的包，通过题上的翻转只在组件出手时才是退步候选（v1.9） | 整包 |
| V4 发布 gate | 活动末一次：W_work 对 W0 在 dev 上，严格或部分得分规则，第二次读数作确认 | 工作线 |
| V5 封存 final | 对外数字只来自这一档 | 发布线 |

V3 的接受是搜索的接受，不是发布：它决定下一轮从哪里起步。R 看不到留出题，但连续多轮在同一组留出题上接受会向它们适应，所以发布只看 dev，dev 只在活动末读一次。

### 6.7 工作线、发布线与存档（v1.2）

- **工作线**：`rsi/active.json`，留出筛选接受就装入（CAS，reason `screen`），父包永远是工作线的上一个 release；活动内的测量、诊断、R 的起点都在工作线上。
- **发布线**：活动末 dev gate 通过的 W_work 成为 W\*（reason `gate`），final 与导出只用它；gate 不通过就没有发布，工作线的历史与留出曲线照实报告。
- **按决定发布（阶段 4c，2026-10-12）**：用户要的对外数字是 final 上的绝对分（W\* 得了多少，W0 臂并列），不是比业界更严的筛选。于是划分可以只有两份：train 给改进器（研究部分 + 留出筛选），test 冻结给 final；K 里没有 dev 题时不冻结 gate 模板、没有发布 gate，轮次跑完由用户 `--phase release` 把工作线按决定提升为发布线（reason `decision`，没有收据），`freeze` 再封存 final。基线阶段也不必跑：第 0 轮自己现量 train，W0 的数来自 final 的 W0 臂。流式学习（test 题首遇计分后变成证据、每 N 题一轮）是下一个阶段，另写设计。
- **工作线跨活动（v1.3，阶段 4d）**：`[rsi] seed_from = "<旧工作区>"` 时，新活动的种子不是代码的种子，而是旧工作区发布线上的 W\*：它的包闭包复制进新工作区的对象库，种子 release 的 reason 是 `import`，R 的策略（估值、剧本）一并导入。K 的 `harness.seed` 仍是代码的种子，只作形状与字面量的基准。final 的 W0 臂就是这个导入的起点，所以每个活动报的是"这一段"的提升，分数一路累积；活动只是预算窗口。导入时 W\* 相对这条链起点代码种子的改动被重放到本代码的种子上（2026-10-12，v1.6）：基准是沿 `seed_imported` 事件回溯到的第一个 reason `seed` 的 release（源活动的种子自己可能也是导入的，v5 → v6 → v7），R 在整条链上没碰过的框架文件（适配层、种子 hook、默认提示、vendor）取本代码的版本，新代码加的文件补进来，R 改过或新加的文件照原样（R 与框架都改过的也照 R 的，事件记 `kept_over_code`）、R 删掉的保持删掉——否则新活动的审核 hook 在旧适配层下答不了 `missing`。重放后的包是新的 ref，`seed_imported` 事件记 `origin`（链起点的工作区与种子 release）、`source_package`（源 W\*）、`package`（重放后的）与 `rebased`（换了的文件）；没换到任何文件时 ref 不变。链断了（起点工作区不在原处）时拒绝。导入哪条线由 `[rsi] seed_line` 定（2026-10-12）：默认 `released`（发布线的 W\*）；`working` 取源活动工作线的头——它已过自己的留出筛选，按决定发布本来也只是无判断地把它提升、再补读它的 train 以供源活动 final 的估值，而不跑 final 的活动（finance-v6）不需要这一步；事件记 `source_line`。源活动没有那条线时拒绝。
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

### 6.16 学会提升概率：单向审核与审核预演（v1.6，阶段 4g）

4f 把失败并排给了 R；剩下的问题是 A 类（文档里的一条要求没执行到，18 道顽固题里 8 道）没有确定性办法，只能靠调模型的审核，而审核在 v5 第 1 轮、v6 第 1、2 轮三次被拒，三次的形态相同：审核把对的也推回去改（`tax_prep_summary` 修好又被改坏）。用户 2026-10-12："就算是概率性的，只能靠审核的，你应该也知道了没通过的原因，框架的提升方向就是要能够学会解决它，学会提升概率。"框架的回答是把审核从"一次性赌一把"变成"可测量、可修订、可累积"的东西：

- **单向，构造上保证**。`on_finish` / `after_turn` 的 hook 多一种答法 `{"missing": ["…", …]}`：宿主（适配层）把它渲染成固定措辞的推回——"An audit found requirements with no matching write: … Add what is missing; do not change or remove what you have already done."——hook 自己写不了别的话。调过 aux 模型的 hook 只能这样答（`improver.audit_additive_only`，fixed true）：它返回自由文本的 `pushback` / `message` 就是 `CandidateDefect`。于是审核的下限是零（最坏不起作用），不再是负的。
- **预演 V2d（the audit's dry run）**。`uses_model` 的 hook 有了免费档：在研究部分已记录的轨迹上回放它（V2b 的回放机器，`ctx.tool` 只读对着新开的世界，`ctx.model` 接研究模型、role aux、计在轮的 `verify` 子账户、`hook_aux_calls` 封顶），对声明的失败题与通过题样本各跑一遍，得到：在通过题上报警的比例（误报率，每个误报都是一次退步的风险）、在声明的失败题上报警几道（命中），以及命中的 `missing` 条目有没有点到没过的断言（断言参数里的值或收件人出现在条目里：确定性的代理，只报告不判）。门槛：命中 ≥ `replay_min_triggers`，误报率 ≤ `audit_max_false_alarm`（0.25，cap）。不过门槛的，把误报（通过题的请求、审核说了什么）作为退回文本交给 R 修订一次（会话的 revise），再预演。一次预演约 16 次 aux 调用、$1～2；比 $20 的筛选便宜一个量级，所以审核可以在一轮里改两版。
- **累积**。台账多 `audited` 行（候选、命中数、误报数、每条误报的说法——研究部分可写题号，留出部分只记计数）；R 的输入多 `audits.json`：这条线上试过的每个审核——它的机制一句话、预演与筛选的数、在研究部分上的误报原文——下一轮的审核从上一轮的误报开始改，而不是从零开始。剧本照常。
- **几个包**。v6 五轮 R 每轮只交一个包（提示词允许三个）：提示词改为"清单有几个簇就交几个包，少交要在 finish 里说为什么"，台账记 `single_package`。

不做：人手写审核进种子；给审核加第二个模型当裁判（预演的命中代理是确定性的，够用）；在留出题上做预演（留出题的轨迹不给 R，预演的误报原文会泄漏它们）。

### 6.17 可复现的接受：V2b 误触发门与翻转确认（v1.7，阶段 4h）

finance-v7（2026-10-13，JOURNAL 同日一条）量出了此前所有"+2、−1"的底：同一个包、同样的 30 道题，两次跑有 10～17% 的题结果会翻（v7 批 1 与 v6 同批是同一个包：2 升 3 降；同一轮 3 个包筛同样 30 道留出题，3～5 道结果不一致；v5 final 三遍，W0 2/50 道、W\* 6/50 道结果不一致）。留出筛选的接受规则"净修复 ≥ 1"在 13～30 道题上低于这个底：v7 十个包装了两个，两个"修好"的题在别的包下照样来回翻，是摇摆题，不是被修好；装上的词表 hook 把系统提示词里 R 自己写的一句话当成请求要求，之后 85% 的尝试每条外发消息都收到无意义提醒——V2b 算出了它在通过题上的触发率（接近 1），但只报不拦。用户的判断（2026-10-13）："确保 RSI 的调整是吸收好的经验，而不是遇到一点错误就全盘修改。"因此不加机制，只收紧两个门：

- **V2b 的误触发门**。`replay_max_false_trigger`（0.25，cap；与 V2d 的 `audit_max_false_alarm` 同义）：hook 在通过题样本上的触发率超过它，这一档不过，台账 `verified` 行记触发率。一个每次都要说话的 hook 是提示词，不是 hook：那就改 prompt 组件（足迹是全部题）。
- **翻转确认**。筛选的一次读数之后，候选与池里读数不同的每道留出题（条件修复与退步；无足迹的通过本来就不计，不再读）再在候选下读一次（run 的 role `confirm`，钱记筛选账户）；另一边是当前工作线 W 对这道题的读数（首遇、现量、以前的确认），不足两次的补到两次（role `confirm`）。可复现修复 = 候选两次都过、有足迹、W 的读数全败；可复现退步 = 候选两次都败、W 的读数全过；W 自己的读数有过有败的题是摇摆题，两边都不算（`noisy`）。算过账：一道 50% 摇摆题被误认成可复现修复的概率是 1/16（候选两过 × W 两败），且要落在候选的足迹里——一个只在 8% 的题上开火的审核 hook 几乎碰不上，而足迹是全部题的载体（prompt / config / agent / vendor）碰得上。所以接受规则是：可复现修复 − 可复现退步 ≥ `accept_min_net_flips`（1）、可复现退步 ≤ `accept_max_regressions`（0）、足迹是全部题的包可复现修复 ≥ `accept_min_whole_fixes`（2，第二道修复代替足迹）、通过数不低于历史最好减容差（一次读数的分，不变）。记功、估值、台账的 `fixed` / `regressed` 都用可复现的集合；一次读数的翻转记 `flipped_fixed` / `flipped_regressed`。每个包多约 3 × 翻转数次尝试（6 道翻转约 $12）。v7 的三次装拒按这条重算：s1 的词表 hook 修的两道题在别的包下来回翻、s3 的 skill 修的两道题在别的包下也都过（W 的首遇是运气差的那次）——都不会装；s0 的审核 hook 在 13 道留出题里对 1 道开火、点出缺的那一步、题过了，却因另一道无足迹的噪声退步记 net 0 被拒——按新规则它是 v7 唯一可复现的修复，会装。
- **提示词**：请求是任务的文本，包自己的提示词不是请求；通过题上的触发率是门，不是提醒。

不做：装入后的回退监测（下一批里新 hook 在通过题上的触发率）与筛选的无效提前停（筛到一半退步已超门就停）——先看这两道门在 v8 上的效果，再定。

### 6.18 不退步的累积：退步证据回灌、前沿与合并、文字只增（v1.8，阶段 4i）

finance-v7 的 13 个包逐题看（JOURNAL 2026-10-13 机制分析一条）：留出 30 道题里约 7 道在同一条线下来回翻（60 道读过 3 次以上的题有 14 道翻转，23%），这部分 4h 的翻转确认已经挡住；剩下的是真实互斥——s3 的三个包都把 `hr.zoom_orientation_sessions`（此前 6/6 通过）弄坏，机制都是"排除 reserved / pending / on leave 的行"，而这道题要求把 on leave 的人照样列进去；s4 的包把四道 6/6 通过的题弄坏，机制是"文档里提到要通知的每个邮箱都进通知名单"。R 从几道失败题归纳出一条全局启发式，基准里另一些题对同一表面形态有相反要求；它只收到"退步 2"的计数，无法把启发式改成条件式，于是 s2 → s3 → s4 换一种启发式再试。能同时对的不变量只有一条：按本题自己的文档办。

v1.8 在 4h 之上加四条，参数都进 K 的 `improver` 节：

| 机制 | 内容 | 超参（默认） |
|---|---|---|
| 退步证据回灌 R | 三档，逐级包含。`counts`：现状，只有计数。`abstract`：每道可复现退步的留出题给 R 一条去题化记录——域、候选下没过而 W 下过的断言**类别**（不含参数）、候选下在该题出手的组件与它们说的话（R 自己写的文字，原样）、agent 写入的变化（按工具与路由的次数：W 下 n 次、候选下 m 次，不含参数值）、终止方式；不含题 id、请求、任何取自世界的值。`research`：只对有可复现退步的包，再从研究部分抽 `regression_research_sample` 道 W 读数通过的题（回放里候选的 hook 误触发的优先，其余按候选名播种的固定顺序）在候选下跑一遍（role `research`），退步的题 R 可以 `read_trajectory` 读 W 下与候选下两条轨迹——这些题 R 本来看得到，不泄漏，每包约 $3。证据写 `rounds/r<i>/regressions.json`，下一轮进 `inputs/regressions.json`；`screened` 行带它的 ref | `regression_evidence`（`research`；可取 `counts` / `abstract` / `research`）、`regression_research_sample`（6，cap） |
| 前沿与合并 | 筛选被拒、可复现修复 ≥ `frontier_min_fixes`、且只因可复现退步被拒的包是**部分包**，进前沿 `rounds/frontier.json`：文件、声明、修复集与退步集（留出题号只在宿主）、退步证据、尝试次数。前沿按可复现修复排序，超 `frontier_max` 丢最弱（`dropped`）；工作线后来已通过它的全部修复题的条目退出（`subsumed`）。前沿非空时聚焦清单加一项 `merge`：给 R 该部分包相对当时基线的 diff（R 自己的代码）、修复与退步的计数、退步证据；要求交一个**条件化版本**——同一机制，触发条件收窄到不碰退步形态（改成"文档明确要求时才"、工具 + 带谓词的 hook 等）。合并包占 `packages_per_round` 的一个名额，每轮至多 `merge_per_round` 个，照常验收与筛选；接受即装入，条目退出（`merged`）；再被拒则 `attempts` 加一，到 `frontier_max_attempts` 退出（`abandoned`）。不做：两个已接受包之间的合并（叠装已覆盖）；前沿条目直接装入（它有可复现退步） | `frontier_max`（4，cap）、`merge_per_round`（1，cap）、`frontier_min_fixes`（1，floor）、`frontier_max_attempts`（2，cap） |
| 文字只增 | `prompt/*.md` 与父包已有的 `skills/*.md`，候选的改动只能是插入：相对父包的 diff 里没有删除或改写的行。要删或改写已有文字，那个包只能含这一个组件（kind prompt 或 skill，声明 `rewrite: true`），单独筛，按全足迹规则接受（`accept_min_whole_fixes`）。V0 执行；Meta-Harness 赢的候选是"纯增量"，ACE 的上下文只做增量更新，都是同一条经验 | `text_additive_only`（true，fixed） |
| 启发式软信号 | V0 扫 hook / tool 代码里作为触发条件的字面词表（正则或列表里 ≥ `heuristic_terms_min` 个自然语言词），记进 verdict 与台账 `verified` 行的 `heuristic_terms`，并进 R 的 `regressions.json` 与 `frontier.json`（"这个机制靠词表触发"）；不拒绝。提示词写明：机制的条件必须来自本题的文档与请求，写死的词表在别的题上会反 | `heuristic_terms_min`（3，floor） |

另外 `replay_max_false_trigger` 从 0.25 降到 0.2（cap；v7 装上的噪声 hook 接近 1.0，v1.7 的夹具是 0.67）。

怎么判断它有没有用（finance-v8 重新冻结后，报告并列）：工作线在受保护集（留出里首次读数通过的题；每轮 `measured` 行记 `protected_passed`）上的通过数逐轮不降；接受的包可复现退步为 0；只因退步被拒的包里，经合并后被接受的比例；研究部分抽样退步的命中率（`research` 档有没有抓到留出上也退步的形态）。产出的度量不是"每轮接受几个包"，是工作线的留出通过数的单调曲线。

仍不做：装入后的回退监测（v1.7 推迟）；两个已接受包之间的 GEPA 式合并。

### 6.19 对着失败题改、改完真跑、通过了再筛退步：样本内试跑、按足迹的退步、带修复证据的前沿（v1.9，阶段 4j）

finance-v8（2026-10-13，JOURNAL 同日 finance-v8 一条）在 4h + 4i 的 K 上跑完：首遇 87/150，五轮零装入，$261。回路的缺陷不在某个门，而在顺序：R 从没在它研究的失败题上真跑过自己的包就进了昂贵的留出筛选（9 个包里只有 1 个动到留出题）；前沿只给 R 退步证据，合并版把修复丢了（R 写了 `lone_request (before_run)` 却没在 manifest 里挂，V0 查不出）；没有足迹的"可复现退步"是噪声底的产物（`route_errors` 只在 2 道题上出手，被记成确认退步的那道不在其中）；V2b 回放 6 道题的误触发率（0.17）严重低估实际出手率（0.53）；会话用完 48 次编辑没 finish 浪费一轮；核价把研究抽样计入每个包的事前上限挤掉了第三个包；`heuristic_terms` 把 HTTP 方法元组与工具名列表当词表；新活动的基线导入只认 base / control 角色，v8 的 150 道 stream 首遇不能复用。用户 2026-10-13 的决定："按照建议来"。

v1.9 把回路改成**"R 对着失败题改、改完真跑、通过了再筛退步"**，参数都进 K 的 `improver` 节：

| 机制 | 内容 | 超参（默认） |
|---|---|---|
| 样本内试跑（最重要） | R 的包过了免费阶梯之后、进留出筛选之前，先在它声明 `predicted_affected`（没有声明的组件取其 cause 的题）里**研究部分当前失败**的题上真跑（run 的 role `trial`，候选名同筛选，钱记本轮的 `trial` 子账户），每轮每包至多 `trial_max_tasks` 道；每道题的通过 / 失败、失败断言的类别与参数（train，R 本来看得到）、哪个组件出手、候选下的 session 交回 R 的会话（session 加进 R 可读集），R 修订一次再试，至多 `trial_max_rounds` 次修订；一轮全部试跑尝试 ≤ `trial_max_attempts`。只有在研究失败题上**真的翻转 ≥ `trial_min_fixes` 道**（通过且有足迹）的包才进留出筛选；没翻转的记台账 `verified` 行 outcome `no_trial_fix`，不筛（省一次约 $30 的筛选）。没有可试的题（声明的题都不是研究失败题、或 `trial_max_tasks` 为 0）时不试、照常筛。核价：付诊断前的下限加 `trial_max_attempts` 次 train 尝试；每次试跑前再核一次，放不下就不试、照常进筛选的核价。试跑的尝试按候选名 + 包 ref 续跑不重付（修订后的包是新 ref、新 run） | `trial_max_tasks`（4，cap）、`trial_max_rounds`（2，cap）、`trial_max_attempts`（12，cap）、`trial_min_fixes`（1，free：它只决定花不花筛选的钱） |
| 退步按足迹 | 只含 tool / hook / skill 组件（足迹可判）的包，留出通过题上的翻转**只有在该包的组件在那次尝试出手时**才是退步候选、才进确认；没出手的记 `unconditioned_regressed`（新字段），不确认、不计退步、不进证据。确认时退步的每次候选读数也都要有足迹（与修复对称）。全足迹包（prompt / agent / config / vendor、rewrite）照旧：每道题都算。容差条不变：一次读数的通过数仍含所有退步 | `regression_by_footprint`（true，fixed） |
| 前沿条目带修复证据 | 每道确认修复的题一条去题化记录（`FixEvidence`：域、W 下没过而候选下过的断言类别、出手的组件与它说的话、前后写入按路由、终止；无题号、请求、世界里的值），与退步记录一起进证据对象、`regressions.json` 与 `frontier.json`，R 看到"它为什么起效"而不只是"它弄坏了什么"。合并包在筛选前先**回放**：条目修复题上候选自己的留出尝试（宿主有 session，R 没有）喂给合并版的 hook（不调模型的），合并版须仍在 ≥ `merge_min_acts` 道上出手，否则退回 R 修订一次（只给计数："合并版在条目修好的 2 道题的尝试上一道都不出手"），仍不出手 → 不筛（verdict 的 `merge` 步不过）；只有工具或调模型 hook 的合并版无法回放，记 `not_replayable` 直接进筛选。流式的筛选窗口在最近 `stream_screen_max` 道之外**另加**本轮合并条目的修复集与退步集；合并包的 `screened` 行记 `kept_fixes`（条目修复集里它确认修复的数） | `merge_min_acts`（1，floor） |
| 导入 stream 的首遇 | `import_baseline` 除 base / control 外也导入 role `stream` 的 train 读数（同一 AttemptIdentity：尝试路径代码、种子包、limits、模型、镜像、划分都相同），连同它们的 `stream_scored` 事件；`--phase stream` 开始时也查 `[rsi] baseline_from`。新 K 的活动于是能复用 finance-v8 的 150 道首遇（$100）而只付轮次 | — |
| V0 的未挂 hook 提示 | 候选模块里定义了形如 hook 入口的函数（两个位置参数 `(ctx, event)`）而 manifest 的 hooks 没有挂它的，写进 V0 结果的 `unregistered_hooks`、verdict 与 `verified` 行，提示 R（不拒）：挂上，或删掉 | — |
| `heuristic_terms` 去误报 | 词只数自然语言的：忽略全大写 token（HTTP 方法）、驼峰 token（`sendMail`）、短于 4 个字符的词；`x in LIST` 只在 `x` 不是名字类的值（事件的 `name` / `method` / `tool` / `site` / `action` / `url` 等）时算；for / 推导式遍历的词表只在循环变量与文本做 `in` 比对时算 | `heuristic_terms_min` 不变 |
| 会话的编辑额度提醒 | 编辑到额度的 75% 时在那次工具结果末尾附一句"用了 36/48 次编辑，剩下的留给 finish"，到 90% 再附一次；会话因额度结束（turns / edits）时台账记 `session_ended`（原因、turns、edits、已声明的包数） | — |
| 核价不挤掉第三个包 | 筛选的事前核价不再把 `regression_research_sample` 计入每个包；研究抽样只对筛选后有可复现退步的包做，抽样前按实际题数再核价一次，放不下就不抽（证据对象的 `research.outcome` 记 `budget`），轮次不停 | — |
| V2b 样本与实际出手份额 | 回放的通过题样本从 6 提到 `replay_sample`（12；V2d 审核预演同一样本）；筛选结果记候选组件在留出上的**实际出手份额** `footprint_share`（一次读数里出手的题数 / 有效读数，全足迹包为 1.0）与 `acted` 计数，进 `screened` 行与报告 | `replay_sample`（12，floor） |

**与 §6.6 / §6.7 的关系：接受仍只看留出。** v1.2 去掉样本内筛选是怕 R 向案例过拟合；这里的试跑不是接受，是 R 的反馈与**入场门槛**：它只决定一个包值不值得花钱去留出筛，不记功、不进估值、不上记分板的修复计数（台账 `trialed` 行只有它自己的计数）；装入仍只凭留出筛选的可复现修复、零可复现退步与容差条。R 看到的是它本来就能看到的研究题；留出题仍从不进 R 的输入。一个只在它试过的 4 道题上起效的包，留出上照样是零翻转、照样被拒——但那时花的是 $30 的筛选，而不是什么都不试就花 $30。

**怎么判断它有没有用（下一次活动）：** 进留出筛选的包里有样本内修复的比例（应为 1）；筛选被拒的包里零留出翻转的比例（v8 是 8/9，应明显下降）；没有足迹的退步候选数（`unconditioned_regressed`，v8 读到的那类）与确认退步数；合并包在条目修复题上的 `kept_fixes`（v8 是 0）；`footprint_share` 对 V2b 误触发率的差；一轮里因额度结束而没有 finish 的会话数。

不做：试跑结果进接受规则（见上）；对全足迹包按"某种足迹"判退步（它们没有足迹）；两个已接受包之间的合并；装入后的回退监测（仍推迟）。

### 6.20 向 Harness² 学来的四处：回放保真、分歧优先、两种 scope、按组件裁决（v1.10，阶段 4l）

finance-v9 r0（JOURNAL 2026-10-19）之后用户问起 Google Research 的 Harness²（"Recursive Agent Harnessing for an Open World"，arXiv 2610.14224，代码 github.com/google-research/harness2，Apache-2.0）。它和本框架同源：模型冻结，改的是 harness（system prompt、guardrails、memory、tool guidance、skills、scripts、subagents、plugin），两层递归——任务级的 Propose-Probe-Compose 与库级的"采用的 harness + EXPERIENCE.md"。它报告的增益大半来自每道题上的测试时算力（参考执行 + 2k 个候选真跑 + 合成版 + 在匿名答案里挑一个；去掉库仍有一半增益；编辑器 Opus 4.8 给弱执行器 GLM-5 / Gemini Flash 写 skills）与 test 流上的在线库，和我们"冻结的 W\* 在封存 final 上比 W0 多多少"不是一个口径；它唯一同口径的数（JobBench 建库、WorkBuddy 单步跑）是 +0.5～+2.9，落在我们量到的噪声带里。所以学的不是它的指标，是它机制里**与我们已有读数一致、且能稳定起效**的四处。用户 2026-10-20："不要光看别人不好的地方，要看好的，能够稳定提升效果的机制，学习进来。"

| # | 学来的 | 它那边 | 我们这边的读数 | 改法 | 超参 |
|---|---|---|---|---|---|
| 1 | **候选在真题上真跑、对着参考执行比**，没有回放这一档 | Probe：每个候选在同一道题上执行，轨迹与交付物和参考执行并排 | v9 r0：V2b 的回放把 R 的 before_tool hold 逼成 after_tool 的事后提示（回放给的是 `smoke_view`，`events` 为空；各站点各自回放，before_tool 看不到 after_tool 喂的状态） | **V2b 回放保真**：`verify/replay.py` 从一条 train 轨迹重建**整条**事件流（六个站点按记录顺序交错，每个事件带该时刻之前的 broker 调用窗口 `view["events"]`，真跑同形，与 `turn`）；一道题在 worker 里重载包（模块状态清零）后，包里全部不调模型的 hook 按 manifest 顺序在每个事件上跑，先出手的 block / pushback 结束该事件的派发（与种子一致；调模型的父 hook 按记录的 `acted` 判）；只有候选在声明站点改过的 hook 出手才计触发。V2d 的审核预演与合并版的回放用同一台机器。回放仍是免费档，不是真跑；真跑是 V2t | — |
| 2 | **证据的顺序：分歧 > 重复失败 > 未兑现的风险**；每处改动追溯到轨迹里的一个时刻；修最早的分歧；每个失败配最便宜的有效组件；常驻文字最贵，优先按需组件 | 编辑器提示词：优先 "runs disagreed and the agent can already do the thing"，再 "a repeated concrete failure"，再 "a visible but unrealised risk"；没有轨迹时刻的改动是 speculation | v7 量到同一个包两次跑 10～17% 的题翻转：研究部分里 W 自己有过有败的题很多，它们正是"agent 已经会做、harness 只需让它稳定"的题；R 至今只看失败轨迹 | `failures.json` 每条并排加 `w_readings`（W 这条线在该题的有效 train 读数：过几次、败几次）、`divergent`（有过有败）与 `passing_session`（W 通过的一次 session，R 可 `read_trajectory`）；排序改为分歧题在先、其次顽固题（`persistent`）、再其余；提示词加一段：先读分歧题的两条轨迹找最早分叉的时刻，改动落在最便宜的有效组件上，常驻 prompt 最贵，按需的 skill / hook 优先；没有轨迹时刻的改动不要交 | — |
| 3 | **每轮两类候选**：通用（INV：procedure、tool use、completion）与领域（DOM：deliverable、audience、controlling sources、required structure） | 消融里最大的一项：去掉 scoping，WorkBuddy −2.9、JobBench −3.7 | 一轮几个包（§6.1 v1.3）已允许，但 v9 配的是一轮 1 个包，提示词只要求"不同机制对不同簇" | 声明加可选 `scope`（`invariant` \| `domain`）；`packages_per_round` ≥ 2 时提示词要求第一个包是通用的（agent 怎么做事：抽要求、核对、完成协议、工具使用）、第二个是领域的（从业者知道什么：交付物的形状、谁收、以哪份文档为准、记录自己的词），各自筛选、各自叠装（§9.4 不变）；台账的 `declared` / `screened` 行记 `scope`，记分板按 scope 另列 | `scoped_packages`（true，fixed） |
| 4 | **按组件裁决**：每个改动的组件一行 helped / hurt / no observed effect / confounded；一次至多一条可迁移的教训，cue 是情境的形状，教训点名组件；没有具体对比就 NO_ENTRY | 库级反思的提示词；"Lessons are retained only when supported by execution contrasts" | v9 r0：hook 在留出 11 道题出手，出手的地方什么都没变——这正是 no_effect，台账里只有 `acted` 的计数，剧本读不出 | 筛选后对包的每个组件算裁决：`hurt`（它出手的题里有可复现退步）、`helped`（出手的题里有可复现修复）、`no_effect`（出手了、出手的题零翻转）、`unobserved`（留出上没出手）；包级另记 `confounded`（无足迹的翻转数）。进 `screened` 行（`verdicts`，只有计数）与 R 的台账投影；剧本提示词要求每条教训点名组件与裁决，`no_effect` 的组件不得写成起效 | — |

**不学的，与为什么。** (a) 逐题的任务级递归（每道题 4 次以上执行 + 编辑会话，交付合成版的答案）：那是测试时算力，我们的对外数字是冻结 W\* 的 final，尝试成本还降了 18～22%；若以后要一个更大的数，另立一臂并与算力对齐的 best-of-4 对照。(b) 在匿名答案里挑一个（selector）：同上。(c) test 流上的在线库：我们的流式学习另有阶段（§6.13），且只在 train 段。(d) 编辑器强于执行器：我们的 actor 与 R 同一模型、同一 effort（PLAN 规则 9、10）；R 的 effort 是否单独提高，列为待用户决定，不在本阶段改。

**怎么判断它有没有用（下一次活动）：** V2b 对 before_tool hold 的触发计数（r0 是 1/6，应与 V2t 真跑的出手一致）；`failures.json` 里分歧题的数量与 R 的包有多少 `predicted_affected` 落在分歧题上；两个 scope 的包各自的试跑修复与留出净值；`verdicts` 里 `no_effect` 的组件数与下一轮 R 是否还交同类组件。

### 6.21 逐题 probe：R 当场试一次、宿主给并排对比（v1.11，阶段 4m）

finance-v10 r0 的阶段 A（2026-10-21，$12.96 后停掉）：R 对着同一批 37 道失败题，交的还是一个调模型的 on_finish 审核和一个词表技能——4g 时审核三次"改过头"、文字载体先验最低。不是 R 不会：聚焦表把 rule_not_applied 这簇只开放给 skill / prompt，`audit_suggested` 明说"一个审核覆盖这些簇"，剧本还写着"r0 的 hook 被拒"；R 按清单走。更根本的是回路的形状：R 读一遍轨迹就要"为 cause 写、不为 case 写"，写完才拿 3 道题试一次、修一次——这不是 Harness² 的 probe（一道题、一次参考执行、改、同一道题真跑、对着参考执行看分叉），分歧优先那段在只有一次读数的活动里也是空的。用户："既然已经知道如何改了，就不用等跑完再改，持续 debug 然后修复。"

| 改法 | 内容 |
|---|---|
| `try_task` | 会话的新工具 `try_task(task, dir?)`：宿主取回候选目录、build、过 V1（包测试、探针、smoke），然后在**这一道研究失败题**上真跑一次（run 的 role `trial`、候选名同筛选 `{tag}:{dir}`、钱记 `trial` 子账户、按包 ref 续跑免付），交回**并排对比**：W 的读数与这次执行各自的通过与否、部分得分、步数、失败断言（类型与参数）、出手的 hook（站点、名字、动作、它说的话）、写入按路由（方法 + 路径，计数）、最后的话；R 的组件哪个出手（足迹）；本轮剩几次尝试。这次执行的 session 进 R 的可读集。只能试研究题（留出题 AccessDenied）；一轮全部尝试（R 的 try 加 finish 后宿主的试跑）≤ `trial_max_attempts`（默认从 12 提到 40，cap）；每次 try 前核价；台账每次一行 `tried`（dir、task、passed、acted、attempts） |
| 提示词 | 工作方式改成 Harness² 的 probe 形状、每道题一遍：按清单取一道失败题 → `read_trajectory` 读 W 的那次执行，找它与请求 / 文档要求分叉的最早时刻 → 写能让那一步走对的最小机制（为 cause 写，这道题是它的第一个案例）→ `try_task` 一次 → 读对比：**出没出手、在哪一步、说了什么、写入变没变是确定的，照它改**；通过与否是一次 rollout，对比里标明"未确认"，不以它为目标 → 同一道题至多 `try_per_task`（2）次 → 下一道 → 累积成包、finish。载体表加一句：对"文档里的一条要求没执行到 / 排除项被写进去"这类，before_tool 的 hold（写入前暂停一次、把证据给 agent、原样重发即放行）是首选载体，回放现在看得到它；审核改成"可以"而不是"做一个"。声明字段清单加 `scope` |
| 载体表 | `allocate.kinds_for`：不可机械化的簇也开放 hook（排在文字载体前）；`require_code` 不变 |
| finish 之后 | 宿主的试跑照旧（§6.19）：R 自己 try 过的题按记录免付；`trial_min_fixes` 的门不变 |

**finance-v11 r0 阶段 A 的读数（2026-10-21，$3.39）与第二批修订。** R 第一次就交了 hold（`excluded_write`：记录写入碰到 agent 自己在 plan_changes 里排除的项时暂停一次，条件只来自本次运行的计划，docstring 写明追溯的时刻），try 了 zoom_deal_review 一次：通过但 hook 没出手（agent 这次自己没建被排除的会议），对比如实说"无足迹的通过"。之后 R 把 44/48 次编辑花在给**回放**加诊断开关上——V2b 要 hook 在 ≥ 2 道认领题出手，它在回放里只做到 zoom 一道（capex 要靠表格行号匹配，没做成；另两道 agent 根本没把人列进排除项，机制够不到）——最后 finish 了空列表，写了的代码随之丢掉。五处修订：(1) R 的 try 里 hook 真出手的认领题计入 V2b（`VerifyContext.tried_acted`，真跑优于回放）；(2) `replay_min_triggers` 默认 2 → 1（floor），试跑的门（`trial_min_fixes`）已经守着钱；(3) 会话上限 40 回合 / 48 次编辑 → 60 / 80（cap）；(4) R 写了但没交的候选目录存档（台账 `unfinished`：文件、R 的收尾说明），下一轮的 `inputs/unfinished.json` 与 `inputs/unfinished/<dir>/` 带进去，提示词要求先读；(5) 回放里 modify 的参数 / 输出传给后面的 hook（与种子一致）。提示词另加：回放比 try 弱，看不懂回放就 try，不要把编辑花在诊断上；做成一道题的包就交。

**与 Harness² 的异同，与稳定性（用户 2026-10-21 问）。** 它每道题只走一遍固定循环：参考执行 → 候选各跑一次 → 对着参考执行合成；明说"单次 rollout 的候选结果是弱证据"。我们学它的 probe 与对比，不学无界的逐题循环：同一道题至多两次 try，对比文字把通过标成未确认，机制仍按 cause 写。接受路径一处不加：留出、两次读数确认、零退步（§6.17）；try 不记功、不进估值。新引入的不稳定只有 try 的花费，有 `trial_max_attempts` 封顶、每次核价。

**不变的。** 接受仍只看留出（§6.6、§6.7）；R 看不到留出题；try 的结果不记功、不进估值。

**怎么判断它有没有用（finance-v11）：** R 一轮 try 了几道题、几次；try 过的题里翻了几道（宿主试跑的修复数应 ≥ 1）；R 交的载体是不是 hold；留出筛选的可复现修复数。

### 6.22 松开接受规则，爬坡到 final（v1.12，阶段 4n）

finance-v6 到 v12 八个活动约 $1,150，工作线一次都没装入，W\* = W0。v7 之后接受规则收成零可复现退步 + 两次确认（§6.17）——它把噪声挡在外面，也把搜索的爬坡堵死了：任何出手面宽的机制（v12 的技能在 27/60 道出手）必然碰上一两道退步，于是一个都装不进。同一个基准、同一个种子 agent 上 StarHarness 报过 57 → 83（单次运行、每题一遍、无区间），它的接受是"20 道选择题均值高于当前就装"、5 轮叠加；Harness² 在它的基准上也是每道题都采用、退步由库里的教训兜底。用户 2026-10-21："我们不需要一切都卡得非常严格，Harness² 不也说自己在同样的任务上做到了 80 多分吗，我们也要做到类似的效果就好了，我们的任务不是一直在跑测试数据集，这一阶段的任务需要尽快收尾掉。"

| 改法 | 内容 |
|---|---|
| 接受规则 | 可复现修复 − 可复现退步 ≥ 1（不变）；可复现退步 ≤ `accept_max_regressions`（0 → 3）；留出通过数 ≥ 历史最好 − `accept_floor_tolerance`（1 → 3）。两次读数确认（§6.17）与按足迹（§6.19）不变：噪声仍不计，退步仍进证据与前沿 |
| 搜索面 | `l5_per_round` 1 → 3：循环、配置、vendor 的改动一轮最多三处（StarHarness +26 的大头是工具与循环补丁） |
| 活动 | 只跑 finance 一个域（三域混跑稀释每个域的机制；final 本来只报 finance），两份划分 50 / 50；`schedule.rounds` 8、`patience` 3；每轮两个 scope 的包；退步题进前沿，下一轮合并条件化 |
| 收尾 | 轮次结束 → `--phase release`（按决定发布工作线）→ `freeze` 封存 final 计划 → `--phase evaluate`（test 50 题 × 3 遍，W0 并列）→ `report`。对外的数仍只来自 final（规则 14） |

**不变的。** 划分冻结、留出题不进 R 的输入、计划哈希先于第一次 test 尝试（防泄漏三条）；两次读数确认；退步按足迹；前沿与合并。**变的只是搜索愿意接受多大的退步换多大的净值**——这是用户的决定，记在这里。

**预期与读法。** 爬坡的每一步都在留出上有两次读数，但 8 轮累积在同一组 20 道留出题上会向它们适应（§9.1）；final 上的数比留出曲线低是正常的。目标是 final 上 W\* 明显高于 W0；能不能到 80，final 说了算。

### 6.23 最后一版：逐题采用 + 重复流评测（v2.0，阶段 4o）

**为什么换口径。** finance-v9～v13 五个活动、$300 多，零装入。读清的原因（JOURNAL 2026-10-22）：AutomationBench 的 train 150 道题是 150 个族，test 又是另外 150 份——每道题是独一份的工作流，没有重复；0.6 以上的"重叠"只是断言类型相近（表格 + 邮件），不是换参变体。我们一直在量"对完全不同的工作流也成立"的泛化，这是最难的一种，也不是产品要的那种。实际工作里一天一千次请求是同一个模式换参数：第一次做错 → 对着这条轨迹改 → 之后都对。Harness² 的分数正是这样来的：为这道题改 harness，改完用它跑这道题交答案，没有留出、没有标签；它跨题的库只贡献小头。所以最后一版学它的主路径，评测改成能体现重复的口径。

**重复流（EVALUATE 新协议）。** test 50 道题按冻结顺序成流，每道题**遇到 E = 3 次**：
- 第 1 次：工作线 W（种子包 + 库）跑一遍，计分 → 首次通过率（= W0 的首次读数，和原来的 final 第一遍同口径）。
- 两次之间：**不读评分**。R 拿到这条轨迹（请求、读过的文档、每次调用与结果、写入、最后的话）、库里的教训与可复用的组件，按 Harness² 的形状提出两个候选（通用 / 领域）各在这道题上**真跑一次**（probe），composer（aux 调用，只看轨迹与交付物，不看评分：交付了没有、形状合不合请求、有没有引用文档、前后一致）在"参考执行、两个候选"里采用一个或保留基座（Harness² 的 KEEP_BASE），采用的成为这道题自己的 harness，反思写一条带证据的教训进库（没有具体对比就不写）。
- 第 2、3 次：用这道题自己的 harness 跑，计分 → 修正后一次 / 稳态通过率。
- **对照臂**：W0 原样跑三次，不学——分出"学到了"与"再跑一次本来就会对"（噪声底 10～17%）。
- 对外的数：首次通过率、稳态通过率（第 3 次）、对照臂第 3 次；配对差与区间照 EVALUATE 的算法。封存计划与哈希先于第一次 test 尝试（规则 6），R 看不到任何评分（防泄漏不变：标签从不进 R，这比原来还严）。

**机制（改进器的第三种回路，`--phase repeat`）。** 复用已有的件：尝试路径不变；R 的会话按题开（输入只有这道题的轨迹、库、`package_api.md`，没有诊断与聚焦）；`try_task` 就是 probe（限在这道题上，每个候选一次）；composer 与反思是两次 aux 调用；库 = 采用的包（整个树）+ `EXPERIENCE.md`（适用情境、教训、证据）+ 索引，放在工作区 `library/`，下一道题的会话输入里有索引与条目文件（Harness² 的 `library.py`）。接受规则、留出筛选、两次确认在这条回路里**不用**：采用靠对比（无标签），正确性由第 2、3 次的计分在 final 上量——"确认"从装入门槛挪到最后的数。工作线不动（W0 仍是公共基座）；每道题的 harness 是基座 + 它自己采用的包。

**钱。** 每道 test 题：3 次计分尝试 + 2 次 probe + R 会话 + 两次 aux ≈ $5.5；50 道 ≈ $275；对照臂 150 次 ≈ $90；先在 train 的 20 道上预演一遍（$0 标签照样不进 R）≈ $110。合计约 $480，分三步批：预演 → test 的学习臂 → 对照臂。

**第二刀（2026-10-22，finance-v14 预演之后）：退单一比特。** 完全无反馈的预演（7 道题，$31.59）读到：R 只能修轨迹里可见的破坏（3 道失败题里 1 道），"只差一项"的失败它判成"都做对了"；通过的题上也开会话、composer 也采用（没退步，但每题 $5～8 白花）；一次通过不等于修好（hold 的证据条件在 agent 跳过读的路径上失效）。改成现实里存在的最弱信号——**请求有没有被接受**，一个比特：`[repeat] feedback = "outcome"`（默认）下第 1 次被接受的题不开会话、保留 base；没被接受的题，R 的 task.json 多一行 `outcome: not accepted`，提示词说"有错，不告诉你是什么，去找"。对外必须写明：学习用了第 1 次的通过 / 不通过这一个比特，没有任何断言、分数或细节。`feedback = "none"` 保留为 Harness² 的严格口径，可并列报。另两处：composer 对"两次写入与最后的话完全一样"的候选一律保留 base；提示词让 R 把"缺读"也当证据（没读过就覆盖第 1 行，单凭这一点就拦）。

**和之前的回路的关系。** 4b～4n 的留出筛选回路保留为"跨工作流泛化"的口径（封存 final 的 W\* 对 W0），不再是主数；本节的重复流是主数。两者都对外披露，各说各的问题。

**怎么判断它有没有用。** 预演（train 20 道）：第 1 → 3 次通过率的差减去对照臂的差 ≥ 3 道；composer 采用率与 KEEP_BASE 率；采用的包在第 2 次上出手的份额。test 上：稳态 − 首次 ≥ 5 道（10 个百分点）且高于对照臂的差，才对外讲。

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
| 4i 不退步的累积（v1.8） | §6.18：退步证据回灌 R（`regression_evidence` 三档）；前沿与合并（`frontier_max`、`merge_per_round`）；文字只增（`text_additive_only`）；启发式软信号（`heuristic_terms_min`）；`replay_max_false_trigger` 0.2 | 假上游：部分包进前沿、合并包条件化后零退步装入；research 档的退步轨迹进 R 的输入且不含留出题号；文字删改被 V0 拒、单独的 rewrite 包可筛。finance-v8 重冻结后：受保护集通过数逐轮不降，接受的包零可复现退步 | 代码 $0；v8 重冻结后 stream 约 $500 |
| 4h 可复现的接受（v1.7） | §6.17：V2b 的误触发门 `replay_max_false_trigger`；翻转确认（候选再读一次、W 补到两次）；接受规则改在可复现的计数上（净 ≥ 1、退步 0、全足迹的包修复 ≥ 2）；提示词一句 | 假上游：通过题上乱触发的 hook 在 V2b 被拒；一次读数翻转、确认时翻回去的题不计，包被拒；可复现的两道修复装入；`screened` 行记两组计数 | 代码 $0；finance-v8 从 v6 的头起步，约 $480 |
| 4g 学会提升概率（v1.6） | §6.16：`missing` 答法与 `audit_additive_only`、V2d 审核预演（误报率、命中、修订一次）、`audited` 台账与 `audits.json`、几个包的要求 | 假上游：一个 `missing` 审核在通过题上不报、在声明的失败题上命中并进筛选；一个在通过题上乱报的审核预演不过、修订后过；自由文本的审核答法是候选缺陷；台账与 `audits.json` 有它 | 代码 $0 |
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
| 2 | `hook_aux_calls` 默认 2；`schedule.rounds` 上限 12、`patience` 3；留出筛选的接受规则（净修复 ≥ 1、退步 ≤ 1、容差 1） | 是；2026-10-13 改为可复现的接受（§6.17：可复现净修复 ≥ 1、可复现退步 0、全足迹的包修复 ≥ 2） |
| 9 | 2026-10-13：v7 量出噪声底后，不加机制，只收紧两个门（§6.17）；v7 不跑 final；v8 从 v6 工作线的头起步 | 已定（用户 2026-10-13） |
| 3 | library 默认关（preset `automationbench_v2` 的 `memory.enabled = false`）；memory 包保留，R 的输入里不再有条目 | 是 |
| 4 | finance-v5 的预算：根 $500、final 份额 $200；基线从 finance-v4 导入（AttemptIdentity 相同时） | 到冻结前批准 |
| 5 | 对外预期：一次活动 dev 严格 +3～6 题；80% 只在部分得分口径上谈 | 是 |
| 5 | 2026-10-12：对外的数字是 final 上的绝对分并列 W0 臂，不追求比业界更严；划分两份（train / test）、基线阶段取消、无 dev 的活动按决定发布（§6.7 第三条、§10 阶段 4c）；流式学习另立阶段 | 已定（用户 2026-10-12） |
| 8 | 2026-10-12：A 类只能靠审核，而审核三次被拒都是"改过头"；框架要学会提升概率：单向审核（构造保证）、预演 V2d、误报累积进 R 的输入（§6.16，阶段 4g） | 已定（用户 2026-10-12："写吧，先改文档再改代码……学会提升概率"） |
| 7 | 2026-10-12：历次版本分数不动、失败的是同一批题（用户）；读轨迹后定：给 R 失败并排、加三个 cause、提示词对准工作方式（§6.15，阶段 4f）；v6 第 0 轮装入的系统提示正对着这三类，看它的效果再定下一步 | 已定（用户 2026-10-12："写吧，先改文档再改代码"） |
| 6 | 2026-10-12：finance-v5 第 0 轮 +2 不够，要的是一次活动提升足够高：一轮几个包并叠装、清单与提示词对准整体机制与审核 hook、流式循环、工作线跨活动（§6.1、§6.4、§6.7、§6.13；阶段 4d）；finance-v5 批式跑到底拿第一个数，v6 从 v5 的 W\* 起步流式跑 | 已定（用户 2026-10-12："跑第 1 轮，四条改动并行写"） |
