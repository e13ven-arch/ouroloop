# M0：公共契约、身份与兼容

> 目标：以最小类型增量支持首版能力包演化与递归回用，保留现有 decision/task/research 行为。教学/训练契约保留为第二版规格；首版用到的新增字段/API 已实现（2026-09-30），第二版字段仍未实现。已有实现见 [历史快照](../history/V0_5_IMPLEMENTATION.md)。所有者：`evolve/contracts.py`。
>
> v0.3（2026-10-04，DESIGN v0.7）：首版交付物从“固定 agent 循环 + 能力包”改为“可演化的 agent 程序 + 最小运行壳 + 被 pin 的模型”（运行壳即 DESIGN §1 的 runner shell，本文与 M1 只用这一个词）。能力包就是 agent 程序：它可以改循环本身。本文新增包 API v2（`agent` 组件、ctx 表面、角色、单次尝试硬上限、宿主写入的轨迹记录、包永远拿不到的东西）与相应的 release/导出变化；v1 包仍合法，由 K 自带的默认 agent 运行。理由与业界对照见 [SURVEY_RSI_FRAMEWORKS.md](../SURVEY_RSI_FRAMEWORKS.md) §4、§6、§7：v0.1/v0.2 装进去的两个改动都是文字提醒，剩下的失败质量在判断与未读状态上，只有循环层的机制（核对回合、判断前的证据表、拿事实的工具、用算力换准确）够得着；报告更大留出收益的框架（StarHarness Finance +29、AutoSaddler +9～10）都改循环与工具。v0.1/v0.2 的记录（[RESULTS.md](../RESULTS.md)）与 2026-10-03/04 的记录不删。

## 1. 身份与版本

保留现有 `type:sha256:<64hex>` 内容寻址、规范 JSON、Objects、Events、最严格分区继承和 `copy_closure`。键排序、列表保序、拒绝 NaN/Infinity；哈希覆盖实际序列化字段。**不能从旧对象统一剔除时间、路径等字段再重算身份**。旧 RunManifest 16 位 hash 与旧模型 store ref 均保持原算法。

`copy_closure` 沿 `refs_in` 找到的引用复制，缺一个就报错。整条嵌入的记录（TaskEvalPlan.gate_plan 与 CampaignPlan.research_gate_plan 中的 `GatePlan.to_json()`）带着自己的 `ref`，它等于其余字段按 `payload()` 的规则取的摘要。这是嵌入记录自己的身份，不是引用：不跟随，也不要求入库。这只用于识别，不改写任何已存对象，也不改变其身份。名为 ref 而指向别的对象的字段（如 Authorization.ref）照常跟随。

v2 对象按原 schema 读写，不补默认键后重存冒充原 ref。v0.6 不引入 v3 记录类型与 reader 分派：涉及新 binding/package/训练语义的 PolicyDescriptor、PolicySet、SystemRelease、EvaluationPermit、ExperimentProposal、Experiment、RunManifest 只在既有记录上增加默认 None 的可选字段（RoundResult 另见下文），未涉及的对象不变。`Record.payload()` 跳过值为 None 的字段（`evolve/contracts.py:201`），新字段缺省时旧对象 ref 不变；`from_json` 拒绝未知键（`contracts.py:219-223`），旧 reader 遇到带新字段的对象明确拒收，不静默忽略 actor/package。新字段一律默认 None，以空串/空列表为默认值会改变旧 ref，禁止。手写 `payload()` 的类型只把新增字段纳入省略规则、值为 None 时不写出，不得改为全局跳过 None：`RunManifest.payload()`（`ouroloop/benchmark.py:302-307`）省略值为假的 V2_FIELDS 与值为 None 的 V06_FIELDS，`model_ref` 等既有字段为 None 时仍序列化，全局改动会改变旧的 16 位 hash；`RunManifest.from_json`（`benchmark.py:321-333`）已改为拒绝未知键；`b1cb7a7` 及更早的 RunManifest reader 仍会静默丢弃新字段，无法补救。RunManifest 没有 schema_version 字段，版本判定以未知键拒绝为准。新增记录类型（ModelIdentity、TargetHarness、HarnessPackage、CollectionRequest 等）直接以现行 `SCHEMA_VERSION=2` 加入，不属于版本分派。RoundResult（`evolve/contracts.py:816`）是普通 dataclass，不是 Record、不入对象库：新增 next_action/action_ref 默认 None 即可，跨重启需要的 next_action/action_ref 由 CollectionRequest 与 operation/事件日志持久化。全局 `SCHEMA_VERSION` 保持 2；本文及其他模块中的“v3”仅指带新字段的对象，不是另一套 schema 或 reader。未知 schema_version 拒绝（`KNOWN_SCHEMA_VERSIONS`，2026-09-30 起显式校验）；需要特定运行能力的 release 仍记录 required_capabilities，旧运行时不能加载后静默忽略 actor。

新字段与新记录分版启用。首版：worker.actor（`binding=generation` + `model_identity_ref`）与 ModelIdentity（未训练 actor 的 provider/底座身份）、TargetHarness（只描述当前宿主）、worker.harness/HarnessPackage、CollectionRequest（`purpose=task_feedback`）、RoundResult 的 `next_action/action_ref`、ExperimentProposal 的 `origin`、SystemRelease 的 `target_harness_ref/required_capabilities`（首版只 pin 当前宿主）。第二版：Experiment `purpose=source_validation`、`execution_base_ref`、EvaluationPermit 的 `target_harness_ref/allowed_modes/subject_release_ref`、训练联合类型与蒸馏相关记录。RunManifest 的新字段按 [M7](07_BENCH_REPORT.md) §2 分版。未启用的字段保持 None。

v2 release 仍由现有 reader 执行原模型配置。升级时冻结其实际 provider 配置为新的 actor policy，创建有 parent/provenance 的 v3 release；旧 ref、历史 run 和分数不变。新配置先 dry-run/preflight；迁移本身不能签发 improved 的结论。

v2 的完整目标集合按原注册清单校验，不因新增 worker.actor/worker.harness 要求旧 PolicySet 补项。v3 bootstrap/显式迁移创建合法空包与对应 policy，再 pin 实际 provider；包不启用组件时保持基础工具行为。旧 reader 遇到需要 package API 的 release 明确拒绝，不能忽略包并运行。

包 API 的版本与 release 的能力声明（v0.3，2026-10-04）：

- manifest 的 `api_version` 取 `harness_package.v1` 或 `harness_package.v2`；v2 比 v1 多一个可选的 `agent` 条目（§2“包 API v2”）。已存的 v1 包不改写、不重存：它的 ref 不变，读它的宿主把“没有 `agent` 条目”解释为“由默认 agent 运行”，所以**一个 v1 包就是一个带默认 agent 的 v2 包**。理由：v0.1/v0.2 的 release 与结果都引用 v1 包，改写会改身份；只在解释上统一，不在存储上统一。
- 包身份（`bundle_ref` 的内容摘要）覆盖 `agent/*.py` 的字节，与 tools/middlewares 相同：同名不同代码是不同的包，不同的 W。理由：候选签名若不含 agent 代码，两份只差循环逻辑的候选会被判成同一输入（§4）。
- 声明了 `agent` 条目的包所在的 release，其 `required_capabilities` 必含 `harness_package.v2`；只服务 v1 的运行时（`agent/harness_package.py:24` 的 `API_VERSION`）加载它时明确拒绝，不能跳过 agent 用旧循环跑完取分。没有 `agent` 条目的包不加这项能力，v0.1/v0.2 的 release 不受影响。
- 新 preset `automationbench_harness_v3`（`bench.PRESETS`，实现 2026-10-04）的 `harness.package_api` 是 `harness_package.v2`、`harness.components` 多出组件种类 `agent`、`max_effort` 为 high，并带五项单次尝试硬上限作 `[bench.budget]` 的缺省（`bench.preset_budget`：配置只能调低，调高拒绝）；`marketing_harness_v1` 原样不动，它的数值已冻进 v0.1/v0.2 的 K；K 冻结后 R 不能提高。`[harness_evolution] components` 不含 `agent` 时，草稿声明 `agent` 条目是结构错误（§3），该活动只演化 v1 种类——这是“只演化文本/工具/hook”的活动与“演化循环”的活动在 K 上的区别，同一活动内不切换。

