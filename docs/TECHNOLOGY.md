# 技术选型与实现原则（v0.6）

> 2026-09-30 修订首/二版边界。本文决定新设计如何落地，具体字段见 [M0](modules/00_CONTRACTS.md)。外部项目是设计来源；不是 benchmark 比较对象。当前依赖以 `pyproject.toml` 为准，以下新增依赖均未接入。

## 1. 选型与责任边界

| 能力 | 选择 | 采用理由与边界 | 首次交付 |
|---|---|---|---|
| agent 执行 | 现有 agent/loop.py、provider、ToolSurface、StopGuard | 已接真实工具；首版补包绑定与 hook，第二版补教师，不重写 Agent | 首版必需 |
| 能力包代码演化 | 内容寻址 bundle/manifest + 固定 package API + Docker 隔离 worker/工具 broker | 包内生成工具/中间件代码，复用原工具权限与评测；不在控制面 import 候选；只含 skills/memory 的包由宿主装配，不启动 worker | 首版必需，先文本组件后代码组件，M0/M1/M4/M5 |
| 业务任务 | 现有 AutomationBench adapter | 保持上游评分语义、任务划分、直接模拟环境 | 必需 |
| 沙箱任务/并行 rollout | 可选 Harbor adapter | 复用原生任务、环境和 trial；不自行再造容器调度 | 明确的 Harbor 任务接入时 |
| actor 生成训练 | 首选 Tinker 托管 LoRA SFT | 复用 GPU 调度、checkpoint 和 sampling；训练能力不绑定供应商 | 第二版；启用后检查账户 |
| controller 训练 | 现有 trainer/TDE 作业与 serving | 保持有限动作、校准、弃权协议及 actor/controller 分离 | 首版兼容已有能力；新增验证第二版 |
| 工作流发现与 R 更新 | 现有 research 流程/provider/campaign | 从反馈提出注册候选，验证后回用；不增加研究服务 | 必需 |
| 教师与适配 | 现有 provider + 受限参考读取 | M4 发现源方法，M8 映射目标动作/逐步审阅 | 第二版；DeepAgents 按实需再评估 |
| 编排/版本/预算 | 现有 `evolve/execution.py`、contracts、release、store | 全部候选复用同一构建—评测—安装流程 | 必需 |
| 存储 | 内容寻址 JSON、JSONL、模型文件/远端 immutable ref | 本地单控制器可审计、易恢复；无新数据库/消息队列 | 必需 |

