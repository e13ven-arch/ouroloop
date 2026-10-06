# M2：两类训练数据与模型构建

> 交付范围：本文件新增 actor/SFT/Tinker 与 controller 新训练验证归第二版，原详细规格保留；文中“首版”指本模块首次接入版本。第一版只保持已有 feedback/trainer/jobs/store 兼容，不要求训练账号或执行新训练。目标仍是两类监督分开、训练阶段不晋升。
>
> v0.3（2026-10-04，[DESIGN v0.7](../DESIGN.md)）：**v0.3 实现的只有网关记录里的 `role` 字段，其余是第二版计划。** 第一版的交付物改为“可演化的 agent 程序 + 最小运行壳 + pin 的模型”，agent 程序就是能力包，可以改循环本身（M0 §2（v0.3）：manifest `harness_package.v2` 增加 `agent` 组件，`run(ctx, task)`，ctx 只暴露 `model/tool/skills/limits/view`）。模型调用因此全部经宿主网关（M1 §1（v0.3）：网关与 broker 在宿主里，宿主为每次调用写轨迹记录，包写不了）。本模块随之改写第二版训练投影的**来源**：从网关日志读，不从包的任何记录读；教师移到网关；aux 调用不进训练数据；演化→冻结→采集/训练→换学生→在学生上再演化的顺序写进 K 的 schedule。M2 的状态不变：仍是第二版，v0.3 不训练模型、不建教师、不要求 Tinker 账号，也不为本模块写测试。本文件写定 M2 以后怎样读网关日志，让 M1 现在就按可训练的形状落盘（记 `role`），免得第二版再改日志格式而使 v0.3 的轨迹作废。每条规则标“（v0.3，2026-10-04）”表示写定日期，不表示 v0.3 实现。

## 1. 两条数据路径

| 路径 | 输入与允许的监督 | 输出/消费者 |
|---|---|---|
| 决策学习 | Observation + DecisionTrace + 有事实依据的 env/human FeedbackRecord | 既有 DataManifest → 决策 trainer → 校准/弃权配置 |
| actor 模仿学习 | M8 学生在环真实执行、环境成功的 student-visible trajectory；v0.3 起其载体是宿主写的网关日志中 `role="act"` 且教师 PASS 的调用（§2，v0.3，2026-10-04） | SFTDataManifest → Tinker SFT → GenerationArtifact |

保持旧规则：Jev/LLM 的决策回答、研究员意见、EVAL_ONLY 记录不能转为决策真值。

本轮增加 actor 蒸馏并保留独立 controller 的实际训练/装配路径；M4 根据数据可得性选择，应用无需同时训练两种模型。ResearchContext 只增加选择方法必需的数据目录/数量/适用性摘要，不把所有轨迹交给 R。新增 actor 模仿是单独的数据目的和入口；实施须审计 providers 文档与训练来源校验，不能删掉全局限制后让所有 LLM 输出可训练。teacher 模仿数据记录来源模型、材料许可/限制和采集条件；不把教师理由当作经环境验证的事实。

`completion_snapshot.v1` 仍只对 train 的 on_finish 状态评分：决策时捕获可复现快照、episode 后调用官方评分；失败或不可捕获返回 unknown。最终任务失败不自动否定此前独立有效的快照；冲突和修订沿用 conflicted/supersedes。Harbor 只给最终 reward 时不得推导 stop 标签。

### 1a. 训练投影的来源：网关日志（第二版计划，v0.3，2026-10-04 写定）

v0.6 的规格假定循环由宿主持有，所以 M8 的审计流/学生流由 `TurnReviewer` 挂在 Agent 里形成（M1 §2）。v0.3 起循环归包，上下文管理、重试、多采样都可能是包自己写的，宿主不再知道“学生当时看到了什么”——除了一处：包要模型作答，只能调 `ctx.model(messages, tools, role, effort)`，这一刻宿主看见的 `messages` 就是模型实际收到的全部输入。训练投影因此改为从这里读：

