# M7：任务后端、配置与成绩报告

> 首版用现有命令接通能力包自动演化、发布和研究方法回用；第二版增加蒸馏配置。保留七个历史 phase，另加 `cycle`。所有者 `bench.py`、`benchmark.py`、`report.py`、`cli.py` 与 adapters。首版配置（`[improvement]`、preset `marketing_harness_v1`、`[harness_evolution]`）与 `cycle` 已实现（2026-09-30）；第二版配置仍为待实现契约，reader 明确拒绝。

## 1. 阶段与端到端入口

| 既有 phase | v0.6 行为 |
|---|---|
| smoke | 首版检查 provider/工具/评分、包 API/计量，以及当前包组件种类在 K 内；K 的 components 含代码种类或当前包运行含代码时检查隔离 worker，不可用则不能以该配置启动；第二版启用方法才检查 review/训练 |
| baseline | 起点 actor（第二版为学生）及已选 prompt/workflow 的无教师基线；首版为所 pin actor 的 train+dev 基线（原定 GLM-5.3，2026-10-03 起为 Claude Opus 5.5，见 ROADMAP §3 第 2a 步），在首轮真实演化前运行；不默认跑领先模型 |
| collect | plain 保持原行为；reviewed 调 M8，仅 train，须有已冻结合法 plan |
| improve | 首版 M4 run_worker_round 读取 train 失败摘要、逐题笔记后汇总诊断，生成包内 patch/策略候选，经冻结前草稿检查进入唯一 executor；第二版准备教学/训练 |
| research | 用 M6 做有界 R0/R1 比较，条件满足才安装 R1 |
| evaluate | 方案锁定后的无教师 final，禁止据此继续选择或采集训练数据 |
| report | 纯离线读取，区分源验证/采集/dev/final、脚本/真实、应用/研究收益和费用 |

无需用户手动拼 refs 或每轮执行七个命令。新增一个薄入口 `ouroloop bench --phase cycle`（已实现，`bench.cycle`）：按冻结 schedule 调用现有 phase handler，先预检和缺失基线，再自动 worker rounds/已计划 research slot/安装后下一轮回用，最后生成开发报告。不重写内层状态机、不默认自动运行 final；中断后复用同一 activity 和 operations。单独 phase 仍可用于排错和续跑。

cycle 按 M4 的结构化结果推进，而非看到 proposals 为空就退出：`next_action=collect` 先完成受限补采并重建 context，再继续剩余轮次；只有无方法、无法获得所需能力、补采无进展或限额耗尽才结束当前 worker 段。M4 driver 负责相同的分派规则，M7 提供现有 collect/执行 handler，不另写一份自动循环。

第二版源方法以 purpose=source_validation 通过后提供教学来源，适配/蒸馏不先走生产 gate；直接发布则创建 deployment 实验。换宿主时先准备新 baseline，再建相应 plan。首版包候选直接走 deployment。所有准备/补采/实验消耗原轮次/费用，阶段切换不重置。

无 worker 提案是该段结束原因，不自动取消活动中已冻结的 research slot；cycle 仍按固定 schedule 在有额度时执行方法比较及下一 worker 段。取消、全局预算耗尽或影响整个活动的能力故障才结束全活动；K 所需能力（如隔离 worker）运行中不可用时暂停活动（M0 §3），不进入 research slot，修复后以同一冻结条件续跑。cycle 每一步的暂停或额度耗尽写入 `rsi/cycle.json` 该步的 outcome/reason 与活动的 status；暂停时退出码为 1，额度耗尽为 0（M5 §3，2026-10-02）。M6 分支本来禁用 research slot，结束后将其最后正式通过 gate 的 W 返回外层。

`--phase research` 与 cycle 的 research slot 共用 M6，不能重复触发相同已完成操作。安装 R1 后 cycle 预留至少一次实际改进调用的额度/轮次验证回用；没有余量则记录回用未完成，不把 ref 更新当作完整验收。

人工模式仍将完整 proposal 直接接 admission/M5 executor，不伪装自动研究。自动/人工入口不能同时控制同一活动；origin 由入口赋值。scripted 模式仅用于旧兼容和 fixture，示例必须标明。`report` 不补模型请求，`evaluate` 只在用户锁定的 final 计划下运行。

