# M5：统一执行、预算与恢复

> 首版在冻结前加入能力包草稿检查（同一 operation 日志），并将构建/隔离执行加入现有一次候选执行；第二版再加入蒸馏子操作。复用 `evolve/execution.py`、jobs、计量 provider 和事件账，明确恢复已确认工作，不能重复未知付费或副作用。

## 1. 状态机与子操作

deployment 保留已有 drafted → admitted → building → screening（可选）→ evaluating → evaluated → accepted → installing → installed；其他终态保持 rejected/inconclusive/invalid/screened_out/installation_failed/stale_parent/cancelled。installation_failed 仅可重试同一已接受 artifact 的安装。

首版 harness_edit 的包检查为 package_check → isolated_probe → package_test，先在冻结前对草稿包运行（草稿检查）；building 按相同输入摘要引用已确认的草稿检查结果，不重复运行；应用 patch/形成草稿 bundle 属于 M0/M4 的纯规范化，检查结果不得事后改写包。M4 把 patch 规范化成草稿包后，M5 在同一 operation 日志中运行草稿检查：operation 记录 activity、分支 scope、轮次与修订序号；检查输入摘要覆盖草稿 bundle/manifest/test bundle、固定 API/image、测试输入与限额。只含 skills/memory 的草稿只做结构/路径/manifest 检查，不需要 isolated_probe。building 子操作的 operation key 仍包含 experiment、bundle/manifest、固定 API/image、测试输入与限额；复用已确认草稿检查只比较上述输入摘要（不含 experiment）。每份草稿（初稿与每次修订）检查前先做组件种类与字面量检查：种类不在 K 内或包文本含 train 断言 params/任务特定标识时为结构错误（回传 R、占用一次修订）。含代码（M0 §2 检查判定）的草稿需要隔离 worker；K 允许而 worker 不可用时为基础设施故障，按唯一重试策略处理，仍不可用则本轮 `infrastructure_failed` 并暂停活动（M0 §3），不消耗修订次数、不回传 R，不收窄组件种类（M1 §1）。isolated_probe 与 package_test 只在合成 fixture 世界或仅由 train 任务构造的世界中运行，从不绑定 dev/final 任务；回传 R 的检查输出有长度上限并去除任务数据，fixture 身份计入检查输入摘要。

草稿检查失败时，只把检查输出回传 R，不含任何任务结果或评分；R 可修订，修订次数上限 `max_draft_repairs` 由 preset/K 固定（默认 3，初稿之后最多修订 3 次、最多 4 份草稿），R 不能提高。每次修订形成新草稿包 ref，草稿与检查逐次记录并计费，重启不重置修订计数。只有检查通过的草稿才冻结进 Experiment；次数用尽则本轮以无效提案结束（RoundResult `outcome=invalid`，M0 §3）并计入 schedule 轮次与失败上限，不冻结 Experiment。冻结后源码不变；building 按相同输入摘要复用已确认的草稿检查，不重复运行。冻结后仍出现的路径/接口/候选代码错误判 invalid；隔离设施故障独立记录，不能卸载包继续评测：building/evaluating 中 worker 不可用同样按唯一重试策略，仍不可用则实验记 infrastructure_failed（评测侧 inconclusive，不当候选失败）并暂停活动。修复只在冻结前、同一轮内有界进行，不另开 proposal，也不占用额外 schedule 轮次；冻结实验内不更换文件。

代码检查、候选自写测试与任务评测都不在控制面 import 包。M1 提供固定隔离 worker（只含 skills/memory 的包不启动 worker），M5 用现有 operation/run/deadline 管理生命周期、资源输出、取消与清理；不新增代码编排服务。已确认检查只在全部输入摘要一致时复用，已存在文件不等于检查完成。未知工具副作用按下节核对，不能因 worker 重启重放复合工具。

第二版 distill_actor 在 building 内依次记录 collect/dataset/train/probe 子操作；沿用 experiment + step + 冻结 input refs 的身份与日志。子操作结果落盘后才能推进，不新增顶层训练状态机或让 collect 自行发布。

v3 `purpose=source_validation` 复用同一 executor 的 build/evaluate：drafted → admitted → building → evaluating → evaluated → **validated**。只新增 validated 这个非发布终态；校验/执行故障仍走 invalid/inconclusive，源验证未达门槛走 rejected。evaluate 返回源验证 artifact，结果为 passed 时写 SourceWorkflow 并结束，receipt/installation 为空；不得进入 accepted/installing。可选 screen 仅用于便宜的预检，不代替源验证。旧 v2/deployment 状态解释不变。

