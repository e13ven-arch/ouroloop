# M6：改进方法比较与递归回用

> 首版必需。目标：把更新后的改进方法实际用于后续改进，并独立检验它产生的任务结果。复用 `research/campaign.py` 和既有安装机制；不创建新元研究系统，不要求专用研究模型训练。
> v0.3（2026-10-04，[DESIGN](../DESIGN.md) v0.7）：两支的内层 gate 用合并对照、确认复测与成本项；内层仍是 dev 冻结顺序的前一半、外层仍是后一半，但内层不得少于 `min_tasks` 25 题，研究位因此只在 dev ≥ 2 × min_tasks（50 题）时开；`campaign.worker_round_limit` 2 → 3；开研究位前先查余量；裁决仍只看下游 W*。考虑过、不做（v0.3）：另立 `research_dev` 外层分区与 `no_outer_pool`、把台账指标写进研究收据（`metrics.ledger.*`），原因见 §2。原因：v0.2 的研究位两支各两轮、内层 gate 12 题，空候选通过率约 1/3，一题就是 8 个点，W* 已在内层 67～80%，两支都装不进任何东西，判 inconclusive，C4 没有证据（[RESULTS](../RESULTS.md)、[ROADMAP §3](../ROADMAP.md) 第 2a 步第 9 项）。本文新增或修改的规则逐条标“（v0.3，2026-10-04）”。

## 1. 比较什么

首条递归实现采用现有 R 策略路径。注册的 R 目标有 `research.diagnose`、`research.retrieve`、`research.propose`、`research.preflight` 四项。

首版研究位中，真实 R 只对 diagnose 或 propose 其中一段提出完整的新提示（`ouroloop/research/basic.py` 的 `PolicyResearcher.propose_research`）；改哪一段由经验投影决定，不预先指定。retrieve 与 preflight 保留注册，供 scripted 或手工路径做工程验证，首版不作为真实的自动候选，等真实结果显示需要时再开放（[DESIGN §7](../DESIGN.md)）。

v0.3（2026-10-04）：R 的候选面不变，仍是这两段文本。变的是 `research.propose` 文本的用途——它现在是 M4 §1（v0.3）编码会话的 system 提示（R 在 worker 内用文件工具编辑包目录副本、产出至多 k 个带声明的候选，不再写 JSON patch），所以 R1 改 propose 即改会话指令；研究位的格式声明、读不出规则与准入/构建的 stage 记法沿用 [M4 §1](04_RESEARCH_LOOP.md)。研究位的输入在经验投影之外增加 M4 §5（v0.3）台账的 train 受限投影（每个历史候选的声明、筛选结果、裁决与 fixed/regressed/still_failing 题数），因为 R 要改的正是“怎样提出候选”，台账是它唯一可核对的依据。会话工具、上限、`read_trajectory` 与筛选都属于 K，R1 不能改。

首版例：R0 遇到失败就新增相似工具；R1 先检查旧工具是否加载/调用，再选择修改描述、修中间件或合并组件。改变的是改进方法；不能以 R 自评“更合理”接受，必须看相同起点下实际产生的 W*。第二版再扩展训练选择。

首版不更新研究 provider 权重或自动接入供应商。actor、package API/隔离镜像/依赖、基础工具权限、包大小/工具/hook/测试资源上限、K 允许的组件种类、目标宿主与方法菜单共同冻结；两支可生成不同包代码，但不能多拿工具权限/先验数据/预算。第二版再冻结共同 teacher/trainer 目录。

## 2. 冻结的比较计划

输入复用 campaign plan：W0（含同一初始包）、R0/R1、E0、K、获准宿主/package API/镜像、train/内层 dev（前一半）/外层 dev（后一半）家族、round/attempt/patience/collection-batch/构建限额、每轮笔记数与草稿修订上限和共同费用上界。每支资源上限相同，总支出仍由主活动预算根约束；外测额度（含一次补跑）冻结为 plan 的 `outer_budget`，既是外测的上限，也在公共测量与两支开跑前从根预留；安装额度预留。第二版追加 source/recipe/训练身份，不为此另建 campaign。

