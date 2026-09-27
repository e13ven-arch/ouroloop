# M6：研究策略的比较、安装与递归回用

> 待实现，对应路线图 M7。新增 `research/campaign.py`，复用 worker round、task evaluator、预算与安装。不新建元训练平台。

## 1. 需要证明的对象

研究候选不是一条更好看的研究 prompt。它必须在相同起点和预算下，通过实际改进任务策略，产生可测的质量/成本效果。R 可以是诊断、检索、提案或辅助检查的声明式配置；不要求训练第二个模型。

工程证据是 `R0 自动提出 R1 → 固定比较 → 安装 R1 → R1 实际提出并评测 W2`。手工给定 R1 可验证接口，但不能标自动发现。C4 进一步要求独立比较支持研究收益；一条工程链和若干安装事件不能替代 C4。

## 2. CampaignPlan 与数据权限

`CampaignPlan` 必填：`id, phase(research_dev|final), core_ref, r_control_ref, r_candidate_ref, campaign_pairs, arm_budget, worker_round_limit, worker_gate_plan_ref, research_gate_plan, retry_policy, selection_rule, finalization_ref?`。

每个 `CampaignPair` 包含 `pair_id, independence_group, initial_w_ref, initial_experience_ref, inner_train_groups, inner_dev_groups, outer_groups, split_ref, repeats, seed_schedule`。所有分组预先冻结；每支的 W/E 起点、基础研究和任务模型、允许训练规模、工具、总额度相同，只有 R 不同。重复相同起点/任务池只增加重复次数，不增加独立 campaign 数。

| 数据/资源 | 分支 worker | 分支 R | 外部 evaluator | 主线后续研究 |
|---|---|---|---|---|
| inner train | 当时任务/合法工具 | 轨迹与允许反馈 | 可读 | 在原全局边界内可读 |
| inner dev | 当时任务/合法工具；无在线分数 | 已完成 dev 比较结果 | 可读 | 在原全局边界内可读 |
| outer research_dev | 仅 evaluator 执行任务时提供合法输入 | 不可读，不参与内层选 W | oracle/结果 | 仅开发汇总及允许诊断；因此属于研究开发数据 |
| outer final | 同上 | 不可读 | 锁定后评分 | 最终报告后不可再回流本实验调优 |
| 另一 arm 新经验/active 指针 | 不可读 | 不可读 | 汇总权限 | 比较结束后可按数据边界归档 |

先验证 W/E/训练数据的来源中没有 outer_groups；记录 R 的来源也不能含 final 任务或其衍生证据。任务曾进入历史经验就不能通过重新命名变成 held-out。全局 test 永不转成 inner train/dev；只有尚未被使用的外层集合才能支持独立最终结论。

首版可从现有 train/dev 的未使用家族中预留 research_dev；若历史 W/E 已见这些家族，应重建干净起点或仅标工程实验。100 个公开任务未必足以支持多个独立 campaign，数据不足时不以更多 seed 冒充独立性。可以先实现 M7，再同时锁定 C1～C4 最终协议；若 M6 的测试已揭晓，后续调优的 R 需要另一个干净外层池。

## 3. 执行算法

```text
validate(plan, policy lineage, disjoint groups, budget, frozen K)
for each predeclared pair/repeat:
    create isolated arm A(R0,W0,E0), arm B(R1,W0,E0)
    for each arm in predeclared order:
        reserve fixed child budget
        run the existing worker loop, research slots disabled
        choose W* using only inner dev and frozen selection_rule
        seal W*, all inner evidence, usage and stop reason
    evaluate both W* on the same outer task/repeat plan
aggregate paired campaign outcomes
fixed research gate -> EvaluationReceipt(profile=research, subject=R1)
```

`selection_rule` 默认取该 arm 最后一个实际 installed 的 W；无接受候选则 W*=W0。所有晋升都须通过同一 inner task gate，不按 outer 分数倒选中间冠军。可以提前声明另一开发选择规则，但不能在看到 outer 后改。

隔离 arm 使用独立工作目录/active/经验索引和 scope 授权，模型只接序列化 Context；只读 artifact 可共享。首版候选无可执行代码、无 shell/任意路径工具，因此不依赖“路径不同就安全”的假设。若未来开放策略代码，必须先实现真正进程和访问隔离，不能直接沿用此内存级信任边界。

外层任务评测费用按对称固定协议计入每支比较的总成本，并另外分列；研究改进成本含采集、研究请求、失败提案、训练、screen、dev、安装探针。生成 R1 自身的费用属于总体 RSI 活动成本，另列一次，不偷偷分配给某个 arm，也不能在成本回收结论里遗漏。