build/evaluate operation 身份包含 purpose、parent、execution base 和冻结输入；恢复时按原 purpose 加载 source 结果或正式 receipt，不能把 validated 解释成待安装。相同内容的纯构建 artifact 可复用，但改变用途/比较条件必须新建 experiment 和评测操作。

harness preparation、首版草稿检查/修复、M8 教学适配和补采使用同一 operation 日志：输入先冻结、权限/预算先检查、产物落盘后才能生成依赖它的 proposal。harness preparation 记录兼容检查、基线 release/permit、baseline attempts 和结果；期间不移动 active。材料准备不是第二实验状态机，也不能绕过 activity schedule 隐藏无限搜索。

补采由持久化 CollectionRequest 驱动，operation key 包含 activity、分支 scope、请求 ref、冻结任务/attempt 身份；事件记录请求、pending、已完成 refs、计数和后续 round 的关联。恢复优先完成/核对 pending，再用实际结果重建 context。只复用已确认数据，未知 API/副作用仍按下节处理；不重置补采上限，不重跑完成批次。

collect 可引用已有完整轨迹；dataset 身份固定选择规则/投影；train 身份固定数据/配方/底座；probe 固定实际 artifact/runtime。任何上游输入变化生成新操作，不覆盖旧结果。同一 plan 的未完成采集只补计划允许的缺口；不能挑最好轨迹无限重试。

## 2. 预算所有权

一个 activity 一个预算根；源工作流实验、材料适配、collect/train/eval、研究提案、campaign 两支及外层验证都用其子额度。计入 actor/R/teacher API、训练 tokens/时间、环境费用、失败与重试。子额度是限额不是额外消费，实际叶子操作只收费一次；不得因开始研究比较另开同额根。

首版还计入逐题失败笔记与汇总的 R 调用、每份草稿的检查及 R 修订调用（含未通过的草稿）、代码生成、隔离构建/测试、worker 资源时间及 broker 内部的每次基础工具调用；不能只计复合工具外层调用。笔记是 train 血缘 artifact，每轮笔记数上限 `max_failure_notes` 与修订上限 `max_draft_repairs` 由 preset/K 固定，R 不能提高；已落盘笔记只在完整笔记身份（M4 §1：渲染后的笔记请求摘要与模型/推理配置）一致时复用，已确认检查在全部输入摘要一致时复用，均不重复收费；max_failure_notes 限制每轮汇总调用读取的笔记数（缓存加新生成）。诊断笔记、草稿检查/修复与构建前均预留正式 gate/回归/安装探针及已安排的 R 比较额度，包代码不能改 timeout/call cap 或另开收费 client。第二版 teacher/训练费用在相应方法启用后计入，不因保留设计就要求首版预留训练账户额度。

按用途预留验证额度：deployment 预留正式 gate 和安装探针；source_validation 预留冻结的源验证，不要求预留并执行无关生产 gate；补采预留该批采集及下一次提案/准入；换 h 预留兼容探针、新基线及所计划后续实验的最低验证。各预留属于同一总根；source 的轻量验证不豁免活动级已冻结的后续发布/递归验证保留额度，不能把钱全花在源搜索、教师或训练。campaign 的外测额度（含一次补跑）冻结为 plan 的 `outer_budget`，是外测 scope 的上限；它在公共测量与两支开跑前以不结算的预留占住，按上界计入每一级直到根，其余步骤花不到；外测开始时释放给外测尝试，外测不运行时退回活动；重启找回原预留，不重复占用。两支各以份额为上限，但都从根花钱：公共测量之后、两支开跑之前，两支的份额同样以不结算的预留占住，各支开跑时释放给自己，后跑的一支因此不会被前面的花费饿死；占不住时两支都不运行，比较判 inconclusive。重启找回这些预留，已跑过的支不再检查。预算维度和计价版本固定：调用/tokens/train_jobs/training_seconds/环境时间/费用按实际后端能力选。未知成本不能算 0，也不能声称严格 USD 限额已保证。

外部收费前 reserve；返回后按实际 usage settle。Tinker 需分别记录训练和 sampling 的计价维度，不能直接用聊天 API 输入/输出价格计算梯度训练。后端提供可执行上界时才能使用严格美元/时间模式；否则启动前要求选择可执行的调用/token/job 限额并报告费用限制，不能静默降级。

本地 CPU/GPU 资源也记录时间/估算依据。教师无外部副作用的文件读取无需假造 API 费用，但读取权限和工具次数受 collect limits 约束。实际费用超上界记 budget_breach 并停止后续付费；无法撤回已发生账单。

## 3. 重试与不确定结果

仅一层负责外部重试。禁用 SDK 隐式重试，或将每次尝试显式计入预算；Harbor 负责它的 trial 内环境恢复时，ouroloop 不再嵌套重试同一动作。供应商幂等只有确实受支持才使用，本地 journal 不等于外部 exactly-once。

