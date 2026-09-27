# M2：证据、反馈、数据分组与训练构建

> 待实现；复用账本、CompletionProbe、Dataset、TrainJob、trainer/store/serving。新增 Context 投影，改造 build/pipeline；不重写训练后端。

## 1. 输入、输出与责任

输入为 Observation/DecisionTrace、环境反馈、冻结 family/split、反馈构造器和训练配方。输出为可审计 Evidence、只读 ResearchContext、DataManifest 和未晋升的模型 artifact。M3 决定候选是否可安装；本模块不得移动任何冠军指针。

现有 `build.to_example` 会用当前 point 重新渲染历史 ctx；`build` 按 session hash 分训练验证；`pipeline.prepare_model` 将构建、校准、消费 GateRecord 和 replay 测试串在一起。业务路径不能仅传入一些 rows 就认为隔离完成，也不能借旧门取得新业务授权。

## 2. 反馈构造与可见性

`FeedbackBuilder` 为固定注册实现：`id/version, eligible_partitions, required_evidence, label_schema, build(trace, evaluator_evidence)`。第一版注册 `completion_snapshot.v1`，只处理 train 的 stop/on_finish；snapshot 在决策时捕获，episode 后官方 rubric 评分。不可捕获或评分失败返回 unknown，不能猜 no。

持久化只保留标签与可追溯受限证据，完整世界快照不进入 Context、训练文本或在线工具；需调试时单独受限保存。每条反馈 id 由 `(decision_id, builder_ref, evidence_digest)` 决定，恢复重复标注不得产生双倍样本。不同可信来源冲突时 status=conflicted，排除训练并保留双方；新反馈用 supersedes，禁止覆盖原始记录。

env/human 标签须有对应事实。研究员判断、晋升结果、LLM 自评、最终 reward 展开成逐步标签均拒绝。completion 标签回答“此刻是否完成”，不回答“继续是否值得”。分支比较是后续 builder，必须另有重放等价性和后续策略/预算身份，首版不实现它也能完成闭环。

任务最后失败不自动否定此前可独立核验的快照标签；仅在该快照契约完整且未受环境故障污染时保留，记录 `episode_failed=true`。数据集 manifest 公布此类样本数量和纳入规则。评分器版本或标签语义变化生成新反馈；旧标签不能换名字直接复用。

## 3. Context 与经验访问

`make_context(scope, release_ref, experience_ref, evidence_refs) -> ResearchContext` 是白名单投影，不返回 Runtime、episode、文件句柄、provider 凭证或 oracle。结构包含总目标、实际 Inspection、允许的 train/dev 结果、Requirement、机制活动、反馈目录、经验和剩余预算。

Context 读取服务先核验证据及其所有祖先的分组，再做脱敏和裁剪；原始文件路径不能当作绕过权限的引用。研究步骤使用控制器提供的固定模型调用，不开放本机 shell/任意文件读取。摘要仍是派生证据，必须保存 refs、生成方法和版本。缺来源的意见可作为 hypothesis，但不能作为已验证结论或训练真值。

全局 train/dev/test 是最外层边界，campaign 再缩小可见任务集合。禁止依据“当前目录在 train”推断所有对象可见。经验检索不自动搜索整个工作区；只在 scope 可见的索引快照查询。

## 4. 分组、表示与样本生成

1. 用冻结 family map 解析任务；同任务的全部 session、重复、前缀分支和变体同组。显式家族规则优先于字符串相似度。
2. 在外层 train 内一次性按 family 分 fit/validation/可选 calibration，并冻结 manifest。分配使用记录的 seed 和排序后 group ID；不按行或 session 随机分。
3. 过滤 feedback status、来源、版本与 view 兼容性。训练 point 候选名不一致、未知 action、空输入均记录排除原因。
4. 默认使用当时已存的 rendered_input。新 view 只可从当时保留的合法 Observation 重渲染，必须记录 transform ref；缺字段就重新采集，不从未来轨迹或世界状态补齐。
5. 生成 train/val 文件及 ref，校准数据保持独立；局部 test 不需要单独从业务 test 取。外层完整任务 dev 负责候选比较，最终 test 不给 trainer。
6. 数据检查覆盖 family 交集、血缘、标签分布、有效组数、默认策略表现、输入截断、重复样本权重。重复决策不增加独立家族数；按任务/家族设上限或权重，政策入 manifest。

fit/validation 组数不足就 `insufficient_data`，不退回行随机划分。calibration 仅在所选推理策略确实需要时启用，并提前预留；不借现有 5% 风险参数声称业务失败保证。view、prompt 或后续策略变化时校验 feedback applicability，特别是分支标签；不兼容的旧数据不自动进入新训练。

现有“一任务一家族”可以保留作已冻结实验，但签名重叠低不是语义独立证明。发现跨 split 的同工作流变体时标记原实验的独立性局限；要更改分组需新 split 身份并重新检查污染，不改旧 manifest 来美化结果。

## 5. 无晋升副作用的训练接口

```python
build_task_dataset(selection, split_manifest, feedback_ref, input_schema) -> DatasetBuild
train_artifact(dataset_ref, recipe_ref, parent_model_ref, budget) -> TrainingBuild
```

`DatasetBuild` 为 `{status: ready|insufficient_data|invalid, dataset_ref?, exclusions}`；`TrainingBuild` 为 `{status: ready|failed|cancelled, model_ref?, job_ref, recipe_ref, dataset_ref, metrics, costs}`。由 `pipeline.py` 抽出已有构建/检查/TrainJob/后端装载的纯构建部分，旧 prepare_model 继续包裹它并执行原校准/门流程，保持旧 API。

task 调用只得到未授权模型 artifact；加载探针检查 model id、输出类别与输入 schema。checkpoint 仅根据训练内部 validation 选；teacher/伪标签默认关闭。需要阈值时由独立 calibration 产生版本化 inference 配置，与模型一起成为 PolicyDescriptor，最终仍由完整任务 gate 验收。

训练开始前 M5 预留 GPU/模型/时长预算；取消或中断调用已有 worker 取消机制，记录 job 状态。远端状态不明先查原 job ID，不能直接发第二个训练；失败产物不能登记为 ready。失败与废弃作业成本照计。

## 6. 用例与验证

| ID | 场景 | 预期 |
|---|---|---|
| DT-01 | 同任务 20 次决策、两个变体、不同 session | 全部同 family 同侧；样本数和独立组数分别报告 |
| DT-02 | train episode 先缺一步后全部完成 | 两个快照分别 no/yes；在线输入没有 oracle；dev 挂 probe 被拒绝 |
| DT-03 | 重启重复 label；随后人工纠正 | 首次去重；更正追加 supersedes，冲突未解决前不训练 |
| DT-04 | 新 view 需要历史未存字段 | 拒绝该重渲染并提示重新采集；不读最终状态填补 |
| DT-05 | 数据选择混入 dev/final 或 final 衍生摘要 | 数据构建失败，训练器未启动 |
| DT-06 | task train_artifact 成功或失败 | 不消费旧 GateRecord、不运行外层 test、不移动 pointer；全部 job 成本可追溯 |
| DT-07 | scorer 抛错但 agent 声称完成 | feedback unknown；不产生 yes/no 标签 |
| DT-08 | 训练中控制器重启且远端作业运行 | 查询并接回原 job；同 operation 不重复提交 |

复用 `test_probe.py`、`test_evolve.py`、`test_jobs.py`、`test_roles.py`；保留原 decision 数据划分回归测试，为 task profile 增加分组与无副作用训练验收。
