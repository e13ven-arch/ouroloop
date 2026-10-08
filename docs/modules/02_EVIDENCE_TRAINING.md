# M2：两类训练数据与模型构建

> 交付范围：本文件新增 actor/SFT/Tinker 与 controller 新训练验证归第二版，原详细规格保留；文中“首版”指本模块首次接入版本。第一版只保持已有 feedback/trainer/jobs/store 兼容，不要求训练账号或执行新训练。目标仍是两类监督分开、训练阶段不晋升。
>
> v0.3（2026-10-04，[DESIGN v0.7](../DESIGN.md)）：**v0.3 实现的只有网关记录里的 `role` 字段，其余是第二版计划。** 第一版的交付物改为“可演化的 agent 程序 + 最小运行壳 + pin 的模型”，agent 程序就是能力包，可以改循环本身（M0 §2（v0.3）：manifest `harness_package.v2` 增加 `agent` 组件，`run(ctx, task)`，ctx 只暴露 `model/tool/skills/limits/view`）。模型调用因此全部经宿主网关（M1 §1（v0.3）：网关与 broker 在宿主里，宿主为每次调用写轨迹记录，包写不了）。本模块随之改写第二版训练投影的**来源**：从网关日志读，不从包的任何记录读；教师移到网关；aux 调用不进训练数据；演化→冻结→采集/训练→换学生→在学生上再演化的顺序写进 K 的 schedule。M2 的状态不变：仍是第二版，v0.3 不训练模型、不建教师、不要求 Tinker 账号，也不为本模块写测试。本文件写定 M2 以后怎样读网关日志，让 M1 现在就按可训练的形状落盘（记 `role`），免得第二版再改日志格式而使 v0.3 的轨迹作废。每条规则标“（v0.3，2026-10-04）”表示写定日期，不表示 v0.3 实现。
>
> v2（2026-10-08）：用户对 [V2_TRAINING_PIPELINE](../design/V2_TRAINING_PIPELINE.md) §7 答“按默认”，八项按默认值定下。本文据此改了：§1 的来源规则（血缘加选择，取代 EVAL_ONLY 闸门）、§1a（不建原文对象库、精确值脱敏、教师读 K）、§2 第 5 步（目标 `accepted`，`pass_only` 只作 dev 消融）、§3a 第 2–3 步（学生在 W0 下、教师带 K 审阅）、新增 §3b（`[train.<role>]` 配置形状与 actor 产物契约、Qwen3.5-9B + Tinker 参考实现、`ouroloop/train/` 与 `[train]`/`[train-jobs]` 命名）、DT6-01/07/10/12/13/14；同日复核后另补 §1 表的决策学习一行、§2 首段（不另建 TrajectoryRecord）、§3 的 role 名、§3a 首段与第 5 步、§4 首段的注。其余决定不在本模块重复，见该文 §7：轨道 B 在 X1 读完后再定开不开，老师写在计划文件里；test 复用规则；finance、hr、marketing、sales、support 五域合训一个学生，operations 整域留出报 LODO；`contact_human` 在本基准上不作效果主张。标“（v2，2026-10-08）”的是写定日期，不表示已实现：代码一律等 support 两份 final 评完、H1 算完才动，从瘦身第一批开始（V2 §1“时机”）。
>
> 2026-10-08（support 两臂对照停止后，[JOURNAL](../JOURNAL.md) 2026-10-08）：对照已由用户停止，上段“等 support 两份 final 评完、H1 算完”的条件作废，代码从瘦身第一批合入之后开始。同日按 V2 的 Tinker/RunPod 核价更正同步 §3b：`[train.actor]` 的自托管写插件模块路径、由 `JobTrainer` 运行（`module:Class` 只用于测试与 demo）；默认配方只做 LoRA，α 在 Tinker 上设不了，lr 改为扫描；serving 默认 `openai_compat` 指向 RunPod 上服务合并权重的 vLLM（V2 §3.3、§3.5、§3.7、§7 第 6 项）。被取代的原文保留在注里。

## 1. 两条数据路径

| 路径 | 输入与允许的监督 | 输出/消费者 |
|---|---|---|
| 决策学习 | Observation + DecisionTrace + 标签（血缘 `env:*`/`human:*`/`model:*`，按数据计划的 `sources_fit` 进拟合；阈值只用 env，评测只用 env 与 human；v2，2026-10-08，原为“有事实依据的 env/human FeedbackRecord”） | 既有 DataManifest → 决策 trainer → 校准/弃权配置 |
| actor 模仿学习 | M8 学生在环真实执行、环境成功的 student-visible trajectory；v0.3 起其载体是宿主写的网关日志中 `role="act"` 的调用（§2，v0.3，2026-10-04）；目标为教师接受的动作，即 PASS 与 REPLACE 的动作部分（§2 第 5 步，v2，2026-10-08） | SFTDataManifest → Tinker SFT → GenerationArtifact |

