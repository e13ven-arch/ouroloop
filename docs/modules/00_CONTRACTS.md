# M0：公共类型、身份与权限契约

> 状态：待实现。本文是跨模块字段与语义的唯一来源；接口均为拟建 Python API。代码落点 `ouroloop/evolve/contracts.py`，纯类型、校验与序列化，不发模型请求、不读全局冠军。

## 1. 身份与兼容原则

不可变对象使用带类型的内容引用 `type:sha256:<64hex>`。哈希覆盖 `schema_version` 与规范 JSON：UTF-8、键排序、紧凑分隔、拒绝 NaN/Infinity；列表保序。时间、路径别名、展示备注不参与内容身份；实际 artifact 字节另存摘要并校验。身份相同才允许复用；短 ID 只作展示。旧 RunManifest 的 16 位 hash 保留原算法，新增协议使用 `schema_version=2` 及完整引用，不能重算旧记录。

已有模型 store 的不可变 ref 不改格式；Policy 中同时记录 `model_ref` 与可核验的 artifact/配置摘要。供应商可变模型别名须记录请求 ID、日期和可获得的版本信息；不能假装别名等于冻结权重。

| 类型 | 必填字段及语义 |
|---|---|
| `ArtifactRef` | `ref, media_type, digest`；ref 可定位，digest 验证内容 |
| `EvidenceRef` | `id, kind, scope_id, task_id?, family_id?, partition, producer_ref, content_ref`；partition 为 `train/dev/research_dev/final`；无任务的研究记录继承其全部输入的数据边界 |
| `PolicyDescriptor` | `schema_version, owner, target, binding, input_schema, output_schema, implementation, applicability, fallback, limits, evidence_refs`；owner 为 `worker/research`，binding 为 `decision/config/research_step` |
| `PolicySet` | `schema_version, owner, parent_ref?, policies, provenance_ref`；policies 按 target 唯一，保存完整集合而非仅 diff |
| `SystemRelease` | `schema_version, parent_ref?, worker_ref, research_ref, core_ref, authorization_ref, provenance_ref`；没有经验可变指针，E 由运行 manifest 固定；authorization 区分 bootstrap、evaluation_permit、accepted receipt |
| `EvaluationPermit` | `schema_version, subject_ref, parent_release, plan_ref, scope_id, core_ref, expires_at`；固定控制器签发的候选评测权限，只允许指定隔离评测，不允许安装或普通 session |
| `Requirement` | `id, owner, context, expected_behavior, observed_behavior, evidence_refs, acceptance, recurrence, status`；status 为 `open/addressed/regressed/insufficient_evidence`；acceptance 为行为描述，不能覆盖正式 gate |
| `ExperimentProposal` | `owner, requirements, hypothesis, evidence_refs, changes, training?, expected, abandon_if?, notes?`；研究员提交的非可信建议，不含正式门槛 |
| `Experiment` | `id, proposal_ref, parent_release, experience_ref, core_ref, eval_template_ref, budget_scope`；由框架冻结；`candidate_ref, dataset_ref, build_ref, eval_plan_ref` 等产物引用由追加事件补齐 |
| `EvaluationReceipt` | `schema_version, experiment_ref, profile, subject_ref, control_ref, plan_ref, results_ref, verdict, reasons, metrics, uncertainty, costs`；profile 为 `task/research`，verdict 为 `accepted/rejected/inconclusive/invalid` |
| `InstallationReceipt` | `operation_id, expected_parent, target_release, preflight_ref, status, observed_pointer, reason?`；status 为 `installed/stale_parent/installation_failed` |

`implementation` 是有标签的联合：`{kind: prompt, artifact_ref}`、`{kind: rule|workflow, builtin, config_ref}`、`{kind: model, model_ref, spec_ref, inference_ref}`。无 `eval`、动态 import 或任意代码路径。`actions` 仅存在于 decision 的 output_schema 内；prompt 配置及研究提案不能被迫编码为动作字符串。输入/输出 schema 必须引用注册表中已知版本。

