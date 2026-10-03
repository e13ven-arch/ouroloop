# M6：改进方法比较与递归回用

> 首版必需。目标：把更新后的改进方法实际用于后续改进，并独立检验它产生的任务结果。复用 `research/campaign.py` 和既有安装机制；不创建新元研究系统，不要求专用研究模型训练。

## 1. 比较什么

首条递归实现采用现有 R 策略路径。注册的 R 目标有 `research.diagnose`、`research.retrieve`、`research.propose`、`research.preflight` 四项。

首版研究位中，真实 R 只对 diagnose 或 propose 其中一段提出完整的新提示（`ouroloop/research/basic.py` 的 `PolicyResearcher.propose_research`）；改哪一段由经验投影决定，不预先指定。retrieve 与 preflight 保留注册，供 scripted 或手工路径做工程验证，首版不作为真实的自动候选，等真实结果显示需要时再开放（[DESIGN §7](../DESIGN.md)）。

首版例：R0 遇到失败就新增相似工具；R1 先检查旧工具是否加载/调用，再选择修改描述、修中间件或合并组件。改变的是改进方法；不能以 R 自评“更合理”接受，必须看相同起点下实际产生的 W*。第二版再扩展训练选择。

首版不更新研究 provider 权重或自动接入供应商。actor、package API/隔离镜像/依赖、基础工具权限、包大小/工具/hook/测试资源上限、K 允许的组件种类、目标宿主与方法菜单共同冻结；两支可生成不同包代码，但不能多拿工具权限/先验数据/预算。第二版再冻结共同 teacher/trainer 目录。

## 2. 冻结的比较计划

输入复用 campaign plan：W0（含同一初始包）、R0/R1、E0、K、获准宿主/package API/镜像、train/内层 dev/外层 research_dev 家族、round/attempt/patience/collection-batch/构建限额、每轮笔记数与草稿修订上限和共同费用上界。每支资源上限相同，总支出仍由主活动预算根约束；外测额度（含一次补跑）冻结为 plan 的 `outer_budget`，既是外测的上限，也在公共测量与两支开跑前从根预留；安装额度预留。第二版追加 source/recipe/训练身份，不为此另建 campaign。

两支只提出 worker 候选，禁用 research slots，避免递归创建第三层 campaign。每支 R 在整个分支内冻结。各自积累 source、teacher 数据、adapter 和经验，禁止交叉读取；共享只限起始 E0 中预声明且身份完全相同的内容：两支的经验日志以配对冻结的 E0 原样开头，第一轮读到同一个 experience_ref；E0 引用的对象不复制进分支。首版 W0 的测量在执行身份一致时作为 campaign 级共同输入；第一轮逐题笔记的任务选择在 campaign scope 按同一规则做一次，两支 diagnose 的完整笔记身份一致时笔记也只生成一次、计入 campaign 共同预留并记为共同输入，不一致时各支在同一组选中任务上、同一上限内各自生成；此后分支内产生的笔记不跨支（笔记身份与上限见 [M4 §1](04_RESEARCH_LOOP.md)）。模型 store 可共享存储介质，但访问范围和可变指针独立。

两支使用相同的 M4 结构化补采驱动；需要数据时在各自 train scope 自动采集再提案，不能让一支返回 collect 就提前判输。各支单独计补采与新 h 基线费用，总根不变。source validated 和仅准备完成的 baseline 都不算 W 晋升；只有 deployment gate 通过的候选才可成为分支 W*。换 h 必须走 M3 preparation，仍在共同许可集合内。

首版两支不训练新模型，菜单为 policy_edit/harness_edit；分别计逐题笔记与汇总、草稿检查/修订、代码生成、构建/隔离运行和工具内部调用。第二版一支选 prompt、一支选蒸馏是合法差异，但共同方法集合/资源仍要先冻结，不能把该未来用例列作首版依赖。

## 3. 执行与裁决

```text
freeze campaign; set aside the outer verification (one refill)
prepare the common inputs; set aside both branches' equal shares
for R in [R0, R1]:
    start isolated scope with the same W0/E0
    execute bounded worker rounds through M4/M5
    keep the last W that passed the branch's inner gate (else W0)
compare the two W* without teacher on untouched research_dev, paired by task
    (both W* the same: no outer run, inconclusive)
emit research EvaluationReceipt; install R1 only if accepted
```

K 所需的隔离 worker 不可用时不启动分支；分支因 infrastructure_failed/repair_runtime 暂停时不视为正常完成，修复后以同一冻结条件续跑至冻结的轮数，无法续跑则 campaign 判 inconclusive，不以已完成轮数不同的两支比较。

外层任务两支内层均未访问；R 不看外层逐任务答案。按首版 preset 的配对规则，外层家族在研究位之前是主线开发家族：W0、R1 与 E0 中的汇总指标都来自覆盖它们的 gate。“未访问”指研究位开始之后，两支不运行它们，也不读取它们的逐题结果；E0 带入的汇总统计计入 research_dev 的使用。复制 R1 与 W 的身份闭包时，主线 gate 的计划与逐题结果会随来源链进入公共与候选支的对象库；这是身份引用，不是可读内容（[M0 §4](00_CONTRACTS.md)），分支里没有读取它们的步骤。它们仍不是 final，最终泛化另测锁定版本。重复选择 R 会逐步使用 research_dev 的统计信息，不能把它当无限可重复的独立 test；报告候选次数和访问历史。

冻结 gain/cost/patience 规则，不能临时挑一支历史最好 W。没有晋升用 W0，不删除失败支。协议错误判 invalid；环境/预算/配对不足判 inconclusive；有效但达不到门槛判 rejected。单支失败无法形成有效配对时不能让另一支自动获胜。