（v2，2026-10-08，[V2_TRAINING_PIPELINE](../design/V2_TRAINING_PIPELINE.md) §0.1 第 3 点、§2.5；M0 §4）旧规则“Jev/LLM 的决策回答、研究员意见、EVAL_ONLY 记录不能转为决策真值”改为血缘加选择：模型给的标签（`judge_vote`、`teacher_review`）记 `model:<identity_ref>`，只有数据计划的 `sources_fit` 选中时才进拟合，与 env 冲突时以 env 为准；评测、阈值、gate 的真值只来自 `env:*` 或 `human:*`；评分信息永不进 actor、teacher、judge 的输入，也不进 SFT 输入。

本轮增加 actor 蒸馏并保留独立 controller 的实际训练/装配路径；M4 根据数据可得性选择，应用无需同时训练两种模型。ResearchContext 只增加选择方法必需的数据目录/数量/适用性摘要，不把所有轨迹交给 R。新增 actor 模仿是单独的数据目的和入口；实施须审计 providers 文档与训练来源校验，不能删掉全局限制后让所有 LLM 输出可训练。（v2，2026-10-08）全局限制由上一段的血缘加选择取代：LLM 输出能否进拟合看 `sources_fit`，阈值与评测仍只收 env/human；providers 与 ledger 的注释随瘦身第一批改，闸门改血缘在 v2.0（M0 §4）。teacher 模仿数据记录来源模型（血缘 `model:<teacher_ref>`）和采集条件；不把教师理由当作经环境验证的事实。

`completion_snapshot.v1` 仍只对 train 的 on_finish 状态评分：决策时捕获可复现快照、episode 后调用官方评分；失败或不可捕获返回 unknown。最终任务失败不自动否定此前独立有效的快照；冲突和修订沿用 conflicted/supersedes。Harbor 只给最终 reward 时不得推导 stop 标签。

### 1a. 训练投影的来源：网关日志（第二版计划，v0.3，2026-10-04 写定）

v0.6 的规格假定循环由宿主持有，所以 M8 的审计流/学生流由 `TurnReviewer` 挂在 Agent 里形成（M1 §2）。v0.3 起循环归包，上下文管理、重试、多采样都可能是包自己写的，宿主不再知道“学生当时看到了什么”——除了一处：包要模型作答，只能调 `ctx.model(messages, tools, role, effort)`，这一刻宿主看见的 `messages` 就是模型实际收到的全部输入。训练投影因此改为从这里读：

