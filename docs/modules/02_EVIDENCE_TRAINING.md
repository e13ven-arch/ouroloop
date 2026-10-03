# M2：两类训练数据与模型构建

> 交付范围：本文件新增 actor/SFT/Tinker 与 controller 新训练验证归第二版，原详细规格保留；文中“首版”指本模块首次接入版本。第一版只保持已有 feedback/trainer/jobs/store 兼容，不要求训练账号或执行新训练。目标仍是两类监督分开、训练阶段不晋升。

## 1. 两条数据路径

| 路径 | 输入与允许的监督 | 输出/消费者 |
|---|---|---|
| 决策学习 | Observation + DecisionTrace + 有事实依据的 env/human FeedbackRecord | 既有 DataManifest → 决策 trainer → 校准/弃权配置 |
| actor 模仿学习 | M8 学生在环真实执行、环境成功的 student-visible trajectory | SFTDataManifest → Tinker SFT → GenerationArtifact |

保持旧规则：Jev/LLM 的决策回答、研究员意见、EVAL_ONLY 记录不能转为决策真值。

本轮增加 actor 蒸馏并保留独立 controller 的实际训练/装配路径；M4 根据数据可得性选择，应用无需同时训练两种模型。ResearchContext 只增加选择方法必需的数据目录/数量/适用性摘要，不把所有轨迹交给 R。新增 actor 模仿是单独的数据目的和入口；实施须审计 providers 文档与训练来源校验，不能删掉全局限制后让所有 LLM 输出可训练。teacher 模仿数据记录来源模型、材料许可/限制和采集条件；不把教师理由当作经环境验证的事实。

`completion_snapshot.v1` 仍只对 train 的 on_finish 状态评分：决策时捕获可复现快照、episode 后调用官方评分；失败或不可捕获返回 unknown。最终任务失败不自动否定此前独立有效的快照；冲突和修订沿用 conflicted/supersedes。Harbor 只给最终 reward 时不得推导 stop 标签。

## 2. actor 数据构建算法

拟建 `build_sft_dataset(plan_ref, trajectory_refs, selection_ref, objects) -> DatasetBuild`；M8 不自建第二个 dataset builder。

1. 验证 plan、学生版本、目标 harness、动作与消息 schema；沿血缘检查所有任务事实属于外层 train，任务家族与冻结映射一致。
2. 只选官方成功条件满足、无环境/运行器故障、执行记录完整的轨迹。初版不混 partial-success/离线改写/仅教师答题样本；排除原因逐类计数。没有成功轨迹返回 insufficient_data，不伪造正样本。
3. 使用 plan 在采集前冻结的内部 fit/validation family 分配；教师参考的任务示例仅来自 fit，validation 内容不能参与编写参考。重复轨迹、同任务前缀、变体和增强样本全部同侧。以 selection 声明每任务上限或权重，不把长任务变成隐式多数。
4. 重建每个 accepted turn 当时的学生输入；工具响应来自实际执行。审计流中的原提议、review 理由、私有 TeachingReference、oracle、后续消息不进入输入或目标。
5. 训练所有合格的 accepted assistant turns，包括 PASS 与 REPLACE，不只训练被纠正的步骤。role mask：system/user/tool/参考示例的 assistant token 为 0；本次执行产生的目标 assistant token 为 1。多模态或未支持的消息类型显式拒绝。
6. 模型 renderer 处理 tool-call 编码、边界与 EOS；固定 tokenizer/renderer/mask policy 的版本和摘要。允许全会话多目标或等价逐步样本，但不可让同一目标被重复加权；实际上下文裁剪后存在差异时必须逐步重建输入。
7. 超长样本首版排除并计数，不从任意 token 处截断 JSON/工具结果。记录原始/保留轨迹数、tokens、任务家族数和 PASS/REPLACE 分布；不能把训练集近乎排空仍返回 ready。

没有 fit/validation 家族就 insufficient_data；最小 smoke 可明确使用非成绩用途的单任务数据检查管线，但不能据它选模型或报告泛化。存在固定公共示例时，采集与无教师评测都使用相同示例，且示例 assistant 不承受训练 loss。

## 3. 训练、checkpoint 与装载