- **唯一来源是宿主写的网关日志。** M1 §1（v0.3）规定宿主在每次 `ctx.model` 调用落一条记录 `{call_id, role, effort, messages, tools, response, usage}`，每次 `ctx.tool` 调用落一条 broker 事件；两者都由宿主写，包没有句柄。训练投影、M8 的采集记录与 R 的轨迹读取都只读这两种记录，从不读包自己写的笔记、文件或“轨迹”。原因：包是被演化的对象，它写的记录既不可信（HSI 的 meta-evolver 就是因为 evolver “声称机制有效而轨迹显示从未调用”才存在，[SURVEY §5](../SURVEY_RSI_FRAMEWORKS.md) 第 9 条），也是 objective hacking 的入口（同 §5 第 3 条：gate 证据必须由固定代码写）。
- **记录的 `messages` 原样落盘，不重建。** v0.6 §2 第 4 步“重建每个 accepted turn 当时的学生输入”在 v0.3 变成读取：包控制上下文裁剪与压缩，宿主无法从外部复算它；逐字记录是唯一忠实的办法。`messages` 与 `response` 可能很大，日志行存摘要与 artifact ref，正文另存对象库；投影按 ref 取正文，身份以摘要为准。TargetHarness 在 v0.6 “固定上下文裁剪方法”（M1 §3）的要求随之改为“裁剪由包做，但每次调用的实际输入由宿主记录”，训练投影复现当时实际 actor 输入这一条不变、只是来源换了。
- **`role` 是训练能否使用一条记录的第一判据。这是 v0.3 唯一实现的一项。** `act` 是产生对环境动作或最终答复的调用；`aux` 是包内部的辅助调用（证据表、发送前的二次核对、关键决策的多采样、路由）。manifest 未声明的 role 在草稿检查时是结构错误（M0 §2（v0.3）），所以日志里不会出现第三种值。
- **教师在网关介入，只对 `act`（第二版）。** 采集模式下，宿主把 `role="act"` 的 response 先交教师，PASS 原样返回、REPLACE 以替换响应返回，包收到的就是被接受的那一条，对包透明：包不知道有教师，也拿不到原 proposal。日志对这条调用多记一个可选字段 `review_ref?`，指向 ReviewRecord（verdict、replacement_ref、rationale_ref、usage_ref）；v0.3 的记录里它不出现，不内嵌 review 对象。proposal 作为审计流保留在 ReviewRecord 里，不在 `response` 里。原因：教师看到的 `messages` 与模型看到的完全相同，M8 §2 “教师只看学生可见历史”自动成立；包不能通过改循环读到教师理由或原草稿。`aux` 调用不经教师：它们的输出不作用于环境，让教师审阅等于让教师参与写包。
- **`aux` 计入采集限额，永不进训练数据。** aux 的调用次数、tokens 与费用计入每次尝试的硬限额（K：总模型调用数、tokens、秒）与 DistillationPlan 的 collection_limits；但训练投影按 role 过滤，aux 的 `messages/response` 不进入任何 SFT 行，也不作为 context 示例。原因：aux 输出的正确性没有环境真值（环境只验证动作），训练它等于训练教师未审、环境未验的中间判断；计费则必须计，否则包可以把工作搬进 aux 以绕开采集预算。
- **包拿不到评分器、provider、凭据、对象库与结果。** 这是 M0 §2（v0.3）对 ctx 的定义；M2 据此不需要在训练投影里再做“包是否偷读了 oracle”的检查，但投影仍按 §2 第 2 步校验血缘与 partition，并在 SFT 行里扫 train 字面量（M0 §3 的同一规则）。

## 2. actor 数据构建算法

拟建 `build_sft_dataset(plan_ref, trajectory_refs, selection_ref, objects) -> DatasetBuild`；M8 不自建第二个 dataset builder。v0.3 起 `trajectory_refs` 指向按 episode 聚合的网关日志记录（宿主为每个 episode 写 TrajectoryRecord，其 `step_refs` 指向该 episode 的模型调用与 broker 事件，`student_messages_ref` 不再单独存在：学生可见输入就是各 act 调用的 `messages`），投影身份 `projection_ref = gateway_projection.v1`（v0.3，2026-10-04）。