- **唯一来源是宿主写的网关日志。** M1 §1（v0.3）规定宿主在每次 `ctx.model` 调用落一条记录 `{call_id, role, effort, messages, tools, response, usage}`，每次 `ctx.tool` 调用落一条 broker 事件；两者都由宿主写，包没有句柄。训练投影、M8 的采集记录与 R 的轨迹读取都只读这两种记录，从不读包自己写的笔记、文件或“轨迹”。原因：包是被演化的对象，它写的记录既不可信（HSI 的 meta-evolver 就是因为 evolver “声称机制有效而轨迹显示从未调用”才存在，[SURVEY §5](../SURVEY_RSI_FRAMEWORKS.md) 第 9 条），也是 objective hacking 的入口（同 §5 第 3 条：gate 证据必须由固定代码写）。
- **记录的 `messages` 原样落盘，不重建。** v0.6 §2 第 4 步“重建每个 accepted turn 当时的学生输入”在 v0.3 变成读取：包控制上下文裁剪与压缩，宿主无法从外部复算它；逐字记录是唯一忠实的办法。（v2，2026-10-08，[V2_TRAINING_PIPELINE](../design/V2_TRAINING_PIPELINE.md) §0.2、§2.2、§3.2）不建原文对象库（原写“正文另存对象库、投影按 ref 取正文”）：日志行就是 M0 §2 的 `messages_prefix` + `messages_tail`，投影按前缀链还原（v4-finance 3,068 次调用 0 处不一致），前缀链被包的压缩打断时在断点处切段。网关日志改为只按宿主持有的密钥值精确脱敏，外加密钥形态的模式；已有记录里带 `[REDACTED]` 的调用标 `redacted_input`，SFT 默认排除，v2.1 用重放回填被脱敏的工具输出、以 `broker_event.output_digest` 核对，核对不上的轨迹排除并计数。TargetHarness 在 v0.6 “固定上下文裁剪方法”（M1 §3）的要求随之改为“裁剪由包做，但每次调用的实际输入由宿主记录”，训练投影复现当时实际 actor 输入这一条不变、只是来源换了。
- **`role` 是训练能否使用一条记录的第一判据。这是 v0.3 唯一实现的一项。** `act` 是产生对环境动作或最终答复的调用；`aux` 是包内部的辅助调用（证据表、发送前的二次核对、关键决策的多采样、路由）。manifest 未声明的 role 在草稿检查时是结构错误（M0 §2（v0.3）），所以日志里不会出现第三种值。
- **教师在网关介入，只对 `act`（第二版）。** 采集模式下，宿主把 `role="act"` 的 response 先交教师，PASS 原样返回、REPLACE 以替换响应返回，包收到的就是被接受的那一条，对包透明：包不知道有教师，也拿不到原 proposal。日志对这条调用多记一个可选字段 `review_ref?`，指向 ReviewRecord（verdict、replacement_ref、rationale_ref、usage_ref）；v0.3 的记录里它不出现，不内嵌 review 对象。proposal 作为审计流保留在 ReviewRecord 里，不在 `response` 里。原因：教师看到的 `messages` 与模型看到的完全相同，M8 §2 “教师只看学生可见历史”自动成立；包不能通过改循环读到教师理由或原草稿。`aux` 调用不经教师：它们的输出不作用于环境，让教师审阅等于让教师参与写包。（v2，2026-10-08，V2 §2.6 第 5 条、§3.7）教师在这份 `messages` 之外只多读 K：各域 W\* 的 memory 与 skills 原文，固定模板渲染（§3a 第 2–3 步）；断言、grading、重放结果永不进教师输入。教师是只属于宿主的 `[teacher]` provider，包可调的 role 仍只有 `act`/`aux`；审阅调用不计入学生的五项硬限额，计入采集预算与费用。
- **`aux` 计入采集限额，永不进训练数据。** aux 的调用次数、tokens 与费用计入每次尝试的硬限额（K：总模型调用数、tokens、秒）与 DistillationPlan 的 collection_limits；但训练投影按 role 过滤，aux 的 `messages/response` 不进入任何 SFT 行，也不作为 context 示例。原因：aux 输出的正确性没有环境真值（环境只验证动作），训练它等于训练教师未审、环境未验的中间判断；计费则必须计，否则包可以把工作搬进 aux 以绕开采集预算。
- **包拿不到评分器、provider、凭据、对象库与结果。** 这是 M0 §2（v0.3）对 ctx 的定义；M2 据此不需要在训练投影里再做“包是否偷读了 oracle”的检查，但投影仍按 §2 第 2 步校验血缘与 partition，并在 SFT 行里扫 train 字面量（M0 §3 的同一规则）。

## 2. actor 数据构建算法

拟建 `build_sft_dataset(plan_ref, trajectory_refs, selection_ref, objects) -> DatasetBuild`；M8 不自建第二个 dataset builder。v0.3 起 `trajectory_refs` 指向按 episode 聚合的网关日志记录（宿主为每个 episode 写 TrajectoryRecord，其 `step_refs` 指向该 episode 的模型调用与 broker 事件，`student_messages_ref` 不再单独存在：学生可见输入就是各 act 调用的 `messages`），投影身份 `projection_ref = gateway_projection.v1`（v0.3，2026-10-04）。（v2，2026-10-08，[V2_TRAINING_PIPELINE](../design/V2_TRAINING_PIPELINE.md) §2.3、§7“考虑过、不做”）不另建 TrajectoryRecord 存储：episode 是投影器按 `attempt_id` 关联网关日志得出的内存视图，宿主不额外写。

