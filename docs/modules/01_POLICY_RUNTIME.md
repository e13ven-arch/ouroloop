# M1：策略注册、运行绑定与实际检查

> 待实现；依赖 [公共契约](00_CONTRACTS.md)。负责 `policies.py`、`research/inspection.py` 及 agent/runtime 接线，不负责晋升评分。

## 1. 当前缺口与目标

已有 DecisionPoint、Policy、StopGuard、session pin 可以复用。但当前 `Agent._task_head()` 只取任务前 300 字符，`_stop()` 传文件名和短动作日志，未系统保留业务工具返回；`stop_view` 面向文件任务。业务要求后半段可能根本没进入决策器。先修可观测输入，再判断模型是否可学。

目标是对每个已启用策略回答：加载了哪个版本、在哪里被调用、看到了什么、提出了什么、最终谁执行、是否被覆盖。提案 artifact 存在不等于生效。

## 2. 最小目标注册表

`TargetRegistration` 为可信代码，含 `target, owner, binding, allowed_kinds, input_schema, output_schema, validate, bind, fallback`。`resolve(target)` 对未知目标报错。第一版仅注册：

| target | binding / 允许载体 | 真实绑定位置 |
|---|---|---|
| `worker.system_prompt` | config / prompt | 创建 Agent 时组合系统提示；每次运行记录最终 prompt hash |
| `worker.finish` | config / prompt、内置 workflow 配置 | 可信模板将结束检查指令加到 agent prompt，或配置已有 StopGuard 的受限提示；不创建额外隐式 LLM |
| `worker.stop` | decision / 内置 rule、model、spec | 只在 `stop/on_finish` 调用，输出 `yes/no/abstain`；spec 仅选已注册 view 参数，不能生成 Python |
| `research.diagnose` | research_step / prompt、内置 workflow | R 的诊断步骤，输出 Requirement 列表 |
| `research.retrieve` | config / 内置 rule | 经验检索字段、排序权重和数量上限；只能缩小已授权证据范围 |
| `research.propose` | research_step / prompt | ResearchAgent.step 的候选生成步骤，输出 Proposal 列表 |
| `research.preflight` | config / 内置 workflow | 在固定准入前增加已有检查或改变检查顺序；不能删除核心校验 |

`worker.finish` 配置指令和 `worker.stop` 判断动作分别注册，避免把 prompt 当动作模型。候选可同时改两者，但 C3 控制保持最终 prompt 相同。配方是训练任务参数，不是运行策略 target；模型构建完成后更新 worker.stop 的 model ref。

后续 retry/route/compact 需分别证明输入、反馈及动作接线，不因内置点存在就自动对业务开放。首版无任意函数名/模块路径插件注入。workflow 是已注册步骤及有限参数，不支持脚本表达式、动态工具或无界循环。

首版配置解析要求拒绝未知字段，最小内置集合如下；参数上界由 K 声明，R 不能放大：

- prompt：UTF-8 文本与长度上限。系统基础模板、worker.system_prompt、worker.finish 按固定顺序各拼接一次，记录最终 hash；不采用字符串查找替换修改宿主源码。
- `default.v1` rule：使用注册 point 在当前 phase 的固定默认，供控制与回退；R 不能通过该配置改硬预算/权限默认。
- `finish_check.v1` workflow：配置 `instruction_ref`，绑定为结束检查提示，不隐式发模型请求或读世界状态。
- `research_retrieve.v1` rule：`tags, targets, statuses, max_items, sort(recency|recurrence)`；仅在授权快照内过滤和稳定排序，同分按证据 ID；不接受自定义表达式或路径。
- `research_preflight.v1` workflow：`checks` 为 `input_coverage/label_availability/duplicate_experiment` 的有序不重复列表。检查分别读取合法字段覆盖统计、M2 信号资格、已冻结实验身份；输出 `pass/warn/block` 及 evidence_refs。可增加辅助检查，核心 schema/权限/预算/评测准入始终另外执行，不能删除。输入覆盖检查本身不使用 oracle 推断哪些业务需求重要。

model 的 inference 配置至少保存动作类别映射、归一化方法、阈值与阈值来源。第一版可用固定 argmax（平票弃权）或在 train 内 calibration 选定的置信阈值；拒绝未知类别/非有限概率，低于阈值弃权。任何阈值改变都形成新 policy ref 并重新做完整任务评测，不套用旧 decision profile 的业务风险保证。

## 3. 完成判断输入与动作语义

新增 `stop.business.v1`，保留 `stop.file.v2` 的旧行为。业务输入保存完整任务文本的 ref，渲染按确定性预算组织：任务要求文本优先，其次当前 final message、当前阶段、已观察工具调用及返回、剩余预算。工具事件为 `{call_id, name, args, result_excerpt, error, step}`，按时序保存；摘要/截断规则和丢失范围进入 `truncation`。可采用头尾保留或已注册结构化字段提取；不得用 oracle 提取“关键条件”。最大输入预算由 K 固定上界，实际长度记录，不能悄悄把任务前 300 字符当完整需求。

先用真实轨迹检查输入覆盖率和截断样例；信息不足则记录、扩大允许的视图或调整干预点，不承诺部分输入必能判断完成。LLM 生成的需求摘要是带成本的可选策略产物，不当作环境真值，首版先用原始任务与工具证据。