1. 验证 plan、学生版本、目标 harness、动作与消息 schema；沿血缘检查所有任务事实属于外层 train，任务家族与冻结映射一致。v0.3 起目标 harness 的身份包含 agent 组件代码（包身份覆盖 agent 代码，M0 §2（v0.3））：同一模型、不同 agent 程序采的轨迹不是同一 plan 的数据。
2. 只选官方成功条件满足、无环境/运行器故障、执行记录完整的轨迹。初版不混 partial-success/离线改写/仅教师答题样本；排除原因逐类计数。没有成功轨迹返回 insufficient_data，不伪造正样本。v0.3 起“执行记录完整”指该 episode 的每次 act 调用都有网关记录且（采集模式下）都有 review_ref，每次工具调用都有 broker 事件；缺任一条即排除并计数为 `incomplete_gateway_log`（v0.3，2026-10-04）。原因：缺记录的 episode 无法证明学生输入是什么。
3. 使用 plan 在采集前冻结的内部 fit/validation family 分配；教师参考的任务示例仅来自 fit，validation 内容不能参与编写参考。重复轨迹、同任务前缀、变体和增强样本全部同侧。以 selection 声明每任务上限或权重，不把长任务变成隐式多数。
4. 每个训练目标的输入就是该 act 调用记录的 `messages` 与 `tools`，逐字读取、不重建（§1a，v0.3，2026-10-04）。审计流中的原 proposal、review 理由、私有 TeachingReference、oracle、后续消息不进入输入或目标——它们本来就不在 `messages` 里，投影另做字面量扫描确认（DT6-02）。同一 episode 内 aux 调用的输入与输出一律跳过（v0.3）。
5. 训练目标只取 `role="act"` 且教师 verdict 为 PASS 的 response（v0.3，2026-10-04）。这改变了 v0.6 的“训练所有合格的 accepted assistant turns，包括 PASS 与 REPLACE”：REPLACE 的替换响应仍进入学生历史并被真实执行（否则 episode 不能继续），但不作为训练目标。原因：替换响应是教师模型在教师上下文里写的文本，不是学生在该状态下被接受的行为；只训练 PASS 使数据集成为“学生自己做出、教师接受、环境确认”的三重过滤行为，三者缺一不训，与 §1 “不把教师理由当作经环境验证的事实”一致。REPLACE 的比例与分布照常记录并报告（M8 §4），高替换率说明任务难或学生弱，不说明数据多。无教师的 plain 采集（没有 review_ref）不进入 actor 模仿数据：它只有环境成功一重过滤。role mask：system/user/tool/参考示例的 assistant token 为 0；本次 PASS 的 act response token 为 1。多模态或未支持的消息类型显式拒绝。
6. 模型 renderer 处理 tool-call 编码、边界与 EOS；固定 tokenizer/renderer/mask policy 的版本和摘要。允许全会话多目标或等价逐步样本，但不可让同一目标被重复加权；每次 act 调用的 `messages` 已是该步实际输入，逐步样本直接按记录成行，不再有“实际上下文裁剪后存在差异时必须逐步重建”的分支（v0.3）。
7. 超长样本首版排除并计数，不从任意 token 处截断 JSON/工具结果。记录原始/保留轨迹数、tokens、任务家族数、PASS/REPLACE 分布与 act/aux 调用数（v0.3）；不能把训练集近乎排空仍返回 ready。

没有 fit/validation 家族就 insufficient_data；最小 smoke 可明确使用非成绩用途的单任务数据检查管线，但不能据它选模型或报告泛化。存在固定公共示例时，采集与无教师评测都使用相同示例，且示例 assistant 不承受训练 loss。

## 3. 训练、checkpoint 与装载

在 `train_artifact` 既有纯构建边界增加 role/method 分派，保持旧调用签名与默认 decision 行为。目标接口：

```text
build_task_dataset(...) -> 既有决策 DatasetBuild          # ouroloop/evolve/build.py:164，现有
build_sft_dataset(...) -> DatasetBuild(status, dataset_ref, exclusions)
train_artifact(..., role=actor 或 controller) -> TrainingBuild
```

M0 的 GenerationArtifact 由 ready 训练结果引用；不修改 active/champion。actor 探针交 M1 provider：加载确切底座/adapter、生成合法 ChatTurn、工具消息往返、接受历史重放。controller 保留 `.decide()` 的输出类与 schema 探针，按需独立 calibration。

配方显式记录 `init_model_ref`，默认从 DistillationPlan.student_release_ref 的 actor 初始化；若策略选择从同底座重训而非延续 adapter，必须在冻结配方及比较中明确。后端不支持该 checkpoint 初始化时预检失败，不能偷偷退回 base。一次新实验允许按配方重置 optimizer，但必须记录；同一被中断作业的精确恢复仍须恢复 optimizer/游标，二者不能混称。

