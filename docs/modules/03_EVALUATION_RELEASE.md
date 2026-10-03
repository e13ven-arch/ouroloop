# M3：无教师评测、完整发布与部署投影

> 目标：验证部署时真正保留的能力，并让评测通过的完整版本实际生效。复用 `evolve/task_eval.py`、`evolve/task_gate.py`、`release.py`；不新建模型晋升平台。

首版发布对象是现成 actor 加演化能力包；正式 gate/安装/导出不依赖教学来源验证或训练。source_validation 与蒸馏相关流程保留为第二版用途。

## 1. 冻结比较

M0 的 eval template 先固定任务家族、control、工具/harness、重复次数、执行顺序、预算、容忍回归、质量/成本门槛及不确定性方法。构建后只填实际 candidate ref；不因成绩不好临时增重复、换任务或改成功定义。

源方法有两种用途，在 freeze 时区分：

| 用途 | 验证和完成条件 | 后续能力 |
|---|---|---|
| `purpose=source_validation`：只作为教学来源 | 在冻结 train/fit 任务上验证成功、覆盖和回归规则；输出验证 artifact，状态 passed 才生成可用 SourceWorkflow | 立即可进入教学适配；不跑生产 dev gate，不安装，不宣称泛化 |
| `purpose=deployment`：直接发布工作流或模型 | 必须按正式任务计划完成无教师 dev gate，取得 accepted receipt | preflight/CAS 后安装；可导出 app |

两种用途复用相同 TaskBackend、任务运行、评分和 M5 executor；只分派评测模板与终止位置。source validation artifact 至少保存 `plan_ref, source_release_ref, task_result_refs, status, reasons, costs_ref`；status 为 passed/failed/inconclusive/invalid，不能伪造 accepted receipt。预算/配对不足返回 inconclusive，不把少量缺失结果当通过；通过也仅证明冻结 train 任务上的方法有效。

源验证通过后不等待额外生产评测。若随后决定直接发布同一源方法，另建 deployment 实验，复用身份匹配的候选 artifact，执行生产 gate；不能把原 source 实验原地改 purpose，不能用 train 结果抵扣必须的 dev 验证。

actor 蒸馏的主要对照是同底座未训练 actor 和已选 prompt/workflow；controller 的主要对照是同一个蒸馏 actor 的原 stop 策略。教师辅助采集分数单列，不进入候选部署 gate。日后若启用领先模型同题复测，在相同目标 harness 上另立 control；默认只引用公布数字作背景，不能把不同 benchmark 的外部榜单分数输入本地 gate。

正式 task evaluation 启动时必须证实 reviewer 为空、教学材料不可访问、actor/provider 身份匹配 W、controller 开关与 plan 一致。收集 observed inspection，检查实际调用，不能仅比 manifest。TargetHarness 不同的配置可以做系统级比较，但不能作“仅训练带来的收益”的同条件证据。

首版包候选沿用 parent 的 actor 与宿主扩展契约，control/candidate 分别 pin 确切 package ref/有效工具 schema/hook 顺序；二者均在相同运行配置中执行：运行含代码（M0 §2 运行判定）时为同一固定隔离配置，只含 skills/memory 的包由宿主装配。接口与候选自写测试通过只允许开始真实评测，不签发 accepted。dev gate 始终比较完整的冻结 dev 清单，control 是当前已采用版本，candidate 是本轮候选；其中对照版本本次成功的任务也参与比较，不因 train 诊断工作集缩小而移除。首版回归范围就是该 dev 清单，不自动收集历轮 train 成功任务，也不新增回归任务库或另一轮评测；评分器/条件不由包代码定义。只增大预算、换模型或偷偷卸载故障组件的运行，不能归为同模型 harness 收益。

### 更换目标 harness 的准备与基线

本节只处理宿主执行契约变化；首版包内工具/hook 演化使用原契约的扩展 API，直接进入 worker 候选，不为每个包版本创建 B 或重复新基线。第二版若需不同目标宿主，沿用以下准备流程。