## 2. 用户配置、默认配方与冻结

新配置示意如下。首版部分已由 reader 实现，完整可运行的示例见 `examples/automationbench/improvement.toml`；其余环境路径、工具绑定、数据划分、定价与硬预算沿用现有完整配置。preset 是代码中的配置模板（`bench.PRESETS`），不是插件；它的 harness 数值是上限，`[harness_evolution]` 只能收窄或调低。

```toml
[bench]
execution_backend = "automationbench" # 缺省保持直连；可选 harbor

[improvement]
mode = "automatic"                   # manual/scripted 另有明确入口
preset = "marketing_harness_v1"
allowed_methods = ["policy_edit", "harness_edit"]
max_candidates_per_round = 1
max_collection_batches = 2           # 每个预先分配的 worker 段上限，重启不重置

[harness_evolution]
package_api = "harness_package.v1"
components = ["memory", "skills", "on_finish", "before_tool", "after_tool", "tools"]  # K 冻结的组件种类；阶段 A 只列 memory/skills
sandbox = "docker"                  # 含代码的草稿检查与运行含代码的包使用（M0 §2）；components 含代码种类时 freeze/smoke 须确认 worker；固定镜像及 CPU/内存/PID 上限来自 preset/K
max_files = 24
max_package_bytes = 262144
test_timeout_seconds = 30           # 示例限额，freeze 后 R 不可提高
max_draft_repairs = 3               # 初稿之后的修订次数上限（最多 4 份草稿）
max_failure_notes = 30              # 每轮汇总读取的笔记数上限（缓存与新生成合计）

[collect]
mode = "plain"                      # 首版收集 task_feedback，不要求教师或训练账号
```

collect.mode 是单独 phase 默认输入；自动补采由 CollectionRequest.purpose 决定。首版 task_feedback 运行合法 plain train attempt、保存任务结果，不要求中间决策标签 builder；decision_feedback 才要求状态真值。第二版 actor_imitation 才用合法教学 plan/reviewed。不能因为配置里有未来训练项就主动检查 Tinker/teacher。

第二版显式加入 distill_actor/decision_train 后才读取 distillation.trainer=tinker、recipe=lora_sft_v1、collect.mode=reviewed 等未来参数，沿用 M2/M8 详细规格。首版与旧 CLI reader 遇到尚未支持的新方法明确拒绝，不伪造运行。

以上为输入便利层，freeze 生成 K、W0/R0、schedule、配方目录等不可变对象后，以 refs 为权威。reader 只接受已实现枚举；未知 preset/算法/参数、能力不足、缺预算在付费前失败。preset 提供 bootstrap prompt、改进/适配模板、参数边界和默认配方，以及段 ID、补采限额和后续提案额度的默认 schedule；它不能替用户扩大工具权限或凭空提供模型账户。首版 harness preset 还固定允许的组件种类 components、max_draft_repairs（默认 3，初稿之后）、每轮 max_failure_notes（示例 30）、失败摘要的条目长度上限及现有 worker GatePlan 的 min_gain/max_regression_rate（沿用原字段，不新增回归配置层），freeze 写入 K 后 R 不能提高；草稿检查/修复与笔记调用逐次计入同一预算。首版 preset 还固定 research slot 的配对规则：当前 W 为两支共同起点，train 家族为内层训练池，开发计划的家族按冻结顺序对半分，前一半作两支内层 gate，后一半作两支都不接触的外层比较（本项目的 split 只有 train/dev/test，这一半即 M6 的 research_dev）；每支预算为冻结活动预算的固定比例（示例 15%），外测额度也是固定比例（示例 8%，含一次补跑），在公共测量与两支开跑前预留；研究位开始时的主线经验冻结为两支共同的 E0（实验记录去掉原始 gate 理由）。开发家族少于 2 个时 research slot 拒绝启动。

