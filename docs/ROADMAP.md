# v0.6 实施路线图

> 2026-09-30 范围修订，代码基线 `b1cb7a7`。首版交付受限 harness 代码演化、应用发布与递归回用；第二版完善训练固化。仅调整相关规格，已有实现、旧 CLI 与历史证据保持原义。详细交接见 [模块目录](modules/README.md)。

## 1. 现有基础与缺口

已有 M0～M7：注册策略、真实业务观察、反馈/训练、完整任务配对评测、W/R 发布、单预算根和恢复、研究一轮及 R0/R1 campaign、七个 bench phase。代码基线 `b1cb7a7` 的离线测试为 554 passed, 1 deselected（[实现快照](history/V0_5_IMPLEMENTATION.md)与 JOURNAL 中的 553 为此前记录）。

已有真实 GLM-5.3 在 `marketing.product_adoption` 一次 dev 运行：该任务共 14 条断言，其中 6 条初始即成立且结束时仍成立，被评分器排除、不计入分母；8 条计分断言全部通过（task_completed_correctly=1，partial 1.0）；15 turns、29 次工具调用、229 秒，第 15 轮请求的历史为 27,132 token；当时未配置价格，cost unknown。该任务保持 dev，不得重新标为未见 test。这只证明一次运行可用，没有证明训练或递归收益。[运行日志](JOURNAL.md)

首版待补：GLM-5.3 actor pin、能力包内容/manifest 身份、包内代码提案与构建、固定隔离 worker/工具 broker、工具与 hook 实际装配、技能/经验加载、回归/发布/导出，以及自动循环和 R campaign 的接线。R 诊断输入同属首版待补：当前 `task_results_summary` 只给逐题 success/partial/终止/失败类，不含断言明细和轨迹（`ouroloop/research/context.py:116-146`）；首版为每个失败的 train/fit 尝试提供失败摘要与断言明细；dev 只给汇总，final 不进入 R 上下文，dev/final 都不给断言明细和轨迹，这改变了 v0.5 研究员看不到任何断言明细的做法（`context.py:120` 的 docstring）；诊断经逐题笔记→汇总产出至多一个 patch 或结构化 next_action。M3 的 `run_tasks` 把逐条断言结果另存 `bench/<domain>/grading.jsonl`，不进 results.jsonl；M7 的结果存储按冻结 split 过滤读取，默认只返回 train。包草稿冻结前做草稿检查：含代码时 package_check → isolated_probe → package_test，只含 skills/memory 时只做结构/路径/manifest 检查；失败只把检查输出回传 R，在 preset 固定的次数内修订，通过后才冻结进 Experiment。第二版保留 generation 训练绑定、自动教学适配、review 前置、SFT/Tinker 及训练恢复设计。两版的新增能力和收益均未测得。

## 2. 交付顺序

| 阶段 | 工作与模块 | 退出条件 |
|---|---|---|
| A 诊断输入与文本能力包 | M1 pin GLM-5.3 actor、宿主加载 skills/memory；M0 HarnessPackage/worker.harness 身份；M4 train/fit 失败摘要与断言明细、逐题笔记→汇总、harness_edit 文本组件；M4/M5 文本草稿检查（结构/路径/manifest）与有界修订；M3 另存判分明细 `grading.jsonl`、M7 按冻结 split 读取；M7 preset/K 先固定 max_failure_notes、失败摘要条目长度上限与 max_draft_repairs；首轮真实演化前运行 GLM-5.3 train+dev 基线 | 包路径、内容摘要和 manifest 可校验；无人工优化方案即可从真实 train 失败生成文本包，读取/注入事件可查，经 gate 裁决，下一轮沿用已采用包；dev/final 断言明细不进入 R；旧兼容通过；不需要 Docker、trainer 或 teacher |
| B 代码组件与隔离 | M1 固定 package API、Docker worker/工具 broker、hook（先 on_finish）、包工具；M4 harness_edit 代码组件；M4/M5 把冻结前试跑修复扩展到 isolated_probe → package_test（同一 max_draft_repairs）、有界构建/恢复 | 无人工优化方案即可从真实 train 失败生成可执行 tools/middlewares 组件，经草稿检查与 gate 裁决，下一轮沿用已采用包；接口可校验；生成代码只在 worker 执行，不在控制面 import，无 in-process 回退；真实 hook/工具行为可查；不需要 trainer 或 teacher；本机 Docker daemon 可用是真实检查前提 |
| C 验证与 app | M3 同模型固定 dev 配对 gate（含逐题退化记录）、preflight/CAS/导出 | 包被真正执行；有收益才采用；干净部署只需 runtime/包/模型服务/工具绑定 |
| D 递归方法回用 | M4/M6 两支搜索能力包；M3 安装 R | 同 W/E 起点、相同代码 API/镜像/预算；下一改进调用实际加载 R1；失败保留原版本 |
| E 用户入口与真实验证 | M7 harness preset/cycle/报告；完整示例 | 自动代码提案、真实任务、包迭代与有界 C4 留有记录；工程与收益结论分开 |