1. 验证 plan、学生版本、目标 harness、动作与消息 schema；沿血缘检查所有任务事实属于外层 train，任务家族与冻结映射一致。v0.3 起目标 harness 的身份包含 agent 组件代码（包身份覆盖 agent 代码，M0 §2（v0.3））：同一模型、不同 agent 程序采的轨迹不是同一 plan 的数据。
2. 只选官方成功条件满足、无环境/运行器故障、执行记录完整的轨迹。初版不混 partial-success/离线改写/仅教师答题样本；排除原因逐类计数。没有成功轨迹返回 insufficient_data，不伪造正样本。v0.3 起“执行记录完整”指该 episode 的每次 act 调用都有网关记录且（采集模式下）都有 review_ref，每次工具调用都有 broker 事件；缺任一条即排除并计数为 `incomplete_gateway_log`（v0.3，2026-10-04）。原因：缺记录的 episode 无法证明学生输入是什么。
3. 使用 plan 在采集前冻结的内部 fit/validation family 分配；教师参考的任务示例仅来自 fit，validation 内容不能参与编写参考。重复轨迹、同任务前缀、变体和增强样本全部同侧。以 selection 声明每任务上限或权重，不把长任务变成隐式多数。
4. 每个训练目标的输入就是该 act 调用记录的 `messages` 与 `tools`，逐字读取、不重建（§1a，v0.3，2026-10-04）。审计流中的原 proposal、review 理由、私有 TeachingReference、oracle、后续消息不进入输入或目标——它们本来就不在 `messages` 里，投影另做字面量扫描确认（DT6-02）。同一 episode 内 aux 调用的输入与输出一律跳过（v0.3）。
5. 训练目标默认 `accepted`：`role="act"` 且教师 verdict 为 PASS 的 response，加上 REPLACE 的替换响应的动作部分（文本、工具调用、完成请求），同 HZ；替换理由与原 proposal 永不进目标（v2，2026-10-08，用户决定，[V2_TRAINING_PIPELINE](../design/V2_TRAINING_PIPELINE.md) §7 第 3 项；配置 `[data] sft_targets`，§3b）。`pass_only`（只训 PASS）只作只看 dev 的消融，不作主张臂。这取代 2026-10-04 的“只取 PASS”，回到 v0.6 的“训练所有合格的 accepted assistant turns，包括 PASS 与 REPLACE”。2026-10-04 的理由是替换响应由教师写成、不是学生在该状态下的行为；现在教师读的是与学生相同的 `messages` 加 K（§1a），这一顾虑交给 `pass_only` 消融在 dev 上读，不预先排除；SFT 行的 `targets[].verdict` 与 `producer` 逐目标记明来源（V2 §2.3），可按它分开统计。REPLACE 的比例与分布照常记录并报告（M8 §4），高替换率说明任务难或学生弱，不说明数据多。无教师的 plain 采集（没有 review_ref）不进 reviewed 集（`reviewed_student`）；老师作 actor、在 W0 包下的现有成功轨迹可以单独建 `teacher_rollouts` 集（`verdict` 记 `none`，只取一个 `package_ref`，脱敏输出经重放回填），只供 X1 的 S_F 臂（V2 §2.1 S1′、§4.2），不与 reviewed 集混用。role mask：system/user/tool/参考示例的 assistant token 为 0；本次作为目标的 act response（PASS 原样、REPLACE 取替换响应）token 为 1。多模态或未支持的消息类型显式拒绝。
6. 模型 renderer 处理 tool-call 编码、边界与 EOS；固定 tokenizer/renderer/mask policy 的版本和摘要。允许全会话多目标或等价逐步样本，但不可让同一目标被重复加权；每次 act 调用的 `messages` 已是该步实际输入，逐步样本直接按记录成行，不再有“实际上下文裁剪后存在差异时必须逐步重建”的分支（v0.3）。
7. 超长样本首版排除并计数，不从任意 token 处截断 JSON/工具结果。记录原始/保留轨迹数、tokens、任务家族数、PASS/REPLACE 分布与 act/aux 调用数（v0.3）；不能把训练集近乎排空仍返回 ready。

没有 fit/validation 家族就 insufficient_data；最小 smoke 可明确使用非成绩用途的单任务数据检查管线，但不能据它选模型或报告泛化。存在固定公共示例时，采集与无教师评测都使用相同示例，且示例 assistant 不承受训练 loss。

## 3. 训练、checkpoint 与装载

在 `train_artifact` 既有纯构建边界增加 role/method 分派，保持旧调用签名与默认 decision 行为。目标接口：

```text
build_task_dataset(...) -> 既有决策 DatasetBuild          # ouroloop/evolve/build.py:164，现有
build_sft_dataset(...) -> DatasetBuild(status, dataset_ref, exclusions)
train_artifact(..., role=actor 或 decision) -> TrainingBuild
```

M0 的 GenerationArtifact 由 ready 训练结果引用；不修改 active/champion。actor 探针交 M1 provider：加载确切底座/adapter、生成合法 ChatTurn、工具消息往返、接受历史重放。controller 保留 `.decide()` 的输出类与 schema 探针，按需独立 calibration。