首版只要求 actor/R，二者可共用已配置供应商；模型、推理配置、计价和限额在 freeze 摘要列明。首版 actor 原定 GLM-5.3（已配置并计价的 OpenAI 兼容接口），2026-10-03 起先跑 Claude Opus 5.5（订阅额度，计价为名义值，见 ROADMAP §3 第 2a 步）；第二版换 Qwen3.5-9B 学生时，h* 须在学生上重新演化并验证。第二版 teacher 配置显式指定或引用能力允许的 provider。任何路径都不能默默选择新收费供应商，正常循环不要求用户每轮写代码/方案/超参数。

宿主 harness 选择来自 K 目录，默认使用当前 h；能力包代码候选在该宿主的固定扩展 API 内生成，不需要每个包 hash 预先进入目录。不得由 CLI 直接在控制面 import 候选入口。只有换宿主才准备 B；报告保留 parent、实际 package/有效工具 schema/hook 和 candidate，发生宿主切换时另外列新基线。

freeze 时 source/teaching/训练 artifact 可能尚不存在；只冻结方法目录、目标范围、数据、限额与模板。运行期各依赖产物生成后，再冻结具体 SourceWorkflow、DistillationPlan 和 Experiment。严禁占位 ref 混入可执行对象或事后改写已冻结实验。

首版交付完整、无密钥、可 freeze 的自动闭环示例（建议 examples/automationbench/improvement.toml）：actor（GLM-5.3）/R、任务家族、基础工具权限、包 API/固定隔离镜像及文件/资源限额、轮数/research slot/总预算；无需 trainer/teacher 字段。第二版再给完整蒸馏示例。默认 final 单独锁定，预采集仅在身份匹配时复用。

v3 RunManifest 增加 mode、actual actor identity、source/target harness refs、distillation_plan_ref（采集时）、dataset/training provenance、teacher present、任务后端/version，以及关联的实际 R/experiment、experiment purpose、parent/execution base 和 pending/completed collection refs。旧 run 无字段时保持旧算法，不冒充完整 v3。人工输入不能绕过 M4 的固定 method 准入；actor 采集前不要求伪造两类决策标签。

首版还冻结 package_ref、有效工具 schema/hook 清单、package API/image/限额与代码来源；teaching/training 字段未启用时为 None（序列化时省略，不以空串/空列表占位，旧 RunManifest hash 不变），不填假 dataset/checkpoint。RunManifest 新字段分版：首版启用 mode、actual actor identity、target harness ref（当前宿主）、任务后端/version、package_ref/有效工具 schema/hook 清单/package API/image/限额、实际 R/experiment、parent 与 pending/completed collection refs；distillation_plan_ref、dataset/training provenance、teacher present、source harness、execution base 与 experiment purpose 第二版启用，首版保持 None 不写出（purpose None 即 deployment，M0 §3）。构建检查、实际 hook/工具行为、安装及下一包迭代分别引用 operation/事件。

最小 app 导出由 M3 实现，M7 为现有 release/CLI 增加一个窄入口（命令拼写由实现随帮助文档一起冻结），输入已安装 release 和目标目录，输出 DeploymentManifest。开发示例须提供精确导出/装载命令；不要只给设计 API 便称用户可用。

## 3. 任务执行后端

抽出当前 AutomationBench 调用处实际需要的窄边界：

```text
TaskBackend.capabilities() -> 能力声明
TaskBackend.run_attempt(frozen_attempt, agent_factory, budget) -> TaskResult + trajectory refs
```

冻结任务发现/评分配置复用现有 manifest，别抽象所有 benchmark 的业务语义。`agent_factory` 由 M1 装配 pin 的 W 和可选 reviewer，后端不得自选另一模型。AutomationBench adapter 保持现有直接模拟世界、工具契约与官方 scorer；不能为“统一接口”改变有效任务/服务权限。

可选 `adapters/harbor.py` 将 Harbor 的任务 ID、环境/trial 配置、verifier 结果和 agent 轨迹映射回来。Harbor 管环境创建、清理与 trial 内调度；ouroloop 管实验、配对与全局预算。只用官方扩展接口；固定实际依赖版本。能力声明至少包含 review_before_execute、snapshot_for_completion、resume_trial；不支持某能力就禁止对应模式。