两支只提出 worker 候选，禁用 research slots，避免递归创建第三层 campaign。每支 R 在整个分支内冻结。各自积累 source、teacher 数据、adapter 和经验，禁止交叉读取；共享只限起始 E0 中预声明且身份完全相同的内容：两支的经验日志以配对冻结的 E0 原样开头，第一轮读到同一个 experience_ref；E0 引用的对象不复制进分支。首版 W0 的测量在执行身份一致时作为 campaign 级共同输入；第一轮逐题笔记的任务选择在 campaign scope 按同一规则做一次，两支 diagnose 的完整笔记身份一致时笔记也只生成一次、计入 campaign 共同预留并记为共同输入，不一致时各支在同一组选中任务上、同一上限内各自生成；此后分支内产生的笔记不跨支（笔记身份与上限见 [M4 §1](04_RESEARCH_LOOP.md)）。模型 store 可共享存储介质，但访问范围和可变指针独立。

两支使用相同的 M4 结构化补采驱动；需要数据时在各自 train scope 自动采集再提案，不能让一支返回 collect 就提前判输。各支单独计补采与新 h 基线费用，总根不变。source validated 和仅准备完成的 baseline 都不算 W 晋升；只有 deployment gate 通过的候选才可成为分支 W*。换 h 必须走 M3 preparation，仍在共同许可集合内。

首版两支不训练新模型，菜单为 policy_edit/harness_edit；分别计逐题笔记与汇总、草稿检查/修订、代码生成、构建/隔离运行和工具内部调用。第二版一支选 prompt、一支选蒸馏是合法差异，但共同方法集合/资源仍要先冻结，不能把该未来用例列作首版依赖。

### 内层 gate、外层池与余量（v0.3，2026-10-04）