## 2. 公共记录

下表列 v3（带新字段对象）的语义必填项；既有记录上的新增字段在类型上仍默认 None，必填由构造器/准入按启用用途校验；`*_ref` 均为不可变引用，文本/文件另存 artifact，避免嵌入巨量轨迹。模型 store 自有 ref 可保留，但必须附可验证的模型身份。

| 记录 | 字段与约束 |
|---|---|
| `PolicyDescriptor` | 继承 owner/target/binding/input_schema/output_schema/implementation/applicability/fallback/limits/evidence_refs；新增 `binding=generation`，仅注册 actor 目标可用 |
| `ModelIdentity` | `role(actor/controller), provider, base_model, revision?, adapter_ref?, tokenizer_ref, renderer_ref, inference_ref, artifact_digest?`；托管别名未冻结时明确 `mutable=true` 及观测时间，不伪造 revision。（v0.3）`inference_ref` 所指的推理配置含 pin 的默认 effort 与 K 的上限 `max_effort`：包在 ctx.model 里请求更高的 effort 时，网关按 `max_effort` 调用并记下请求值，不拒绝 |
| `TargetHarness` | `runtime_ref, tool_schema_ref, action_schema_ref, message_schema_ref, context_policy_ref, finish_protocol_ref, permission_ref, limits_ref, extension_api_ref?`；固定宿主/基础工具及可用 package API，不包含教师。（v0.3）`runtime_ref` 指运行壳（网关、broker、硬上限、完成协议）；`finish_protocol_ref` 只剩两条硬规则：agent 返回即完成，硬上限到即终止；`context_policy_ref` 从“宿主固定的上下文算法”改为“默认 agent 的上下文算法”，只作种子包的说明，不再约束带自己 agent 的包；`limits_ref` 列 §2“单次尝试硬上限”的五个维度 |
| `HarnessPackage` | `bundle_ref, manifest_ref, package_api_ref, dependency_set_ref, test_bundle_ref?, evidence_refs`；bundle 覆盖运行路径/字节，tests 独立保存；manifest 指工具/hook/技能/记忆及启用顺序，不嵌入引用自身的验证报告。（v0.3）manifest 可含 `agent` 条目；bundle 覆盖 `agent/*.py`；`package_api_ref` 记 v1 或 v2 |
| `SystemRelease`（v3） | 保留完整 W/R/K、授权及 parent/provenance refs；新增 `target_harness_ref, required_capabilities`，将实际执行契约与发布绑定；首版只 pin 当前宿主。（v0.3）包带 `agent` 时 `required_capabilities` 含 `harness_package.v2` |
| `EvaluationPermit`（v3） | 保留 subject/parent/plan/scope/core/expiry；新增 `target_harness_ref, allowed_modes, subject_release_ref?`，约束受限版本可用的实际契约与执行模式；不可安装；新增字段第二版启用 |
| `CollectionRequest` | `purpose(task_feedback/decision_feedback/actor_imitation), release_ref, target_harness_ref, task_selection_ref, mode(plain/reviewed), plan_ref?, limits_ref, requirement_refs`；task_feedback 只运行 plain train attempt，返回任务事实而非中间决策标签；reviewed 必须有匹配的 DistillationPlan |
| `SourceWorkflow` | `source_release_ref, source_harness_ref, guide_ref, validation_ref, partition`；绑定真正执行的方法与 train 验证事实，不能只有自然语言建议；validation 不授权安装 |
| `TeachingReference` | `source_workflow_ref, content_ref, evidence_refs, target_harness_ref, mapping_ref, adaptation_ref, partition`；privileged 构建材料，只能在许可的采集 scope 读 |
| `DistillationPlan` | `student_release_ref, target_harness_ref, teaching_ref, teacher_model_ref, teacher_prompt_ref, review_schema_ref, train_split_ref, sampling_ref, collection_limits_ref, success_rule_ref, projection_ref, budget_scope`；采集前冻结 |
| `ReviewRecord` | `episode_id, step_id, proposal_ref, observation_ref, teacher_ref, verdict(PASS/REPLACE), replacement_ref?, rationale_ref?, usage_ref`；PASS 禁止 replacement，REPLACE 必须完整替换响应。（第二版）`step_id` 即被审阅那次 `role=act` 网关调用的 `call_id`，见 §2 末“第二版”一段 |
| `TrajectoryRecord` | `episode_id, task_id, family_id, partition, student_release_ref, target_harness_ref, plan_ref, step_refs, student_messages_ref, result_ref, audit_ref, status`；status 为 completed/failed/incomplete，成功另读环境 result。（v0.3）`step_refs` 指网关日志条目（`gateway_call.v1`），不指包自己写的任何东西 |
| `TrajectoryStep` | `step_id, observation_ref, proposal_ref, review_ref, accepted_turn_ref, execution_ref`；执行事件含调用顺序与实际返回；未执行步骤不能伪装训练动作 |
| `SFTDataManifest` | `purpose=actor_imitation, split_ref, fit_groups, validation_groups, trajectory_refs, projection_ref, tokenizer_ref, renderer_ref, mask_policy_ref, row_refs, exclusions, source_counts`；全部样本和教师材料的任务事实来自 train。（第二版）只收 `role=act` 的网关条目，`aux` 一律在 `exclusions` |
| `GenerationArtifact` | `model_identity_ref, dataset_ref, recipe_ref, trainer_ref, checkpoint_ref, sampling_ref, recovery_ref?, export_ref?, probe_ref`；recovery/采样/便携权重引用不同，不能混用 |
| `DeploymentManifest` | `source_release_ref, worker_ref, target_harness_ref, runtime_ref, required_core_ref, model_refs, tool_bindings_schema_ref, validation_ref, external_dependencies`；白名单导出，不复制全量对象闭包。（v0.3）app = agent 包 + 运行壳 + 模型服务描述：`worker_ref` 所含的包（含 agent 代码）、`runtime_ref` 的运行壳、`external_dependencies.model_service` 的 provider/模型身份；不带 R、评分器、任务数据、网关日志 |

`HarnessPackage`、`CollectionRequest`（首版仅 task_feedback/plain）、`ModelIdentity`（未训练 actor 的 provider/底座身份）与 `TargetHarness`（只描述当前宿主）是首版新增公共记录。TeachingReference、DistillationPlan、ReviewRecord、reviewed Trajectory/SFTDataManifest 与 GenerationArtifact 属于第二版；首版 bootstrap actor 只需真实 provider 身份，不创建训练产物。DeploymentManifest 两版共用。

