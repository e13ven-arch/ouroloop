# M8：工作流蒸馏与学生在环采集

> 第二版新增模块，首版 harness 演化不依赖 M8。完整保留“有效方法 → 目标动作 → 真实执行 → 成功轨迹 → SFT → 无教师运行”规格；文中“首版”指本模块首次接入版本。拟增 distill/teacher.py 与 distill/collect.py，调用 M1/M2/M5，不建立独立平台。（2026-10-08，V2 §3.8、§5 v2.2、§7 第 7 项：不另建 `distill/`；v2 的代码进可选子包 `ouroloop/train/`（入口 `python -m ouroloop.train`），审阅钩子是网关的 role → provider 映射加只作用于 act 调用的审阅钩子与 ReviewRecord，在 v2.2 做。）

## 1. 从可执行源方法到教师参考

（v2，2026-10-08，[V2_TRAINING_PIPELINE](../design/V2_TRAINING_PIPELINE.md) §7 第 4 项与“考虑过、不做”）v2 不做 `adapt_workflow`：教师的私有参考就是 K，即各域 W\* 的 memory 与 skills 原文，用固定模板渲染（没有 W\* 的域为空），以 ref 冻结进 DistillationPlan；学生在 W0 下行动。本节适配规格保留作记录，不在 v2 实现；DS6-01、DS6-09～11 随之不适用。

M4 负责提出和执行验证源工作流；本模块负责把已验证方法适配到学生的目标动作空间，然后组织学生在环采集。SourceWorkflow 引用确切源 release/harness、train 方法说明及实际验证结果；不要求人写优化工作流或教学提示。手工导入只作为可选入口，仍受同一验证约束。`purpose=source_validation` 的 passed 结果即可作为输入，不要求 source 已做生产 dev gate 或已安装；无发布资格不会阻断合规教学适配。第二版的源 h* 在学生 actor（Qwen3.5-9B）上演化并验证（[DESIGN](../DESIGN.md) §2、ROADMAP（tag archive-2026-10-09） §3）；首版在 GLM-5.3 上演化的包不直接作为学生的 source。

拟新增窄函数 `adapt_workflow(source_ref, target_harness_ref, adaptation_spec, services, budget, operation) -> reference_ref 或 unsupported/invalid`，放 `distill/teacher.py`。它不是通用代码编译器。适配前固定来源、目标、模板、provider、允许输入和修复次数，逐次生成调用纳入 M5：

1. 验证源运行证据存在、符合预先冻结的 source 成功/回归规则，且材料任务事实来自 train/fit；无证据返回待验证。
2. 加载目标工具、动作、消息、上下文和完成协议，只读取许可的源 guide/训练执行片段，不读取 dev/final、oracle 或任意源码目录。
3. 用框架提供的适配提示自动转换：源工具/技能变为目标原生动作配方；源 middleware/检查习惯变为教师审阅要点；源记忆中的可用方法变为受限诊断指导。工具签名/权限须可核对，不能只有一段笼统总结。
4. 产出 `mapping_ref`：每项源能力的目标动作/输入前提/验证方式，或 unsupported 原因；`content_ref` 为教师指导，`adaptation_ref` 保留真实输入和生成调用证据。
5. 固定校验结构、血缘、工具可用性和完成协议。必要的语义有效性由后续真实 reviewed 执行验证，不把“格式合法”解释为方法保证正确。超过有界修复仍失败则不创建可训练 plan。

例如“更新并确认通知”可映射为查询、更新、检查发送状态、必要时通知；依赖不存在的服务则 unsupported。无法映射时返回 M4 选择已有可行方法或报告能力缺口，不偷偷给学生增加教师工具，也不要求用户亲自重写下一套工作流。

内部 fit/validation 家族在源示例和参考编写前冻结，参考的任务事实只来自 fit；validation 可做 reviewed 采集及训练内验证，但不得反向进入参考生成。目标 h 在本次采集/SFT/评测中固定，源 h* 和下一次实验可继续演化。

目标 h 更换时，先完成 M3 harness preparation，以新基线 release 作为学生；M8 不自行改 release 或跳过新基线。适配通过后由 M4/M7 固定代码创建 DistillationPlan。`collect_reviewed` 输入该 plan、合规 TeachingReference、train 任务和 M1 装配的学生；M4 的 actor 补采调用同一个 collector，并要求 CollectionRequest 与 plan 的 release/harness/任务范围一致，不能后台切 checkpoint；输出 M0 ReviewRecord、TrajectoryStep、TrajectoryRecord 及受限审计流，不直接输出已晋升模型。数据训练归 M2，发布归 M3。

## 2. 教师协议

教师每次仅看到：当前任务、学生可见的已接受历史/工具返回、待执行完整 proposal、目标动作 schema、剩余合法预算，以及许可的私有参考。不能看到环境 rubric/答案、未来状态、最终评分、dev/test 内容或隐藏模型思维。首个实现只需 provider 和参考投影，不依赖 DeepAgents。

结构化输出：

```json
{"proposal_ref":"...","verdict":"PASS","replacement":null,"rationale":"..."}
```

或 `verdict=REPLACE` 且 replacement 是**完整**目标 ChatTurn（文本/工具调用/完成请求）。PASS 表示接受当前 proposal；不表示任务已成功。rationale 仅审计保存，不追加到学生上下文，不作为 SFT 目标。