Harbor 任务仍须固定 revision/family/split，费用/time/trace 可查；可选接入先验证一个原生任务的评分映射。未选择 Harbor 不初始化其环境；代码演化所需 Docker worker 是独立能力，不以 Harbor 安装与否决定，也不把 AutomationBench 强制迁移进 Harbor。

## 4. 报告与失败可见性

结果存储（`benchmark.py`）新增判分明细读取接口：按冻结 split 过滤，默认只返回 train，非 train（dev/research_dev/final）返回空；这是“dev/final 明细不进入 R”的边界。report 可离线汇总成功/失败，但不把 dev/final 断言明细投影给 R，也不写入报告正文。

原有 comparisons 与成本报告扩展以下字段：actor base/checkpoint、controller 是否加载/触发、target harness、教师辅助标识、来源 proposal 类型、独立任务/家族数、PASS/REPLACE、有效/排除轨迹和训练 tokens、无教师任务结果、采集/训练/推理支出、安装与下一 session 回用事实；source 验证、适配映射、实际 loaded R、R0/R1 外层比较及下一改进调用的 refs。

首版主表为同 actor 起点、有效 prompt/workflow、演化能力包；补包代码/材料摘要、实际工具/hook/技能/经验使用、固定 dev 配对结果与观察到的退化和构建/隔离、草稿检查/修复及逐题笔记成本。训练指标归第二版，首版未运行 C3/蒸馏明确 not_run；包通过测试不当任务成功，包 hash 变化不当 C1/C4 提升。

M4 的失败 train 子集重跑单列诊断覆盖，未重跑任务标明历史版本；不与新版本子集结果拼成完整 train 成绩，也不把 dev 回归写成所有 train 成功任务已复测；展示实际冻结的 min_gain/max_regression_rate 与 verdict，容忍范围内的退化也保留：逐题列出退化任务及两臂均值，早于该清单的收据显示 not recorded。复用测量引用原 TaskResult/版本/attempt 与匹配依据，不计作新增独立样本；正式主表和发布结论仍依据 M3 的冻结完整评测计划。

v3 冻结比较计划显式绑定本地 C1/C3（及可选同题 C2）的 control/candidate ref：C1 比学生起点与改进后的完整 W，包含工作流和模型变化；若单独主张训练收益，另固定其他 W 内容；C3 必须是同 actor、同提示下 controller 开/关。当前 `report.py` 把 evolved 与 prompt_best 用作 C3，新增 actor 训练后不能继续自动套用，否则会把 actor 训练收益误记为决策模型收益。v2 历史报告保持原映射，v3 缺 C3 配对则 not_run。

保留 2026-09-28 的默认决定：领先模型数字只取官方榜单/上游公开结果，不在本地跑。新增报告输入 `external_references`，每项保存 source_url、retrieved_at、model、effort、score、metric、task_set、domain 和 protocol_notes；作为不可变引用材料，不能伪造成 TaskResult/本地 attempt。

v3 最终计划区分 C2 的 `mode=published_reference`（默认）与 `paired_run`（未来可选）。引用模式只生成背景表；本地配对主张标 inconclusive、evidence_kind=external_reference，并说明不能作同题胜负判断。`seal/report` 不能沿用“必须存在两个本地 frontier 臂”去阻断引用模式。只有 paired_run 明确启用时才按已冻结范围要求实际领先模型运行；广义多模型 C2 沿用至少两个供应商，单模型结果只声明相对该模型。旧 v2 报告语义保持原样。

主表只放正式无教师结果；教师辅助采集单独展示，用来诊断可学上限，不当作模型本身的分数。官方外部榜单单独展示任务集/日期/来源，不进入本地数值差。引用模式明确写未做本地同题比较；可选 paired_run 的缺测如实保留，不能将缺配置当失败。

报告必须保留无收益、拒绝、成本未知、训练失败、预算耗尽与未运行项。source 的 validated 单列为“教学来源验证通过”，不计 accepted/installed 数量；补采仅记数据进展。某个 worker 段结束不等于全活动失败，仍展示后续 research slot 的真实状态。