配方显式记录 `init_model_ref`，默认从 DistillationPlan.student_release_ref 的 actor 初始化；若策略选择从同底座重训而非延续 adapter，必须在冻结配方及比较中明确。后端不支持该 checkpoint 初始化时预检失败，不能偷偷退回 base。一次新实验允许按配方重置 optimizer，但必须记录；同一被中断作业的精确恢复仍须恢复 optimizer/游标，二者不能混称。

Tinker trainer 首版仅 LoRA SFT：validate 模型/renderer/预算 → 创建 client → 固定数据顺序与 batch 计划 → forward/backward → optim step → validation → checkpoint → sampling artifact → probe。记录 base、rank、学习率、epochs/steps、seed、batch/sequence 上限、SDK/renderer、数据摘要、实际 token 和费用；参数须有边界，研究员不能取消上限。checkpoint 选择只看内部 validation，dev 任务只由 M3 gate 使用。

Tinker 训练状态与 sampling 权重不是同一产物。恢复保存 training state、已确认 step、数据游标及 optimizer 状态；无法确认最近 optim step 是否生效时，不盲目重放该 step。由 M5 保持 unknown、核对或终止该候选，已花费用不抹去。不能只加载采样权重就宣称精确恢复优化器。

训练不满足服务访问、数据权限、模型能力或计量条件时，在提交前失败。外部作业失败后保留诊断和费用；没有 ready artifact 不创建可装配 actor。数据少或只学会格式时回到采集/工作流，不自动升级模型规模、启动 RL 或增加专家池。

### 3a. 演化、冻结、训练、换学生的顺序（第二版计划，v0.3，2026-10-04 写定）

第二版的一次蒸馏按下面的顺序进行，顺序写进 K 的 schedule（第二版 preset 字段 `distillation.stages`，固定为 `["evolve", "freeze_release", "collect_train", "switch_student", "re_evolve"]`，freeze 后 R 不能改序、不能跳步）（v2，2026-10-08：v2 的 stages 只有前四项，`re_evolve` 到 v2.5 才加入，见第 5 步）：

1. **evolve**：在 pin 的模型（首版为 Claude Opus 5.5）上演化 agent 程序，得到 W*；这是 v0.3 的活动本身。
2. **freeze_release**：（v2，2026-10-08，用户决定，[V2_TRAINING_PIPELINE](../design/V2_TRAINING_PIPELINE.md) §7 第 4 项，同 HZ）DistillationPlan 的 `student_release_ref` 与 `target_harness_ref` 是学生模型 + **W0** 包的 release，采集与无教师评测的 h 都是 W0；W\* 不装给学生，它的 memory 与 skills 原文按固定模板渲染成 K（各域取本域 W\*，没有 W\* 的域 K 为空），以 ref 冻结进 plan，只给教师读。同时冻结 FamilySplit、学生与教师身份、配方、采样温度、上限、预算根（V2 §5 轨道 B “冻结”一步）。此后采集与训练都在这一个 release 上，W\* 继续演化不影响已冻结的 plan。原因：采集一半换包会让同一数据集里的学生输入来自两个不同的循环。（原写“以 W\* 的 release 作为 `student_release_ref` 冻结”，2026-10-08 改。）
3. **collect_train**：学生在 W0 下行动（W0 的 agent 包为循环、学生模型在 `ctx.model` 背后），教师带 K 在网关对每个 act 调用给 PASS 或 REPLACE；每次尝试最多替换 5 次、每次替换可修复 1 次，仍失败记 ERROR、episode 记 incomplete；只保留成功的 episode（V2 §4.1“采集方式”）。在 train/fit 任务上采集；按 §2 建集、§3 训练、探针。
4. **switch_student**：训练产物经 M1 装载探针与 M3 无教师 dev gate（学生 release 对同 h 的基线 B，比较见 [DESIGN §8](../DESIGN.md) 表“有效 harness 与蒸馏 actor”）后才能形成新 release；gate 不过则保留原 release，费用照记。
5. **re_evolve**：（v2，2026-10-08：在 v2.5 实现，V2 §5“v2.5 以后”；轨道 B 的 C5、C5′ 不依赖它，报告“蒸馏 app”的成绩仍要它，见下文 (a)(b)）以学生为 pin 的模型重新开一个活动，在学生上演化 agent 程序。学生上的 **W0 是更简单的种子**：M0 §2（v0.3）的默认 agent（`agent/default_agent.py`，即今天的宿主循环移植成包）加上 Opus 活动中被接受的文本组件（memory/skills），不是 Opus 上演化出的整个 agent 程序。Opus 上的 W* 可以作为该活动第一轮的一个候选，与学生 W0 过同一个 gate；它不是种子。（2026-10-08：v2 里学生的 W0 就是默认 agent 包，不带 Opus 的文本组件；W\* 的 memory/skills 只作教师的 K（第 2 步）。re_evolve 的种子到 v2.5 再定。）