- **内层 gate 仍是 dev 的前一半，但不得少于 25 题。** preset 把 dev 家族按冻结顺序对半分，前一半给两支内层 gate、后一半给外层比较（`ouroloop/bench.py:1043`、`cycle_pairs` 的 `dev_families[:half]`/`[half:]`，`:2644-2672`）；v0.3 不改这个划分，改的是下限：内层 gate 计划受 `min_tasks` 25 约束（M3 §2.1(f)（v0.3）），所以研究位只在 dev ≥ 2 × `min_tasks`（50 题）时开。dev 不足 50 题的活动——包括只含 Marketing 的活动（dev 25 题）——不开研究位：不问 R、不预留两支与外测份额、不开两支，研究位记 `no_slot_pool`，活动按 schedule 进入下一段，报告的 C4 为 not_run 并写明原因（[M7 §4](07_BENCH_REPORT.md)）。多领域活动的 dev 是各领域合并的 dev（M7 §2（v0.3）），前后两半在合并后的冻结顺序上切。（v0.3，2026-10-05 改，实现 `bench.halve_dev`：）按合并顺序直接切会让前两个领域整个落在内层、后两个整个落在外层，两半各自只代表一部分领域；所以多领域时改为**在每个领域内**按其冻结顺序切，前一部分进内层、其余进外层，题数为奇数的领域在列表的偶数位把多出的一题给外层、奇数位给内层，两半相差不超过一题、都覆盖每个领域，各自保持合并顺序。单领域照旧在 `len // 2` 处切（v0.1/v0.2 不变）。第 5 步的五个领域各 25 道 dev：内层 62（12/13/12/13/12）、外层 63，各 ≥25。研究位记录每个领域在两半中的题数（`halves_by_domain`）与内层余量的按领域计数（`headroom_by_domain`），报告列出（M7 §4）；测试 `test_cycle.py::test_the_slot_halves_a_pooled_dev_inside_each_domain`、`test_bench_phase.py::test_it6_36_…`。原因：12 题 × 1 次的 gate 空候选通过率约 1/3、一题 8 个点，分辨不出任何真实效应；而外层任务必须是两支都没碰过的，dev 全给内层就得另立分区。v0.3 考虑过把内层扩到完整 dev、另立 `research_dev` 分区做外层池（Marketing 没有该分区则 `no_outer_pool`），不做：dev ≥ 50 时后一半已够配对，新分区与新结局都是多余的。
- **合并对照。** 每支内层 gate 的对照臂按 M3 §2.1(a)（v0.3）合并：同一冻结计划与模型身份下、该支 scope 可见的已安装 W 的全部有效读数。可见范围是：campaign 级公共输入（起始 W 的 dev/train 测量，今天已复制进每支的工作区，`bench.py:1895-1900`）加该支自己的读数；另一支的读数永不可见，两支因此从同一份合并对照起步、各自累积。对照不足时补齐缺少的读数（只读缺的题，M3 §2.1(a)（v0.3））。确认复测、幅度退步规则与成本项（M3 §2.1(b)、(d)、(e)（v0.3））在内层 gate 同样执行，min_gain 仍为 0.05；两支的份额按此重估（主线 worker 轮的实测费用乘 `worker_round_limit`，加确认复测），不再沿用 v0.2 的 0.15（v3 preset 今天仍写 0.15 作占位，重估值在第 5 步开根前写进 PRESETS，`bench.py` 的 v3 campaign 表有同一注释）。（v0.3，2026-10-05 已重估并写进 PRESETS：`arm_share` 0.16、`outer_share` 0.06，是第 5 步 $2,800 根的比例。）算式：第 5 步尚未有主线轮，按实测单价估一轮——W0（effort medium）每次 $0.45（v0.2 W0 train+dev $0.409、test $0.452），候选与 W* 每次 $0.69（天花板实验 W* 在 effort high 下 $0.685）；一轮 = R 的笔记与会话 $10 + train 筛选约 36 次 × $0.69 = $25 + dev gate 125 × $0.69 = $86.25，共 $121.25；× `worker_round_limit` 3 + 一次确认 125 × $0.69 = $86.25，得 $450，450 / 2800 = 0.1607 → 0.16（$448）。它盖得住一支实际可能的最大花费：内层 62 题 gate 三轮 3 × ($10 + $25 + $42.8) = $233 + 一次内层确认 $42.8 + 一次安装后的 train 重跑约 131 × $0.69 = $90 = $366，加 8 路在飞预留 8 × $7.2768 = $58.2 与两次 unknown $14.6，共 $439。外测份额：两支 W* 在 63 道外层题上 2 × 63 × $0.69 = $86.9，加一次补跑 10% 与同样的 $72.8 预留，$168 / 2800 = 0.06。份额是根的比例：换根（例如用户定 $1,800）要在开根前按同样的美元数重算（$1,800 时为 0.25 与 0.093），否则一支三轮跑不完。实现（v0.3，2026-10-04）：公共输入在活动模板 `control_pooling = pooled` 时多取一次起点 W 在内层 dev 任务上的读数（`_campaign_common`，付 common scope），连同 run manifest 原样复制进两支的结果记录（`_copy_readings`，attempt id 不变、已有的不重写）；两支的内层计划带活动模板的 `control_pooling` 与 `confirmation_repeats`，内层 gate 带模板 gate 的全部条款（只有 `min_groups` 按内层家族数收窄）；`fresh` 的旧模板两支仍各自新测对照，公共输入不取 dev 读数。
- **轮数。** `campaign.worker_round_limit` 从 2 改为 3（v3 preset `automationbench_harness_v3`；v1 preset 保持 2，它的数值已冻进 v0.1/v0.2 的核心，v0.3，2026-10-04），与主线段的 `worker_rounds` 相同；两支仍在相同已完成轮数下比较（基础设施中断的轮重跑不计，`bench.py:1886-1888`）。原因：两轮里一轮用于试错，v0.2 两支共四轮无一安装；三轮给 k 候选与筛选以可比的机会。
- **公共输入复用主线读数与开位的钱检查（v0.3，2026-10-05，第 5 步评审第 1、5 条）。** 开位前的钱检查原来只算外测份额、两支份额与 R 的一次调用，没算公共输入（起点 W 在内层 train 与内层 dev 上的读数），可能开位、付了公共输入、再放不下两支，得到一个付了钱的 inconclusive。改为：(1) 公共输入先复用主线里起点 W 的有效读数（`bench._mainline_readings`）：train 取执行身份相同（标签之外的 run 载荷相同）的测量与筛选读数，每题最新一条；内层 dev 只取没有计划的读数（基线的 dev 运行），因为 gate 的读数按它自己计划的题单冻结，按 M3 §2.1(a) 的条件进不了两支的内层池。这些行连同 run manifest、判分明细、轨迹与它们所指 release 的闭包原样拷进公共工作区，公共测量只跑没有读数的题（`Improvement.prior_readings`），内层 dev 也只读没读过的题。起点是 W0 时（基线测过全部 train 与 dev）公共输入一次也不付费；起点是已装的 W 时复用它已有的 train 读数（安装后的重跑与筛选），其余照测。(2) 开位的钱检查加上公共输入的估算（`bench.common_inputs_estimate`：没有可复用读数的题数 × 起点 W 实测的每次尝试均值，按根所限的每个维度）。(3) 为让执行身份相同，研究位的 release（公共与两支）带上主线 release 的宿主契约（`target_harness_ref` 与 `required_capabilities`，`bench._campaign_host`）；原来它们没有，run 身份与主线不同。(4) 两支在还没有自己的测量时从公共测量原样起步（加上它在暂停前跑过的补采），不再按 release 分组重建，因为公共测量的行现在可能名为几个执行同一 W 的 release。测试 `test_cycle.py::test_a_slot_whose_common_inputs_the_root_cannot_hold_asks_r_nothing`、`test_the_slot_reuses_the_mainline_readings_of_the_start_w`、`test_an_installed_start_w_reuses_its_own_readings_and_measures_the_rest`；`test_an_arm_pools_only_the_common_readings_and_its_own` 与 `test_common_inputs_that_leave_the_arms_short_end_the_slot_inconclusive` 按复用改写。
- **公共输入的第一轮笔记（v0.3，2026-10-06）。** 两支共享的第一轮笔记由同一选择规则选出（`dg.select_failures`，M4 §1）：多领域时按领域轮流，两支看到的失败覆盖每个领域。
- **研究位推迟到另一个工作区（v0.3，2026-10-05，第 5 步的分阶段运行）。** 冻结的 schedule 可以比 preset 少开研究位（`_beyond_preset` 只拒绝更多）；这时 cycle 在 `rsi/cycle.json` 记 `research_deferred`，报告的 C4 为 not_run，理由写“deferred to a separate workspace”。第 5 步的活动如此：研究位作为第 5 阶段在工作区的拷贝里用自己的根跑（ROADMAP 第 5 步）。测试 `test_bench_phase.py::test_a_schedule_without_the_presets_research_slot_reports_c4_deferred`。
- **外层池。** 仍是 dev 冻结顺序的后一半（`dev_families[half:]`），两支内层不运行、不读取其逐题结果；因为研究位只在 dev ≥ 50 时开，外层池也不少于 25 题。test 永不进入外层比较；Marketing 的划分冻结如旧（52/25/23，test 已读三次、对新主张为已耗尽，M3 §2.2（v0.3））。
- **余量检查。** 研究位开始、询问 R 之前，固定代码统计起始 W 在内层 dev 的合并对照读数里从未通过的任务数；少于 `campaign.min_headroom_tasks`（预设 6）时不问 R、不开两支，研究位记 `no_headroom`（不是 budget_exhausted，也不是 invalid），活动按 schedule 进入下一段，报告的 C4 写明原因。6 的依据：`min_gain` 0.05 在 25 题下是净增 2 题（1.25 向上取整），且安装前还要确认复测再清一次（M3 §2.1(b)（v0.3））；一个候选少有修好一半以上从未通过题的，可修的题少于 6 时理想候选也只有 ≤ 3 题的空间，在 sd 0.06～0.076 的噪声里两次都清过的机会很小，两支多半同为 W*，v0.2 的 inconclusive 会重演。检查顺序：dev 规模（`no_slot_pool`）→ 余量（`no_headroom`）→ 预算（两支份额 + 外测份额 + R 一次调用），三者都写进 `rsi/cycle.json` 的研究位步。实现（v0.3，2026-10-04）：`bench._cycle_slot` 先按 `cycle_pairs` 切出的两半各与 gate 的 `min_tasks` 比（任一半不足即 `no_slot_pool`，理由写两半的题数与 `dev < 2 × min_tasks`），再由 `bench.slot_headroom` 按 gate 的合并对照规则（`Improvement.control_pool`）读起点 W 在内层任务上的全部有效读数（模板不合并时也按同一规则只读），没有任何读数的任务计为从未通过，数字记进研究位步的 `headroom`；`campaign.min_headroom_tasks` 缺省 0 即不检查（v1 preset），v3 preset 6；`min_tasks` 缺省（None）即不限（v1 preset）。两种结局与 `no_proposal` 一样是已记录的研究位结局，再跑 cycle 不再问 R；报告的 C4 为 not_run 并写明哪一条。
- **E0 与台账。** E0 仍以主线经验原样开头；台账记录属于 E，随 E0 进入两支，但其 dev 逐题行按 `cycle_pairs` 去掉原始 gate 理由的同一规则去掉（只留 fixed/regressed/still_failing 的题数），因为 dev 的前一半是两支内层共同优化的集合、后一半是外层集合，两者的题号都不得由经验带入 R 的可读内容。两支各自的台账从 E0 的副本分别追加，不跨支。第一轮笔记的共享规则不变；编码会话的产出不共享（两支的 R 不同，会话从第一轮起就是分支内产物）。
- v0.3 考虑过把两支的台账指标（接受率、筛选净翻转之和、预测命中率、代码候选占比）写进研究收据的 `metrics.ledger.*` 作次要比较，不做：不参与裁决的数字在报告的台账节即可读到，收据不另加。