SourceWorkflow 指第二版教学来源，源 h* 的完整身份是 source release 中的 W/package 加宿主 TargetHarness；source_harness_ref 引同一宿主类型，不新增另一套 runtime schema。源和学生的宿主契约可相同，W/package 可不同。源包代码在 K 许可的扩展 API 内演化；目标宿主与学生非训练 W 在一次蒸馏内固定。source_validation 只用冻结 train/fit，输出 passed/failed/inconclusive/invalid artifact，不授予安装权；guide 与教学材料内容血缘独立校验。

TargetHarness 固定宿主执行协议、基础工具和上下文算法；`finish_protocol_ref` 是硬完成/否决协议，不是可调的 `worker.finish` 提示正文。（v0.3，2026-10-04）上下文算法不再由 TargetHarness 固定：它是 agent 程序的一部分，随包演化；TargetHarness 固定的是运行壳——网关、broker、权限、硬上限与两条完成硬规则。（第二版的训练投影因此不必“复现宿主当时的裁剪”，它读网关日志里实际传给模型的 messages；见 §2 末“第二版”一段。）`worker.harness` 是新增注册 W 目标，沿用 `kind=workflow, binding=config, builtin=harness_package.v1, config_ref`，config 仅含 `package_ref`；不增加通用 module-path implementation。（v0.3）`builtin` 取 `harness_package.v1` 或 `harness_package.v2`，由包 manifest 的 `api_version` 决定，config 仍只含 `package_ref`（`policies.py:156-159` 的校验扩到两个值）。manifest 的工具/hook 入口只能在 bundle 内由隔离 worker 解析。包内封装/中间件是扩展契约内的 W 变化，新增 W/package ref；有效工具 schema、hook 顺序、技能/记忆投影摘要由 M1 派生，作为冻结运行身份记录。修改基础工具语义、宿主 API、硬完成规则或权限需新的工程契约/K，R 不能自行扩权；同条件训练比较冻结非训练 W（含 package）。（v0.3）agent 程序改的是包内的循环，不是宿主 API：ctx 的五个名字、网关、broker、硬上限是 K，包只能用，不能加。