教师不执行业务工具；必要的参考读取是只读白名单，不允许 shell 或任意目录。教师不能让学生调用其私有读取工具。可对已知格式错误进行冻结次数的修复请求，仍逐次计费；超限或超时使 episode incomplete/failed，**不默认 PASS**，不执行未经接受的动作。首版不实现多教师投票或自动选择任意替代模型。

## 3. 采集算法与双流记录

目标函数 `collect_reviewed(plan_ref, task_ids, services, budget, operation) -> collection artifact ref`，只接受冻结 train 列表和 M5 operation。内部不拥有外层循环或新预算根。

```text
for each frozen task attempt:
    create/reset a permitted task world
    pin student release and target harness
    while limits permit:
        proposal = student.chat(accepted_history)
        review = teacher.review(visible_state, proposal, private_reference)
        accepted = validate_review_and_turn(review, proposal)
        execute accepted through the original runtime
        persist audit refs and student-visible accepted turn/results
        if runtime ends: break
    obtain final environment result outside student/teacher context
    persist trajectory with result and termination facts
```

上段是逻辑规格，不另写一份 Agent while loop。实际循环仍由 M1 Agent 持有，M8 通过 TurnReviewer 和轨迹 recorder 协作；collect 只迭代任务并管理输入/产物。

双流是访问投影，不强求两份完整消息文件：审计流保存 observation/proposal/review/accepted/executed 引用；学生流只投影 accepted turn 和真实 tool results。每个 tool result 对应唯一合法调用 ID；工具失败如实返回。工具产生的新状态会影响后续 actor 输入，不能在离线原轨迹上修改一句答案就冒充闭环干预。

环境验收在 episode 结束后独立执行。成功筛选规则引用环境官方 success predicate，并在 plan 冻结；不要硬编码所有 benchmark 都以 reward >= 1 为成功。最终成功也不证明轨迹每一步最优，保留多余动作/成本作为诊断。

## 4. 控制、兼容与边界

teacher 可替换 finish 请求或工具动作，但 controller、硬预算、权限与确定性 verifier 仍按固定 TargetHarness 生效。用于识别 actor 蒸馏收益的基线固定 stop 策略；分离模型配置必须独立记录，不能将 teacher 与 controller 合并记账。

源工作流效果先由 train 上的真实运行支持。无充分结果可作为待检验 hypothesis 保留，但默认自动蒸馏必须先补源验证；不能声称已验证。教师能否在学生所在状态提出合法有效动作，由 reviewed collection 实测，而非以源 h* 的验证成绩保证。

采集只用同一个冻结学生版本；新 actor 安装后的下一批采集生成新 plan。避免同时在线训练、采集一半切 checkpoint、无限主动学习等复杂性。重启、未知副作用、重试和费用由 M5 统一处理。

M8 提供 PASS/REPLACE 比例、格式失败、替换动作实际执行率、轨迹成功率和排除统计给报告。高替换率可能说明任务难、学生弱或教师过度干预，不能直接解释为学习收益。

## 5. 用例

| ID | 条件 | 预期 |
|---|---|---|
| DS6-01 | train 工作流能映射到目标工具；另有缺失工具 | 前者生成有效参考；后者 unsupported，不偷偷扩工具 |
| DS6-02 | 原 proposal 错误，教师替换后真实环境成功 | 审计保留原错误；训练候选仅是实际接受与执行轨迹 |
| DS6-03 | 教师 PASS 但任务失败；教师理由说成功 | 环境判失败，轨迹不进首版 actor SFT |
| DS6-04 | 教师返回旧 proposal_ref、缺参数或非法 action | 修复在冻结次数内；未修复则终止，未执行草稿 |
| DS6-05 | 教师私有标记、学生被拒草稿、oracle 含标记 | 学生历史/SFT/部署都无泄漏，私有审计仍可追踪 |
| DS6-06 | 全 PASS 与多次 REPLACE 的成功 episode | 两者合格 assistant 动作均可训练，来源可统计（v0.3，2026-10-04 曾按 M2 §2 第 5 步改为只训 PASS；v2，2026-10-08 用户决定改回：训练目标为 `accepted`，即 PASS 的 act 响应加 REPLACE 替换响应的动作部分，同 HZ，rationale 不进目标，每个目标记 verdict 以便分开统计；`pass_only` 只作只看 dev 的消融，见 M2 §2 第 5 步、[V2_TRAINING_PIPELINE](../design/V2_TRAINING_PIPELINE.md) §7 第 3 项） |
| DS6-07 | 到预算上限或教师故障 | 明确 incomplete/failed，支出保留，无假成功 |
| DS6-08 | 模型安装后下一次 reviewed collect | plan pin 新 W，原轨迹不被改写为新模型产出 |
| DS6-09 | 已验证 source 自动适配到不同目标工具面 | 生成可核对的动作映射；仅目标动作执行，缺失能力 unsupported |
| DS6-10 | 适配器输入含 dev 轨迹，或 source 无执行证据 | 准入拒绝，未创建可训练 plan；不靠摘要洗白 |
| DS6-11 | 同一 source 演化到新版本后再适配 | 新参考和 plan，原 reference 不被覆盖，历史可重放 |

默认验收用脚本 actor、teacher 和可变业务世界；真实 provider smoke 单列。必须观察工具真实副作用和下一轮输入，不能只比较输出文本。