## 3. 执行与裁决

```text
slot pool check (v0.3): dev < 2 x min_tasks (50) -> slot outcome no_slot_pool, R is not asked, no arms
headroom check (v0.3): tasks of the inner dev (first half) the start W never passed in its pooled readings
    < min_headroom_tasks -> slot outcome no_headroom, R is not asked, no arms
freeze campaign; set aside the outer verification (one refill)
prepare the common inputs; set aside both branches' equal shares
for R in [R0, R1]:
    start isolated scope with the same W0/E0 (ledger rows included, dev task ids stripped)
    execute bounded worker rounds through M4/M5 (v0.3: k candidates, screen, inner gate on the first half of dev
        with the pooled control visible to this arm: common inputs + its own readings; confirmation re-run before install)
    keep the last W that passed the branch's inner gate (else W0)
compare the two W* without teacher on the untouched second half of dev, paired by task
    (both W* the same: no outer run, inconclusive)
emit research EvaluationReceipt; install R1 only if accepted
```

K 所需的隔离 worker 不可用时不启动分支；分支因 infrastructure_failed/repair_runtime 暂停时不视为正常完成，修复后以同一冻结条件续跑至冻结的轮数，无法续跑则 campaign 判 inconclusive，不以已完成轮数不同的两支比较。

外层任务两支内层均未访问；R 不看外层逐任务答案。按首版 preset 的配对规则，外层家族在研究位之前是主线开发家族：W0、R1 与 E0 中的汇总指标都来自覆盖它们的 gate。“未访问”指研究位开始之后，两支不运行它们，也不读取它们的逐题结果；E0 带入的汇总统计计入外层池的使用。复制 R1 与 W 的身份闭包时，主线 gate 的计划与逐题结果会随来源链进入公共与候选支的对象库；这是身份引用，不是可读内容（[M0 §4](00_CONTRACTS.md)），分支里没有读取它们的步骤。它们仍不是 final，最终泛化另测锁定版本。重复选择 R 会逐步使用外层池的统计信息，不能把它当无限可重复的独立 test；报告候选次数和访问历史。