`changes` 每项为 `{target, kind, change_ref}`，或在提案阶段以内联 JSON/text 表达 change；准入时规范化为不可变 artifact。owner 必须与注册 target 一致。训练条目为 `{point, feedback_ref, data_selection_ref, recipe_ref}`，四个引用都由注册目录校验。旧 `Candidate.kind` 继续表示 spec/recipe/prompt 等改动载体，**不表示 owner**；由 `ExperimentProposal` 包装旧 Candidate，旧调用方默认走原 decision profile。

评测模板先冻结任务、比较政策、预算与统计参数；构建完成后仅将实际 candidate artifact ref 填入完整 eval plan，再签发 EvaluationPermit，随后才能评测。permit 引用 policy set 与 plan，plan 不反向引用 permit/release，避免哈希循环。评测用 SystemRelease 的 authorization 为 permit；晋升后另建引用 accepted receipt 的 SystemRelease，W/R 内容不变，但 release 身份不同，provenance 连接二者。不能因“候选未接受”而无法评测，也不能因“允许评测”而直接上线。

## 2. 运行与数据契约

`RunManifest v2` 保留已有字段，新增 `system_release_ref, worker_ref, research_ref, experience_ref, core_ref, eval_plan_ref, feedback_ref?, input_schema_refs, data_lineage_ref, budget_scope, pricing_ref?, capability_ref`。`run` 与 `attempt_id` 分开：相同 run 下可以有多个任务/重复/基础设施补跑。

`AttemptKey = (run_ref, task_id, repeat_index, arm)`；`attempt_id` 再包含 `retry_index`。补跑不能覆盖原记录。正式配对键为 `(plan_ref, task_id, repeat_index)`，不使用随机 session ID 匹配。`TaskResult v2` 增加 `attempt_id, pair_key, system_release_ref, failure_class?, supersedes?, budget_receipt_ref, observation_refs`，保持原 success、partial_credit、validity 语义。运行器错误没有有效得分时，不擅自写成成功/失败的任务证据。

`Observation` 保存 `task_text, messages/tool_events, phase, step, budget_remaining, visible_refs, truncation`，只含决策当时可见的信息。`DecisionTrace` 保存 `decision_id, observation_ref, policy_ref, input_schema, rendered_input_ref, proposed_action, executed_action, executor, fallback_reason?, override_reason?`。trace 中的“提议”和“执行”必须来自两个对应位置的事实。

`FeedbackRecord` 保存 `decision_id, task_id, family_id, source(env|human), feedback_ref, target, evidence_refs, observed_policy_ref, input_schema, status, supersedes?`。status 为 `valid/unknown/conflicted/invalid`；EVAL_ONLY 记录不能转换为这一训练类型。可见性是输入血缘的交集：摘要、训练产物与经验不能通过换 partition 洗掉来源。

`DataManifest` 包含 `source_split_ref, family_map_ref, feedback_ref, input_schema, transform_ref, fit_groups, validation_groups, calibration_groups, row_refs, exclusions`。三组互斥且均在外层 train；外层 dev/test 不写入训练文件。变更字段语义需新 schema/ref，不可原地重新解释旧数据。

## 3. 核心接口与不可变核心

```python
# 所有参数类型由本文件定义；实现分别归属对应模块。
build_candidate(experiment, context, budget) -> BuildResult       # M1/M2
evaluate_task(candidate_ref, plan, budget) -> EvaluationReceipt  # M3
evaluate_research(candidate_ref, campaign_plan, budget) -> EvaluationReceipt  # M6
install(receipt_ref, expected_parent, operation_id) -> InstallationReceipt  # M3
run_worker_round(round_context, budget) -> RoundResult           # M4
```

`BuildResult = {status: ready|invalid|infrastructure_failed, candidate_ref?, artifacts, errors, costs}`；错误不能折叠为空候选。`RoundResult = {experiment_id?, outcome, release_before, release_after, evidence_refs, budget_receipt_ref}`；outcome 使用 M5 终态或 `no_proposal`，不以 gate accepted 代替 installed。