Harbor 是 Terminal-Bench 团队维护的 agent 评测与环境运行基础设施，可运行任务、沙箱和 agent trial；它不负责 ouroloop 的研究策略晋升。[官方仓库](https://github.com/harbor-framework/harbor)、[任务格式](https://docs.harborframework.com/core-concepts/tasks/overview)

DeepAgents 来自 LangChain，提供规划、文件、上下文和子 agent 等 harness 能力。[官方仓库](https://github.com/langchain-ai/deepagents)、[架构说明](https://docs.langchain.com/oss/python/deepagents/overview)

Tinker 来自 Thinking Machines Lab。SDK 与 cookbook 可读可用，但训练服务是托管服务，下载 SDK 不等于获得可自部署的 GPU 训练平台。[官方产品](https://thinkingmachines.ai/tinker/)、[SDK](https://github.com/thinking-machines-lab/tinker)、[cookbook](https://github.com/thinking-machines-lab/tinker-cookbook)

首版只采用一套固定 Docker 隔离 worker，不引入集群调度或通用插件平台。只有含代码的草稿检查（含 tests/*.py）与运行含代码的包（manifest 有 tools/hooks 条目或运行 bundle 含 .py，M0 §2）使用 worker；只含 skills/memory 的包由宿主装配，不启动隔离 worker。允许的组件种类由 K 在 freeze 时固定；K 允许代码时 freeze/smoke 须确认合格 worker，否则不能以该配置启动；活动中 worker 不可用按基础设施故障处理，重试后暂停活动（M0 §3），不收窄组件种类、不 in-process 执行。因此首版可先交付文本组件，再交付代码组件。非特权、无候选网络/宿主仓库/工作区（bench/<domain>/ 下的 results.jsonl、grading.jsonl，以及 rsi/objects 对象库）/daemon socket、只读运行包及资源限制由固定代码配置；业务 I/O 通过宿主 broker 复用原权限和预算。默认容器配置和 AST 校验不能自动保证这些边界，实施须覆盖 M1/集成边界用例。[Docker 安全机制](https://docs.docker.com/engine/security/)

## 2. Tinker 接入准则

本节保留为第二版设计。首版代码演化/评测/发布/递归不要求 Tinker 账号、teacher 或 GPU trainer；缺训练服务不阻止首版运行。未来选用 Tinker 时才执行以下接入检查。

首个候选底座为 `Qwen3.5-9B`，官方当前列为支持模型；这是工程起点而非性能保证。实施时通过服务实际返回值确认模型 ID、LoRA 能力、上下文和 renderer，再冻结计划。不得仅按字符串相近切换到另一个底座。教师可用现有 GLM-5.3 或其他已授权强模型，身份与学生分开。

训练路径：建立 LoRA training client → renderer 构造 token/loss mask → `forward_backward` → `optim_step` → 保存可恢复训练状态 → 保存用于 sampling 的权重引用。这些是官方 SDK 的职责边界，不在本项目实现 GPU 集群管理或优化器内核。采样 provider 同时支持未训练底座和候选 adapter，用同一工具消息格式进行 before/after 比较。

前置检查必须覆盖：账号与凭据、真实可训练模型、tool-call 消息往返、assistant mask、checkpoint 恢复能力、sampling 接口、训练与采样收费上界，以及权重可导出性。选择最小真实请求验证时仍需用户提供的运行预算；文档设计不授权付费实验。

原有 `.decide()` 装载探针仅适用于决策模型。actor 探针检查生成、工具调用解析和模型身份，不进行分类校准；服务 ref 无法查询、格式不能复现或模型不可用时拒绝发布。托管 checkpoint ref 与下载后的 artifact digest 分别记录，不能把可变别名视为冻结权重。

依赖放可选 extra/训练 worker，保持核心安装轻量。当前核心 Python >=3.11；若所锁 SDK 需要更高版本，在独立训练环境声明，不无故提高所有用户门槛。首次实现锁定实测 SDK/renderer 版本及模型 ID，记录到 job manifest；本文不捏造已验证版本号。

HF Transformers/PEFT 本地 SFT 是后续替代后端，只有托管限制或本地部署需求成立才接入；不在首轮并行维护两套生成训练栈。不能因 Tinker 未开通而伪造训练成功或静默换后端。

## 3. Harbor 与 DeepAgents 接入准则

Harbor adapter 只做三件事：将冻结任务/agent 配置传入 Harbor，启动或接回 trial，将合法 observation、最终 verifier 结果、轨迹引用和费用映射回公共记录。任务环境、清理和该 trial 的并发由 Harbor 负责；实验层配对、授权、安装与总预算由 ouroloop 负责。

自有学生 agent 通过 Harbor 的 agent 接口执行，教师审阅仍在动作执行前发生。任意第三方 agent 不保证可截获该位置，能力不足就只开放普通评测。不要通过最终轨迹的文本替换冒充学生在环采集。[官方 agent 接口](https://docs.harborframework.com/core-concepts/agents/pre-integrated-agents)

后端需声明 `review_before_execute`、`snapshot_for_completion`、`resume_trial` 等实际能力。只有最终 reward 的后端不能产生中途 stop 标签。不把 AutomationBench 强制搬进 Harbor；转换任务格式必须独立验证评分等价性，不属于第一蒸馏闭环。

DeepAgents 作为依赖调用，既不复制源码也不把现有 Agent 再包进一层完整 agent 调度。首个教师只需受限读取参考和返回结构化 review；这些能力用现有 provider 足够时不安装 DeepAgents。只有注册工作流实际需要其能力才引入，生产包也只声明其真正使用的依赖。

## 4. 参考设计如何转为独立实现

参考 Harness-Zero 的固定提交 [`aa63a7751997969c2880896f9df3098c7f650fe2`](https://github.com/metaevo-ai/harness-zero/tree/aa63a7751997969c2880896f9df3098c7f650fe2)与[论文](https://arxiv.org/html/2609.24974v1)。借鉴的是方法与责任分工；**不复制其代码**。最终交付的生产源码、测试命名、默认提示、依赖与产物 schema 不带该项目名称；开发文档保留来源，便于核对设计依据。

| 参考逻辑 | ouroloop 落点 | 本项目必须补齐的边界 |
|---|---|---|
| 分析失败、演化源 harness、执行验证，再适配到目标 h | M4/M1/M3；第二版 M8 | 首版允许包内工具/中间件代码及技能/经验，固定核心不变；实际执行与回归验证 |
| 源工作流、教师私有指导、固定目标 harness 分开 | M0、M8 | 类型化身份、数据分区与许可材料 |
| 学生提议，教师 PASS/REPLACE，接受后执行 | M1、M8 | reviewer 插入真实执行前；多工具调用在执行前整组校验/接受 |
| 原提议、review、接受轨迹分开保存 | M0、M8 | 审计流不进入学生训练或部署 |
| 成功轨迹的 assistant SFT 与 mask | M2 | 按任务家族划分，保留 exclusion，区分模仿与真值标签 |
| Tinker 训练与固定目标 harness 评测 | M2、M3 | 单预算、崩溃恢复、同底座比较、完整 release |
| 最小模型+harness 部署 | M3 | 独立 controller 可选、模型装载验证、工具绑定与兼容性 |

论文 §3.1 与附录 A 将源 harness h* 的失败驱动演化、适配及固定目标 h 分开描述；代码仓库的源演化通过 coding-agent skill 等工作流衔接，不据此声称它已是统一的多代 RSI 编排器。我们借鉴完整方法链，采用现有声明式策略和实验器独立实现；不要求照搬其多代理开发流程。[论文](https://arxiv.org/html/2609.24974v1)

首版额外参考其 [能力库演化 skill](https://github.com/metaevo-ai/harness-zero/blob/aa63a7751997969c2880896f9df3098c7f650fe2/.agents/skills/student-harness-evolve/SKILL.md)：演化限制在组件目录，优先泛化已有机制，合并重复项，按真实调用与反馈决定继续/停用。我们使用一个串行 R 流程产出受限 patch，不复制实现；候选采用前比较完整固定 dev 清单，包含本次对照版本成功的任务，停用本身也经同一 gate。h* 可在首版直接发布，模型蒸馏是第二版后续路径。

该 skill 的 h* 产出流程：共 3 轮；第 1 轮取裸 baseline 失败的 train 任务，第 2～3 轮取上一轮仍失败的任务；已通过的任务退出，不做回归检查。每轮最多 10 个分析子 agent 逐题读失败轨迹，写带泛化论证的笔记，子 agent 不直接改库；主 agent 汇总去重后统一改库，先做 import/加载/pytest 冒烟，再只在该轮失败任务上提交评测；连续两轮没有收益证据的组件移出 manifest（文件保留）。组件在 agent 进程内用 importlib 加载，任务环境与 verifier 在 Harbor 容器一侧。三个学生库最终共 12 个中间件、3 个 skill、3 个工具；工具只出现在 USPTO（化学校验），SpreadsheetBench 与 AppWorld 没有工具。中间件种类包括输出/提交/答案/完整性守卫、重试循环守卫、失败恢复提示、空回合守卫、turn_budget、bootstrap 指令和 context_guard。论文结果（学生 base@h / base+h* / 蒸馏@h）：SpreadsheetBench 31.0/39.0/44.0，AppWorld 26.8/48.2/58.9，USPTO 12.0/38.0/30.0，宏平均 23.3/41.7/44.3；h* 相对 base 增益 +8.0/+21.4/+26.0 个百分点，功效推论见 [ROADMAP](ROADMAP.md) §4。其 h* 在学生上验证（论文 base+h* 是学生带 h* 的成绩）；首版 actor 为 GLM-5.3，第二版换 Qwen3.5-9B 学生时 h* 须在学生上重新演化。

| 环节 | Harness-Zero | ouroloop 首版 | 差异依据 |
|---|---|---|---|
| 验证范围 | 只在该轮失败任务上评测，已通过任务退出 | 诊断可聚焦失败 train；采用前复用完整固定 dev 配对 gate，记录逐题变化 | 按冻结的整体收益与退化比例门槛采用；仅说明该 dev 范围的表现，不承诺全部历史成功任务保持成功，不另建回归系统 |
| 组件执行与隔离 | 组件在 agent 进程内 importlib 加载；Harbor 容器分开任务环境与 verifier | tools/middlewares 代码只在 Docker worker 执行，经 broker 调业务工具；只含 skills/memory 的包由宿主装配 | 直连模式下世界状态在宿主进程内，所有分区的断言都在本地任务数据里，能读文件的生成代码就能读到 dev/test 答案 |
| 失败诊断 | 每轮最多 10 个子 agent 逐题笔记，主 agent 汇总去重后改库 | 输入为 train/fit 失败摘要与断言明细，dev/final 不给明细和轨迹；复用现有 research provider 串行 map/reduce：每个纳入本轮的失败任务一次笔记调用（每轮读取笔记数有上限，久未纳入者优先，覆盖如实记录；须论证适用于一类任务，不写单题答案），再一次汇总调用产出至多一个 patch 或结构化 next_action；笔记为 train 血缘 artifact 并计费，每轮笔记数上限由 preset/K 固定 | 单控制面串行与单预算，不建子 agent 平台 |
| 冒烟与修复 | 主 agent 改库后先做 import/加载/pytest 冒烟，再提交评测 | 草稿包在同一 operation 日志内做 package_check → isolated_probe → package_test；只含 skills/memory 的草稿只做结构/路径/manifest 检查；失败时只把检查输出回传 R，在 preset/K 固定次数内修订，用尽则本轮以无效提案结束；通过才冻结进 Experiment，冻结后源码不变 | 修复有界且逐次记录计费，冻结实验可复现 |
| 组件优先级与挂点 | 学生库以中间件为主，工具只在 USPTO | memory/skills → on_finish 守卫 → before_tool/after_tool 守卫 → 包工具；除 context_guard 外的中间件都落到三个挂点：turn_budget 以 after_tool feedback 近似，空回合即完成请求（`ouroloop/agent/loop.py:149`）由 on_finish 处理（stop 为 end/other 时才经 on_finish，`loop.py:150`；因长度截断的空回合直接结束），bootstrap 指令由宿主注入的 memory/skills 承担 | 改上下文（context_guard 类）不支持，是有意取舍：TargetHarness 固定上下文算法 |

开发时可按以下入口核对控制顺序、表示与恢复思路，不拷贝实现：

- 教师干预和学生循环：[student.py](https://github.com/metaevo-ai/harness-zero/blob/aa63a7751997969c2880896f9df3098c7f650fe2/src/harness_zero/student.py)、[teacher.py](https://github.com/metaevo-ai/harness-zero/blob/aa63a7751997969c2880896f9df3098c7f650fe2/src/harness_zero/teacher.py)、[review.py](https://github.com/metaevo-ai/harness-zero/blob/aa63a7751997969c2880896f9df3098c7f650fe2/src/harness_zero/review.py)。
- 目标 harness 与轨迹记录：[student_harness.py](https://github.com/metaevo-ai/harness-zero/blob/aa63a7751997969c2880896f9df3098c7f650fe2/src/harness_zero/student_harness.py)、[store.py](https://github.com/metaevo-ai/harness-zero/blob/aa63a7751997969c2880896f9df3098c7f650fe2/src/harness_zero/store.py)。
- 成功轨迹投影、mask 与训练：[sft.py](https://github.com/metaevo-ai/harness-zero/blob/aa63a7751997969c2880896f9df3098c7f650fe2/src/harness_zero/sft.py)、[dataset_utils.py](https://github.com/metaevo-ai/harness-zero/blob/aa63a7751997969c2880896f9df3098c7f650fe2/src/harness_zero/dataset_utils.py)、[train.py](https://github.com/metaevo-ai/harness-zero/blob/aa63a7751997969c2880896f9df3098c7f650fe2/src/harness_zero/train.py)。

该方法没有提供通用“训练完自动生成最小 harness”的编译器；目标 harness 由实现者提供，在一次蒸馏内固定，源 harness 仍可演化。不要把论文结果、任务范围或短代码量视作本项目已具备同等效果。依赖承担的执行和训练工作也是完整系统的一部分。

项目内可直接复用的实现：`policies.py` 的注册/装配、`agent/loop.py` 的工具执行、`evolve/pipeline.py` 的纯 artifact 构建边界、`evolve/execution.py` 的状态机与共享预算、`release.py` 的 preflight/CAS。`research/loop.py` 和 `research/campaign.py` 继续承担自动改进及方法比较，接入新的构建方法；避免平行 registry、release、scheduler 或研究引擎。

## 5. 防止实现膨胀的审查标准

- 一个公共概念只有一个类型来源；默认纯函数与现有对象库。新增持久化类型必须被两个真实边界使用或承担恢复职责。
- 复用现有 researcher 接口和 R 策略，只扩展必要的候选/上下文与方法分派；不创建独立研究服务、专用模型训练或多专家系统。自动发现和递归回用属于首版，不能以减层为由改成人工职责。
- M8 只新增教学适配、教师与采集协调；source 候选选择归 M4，数据、训练、安装分别归 M2/M3。用命名子操作扩展一次 build，不新建 distillation 引擎。
- provider/trainer/backend 能力显式声明；包内入口只由固定隔离 worker 装配，控制面不动态 import 候选；缺能力明确报错，不用 fallback 隐藏缺口。
- 教学来源与正式发布通过同一 executor 的 purpose 分派，源验证完成后不强制再跑生产 gate；换 h 和补采复用 operation/预算日志。只新增必要字段及 validated 终态，不建设另一套工作流引擎。
- 默认单控制面、串行实验与隔离 worker、一套重试策略、一次叶子收费；复合工具的内部调用均经 broker 记账，不扩展并行集群。
- 测试锁定因果边界：动作未接受不能执行、教师不能泄漏、无教师能跑、模型确被加载、失败不能晋升。不要逐字段测试数据类或为凑用例重复实现。
- 开发提交写明复用何处、为何新增文件、哪些限制仍在；不按代码行数硬压逻辑，也不为未来功能提前建空插件。