冻结 gain/cost/patience 规则，不能临时挑一支历史最好 W。没有晋升用 W0，不删除失败支。协议错误判 invalid；环境/预算/配对不足判 inconclusive；有效但达不到门槛判 rejected。单支失败无法形成有效配对时不能让另一支自动获胜。

外层比较（2026-10-03）：
- 两支 W* 是同一个 worker ref（通常是两支都没有安装）时不跑外测，判 inconclusive：同一个 W 测两遍，说明不了 R。有多个 pair 时，任一 pair 相同即判 inconclusive，后面的 pair 不再运行。
- 两支在同一组外层任务上各跑一遍。基础设施失败的尝试按外测 TaskEvalPlan 冻结的 retry_policy 补跑一次，原尝试保留，补跑记 supersedes；任务失败、候选缺陷、评分器失败与额度耗尽不补跑。CampaignPlan.retry_policy 不读。
- 裁决按任务配对：同一 pair 内，任务的每次重复在两支都有有效尝试才计入；缺任一即判 inconclusive，不按各支剩下的任务各自求均值。外测上两边都是封存的 W*，候选缺陷与评分器失败也只使配对不全；invalid 只留给协议错误。独立组仍是统计单位。
- 两支等额：公共测量之后、两支开跑之前，两支的份额以不结算的预留占住，各支开跑时释放给自己。占不住时两支都不运行，外测份额退回，判 inconclusive：两支各以份额为上限，却都从根花钱，不预留时后跑的一支（R1）会被前面的花费饿死。重启找回这些预留，已跑过的支不再检查。
- 外测额度冻结为 `outer_budget`：外测 scope 以它为上限，并在公共测量与两支开跑前以不结算的预留占住；外测开始时释放给外测尝试，外测不运行时退回活动；重启找回原预留，不重复占用。外测额度耗尽时，没跑成的尝试使配对不全，判 inconclusive。
- 收据 metrics 记 `tasks`（配对的任务数）、`incomplete`（各 pair 缺的尝试，按失败类别计数）与 `identical_arms`。
- （v0.3，2026-10-04）外层比较的 W* 是两支内层 gate 安装的最后一个 W；内层 gate 接受前的确认复测（M3 §2.1(b)（v0.3））是该支自己的费用，不占外测份额。裁决只由外层配对差值、配对完整性与 `identical_arms` 决定；收据不记台账指标（§2）。

