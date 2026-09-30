# 模块详细设计与开发交接（v0.6）

> 代码基线 `b1cb7a7`，2026-09-30 修订交付边界。首版交付能力包代码演化/发布/递归；第二版完善训练，保留其详细规格。M0～M7 既有实现不重写。历史状态见 [快照](../history/V0_5_IMPLEMENTATION.md)。首版 `*6-` 用例已按阶段 A～E 实现为离线测试（覆盖清单见 [JOURNAL](../JOURNAL.md)），标为第二版的用例仍是待实现规格；离线通过只证明接线，不是真实收益。

先读 [总设计](../DESIGN.md)、[技术选型](../TECHNOLOGY.md)和 [实施顺序](../ROADMAP.md)。核心交付是自动发现有效方法、验证、固化、发布，并让改进方法的更新实际参与下一轮。源 harness 可以演化；单次蒸馏的目标 harness 冻结。人工流程可调试，不能替代核心自动路径。

## 模块所有权

代码路径相对 `ouroloop/`。拟增文件可合并进现有小文件，不得另建同职责系统。

| 模块 | 设计目标 | 复用/改造位置 | 允许新增核心位置 |
|---|---|---|---|
| M0 [公共契约](00_CONTRACTS.md) | 身份、源/目标方法、两类模型、权限、v2/v3 兼容 | `evolve/contracts.py` | 无新类型中心 |
| M1 [运行时](01_POLICY_RUNTIME.md) | 首版包代码/API/broker/hook/材料装配；第二版教师/sampling | policies.py、agent/loop.py、runtime.py、stop.py、providers/ | 首版 agent/harness_package.py；第二版 providers/tinker.py |
| M2 [证据与训练](02_EVIDENCE_TRAINING.md) | 既有反馈/训练兼容；第二版模仿/事实监督、SFT/artifact | evolve/build.py、evolve/pipeline.py、evolve/feedback.py、research/context.py、trainers/store/jobs | 第二版 trainers/tinker.py；按需 evolve/trajectories.py |
| M3 [评测与发布](03_EVALUATION_RELEASE.md) | 源验证、无教师 gate、完整 release、最小 app 导出 | `evolve/task_eval.py`、`evolve/task_gate.py`、`release.py` | 不新增发布服务 |
| M4 [自动改进](04_RESEARCH_LOOP.md) | 从反馈产生候选、选择固化方法、记经验 | `research/agent.py`、`basic.py`、`context.py`（首版失败摘要/诊断输入）、`experiment.py`、`loop.py`、`evolve/experience.py` | 无新研究平台 |
| M5 [执行预算](05_EXECUTION_BUDGET.md) | 唯一状态机、全活动预算及恢复 | `evolve/execution.py`、jobs | 无第二编排器 |
| M6 [递归比较](06_RESEARCH_CAMPAIGN.md) | 相同起点比较改进方法、验证 R1 实际回用 | `research/campaign.py` | 无新 campaign 系统 |
| M7 [任务与报告](07_BENCH_REPORT.md) | 配置/preset、cycle 入口、后端、证据报告 | `bench.py`、`benchmark.py`、`report.py`、`cli.py`、`adapters/automationbench.py` | 按需 `adapters/harbor.py` |
| M8 [工作流蒸馏](09_DISTILLATION.md) | 第二版：自动教学适配、teacher、学生在环采集 | 调用 M1/M2/M5 | 第二版 distill/teacher.py、distill/collect.py |
| [集成验收](08_INTEGRATION_CASES.md) | 自动闭环、独立部署、递归和故障场景 | 现有 tests 与离线 fixture | 少量集成 fixture，无新生产模块 |

M0 独占公共字段；M1 独占实际运行/provider 绑定；M2 独占建集/训练；M3 独占安装；M4 选择候选但不直接发布；M5 独占状态/预算；M6 复用 M4/M5 做分支；M7 独占 CLI/config/RunManifest；M8 不复制 Agent 循环或训练器。

## 开发顺序与完成条件

1. 首版先做诊断输入与文本能力包：M1 pin GLM-5.3 actor；M0 HarnessPackage/worker.harness 身份；M1 宿主装配 skills/memory，纯文本包不启动隔离 worker；M4 train/fit 失败摘要与断言明细、逐题笔记→汇总、harness_edit 文本组件；M4/M5 文本草稿检查（结构/路径/manifest）与有界修订；M3 的 run_tasks 另存判分明细 `bench/<domain>/grading.jsonl`，M7 结果存储按冻结 split 读取；所需上限（max_failure_notes、失败摘要条目长度上限、max_draft_repairs）由 preset/K 先行固定。首轮真实演化前运行 GLM-5.3 的 train+dev 基线；补任务反馈后按有界 driver 继续，零人工优化方案即可起步，不需要 Docker、trainer 或 teacher。
2. 首版再接代码组件与隔离：M1 固定 API/工具 broker/Docker 隔离 worker、hook（先 on_finish）与包工具；M4 harness_edit 代码组件；M4/M5 把冻结前草稿试跑修复扩展到 isolated_probe/package_test，次数由同一 preset 上限固定。生成代码只在 worker 执行、不在控制面 import，组件种类由 K 冻结，K 允许代码时 freeze/smoke 须确认 worker，运行中不可用按 M0 §3 记 infrastructure_failed/repair_runtime 并暂停活动，不收窄种类；保留旧绑定/身份。本机 Docker daemon 可用是真实检查前提。
3. 首版 M3 同 actor gate、成功任务回归、完整安装/回退/导出；M7 给无 trainer/teacher 的 preset/cycle/报告。
4. 首版 M4/M6 在共同 package API/资源下比较 R0/R1，验证安装后的实际改进请求；这条递归核心不能因训练后置而取消。
5. 第二版再接 M8 教学源验证/适配/采集、M2 SFT/Tinker、M1 教师/sampling 及 actor/controller 新训练验证。保留既有规格/用例，按分版验收执行。

前四步构成首版最小完整 RSI，第五步是第二版模型固化。函数签名可适配既有风格，包的真实执行、故障、预算与递归边界不得缩水。公共字段/运行接口分别以 M0/M1 为权威，避免多个模块另定义一套 package schema。

## 兼容与控制规模

保留旧 CLI、decision profile、旧 hash、v0.5 用例及完整 release。v3 generation 不复用旧 `.decide()` 路径；旧 run 按旧规则解释。未实现配置启动时拒绝，不走伪 fallback。

包内工具/中间件代码生成属于首版；Harbor/DeepAgents、新决策点、新训练算法、全仓代码演化、通用插件平台和市场服务不属于首版核心。默认单控制面加固定隔离 worker（只由含代码的草稿检查与运行含代码的包使用）、串行实验、一套重试；新增宿主 API/依赖仍按工程接入，不让候选自己扩权。

每个模块交付须含实现、对应行为测试、准确示例和状态更新。工程 fixture 不写成真实收益；真实结果无提升不伪造成功，效果目标与工程完成状态分别报告。
