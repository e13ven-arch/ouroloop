# v0.5 架构审查：RSI 完整性、Raven 借鉴与实施缺口

> 2026-09-27。审查对象为本仓库当前代码与 v0.5 设计；Raven 固定在 `40a6b456dcd13f9c551a0f78dc1361e55b90e4e4`。结论针对该版本和已审查路径，不是对所有 RSI 定义或未来版本的判定。本次只有文档变更，没有新增运行效果。

## 1. 结论

**修订后的设计可以论证为一个边界明确、工程闭环完整的系统层 RSI 方案；当前代码还不能宣称完整 RSI 已实现，更不能宣称递归收益已被证明。**

原 v0.5 已有 W/R/E/K、反馈、候选、评测和回用方向，但“完整版本安装”“统一策略”“研究策略比较”仍偏概念。若直接交给不同开发者，容易实现出互不兼容的冠军指针、训练时提前消费旧评测门、只存 R1 名字而没有加载 R1 等问题。本次详细设计补的是这些执行契约；通过文档审查不等于通过代码验收。

本项目采用的完整性是：系统能持久改变任务方法，也能持久改变产生后续改进的方法，固定核心负责验证、预算、权限和发布；变化被实际执行且可追溯。它不要求任意改写自身所有代码、基础模型权重或裁判，也不承诺自加速、逐代单调提升。这是项目的操作性定义，不能说成业界唯一 RSI 定义。

## 2. Raven 对照：区分设计、实现和证据

Raven 仓库包含不同路径，不能把各路径的设计与实验拼成一个已经全部集成的系统：Curator 侧重运行中的 worker harness 改进，Evolver 侧重受评测约束的候选搜索，另有研究用例。以下只比较可核验机制，不设框架性能排行榜。

| 维度 | Raven 已审查设计/源码 | ouroloop 的落点与边界 |
|---|---|---|
| 改什么 | Curator 把 memory/planning/capability/action 作为可替换策略职责 | 借鉴职责分类，统一注册 typed target；首版只开放完成判断与有限研究步骤，不建四个服务 |
| 从哪里开始 | workflow 读取 worker.inspect 的实际事实，再设计和检查候选 | 增加实际绑定/输入覆盖/动作执行者检查；不能只读取配置宣称生效 |
| 如何改 | generation 明确理解、选择、设计、实现、修复等阶段 | 保留必要逻辑，复用一个 ResearchAgent；首版改声明式 prompt/rule/workflow 或训练模型，不开放任意源码 |
| 如何装 | worker.check/probe 后再 install；组合策略需要协调激活 | 以完整 W/R release、加载探针、单一指针 CAS、撤销与恢复交付，避免只改模型卡片 |
| 如何继承 | feedback iteration 将要求、失败及后续表现带回下一轮 | 同时保存正负经验、动作输入、局部标签来源、数据和模型版本；经验与训练共用证据链 |
| 如何泛化 | Dataset 源码明确反复同案例只说明在这些案例上训练，泛化需冻结 harness 的保留案例 | 外层 task final 与 research final 单独封存；公开任务预训练污染仍是限制 |
| 如何选模型 | 本次固定提交的 sealed runner 按训练分数选 best，再报告 test | 吸收封存思想；不沿用历史文档中可能存在的按 test 选优描述来批评当前实现 |
| 谁改进研究者 | 已查 Curator 路径主要是 curator/provider 驱动 worker 改进，不能据此认定已证明“改进器自身变强” | 明确注册 R 的步骤，paired campaign 评价 R1，安装后真实回用；这是待实现和待验证目标，不是现有领先优势 |