报告无教师完整任务结果及总改进费用，不以提案数量、训练 loss 或 R 的评分替代。相同任务多种 seed 不增加独立家族；费用未知时不宣称成本效率改善。

## 4. 安装后怎么证明实际回用

M3 只更新主线 release 的 R，保持主线 W/K。campaign 内 W* 不随方法晋升成为生产冠军；要部署 W* 必须满足主线任务评测/授权。固定核心 K 不被分支实验改动。

下一主线研究轮读取 active R1，`bind_release` 和现有 propose 流程将它用于实际步骤：诊断/提案要记录所用 prompt 内容摘要，检索/辅助预检要记录实际规则配置和执行结果。关联 context_ref、loaded R ref、步骤名、provider 请求摘要、proposal_ref 及后续 Experiment ref。不能只让 report 抄新 R 名称。

只更新一个目标时只要求对应步骤实际改变，其余绑定应保持一致。核心 admission 始终运行；R1 辅助流程不能关掉权限、标签、预算或 gate 检查。新回用记录进入 E，下一轮继续受原 schedule 限制。

（v0.3，2026-10-04）R1 改了 propose 时，回用的证据是紧随研究位的 worker 段里编码会话记录的 system 提示摘要等于 R1 的 propose 文本摘要，加会话记录里实际加载的 R ref；改了 diagnose 时与今天相同，看笔记请求摘要。会话的工具调用记录由宿主写（M4 §1（v0.3）），R1 不能伪造加载记录。

## 5. 机制与效果的验收

工程 fixture 必须覆盖“提出 R1 → 两支实际产生 W* → 比较 → 安装 R1 → 后续改进调用真正加载 R1”，并覆盖拒绝和不确定分支。真实实验必须安排有界方法比较；通过才使用 R1，不通过保留 R0。不能为了展示 RSI 绕过 gate。