实现状态（2026-09-30）：A～E 的工程链路已实现，离线测试 667 passed, 1 skipped, 1 deselected；跳过的是 IT6-23 Docker 隔离测试（需本机 Docker daemon 与 worker 镜像）。E 的退出条件中“真实任务、包迭代与有界 C4”尚未运行，需冻结预算并由用户决定开始付费运行。明细见 [JOURNAL](JOURNAL.md)。

A～E 属于首版，递归方法比较与实际回用仍为必做。第二版接 M8 教学适配/采集、M2 SFT、M1 sampling/teacher hook 及独立 controller 新训练验证，沿用已写规格；不把这些账户或依赖放入首版预检。按 [分版验收](modules/08_INTEGRATION_CASES.md)执行，未来用例不删除也不算首版缺项。真实试验需模型服务与冻结预算；不具备时真实验证标未完成。

补采请求经同一 driver 自动续跑（purpose=task_feedback）属首版，IT6-19/21 首版验收，覆盖补采恢复及 worker 段与活动的停止区别；更换 h 时创建 baseline release/新比较身份与 source_validation（只验证教学来源）属第二版，IT6-18/20 第二版验收。

新增 cycle 只是按固定 schedule 调用现有 handlers；不另造编排器。R 使用已有 provider/策略对象，逐题笔记与汇总串行调用同一 provider，不建子 agent 平台；M6 复用已有 campaign。scope 首版允许能力包目录内工具/中间件代码、技能/经验与注册 W/R 策略；框架源码、任务数据/评分、权限、预算、依赖锁/镜像和发布核心不可演化。固定 package API 的变化仍由工程开发接入，不让候选自行注册新的宿主入口。

## 3. 首条真实实验路径