## 4. 评价、裁决与声明

研究 gate 复用 M3 的 integrity、quality/economy 原则，但统计单位是 campaign independence_group：先汇总同一任务池的 repeats，再比较 R1/R0 产出的 W* 的 outer 完整通过率。不同 campaign 重用任务或共同数据导致相关时合并 independence_group，不能独立 bootstrap；样本不够则 exploratory/inconclusive。

quality：同额度下 outer 任务质量按预设开发门达到增益与回归门槛，可接受 R1 作开发版本；区间及独立组数照报，不推导显著性。economy：按预定 inner dev 质量阈值停止，到达时间/累计成本及最终 outer 非劣性共同判断。未达到阈值的 arm 记 `not_reached` 和全部支出；不能只比较达标者平均费用，或把它的成本写零。首版优先 quality 模式，避免为成本到达时间再建复杂搜索。

正式 C4 在 R0/R1、所有起点、任务池、预算、重复次数锁定后运行 final CampaignPlan；结果只报告、不挑 R。按预先指定的方向解释配对差值和区间，不成立就报告未证实。仅有一个 campaign 池的重复结果可以展示条件内稳定性，不能声称跨场景普遍研究能力提高。

已接受 R1 的安装走 M3，只更新 R。分支产生的 W* 是研究评测产物，不自动晋升主线；若要采用需主线 task plan 重新比较和安装。安装后下一 worker round 必须加载 R1，trace 完整穿过 diagnose/retrieve/propose/preflight 中实际修改的位置，W2 来源引用 R1。R1 不得修改固定 gate 或在 campaign 内更新自身。

## 5. 用例与验证

| ID | 场景 | 预期 |
|---|---|---|
| RC-01 | R1 只是在提案前检查 view 覆盖 | 分支确实执行该检查；可减少无效训练，效果由 W* outer 结果/总成本回答 |
| RC-02 | 一支失败、无候选或预算耗尽 | 输出 W0/最后已安装 W 和全部费用；不能丢弃该 campaign |
| RC-03 | R1 试读另一支结果、outer oracle 或主线 active | 能力接口拒绝，记录 invalid/权限事件；两支 E 独立 |
| RC-04 | 初始模型训练来源含 outer family | 正式计划拒绝；不能靠 task 改名通过 |
| RC-05 | 同一任务池重复 10 个 seeds | independence_group 仍为一个，不报告 10 个独立 campaigns |
| RC-06 | 两支同额度，但 B 的重试/训练超预算 | M5 阻止；不能给 B 隐式追加额度 |
| RC-07 | R1 被接受但安装失败；随后重试成功 | 首次主线 R0 不变，成功后下一轮才用 R1，主线 W 不变 |
| RC-08 | 分支 W* 很好，主线已有另一 W | 分支不更新主线；采用必须另走主线任务比较 |
| RC-09 | final 分数不支持 C4，但工程链跑通 | 报告工程完成、C4 未证实；不挑 seed 重写结论 |
| RC-10 | R1 改动由人手写，循环正常运行 | 标 manual_seed；工程测试通过，自动递归发现主张不通过 |

离线夹具必须能区分 R0/R1 的真实请求/步骤，不能只按版本名字返回更高分。真实 C4 所需独立数据不足不阻碍交付框架，但阻碍效果结论。

## 6. 实现状态（2026-09-27）

已实现：`ouroloop/research/campaign.py`（`CampaignPair` 构造时拒绝内外层重叠、`CampaignPlan`、`validate` 的血缘与独立组检查、`arm_workspace`、`ArmOutcome`、`select_w`、`run_campaign`、`judge_research` 以 independence_group 为统计单位）、`ouroloop/bench.py` 的 `_run_campaign_arm`（每支独立工作区、独立 active.json 与经验、固定子预算、research slots 关闭）、`_evaluate_outer`（同一外层任务与重复由评测者运行）、`research` 阶段与 `research_slot`（R0 提出 R1 → 冻结 campaign → 比较 → 安装 R）。

分支 W* 只经 `copy_closure` 回到评测者的对象库，主线 W 不变；`discovery` 字段区分 automatic/scripted/manual_seed 并进入 receipt。

验收：`python -m pytest tests/test_campaign.py -q` → 13 passed，覆盖 RC-01～RC-10；完整链条见 `tests/test_rsi_integration.py::test_it03_*`。

已知缺口：内层任务改进只跑到 `worker_round_limit`，没有经济模式（按达到质量阈值的时间/成本比较）；arm 隔离是目录与 scope 级，不是进程隔离，开放生成代码前必须升级；C4 的独立数据不足，fixture 的 1 个 independence group 只能出探索性结论。