scripted 成功只证明机制；实际模型提出候选证明自动提案；C4 成立还需要相同起点/预算的下游改进收益。三者分开标。W 的数据增长、版本 ref 变化、R1 被加载都不能单独支持 C4。（v0.3，2026-10-04）两支台账里提案记录的差别（接受率、筛选结果）也不能：它们只在报告的台账节呈现，裁决不读。首版不承诺递归收益或代际单调提高，但这条可执行且可验证的递归路径不能延期删除。

以后可以用训练后的 actor 辅助工作流搜索等方式扩展递归；需要新的实际调用绑定和比较设计。本版不提前实现多种递归后端。

## 6. 用例

| ID | 条件 | 预期 |
|---|---|---|
| RC6-01 | 一支读取另一支新轨迹/adapter | scope 拒绝，共同 E0 仍可用 |
| RC6-02 | R1 选蒸馏，R0 选 prompt | 共用 M4/M5 构建评测安装；两支费用与外评共用总根 |
| RC6-03 | 内层读取外层池（dev 后一半）/final 或开 research slot | 拒绝，无第三层 campaign |
| RC6-04 | 一支没有通过 gate 的 W | 外测 W0；两支都没有（W* 同为 W0）时不跑外测，判 inconclusive；故障造成配对不全则 inconclusive |
| RC6-05 | R1 通过并安装 | 主线 W 不变，下一改进请求实际采用 R1 的变更目标 |
| RC6-06 | 多 seed 同家族或 scripted R | 家族数/来源准确，不冒充真实自动发现或递归收益 |
| RC6-07 | manifest 写 R1，运行仍沿用 R0 配置 | 回用验收失败，不能出 C4 结论 |
| RC6-08 | R1 失败或 budget unknown | 不安装，保留 R0 和两支失败/费用，不强制涨分 |
| RC6-09 | 一支先要求补采/验证源方法，另一支直接提候选 | 共用有界驱动和相同限额；数据/源验证不当作 W 晋升，也不误判该支无提案退出 |
| RC6-10 | 两支生成不同能力包代码，一支请求更高 API/资源权限 | 共用固定 API/镜像和原预算根，越权被拒；包/经验不跨支共享，仅比较正式通过 gate 的 W* |
| RC6-11 | R1 只改 propose；另一次 R1 改了 diagnose；另一次 R1 分支第 2 轮 worker 失联 | 第一种第一轮笔记在 campaign scope 生成一次并记为共同输入；第二种两支在同一组选中任务、同一上限内各自生成；第三种该支暂停并在修复后续跑至冻结轮数，否则 campaign inconclusive |
| RC6-12（v0.3） | 起始 W 在内层 dev 合并读数里从未通过的题只有 4 道 | 研究位记 `no_headroom`，不问 R、不预留两支与外测份额、不开两支；下一段照常；报告 C4 not_run 写明原因 |
| RC6-13（v0.3） | 两支内层 gate；R0 支已装一个候选并有两次自己的对照读数 | 两支内层任务计划都是 dev 的前一半（≥25 题）；R0 支的合并对照 = 公共输入读数 + 自己两次；R1 支看不到 R0 支的读数；两支起步时合并对照相同；R0 支的安装前有确认复测 |
| RC6-14（v0.3） | R1 支装了 2 个候选、R0 支 0 个；外层配对差值 0 | verdict 按外层判 rejected/inconclusive，不因台账记录接受 R1；报告的台账节呈现两支记录并注明不支持 C4；收据不记台账指标 |
| RC6-15（v0.3） | 只含 Marketing 的活动（dev 25 题）到研究位 | 研究位记 `no_slot_pool`，不问 R、不预留、不开两支，不运行外层比较；C4 not_run 写明原因；下一段照常 |
| RC6-16（v0.3） | 多领域活动，合并 dev ≥ 50 题 | 两支内层只读合并 dev 的前一半，不运行、不读取后一半的逐题结果；外层在后一半上配对；E0 与台账进入两支时 dev 的题号被去掉 |
| RC6-17（v0.3） | `campaign.worker_round_limit` 3；一支第 2 轮因基础设施中断 | 该轮重跑不计；两支都完成 3 轮后比较；活动配置把 worker_round_limit 调高于 preset 时拒绝 |