响应丢失、进程死亡或提交后未保存 ID：保留 open/unknown reservation 的最坏占用，不直接释放；按 request/client/job/checkpoint ID 核对。确认未执行可重试，确认已执行复用，不能确定则停候选并标 inconclusive，等待可核对事实。新请求必有新预留，不能把相同 operation ID 当作免费重放。

Tinker optim_step 若状态不明，不能重复 apply。只有已确认训练 checkpoint、optimizer 状态及数据游标可恢复时继续；否则终止该候选或另建有明确预算的新训练，历史费用保留。Harbor 是否可 attach/resume 由能力声明决定，不能默认存在稳定恢复 API。

首版的暂停与结束规则（2026-10-02）按故障在哪一层被处理来划分。

**actor 单次尝试内**（基线、测量、补采、gate 两臂、公共测量与外测中的每次尝试）：
- 账号类错误（余额、配额、被拒的 key）上抛，活动以 repair_runtime 暂停。
- 其余 ProviderError，包括计费层重试用尽的：
  - 记该次尝试 invalid（failure_class=infrastructure），按冻结的 retry_policy 补跑一次；仍缺则所属比较判 inconclusive，不暂停活动。
  - 连续的基础设施失败轮由 `max_infrastructure_failures` 升级为暂停。
- 尝试内的账本预算拒绝：记 budget_exhausted，所属比较判 inconclusive。

**尝试之外的调用**（逐题笔记、汇总、修订、研究位提案，以及 collect、续跑、基线、研究位、公共测量与外测的编排步骤）：
- 触发暂停的故障：账号类错误、研究回复在输出上限处被截断（ReplyTruncated）、计费层重试用尽后的 ProviderError。
- 暂停时：
  - 写入 paused 及原因，不以未捕获的异常退出；
  - 已结算的费用保留，open 预留按上文处理；
  - 不消耗轮次、修订次数或失败额度。
- 排除问题后续跑：从记录的状态继续，不重付已确认完成的尝试。

**账本预算（BudgetError）分两类：**
- 额度耗尽：reserve 会超出某级上限，或 budget_breach 之后 scope 已停止。这不是故障，是有界结束：
  - 子 scope（campaign 的某一支、外测预留）耗尽：只结束该子步骤，所属比较判 inconclusive，活动继续。
  - 研究位开始时根覆盖不了外测预留与两支份额（由 cycle 自己的研究员作答时，再加 R 这一次调用）：按活动根耗尽处理，研究位不问 R。
  - 活动根耗尽：活动以 budget_exhausted 结束并出报告，不写 paused。
  - 上面列出的编排步骤遇到额度耗尽时，同样按此记录，不以异常退出。
- 配置不一致：scope 已以其他限额存在、scope 不存在、未知维度。按下文“续跑的冻结输入”暂停并写明原因；能在 freeze 时发现的，在 freeze 时拒绝启动。

供应商账号的余额或配额耗尽属于账号类错误，充值后续跑即可，与账本预算耗尽不是一回事。

**续跑的冻结输入（2026-10-02）：**
- 续跑一个未完成的实验时，评测计划与预算 scope 只取 Experiment 冻结的 `eval_template_ref` 与 `budget_scope`。
- 当前活动的计划或 scope 与之不一致时不续跑：实验保持打开并记录原因，活动暂停，由人决定关闭该实验还是恢复原计划。不得用新计划或新预算完成旧实验。
- 筛选与评测的 control、permit 与候选 release，都建在冻结的 `parent_release` 上。
- 安装仍按冻结的 `parent_release` 做 CAS；active 已移动时记 stale_parent，不暂停。
- 首版活动内不重新冻结 task plan、schedule、预算或计价（[ROADMAP §3](../ROADMAP.md) 首个真实活动协议）。

## 4. episode 与副作用恢复

采集记录草稿 → review → accepted → execution 各事实；写入采用已有原子对象与追加事件机制。不对失败的草稿执行任何工具。动作执行后、结果落盘前崩溃形成不确定副作用：优先查询环境真实状态，不能直接重发营销通知等操作。

首版 benchmark 可将未完成 episode 标 incomplete，并在全新可重建世界中按冻结 retry policy 补一次；旧轨迹和费用保留，不进入 SFT。真实业务环境不能 reset 时，必须有工具幂等/查询契约或人工处理；没有该能力就停止，不把 benchmark 的重建能力承诺给生产。

cancel 停新付费请求并尽力取消后台作业；取消仍可能收费，保留对账。已完成 artifact 核验后复用；已评测配对只补缺项；accepted 未安装继续同一次安装，CAS 已提交以实际 pointer 为准。