**harness–模型共适应是一项成本，不是假设。** 在 Opus 上演化出的循环（更高 effort、多采样、发送前核对）以 Opus 的判断力为前提，放到 9B 学生上可能费用更高、收益更低，甚至为负（Harness-Zero 的 h* 之所以在学生上验证，正是因为 base+h* 是学生带 h* 的成绩，[ROADMAP §3](../ROADMAP.md)）。所以：(a) re_evolve 是必经阶段，不能把 Opus 的 W* 直接装给学生就报告蒸馏完成；(b) 报告里“蒸馏 app”的成绩是学生 + 学生上演化出的包，与 Opus + Opus 上的包并列，再加学生 + Opus 的包作为共适应成本的直接读数（三者都是无教师、同 split）；(c) 每一步的费用计入同一预算根，re_evolve 的预算在 freeze 时预留，不能把钱全花在前四步。

### 3b. 配置形状与 actor 产物契约（v2，2026-10-08，[V2_TRAINING_PIPELINE](../design/V2_TRAINING_PIPELINE.md) §3.3、§3.7）

teacher、judge、actor（含学生）与 trainer 都是配置项，开发者换模型只改配置；完整示例见 V2 §3.7，这里只写定形状：

| 表 | 内容 |
|---|---|
| `[llm]` | 活动的 actor；学生臂就是把它换成学生（例如 `openai_compat`，`model` = 产物 ref）。provider 由 `config.build_provider` 构造：anthropic、openai_compat、claude_code 或 `module:Class` |
| `[teacher]`、`[judge]` | 只属于宿主（K）：teacher 在采集时审阅 act 调用（v2.2 起），judge 离线打标；包可调的 role 仍只有 `act`/`aux` |
| `[train.<role>]` | 按角色选 trainer：`[train.actor]` 的 `trainer` 取 `tinker_lora`、`mock_sft` 或插件模块路径（自托管 GPU 训练，由 `JobTrainer` 作为作业运行；`module:Class` 在控制面进程里直接构造，没有作业与价格上限，只用于测试与 demo；2026-10-08 按核价更正，原写 `module:Class`），`recipe` 记底座与超参；`[train.decision]` 的 `trainer` 取 `tde`、`signature`、`decider_lora` 或 `module:Class`。`[train.decision]` 取代旧的 `[trainer]`，旧表按 `[train.decision]` 读以保持兼容。不用 `[trainer.actor]`：`build_trainer` 直接从 `cfg["trainer"]` 取 `type`（config.py:124-138），嵌套表会 KeyError |
| `[trainers.<name>]` | 插件选项（怎么运行），沿用现有 plugin_options |
| `[data]` | `sources_fit`、`sources_threshold`（§1、M0 §4）、`sft_targets`（默认 `accepted`，§2 第 5 步）、`per_task_cap` |
| `[pricing.<provider>]` | 学生必须有价（按 token 或 GPU 小时），否则 gate 的成本项判 inconclusive |

参考实现（v2，2026-10-08，用户决定，V2 §7 第 6 项）：底座 Qwen3.5-9B，训练后端 Tinker（`tinker_lora`）。默认配方（2026-10-08 按 Tinker/RunPod 核价更正，V2 §3.7）：只做 LoRA，rank 32、所有层含 MLP；α 在 Tinker 上设不了，以导出的 `adapter_config.json` 为准；lr 扫 {2e-4, 5e-4, 1e-3}、按 val NLL 选；batch 8、2 epochs、5% warmup 后线性衰减、seed 42；`max_seq` 32768（排除 X1 训练集 S1′ 的 4.8%）。自托管训练写插件模块路径、由 `JobTrainer` 运行；评测时大批量的学生推理走 RunPod 上自托管的 vLLM（合并权重，经 `openai_compat`），Tinker 采样只作冒烟或兜底；v2 不维护第二套生成训练栈。原写“默认配方沿用 HZ：rank 32、α 32、2 epochs、lr 2e-4……；自托管走 `module:Class`”。学生训练与评测都关 thinking（是否改为 low/medium 是 V2 §7 第 9 项，待用户定）。

**actor 产物契约**（写进 trainers/__init__.py；现有插件契约的 serving 端只返回决策 Backend，这里为 actor 补上）：