在 `train_artifact` 既有纯构建边界增加 role/method 分派，保持旧调用签名与默认 decision 行为。目标接口：

```text
build_task_dataset(...) -> 既有决策 DatasetBuild
build_sft_dataset(...) -> DatasetBuild(status, dataset_ref, exclusions)
train_artifact(..., role=actor 或 controller) -> TrainingBuild
```

M0 的 GenerationArtifact 由 ready 训练结果引用；不修改 active/champion。actor 探针交 M1 provider：加载确切底座/adapter、生成合法 ChatTurn、工具消息往返、接受历史重放。controller 保留 `.decide()` 的输出类与 schema 探针，按需独立 calibration。

配方显式记录 `init_model_ref`，默认从 DistillationPlan.student_release_ref 的 actor 初始化；若策略选择从同底座重训而非延续 adapter，必须在冻结配方及比较中明确。后端不支持该 checkpoint 初始化时预检失败，不能偷偷退回 base。一次新实验允许按配方重置 optimizer，但必须记录；同一被中断作业的精确恢复仍须恢复 optimizer/游标，二者不能混称。

Tinker trainer 首版仅 LoRA SFT：validate 模型/renderer/预算 → 创建 client → 固定数据顺序与 batch 计划 → forward/backward → optim step → validation → checkpoint → sampling artifact → probe。记录 base、rank、学习率、epochs/steps、seed、batch/sequence 上限、SDK/renderer、数据摘要、实际 token 和费用；参数须有边界，研究员不能取消上限。checkpoint 选择只看内部 validation，dev 任务只由 M3 gate 使用。

Tinker 训练状态与 sampling 权重不是同一产物。恢复保存 training state、已确认 step、数据游标及 optimizer 状态；无法确认最近 optim step 是否生效时，不盲目重放该 step。由 M5 保持 unknown、核对或终止该候选，已花费用不抹去。不能只加载采样权重就宣称精确恢复优化器。

训练不满足服务访问、数据权限、模型能力或计量条件时，在提交前失败。外部作业失败后保留诊断和费用；没有 ready artifact 不创建可装配 actor。数据少或只学会格式时回到采集/工作流，不自动升级模型规模、启动 RL 或增加专家池。

## 4. 决策模型独立性与漂移

首个参考 controller 仍是 `worker.stop`，输入只含当时可见事实，标签回答“此刻是否满足完成条件”。它不天然回答“继续的期望收益是否大于费用”。后者需要分支实验和明确成本效用，暂不混用标签。

actor 的版本是 controller 数据 applicability 的一部分。换 actor 后允许保留逻辑上兼容的历史完成标签，但必须重新检查输入分布、校准及完整任务行为；阈值不自动继承。actor 自己也能生成 finish 请求，controller 只是独立检查这一动作，不要求把所有停止意识从 actor 权重中删除。

## 5. 用例

| ID | 条件 | 预期 |
|---|---|---|
| DT6-01 | PASS/REPLACE 混合成功轨迹，另含失败/中断轨迹 | 前两者合格动作进入 SFT；其余排除并列原因 |
| DT6-02 | teacher 私有标记、原草稿和 oracle 有唯一字符串 | 所有训练输入和目标均无这些字符串 |
| DT6-03 | 工具 JSON、公共 assistant 示例、EOS 与超长样本 | mask 与边界正确；示例不计 loss；超长不破坏动作 |
| DT6-04 | 同任务多前缀、变体；dev 或内部 validation 答案进入教学参考 | family 不跨组；污染使构建失败且未发训练 |
| DT6-05 | actor artifact 被送入旧 decide probe | role 分派到生成探针；错误 schema 明确拒绝 |
| DT6-06 | optim step 后响应丢失，重启 | 不盲重训；保留收费与未确认状态 |
| DT6-07 | 仅有最终 reward、教师说 stop=yes | 没有 env/human 状态标签，不启动 controller 训练 |
| DT6-08 | 训练成功但 gate 未运行；或数据为空 | 前者无晋升，后者 insufficient_data 且无 GPU 请求 |

复用 `test_evidence_training.py`、`test_train_config.py`、`test_jobs.py`；新增少量生成 renderer/mask 与 fake Tinker 测试。实际云 smoke 单列，不放默认离线测试。