包结构只允许 manifest.json、tools/*.py、middlewares/*.py、skills/**.md、memory.md、tests/*.py 的相对路径；（v0.3，2026-10-04）另允许 `agent/*.py`，文件数与字节数计入既有 `max_files`/`max_package_bytes`，不另设上限（理由：循环代码与工具代码没有不同的风险，共用一个预算即可）。是否需要隔离 worker 分两种判定：**检查需要 worker**（“含代码的草稿”）指 manifest 有 tools/hooks 条目，或运行 bundle/test bundle 含 tools/*.py、middlewares/*.py、tests/*.py 任一文件，草稿检查与 package_test 据此判定；**运行需要 worker**（“运行含代码的包”）指 manifest 有 tools/hooks 条目或运行 bundle 含 .py 文件，任务执行、当前版本预检、安装与导出据此判定，test bundle 不挂载进这些环节，不据此要求运行时 worker；两者都不满足即“只含 skills/memory 的包”，由宿主装配。（v0.3）manifest 有 `agent` 条目，或 bundle 含 `agent/*.py`，两种判定都为真：带自己 agent 的包在任务执行时整段在 Docker worker 内运行（M1 §1“宿主作为运行壳”）。没有 `agent` 条目的包由 K 自带的默认 agent 运行，默认 agent 是 K 的代码，不计入包身份；（实现，2026-10-04）它在哪跑随包：包含代码（tools/hooks）则**整个默认 agent 在 worker 里运行**，宿主加载时把 `agent/default_agent.py` 加进 bundle（M1 §1“放置”）；只含文本的包在宿主进程内运行，不为它启动 worker——与今天一致，彩排的纯文本路径不变。tests/*.py 仅在 K 至少允许一种代码组件时允许；middlewares 按 manifest 的 site 归入 on_finish/before_tool/after_tool 判定是否在 K 内；拒绝绝对路径、..、symlink、未知类型和包外 entrypoint，路径/内容/数量上限由 K 固定。manifest 含 api_version、tools(name/description/input_schema/entrypoint)、hooks(name/site/entrypoint)、skills/memory 及 enabled/order；无效引用、名称冲突和非法输入 schema 拒绝，site 仅 before_tool/after_tool/on_finish。工具名须为各 provider 共同接受的 `[a-zA-Z0-9_-]{1,64}`，description 不超过 1024 字符，input_schema 须是 provider 接受的 JSON object schema（顶层 type=object、无 anyOf/oneOf/allOf，properties 为 schema 对象，type 取 JSON 类型，array 给出 items，嵌套不超过 16 层）；enabled 只取布尔，停用的工具不暴露给 actor。这些都在草稿检查时作为结构错误回传 R，不留到 provider 返回 400 时记作基础设施故障（2026-10-02）。依赖来自固定 dependency_set_ref/镜像，不接受候选 pip 安装。包存在不意味着验证通过，检查结果独立落 operation，gate 仍读环境事实。

规范化把 tests 分到 test_bundle_ref，其余运行文件形成 bundle_ref；固定代码完成两份内容摘要，之后不改写。包测试时可挂测试 bundle，正式评测与部署均只挂同一运行 bundle，避免导出时删除文件改变被评测行为。测试变化改变相应 proposal/check 输入身份，不把测试脚本带进业务执行。

两种判定都按应用 patch 后的完整草稿包进行，包含继承的 manifest 条目、运行 bundle 和 test bundle，不能只检查本次修改的文件。文本 patch 继承父包的代码时，草稿检查仍需隔离 worker；只继承父包 tests 时，检查需要 worker、运行不需要。（v0.3）只继承父包 `agent/*.py` 而本次只改文本的草稿，仍是带自己 agent 的包：检查与运行都要 worker。

### 包 API v2：agent 组件（v0.3，2026-10-04）

manifest 的 `agent` 条目：

```text
"agent": {"entrypoint": "agent/<file>.py:run", "roles": ["act"] | ["act", "aux"]}
run(ctx, task: str) -> str            # 返回最终消息正文；返回即完成
```

- `entrypoint` 只能指 `agent/` 下的文件与名为 `run` 的可调用对象；别的目录、别的名字、找不到的文件是结构错误。`roles` 列这个 agent 会以哪些角色调 `ctx.model`；没列的角色在 ctx.model 里被宿主拒绝（M1 §1），而草稿检查用隔离 smoke 先发现它（§3）。理由：角色写进每条网关记录（`role` 字段，第二版的训练投影按它挑数据），在 manifest 里声明则冻结时就能审计，不必跑完才知道。
- 允许一个 `agent` 条目；两个及以上是结构错误（理由：一次尝试只有一个入口，多入口的分派逻辑属于 agent 代码本身）。

ctx 向包暴露的**全部**是下面五个名字，加一个自述入口 `ctx.mark`（实现，2026-10-04：worker 里是 `agent/worker_runner.py` 的 `AgentContext`，公开名字只有这六个，其余是它与宿主通话的内部方法，经过的仍是宿主的处理器；进程内是 `agent/gateway.py` 的 `HostContext`，`__slots__` 只有这六个名字，宿主对象都在闭包里，`getattr` 其他任何名字是 AttributeError），多一个都不给：

| 名字 | 形状 | 由谁实现 |
|---|---|---|
| `ctx.model(messages, tools=(), role="act", effort=None)` | → `{text, tool_calls: [{id, name, args}], stop}`（实现另附 `limits` 快照）；`role` 取 manifest 声明的 `act`/`aux`，缺省 `act`；`effort` 取 `low/medium/high/xhigh/max` 或 None（None = W0 pin 的默认档），高于 K 的 `max_effort` 时**压到 `max_effort`**，不拒绝，请求值记进网关记录的 `requested_effort`。形状不对（`gateway.malformed`）、角色未声明、effort 名未知 → 答 `is_error` 的错误，包可捕获，宿主记一条 `protocol` 事件，不调模型（M1 §1） | 宿主网关（`agent/gateway.py` 的 `Gateway`）：调被 pin 的模型（worker.actor 的身份与推理配置），计量、记日志、扣硬上限 |
| `ctx.tool(name, args, read_only=False)` | → `{output, is_error}`；`name` 限 K 授权的基础工具（包工具是 agent 自己的代码，broker 只见它发出的基础调用）；hook 以 `read_only=True` 调，写操作被拒 | 宿主 broker（`agent/gateway.py` 的 `AttemptBroker`）：tool_gate、权限、预算、事件，与今天 `ctx.call_tool` 相同 |
| `ctx.skills` | 只读：`index`（名称与摘要）、`read(name) -> str | None`（受 `max_skill_chars` 截断）、`memory -> str`（memory.md 正文） | 宿主（`SkillReader`）：每次 `read` 记 `skill_read` 事件，另记一条名为 read_skill 的 `tool` 事件给诊断看 |
| `ctx.limits` | 只读快照：`act_calls, model_calls, tool_calls, seconds, tokens` 各自的剩余量（None = 冻结计划没有这项）；每次 ctx 调用返回时刷新 | 宿主 |
| `ctx.view` | 只读投影，与今天 hook/包工具的 view 同源（`agent/runner.py` 的 `attempt_view`）：`task`、`system`（宿主合成的系统提示，默认 agent 把它作第一条 `role=system` 消息）、`base_tools`（基础工具的 `{name, description, schema}`）、`package`（manifest 启用的 hooks/tools、技能索引、有没有 memory——默认 agent 据此运行 v1 包）、`policies`（W 的 `system_prompt`、`finish` 正文）、`events`（本次尝试最近的 broker 事件，broker 就地刷新）、`max_output_chars`（K 对任何工具输出的上限） | 宿主 |
| `ctx.mark(kind, **data)` | 包对自己循环的自述（M1 §1 的 marks）：宿主标 `source: "package"` 写入，不是证据，见下文“包的自述” | 宿主（`Marks`） |

ctx.model 的 `tools` 是 agent 给模型看的工具清单，由 agent 自定；模型回复里的工具调用是 agent 自己去执行的：要么经 `ctx.tool` 到 broker，要么是 agent 自己的代码。宿主不替 agent 执行模型回复里的任何调用，也不校验那份清单与 broker 清单一致——这是循环的事，演化的正是它。

包**永远**拿不到：评分器与任务断言、provider 对象与凭据、对象库（Objects）、结果存储（results.jsonl、grading.jsonl）、网关日志与 broker 事件的写入口、冻结划分、预算账本、宿主文件系统与网络。这些既不在 ctx 上，也不在 worker 的镜像或挂载里（M1 §1 的隔离条目）。理由：[SURVEY](../SURVEY_RSI_FRAMEWORKS.md) §5 第 3 条，评分器可达的系统都被候选钻过；可变面扩大到循环之后，这条边界是唯一没有扩大的东西。

**单次尝试硬上限**（K；`[bench.budget]`，冻结进 task plan 的 `limits_ref`，`bench.py:1170`）有五个维度：

| 维度 | 字段 | 含义 | preset |
|---|---|---|---|
| act 模型调用数 | `max_steps`（名字沿用） | `role=act` 的 ctx.model 调用数；今天一回合正好一次 act 调用，所以数值与语义都不变 | 50 |
| 全部模型调用数 | `max_model_calls`（新增） | act 与 aux 合计 | 200 |
| 工具调用数 | `max_tool_calls` | 经 broker 的调用数（与今天同） | 200 |
| 秒数 | `max_seconds` | 从 `run` 开始到返回（与今天同） | 900 |
| 输出 token 数 | `max_tokens`（新增） | 本次尝试全部模型调用的输出 token 合计（含计费的 thinking） | 400000 |

这里的 `max_tokens` 是**每次尝试**的合计上限，不是 `[llm] max_tokens`：后者是一次模型请求的输出上限（M1 §1 `claude_code` 的配置项），两者名字相同、归属不同，preset 与收据里各写各的。

旧的冻结 task plan 没有后两个字段：它的 `limits_ref` 不变，读到 None 的宿主按“无此上限”执行，与冻结时一致；新冻结的计划五项齐全，缺一项拒绝冻结（`bench.py:2824` 的检查扩到五项）。理由：多采样与长核对把调用数和 token 从“步数”里解耦了，只限步数等于不限；而把上限加进旧计划会改它的身份。到任一上限的尝试以 termination `budget` 结束，`limit` 字段写明是哪一项（M1 §3）。

宿主写入的轨迹记录都是 `transcripts/<session>.jsonl` 里的事件条目（与今天 `_emit` 同一文件，`agent/runner.py` 的 `PackageAgent._emit`），不是入库对象；TaskResult 的可选字段 `trajectory_ref`（默认 None，旧 ref 不变）指向该文件的内容摘要——尚未实现，留第 2 步。实现的记录形状（2026-10-04，`agent/gateway.py`）：

```text
assistant:       今天的事件，加 {role, effort, requested_effort, call_id, seq}
                 # attempt runner 只从它读 usage 入账（task_eval.run_attempt.on_event），不从 gateway_call 再读一次
gateway_call.v1: {call_id, attempt_id, seq, role, effort, requested_effort, model_identity_ref,
                  messages_prefix, messages_tail, tools: [{name, schema_digest}],
                  response: {text, tool_calls, stop}, usage, elapsed, review_ref?}
tool_specs.v1:   {attempt_id, digest, specs: [{name, description, schema}]}   # 工具清单首次出现或变化时各一条
broker_event.v1: {event_id, attempt_id, seq, after_call_id?, tool, args, output_chars, output_digest,
                  is_error, gate: {decision: run|veto|refused, veto?, reason?, limit?}, elapsed}
agent:           {entry, placement: worker|host, roles}       # 每次尝试一条，task 之后：哪个程序在哪跑
end:             {stop, turns, limit?}                        # 硬上限结束时 limit 写维度
protocol:        {error, role, requested_effort}              # 网关拒绝的一次 ctx.model 调用（形状、角色、effort 名）
```

`messages` 的还原规则：第 i 条 `gateway_call` 的 messages = 第 i−1 条还原出的 messages 的前 `messages_prefix` 项 + 本条的 `messages_tail`（第一条的 prefix 为 0）。历史只追加时每条只多出尾段；agent 改写了历史（压缩、重排）时 prefix 变短、尾段变长，什么都不丢，也不重建（`research/diagnosis.py` 的 `gateway_results` 照此读）。理由：每条都存全量会让一次 50 步的尝试写出平方级的字节，而摘要又丢了训练投影要逐字读的东西（M2 §1a）。`tools` 每条只记名字与 schema 摘要，全部 spec 在同一尝试里第一次见到这份清单、以及清单变化时写成一条 `tool_specs`。`cost_usd` 不在行上：费用由计量层按 `assistant` 事件的 usage 逐次入账（M5 §2）。`effort` 是实际调用的档（包没请求时是 provider pin 的档），`requested_effort` 是包请求的（None = 没请求）；`review_ref` 第二版。

**包的自述**（`ctx.mark(kind, **data)`，实现 2026-10-04，`gateway.Marks`）：kind 为 `pushback`/`hook`/`package_tool`/`tool_blocked`/`tool_skipped` 之一时按该 kind 写一行、加 `source: "package"`；其他 kind 写成 `package_mark`、原 kind 放 `mark` 字段；data 里与记录自身字段同名的键（`kind`、`ts`、`source`、`mark`）先剥掉，所以包伪造不了宿主的时间与标签，也占不了宿主记录种类的名字。默认 agent 用它记回推、hook 的答复、包工具的运行、被 before_tool 拦下的调用与同回合跳过的调用——这几种 broker 看不到，没有 mark 记录里就不会有“模型被告知 Stopped before execution”这一笔。它**只是自述**：gate 收据、训练投影、R 的轨迹工具都不把它当事实；失败摘要把它列为包的自述，不当作结果。

两类轨迹记录（网关调用与 broker 事件）都由宿主在网关与 broker 的调用点写，包写不了也读不到；包自己的笔记、日志、“我做了什么”的自述，不是记录。训练投影（M2）、R 的原始轨迹工具（M4 §1（v0.3）：R 经只读、按字节上限的 `read_trajectory` 读 train 轨迹）、Inspection 与 gate 收据只读这两类。理由：[SURVEY](../SURVEY_RSI_FRAMEWORKS.md) §5 第 9 条，自述的教训不可信；证据必须由固定代码写。

第二版（不在 v0.3 范围，一段说完）：教师在网关对 `role=act` 的调用介入，响应交给包之前 PASS/REPLACE，结果以 `review_ref` 记在同一条网关记录里，包看不出有没有教师；训练投影只读网关日志的 act 条目，`messages` 就是模型实际读到的；时间表是演化 → 冻结 release → 在该 release 上采集/训练 → 换学生 → 在学生上重新演化，在 Opus 上演化出的循环不保证迁移到更小的学生，学生的 W0 是更简单的种子，这是已命名的成本。v0.3 实现的只有网关记录里的 `role` 字段；其余见 M2。

`teacher_model_ref` 指冻结的 provider 配置/身份 artifact，不使用只有 actor/controller 两种 role 的 ModelIdentity。`train_split_ref` 包含外层 train 清单及采集前冻结的内部 fit/validation 家族分配；教学参考中的任务示例来自 fit，validation 的答案和轨迹不参与参考编写。

辅助内部类型不全部提升为公共记录。`TrajectoryStep` 的 accepted turn 使用 M1 公共 ChatTurn 可序列化表示；provider 私有状态另存受限引用，不把签名 thinking 任意改写。

保留 `PolicySet` 的完整目标集合、SystemRelease 的既有字段、Observation、DecisionTrace、FeedbackRecord、DataManifest、EvaluationReceipt 和 InstallationReceipt。v3 release 的 target_harness_ref 是宿主契约权威，W 的 package_ref 是可变组件权威；DistillationPlan/SourceWorkflow 与相应 release 一致。首版包内代码候选使用原 parent 及其获准扩展 API，不要求先执行 prepare_harness。真正切换宿主才由 M3 准备新起点和基线；不能在 bench 参数覆盖。actor 是 W 的运行策略，第二版教师参考属于构建 provenance；R 配置不导出给业务应用。

worker 实验默认继承 parent 的运行契约；显式指定已验证的 `execution_base_ref` 时，以该起点的 harness 构建候选。它仍继承主线 R/K，生产安装必须对主线 parent 过 gate/CAS。research 实验只变 R、继承主线 W/K/target_harness_ref。bootstrap 使用默认 R0 和获准 provider，不要求用户写研究提示或训练新研究模型。M6 接受后安装 R1，下一改进调用按真实绑定记录 loaded R、目标配置摘要及 proposal/experiment refs；可扩展既有事件 payload，不新增第二套 lineage。

v3 模型 implementation 仍是 `kind=model`，由 `binding` 分派。generation 必须包含 `model_identity_ref`；未训练的 bootstrap actor 直接使用该 provider/底座身份，不伪造 dataset/recipe/checkpoint。经训练的 actor 另外要求 `artifact_ref` 指向 GenerationArtifact，且两者模型身份一致。decision 继续校验旧 spec/inference 的有限动作。actor 不能调用 `.decide()`，controller 不能被当作 ChatProvider。（v0.3）比较的口径从“同 actor”改为“同模型”（业界的说法）：同一 ModelIdentity、同一推理配置上限之下，循环、工具、提示、材料都可以不同；C1～C4 的含义不变。切换 ModelIdentity 或 `max_effort` 创建新比较身份（§4）。

## 3. 提案与训练操作

首版 `harness_edit` 复用 worker proposal 的 changes，目标为 worker.harness；M4 把 LLM 返回的包内 patch 经固定路径/大小校验后应用到当前 bundle 的新副本，存不可变 source refs，形成草稿包（新 package ref）。规范化只写对象，不 import 代码。冻结前由 M5 在同一 operation 日志里对草稿做草稿检查（package_check → isolated_probe → package_test）；只含 skills/memory 的草稿只做结构/路径/manifest 检查，不需要 isolated_probe。（v0.3，2026-10-04）运行含代码的草稿——带 `agent` 条目的经自己的入口，v1 含代码的经默认 agent——isolated_probe 除 import 外还做一次**接口 smoke**（实现：`agent/worker.py` 的 `check_package`、`agent/worker_runner.py` 的 `SmokeContext`）：在 worker 里对草稿检查既有的 fixture 世界（合成或仅 train 构造的，不是 dev/test）以罐装模型跑一遍 `run`——第一次带工具的调用答一回合 tool_use、每个所给工具各调一次最小参数，之后每次答结束——核对它返回字符串、只用声明的角色、hook 与包工具按协议作答、不在 smoke 的小上限（`SMOKE_LIMITS`）内崩溃或打转；smoke 失败是草稿失败（检查输出回传 R，占一次修订），不是基础设施故障；smoke 不计任务成绩，不进任何测量。理由：M1 §1 把 agent 运行时的异常记作基础设施故障（Leon 的决定），若没有 smoke，一个必崩的 agent 会在 gate 里花钱、被补跑、再把活动暂停；smoke 让确定性的崩溃在付费前就以草稿失败回到 R。允许的组件种类由 K 在 freeze 时固定并写入 R 上下文；freeze/smoke 预检须确认当前已采用包的组件种类都在 K 内，K 允许代码组件或当前包运行含代码时还须确认合格隔离 worker，否则不能以该配置启动；“改用只含文本组件的 preset”只适用于当前包不含代码的情形。草稿（按应用 patch 后的完整包判断）使用 K 未允许的组件种类属于结构错误：检查输出回传 R，占用一次修订。完整草稿含代码、K 允许代码而运行中隔离 worker 不可用，属于基础设施故障，不是候选缺陷：按唯一重试策略重试，仍不可用则本轮记 `outcome=infrastructure_failed`（无 experiment_id，保留草稿 ref、修订序号与故障记录；不消耗修订次数、不回传 R，计入 schedule 的 max_infrastructure_failures，不计失败上限与耐心，已发生费用照计），并以 `next_action=repair_runtime` 暂停活动：不再调度后续 worker 段与 research slot；smoke 复核 worker 恢复后，以保留的草稿 ref 与原修订序号重跑草稿检查、继续本轮，不重新调用 R。不 in-process 执行，也不在活动中途收窄组件种类。package_check 还做字面量检查：包文本出现 train 断言 params 或任务初始状态中的特定标识（邮箱、ID、记录名等，不含工具 schema 中的名称）属于结构错误，回传 R 并占用一次修订，防止把单题答案写进共享组件。（v0.3）字面量检查覆盖 `agent/*.py`，与其他包文本相同。W 的 prompt 改动（worker.system_prompt 的文本，worker.finish 的文本或 finish_check 指令）在准入前做同一字面量检查；prompt 改动没有草稿与修订，命中即本轮 `outcome=invalid`，不回传 R 修订、不冻结、不评测（2026-10-02）。检查失败时只把检查输出（不含任何任务结果或评分）回传 R，R 可修订；修订次数上限 `max_draft_repairs` 由 preset/K 固定（默认 3，指初稿之后最多修订 3 次、最多 4 份草稿），R 不能提高；每次草稿都有新 package ref，逐次记录并计费。只有检查通过的草稿才写入完整 proposal 并冻结进 Experiment；用尽次数则本轮以无效提案结束：RoundResult `outcome=invalid`（无 experiment_id），保留各草稿 ref 与检查 operation，计入候选轮次与失败上限。冻结后源码不变；building 按相同输入摘要复用已确认的草稿检查，不重复运行。proposal 引确切源包和新文件内容，不把“未来会生成的包”占位 ref 写入 Experiment。首版默认菜单不含 distill_actor/decision_train，training=[]；旧 trainer 和第二版联合类型保留兼容。

（v0.3，2026-10-04）R 以有界编码会话产出草稿时（M4 §1（v0.3）：**每轮一个会话**，R 在 worker 里对候选目录用文件工具编辑，`research_session.max_turns` 40、`research_session.max_edits` 24，后者跨本轮全部候选目录与修订合计），进入草稿检查的仍是“应用全部修改后的完整包”：会话的每次文件写入不是草稿，R `finish` 时固定代码从 worker 取回每个候选目录、按同样的路径/大小校验形成一份草稿、一个 package ref；会话中途的中间状态不入库、不计费为草稿。修订不开新会话：检查输出回传后 R 在**同一会话**里继续改，再次 `finish` 产出一个新草稿、一个新 package ref，轮数与编辑数仍从本轮的上限里扣。一轮至多 `max_candidates_per_round`（M4 §1（v0.3）：preset 3；`bench.py:1083-1086` 今天拒绝 ≠1，这是要改的那处）个候选目录时，每个候选各有自己的草稿链与修订计数，`max_draft_repairs` 按候选计，会话上限按整轮计。

组件种类属于活动的冻结条件：中途收窄会让同一 worker 段前后、以及 M6 两支之间的方法集合不同，比较失去同条件，因此不做。能力故障同理只暂停、不结束：结束 worker 段会让 M7 继续进入 research slot，两支在故障下以不同轮数完成比较。每轮开始前按运行判定预检当前已采用版本：其所需隔离 worker 不可用时，任务运行前记 repair_runtime 并暂停活动（同上），不自动删除代码、换回旧包或伪造文本起点，也不把设施缺口送入草稿修订。worker 恢复后以同一冻结条件续跑，未完成操作按 M5 恢复。

v3 `ExperimentProposal` 增加 `origin=manual/scripted/llm`，由实际入口固定写入，不接受模型自报；增加可选 `execution_base_ref`（第二版启用），缺省为冻结时的 parent release。显式值只能来自固定代码已完成兼容检查和基线的 harness preparation，不能任意指定一份历史 W。owner 继续区分 worker/research；source 方法仍是 worker 候选，不能为蒸馏另建 owner。v2 不补字段、不重算旧 hash。M4 的材料准备先产生合法 ref，再 normalize 成完整 proposal，不能提交带未来占位 ref 的实验。（v0.3，2026-10-04）增加可选 `declaration_ref`（默认 None，旧 ref 不变）：指向 R 对候选的声明 artifact `{mechanism, site, carrier, predicted_affected: [train task ids], trigger_condition, differs_from?: [rejected declaration refs]}`；由 M4 的入口在准入前写入，`predicted_affected` 只允许 train 任务 id（含 dev/test id 是准入拒绝），`differs_from` 是 M4 §2（v0.3）“(mechanism, site) 与已拒绝候选相同的提案须说明不同之处，否则准入以 `repeated_mechanism` 拒绝”所要求的引用。实验台账（M4 §5（v0.3））按 proposal ref 读它；它不进入候选签名——同一改动配不同声明仍是同一候选。

v3 `Experiment` 增加 `purpose=deployment/source_validation`（缺省 None 即 deployment，覆盖既有 task/research 正式晋升实验，不改变 profile；首版 deployment 实验不写该字段，显式传入 deployment 的入口规范化为 None；source_validation 第二版启用），并冻结解析后的 `execution_base_ref`（第二版启用；首版保持 None，即 parent）。`parent_release` 始终表示本实验的主线起点及 CAS 预期值，不因换 harness 改成未安装的基线版本；execution base 是实际构建/采集起点。前者负责生产版本关系，后者负责执行与比较条件，二者不能混用。

- harness preparation 引用由 M3 生成：`parent_release_ref, target_harness_ref, baseline_release_ref, compatibility_ref, baseline_plan_ref, baseline_result_ref`。使用已有 plan/operation 对象存储，不增服务。baseline release 仅改变 harness 和由能力检查计算的 required_capabilities，W/R/K 保持 parent 内容；无权改权限或偷偷换 checkpoint。
- `purpose=source_validation` 只允许 worker 的非训练源方法候选，训练条目必须为空；冻结 train/fit 验证规则。通过后输出 SourceWorkflow，不能签发安装许可。需要发布同一方法时另建 deployment 实验，可复用候选 artifact，但必须做生产 gate。
- v3 `RoundResult` 在原 outcome 外保留结构化 `next_action=none/collect/repair_runtime/stop` 与可选 `action_ref`。collect 对应 `outcome=needs_collection`，必须指向经固定代码校验的 CollectionRequest；不得将 next_action 塞进 reason 文本后丢弃。旧 v2 round 的无提案行为保持兼容。

v3 的重复提案检查与恢复身份包含 purpose（None 与 deployment 等价，不形成两个身份）、实际 execution base/harness、相关数据输入及评测模板；不能只按 changes/training 判重，否则已做 source_validation 的同一改动将无法再申请 deployment。h-only 的 execution base 变化属于真实候选变化，不能因 changes 为空而判 no-op。v2 原签名不改。

既有 v2 `training` 条目 `{point, feedback_ref, data_selection_ref, recipe_ref}` 按原意义处理。v3 训练条目使用明确联合，不允许未知 method：

```text
DecisionTraining = {method: decision_train, target: worker.stop,
                    feedback_ref, data_selection_ref, recipe_ref}
ActorTraining    = {method: distill_actor, target: worker.actor,
                    distillation_plan_ref, data_selection_ref, recipe_ref}
```

以下训练操作用于第二版：data_selection_ref 是冻结选择规则，实际 dataset_ref 由 build 追加产物事件，不在采集前假造。仅复用 plan/学生版本/划分/投影完全匹配的采集；新增 v3 路径每实验最多一个训练条目。旧 API 多条训练兼容保留。首版包变化和 prompt/workflow 变化可组合但须先冻结，同模型比较保持 actor 身份和推理参数不变。（v0.3）“推理参数不变”指 pin 的默认档与 K 的 `max_effort` 不变；包在上限内按调用选择 effort 属于 W 的变化，不是模型身份的变化。第二版的演化/训练时间表见 §2 末“第二版”一段。

`Experiment` 继续冻结 proposal、parent release、experience、K、eval template 和 budget；增加的 purpose/execution base 不允许运行中改写。续跑也只按这些冻结值完成，评测模板或预算 scope 不一致则不续跑并暂停（M5 §3）。source_validation 使用 train 验证模板，deployment 使用生产 gate 模板；不把 collect/train/evaluate 分成三个可以各自安装的候选。`build_candidate` 输出 ready/invalid/infrastructure_failed；内部 `TrainingBuild` 保持 ready/failed/cancelled，外部未知请求保存在操作记录，由 M5 映射为待核对或 inconclusive，不能伪造 ready。

## 4. 数据、权限与授权

沿用 partition：train/dev/research_dev/final。用户语境的 test 对应冻结 test 任务及 `final` 证据权限，不新增第二套同义 enum。（v0.3，2026-10-04）不新设分区：研究位的内层 gate 用 dev 的前一半、外层比较用后一半（M6 §2（v0.3）），都是 dev 分区的任务；`research_dev` 仍只是代码里既有的枚举值，v0.3 不给它新的任务池。派生证据继承最严格来源。配置超参数本身不是训练样本，但若文件包含 dev 任务事实，其教学材料权限仍是 dev，不能用于 train 采集。

沿用 EvidenceRef.parents 的**内容派生**血缘；用于运行身份的 release/receipt 引用不自动变成模型可读内容。例如经 dev 验证发布的 W 在 train 生成新轨迹，不能因版本 ref 可追溯到 dev receipt 而把所有新轨迹判为 dev；但读取了 dev 内容写出的教学摘要必须继承 dev。访问检查根据类型白名单解析引用，不能对对象库无限展开后交给教师。v3 新内容构造器计算并校验实际输入 parents；调用方不能自行改 partition 或把内容来源伪装为身份引用。R/teacher 的实际提示正文也是材料生成的输入：含 dev 任务事实时必须继承限制，不能仅以 research_ref 为身份而跳过。只有固定代码投影出的通用枚举、参数选择和不含任务事实的统计可用于方法选择；参考正文仍用独立 train 输入生成。

首版 train/fit 任务的断言级失败明细（每条断言 type/params/passed；评分器排除的断言不给，即作者声明不计分、或初始即成立且结束时仍成立的断言；初始成立但被破坏的断言按失败计分，照常作为失败明细给 R）对 R 可见；dev 只给汇总，final 不进入 R 上下文，dev/final 都不给断言明细和轨迹。这是本版变化：v0.5 研究上下文的 task_results_summary 只给逐题 success/partial/终止/失败类，不含断言明细（docstring `research/context.py:120`）也不含轨迹（`:116-146`）。放开 train 明细定位“漏了哪一步”最直接，不涉及 dev/test；泛化由 dev gate 把关，包内材料仍只写通用规则。判分明细与任务结果分开存：每次尝试的逐条断言结果（type/params/passed/excluded）写入 `bench/<domain>/grading.jsonl`，不进入 results.jsonl；写入点在 M3 的 run_tasks（`evolve/task_eval.py`）追加结果之后，读取接口由 M7 的结果存储（`benchmark.py`）提供，按冻结 split 过滤，默认只返回 train，非 train 返回空；dev/final 明细即使存在于 grading.jsonl 也不进入 R 上下文；投影只影响 R 的输入，原始判分记录（含 excluded 条目）完整保留，供审计与复现。（v0.3，2026-10-04）网关日志与 broker 事件（§2）按同一规则读：R 的原始轨迹工具（M4 §1（v0.3））经结果存储的 split 过滤只返回 train 尝试的条目，dev/final 的条目即使在同一 transcripts 目录里也不返回；dev 给 R 的只有固定代码投影的失败**类型**直方图（枚举类型与计数，M4 §1（v0.3）：无 params、无任务 id），test 什么都不给。网关日志里的 `messages` 含任务正文与工具结果，它的分区就是该尝试的 split，不因 release ref 可追溯到 dev 收据而改变。

反馈与模仿数据是两种类型：`FeedbackRecord.source=env/human` 不变；教师输出只进入 ReviewRecord 与 `purpose=actor_imitation` 的轨迹，不能通过改 source 绕过既有 EVAL_ONLY 限制。环境最终成功是轨迹筛选条件，不证明各步最优或教师理由真实。

隔离执行复用现有 EvaluationPermit，以显式 scope 限定允许的 release、任务及 mode；v3 permit 字段和外部许可校验规则须一起接入，v2 不自动获得额外权限；本版扩展其受限 baseline/source_validation/reviewed_collection 用途，不让准备版本冒充 installed。候选生产评测仍走正式许可链；accepted deployment/research receipt 才能授权安装，source 验证或 baseline 完成不授予安装权。初次创建受限 release 时保持 plan → permit → evaluation release 的单向引用，避免哈希循环。已存在的准备版本 B 用于后续采集时，先冻结以 B 为 student 的 DistillationPlan，再签发含 subject_release_ref=B 的补充 permit；M1 验证该外部许可的确切 release/plan/scope/mode，不修改 B 的内容或冒充 installed。补充 permit 仅为已存在 release 授予本次执行权，不授予安装权；安装 release 引 accepted receipt，W/R 内容与被评测候选一致。正式 verdict 仅 accepted/rejected/inconclusive/invalid；采集 PASS 不是 release accepted。

v3 评测计划显式记录本地各项主张的 control/candidate refs；不再从 evolved 名称推断 controller 增益。C2 默认 published_reference，外部引用材料不成为本地 TaskResult；需要 paired_run 时单独冻结，详见 M7。

K 固定评分器、任务划分、获准基础工具/权限、可修改目标、package API/目录/依赖/隔离镜像/资源限额、允许的组件种类、草稿修订上限（max_draft_repairs）、每轮失败笔记上限（max_failure_notes）与失败摘要条目长度上限、模型与宿主选择集、gate 与预算。（v0.3，2026-10-04）K 另固定：模型身份与推理配置上限（`max_effort`，preset `high`；W0 pin 的默认档仍是 `medium`，包不指定 effort 时用默认档）、模型网关（计量、pin）、工具 broker（每一次真实工具调用都经它）、单次尝试的五个硬上限（§2）、宿主写入的事件/轨迹记录、R 的代码与研究位、`max_candidates_per_round`、ctx 的五个名字。包提出的复合工具不能创造新的基础工具权限。首版任务结果记录 package、有效工具 schema 与实际 hook 摘要，候选签名包含包文件字节/启用顺序，不能将同名不同代码视为同输入。（v0.3）候选签名含 `agent/*.py` 字节；任务结果另记 `trajectory_ref`、按角色的模型调用数与 token 数（M1 §4）。切换底座或宿主创建新比较身份，不能复用旧 control 成绩。（v0.3）切换 `max_effort` 或 package API 版本同样创建新比较身份：W0 的 v1 包在 v2 宿主下由默认 agent 运行，行为与 v1 宿主相同（M1 §5 PR7-07），所以 **v0.1/v0.2 的 W0 读数在 `max_effort` 不变时可以进入 v0.3 的 pooled control**（M3 §2.1(a)（v0.3）：对照臂是同一冻结计划与模型身份下已装 W 的全部有效读数的合并，`control_pooling="pooled"`）；`max_effort` 从 medium 提到 high 则不能，因为比较身份变了——这是开新活动时要先决定的事。

什么没有变（v0.3 明确重申）：train/dev/test 的信息分离由固定代码执行；final 先封存、提交再运行；预算账本与预留；R0 对 R1 以下游 W* 比较；Docker 隔离与 broker；`claude_code` provider（M1 §1）。

## 5. 用例

| ID | 条件 | 预期 |
|---|---|---|
| CT6-01 | 同时读取 v2 对象、带新字段的对象和未知 schema_version（Record 类型与 RunManifest） | v2 ref 与字段行为不变；带新字段的对象被不认识该字段的 reader 拒绝（Record 类型经 from_json 未知键；RunManifest 仅对未知键改动落地后的 reader 成立）；Record 类型未知 schema_version 拒绝，RunManifest 以未知键判定 |
| CT6-02 | generation 装入 worker.stop，或 controller 装入 worker.actor | 冻结/构建前拒绝，不发模型调用 |
| CT6-03 | dev 答案经摘要变成 TeachingReference | 血缘仍为 dev，train 采集拒绝 |
| CT6-04 | 教师 REPLACE 转为 env FeedbackRecord | 类型/来源校验拒绝；合格轨迹可作为模仿数据 |
| CT6-05 | teacher/model/harness 任一身份变化 | 新 plan/ref；不能复用不匹配采集或成绩 |
| CT6-06 | permit release 尝试安装、部署导出 | 拒绝；已接受但未安装与正式部署资格按 M3 校验 |
| CT6-07 | source 演化或下一实验更换 target h | 创建相应新 ref；当前蒸馏不变；不复用旧采集/评测身份 |
| CT6-08 | source 只有说明/伪造 validation，或模型自报 origin | 准入拒绝未验证默认蒸馏；来源由入口决定 |
| CT6-09 | 未训练 bootstrap actor 与已训练 actor | 前者无需伪造训练 artifact，后者身份/artifact 一致且实际加载；不允许 ref 空缺回退 |
| CT6-10 | 换 h 的 execution base 与 parent 不同 | base 仅改获准 h；plan 与 base 一致，CAS 仍针对 parent；任意旧 W/未完成基线拒绝 |
| CT6-11 | 无 proposal 但 next_action=collect | RoundResult 保留结构化 action_ref，不降为终止型 no_proposal；不完整请求拒绝 |
| CT6-12 | source_validation 结果被用于安装 | purpose 与授权检查拒绝，SourceWorkflow 仍可用于合规教学适配 |
| CT6-13 | 包文件/manifest 有越界路径、包外入口、名称冲突或内容变化 | 结构非法在执行前拒绝；有效内容/顺序变化生成新 package/W 身份，不重算 v2 ref |
| CT6-14 | 新字段以空串/空列表为默认值，手写 `payload()` 未跳过新增的 None 字段，或改为全局跳过 None 使既有字段序列化变化 | 测试必须失败：旧对象 ref 必须不变 |
| CT7-01（v0.3） | 读取 v0.1/v0.2 工作区里的 v1 包与其 release | 包 ref 不变，不重存；解释为“默认 agent 运行”；release 的 required_capabilities 不含 `harness_package.v2` |
| CT7-02（v0.3） | manifest 的 `agent` 入口在 `agent/` 之外、名字不是 `run`、文件不存在、或有两个 `agent` 条目 | 结构错误，草稿检查回传 R；不进 isolated_probe |
| CT7-03（v0.3） | `agent/*.py` 只差一字节的两个包；只继承父包 agent 代码而本次只改 memory 的草稿 | 两个 package ref、两个 W；后者按带 agent 的包判定，检查与运行都要 worker |
| CT7-04（v0.3） | 带 `agent` 条目的包所在 release 被只服务 v1 的运行时加载 | 明确拒绝，不以默认 agent 代跑；同一 release 在 v2 宿主下正常装配 |
| CT7-05（v0.3） | `[harness_evolution] components` 不含 `agent`，草稿声明 `agent` | 结构错误、占一次修订；冻结后的 K 不能在活动中加回 `agent` |
| CT7-06（v0.3） | 旧的冻结 task plan（三项上限）与新冻结的计划（五项） | 旧 plan 的 `limits_ref` 不变，缺的两项按“无上限”执行；新计划缺任一项拒绝冻结 |
| CT7-07（v0.3） | TaskResult 的 `trajectory_ref`、ExperimentProposal 的 `declaration_ref`、ModelIdentity 推理配置里的 `max_effort` 为 None | 旧对象 ref 不变（CT6-14 的规则覆盖新字段） |
| CT7-08（v0.3） | `declaration_ref` 的 `predicted_affected` 含 dev 或 test 任务 id | 准入拒绝；只含 train id 通过；声明不同、改动相同的两份提案是同一候选 |
| CT7-09（v0.3） | 导出带 agent 的 release 为 app | 导出物只有包（含 agent 代码）、运行壳、模型服务描述；没有 R、评分器、任务数据、网关日志 |
| CT7-10（v0.3） | 同一 W0 的 v1 包，分别在 `max_effort` 不变与提高的两份 K 下 | 前者读数可并入 pooled control，后者是新比较身份 |

验证落点：扩展 `tests/test_contracts.py`、`test_policies.py`；保留旧 CT 用例，不为每个字段机械写测试。（v0.3，测试名以实现为准，2026-10-04）CT7 用例的测试：CT7-01 `test_harness_package.py::test_a_v1_package_parses_unchanged_and_its_api_ref_differs_from_a_v2_one`；CT7-02 `test_harness_package.py::test_a_malformed_agent_entry_is_a_structure_error`；CT7-03 `test_harness_package.py::test_a_v2_manifest_with_an_agent_entry_parses_and_the_package_runs_in_the_worker`（两种判定）与上述 CT7-01 的测试（两个 ref）；CT7-04 `test_package_worker.py::test_a_v2_package_binds_under_the_v2_builtin_and_its_release_names_the_capability`；CT7-05 `test_harness_package.py::test_an_agent_under_a_core_that_does_not_allow_it_is_refused_by_the_rule_it_breaks`；CT7-06 `test_cycle.py::test_a_new_freeze_needs_all_five_limits_and_an_old_plan_is_read_with_its_three` 与 `test_the_frozen_plan_and_the_host_contract_carry_the_five_hard_limits`；preset 与 `max_effort` `test_cycle.py::test_the_v3_preset_freezes_a_k_with_the_agent_kind_the_effort_ceiling_and_the_five_limits`、`test_max_effort_is_frozen_into_k_with_a_default_of_high_and_narrows_only`；CT7-09 `test_export.py::test_er6_14_a_code_package_ships_its_run_bundle_and_not_its_tests` 与 `test_it6_11_the_export_runs_a_task_from_a_clean_directory`（导出带 `max_effort` 时在运行壳下跑）。尚未实现、因此没有测试的：CT7-07（`trajectory_ref`、`declaration_ref` 尚未加字段，加时 CT6-14 的规则覆盖）、CT7-08（声明，第 3 步）、CT7-10（合并对照，第 2 步）。