`prepare_harness(parent_release_ref, target_harness_ref, baseline_plan_ref, scope, budget) -> preparation_ref 或明确失败` 由 M3 固定代码提供，使用 M1 兼容检查与 M5 operation，不新增安装入口。R 只从 K 预授权的已注册目标中选择；不提交任意 runtime 代码。

1. 校验 target 在 K 允许集合内，工具权限/任务评分语义不越界，actor renderer、消息/动作 schema、controller observation、上下文/完成协议能装配；失败不改 active、不训练。
2. baseline_plan_ref 先冻结 parent 的 W/目标 h、任务、条件和预算，不引用尚未创建的 B，保持 plan → permit → release 的单向关系。从主线 parent 创建基线 release B：保留 W/R/K，只改变 target_harness_ref 及计算得到的 required_capabilities。它有 parent/provenance 和限定任务/mode 的执行许可，不伪装已安装版本。
3. 按已冻结计划运行 B 的无教师基线；记录完整 release/harness/model/任务/预算/比较身份。所需基线未完成则 preparation 非 ready，不生成依赖它的训练实验；重启只补缺项。
4. M4 用 B 构造 DistillationPlan.student_release_ref 及 proposal.execution_base_ref。计划冻结后，M3 针对确切 B/plan 签发 reviewed_collection 的补充 v3 permit；它从既有 B 派生而不改写 B，避免“plan 引 release、release 又引该 plan 的 permit”的哈希环。基线许可本身不能拿来执行任意采集任务。冻结后的 Experiment.parent_release 仍是原主线 parent；candidate 在 B 的目标 h 下构建、采集和验证，运行中不换 h。
5. 要安装最终 candidate，按冻结协议与原主线 parent 比较完整系统效果，并对原 parent 做 CAS。不能因 candidate 比 B 好，就忽略它比当前生产版本更差；训练本身的贡献另外比较 B 与 candidate，二者同 h。旧 h 的成绩不能充当新 h 的同条件基线。

换 h 的实验可只改 harness，也可后续蒸馏；h-only 候选以明确 execution_base_ref 表示真实变化，不要求伪造 prompt 改动。若原 parent 已被另一安装替换，本实验保持旧输入，安装返回 stale_parent，不能偷偷继承新 W/R。若新 h 超出 K，需要另行授权活动，不让 R 修改 K。没有选新 h 时 execution base 就是 parent，不增加准备或额外基线开销。

## 2. 评测流程与结果

deployment 沿用 permit → 候选隔离运行 → 配对 TaskResult → EvaluationReceipt；source_validation 在同一运行/评分层输出源验证 artifact，不能进入安装分支。配对键仍为 `(plan_ref, task_id, repeat_index)`；attempt retry 不增加独立样本，补跑保存 supersedes。任务世界每次按同一输入复建；容器或服务故障没有有效得分时不要强行记成任务失败。

首版判分明细分存：`run_tasks`（`evolve/task_eval.py`）在追加 TaskResult 之后，把每次尝试的逐条断言结果（type/params/passed/excluded）另存 `bench/<domain>/grading.jsonl`，不进入 results.jsonl，TaskResult 与既有结果读取不变；明细只经 M7 结果存储按冻结 split 过滤的接口读取（M0 §4、M4 §1）。

主要报告完整任务成功、partial credit、任务/家族数、配对增减与不确定性、回归、延迟、实际推理费用、工具调用。训练/教师/搜索费用归总项目成本，推理节省单列。质量优先还是同预算质量比较由 K 的 gate 提前选择；没测费用就 unknown，不当 0。

M4 的失败 train 子集重跑是诊断采集，不替代正式 dev gate。未重跑的 train 成绩保留原版本/attempt 身份，仅作为历史记录；不能与新包的子集结果拼成当前版本的完整 train 成绩，或宣称这些任务已通过当前版本回归。测量复用仅限 M4 §4 的实际任务执行身份与冻结计划条件匹配，保留原 TaskResult refs；新候选仍须完成同一冻结 dev 清单的配对评测，缺项沿用 inconclusive，不因诊断子集涨分跳过 gate。