对应来源：[Curator 设计](https://github.com/EverMind-AI/Raven/blob/40a6b456dcd13f9c551a0f78dc1361e55b90e4e4/experimental/docs/design.md)、[实际 workflow](https://github.com/EverMind-AI/Raven/blob/40a6b456dcd13f9c551a0f78dc1361e55b90e4e4/experimental/curator/workflow.py)、[生成阶段](https://github.com/EverMind-AI/Raven/blob/40a6b456dcd13f9c551a0f78dc1361e55b90e4e4/experimental/curator/generation/run.py)、[反馈迭代](https://github.com/EverMind-AI/Raven/blob/40a6b456dcd13f9c551a0f78dc1361e55b90e4e4/experimental/iteration/run.py)、[Dataset 的证据边界](https://github.com/EverMind-AI/Raven/blob/40a6b456dcd13f9c551a0f78dc1361e55b90e4e4/experimental/iteration/dataset.py)、[sealed 选择实现](https://github.com/EverMind-AI/Raven/blob/40a6b456dcd13f9c551a0f78dc1361e55b90e4e4/evolver/orchestrator/sealed/runner.py)。

Raven 对“实际行为如何被修改并安装”的描述比原 v0.5 更具体，值得吸收；仅因四类策略覆盖更广，不能推导其递归定义或实验证据一定更完整。反过来，ouroloop 写出 R 自更新，也不能推导实际效果比 Raven 好。架构比较回答“哪些机制值得实现”，效果比较仍面向领先模型与同底座起点。

## 3. 审查发现及本次设计修订

| 级别 / 缺口 | 当前依据 | 修订与实现验收 |
|---|---|---|
| P1 策略载体与返回类型混淆 | 原描述所有策略都有 actions，未说明研究 prompt 如何加载 | [M0](modules/00_CONTRACTS.md)、[M1](modules/01_POLICY_RUNTIME.md)：decision/config/research_step 分开；target 注册、真实绑定与 R1 调用验证 |
| P1 业务输入可能不足 | [agent/loop.py](../ouroloop/agent/loop.py) 的 _task_head 截断为 300 字符，_stop 以文件和短动作输入为主 | M1：版本化业务 view、合法工具证据、截断记录；先审计可见性再训练 |
| P1 分组和历史表示不兼容 | [build.py](../ouroloop/evolve/build.py) 使用 session 切验证，并用当前 point 渲染旧 ctx | [M2](modules/02_EVIDENCE_TRAINING.md)：family 内部分组、Observation 血缘、版本化重渲染和排除规则 |
| P1 构建与旧门存在副作用耦合 | [pipeline.py](../ouroloop/evolve/pipeline.py) prepare_model 包含校准、record.consume 和 replay | M2：抽取纯训练 artifact 路径；保留 legacy 包装，不制造业务假 GateRecord |
| P1 授权与原子发布不具体 | [release.py](../ouroloop/release.py) 的三元组与旧 Accepted，[registry.py](../ouroloop/evolve/registry.py) 的模型冠军不足以表达完整 W/R | [M3](modules/03_EVALUATION_RELEASE.md)：SystemRelease、专用 receipt、唯一 active、CAS 与崩溃恢复；旧 profile 不混装 |
| P1 错误和预算可能使比较失真 | [task_eval.py](../ouroloop/evolve/task_eval.py) 的资源/异常边界；[benchmark.py](../ouroloop/benchmark.py) 的 max_usd 对未知费用不执行限制 | M3/[M5](modules/05_EXECUTION_BUDGET.md)：候选缺陷与 infra 分离、finally 清理、预留/结算/unknown、嵌套同一总账 |
| P1 研究评测尚无可执行隔离协议 | 原设计仅概述同起点、外层保留和回用 | [M6](modules/06_RESEARCH_CAMPAIGN.md)：CampaignPlan、数据权限、分支 W*/R 固定、独立单位、实际 R1→W2 链 |
| P2 “受限代码”范围过宽 | Context 去字段与文件白名单不等于隔离；旧 Workspace 持有 runtime | M0/M1/[M4](modules/04_RESEARCH_LOOP.md)：首版仅非执行配置和可信函数；任意生成代码需后续独立隔离交付 |
| P2 独立性与成绩声明可能过强 | 断言期望值签名低重叠不足以证明不同工作流家族；accepted 易被理解为统计提升 | ROADMAP 修正；M3/[M7](modules/07_BENCH_REPORT.md) 分开开发晋升、最终证据与工程状态 |

以上是设计补齐项，代码中的差距依然待后续模块实现；不能把此表标“运行缺陷已修复”。

## 4. 完整 RSI 的论证与可证伪条件

在固定 K 下，R 读取允许的经验 E，产生候选 C；构建、完整评测及安装产生新的 W 或 R；下一轮实际使用新版本。两条更新路径共享同一状态机、证据和预算，因此可以形成有限、可恢复的自我改进过程。

| 必要性质 | 设计保证与验收 | 何时不能宣称完成 |
|---|---|---|
| 可观测行为 | M1/M2，PR-01/02、DT-02 | 只有最终分数，不知实际输入/动作 |
| 可执行自修改 | M1/M4，IT-01/02 | 只有反思文字或未加载 artifact |
| 外部结果约束 | M3，ER-01～05 | 自评、换指标、删坏题、用 test 选优 |
| 持久且可恢复 | M3/M5，IT-04 | accepted 未安装、混合版本或重启丢状态 |
| 经验进入后续改进 | M2/M4，RL-02/03 | 只增加日志，下一轮不可检索或不知适用版本 |
| 改进方法也进入更新闭环 | M4/M6，IT-03 | 永远固定 R，或只变 R 名字、不变实际研究行为 |
| 有边界的递归与资源 | M5/M6，IT-05/06 | 每层重置预算、无限元研究、候选改裁判 |
| 独立效果可检验 | M6/M7，IT-07 和真实 C1～C4 | 用工程夹具、开发晋升或重复同题冒充收益 |

所有工程条目通过，且至少出现一条真实系统提出的 R 更新及回用链后，可以称“实现了本设计定义下的完整 RSI 框架”。C1～C4 仍须逐项报告；C4 不支持时，应说框架支持递归更新、该实验尚未证明研究收益，不能换一种 RSI 定义来回避负结果。

## 5. 区别与价值落在哪里

核心假设是：agent 的某些高频、有限动作判断存在可学习结构，执行反馈能形成比最终 reward 更具体的训练信号，训练后的策略在完整任务中提高质量或降低成本。训练并非必然优于 prompt；可观测信息不足、反馈太少或 prompt 已解决问题时，小模型可能没有净价值。

因此本项目交付的是 prompt/规则/小模型共用的可验证改进框架，重点研究可学习决策，而不是一个必须训练的按钮。独特组合落实在类型化决策、局部反馈与整任务评测相连、W/R 共用可恢复发布，以及小模型与研究策略各有必要的贡献对照。它是可实现的产品与研究定位，不是已经证明的首创性或性能领先。

最能说明价值的演示应从真实缺口出发：原 agent 漏掉一步；策略利用当时合法证据改变结束行为；完整新任务结果改善；后续研究又从失败实验中改进自己的诊断/检查并真实回用。最终是否“让人眼前一亮”，取决于这条链的真实效果、成本和复现性，不取决于框架模块数量。

开发从 [模块入口](modules/README.md) 开始；完整跨模块用例见 [集成验收](modules/08_INTEGRATION_CASES.md)。
