# M4：行为诊断、实验提案与经验继承

> 待实现；保留 ResearchAgent.step 和 Candidate，新增 `research/experiment.py` 的薄规范化层，改造 basic/loop/experience。Context 权限由 M2 保证，执行由 M5、评测安装由 M3 保证。

## 1. 一轮的边界

```python
run_worker_round(round_context, budget) -> RoundResult
# round_context 固定 release_ref、experience_ref、scope、K、候选上限和评测计划模板
propose_experiment(context, bound_research_policy, owner) -> ProposalResult
```

`ProposalResult` 包含 `proposals` 与 `next_action(collect|repair_runtime|stop|none)`、reason、evidence_refs；旧 step 返回 [] 时记录 no_proposal，不等价于系统崩溃。默认最多一个候选；同一假设可含多个必要 changes。多出候选按冻结优先顺序截取并记录，不能自动增加付费试验数。

逻辑阶段依次为 inspect → diagnose → retrieve → propose → core admission。这些是一个研究员的步骤，不新建 Analyst、Curator 等独立进程。基础研究模型、effort、采样、工具和预算冻结在 K；加载 R 改变的是已注册阶段的 prompt/config，而非偷偷替换供应商或基础模型。

## 2. 行为诊断与候选逻辑

1. inspect 确认实际运行版本、合法输入覆盖和最终动作路线。无真实 trace 时先采集，不能从计划推断已生效。
2. 根据训练/开发结果生成 Requirement。expected 来自用户任务或允许材料；observed 引用实际事件；因果解释单独存 hypothesis。声称复发必须给出不同 episode 的 refs，重复读取同轨迹不增加 recurrence。
3. 按 problem tag、owner、target、输入/反馈版本、环境与底座过滤经验。先看适用条件，再看成功与失败、回归；过期记录仍可解释历史，不当当前证据。
4. 选择最简单可执行载体：缺字段先改输入；流程遗漏先 prompt/workflow；确定条件用 rule；依赖上下文且有可靠标签/数据规模时选 model。规则不是必须永远优于模型，训练也不是固定步骤。
5. 提案写出预期完整任务影响、可能回归、训练数据和反馈引用、停止条件。LLM 不得填写实际 result 或 accepted。
6. core admission 校验 owner/target、证据权限、schema、允许配方、模型规模、输入可得性、预算与预定评测器。不符合就 invalid 或请求采集；不得靠 LLM 二次解释绕过。

研究策略可增加辅助检查，但核心 admission 无论如何执行。`abandon_if` 仅可收紧搜索方向，不能降低 gate 或删去失败题。训练数据不足时返回 collect，下一次采集范围/数量仍由固定 schedule 与预算批准；不是研究员任意浏览测试任务。

## 3. 执行与经验写入

Experiment 把研究员建议与框架固定字段分开：父 release、数据与反馈 refs、控制版本、正式计划、预算由控制器填并持久化，再调用 M5。已有 research.loop 的 old GateRecord 路径通过 profile 分流保留；task profile 不调用旧 replay evaluator 冒充完整任务评测。

一次实验结束，无论是否安装，经验追加以下事实：问题/假设、变化、构建产物、实际装配、触发及覆盖、目标任务/全体任务结果、回归、费用、receipt、未解决问题。Requirement 的 addressed 只表示开发证据支持已处理；后续反例可生成 regressed 事件，不能擦掉原结论。

经验摘要为 `{summary_ref, source_refs, producer_policy_ref, applicable_to, counterexamples, status}`；status 为 `supported/negative/regression/inconclusive/expired`。摘要文字由 LLM 生成时保留 producer，数值由框架计算。检索先用已有 JSON 标签过滤和排序；不给每次实验训练 embedding，也不建多层专家记忆。

`experience_ref` 固定当轮可见快照；写入新事实后下一轮生成新快照。失败实验也被继承，避免重复同配置；相同假设在新输入或新反馈下可重试，必须说明条件变化。inconclusive 不等于假设已被反驳，不能永久禁用方向。