C4 读取真实 campaign 结果，不预设 not_run。只有未执行才标 not_run；研究位在形成 campaign 之前结束时（研究位回复无效、R 没有提案、准入拒绝或构建失败），C4 仍标 not_run，detail 按 `rsi/cycle.json` 中该研究位的结局如实写出，不写成“没有 campaign 记录”（2026-10-02，见 [M4 §1](04_RESEARCH_LOOP.md)）。`cycle.json` 中研究位在 campaign 之前的结局记 outcome、reason 与 stage（reply 指 R 的回复，admission 指准入，build 指构建），报告据此区分这四种；研究位暂停、预算耗尽（写明在 campaign 之前还是之中）、计划已冻结而尚无收据时，同样标 not_run 并写明。campaign 有收据时（含某一支暂停时登记的 inconclusive），C4 按收据读；已运行但样本不足为 inconclusive；没有收益如实记录。完整 v0.6 验收要求递归路径和有界真实验证，单个 manual 蒸馏实验无需伪装覆盖 C4。历史 research 报告仍按既有结果读取。`report` 禁止触发模型请求、重新选择 checkpoint 或补跑 final。恢复 `evaluate/improve` 只补计划缺项，已完成结果不重花钱。

## 5. 用例

| ID | 条件 | 预期 |
|---|---|---|
| BR6-01 | 旧配置无新增块；新配置有未知字段/占位符 | 旧路径不变；不完整新配置在付费前拒绝 |
| BR6-02 | collect 与 build 指向不同学生/harness plan | freeze/admission 拒绝，不复用不匹配轨迹 |
| BR6-03 | 同一任务教师辅助和无教师各跑一次 | 报告分表，主要成绩只用无教师结果 |
| BR6-04 | 选择 Harbor 缺依赖或缺 review 能力 | 预检失败；AutomationBench 用户不受影响 |
| BR6-05 | 原生 Harbor verifier 与适配后结果 | 对同任务一致；环境故障不变成成功/假业务失败 |
| BR6-06 | 离线 report 面对缺测与成本 unknown | 无网络调用、如实保留，不能填 0 |
| BR6-07 | 仅训练 actor，没有 controller 对照 | 可报告 C1；C3 为 not_run，不自动沿用 evolved/prompt_best 映射 |
| BR6-08 | 默认 published_reference 模式只有外部榜单材料 | 不运行 frontier、不伪造 TaskResult、不阻断 C1；C2 参考与同题结论区分 |
| BR6-09 | cycle 初始无 source/plan，仅有完整环境/preset/资源 | 自动产出验证方法与合法 plan；无需人工填 ref/逐轮选配方 |
| BR6-10 | cycle 在安装前/后中断再启动 | 同一活动预算和实验；已完成 phase 不重付费，回用事实不伪造 |
| BR6-11 | 调用 manual 或 report | manual 来源准确，report 无模型请求；两者不能被当作自动闭环完成 |
| BR6-12 | worker 返回 collect 或 none | collect 自动分派/续跑；none 结束该段但不吞掉获准 research slot |
| BR6-13 | source validated、新 h baseline 和正式安装均存在 | 三种身份/状态分开报告；train 验证与新 h 基线都不计生产晋升 |
| BR6-14 | harness 首版 preset 无 trainer/teacher/Tinker 配置 | freeze/smoke/cycle/导出可推进，训练请求哨兵不触发；报告实际包行为及未运行的训练项 |
| BR6-15 | 首版 harness preset 设 max_draft_repairs/max_failure_notes/失败摘要条目长度上限；R 请求更多草稿修订或笔记 | 数值由 preset 固定并写入 freeze 摘要，freeze 后 R 不能提高，修订/笔记调用不超上限；示例 actor 为 GLM-5.3 |
| BR6-16 | grading.jsonl 同时有 train/dev/final 行 | 默认读取只返回 train 行；请求 dev 或 final 返回空；R 上下文中找不到 dev/final 明细 |
| BR6-17 | K 的 components 含代码种类，或当前包运行含代码，而 freeze/smoke 时无合格隔离 worker；另一次当前包的组件种类不在 K 内 | 两者都在付费前拒绝启动并说明原因；不自动改用文本 preset |

扩展 `test_bench_phase.py`、`test_benchmark.py`、`test_report.py`；Harbor 使用 fake trial 默认测试，真实环境 smoke 单列。