## 5. 当前已知差距与用例

当前已有逐调用预算、未知占用、活动预算根和实验续跑。待补：自动 wall time 计量、SDK 隐式重试覆盖、外部请求对账，以及教师/Tinker/Harbor 新边界。不能把这些设计写成当前严格保证。

§3 中 2026-10-02 写定的规则，已在阶段 1 与阶段 3 实现（2026-10-02）：
- 尝试之外的故障由 `research/loop.py` 的 `halt_of` 统一分类：
  - worker 轮由 `run_worker_rounds` 落在 repair_runtime 或 budget_exhausted；
  - 续跑未完成的实验时，由 `resume_unfinished` 按同一规则停下；
  - cycle 的每一步（基线、段、研究位）把结局与原因写进 `rsi/cycle.json` 该步的 outcome/reason 以及活动的 status。
- 账本区分额度耗尽（`BudgetExhausted`，有界结束）、预算不符（`BudgetMismatch`，暂停）与其他账本误用（直接上抛，不当作暂停）。
- 计费层重试用尽后，供应商抛出的任何异常都以 ProviderError 离开计费层，带上是否可重试与 HTTP 状态，所以 SDK 自己的连接或超时错误同样按暂停处理。
- 冻结的 task plan 带全部单次尝试上限；冻结后再改 `[bench.budget]` 不影响任何已冻结的比较。
- 续跑比对 Experiment 冻结的 `eval_template_ref` 与 `budget_scope`：不一致就不续跑，记 `resume_refused` 事件并暂停。筛选与评测建在冻结的 parent 上。

尚未覆盖：
- 单独运行的 `--phase baseline` 与 `--phase research`，遇到尝试之外的故障时仍以异常退出（单独运行的 `--phase improve` 已按规则停下）。
- 尝试内的预算不符，仍按 budget_exhausted 记录。

首版按 [ROADMAP §2](../ROADMAP.md) 阶段补：阶段 A 补文本草稿检查（结构/路径/manifest）与有界修订、笔记调用计量；阶段 B 把同一修复循环扩展到 isolated_probe/package_test，并补包 worker 生命周期/资源与 broker 内部工具调用计量。教师/Tinker 的新恢复路径归第二版，已有旧 job/serving 兼容不删除。

| ID | 条件 | 预期 |
|---|---|---|
| EX6-01 | 工作流搜索+适配+actor/teacher+训练+campaign+外评记账 | 共享一个根，叶子无重复累计；子额度不增加总可花预算 |
| EX6-02 | 钱够采集训练但不够最低评测 | admission 不启动付费构建 |
| EX6-03 | teacher 或 optim_step 响应丢失 | 额度继续占用；核对或 inconclusive，不盲重试 |
| EX6-04 | collect/dataset/train/probe 各完成后崩溃 | 已确认产物按身份复用；只恢复缺失部分 |
| EX6-05 | 工具已执行但回执未保存 | 不重复副作用；隔离世界按冻结策略重建，旧 episode incomplete |
| EX6-06 | SDK 自动重试或 Harbor 内外双重重试 | 启动配置阻止，或每次外部收费可追溯 |
| EX6-07 | 取消恰逢安装提交 | 读取安装事实，不把已安装记成未安装 |
| EX6-08 | source validated 后重启，installer 设失败哨兵 | 恢复已完成源产物，零安装调用；不进入 accepted/installing |
| EX6-09 | 补采已返回数据、后续提案前崩溃 | 已完成批次不重跑，计数和证据保留，下一轮从新 context 继续 |
| EX6-10 | 新 h 基线部分完成后中断 | 保留 preparation/parent，补缺项且共用原预算，不将 B 设为 active |
| EX6-11 | 包测试完成后重启，或复合工具执行后 worker 失联 | 同输入复用已确认检查；未知工具副作用先核对、不重放；计数/费用/资源占用不清零 |
| EX6-12 | 草稿检查失败、R 修订后通过；期间重启，随后冻结并进入 building | 每份草稿、检查与修订调用逐次记录计费，修订计数不重置；重启复用已确认检查；building 按输入摘要复用，不重跑 |
| EX6-13 | 含代码草稿检查时 worker 失联；另一次 building/evaluating 中失联 | 唯一重试后 infrastructure_failed，修订计数与失败上限不变，已发生费用保留，活动暂停；恢复后以原草稿 ref/修订序号或原 operation 续跑，不重复收费，不 in-process 执行 |

复用 `test_execution.py`、`test_paid_run_safety.py`、`test_jobs.py`；使用 fake provider/job/clock 故障注入，不用真账单证明预算正确。