动作映射：`yes` 允许本次结束；`no` 返回固定、无隐藏答案的继续核查消息；`abstain`/超时/模型不可用走默认 on_finish=`yes`。`no` 不表示继续必然有收益。StopGuard 的最大 pushback、任务上限仍是硬边界；达到上限即按固定终止规则结束，记录模型建议被覆盖，不伪记为模型同意。per_turn 默认不启用学习判断。业务模式禁止接官方评分器作在线 checker。

模型合法但总是 abstain 可以产生任务结果；其“已装载但未干预”在报告明确标注。artifact/schema 不匹配属于安装失败；线上短暂服务失败才走声明回退。禁止把无法加载的 candidate 自动换成默认策略后宣称该候选通过评测。

## 4. 装配与会话生命周期

```python
validate_policy_set(policy_set, registry, capabilities) -> Validation
bind_release(release_ref, *, profile, services) -> BoundRuntime
inspect(bound_runtime, trace_refs=()) -> Inspection
```

`BoundRuntime` 是已有 runtime/agent factory 的适配结果，不是第二个 agent loop。评测候选从显式 release ref 和 plan 作用域的 EvaluationPermit 装配，与 active.json 无关；每个 arm 新建 session、工具环境和 pin。permit 只允许隔离评测，普通 session 必须有 bootstrap 或 accepted 授权。model backend 继续经 serving 获取；加载后校验返回 model id 与 descriptor 一致。

新 task/research profile 使用 M3 的 authorization 校验完整 W/R，不通过伪造旧 `Accepted` 或 calibration 来放行动作。旧 decision profile 的风险门和可执行点限制原样保留。新 profile 也只开放 K 中声明可作用的点，权限/deny 规则优先于策略。

普通新 session 读取一次 active release；已有 session 保持原版本。每次执行前仍检查撤销状态，撤销后的 worker 走已固定的安全默认并记录原因；不在同一 session 热切换另一套模型。research session 若其 R 被撤销，则中止后续提案，由调度器在下一轮 pin 合法 R，不继续执行已撤销研究流程。

`Inspection` 包含 loaded W/R、各 target 的绑定位置/版本、输入 schema、可用字段、禁用原因、默认值、实际调用/回退计数。没有 trace 时返回 `observed=false`，不填零表示已观察；summary 只能引用已产生事件。对研究员过滤掉内部路径、oracle 和 final 数据。

## 5. 用例与验证

| ID | Given / When | Then |
|---|---|---|
| PR-01 | 任务重要要求在第 300 字符之后，工具已完成部分动作 | stop 视图包含该要求或明确截断，不无声丢失；无 rubric/世界快照 |
| PR-02 | 相同 W 分别绑定默认与小模型；模型给 no | trace 显示模型提出 no、StopGuard 实际继续；随后任务可再次结束 |
| PR-03 | 两次 pushback 后仍给 no；或服务超时 | 记录预算/guard 覆盖或回退，动作与轨迹一致 |
| PR-04 | 配置说 model=A，服务返回 B；或未知 workflow | preflight 失败，候选不进入正式评测，active 不变 |
| PR-05 | W1 正在运行时安装 W2 | 旧 session 保持 W1；新 session 使用 W2；撤销 W1 后旧 session 走固定默认 |
| PR-06 | R1 改 research.propose 的 prompt | 下一轮实际 LLM 请求使用 R1 prompt hash，Inspection 和候选来源都引用 R1 |
| PR-07 | 候选把评分函数名写进 rule 配置 | 注册表拒绝；线上工具仍仅有合法工具集 |

复用 `tests/test_agent.py`、`test_act.py`、`test_harness.py`、`test_roles.py`，新增业务视图/注册表测试。脚本 provider 用于证明接线与回退；不能据此宣称小模型有真实收益。

## 6. 实现状态（2026-09-27）

已实现：`ouroloop/policies.py`（`TargetRegistration` 与本文七个目标、内置配置解析且拒绝未知字段、`validate_policy_set` 要求完整集合、`descriptor`/`replace_policies`、`StopDecider`、`Activity`、`BoundRuntime`、`bind_release`、`SessionBinder`、`fixed_defaults`、`build_candidate`）、`ouroloop/research/inspection.py`（`inspect`、字段覆盖率、`public` 投影）、`ouroloop/points/__init__.py` 的 `stop.business.v1` 视图与 `business_stop_point`、`ouroloop/runtime.py` 的 `propose`（route `policy`，记录 proposed/executed/executor/fallback_reason）、`ouroloop/stop.py` 的 `policy=` 接线与 override 事件、`ouroloop/agent/loop.py` 的业务观察、工具事件与 Observation/DecisionTrace 落盘。

`worker.stop` 只在 `on_finish` 生效：`per_turn` 记录观察但不询问策略，行里 `proposed` 为空、`executor` 为 `default`。模型输出非法类别抛 `CandidateDefect`（尝试作废），声明的弃权、低于阈值和服务不可用走回退并记录原因。

验收：`python -m pytest tests/test_policies.py tests/test_policy_runtime.py -q` → 25 passed，覆盖 PR-01～PR-05、PR-07、ER-03、ER-09 及 CT-03/CT-05/CT-06。PR-06 在 `tests/test_rsi_integration.py::test_it03_*` 以真实请求的 prompt hash 验证。

已知缺口：`worker.stop` 的 `spec` 载体通过"同 model_ref、新 spec"表达，没有独立的 spec kind；`retry/route/compact/tool_gate` 未注册为可改目标；校准阈值只支持 `argmax` 与预先给定的阈值来源，没有接入自动 calibration 产出；进程级隔离未实现，首版只执行声明式配置。