Tinker trainer 首版仅 LoRA SFT：validate 模型/renderer/预算 → 创建 client → 固定数据顺序与 batch 计划 → forward/backward → optim step → validation → checkpoint → sampling artifact → probe。记录 base、rank、学习率、epochs/steps、seed、batch/sequence 上限、SDK/renderer、数据摘要、实际 token 和费用；参数须有边界，研究员不能取消上限。checkpoint 选择只看内部 validation，dev 任务只由 M3 gate 使用。

Tinker 训练状态与 sampling 权重不是同一产物。恢复保存 training state、已确认 step、数据游标及 optimizer 状态；无法确认最近 optim step 是否生效时，不盲目重放该 step。由 M5 保持 unknown、核对或终止该候选，已花费用不抹去。不能只加载采样权重就宣称精确恢复优化器。

训练不满足服务访问、数据权限、模型能力或计量条件时，在提交前失败。外部作业失败后保留诊断和费用；没有 ready artifact 不创建可装配 actor。数据少或只学会格式时回到采集/工作流，不自动升级模型规模、启动 RL 或增加专家池。

### 3a. 演化、冻结、训练、换学生的顺序（第二版计划，v0.3，2026-10-04 写定）

第二版的一次蒸馏按下面的顺序进行，顺序写进 K 的 schedule（第二版 preset 字段 `distillation.stages`，固定为 `["evolve", "freeze_release", "collect_train", "switch_student", "re_evolve"]`，freeze 后 R 不能改序、不能跳步）：

1. **evolve**：在 pin 的模型（首版为 Claude Opus 5.5）上演化 agent 程序，得到 W*；这是 v0.3 的活动本身。
2. **freeze_release**：以 W* 的 release（agent 包 + 模型身份 + K）作为 DistillationPlan 的 `student_release_ref` 与 `target_harness_ref` 冻结；此后采集与训练都在这一个 release 上，W* 继续演化不影响已冻结的 plan。原因：采集一半换包会让同一数据集里的学生输入来自两个不同的循环。
3. **collect_train**：以该 release 的 agent 包为循环、学生模型为 `ctx.model` 背后的模型、教师在网关审阅 act 调用，在 train/fit 任务上采集；按 §2 建集、§3 训练、探针。
4. **switch_student**：训练产物经 M1 装载探针与 M3 无教师 dev gate（学生 release 对同 h 的基线 B，比较见 [DESIGN §8](../DESIGN.md) 表“有效 harness 与蒸馏 actor”）后才能形成新 release；gate 不过则保留原 release，费用照记。
5. **re_evolve**：以学生为 pin 的模型重新开一个活动，在学生上演化 agent 程序。学生上的 **W0 是更简单的种子**：M0 §2（v0.3）的默认 agent（`agent/default_agent.py`，即今天的宿主循环移植成包）加上 Opus 活动中被接受的文本组件（memory/skills），不是 Opus 上演化出的整个 agent 程序。Opus 上的 W* 可以作为该活动第一轮的一个候选，与学生 W0 过同一个 gate；它不是种子。

**harness–模型共适应是一项成本，不是假设。** 在 Opus 上演化出的循环（更高 effort、多采样、发送前核对）以 Opus 的判断力为前提，放到 9B 学生上可能费用更高、收益更低，甚至为负（Harness-Zero 的 h* 之所以在学生上验证，正是因为 base+h* 是学生带 h* 的成绩，[ROADMAP §3](../ROADMAP.md)）。所以：(a) re_evolve 是必经阶段，不能把 Opus 的 W* 直接装给学生就报告蒸馏完成；(b) 报告里“蒸馏 app”的成绩是学生 + 学生上演化出的包，与 Opus + Opus 上的包并列，再加学生 + Opus 的包作为共适应成本的直接读数（三者都是无教师、同 split）；(c) 每一步的费用计入同一预算根，re_evolve 的预算在 freeze 时预留，不能把钱全花在前四步。

## 4. 决策模型独立性与漂移

首个参考 controller 仍是 `worker.stop`，输入只含当时可见事实，标签回答“此刻是否满足完成条件”。它不天然回答“继续的期望收益是否大于费用”。后者需要分支实验和明确成本效用，暂不混用标签。