采用现有 GatePlan 和家族聚合方法。首版 quality 模式按完整配对结果判断：整体任务成功率增益达到 min_gain，且 regression_rate 不超过 max_regression_rate，再检查计划中已有的其他条件；regression_rate 沿用现有定义，即各任务有效重复的平均 success 比对照下降的任务数 / 全部配对任务数。它是本次观察到的退化比例，不是所有历史成功任务的失败比例。收据的 `metrics.regressions` 逐题列出比例计入的任务（任务、家族、两臂有效重复的平均 success）；其中含 dev 任务 id，经验日志不保存该键，只保留比例和收据 ref，所以不会进入 R（M0 §4，2026-10-02）。GatePlan 现有默认 max_regression_rate=0.2 只是工程默认，preset 明示并冻结实际值，不把它当成已经验证的最优值；后续根据实跑调整只作用于新活动。未超过冻结上限的退化如实报告，不一律拒绝。以下情形必须明确：候选协议错误为 invalid；样本/完整配对/预算不足为 inconclusive；达不到既定门槛为 rejected；符合冻结门槛才 accepted。deployment 的可选 screen 沿用现有规则：仅在显式冻结的 screen_ids 上发现对照成功、候选失败时提前筛掉候选；首版不为回归新增 screen，未被筛掉仍需完整 dev gate；source_validation 的独立终止路径不产生发布资格。

首个小规模实验可只报告方向和案例，不以训练 loss 或单任务成功降低正式发布门槛。演示用脚本 fixture 的 accepted 只证明控制路径，不能合并到真实成绩。

## 3. 发布、恢复与回退

保持完整 `SystemRelease`，安装同时 pin W/R/K。训练器不触碰 active；M3 根据 accepted receipt 做 artifact/服务装载、schema、权限和依赖 preflight，再 CAS 更新 expected_parent。模型、prompt、controller 校准与 runtime config 不可分别移动冠军指针。

首版 preflight 还校验包字节/manifest/API（运行含代码时另校验 image）与被评测内容相同，测试与正式运行均使用规定的运行方式；包文件损坏、运行含代码的包 worker 不可用、schema 不兼容不能回退旧包后安装。停用/删除组件同样是新的评测候选；回退恢复完整旧 release，不只替换个别 .py 文件。

安装过程中崩溃，按原 installation operation 与 active pointer 事实恢复；同一 accepted artifact 可重试安装，不能自动换新模型。stale_parent 不覆盖其他已安装版本；另起评测或明确结束。旧 session 继续其 pin，新 session 读取新 release。

回退选择已有有效 release，记录操作并 preflight；撤销模型不删除账本。托管 checkpoint 过期或服务不可用时不能声称历史 release 可部署；状态清楚标明，不能暗换别的底座。

M6 的 research receipt 沿用同一 preflight/CAS，只变主线 R；分支 W* 不自动成为主线 W。下一研究步骤实际绑定的 R 由 M4/M6 记录。一次 release 不混装两个未经共同授权的 W/R 候选。

## 4. 业务流程 app 的最小导出

新增窄函数 `export_deployment(installed_release_ref, destination) -> DeploymentManifest`，归 `release.py`；CLI 接线由 M7 在本版交付。首版只支持已安装且未撤销的 release。导出是选择运行所需内容，不能递归复制整个 release 对象闭包。

白名单包括：W 的运行策略、release pin 的 TargetHarness、所需模型与 inference 配置、runtime/依赖版本、工具绑定 schema、运行权限与预算、可公开验证的发布凭据摘要。`required_core_ref` 指一个固定的运行核心投影（权限/limits/合法目标），保留源 K 身份摘要，不包含 oracle、任务答案或研究访问权限。发布凭据只提供验证所需结果，不导出原始受限评测记录。