| 端 | 契约 |
|---|---|
| 训练端 | `train()` 写出 adapter 和 `manifest.json`：`{base, tokenizer, renderer, sha256, max_seq}`；sha256 写进 `ModelIdentity.artifact_digest`，同时填 `adapter_ref`、`tokenizer_ref`、`renderer_ref`，release 只钉 digest（V2 §3.5） |
| serving 端 | `provider(run, ref, options) -> dict`，返回一张 `build_provider` 能构造的表：`openai_compat`（`base_url`，加上与 ref 同名的服务模型名；评测默认，指向 RunPod 上自托管的 vLLM），或 Tinker 采样（只作冒烟或兜底）（2026-10-08 按核价更正） |
| 自托管 | 唯一一步手工操作：在 RunPod 上起 vLLM，默认服务合并后的完整权重（`vllm serve <merged_dir> --served-model-name <ref> --max-model-len <≥ max_seq> …`）；以 LoRA 方式服务 adapter 是备选，要给 `--max-lora-rank 32`；完整参数见 V2 §3.3（2026-10-08 按核价更正，原写只有 `vllm serve <base> --enable-lora --lora-modules <ref>=<dir>`） |
| 生成探针 | 核对服务名等于 ref；做一次 tool-call 往返（含 `_unparsed`）；核对上下文长度 ≥ `max_seq`；（2026-10-08 按核价补）用 Tinker 训练时的 renderer 与 vLLM 的 HF chat template 各渲染同一条轨迹、逐 token 比对，不一致就不过（V2 §3.3）。actor 产物送进决策探针时拒绝（DT6-05） |

命名（v2，2026-10-08，用户决定，V2 §7 第 7 项）：子包 `ouroloop/train/`，extra `[train]` 与 `[train-jobs]`（`TrainJob`/`TrainResult`/`Trainer` 契约与 `train_artifact` 在 `[train]`，`JobTrainer` 与云、主机作业层在 `[train-jobs]`，SLIMMING §0.1 A、C），入口 `python -m ouroloop.train`；取代瘦身清单里的 `ouroloop[decision]`/`ouroloop[decision-jobs]`，代码随瘦身第一批只搬一次，等 support 两份 final 与 H1 完成后合入（2026-10-08：对照已停止，这个条件作废，按瘦身的批次顺序合入）。

## 4. 决策模型独立性与漂移

首个参考 controller 仍是 `worker.stop`，输入只含当时可见事实，标签回答“此刻是否满足完成条件”。它不天然回答“继续的期望收益是否大于费用”。后者需要分支实验和明确成本效用，暂不混用标签。（v2，2026-10-08，V2 §0.2、§3.6：第二版首个决策点改为 broker 边界的 `write_gate`，target 推广为 `worker.decide.<point>`，`worker.stop` 迁为 `worker.decide.stop`；运行时 stop 推到 v2.5，本段对 stop 的论述届时适用。）

actor 的版本是 controller 数据 applicability 的一部分。换 actor 后允许保留逻辑上兼容的历史完成标签，但必须重新检查输入分布、校准及完整任务行为；阈值不自动继承。actor 自己也能生成 finish 请求，controller 只是独立检查这一动作，不要求把所有停止意识从 actor 权重中删除。

v0.3 的宿主不问任何决策点：包返回即完成（termination `finished`），硬限额到即 `budget`；StopGuard 的回推是默认 agent 内部的事件，不是 termination（M1 §3（v0.3））。第二版：把 stop 决策点放到完成协议的边界，controller 的 observation 来自宿主在该边界看到的事实（最终消息、broker 事件、剩余限额），不来自包内部状态；届时 agent 包的身份也是 controller 数据 applicability 的一部分：换了循环，完成请求出现的时刻与前文分布都会变（v0.3，2026-10-04 写定，第二版实现）。

## 5. 用例