actor 的版本是 controller 数据 applicability 的一部分。换 actor 后允许保留逻辑上兼容的历史完成标签，但必须重新检查输入分布、校准及完整任务行为；阈值不自动继承。actor 自己也能生成 finish 请求，controller 只是独立检查这一动作，不要求把所有停止意识从 actor 权重中删除。

v0.3 的宿主不问任何决策点：包返回即完成（termination `finished`），硬限额到即 `budget`；StopGuard 的回推是默认 agent 内部的事件，不是 termination（M1 §3（v0.3））。第二版：把 stop 决策点放到完成协议的边界，controller 的 observation 来自宿主在该边界看到的事实（最终消息、broker 事件、剩余限额），不来自包内部状态；届时 agent 包的身份也是 controller 数据 applicability 的一部分：换了循环，完成请求出现的时刻与前文分布都会变（v0.3，2026-10-04 写定，第二版实现）。

## 5. 用例

| ID | 条件 | 预期 |
|---|---|---|
| DT6-01 | PASS/REPLACE 混合成功轨迹，另含失败/中断轨迹 | v0.3 起：PASS 的 act 调用进入 SFT；REPLACE 的替换响应留在学生历史、不作目标、计入 REPLACE 分布；失败/中断轨迹排除并列原因（v0.6 原预期为 PASS/REPLACE 都进入，2026-10-04 改） |
| DT6-02 | teacher 私有标记、原草稿和 oracle 有唯一字符串 | 所有训练输入和目标均无这些字符串 |
| DT6-03 | 工具 JSON、公共 assistant 示例、EOS 与超长样本 | mask 与边界正确；示例不计 loss；超长不破坏动作 |
| DT6-04 | 同任务多前缀、变体；dev 或内部 validation 答案进入教学参考 | family 不跨组；污染使构建失败且未发训练 |
| DT6-05 | actor artifact 被送入旧 decide probe | role 分派到生成探针；错误 schema 明确拒绝 |
| DT6-06 | optim step 后响应丢失，重启 | 不盲重训；保留收费与未确认状态 |
| DT6-07 | 仅有最终 reward、教师说 stop=yes | 没有 env/human 状态标签，不启动 controller 训练 |
| DT6-08 | 训练成功但 gate 未运行；或数据为空 | 前者无晋升，后者 insufficient_data 且无 GPU 请求 |
| DT6-09（第二版） | 一个成功 episode 的网关日志含 act 与 aux 调用；包另在自己的工作目录写了一份“轨迹” | SFT 行只来自 act 记录的 `messages/response`；aux 的调用数、tokens、费用计入采集限额与报告，但不在任何 SFT 行；包写的文件从未被投影打开 |
| DT6-10（第二版） | 采集模式下教师对一次 act 调用 REPLACE | 包收到替换响应并据此继续，日志该条记 review_ref，proposal 只在 ReviewRecord；该步不进训练目标；同一 episode 其余 PASS 步照常进入 |
| DT6-11（第二版） | 同一 episode 的 act 调用缺一条网关记录或缺 review_ref；另一 episode 的工具调用缺 broker 事件 | 两者都排除，计数为 incomplete_gateway_log；不以包的记录补齐 |
| DT6-12（第二版） | 无教师的 plain 采集成功轨迹被送入 build_sft_dataset | 排除并计数（没有 review_ref）；可作 task_feedback，不作 actor 模仿数据 |
| DT6-13（第二版） | 同一学生模型、两个不同 agent 包采的轨迹送入一个 plan | plan 校验拒绝：目标 harness 身份含 agent 代码，不是同一 plan 的数据 |
| DT6-14（第二版） | preset 的 distillation.stages 被改序或跳过 re_evolve；或把 Opus 的 W* 作为学生的 W0 | freeze 拒绝；学生 W0 由固定代码构造为默认 agent + 文本组件，Opus 的 W* 只能作为候选进 gate |

DT6-09～14 于 2026-10-04 随 v0.3 写定，验收归第二版；v0.3 不为它们写测试。复用 `test_evidence_training.py`、`test_train_config.py`、`test_jobs.py`；第二版新增少量生成 renderer/mask 与 fake Tinker 测试。实际云 smoke 单列，不放默认离线测试。第二版的测试以 M1 §1（v0.3）落盘的网关日志格式为 fixture；格式一变，先改 M1 再改这里，不在 M2 另立一份 schema。