`CoreManifest` 固定 adapter/scorer/工具语义、schema 注册表、能力权限、数据划分与访问规则、GatePlan、预算与重试政策、代码版本。变更 K 建立新实验身份；不允许研究员在自然语言 acceptance、workflow 或工具参数里覆盖。注册合法目标与安装权限属于 K，候选不得自己注册新目标。

## 4. 存储与权限

复用工作区文件和追加 JSONL，不引入数据库。新文件建议位于 `rsi/objects/<type>/<digest>.json`、`rsi/events.jsonl`、`rsi/active.json`、`rsi/operations/`、`rsi/budget/`；大 artifact 和模型继续使用已有 stores。events 引用对象，不重复保存完整轨迹。写对象采用临时文件、flush/fsync、同文件系统 rename，必要时 fsync 父目录；控制操作由一把工作区锁串行化，基于 `ledger.locked/write_json` 补齐持久化要求，不假定已有实现全部满足。active.json 保存 `{release_ref, revision, last_operation_id}`。

新 task/research profile 的 active.json 是唯一生效来源；已有 models/champion.json 只服务 legacy decision profile。启动必须显式选择 profile，禁止两个指针隐式拼装一个 session。初始化通过固定配置生成 bootstrap authorization（非评测接受凭据），基线不得借此宣称提升。

研究员接收 `ResearchContext` 序列化白名单；worker 只接收合法 observation；evaluator 单独持有 oracle；控制器持有预算、安装与全量存储权限。首版只执行可信内置函数和非执行数据，因此不把 Python 同进程对象包装称为安全沙箱。开放生成代码前必须另行交付进程/文件/网络隔离。

## 5. 验收用例

| ID | 条件与操作 | 预期 |
|---|---|---|
| CT-01 | 相同对象换 JSON 键顺序；改实际 prompt 一个字 | 前者 ref 相同，后者不同；NaN 拒绝 |
| CT-02 | 读旧 RunManifest、TaskResult 和 Candidate | 旧 hash/行为不变；缺 v2 字段不能冒充 v2 完整运行 |
| CT-03 | research 提案改 worker target；prompt 声明 decision 输出 | schema/owner 校验拒绝，未运行模型 |
| CT-04 | final 证据包装成摘要再标 train | 依据血缘拒绝进入 Context/数据集 |
| CT-05 | legacy 与新 profile 同目录启动 | 每个 session 仅 pin 一个明确来源；混合配置报错 |
| CT-06 | 未接受候选获得指定 plan 的 evaluation permit；尝试普通 session/安装 | 隔离评测可以执行；普通 session/安装拒绝，permit 不升级为 accepted |

下游模块用本文件的结构作为契约测试夹具；模块自有内部类型无需全部放进公共文件。

## 6. 实现状态（2026-09-27）

已实现于 `ouroloop/evolve/contracts.py`：规范 JSON 与 `type:sha256:<64hex>` 身份、分区合并（派生记录继承最严格来源）、schema 注册表（含各视图的 `required` 字段）、本文表中的全部记录类型、`Objects` 对象库（原子落盘、写入即去重、读取校验哈希）、`Events` 追加日志、`copy_closure`（跨工作区复制且身份不变）。`CandidateDefect` 在此定义：候选缺陷与基础设施故障必须分开，否则坏候选会被无限重试到通过。

`RunManifest v2` 与 `TaskResult v2` 在 `ouroloop/benchmark.py` 扩展：v2 字段未设置时不进入哈希载荷，旧运行的 hash 不变；`RunManifest.v2()` 明确回答"这是否是完整 v2 运行"，缺字段的旧记录不能冒充。

验收：`python -m pytest tests/test_contracts.py -q` → 13 passed（CT-01、CT-02、CT-04 及对象库/事件日志）；CT-03 见 `tests/test_policies.py`、CT-05/CT-06 见 `tests/test_policies.py` 的 profile 与 permit 用例。

已知缺口：`ArtifactRef` 只记录摘要，不校验大文件字节；短 ID 仅用于显示，没有解析路径；旧 `Candidate` 仍由 `ExperimentProposal` 包装，未提供自动迁移工具。