扩展 `test_campaign.py`、`test_rsi_integration.py`，不为蒸馏另写 runner。v0.3（2026-10-04）的验收：
- `pytest tests/test_campaign.py tests/test_cycle.py tests/test_rsi_integration.py tests/test_report.py`
- 第 2 步已实现（2026-10-04，都在 `test_cycle.py`，夹具的 dev 只有 3 个家族，所以用 `min_tasks` 2 与 `min_headroom_tasks` 1/2 演练规则）：`test_a_slot_without_headroom_asks_r_nothing`（RC6-12、IT6-40，参数化：少于下限不开位、够下限开位）、`test_the_headroom_check_runs_before_the_budget_check`（含 dev 规模在余量之前）、`test_a_dev_split_under_twice_min_tasks_opens_no_slot`（RC6-15、ER6-22；兼作内层 = 前一半、外层 = 后一半的断言，原名 `test_inner_gates_read_the_first_half_of_dev_with_at_least_min_tasks`/`test_the_outer_pool_is_the_second_half_of_dev`）、`test_an_arm_pools_only_the_common_readings_and_its_own`（RC6-13）、`test_an_inner_gate_confirms_before_it_installs`（RC6-13）；`worker_round_limit` 3 与 `min_headroom_tasks` 6 由 `test_cycle.py::test_the_v3_preset_freezes_a_k_with_the_agent_kind_the_effort_ceiling_and_the_five_limits` 断言。
- 随第 3 步（台账、回用；已实现，2026-10-05）：`test_campaign.py::test_the_verdict_never_reads_the_ledger`（RC6-14）、`test_cycle.py::test_e0_strips_dev_task_ids_from_ledger_rows`（RC6-16；`experience.strip_dev_task_ids`）、`test_campaign.py::test_worker_round_limit_three_compares_at_equal_completed_rounds`（RC6-17）、`test_rsi_integration.py::test_the_segment_after_an_install_runs_its_session_under_r1s_propose_text`（§4；回用的证据是 `research_steps` 事件里 step 为 `session` 的请求与 R1 的 propose 摘要，`RESEARCH_STEPS["research.propose"]` 多了 `session`）；现有 `test_rc02_an_arm_that_installed_nothing_returns_w0_with_its_costs` 等夹具不改（它们直接构造 ArmOutcome，不经 `min_tasks`）。夹具的说明（2026-10-05 复核后改）：rsi 世界有缺口的 train 家族正是该测试里两支的外层池，两支的内层 train 池原本没有失败题；M4 §2 复核后只凭通过样本的筛选不算读数（`invalid`），所以该测试换了一个只在本测试里用的 actor（`Forgetful`：只要求发消息的请求，没有提醒就不发），W0 在内层 train 的 `rsi.notify_once` 上也失败、同一条 memory 能修好它，两支的筛选有失败集，`screen.min_net_flips` 用真实的 1；分支里 R 读到复制来的 W0 train 失败轨迹。同节的 `test_rsi_integration.py::test_a_restarted_arm_keeps_the_collection_batch_it_paid_for_before_the_crash` 断言重启的一支把重启前付过的采集批次并入测量（复制来的行名为公共 release，分支自己的批次记在起点 release 下）。两支的 E0 带台账行（dev 题号换成题数），两支各自的台账从副本追加。

preset/K 新增或改动参数（v0.3，2026-10-04）：`campaign.worker_round_limit` 2 → 3；`campaign.min_headroom_tasks` 6（新增）；`campaign.arm_share` 由主线 worker 轮实测费用 × worker_round_limit 加确认复测重估（数值在 ROADMAP 的下一活动配置里定，不在本文；v3 preset 今天沿用 0.15 作占位，第 5 步开根前重估并写进 PRESETS；（v0.3，2026-10-05）已重估为 `arm_share` 0.16、`outer_share` 0.06，按第 5 步 $2,800 的根，算式见 §2）；研究位开放条件 dev ≥ 2 × `min_tasks`（50，由 M3 的 `min_tasks` 推出，不另设参数）；新研究位结局 `no_slot_pool`、`no_headroom`。