外层比较（2026-10-03）：
- 两支 W* 是同一个 worker ref（通常是两支都没有安装）时不跑外测，判 inconclusive：同一个 W 测两遍，说明不了 R。有多个 pair 时，任一 pair 相同即判 inconclusive，后面的 pair 不再运行。
- 两支在同一组外层任务上各跑一遍。基础设施失败的尝试按外测 TaskEvalPlan 冻结的 retry_policy 补跑一次，原尝试保留，补跑记 supersedes；任务失败、候选缺陷、评分器失败与额度耗尽不补跑。CampaignPlan.retry_policy 不读。
- 裁决按任务配对：同一 pair 内，任务的每次重复在两支都有有效尝试才计入；缺任一即判 inconclusive，不按各支剩下的任务各自求均值。外测上两边都是封存的 W*，候选缺陷与评分器失败也只使配对不全；invalid 只留给协议错误。独立组仍是统计单位。
- 两支等额：公共测量之后、两支开跑之前，两支的份额以不结算的预留占住，各支开跑时释放给自己。占不住时两支都不运行，外测份额退回，判 inconclusive：两支各以份额为上限，却都从根花钱，不预留时后跑的一支（R1）会被前面的花费饿死。重启找回这些预留，已跑过的支不再检查。
- 外测额度冻结为 `outer_budget`：外测 scope 以它为上限，并在公共测量与两支开跑前以不结算的预留占住；外测开始时释放给外测尝试，外测不运行时退回活动；重启找回原预留，不重复占用。外测额度耗尽时，没跑成的尝试使配对不全，判 inconclusive。
- 收据 metrics 记 `tasks`（配对的任务数）、`incomplete`（各 pair 缺的尝试，按失败类别计数）与 `identical_arms`。

报告无教师完整任务结果及总改进费用，不以提案数量、训练 loss 或 R 的评分替代。相同任务多种 seed 不增加独立家族；费用未知时不宣称成本效率改善。

## 4. 安装后怎么证明实际回用

M3 只更新主线 release 的 R，保持主线 W/K。campaign 内 W* 不随方法晋升成为生产冠军；要部署 W* 必须满足主线任务评测/授权。固定核心 K 不被分支实验改动。

下一主线研究轮读取 active R1，`bind_release` 和现有 propose 流程将它用于实际步骤：诊断/提案要记录所用 prompt 内容摘要，检索/辅助预检要记录实际规则配置和执行结果。关联 context_ref、loaded R ref、步骤名、provider 请求摘要、proposal_ref 及后续 Experiment ref。不能只让 report 抄新 R 名称。

只更新一个目标时只要求对应步骤实际改变，其余绑定应保持一致。核心 admission 始终运行；R1 辅助流程不能关掉权限、标签、预算或 gate 检查。新回用记录进入 E，下一轮继续受原 schedule 限制。

## 5. 机制与效果的验收

工程 fixture 必须覆盖“提出 R1 → 两支实际产生 W* → 比较 → 安装 R1 → 后续改进调用真正加载 R1”，并覆盖拒绝和不确定分支。真实实验必须安排有界方法比较；通过才使用 R1，不通过保留 R0。不能为了展示 RSI 绕过 gate。

scripted 成功只证明机制；实际模型提出候选证明自动提案；C4 成立还需要相同起点/预算的下游改进收益。三者分开标。W 的数据增长、版本 ref 变化、R1 被加载都不能单独支持 C4。首版不承诺递归收益或代际单调提高，但这条可执行且可验证的递归路径不能延期删除。

以后可以用训练后的 actor 辅助工作流搜索等方式扩展递归；需要新的实际调用绑定和比较设计。本版不提前实现多种递归后端。

## 6. 用例

| ID | 条件 | 预期 |
|---|---|---|
| RC6-01 | 一支读取另一支新轨迹/adapter | scope 拒绝，共同 E0 仍可用 |
| RC6-02 | R1 选蒸馏，R0 选 prompt | 共用 M4/M5 构建评测安装；两支费用与外评共用总根 |
| RC6-03 | 内层读取 research_dev/final 或开 research slot | 拒绝，无第三层 campaign |
| RC6-04 | 一支没有通过 gate 的 W | 外测 W0；两支都没有（W* 同为 W0）时不跑外测，判 inconclusive；故障造成配对不全则 inconclusive |
| RC6-05 | R1 通过并安装 | 主线 W 不变，下一改进请求实际采用 R1 的变更目标 |
| RC6-06 | 多 seed 同家族或 scripted R | 家族数/来源准确，不冒充真实自动发现或递归收益 |
| RC6-07 | manifest 写 R1，运行仍沿用 R0 配置 | 回用验收失败，不能出 C4 结论 |
| RC6-08 | R1 失败或 budget unknown | 不安装，保留 R0 和两支失败/费用，不强制涨分 |
| RC6-09 | 一支先要求补采/验证源方法，另一支直接提候选 | 共用有界驱动和相同限额；数据/源验证不当作 W 晋升，也不误判该支无提案退出 |
| RC6-10 | 两支生成不同能力包代码，一支请求更高 API/资源权限 | 共用固定 API/镜像和原预算根，越权被拒；包/经验不跨支共享，仅比较正式通过 gate 的 W* |
| RC6-11 | R1 只改 propose；另一次 R1 改了 diagnose；另一次 R1 分支第 2 轮 worker 失联 | 第一种第一轮笔记在 campaign scope 生成一次并记为共同输入；第二种两支在同一组选中任务、同一上限内各自生成；第三种该支暂停并在修复后续跑至冻结轮数，否则 campaign inconclusive |

扩展 `test_campaign.py`、`test_rsi_integration.py`，不为蒸馏另写 runner。