## 4. R 的更新与调度连接

M5 固定 R0，worker round 只能输出 worker owner。在已启用的 research slot 中，当前 R 可根据研究历史提出一个 research owner 提案，例如“把输入覆盖检查加入训练前流程”。候选变成完整 R1 PolicySet 后交给 M6；当前 R0 不裁决自己提出的 R1。

安装 R1 只替换主线 R，主线 W 沿用父版本。下一轮 `bound_research_policy` 必须从新 release 加载 R1，诊断、检索或提案调用的 trace 记录其 ref；R1 产生的 W2 提案引用 R1 和更新后的 E。仅改 manifest 标签而仍调用默认 R0 prompt 的实现验收失败。

如果 R1 没有任何 worker 提案，链条只到 R1 已装载；还不能宣称它推动了下一轮改进。R1 产生且实际评测 W2 证明回用；W2 是否更好由独立 gate/结果回答，不要求为了演示强行接受 W2。

## 5. 用例与验证

| ID | 场景 | 预期 |
|---|---|---|
| RL-01 | 轨迹缺任务要求字段；研究员提议直接训练 | admission 提醒输入/信号缺口；可改 view 或采集，不自动花训练预算 |
| RL-02 | 一次实验解决遗漏但破坏原成功任务 | 新经验同时含改善与回归，下一 Context 可检索二者 |
| RL-03 | 实验 infra 失败后同假设重试 | 不写为反驳证据；保留费用与条件，受有限重试/总预算约束 |
| RL-04 | LLM 引用不存在证据或把最终分数当所有标签 | 提案 invalid；不产生 env FeedbackRecord |
| RL-05 | 不训练的 prompt 候选获得开发接受 | 走同一 build/eval/install 路径，下一轮实际用新 W |
| RL-06 | R1 修改 preflight 次序但删除核心校验 | 可改变辅助顺序，核心校验仍执行；非法跳过请求被拒绝 |
| RL-07 | R1 已安装并生成 W2 | 实际模型请求、提案、实验及经验共同指向 R1；W2 经正式评测 |
| RL-08 | 研究员返回空列表、需要材料或停止 | 形成结构化 next_action 和 reason；不制造空训练作业 |

用脚本研究员检查控制流与经验权限；真实研究模型是否能提出有效改动属于后续业务实验，不用手写正确候选的测试冒充自动发现。

## 6. 实现状态（2026-09-27）

已实现：`ouroloop/research/experiment.py`（`ProposalResult`、`normalize`、三项辅助检查、`core_checks`/`admit`、`freeze`、`propose_experiment`、`retrieve`）、`ouroloop/research/loop.py` 的 `run_worker_round` 与 `run_worker_rounds`（旧 `run_round` 原样保留）、`ouroloop/research/basic.py` 的 `PolicyResearcher`（用已加载 R 的 prompt 发请求，记录 prompt hash）、`ouroloop/evolve/experience.py` 的 `experiment_record`/`summary_record`/`snapshot`/`requirement_status`。

辅助检查 block 时按原因区分 `collect` 与 `repair_runtime`；核心检查无论辅助列表如何都执行，且默认反馈构造器取注册表（空默认会让所有训练提案因错误原因作废）。`verdict` 与 `installed` 在经验记录里是两个字段。

验收：`python -m pytest tests/test_research_round.py -q` → 11 passed，覆盖 RL-01～RL-04、RL-06、RL-08 及实验冻结；RL-05 见 `tests/test_rsi_integration.py::test_it01_*`，RL-07 见 `test_it03_*`。

已知缺口：`research.diagnose` 的输出仍是字典而非持久化 `Requirement` 对象（requirement id 只落在经验记录里）；`abandon_if` 记录但未自动执行；经验检索只做标签过滤与稳定排序，没有语义检索。