首版额外导出 HarnessPackage 的运行 bundle/manifest、启用组件、package API、运行含代码时的隔离镜像 digest、broker 的业务工具绑定与资源要求；评测和部署挂载相同运行字节。候选 tests/失败轨迹/研究笔记分开保存，不进入运行 bundle。不能只导出 prompt 而漏掉真正产生效果的工具/中间件；有生成代码的 app 仍使用隔离 worker，不因已接受就改为控制面 import。容器镜像属于声明的依赖，不把构建集群或训练环境装入 app。

排除 R、teacher、TeachingReference、训练数据、私有原草稿/理由、验证答案、云密钥、本地绝对路径和训练恢复凭据。外部依赖清单区分文件权重与托管模型服务；服务凭据由使用环境注入。artifact 下载后校验字节摘要；不可导出服务模型仍可输出服务绑定，但明确不能离线运行。

导入只检查兼容性与绑定，不能把未知包自动安装成改进冠军。用干净工作目录运行一个合法任务，证明不需要研究/训练进程；不要顺手实现远端目录、搜索、签名平台、市场或多租户授权系统。

## 5. 用例

| ID | 条件 | 预期 |
|---|---|---|
| ER6-01 | 教师辅助高分，无教师仍失败 | 高分仅列采集；候选按无教师结果裁决 |
| ER6-02 | control 与 candidate 底座/工具不匹配 | 同底座声明被拒绝或明确成为另一比较，不复用旧分数 |
| ER6-03 | 一侧 infra 故障或预算用尽 | 不伪造完整配对；按计划补跑或 inconclusive |
| ER6-04 | actor 装载失败、controller inference 不兼容 | preflight 失败，active 不动 |
| ER6-05 | CAS 前后分别崩溃、另一个安装先完成 | 原子恢复；stale_parent 不覆盖 |
| ER6-06 | 导出物内埋私有参考/密钥标记 | 白名单检查阻止；干净部署输入中找不到该标记 |
| ER6-07 | exported runtime 未启动 teacher/research 服务 | 合法任务可运行，实际加载模型与 release 一致 |
| ER6-08 | 源方法仅作为教学来源且 train 验证通过 | SourceWorkflow 立即可用；生产 dev evaluator/installer 设失败哨兵也不被调用 |
| ER6-09 | 新 h 下候选优于新基线 B，却劣于旧生产 parent | 可报告同 h 学习收益，但生产 gate 拒绝，active 不变 |
| ER6-10 | 新 h 基线中断或依赖不兼容 | preparation 不就绪，不采集训练；恢复复用已确认结果，不回退旧 h 冒充新基线 |
| ER6-11 | 经验证 source 随后请求直接发布 | 新 deployment 实验做 dev gate；旧 train 结果无安装权限 |
| ER6-12 | B 基线完成后冻结采集 plan 并请求执行 | 正确的补充许可可执行，不重写 B；旧基线许可或错 plan/任务/mode 的许可拒绝，引用图无环 |
| ER6-13 | 完整 dev 比较整体收益达标、其余条件满足且未被 screen 拦截；退化比例分别在冻结上限内和超出上限 | 上限内可 accepted，并保留退化记录；超出则 rejected、保留当前版本；不能仅报告失败子集涨分 |
| ER6-14 | 导出包含运行包代码但无 tests/研究材料，或包字节被改写 | 前者以评测时同字节/同隔离配置运行；后者 preflight 拒绝，不暗换代码或运行模式 |
| ER6-15 | train 与 dev 尝试各完成一次 | 两者逐条断言结果都写入 grading.jsonl；results.jsonl 行与旧结果读取不变 |
| ER6-16 | 新包只重跑失败 train 子集，旧包有其他 train 成功记录 | 两版本结果分别保留身份与覆盖范围；不拼成新包全量 train 成绩，正式 dev gate 仍完整执行 |

复用 `test_task_gate.py`、`test_gate_bypass.py`、`test_integrations.py`；故障注入沿用既有 release 测试风格。