首个业务后端采用 AutomationBench 直连。首版 actor 固定为 GLM-5.3（已配置并计价的 OpenAI 兼容接口），冻结底座、权重身份和推理配置；R 可共用供应商。Docker 用于能力包代码隔离，不管理任务集群，也不要求接 Harbor；只含 skills/memory 的包由宿主装配，不启动 worker。现有决策 trainer 保持兼容，但首版实验不训练新模型。第二版生成训练首选 Tinker LoRA SFT，候选学生 Qwen3.5-9B；届时验证实际模型/SDK/费用，不提前构成首版依赖。换 Qwen3.5-9B 学生时，h* 需在学生上重新演化：[Harness-Zero](TECHNOLOGY.md#4-参考设计如何转为独立实现) 的 h* 是在学生上验证的（其 base+h* 为学生带 h* 的成绩），首版在 GLM-5.3 上演化的包不直接作为学生的 h*。

先测 GLM-5.3 的无教师起点。真实基线尚未运行：2026-09-30 一次启动在第一个任务中途中止（4 次模型调用，无完成尝试）；train+dev 基线在首轮真实 h* 演化就绪时、演化开始前运行，既有单任务 dev 成绩不能作为 before。从 train/fit 失败摘要与断言明细经逐题笔记→汇总生成共享能力包；先交付 skills/memory 文本组件，再交付代码组件。代码草稿冻结前完成隔离接口 smoke（package_check → isolated_probe → package_test）及有界修复，再真实执行和 dev gate；本机 Docker daemon 当前未运行，是 B 阶段代码组件真实隔离检查的前提。诊断可聚焦失败 train 任务；候选采用前仍比较完整固定 dev 清单，不另建历史成功任务池或回归作业。至少观察一个工具封装或中间件改变真实执行；静态检查通过或文件变化不能作为 C1 证据。

任务家族和失败覆盖先统计，不预设一定涨分。首版按 purpose=task_feedback 的 CollectionRequest 补 plain train attempt；第二版训练缺口使用对应 purpose，同一 driver 不变。不能把 test 迁入 train，或通过代码编写读取 dev/final 答案。预留 gate/递归额度，计入 R/actor、逐题笔记、草稿检查/修复、代码构建/隔离运行、失败候选与全部评测。

在基础 app 链路可运行后，安排一次冻结的 R 方法比较及其下一轮回用检查。R1 无收益则保留 R0，报告 C4 未成立；不为展示递归而强制安装。以后可增加实验规模或新后端，但不能把这条核心能力永远留在设计里。

## 4. 效果验收与最小比较

保留 C1 同底座整体迭代收益、C2 领先模型位置、C3 独立决策贡献、C4 改进方法效果。首份报告包含起点、有效 prompt/workflow、演化能力包和有界 R0/R1 比较；新训练/蒸馏及 C3 效果验证归第二版，未运行明确 not_run。包的装配、实际行为、固定 dev 比较中的退化与总成本必须可查。首版沿用现有 GatePlan 的整体收益与退化比例门槛，不把任何单题退化一律判为拒绝；进一步的回归能力等真实运行暴露具体问题后再设计，不成为本轮开发依赖。

C2 默认只引用官方榜单和上游公开结果，记录来源/日期/任务集/effort；不运行本地领先模型，不混成配对差。若将来需要同题结论，另行冻结范围和预算。

工程测试证明机制，不保证模型涨分。真实 C1、C4 仍是待证明的效果目标：未提升就记未达目标，不用“完整 RSI 不再验收”回避。最终 final 在方案锁定后按冻结协议运行；用其结果继续优化后需新的独立任务支持泛化主张。

功效与成本按 GLM-5.3 串行预期：单任务约 $0.14–0.45、约 4 分钟；train 52 + dev 25 基线约 $11–35、约 5 小时；每轮（dev gate 对照臂与候选臂各 25 题，gate 每次重跑对照臂，`ouroloop/bench.py:571-591`；另重跑 train 失败题）约 $10–40、约 5–6 小时；逐题笔记诊断每轮约 $1–2。Harness-Zero 的 h* 相对 base 增益为 SpreadsheetBench +8.0、AppWorld +21.4、USPTO +26.0 个百分点；dev 25 族时约 +20 个百分点才可能显著，SpreadsheetBench 量级在 25 族上测不出。gate 按现行规则装包：点估计 ≥ 冻结任务计划的 min_gain 且回归在容忍内即 accepted，显著性另记（`ouroloop/evolve/task_gate.py:254-269`）；GatePlan 默认 min_gain 为 0.05，bench 未给 worker gate 计划时回退 0.01（`ouroloop/bench.py:1087`），首版由冻结任务计划（`[freeze.task_plan.gate]`）显式固定该值，不另设 preset 键。C1 结论按“只能识别约 +20 个百分点的效应”预期；锁定后的 final（23 族）同样只看得出大效应。

## 5. 明确后置的功能

Harbor 仅在需要其原生任务/沙箱时接入，DeepAgents 仅在 source 原语确实需要时接入。首版不做新决策点大全、新训练后端、专用研究权重训练、多 agent 研究群、市场、多租户、数据库/队列、框架全仓代码自修改或 RL/DPO 平台。受限能力包代码演化属于首版，不能以此条禁止其实现。

每个模块提交说明复用位置、新增文件/依赖的必要性、错误恢复语义和对应验证。保留包名、CLI、旧身份；不复制第三方代码。完整方法链与递归核心必须完成，外围 feature 按实际缺口逐步加。