| ID | 条件 | 预期 |
|---|---|---|
| DT6-01 | PASS/REPLACE 混合成功轨迹，另含失败/中断轨迹 | v2 起（2026-10-08）：`sft_targets="accepted"` 时 PASS 的 act response 与 REPLACE 替换响应的动作部分都是目标，各自记 `verdict`，替换理由与原 proposal 不在任何 SFT 行；`pass_only` 时只有 PASS 进入，REPLACE 计入分布；失败/中断轨迹排除并列原因（v0.6 原预期为 PASS/REPLACE 都进入，2026-10-04 改为只 PASS，2026-10-08 改回 accepted） |
| DT6-02 | teacher 私有标记、原草稿和 oracle 有唯一字符串 | 所有训练输入和目标均无这些字符串 |
| DT6-03 | 工具 JSON、公共 assistant 示例、EOS 与超长样本 | mask 与边界正确；示例不计 loss；超长不破坏动作 |
| DT6-04 | 同任务多前缀、变体；dev 或内部 validation 答案进入教学参考 | family 不跨组；污染使构建失败且未发训练 |
| DT6-05 | actor artifact 被送入旧 decide probe | role 分派到生成探针；错误 schema 明确拒绝 |
| DT6-06 | optim step 后响应丢失，重启 | 不盲重训；保留收费与未确认状态 |
| DT6-07 | 仅有最终 reward、教师说 stop=yes | 最终 reward 不推导状态标签；教师标签记 `model:<teacher_ref>`，`sources_fit` 选中时可进拟合，但阈值集没有 env 标签，模型记 `uncalibrated`、只能 shadow 绑定，评测不收它（v2，2026-10-08 改；原预期为“不启动 controller 训练”） |
| DT6-08 | 训练成功但 gate 未运行；或数据为空 | 前者无晋升，后者 insufficient_data 且无 GPU 请求 |
| DT6-09（第二版） | 一个成功 episode 的网关日志含 act 与 aux 调用；包另在自己的工作目录写了一份“轨迹” | SFT 行只来自 act 记录的 `messages/response`；aux 的调用数、tokens、费用计入采集限额与报告，但不在任何 SFT 行；包写的文件从未被投影打开 |
| DT6-10（第二版） | 采集模式下教师对一次 act 调用 REPLACE | 包收到替换响应并据此继续，日志该条记 review_ref，proposal 只在 ReviewRecord；（v2，2026-10-08）该步以替换响应的动作部分作训练目标（`verdict=REPLACE`），替换理由不进目标；REPLACE 删掉或改动了一次写时另记一个 `write_gate` 标签（`teacher_review@1`，`model:<teacher_ref>`）；同一 episode 其余 PASS 步照常进入 |
| DT6-11（第二版） | 同一 episode 的 act 调用缺一条网关记录或缺 review_ref；另一 episode 的工具调用缺 broker 事件 | 两者都排除，计数为 incomplete_gateway_log；不以包的记录补齐 |
| DT6-12（第二版） | 无教师的 plain 采集成功轨迹被送入 build_sft_dataset | 建 reviewed 集时排除并计数（没有 review_ref）；可作 task_feedback。（v2，2026-10-08）只能以 `--dataset teacher_rollouts` 单独建集：`verdict` 记 `none`、`producer` 记实际作答模型、只取一个 `package_ref`、脱敏输出经重放回填，供 X1 的 S_F 臂，不与 reviewed 集混在一个数据集里 |
| DT6-13（第二版） | 同一学生模型、两个不同 agent 包采的轨迹送入一个 plan | plan 校验拒绝：目标 harness 身份含 agent 代码，不是同一 plan 的数据。（v2，2026-10-08）按包 digest（`package_ref`）判定，不按工作区：不同工作区里同一 digest 的轨迹由投影器合并计入；`teacher_rollouts` 同样只收一个包（V2 §2.1 S1′） |
| DT6-14（第二版） | preset 的 distillation.stages 被改序（v2.5 起：或跳过 re_evolve）；或把 Opus 的 W\*（或其 memory/skills）装给采集中的学生 | freeze 拒绝。（v2，2026-10-08，§3a 第 2–3 步）采集与训练的学生 h 是 W0；W\* 的 memory 与 skills 原文只以 K 交给教师，固定模板渲染、以 ref 冻结进 plan，学生的 `messages` 里没有 K。W\* 装给学生只出现在 C5′ 的 S+W\* 对照臂，或 re_evolve（v2.5）里作为候选进 gate |

DT6-09～14 于 2026-10-04 随 v0.3 写定，验收归第二版；v0.3 不为它们写测试。DT6-01/07/10/12/13/14 于 2026-10-08 按 [V2_TRAINING_PIPELINE](../design/V2_TRAINING_PIPELINE.md) §7 改写，测试落点按其 §5（v2.0 的离线套件；v2.2 在 fake_upstream 上用脚本化的学生和教师）。复用 `test_evidence_training.py`、`test_train_config.py`、`test_jobs.py`；第二版新增少量生成 renderer/mask 与 fake Tinker 测试。实际云 smoke 单列，不放默认离线测试。第二版的测试以 M1 §1（v0.3）落盘的网关日志格式为 fixture；格式一变，先改 M1 再改这里，不在 M2 另立一份 schema。
