# 开发日志

倒序，最新在上。一条记录一次有意义的改动：做了什么、证据（真实运行或测试量到的数，带数字）、因此改了什么设计、commit id。日期取自 git log。

证据只写量到过的：贪吃蛇（当时的 `examples/snake`）在一台 GPU 主机上的几次多代运行（下称 run4、run5、run6）、离线运行、决策集上的统计，以及先写失败测试再修的评审。没有证据的改动照实说"没有运行证据"。

哪些数字能复算，哪些不能：决策集上的统计从 `evals/replay_campaign/` 重算；M6 的真实验收从 `examples/workspace-m6/ledger/` 重算。**贪吃蛇场景包与多代演示循环已于 2026-09-27 删除**（见当日第二条记录），因此所有贪吃蛇数字——离线演示与 run4～run6——现在都无法从本仓库复算；它们留在下面的历史条目里，作为当时改动的依据，不作为可复现结果。run4、run5、run6 本来也跑在一台未发布工作区的 GPU 主机上。

---

## 2026-10-06 v0.3 第 5 步续跑第 1 轮（整体第 3 轮）：被拒

- 做了什么：`--phase improve --rounds 1 --reason ...`（`schedule_extended` 事件记下偏离冻结调度），笔记按领域分层后的第一轮。
- 证据：R 7 回合、读轨迹 3 次、3 个候选；withhold（净翻转 1）进 gate：+0.8（41.6% 对 40.8%），marketing +12、hr −4、sales −4、其余 0，退步 7 题；拒。名义 $78.34，累计 $424.61。
- 读法：三轮都是 marketing 单独上涨，其余领域不动或退步；合并 dev 的 0.05 门槛要求改动在至少两个领域都有效。筛选信号这一轮弱（最好的候选净翻转 1）。
- commit：见 git log。

---

## 2026-10-06 v0.3 第 5 步：笔记按领域分层与续跑

- 做了什么（离线：没有调用模型、没有付费阶段、没有读 `.env`，未提交；工作区 `workspace-claude-v3` 只读，只经下面的代码路径）：
  - 笔记选择按领域分层（`research/diagnosis.select_failures`）：失败跨几个领域时（`领域.题名`），每个领域内按原规则排（从未纳入的在前、再按最近一次纳入从久到新、同序按冻结顺序），领域按其题在冻结顺序里首次出现的先后轮流各取一题，取满上限；某领域不够时由其余领域补。单领域（或 id 不带领域）时与以前逐字相同，旧工作区的笔记身份、覆盖与摘要不变。覆盖记录多领域时另有 `by_domain`（每领域失败数与纳入数）。研究位公共输入的第一轮笔记用同一规则。
  - R 读到每个领域的 gate 结果（`experience.gate_by_domain`、`ledger_projection(rows, objects)`）：投影的 gate 项多出 `by_domain`——每领域 fixed/regressed/still_failing 的题数与两臂 dev 均值及差值（收据的 `metrics.per_domain`）；两支 E0 去题号时先把计数留下；只有计数与均值，没有 dev 任务 id。报告的台账同样带它。
  - 冻结 schedule 之外的显式续跑：`--phase improve --rounds N --reason "…"`（CLI 新增 `--reason`）对 cycle 活动里已结束的最后一个 worker 段起作用：记 `schedule_extended` 事件，`RoundLog.extend` 重开段的轮次记录（记下从第几轮起、为什么、谁、重开前的结束原因），在同一计划、同一根、同一 K 下跑 N 轮，轮次与诊断序号接着；patience 与基础设施失败上限只数这次续跑的轮（带上之前的空转，一轮也跑不了），预算、暂停照常；中断后同一命令续同一次续跑；结束后段与 `rsi/cycle.json` 记续跑的结束原因与 `extensions`，cycle 之后仍把该段当已结束；报告列出每次续跑。段未结束、活动暂停、缺理由、续一个未完的续跑却换了轮数，都拒绝。
- 证据：
  - 对本工作区的离线核对（只读）：W0 的 148 道 train 失败为 marketing 27、sales 33、support 27、hr 38、finance 23；按新规则第 2 轮的 30 条笔记各域 6 条（12 条是 marketing、sales 最久以前纳入的，按身份复用缓存）。前两轮在 R 的台账投影里现在是：rulesmem marketing +24（修 6、坏 0）、hr +8、finance +4、support −4、sales −16（修 2、坏 6）；writemem marketing +16、finance +4、hr 0、support 0、sales −8；投影里没有任何 dev 任务 id。
  - 新增测试：`test_diagnosis.py::test_notes_over_five_domains_are_stratified_and_rotate_within_each_domain`、`test_a_single_domain_selects_notes_exactly_as_before`、`test_research_round.py::test_the_ledger_projection_shows_each_domains_gate_outcome_without_dev_task_ids`、`test_bench_phase.py::test_an_extension_after_a_patience_stop_runs_the_next_round_and_is_recorded`（夹具：三个领域、两轮被拒后 patience 停下，续跑 1 轮跑第 2 轮，笔记每域 1 条且是各域没读过的，gate 读合并对照、没付对照臂，事件、段记录、cycle.json 与报告都有这次续跑，之后的 cycle 不再跑这个段）。全套离线测试（前台）1139 passed、1 deselected（live）。
  - 续跑一轮的费用（第 2 阶段实测）：笔记与会话每轮约 $5.35、筛选约 $18、gate 候选臂 125 × $0.495 ≈ $62，约 $85；加确认约 $147；装上后的下一轮另有约 $66 的 train 重测。根里可用约 $684（扣 final 份额、已花、3 笔 unknown 与 8 路在飞）。
- 每轮的命令：`.venv/bin/ouroloop bench --config examples/automationbench/claude-v3.toml --phase improve --rounds 1 --reason "2026-10-06: R's notes are now stratified by domain (stage 2 noted only marketing and sales); continue segment-0 after its patience stop, one round at a time (user decision)"`。
- 读法：续跑是对冻结 schedule 的公开偏离，理由是框架缺陷（不是看了结果再调），记录随活动走；final 仍按第 4 阶段的规则，只在有安装时跑。
- commit：未提交。

---

## 2026-10-05 v0.3 第 5 步第 2 阶段：两轮被拒，笔记只来自两个领域

- 做了什么：`--phase cycle --until segment-0/round-2`，终端面板里跑；中途订阅会话限额（`ProviderAccountError`）暂停，9 笔调用不计费释放，限额重置后同一命令续跑，gate 从 evaluating 接着跑完。
- 证据：第 1 轮 rulesmem（memory）dev +3.2（marketing +24、sales −16，退步 10 题），第 2 轮 writemem（memory）+2.4（marketing +16、sales −8，退步 6 题），都低于 min_gain 0.05；patience 2 停在第 2 轮，没有安装。名义 $170.30，累计 $346.27。R 用满了 k = 3、读了轨迹（第 1 轮 4 次 83 KB，第 2 轮 2 次 60 KB），说明上一条的说明改动起了作用。
- 原因：笔记的失败题按合并后的冻结顺序取前 30 道、逐轮轮转；合并顺序 marketing 在前、sales 其次，所以第 1 轮笔记是 marketing 27 + sales 3、第 2 轮是 sales 30，support/hr/finance 一道没进笔记。候选因此偏向这两个领域。单领域活动里这个顺序无害，多领域时它决定了 R 看到哪些领域。
- 读法：按事先的规则本阶段是“停”。缺陷在框架（多领域的笔记选择），修好之前的结果不说明 RSI 在五个领域上能不能提升。
- commit：见 git log。

---

## 2026-10-05 v0.3 第 5 步：第 2 阶段之前给 R 的会话说明加一句

- 做了什么：试点里 R 在 k = 3 下只交了 1 个候选、一次轨迹也没读（只看笔记）。`research/session.py::SESSION_FORMAT` 加一句：除非说得出少做的理由，做满 k 个针对不同失败原因的候选（筛选只留 train 上最好的那个，多一个候选代价小）；写一个候选之前至少用 read_trajectory 读一次它针对的失败尝试（笔记是摘要，轨迹是 agent 实际看到与做的）。这是框架改动，在本活动任何一轮 R 运行之前做的；R0 的 policy 对象与冻结的计划不变，`research_session` 事件的 `prompt_hash` 记下新文本。没有动 gate、final、test 或任何计划。
- 证据：相关测试 284 passed；效果看第 2 阶段的会话记录（候选数、轨迹读取次数）。
- commit：见 git log。

---

## 2026-10-05 v0.3 第 5 步第 1 阶段：五领域基线

- 做了什么：`ouroloop freeze --config examples/automationbench/claude-v3.toml`（五个领域：Marketing 沿用 v0.2 划分 52/25/23、test 标 spent；sales、support、hr、finance 分层 50/25/25；根 $1,500、final 份额 $390；K 的 effort 上限 medium；一段 6 轮、研究位延后），把打印的 `[rsi]` 引用写进配置（commit `4d9b345`）；在终端面板里跑 `--phase cycle --until baseline`，8 路并发，没有做 16 路试跑。
- 证据：377 次尝试全部有效、全部正常结束，名义 $175.97，每次 $0.466。W0 train 41.3%（104/252）、dev 40.8%（51/125）；按领域 train / dev：marketing 48.1 / 28.0，sales 34.0 / 48.0，support 46.0 / 36.0，hr 24.0 / 60.0，finance 54.0 / 32.0。3 笔调用结算为 unknown（尝试本身都由计量器重试后有效完成），共占 $21.83。
- 读法：W0 在合并 dev 上 40.8%，余量大；每域 25 道 dev 的差别是噪声。单价略高于估计、unknown 比率约高一倍，第 2 + 3 阶段与根的余额刚好相当，第 3 阶段按第 2 阶段的实花再定。
- commit：见 git log。

---

## 2026-10-05 v0.3 第 5 步第 0 阶段：试点

- 做了什么：`claude-v3-pilot.toml`（operations + Marketing，分层各 6/13/81，合并 train 12、dev 26；k = 3、research_slots 0、K 的 effort 上限 medium、根 $130）跑 `--phase cycle --until segment-0/round-0`，在终端面板里跑（不受后台 30 分钟时限）。之前一次被 `--checks-only` 误启动，SIGINT 后干净停下（预留全部结算，$11.95），续跑接上。
- 证据：总 $47.11；基线 W0 train 5/12、dev 11/26（42.3%）；R 一次会话 3 回合 2 次编辑、0 次轨迹读取，交 1 个 memory 候选（写前取全证据的清单），筛选净翻转 4，gate 确认复测后接受：dev 42.3% → 69.2%（+26.9，两次读数相同，无退步；Marketing +30.8、operations +23.1），安装。每次尝试 W0 约 $0.39～0.42、候选约 $0.48；R 的笔记与会话一轮不到 $1.2。
- 因此改了什么：读与列目录的路径接受尾斜杠（R 的 `list_files("package/")` 被拒浪费一回合），镜像重建；`--checks-only` 对 smoke 以外的阶段拒绝（它在 cycle 上被忽略、照常付费）。全套 1135 passed。
- 读法与边界：试点证明新 R 路径在真实模型下走通（会话、声明、草稿、筛选、gate、确认、安装、会话记录入库），并给出单价；dev 26 题上的 +26.9 不作任何主张。R 只用了 k = 3 中的 1 个、没读轨迹——是否值得在提示里鼓励多候选与读轨迹，看段 2 的会话再定。
- commit：见 git log。

---

## 2026-10-05 v0.3 第 5 步：分阶段运行与评审修正

- 用户决定（同日，多领域的实现之后）：第 5 步分阶段运行，每段结束停下来分析，不一次跑完约 $2,000、16 小时；K 的 `max_effort` 降到 medium（W0 的 pin 也是 medium），候选不能用更高的 effort 换增益，C1 读的是 harness 而不是算力；研究位（C4）推迟到工作区的拷贝里用自己的根跑。全程离线：没有调用模型、没有付费阶段、没有读 `.env`，未提交。
- 运行手册（写进 ROADMAP 第 5 步，含每段的命令、费用、时间与继续/停止规则）：第 0 阶段试点（k = 3，约 $52）→ 第 1 阶段基线（`--until baseline`，约 $170，根在这里打开，头 50 次是 16 路试跑）→ 第 2 阶段前三轮（`--until segment-0/round-2`，约 $400；没有安装就停、不跑 final）→ 第 3 阶段第 4～6 轮（`--until segment-0/round-5`，可选，约 $400）→ 第 4 阶段封存的 final（100 题 × 3，约 $285）→ 第 5 阶段研究位（可选，另一个工作区）。
- 配置：`claude-v3.toml` 的 `[freeze.schedule]` 为一个 segment 6 轮、`research_slots = 0`、patience 2（`_beyond_preset` 只拒绝更多的研究位，不查轮数；preset 不用改），`max_effort = "medium"`，`[estimate] cost_per_attempt = {base = 0.45, evolved = 0.50}`，根 $1,500，final 份额 $370；`claude-v3-pilot.toml` 改为 k = 3（评审第 6 条：会话费用才能按比例推到活动），根 $130。cycle 在 schedule 少于 preset 的研究位时记 `research_deferred`，报告的 C4 为 not_run、理由“deferred to a separate workspace”。
- 费用重算（effort medium）：W0 每次 $0.45，候选与 W* 每次 $0.50；基线 $169.65；一轮 $90.5（R $10 + 筛选 $18 + gate $62.5），确认 $62.5、安装后重跑 $65.5；六轮 $799；final $285；合计约 $1,254。卡住的预留按实测率约每 2,250 次调用一个（v0.1/v0.2 与天花板实验约 9,900 次调用留下 4 个）：final 之前约 16,400 次调用 → 8 个，final 约 5,160 次 → 3 个。final 份额 $285 + 8 路 $58.21 + 3 个 $21.83 = $365.04 → $370；根 $968.65 + 8 路 + 8 个卡住（各 $58.21）+ $370 = $1,455.07 → $1,500。
- 评审修正（每条先写一个修正前会失败的测试）：
  1. 研究位开位的钱检查加上公共输入的估算（`bench.common_inputs_estimate`）；公共输入复用主线对起点 W 的有效读数（`bench._mainline_readings`、`Improvement.prior_readings`），W0 时 train 与内层 dev 全部复用、公共工作区一次也不付费，已装的 W 复用它已有的 train 读数；为让执行身份相同，研究位的 release 带上主线的宿主契约（`bench._campaign_host`）；两支在没有自己的测量时从公共测量原样起步。测试 `test_cycle.py::test_a_slot_whose_common_inputs_the_root_cannot_hold_asks_r_nothing`、`test_the_slot_reuses_the_mainline_readings_of_the_start_w`、`test_an_installed_start_w_reuses_its_own_readings_and_measures_the_rest`。改写的旧测试：`test_an_arm_pools_only_the_common_readings_and_its_own`（公共 dev 读数现在是主线基线的 `base` 读数）、`test_common_inputs_that_leave_the_arms_short_end_the_slot_inconclusive` 与 `test_real_run_findings.py::test_every_cycle_step_pauses_on_an_account_error[common-slot-0]`（关掉复用，保住这一步里要付费的调用）。
  2. `final_estimate` 按执行身份（W 与 K）找读数，W* 在 gate 里的读数（记在候选 release 上）也算；份额余额放不下“预期 + 在飞预留”时 `--phase evaluate` 在付费前拒绝。测试 `test_paid_run_safety.py::test_the_final_estimate_reads_w_stars_gate_readings_by_execution_identity`、`test_a_final_the_share_cannot_hold_is_refused_before_the_first_attempt`。
  3. `ouroloop freeze` 封存 final 计划前打印并检查估算（读数优先，否则 `[estimate]`；预期 + 并发 × 上界 + 2 个卡住的上界须放进份额，否则不封存）。测试 `test_a_final_plan_the_share_cannot_hold_is_not_sealed`。
  4. `Improvement._verify_bound` 加美元项（尝试数 × 父版本实测每次成本 × (1 + β0)），筛选按同价并入第一次筛选前的检查；付不起的 gate 不开始。测试 `test_the_verification_bound_prices_the_gate_and_a_gate_it_cannot_pay_for_never_starts`。
  5. 省钱的复用：由第 1 条覆盖。6. 试点的 k：见上。
  7. `test_task_gate.py::test_a_receipt_over_several_domains_carries_per_domain_means`（真实收据）；IT6-36 报告测试的 docstring 改为它实际检查的内容；`per_domain` 只对 `领域.题名` 形式的 id 生效（夹具里 `t1` 这类 id 原来会被当成五个领域）。
  8. `test_bench_phase.py::test_a_domain_list_changed_after_bootstrap_is_refused_at_startup`。
  另加 `test_a_schedule_without_the_presets_research_slot_reports_c4_deferred`。
- 证据：全套离线测试（前台）1122 passed、1 deselected（live 标记），约 4 分钟（本日上一条为 1112；新增 10 个测试）；两份配置的读取、preset 校验、schedule 与 `_beyond_preset`、预算与 final 份额检查、停止点都离线核对过。
- 第 0/1 阶段评审（2026-10-05，隔离副本里经真实 CLI 路径、真实上游、Docker 镜像与账本的脚本化假模型运行；第 0 阶段的路径没有拒绝或崩溃，美元上界、effort 截顶、k = 3、停止点、续跑与暂停都核过）确认的五条，均已修正、各有测试：(1) R 的会话整段存为一个对象（`CodingSession.transcript()`：回复文本、工具调用与参数、每个工具的输出或拒绝原因，逐项截断、严格 UTF-8），`research_session` 事件记 `transcript_ref`，被拒调用的行带错误文本（`test_research_session.py::test_the_sessions_turns_are_stored_with_refusals_and_the_event_names_them`、`test_a_transcript_item_is_cut_and_strict_utf8`）；(2) final 之前的主张只读 dev 读数，不再把筛选与 train 测量平均进去（`test_report.py::test_a_claim_before_the_final_reads_the_dev_readings_only`）；(3) W0 读数里因基础设施失败的尝试按 M5 §3 补跑一次（train 测量与基线 dev 臂，`refill_attempts`，读数取每个配对最新的一条），16 路试跑的 429 伤亡不再是永久的洞（`test_cycle.py::test_an_infrastructure_casualty_in_the_baseline_is_refilled_once_and_the_pooled_reading_is_complete`）；(4) final 份额 $370 → $390（W* 每次可到 $0.607），根仍 $1,500；(5) `claude` 子进程各在自己的 session 里（`start_new_session`），终端 Ctrl-C 只到父进程、在飞的调用跑完并结算，超时与父进程等待被打断时杀子进程的进程组，解释器退出时杀仍在跑的子进程（`test_claude_code_provider.py::test_a_terminal_interrupt_reaches_the_parent_and_not_the_claude_child`、`test_a_timeout_kills_the_child_and_what_it_started`）；运行手册加了第 1～3 阶段的操作规则。修正后全套离线测试（前台）1128 passed、1 deselected（live），新增 6 个测试。
- 未做 / 留下：第 5 阶段（研究位在拷贝里跑）的路径离线没有走过——新 `scope_id` 下 cycle 跳过已完成的基线与 segment-0 再开研究位，要在第 4 阶段之后核对；preset 的 `arm_share`/`outer_share` 仍是按 $2,800 根算的 0.16/0.06，第 5 阶段开根前按它的根重算（注释给了 $346 与 $142 的美元数）。
- commit：未提交。

---

## 2026-10-05 v0.3 第 5 步：多领域的实现

- 做了什么：按用户在天花板结论之后的决定（第 5 步照完整设计跑，含研究位），把多领域活动实现到“可以运行”为止。全程离线：没有调用任何模型、没有付费阶段、没有读 `.env`，未提交。
  - 领域列表：`[bench] domains`、`split_rule`、`spent`、`[bench.frozen_splits]`（`bench._domain_keys`）；单值 `domain` 读作一个元素的列表，旧配置的路径、manifest 与 hash 不变（`claude.toml`、`claude-full.toml` 照原样读）。每个领域的划分冻结在 `bench/<domain>/`；多领域时合并成一份 `family_rule = "pooled"` 的 manifest（`benchmark.pool_manifests`），记下每个领域的 hash、规则、计数、分层计数与 spent 领域，冻结在合并名（`a+b+...`）下；合并 suite 拒绝没有领域前缀或撞名的任务 id（`ab.pool_suites`）。结果与判分明细存在合并名下的一个目录，不按领域分目录（M7 §2 原文写按领域存放；一个读数是跨领域的一个 RunManifest，拆目录要给每个读者加路由，按领域读只需按 task id 前缀过滤），M7 §2 已改写。
  - 分层划分：层键 = 计分断言的种类集合 × 工具族（`ab.stratum`）。种类用天花板实验的读法（`_not_`/`count`/`contains`/其余），取集合而不是类型多重集：真实上游每领域 100 题有 56～98 种多重集，按多重集分层每层一题。工具族：订阅了办公服务（gmail、drive、sheets、calendar、slack）以外的业务应用为 `app`，否则 `office`。分配（`benchmark.stratified_splits`）用一条“落后最多者先得”的序列定各层名额，整域计数正好是比例，层内按原哈希点填；只读任务定义，不读结果、不给状态评分。manifest 的 `split_rule`/`strata` 为 None 时不进哈希，旧 manifest 的 hash 不变；已冻结的划分在规则改变后照原样保留（只打印说明）；`frozen_splits` 钉住的领域按哈希规则划分并核对 hash。
  - 启动检查与 preset：v3 preset 去掉 Marketing 锁（`domain` 为 None），每次打开活动都经 `bench.load_domains`，装不上的领域点名拒绝；`startup_checks` 另核对各领域划分的 hash 与合并划分所记的一致、核心 `split_ref` 等于合并 hash，并对每个领域一道 dev 题做评分器契约检查；`ouroloop freeze` 打印每个领域的 hash、计数与分层计数。
  - 研究位：合并 dev 在每个领域内对半分（`bench.halve_dev`，奇数题的领域交替把多出的一题给外层/内层），两半都覆盖每个领域；按合并顺序直接切，前两个领域会整个落在内层。研究位记下 `headroom_by_domain` 与 `halves_by_domain`（计数，不记题号）。
  - final：`tasks = "test"` 只解析为非 spent 领域的 test，显式列出 spent 领域 test 题的计划拒绝冻结，`--phase evaluate` 对这样的计划在付费前拒绝；`bench.final_share_problem` 在冻结预算时拒绝放不下 `concurrency` × 单次上界的 final 份额，并打印 `bench.final_estimate` 的预期花费与建议份额；`--phase evaluate` 在第一次尝试前打印每臂“还要跑的尝试数 × 该臂 release 的实测每次成本（dev 读数）”、合计、在飞预留与份额余额（只打印）。
  - 报告：多领域时 `identity.domains`/`spent`、每 run 的 `per_domain`（通过率、`cost_per_attempt`）、`final_by_domain`（spent 领域单列、主张行只合并非 spent 领域、best-of-k 未跑写 not_run）、C1～C3 的区间去掉 spent 领域的题；gate 收据在多领域时多一项 `metrics.per_domain`（按领域两臂均值与差值，不进经验日志）；台账 `by_domain`、研究位 `headroom`。单领域报告不变。
  - preset：`arm_share` 0.15 → 0.16、`outer_share` 0.08 → 0.06（按 $2,800 的根重估，算式在 `bench.PRESETS` 与 M6 §2）。
  - 配置：`examples/automationbench/claude-v3.toml`（五个领域；Opus 5.5 经 claude_code，actor effort medium、K 的 max_effort high；research.llm 同 claude-full；并发 8；根 $2,800、final 份额 $420）与 `examples/automationbench/claude-v3-pilot.toml`（试点，见下）。测试夹具的假上游多了 `sales`、`support` 两个领域（各 12 题、六层）与两种断言类型。
- 证据（离线）：
  - 真实上游（`4a8e106`）六个领域各 100 题。seed 1：Marketing 复现 `aa7ff7ec189c0cbb`（52/25/23）；sales/support/hr/finance 各 50/25/25（8、4、7、10 层），合并 train 252、dev 125、test 123（主张可读 100），合并 hash `5e68b39fca26a675`，同一配置在新目录里得到同一 hash；研究位内层 62、外层 63，每个领域在两半各 12 或 13 题。两份配置的读取、preset 校验、预算与 final 份额检查、划分都离线跑过。
  - 新增 16 个测试（M7 §5 的八个、IT6-36 的两个、M5 的两个、报告的三个、研究位对半分一个），全套离线测试 1112 通过（前台运行，约 4 分钟；HEAD `a469dd8` 时 1096）。改动的旧测试只有一个：`test_cycle.py::test_the_v3_preset_freezes_…` 里 v3 preset 的 `domain` 与两个份额。
- 费用重算：按实测单价（W0 medium $0.45、候选与 W* $0.69 即天花板实验 W* 在 high 下的 $0.685、单次上界 $7.2768）中央估计花费约 $2,628（基线 $170、主线六轮 $1,080、研究位 $1,036、final $342），根 $2,800、final 份额 $420；文档的 $1,800 只够低情形（约 $1,590 花费），中央情形差约 $1,000；高情形（包每次 $1.06）约 $4,400。原估算漏了安装后的 train 重跑（每次约 $90）、研究位公共输入（$217）、k = 3 的筛选（约 $25 而不是 $5）与在飞预留。根由用户在开根前确认（ROADMAP 第 5 步）。
- 试点：`claude-v3-pilot.toml`，operations + Marketing 的分层小子集（train 12、dev 26；gate 的 `min_tasks` 25 不调低，dev < 50 所以不开研究位，`research_slots = 0`），k = 1、`max_effort` medium、并发 4、根 $120；`--phase cycle --until segment-0/round-0` 跑基线与一轮真实 R 会话、筛选与 gate，约 $40 名义。它补第 3 步“新 R 路径未经真实模型”的出口，量到的每轮会话费用替换估算里的 $10。
- 16 路并发试跑写成手动一步（现有的 `--phase baseline --limit 25 --concurrency 16`，即基线头 50 次尝试），不加新参数。
- 未做 / 留下：试点与活动都没跑；IT6-37（best-of-k 臂本身，发表时）、IT6-41（导出）仍待实现；M7 §5 里第 2 步留下的三个报告测试（`test_a_gate_line_states_pooled_readings_and_the_confirmation` 等）不在本步。
- commit：未提交。

---

## 2026-10-05 v0.3 第 4 步：天花板实验的结果

- 做了什么：按 `882fed5` 冻结的设计跑天花板实验（`ceiling_run.py`，`claude-ceiling.toml`，Opus 5.5 经 claude_code、effort high、并发 8、根 $150 硬停）。第一段约 30 分钟后被会话的后台时限截断（31/52 次尝试已落盘，账本留 1 笔 open 预留），同一命令续跑补完其余 21 次，未重复付费。
- 证据（`workspace-claude-ceiling/ceiling/result.json`）：52 次全部有效，无硬限额截断、无包崩溃、无未读；名义 $45.28（W* $17.82 / 26 次，$0.69 每次；手写包 $27.46 / 26 次，$1.06 每次）。逐题：R = 8（W* 本次两次都失败），X = 1（`featured_snippet` 0/2 → 1/2），B = 2（`voice_of_customer` 1/2 → 0/2，`google_ads_pause_low_ctr` 1/2 → 0/2），E = 5；两臂通过率都是 6/26 = 23.1%。v0.2 三次读数都失败的 4 道 dev 题（`ad_performance_review`、`event_registration_sync`、`industry_event_tracking`、`seo_competitor_gap`）两臂都 0/2。手写包的机制确实触发：27 份记录里 aux 调用 53 次。
- 结论（按事先写定的规则）：training——循环层机制修不掉 W* 剩下的失败，重心转向第二版的训练准备。边界：一个手写包、每臂每题 2 次、13 题；只说明 Marketing 在 RSI 之后的残余，不说明新领域从 W0 起步的可达增益。ROADMAP 第 4 步的止损写的是“结论为模型能力时，第 5 步只跑基线与 final”，这与发布目标（RSI 在留出 test 上带来多少提升）冲突；用户决定第 5 步照完整设计跑（含研究位），止损不执行，已写进 ROADMAP 第 4、5 步。
- commit：见 git log。

---

## 2026-10-05 v0.3 第 4 步：天花板实验的准备

- 做了什么：按 ROADMAP v0.3 第 4 步，把天花板实验准备到可以运行为止。全程离线，没有调用任何模型，没有读 `.env`，未提交。
  - 任务集：从 v0.2 的记录读出，冻结为 `examples/automationbench/ceiling/tasks.json`，带来源（工作区、release、run hash、attempt id、读法）。读法用 `Results.all/runs/grading`、`Releases.active`、`Objects`、`rsi/cycle.json` 与 `rsi/events.jsonl`。
    - W* = `release:6cd37b8e49a4`：active.json 与 cycle.json 的 segment-1 一致，`install-cb19d2b77cb2` 装入。
    - W* 的 dev 读数三次：84%（装入它的候选臂）、76%、76%（segment-1 第 2、3 轮的对照臂）。
    - dev 取三次里至少两次失败的 6 题：三次都失败的 4 题，即文档的“4 道”，记 `persistent`；后两次失败的 2 题。
    - train 取 W* 唯一一次 train 测量（11 题；安装后只重跑上一版失败的题）中失败的 7 题。
    - 每题的失败断言类型按名字分为 missing_write、forbidden_write、content、count；文件里不放断言参数。
  - 手写包 `examples/automationbench/ceiling/package/`：manifest v2，roles act/aux。W* 的 `memory.md`、`middlewares/finish_audit.py` 与其测试逐字节带上，另加 `agent/ceiling_agent.py` 与 7 个包内测试。`ceiling_agent.py` 是默认 agent 的循环，加四个机制：
    - (a) 全文读取：`read_full` 一次至多 20 个只读 GET，全文分页，每页 ≤ `max_output_chars`，后一页从本次尝试的缓存读；结果被截断、有下一页或只有 id/摘要时，附读取提示。
    - (b) 第一次写之前用一次 aux 调用做证据表，写操作对着表重发。
    - (c) W* 的 hook 之后，用一次 aux 调用逐条核对本次实际执行的写与发送；有缺口时送回，至多两次。
    - (d) 每次调用请求 effort high。
    - 另外，W* 的包逐字节复制到 `wstar_package/`。
  - runner `examples/automationbench/ceiling_run.py` 与配置 `claude-ceiling.toml`：
    - 配置：v3 preset，`claude_code` 的 actor 钉在 effort high，两臂同用；根 $150 名义、硬停（初稿 $30，后改 $60，评审后定 $150，见本条末）；并发 8；每题每臂 2 次。
    - 复用的现有路径：`bench._cycle_config` 冻结计划与根，`open_activity`、`startup_checks`，`Drafts.check`（与 R 的候选同一检查），`build_candidate`；每臂每划分一个不安装的 TaskEvalPlan，加 permit、候选 release、`run_arm` 与 `task_gate.refill`，与 train 侧筛选同一路径。
    - 任务只从冻结文件读：核对划分 hash，dev 题在 dev，train 题在 train，不许有 test。
    - 重复之间交替两臂的先后，免得同一臂总在最后跑。硬停在中途时，有的题两臂读数不全，结论为 incomplete。
    - 输出：逐题逐臂的成败、失败断言类型、每次尝试的成本与判定规则的数字，写 `<ws>/ceiling/result.json` 与 `result.md` 并打印。
    - `--checks-only` 不调模型、不开根、不跑尝试。
  - 判定规则在运行前写进 ROADMAP 第 4 步与 ceiling/README：
    - F 全部读到且 |R| ≥ 4 时，|X| > |R|/2 且 |X| > |B| 判“搜索”，否则判“训练准备”。
    - |R| < 4 判 inconclusive；有题没读到判 incomplete。
- 证据：
  - 草稿检查：两个包在 Docker worker（`ouroloop-worker:py3.12`，镜像 `9ace6dbc112a`）里离线通过，用的是 v3 preset 的包上限、Marketing train 初始状态与 v0.2 train 判分的字面量（2,761 个）、第一道 train 题作 fixture。手写包 6 个文件约 41 KB；W* 包 4 个文件，重算的 bundle ref 等于 v0.2 记录的。检查过程中改了两处：
    - 第一版草稿被字面量检查拒绝：7 个四位以上的数字常量（20000、4000 等）恰好出现在 train 题的初始状态里。改写成乘式后通过。
    - aux 调用被拒时，代理本来会自己吞掉错误；改为不捕获。现在 manifest 去掉 `aux` 时，smoke 报未声明的角色，机制不会悄悄关掉而让手写包读成 W*。
  - 离线重放 W* 在这 13 题上 25 次尝试的 576 次工具调用（不调模型，只量输出长度）：最长 7,689 字符，宿主的 20,000 截断一次也没有触发。所以在这个世界里，(a) 能起作用的只有 `read_full` 与分页/摘要提示。
  - 新增 `tests/test_ceiling.py`，9 个测试：
    - 两个包在 ProcessWorker 与 Docker worker 里通过草稿检查；去掉 aux 角色时 smoke 失败。
    - 判定规则的各分支。
    - `--checks-only` 不调模型。
    - 脚本化模型下两臂各跑 2 题 × 2 次并写出表；同一命令续跑不再付费。
    - 任务表含 test 题或划分不符时拒绝。
    - 冻结文件与 W* 包的 bundle ref 可复算。
    - 根太小时硬停，结论为 incomplete。
  - 全套：`1095 passed, 1 deselected`（3 分 45 秒，含 Docker 的测试；上一条 1086，本条加 9 个）。
- 因此改了什么：
  - 任务数与文档不同：文档写“4 道 dev 题”，按多数读数或最近读数实为 6 道，另加 7 道 train，共 13 题、52 次尝试。
  - 费用估算约 $40（$34～47）。依据是 v0.2 的 W* 在这 13 题上每次 $0.50（medium），加 effort high 与手写包的 aux 调用。这高于 ROADMAP 原写的 $10～20，也高于初稿 $30 的根：每次调用预留 $7.28，根里可花约 $22.7，多半停在第二轮重复，结论为 incomplete。协调者随后把根改为 $60，评审后定为 $150（见本条末），并把 worker 镜像按 Dockerfile 干净重建（Docker Hub 放开；`ouroloop-worker:py3.12` = `c7005dd5c421`，runner 摘要 `a79b6f9d…` 与宿主一致；旧的叠层镜像留作 `ouroloop-worker:overlay-step3`）。
- 证据的边界：
  - 只证明接线与记账，不证明任何机制有效：脚本化模型的行为是夹具定的。
  - 没有对真实配置跑 `--checks-only`：它会 bootstrap `workspace-claude-ceiling`、把当时的镜像 id 冻进 K，并执行 `claude auth status`，留给运行前。镜像已由协调者按 Dockerfile 重建（`c7005dd5c421`），两个包在它上面的草稿检查也已离线通过。
  - W* 从未跑过的 41 道 train 题，不知道它在上面是否也失败。
- 评审（协调者，各评审在各自的副本里；真实工作树未动）：任务选择、W*、划分 hash、`wstar_package` 的 bundle ref、两臂条件相同，都复算无误。确认 6 处问题，均已离线修正（未调用模型，未读 `.env`，未提交）：
  1. 根与限流（两条）：
     - 问题：每次调用预留 $7.2768，等待预留发生在尝试的 900 秒之内，所以同时在飞的调用数 = floor((根 − 已花 − unknown 数 × bound) / bound)。$60 时最后几条线（W* 的第 2 轮重复总在最后）只有 2～3 个槽位：被 seconds 截断的尝试会记成 W* 失败，偏向“搜索”；一次 Ctrl-C（每个被杀的 `claude` 子进程按 $7.28 记 unknown）就可能让结论成为 incomplete。
     - 修正：根定为 $150 名义（钱的护栏是关掉 extra usage 的订阅，预计花费仍约 $40）。`claude-ceiling.toml`、ceiling/README、ROADMAP 第 4 步按这条规则重写根的说明。
     - runner 运行前的提示改为拿估算与“根 − bound × (1 + 并发)”比较（`ceiling_run.py` 的 `run`）。
     - 改正 docstring 里“硬停时两臂重复数相同”的错误说法（`ceiling_run.py:23-31`）。
     - result.json/result.md 每臂列出被硬上限截断的尝试（按维度，`limits`）。
  2. （高）(b) 把只读的 POST 当成写：Google Ads `googleAds:search`、HubSpot `…/search`、Notion `/v1/search`。13 题里有 4 题会在读到数据之前就建证据表。
     - 修正：加 `READ_POST = re.compile(r"[/:](search|searchStream|query|find)$")`，`_is_write` 对这些路径返回 False。规则锚在路径末尾，`:findOrCreate…` 仍算写。核对的写清单与证据表的触发随之不含搜索（`ceiling_agent.py` `_is_write`、`CHECK_PROMPT`）。
     - 包内测试 `test_a_search_or_query_post_is_a_read_and_the_first_real_write_waits_for_the_table`：GET 文档 → POST `googleAds:search` 与 `contacts/search` 照常执行 → `campaigns:mutate` 被压住建表；核对的写清单里只有 mutate。
  3. (a) `_page_key` 把 `{"next": {"after": null}}`（HubSpot 每次列表/搜索都带）当成有下一页，正好在 event_registration_sync 决定建不建记录时给出假的“还有下一页”。
     - 修正：嵌套的标记只在含非空值时算（`_nonempty`）。
     - 包内测试 `test_an_empty_next_page_is_no_page_signal`：空的 HubSpot 搜索不再给提示，`{"after": "abc"}` 与 `has_more: true` 仍给。
  4. 基础设施失败的题对会永远未读：
     - 修正：runner 计划的 `max_infrastructure_retries` 1 → 3（`INFRASTRUCTURE_RETRIES`）。
     - 运行前定下包自己崩溃怎么算。查 `task_eval.run_attempt`：运行壳下 agent 程序在运行时抛错，worker 答 kind "package"，变成 `AgentRunError`，记为 invalid、failure_class infrastructure、invalid_reason `harness: AgentRunError: …`，照常补跑；装配失败记 `candidate_defect`。
     - 规则：每个（题、臂、重复）按最后一行读。最后一行因本臂的包崩溃而无效，算该臂失败一次（`package_crash`、`final_rows`、`tabulate`）；只有环境失败（provider、worker、预算）留作未读。写进 ROADMAP 第 4 步与 ceiling/README 的规则。
     - 测试 `test_a_crash_of_the_arms_own_package_is_a_failed_attempt_and_an_environment_failure_leaves_the_pair_unread`：覆盖包崩溃 4 次仍崩、provider 失败后补跑通过、worker 崩溃 4 次未读、`candidate_defect`、seconds 截断的列表。
  5. ROADMAP 第 4 步的 inconclusive 一行加上“F 全部读到且”，并写明判定顺序：先 incomplete。
  6. 本段。
  - 证据：两个包在重建的镜像 `c7005dd5c421` 上重跑草稿检查，离线通过（手写包 6 个文件 44,927 字节，包内测试 9 个）。`--checks-only` 不调模型的测试仍在，并加上“$30 的测试根不出提示”。runner 测试核对每条线的计划 `max_infrastructure_retries` 为 3；硬停测试核对提示按 1 + 并发份预留比较。全套（前台）`1096 passed, 1 deselected`（3 分 49 秒；上次 1095，加 1 个）。
- commit：未提交。

---

## 2026-10-05 v0.3 第 3 步三复核：4 条

- 做了什么：协调者在各自的仓库副本里第三次验证（真实工作树与快照逐字节相同）：上一条的 18 处修正成立，每处撤回都让其测试失败；`run_worker_rounds` 改为 `while True` 后，对抗序列（丢失与采集交替、只丢失、只采集、50 轮混合）都停下，付费会话数有界；副本全套同为 1083。另确认 4 处，由协调者直接改：(1)/(3) 同一问题——Anthropic provider 的回复自带原生内容并在下一轮原样回放，若其正文里写出的 JSON 调用被读作调用，紧随的 tool_result 无对应的 tool_use，API 以 400 拒绝、活动暂停为 repair_runtime；改为有原生内容的回复不读正文调用、回以提示语（`research/session.py::_calls_of`），纯文本 provider（manual）照旧；(2) 续跑的 `loop.counters_of` 未把丢给基础设施的轮次计入连续失败数，续跑后可多付一轮；改为与运行中一致：计入失败数、不计 idle，暂停记号归零；`test_campaign.py` 的 RC6-17 例期望由 (1, 0) 改为 (1, 1)；(4) 包自己的异常文本含孤立代理字符时，草稿检查的错误写事件会 UnicodeEncodeError 终止本轮；`Drafts.check` 统一把错误转成反斜杠转义的严格 UTF-8（会话路径与 v2 路径都经此）。
- 证据：新增 `test_research_session.py::test_a_call_written_in_the_text_of_a_native_tool_use_reply_is_no_call`（假 Anthropic 客户端，断言每个 tool_result 都对应前一条助手消息里的 tool_use；纯文本 provider 仍读正文调用）、`::test_a_draft_check_error_is_strict_utf8`、`test_cycle.py::test_a_resumed_run_counts_rounds_lost_to_infrastructure_as_the_live_loop_does`；三处修正在隔离副本里各自撤回都让对应测试失败。
- worker 镜像：按 `docker/worker.Dockerfile` 从头重建仍被 Docker Hub 限流（`python:3.12-slim` 元数据 HEAD 429）；现用的 `ouroloop-worker:py3.12` 是在缓存镜像上叠加新 `worker_runner.py` 的层，基础层、用户（65534:65534）、entrypoint 与 Dockerfile 构建的相同，runner 摘要 `a79b6f9d…` 与宿主文件一致（每次 `load` 都核对，不一致即 `StaleWorker`）。注册表放开后应按 Dockerfile 重建一次。
- 全套：`1086 passed, 1 deselected`（前台）。
- commit：见 git log（第 3 步提交）。

---

## 2026-10-05 v0.3 第 3 步再复核：18 条

- 做了什么：协调者在各自的仓库副本里再验证（真实工作树未动）：上一条的 25 处修正成立，副本的全套同为 1060，30 个示例对象存储里的 33,409 个 Record 对象都重算回各自的名字；另确认 18 处问题、驳回 3 处。全部改掉，规则有变的先改文档（M4 §1/§2/§5，M0 §3 与 M4 §2/§6 的过时行号，08 §1a 的测试状态，M3 §2.2 与 README 第 5 步的待实现标记）。
  - 原生内容按 provider 名存（HIGH，`research/session.py`）：会话把回复的 `native` 存成裸列表，而 `agent/loop.py` 存 `{provider.name: content}`、`providers/anthropic.to_anthropic` 按名 `.get`——研究调用落到 `anthropic` 时会话第二次模型调用就 AttributeError（`marketing.toml` 用 anthropic，研究调用会回落到它）。现在按研究完成接口的 `name` 存（`CodingSession(provider=…)`，轮次取 `ctx.researcher.llm.name`）；一个回复里只要有一次调用或正文不是严格 UTF-8，就不存原生内容，下一次请求由转义后的正文与调用重建。
  - worker 的 `work`（`agent/worker_runner.py`）：写入预留回答外壳的固定长度（`EXPORT_WRAPPER`，`if line > LINE_BUDGET - EXPORT_WRAPPER`），路径相对 realpath 后的候选目录计——在边界上被接受的目录整份导出仍在行预算内，多一个字符就被拒；W 的提示文件 `worker/system_prompt.md`、`worker/finish.md` 不计入 K 的包字节上限（与 `harness_package.check_shape` 一致）但计入行预算，删除它们被拒（“to clear a prompt, write it empty”）。宿主侧取回候选目录失败（`WorkTree.fetch` 得 `ok: false`）时——首次取回、`if not kept` 的原因、修订后取回三处——该候选 `invalid` 带 worker 的错误，不再以 KeyError 抛出本轮；台账的 declared 行允许 `invalid`。
  - 会话的边界：一个候选的声明只要有一处不合法就不再构建（类型错的 `differs_from`、`predicted_affected`、`notes` 曾以 TypeError/KeyError 抛出 `improve()`）；文本里的 `tool_calls` 只有是列表才读，`{"tool_calls": 1}` 得到提示而不是 TypeError；结构化路径只收 `ToolCall`——没有任何 provider 产出 dict 形式的调用（anthropic、openai_compat、claude_code、manual 都给 ToolCall），所以删掉了 dict 分支，而不是给它补测试。
  - 轮次与运行（`research/loop.py`）：运行中，`infrastructure_round()` 认得的一轮（筛选尝试全部丢失的 `inconclusive`、gate 的完整性损失、`installation_failed`）在同一轮位上重跑：不占 `schedule.worker_rounds`、不加 idle，连续基础设施失败数加一，既有上限照旧停下（不会无限循环）；一支的 `worker_round_limit` 同理。本轮最新 train 测量里没有失败题时，会话不开，本轮以 `needs_collection`、`next_action=collect` 结束（“no train task failed in the latest measurement: …; the session was not opened”），走既有采集路径；`SESSION_FORMAT` 加一句“候选在当前失败的 train 题上筛选”。续跑开始时补写缺失的 gated 行：筛选 `selected`、带 experiment id、没有 gated 行、Execution 已在终态的条目，从终态记录的收据经 `gate_ledger_fields` 补写。`lost_to` 只出现在 `lost` 的筛选上，取自丢失的那些尝试（补跑成功而被取代的尝试不算）。筛选行结局的校验移到 `experience.ledger_row`（所有台账行都从这里生成）。
  - 测试与文档：`ledger_candidate_of` 的回退过滤（跳过已 gated、跳过带别的 experiment id 的条目）有了单测；`lost` 的测试按 `infrastructure`/`evaluator` 参数化；“没有失败题的候选不筛”断言一次筛选尝试都没跑；`thin_screen` 全处统一为“失败集（笔记任务 ∪ predicted_affected，∩ 最新 train 测量仍失败）少于 3 题；通过样本不计”（08 IT6-32、ROADMAP 第 3 步、本日志第 3 步一条、M4 §2、`session.py` 模块说明）；`bench.py` 的 `HARNESS_TABLES` 注释写明方向；M4 §2 与 §6、M0 §3 去掉 “bench.py:1083-1086 拒绝 ≠1” 与过时行号，改用符号名（`improvement_settings`、`_cycle_tables`、`_beyond_preset`）；08 §1a 逐行写明测试状态（IT6-29/30/43 已实现，第 1 步；IT6-36 第 5 步待实现；IT6-37 可选、发表时待实现；IT6-41 待实现，未排进第 1～5 步），M3 §2.2 与 README 第 5 步的测试名加待实现标记；上一条“30 个工作区”更正为“30 个对象存储（15 个工作区，加 campaign pair 的 15 个存储）”。
- 结果：
  - 新测试 14 个函数、23 个用例（`test_package_worker.py` 2；`test_research_session.py` 4——两个参数化共 10 例；`test_research_round.py` 7，另把 `lost` 测试参数化多 1 例；`test_cycle.py` 1），改 3 个既有测试：`test_a_gate_finished_on_resume_writes_its_gated_ledger_row`（续跑之后那一轮在已装 W 下没有失败题，现在是 `needs_collection`）、`test_a_segment_paused_on_infrastructure_resumes_after_the_repair` 第二组（一次损失在原轮位重跑，暂停要等连续损失到上限，序列多一次损失）、筛选补跑测试（断言补跑后没有 `lost_to`）。
  - 回退检验：17 处修正逐条改回，点名的测试全部失败。worker 镜像用叠加层刷新了 `worker_runner.py`。彩排干跑数字不变（会话 6、筛选 6、台账 6 条）。
  - 全套：1083 passed, 1 deselected（3 分 44 秒，前台，含 Docker 的隔离测试与彩排干跑；上一条 1060，本条加 23 个用例）。
- 证据的边界：同前两条；Anthropic 的两回合由假的 Messages 客户端答，没有真实调用，费用 0。
- 与再复核决定不同之处：结构化路径的 dict 分支删掉而不是补测试（见上，决定把这一选择留给我）；`_within_caps` 的外壳按完整回答 `{"op": "result", "value": {"ok": true, "value": {}}}` 的长度预留，比实际多留 2 个字符（空字典的花括号），边界测试按这个常量写。
- commit：未提交。

## 2026-10-05 v0.3 第 3 步复核：25 条

- 做了什么：协调者复核第 3 步评审的 26 条修正（每条测试都能杀死它的回退），另确认 25 处问题，全部改掉；规则有变的先改文档（M4 §1/§2/§5 与 preset 汇总、M5 §1 与 EX6-17、M6 §6 的夹具说明、M7 §4/§5、08 IT6-32、README 与 ROADMAP 第 3 步）。
  - worker 的 `work` 操作（`agent/worker_runner.py`）：协议行按转义后的长度算——`json.dumps` 的 ASCII 转义让控制字符占 6 个字符、基本平面外的字符占 12 个，磁盘字节数不是行长；写入与编辑另查候选目录转义后的总长不超过 `LINE_BUDGET` 900000（宿主 `MAX_LINE` 1000000），`work` 的回答发出前再量一次，超了改答 “the answer would exceed the protocol line; read it in pieces”。`read` 分页：一次至多 `READ_CHARS` 131072 个字符（`offset`/`limit` 按字符），后面还有时写明剩几个字符与下一次的 offset，一页转义后仍超行预算就折半；`inputs/ledger.json` 随活动增长，也这样读。路径至多 32 段、1024 字符（1500 层的路径曾在 `os.makedirs` 里 RecursionError、以 kind "package" 出去）；`work` 分支捕获全部 `Exception`（`SystemExit`/`KeyboardInterrupt` 照旧不在其内）；路径必须是严格 UTF-8（`surrogateescape` 的 `\udc80`–`\udcff` 会建出原始字节文件名）。
  - 会话（`research/session.py`）：宿主在执行任何调用前查工具名、id 与参数能否严格 UTF-8 编码，含孤立代理项的调用不执行、作工具错误，会话记录只写安全化的工具名与错误、助手消息存安全化的副本（此前 worker 答了 ok:false 之后，会话事件写盘时 UnicodeEncodeError 让本轮崩溃）；`read_file` 带 `offset`/`limit`（工具说明与 `SESSION_FORMAT` 写明 131072 字符一页）；一次 `finish` 里同一 `dir` 只能声明一次（`SESSION_FORMAT` 写明）；`_calls_of` 在三条路径上都把参数原样交给工具（`dict("c1x")` 曾在解析时抛错）；`TrajectoryReader.read` 的 offset/limit 不合法时先记一笔（0 字节、拒绝原因）再答。续跑补 gated 行时 `ledger_candidate_of` 先按 experiment id 找，只有不带 experiment id 的旧条目才按 `proposal_ref` 找（取最近一个未付 gate 的）；`loop.resume_unfinished` 传入 experiment id——提案 ref 是内容地址，同一 R 输出与缓存笔记会让不同轮的候选同 ref，首个匹配会把 gated 行落到更早的候选上。
  - 筛选（M4 §2）：净翻转只在失败集有有效 after 读数时才算——失败集为空的候选不付筛选，失败集没有读数的候选不再凭通过样本记 `screened_out`（那会成为重复机制的触发项）；读不出时按原因分：失败集为空、构不出、任一尝试 `candidate_defect`、其他原因的无效 → `invalid`；失败集的尝试（补跑后）只因 `infrastructure`/`evaluator` 无效 → 新结局 `lost`，筛选行记 `lost_to`；任一尝试 `budget_exhausted` → 本轮 `budget_exhausted`。一轮里没有任何候选有读数且其中有 `lost` 的，本轮 `inconclusive`、原因以 “infrastructure:” 开头，与 gate 的完整性损失同样处理（重跑、计入失败上限、不算完成的轮次）。筛选行结局共六种，`SCREEN_OUTCOMES` 在写筛选行时校验；`ledger_rows` 折叠 `lost`，投影带 `lost_to`。
  - 研究位与兼容：重启的一支，复制来的测量行名为公共 release，分支自己的采集批次记在起点 release 下，`last_measurement` 把它们分开；`own → start` 改写时把这些批次并入测量（`_run_collection` 的 `held` 让重启后补完的批次不重复计）。`CampaignPlan.candidates_per_round` 改为 `int | None = None`（None 读作 1、不进 payload）：六个示例工作区里存着的 campaign 计划（workspace-claude-full、-full-final、workspace-rehearsal、2、3、4）此前读回来 ref 都变了，现在不变；对 `examples/*/workspace*` 下 30 个对象存储（15 个工作区，加 campaign pair 的 15 个存储）里的每个 Record 对象逐一重算 ref，只有这六个 campaign 曾经不符，`ExperimentProposal.declaration_ref`（None 不进 payload）、core、plan、proposal 等全部一致；`schedule` 是原样 JSON，字段在第 3 步前就有。
  - 测试与文档：IT6-32 把技能声明在 memory 之前，证明平局按成本而不是按顺序；M4 §5 去掉过时的 `<scope>/r<round>/<dir>`；描述落选候选的各处改为 `not_selected`/`screened_out`/`invalid`/`lost`（08 IT6-32 并删去过时的 “bench.py:1083-1086 拒绝 ≠1”、05 §1 与 EX6-17、`test_harness_loop.py` 与 `session.py` 的 docstring）；M4 preset 汇总与 M7 写明 `research_session` 与 `screen.passing_sample` 只能调低、`screen.min_net_flips` 是下限只能调高；M7 的测试清单换上已实现的 `test_paid_run_safety.py::test_concurrency_is_a_machine_setting_the_frozen_plan_does_not_carry`，第 5 步的名字标“待实现”；README 与 ROADMAP 第 3 步改为“实现完成，退出条件部分达成”并写明阻塞点；本日志上一条把筛选行结局的计数更正为五种。
- 结果：
  - 新测试 17 个函数、25 个用例（`test_package_worker.py` 2、`test_research_session.py` 4、`test_research_round.py` 9——其中一个参数化 9 例、`test_rsi_integration.py` 1、`test_campaign.py` 1），改写 5 个：`test_the_work_tree_refuses_a_write_past_ks_byte_caps_and_every_answer_fits_a_protocol_line`（字节上限远高于行时只靠转义长度拒绝，含 emoji 与控制字符，以及超行的回答被拒发）、IT6-32（声明顺序）、`test_the_screen_builds_on_the_pointer_not_on_the_previous_gates_parent` 与 `test_the_segment_after_an_install_runs_its_session_under_r1s_propose_text`（各换一个只在本测试里用的 actor，让 W1 或分支的内层 train 池仍有失败题——没有失败集就没有筛选；后者因此用回真实的 `min_net_flips` 1，并断言分支列出、读到复制来的 W0 train 失败轨迹）、`test_paid_screens_are_on_the_ledger_when_a_later_screen_ends_the_round`（加 b 的 `proposal_ref`）。
  - 每条修正都做了回退检验：逐条把修正改回去，跑点名的测试，25 处回退全部让测试失败（`except Exception` 那一条起初只被 Docker 测试的旧镜像检查“杀死”，补了一个畸形 `init` 的 AttributeError 断言后由行为测试杀死）。worker 镜像用叠加层刷新了 `worker_runner.py`。
  - 全套：1060 passed, 1 deselected（3 分 40 秒，前台，含 Docker 的隔离测试与彩排干跑；上一条 1035，本条加 25 个用例）。彩排干跑在新规则下数字不变（会话 6、筛选 6、台账 6 条，C 的 memory 仍是 `screened_out`：它的失败集有读数、净翻转 0）。
- 证据的边界：同上一条，全是离线与 fixture，没有真实模型调用，费用 0。`lost` 与 `inconclusive` 的轮次在 fixture 里由改写筛选行的 failure class 造出，没有真实的基础设施故障。
- 与复核决定不同之处：（1）失败集为空的候选不付筛选、直接 `invalid`（决定只说“失败集没有有效 after 读数 → invalid”，不付钱是同一规则下省下的一步）；（2）任一筛选尝试是 `candidate_defect` 的候选也记 `invalid`，与 gate 对候选缺陷的判法一致（决定没写这一类）；（3）“丢给基础设施”的候选单列结局 `lost`（决定只说不是 `invalid`，没给名字），不触发重复机制；（4）有候选有读数、另有候选 `lost` 的一轮照已有的读数挑选或 `screened_out`，不判 `inconclusive`——决定只覆盖“没有任何候选有读数”；（5）读页在 131072 字符之外再按转义长度折半，所以任何一次读都放得进一行，而不是让超行的读答错误。
- commit：未提交。

## 2026-10-05 v0.3 第 3 步评审：26 条修正

- 做了什么：第 3 步工作树的评审提出 44 条（确认 39、驳回 5），归并为 26 条修正项，全部落地；规则有变的先改文档（M4 §1/§2/§5、M5 §1、M7 §2/§5、08 §1a、ROADMAP 第 3 步），再改代码与测试。
  - 会话协议（A1～A5，`agent/worker_runner.py`、`research/session.py`）：`work` 操作里的文件系统与编码错误（`FileExistsError`、`IsADirectoryError`、`UnicodeEncodeError`、磁盘满……）一律答成该工具的 `{"ok": false, "error": "<Type>: <msg>"}`（`WORK_FAILURES`），目标是目录、父段是文件在碰盘前就以 `WorkError` 拒绝，从不是 `kind: "package"`（那会让宿主当作包的缺陷、崩掉本轮）；`WorkTree.init` 带 K 的 `max_package_bytes`（缺省 262144）作目录与单文件上限，超限的写与编辑是工具错误，所以 `read`/`export` 的协议行永不超 `MAX_LINE`；修订改文件不改声明——修订的 `finish` 对同一目录改报另一组 (mechanism, site, carrier) 时候选 invalid（“the revision redeclared …”），台账行、声明对象、`declaration_ref`、提案、准入与筛选读同一份；`screened` 行带 `experiment_id` 与 `proposal_ref`，gated 行的字段抽成 `session.gate_ledger_fields`，`research/loop.resume_unfinished` 续完的 gate 也经它补 `gated` 行，`screened` 行在 `finally` 里补写（结局 `interrupted`，折叠为 pending），后一个候选的额度耗尽或 worker 崩溃不再丢已付的筛选；工具参数不是对象 → 工具错误、占一回合；同一回复里 `finish` 被接受之后的调用不执行（答 “not performed: the session finished”），已接受的声明不因它们失效；`read_trajectory` 的 offset/limit 守卫加 OverflowError；会话记录里每次调用的 `bytes` 取读者的计数；worker 侧以 `newline=""` 读写，CRLF 文件原样往返、没碰过的文件不进 diff；`finish` 拒绝空白或非字符串的 mechanism、site、trigger_condition。
  - 分支里 R 的输入（B6～B7，`bench.py`）：`_copy_measurement` 把公共 train 测量的结果行与运行清单（经 `_copy_readings`）连同 transcripts 复制进分支自己的存储，分支的 `TrajectoryReader` 能解析 W0 的 train 尝试（此前分支里每次 `read_trajectory` 都被拒、`trajectories.json` 为空）；dev 池的 `_copy_readings` 连 grading 行一起复制（`copy_grading`），分支的 dev 失败类型直方图不再是 `{}`。
  - 轮次与筛选（C8～C12）：`screen_build` 与 `train_screen` 的父 release 取 `releases.active()`——与会话输入、Experiment 同一来源，不取上一次 gate 留在 `_parent_release` 里的值（此前第一次安装之后每次筛选都建在 W0 上、gate 却建在 W1 上），`handlers()` 开轮时清掉它；筛选尝试像 gate 的臂一样经计划的 `refill` 补跑一次；没有任何有效读数的候选净翻转为 None → 结局 `invalid` 并写原因，不记 `screened_out`；筛选行的结局 `selected`/`not_selected`/`screened_out`/`invalid`，加挑选之前的占位 `interrupted`，共五种（复核时更正此处的“四种”；复核又加 `lost`，见上一条），`REPEAT_OUTCOMES` 仍是 rejected 与 screened_out，准入答 collect/repair_runtime 折成 `blocked`；失败集 = （笔记任务 ∪ `predicted_affected`）∩ 最新 train 测量里失败的题，写了却已通过的 `predicted_affected` 留在声明里不进失败集；第一次筛选之前一次性查全部已准入候选的筛选尝试加 gate 的验证预留（`budget.affordable`），容不下则在付任何筛选之前以 `budget_exhausted` 结束。
  - 台账与准入（D13～D14，`evolve/experience.py`）：候选 id 改为 `<scope>/d<诊断序号>/<dir>`（诊断序号即 `Diagnosis.round_index`，跨段、跨重跑唯一），行 tag 带 `ordinal`；`proposal_ref` 从写它的 `admitted` 行读，不从 `drafted` 行读。
  - preset/K（E15～E16）：`CampaignPlan.candidates_per_round`（缺省 1，从主线 schedule 冻结）传到分支的 Schedule，v3 下分支的会话拿到 k=3；`_preset_harness` 的嵌套表按键分方向——`screen.min_net_flips` 是下限（调低即放宽，拒绝），其余是上限——并有下界（`research_session.*` ≥ 1、`screen.passing_sample` ≥ 0、`min_net_flips` ≥ preset），无 preset 时畸形表（未知键、非表、非整数）给具名的配置 ValueError，两条分支共用 `_harness_table`；`research_session`/`screen` 按出现与否而非真值（空表头即以缺省上限要求会话）。
  - 测试（F17～F21）：`pick_best` 直接单测（最高净翻转不看列表顺序、平局取低均值成本、成本未知排在已知后、再同按声明顺序、低于门槛不入选）；真实轮次里声明 dev 题的候选被准入拒绝（CT7-08，证明 `train_tasks` 到了 `_admit`）；`declaration_reasons` 的 `how` 与被指机制同词（规范化后）不算写出、指向无关行不满足 `differs_from`；`finish` 给 k+1 个候选时第 k+1 个 dropped、不草稿不筛选；`test_harness_loop.py` 第 704 行的注释改为 good 赢在成本。
  - 文档（G22～G26）：ROADMAP 第 3 步“退出条件部分达成”与阻塞点（`rehearsal.toml` 的 v1 preset 不进会话路径；v3 preset 的 `min_tasks` 25 容不下 16 题）与补齐所需；07:90 与 04:40 写明 `max_edits` 计 `write_file`/`edit_file`/`delete_file`；07:58 与 05:19 去掉“reader 今天拒绝 ≠1”并引测试名；07:208 三个不存在的 `test_report` 名明标“待实现”；M4 §1/§2/§5 的实现段写入上述规则。
- 结果：
  - 新测试 20 个（`test_research_session.py` 5、`test_research_round.py` 12、`test_bench_phase.py` 1、`test_package_worker.py` 2），改 2 个既有断言：`test_rsi_integration.py` 的分支断言改为直接读分支的存储（复制来的 W0 train 行与 transcripts、reader 解析成功、直方图有类型、k=3——该夹具的内层 train 池没有失败题，轮次列不出可读轨迹，所以 `trajectories.json` 为空是对的）；`test_rehearsal_world.py` 的主线台账从 3 条改为 6 条——旧断言能过正是 D13 的症状：segment-1 的 `r*/c1` 覆盖了 segment-0 的同名 id。Docker 干跑在新 id 下的台账：`d0/c1` 技能 rejected（成本项）、`d1/c1` 包工具 installed、`d2/c1` memory screened_out、`d3～d5/c1` memory installed；会话 6、筛选 6 与其余数字不变（REHEARSAL §8）。
  - 全套：1035 passed, 1 deselected（3 分 41 秒，含 Docker 的隔离测试与彩排干跑；上一条 1015，本条加 20 个用例、改 2 个）。一个教训记下：pytest 作为 shell 的后台作业（`( … ) &`）启动时继承 SIGINT = SIG_IGN，Python 便不装 `default_int_handler`，`test_automationbench.py::TestConcurrency::test_an_interrupt_lets_running_attempts_finish_and_starts_no_more` 的 `_thread.interrupt_main()` 就什么都不做而报 DID NOT RAISE——三次后台全套都只失败这一条，前台全套全绿；该测试与它的代码本条未改。没有真实模型调用，费用 0。
- 与评审决定不同之处：C12 落成第一次筛选前的一次性 `affordable` 检查，`_verify_bound` 本身不加 k 份筛选（它只算 gate 的预留）；`train_screen` 仍是轮次级事件（`rsi/events.jsonl`），不是 Experiment 的 operation——筛选时还没有 Experiment，只有被选中的候选冻结进去。
- commit：未提交。

## 2026-10-05 v0.3 第 3 步：R 作为编码会话、台账、k 候选、筛选、输入

- 做了什么：按 M4 §1/§2/§5、M0 §3、M5 §1～§2、M6 §2～§4、M7 §2/§4 的 v0.3 规则，把 R 的汇总步换成每轮一个有界编码会话，并加上台账、k 候选、train 侧筛选与三项新输入；只在 K 带 `research_session`（package_api v2 的核心，v3 preset）时启用，v1 preset 与第 3 步前冻结的工作区仍走汇总调用。
  - 会话（`ouroloop/research/session.py`）：宿主驱动模型循环（`PolicyResearcher.session_turn`，经 `_Completions.turn`，计量标签 `session`），`/work` 树在 worker（`agent/worker_runner.py` 新操作 `work`：`init/list/read/write/edit/delete/export`，写只许 `candidates/<dir>/`，首次写入以包副本建目录）；工具 `list_files/read_file/write_file/edit_file/delete_file/read_trajectory/finish`，无 shell；上限 40 回合、24 次编辑、`read_trajectory` 单次 65536、累计 524288 字节，到顶即结束、本轮 invalid；修订续写同一会话；`finish` 的声明按 `candidate_declaration.v1` 读，不合法作为该调用的错误回给 R。每轮一条 `research_session` 事件（回合、编辑、每次工具调用、轨迹读取次数与字节）。
  - 声明与 k 候选：`ExperimentProposal.declaration_ref`（默认 None，旧 ref 不变）指向 `declaration` 对象；固定代码按候选目录对当前包的 diff 核对 carrier/site（memory/skill/prompt/hook/tool/agent），不符记 `declaration_mismatch`，同 (mechanism, site) 的后者记 dropped，超过 k 的记 dropped；W 的提示文本以 `worker/system_prompt.md`、`worker/finish.md` 出现在候选目录里，改它们即 `carrier=prompt`。每个候选自己的草稿链（`Drafts.check`，`max_draft_repairs` 按候选计）、准入（核心检查 + `declaration_reasons`：`repeated_mechanism`、`predicted_affected` 限 train）。`bench.py` 的 `max_candidates_per_round` ≠1 拒绝改为 1..preset（v3 为 3、缺省 3），`_beyond_preset` 对 schedule 的 `candidates_per_round` 只拒高于配置。
  - 筛选：`Improvement.train_screen`（arm `train_screen`，split train，一次尝试，自己的 permit 与计划），任务 = 笔记来源的失败题 ∪ `predicted_affected`（train）+ 固定通过样本（`passing_sample` 5，按 `order_seed` 抽，一轮一组）；净翻转 ≥ `min_net_flips` 1 的最高者付 dev gate，同分取均值成本低者再按声明顺序；都不满足本轮 `screened_out`（计入 patience）；失败集（笔记任务 ∪ predicted_affected，∩ 最新 train 测量仍失败）少于 3 题；通过样本不计的标 `thin_screen`（定义 2026-10-05 再复核时统一）；候选的 Experiment id 在筛选时定下（`screen_build(proposal, experiment_id)`），筛选的 W 与 gate 的 W 同一；安装后的下一轮测量按运行身份（`RUN_LABELS` 之外的字段相等）复用筛选读数（`screen_readings`），不再付。
  - 台账（`evolve/experience.py`）：每阶段一行 `kind="ledger"`（declared/drafted/admitted/screened/gated），`ledger_rows` 折叠成每候选一条带 `outcome`；gated 行的 fixed/regressed/still_failing 由 `task_gate.receipt_differences` 从收据结果对象读出；`ledger_projection` 给 R（dev 只以三类题数出现）；两支 E0 经 `strip_dev_task_ids`；报告加 `ledger` 与 `sessions` 小节（`report._ledger_report`）。
  - 输入：`TrajectoryReader`（只接受有效 train 尝试的 attempt id，经结果存储判 split；dev 答 AccessDenied 并记录）；`dev_failure_histogram`（合并对照规则下已装 W 的 dev 读数，按 attempt 读 grading 的 dev 行，只计失败且未排除断言的 `type`）；台账投影；原有笔记、覆盖、基础工具、经验投影照旧，全部进 `/work/inputs/` 与会话第一条消息。
  - 回用：`RESEARCH_STEPS["research.propose"]` 加 `session`，`_reuse_fact` 据此认 R1 的会话请求。
- 与设计的出入（都写进 M4 §1/§2/§5 的“实现”段）：模型循环在宿主、文件在 worker（设计写“会话在 worker 中运行”）；多一个 `delete_file`；候选目录由首次写入建立；prompt 载体的文件形式；`differs_from` 为 `[{ref, how}]`；`thin_screen`（失败集（笔记任务 ∪ predicted_affected，∩ 最新 train 测量仍失败）少于 3 题；通过样本不计；再复核时统一）；平局规则；会话草稿事件带 `session: true` 不续跑（续跑重开会话）；`train_screen` 是轮次级事件而非 Experiment 的 operation（筛选时还没有 Experiment）；`_verify_bound` 不加 k 份筛选（筛选前逐候选查 `affordable`）；`repeated_mechanism` 作为固定检查放在核心检查旁，不在 R 可配置的 preflight 里。
- 结果：
  - 新测试 34 个（`test_research_session.py` 6、`test_research_round.py` 15、`test_harness_loop.py` 1、`test_bench_phase.py` 1、`test_diagnosis.py` 3、`test_report.py` 1、`test_cycle.py` 1、`test_campaign.py` 2、`test_rsi_integration.py` 1、`test_paid_run_safety.py` 1、`test_package_worker.py` 2），改 3 个既有断言（v3 preset 的 harness 多出两张表；`it6_16` 的提示文字多了 `session`；`br6_01` 的拒绝文字）。测试研究员是 `tests/rsi.py` 的 `SessionScript`（按计划以工具调用写文件再 `finish`）。
  - 退出条件：离线干跑（`examples/rehearsal/dry_run.py`，v3 preset 按 16 题的世界调低 `min_tasks` 8、`min_headroom_tasks` 2，尝试串行，Docker 真实 worker）在新 R 路径上走到：segment-0 第 0 轮 E 的技能过筛、gate 被成本项拒绝（每次尝试 0.001582 对允许 0.001134：读技能多一次模型调用），第 1 轮 F 的包工具装入（Docker 草稿检查链），第 2 轮 C 的 memory 筛出（净翻转 0）；研究位开位，两支各装一个、W* 不同，外测 7/8 对 4/8 accepted，R1 装入；segment-1 在 R1 下三轮各装一个 memory，回用 verified（3 轮会话 12 个请求）；补跑两次有效；Docker 修订回路一次；主线 6 个会话、6 次筛选，hook 事件 53、包工具 32、技能读取 25。`test_rehearsal_world.py::test_the_expected_path_runs_end_to_end_offline` 核对。
  - 1015 passed, 1 deselected（3 分 40 秒，含 Docker 的隔离测试与彩排干跑；上一条 981，本条加 34 个用例、改 3 个）。一次中途运行里 `test_gate_record.py` 的一条因编辑中的 `session.py` 改变了 `code_version()` 而失败，无改动重跑全绿。没有真实模型调用，费用 0。
- 证据的边界：全是离线与 fixture；会话没有在真实模型下跑过（`claude_code` 的结构化输出会给出 tool_calls，manual provider 的回复要写成工具调用，CLI 彩排留待下次）；筛选与台账的数字来自假 actor 的世界；rsi 夹具唯一有缺口的家族是研究位测试的外层池，该夹具的两支把 `screen.min_net_flips` 调成 0 才能装入（M6 §6 注明）。
- 因此改了什么设计：M4 §1/§2/§5 各加“实现”段并把 §6 的测试名定为实现名；M0 §3/§5 的 `declaration_ref`、`candidate_declaration.v1`、CT7-07/08 测试名；M1 §1 的 `work` 操作；M5 §1 的 `train_screen` 事件与 §5 的未实现清单；M6 §6 的测试名与夹具注；M7 §2 表格去掉“尚未进 preset”、§4 台账字段、§5 测试名；08 §1a IT6-31/32/39 的测试名与 §3c 的干跑说明；README 第 3 步、ROADMAP 第 3 步、REHEARSAL §8。
- commit：未提交。

## 2026-10-05 v0.3 第 2 步评审：26 条修正

- 做了什么：对第 2 步工作树的评审确认 26 条（提出 31、驳回 5），逐条改。规则层面只有一条：**合并对照的完整性**——原实现“池非空就不跑对照臂”，一题读数短缺（基线一次坏尝试、筛选臂的部分读数、新测对照中途崩溃再续跑）会让此后每个 gate 都 inconclusive；改为池是起点、按题补齐（`task_gate.shortfall` 算出每题缺 `repeats − 已有读数` 次的配对，`evaluate_task` 只为这些配对跑对照臂，经同一 refill 与同一运行身份，续跑找回已记录的；空池即全部补齐，同一条路径；`Improvement._verify_bound` 用同一个缺项数；筛选臂 `screen_subject`/`screen_control` 不入池，本 gate 自己计划 ref 下的早先对照读数保留在池里）。补齐的配对取该题池里没有的重复索引（`shortfall`），同一运行续跑时不会把已记录的那对再要一次、计两次。边界：`confirmation_repeats` 只取 0/1，economy gate 不与确认同用，`regression_drop` 在开区间 (0, 1)，收据 `readings = 1 + confirmation_repeats`；`_beyond_preset` 也查计划级的 `control_pooling`/`confirmation_repeats`（省略或放松即拒绝）；`_cycle_slot` 的检查顺序改为池 → 余量 → 预算（原来预算先查，记成 budget_exhausted），`no_slot_pool` 的理由写实际不成立的那条不等式。并发：笔记在应答到达时立即缓存（并发用 `as_completed`），一条失败或中断先缓存已应答的再抛出；研究计量器的 429 按轮记 arm `research` 的 `rate_limited` 事件；臂的事件改在 `finally` 里写；`in_flight_cap` 的说明与 M5 §2“并发的范围”改为两支先后跑、同一进程、同一上限。测试：在飞上限的测试改为两个真实的 `run_tasks` 调用方；新增 `test_report.py::test_each_arm_shows_its_cost_per_attempt`。文档：ER6-18、EX6-15 改成第二版规则；ROADMAP/DESIGN 的“≥0.5”改“超过”；M5 §2 第 1 步的三个不存在的测试名改为实现的；M3 (d) 的测试说明改为“既有测试不改”；M3 (a)(b) 的缺省写法与 §2.3 一致；M7 preset 表加 `control_pooling`、`concurrency`，final repeats 改 3（定值），第 3 步的行标“尚未进 preset”，BR6-24 限定 v3 preset；M6 与 `bench.py` 的 v3 campaign 表注明 arm_share 0.15 是占位；examples README 与 `aa_replay.py` 写明读数分布在三个工作区；M3/M5 里的行号指针改为符号名。
- 结果：981 passed, 1 deselected（3 分 30 秒；上一条 973，本条加 8 个用例、改 3 个、移 1 个到 `test_cycle.py`）。A/A 回放的数字不变（dev 0/20、test 0/12）：合并对照的补齐不改变 5 次读数两两配对的裁决。
- 证据的边界：补齐规则只在 fixture 上跑过（对照臂第 2 次尝试处崩溃再续跑）；研究计量器的 429 记录用脚本模型造了一次拒绝。
- commit：未提交。

## 2026-10-04 v0.3 第 2 步续：确认复测规则第二版

- 做了什么：上一条的 A/A 回放接受 4/20，退出条件未达成，按第 2 步的约定回到 M3 §2.1(b) 改规则。决定：确认读数单独对合并对照也须清过 `min_gain`（不再只是 ≥ 0）——候选要在两次独立读数上各清过一次 gate；其余不动（第一读数清过 `min_gain` 与退步规则；退步规则与成本项按合并读数判；合并增益照记为 `metrics.gain`；`kind`、`first_reading`、`confirmation_gain` 的形状不变；`min_gain` 0.05 已定）。先改文档（M3 §2.1(b) 第二版一段与依据、(h)、§2.3；DESIGN §、M5 §1、M7 §1/§2/§4、M8 IT6-33、README、ROADMAP 的措辞），再改 `judge` 的确认条款（`ouroloop/evolve/task_gate.py`：`confirmation_gain < gate.min_gain` 即 rejected，理由写两个数 `the confirmation reading alone has gain X, below the plan's min_gain 0.05`；接受理由写“on the first reading, on the confirmation reading alone and on the pool of both”），改受影响的测试（原“复测 < 0 拒绝”的用例改为“复测恰为 0 拒绝”，加“两次都清过才接受”含阈值恰为 0.05 的边界，`test_aa_replay.py` 加真实记录形状的用例）。
- 依据（写进 M3 §2.1(b)）：同一个 W 再读一次，对合并对照的期望正好是 0，所以“≥ 0”约束不了任何东西；合并对照之下每次读数的噪声约等于单次读数的 sd（25 × 1 时 0.06～0.076），无改动时一次读数 ≥ +0.05 约 0.22，两次独立读数都 ≥ +0.05 约 0.05 ≈ 1/20；真实 +0.10 时两次都清过的功效约 0.64、+0.15 约 0.88、+0.05 约 0.25——放弃的是 test 分区本来也证实不了的小增益，换的是误接受率。
- 结果：同一回放，**dev 0/20**（第一读数仍清过 4 组，都是 48% 的读数作候选；确认读数 36% 对合并 36% 都是 0 < 0.05），加成本项 0/20；**test 0/12**（第一读数清过 3 组，确认 +0.022、−0.043、+0.022）；第一版规则的 4/20 与 2/12 留在上一条与 M3 §2.1 末。退出条件达成。第 2 步实现里没有别处依赖“≥ 0”：`Releases.install` 只看 kind 与计划的 `confirmation_repeats`，`confirming` 状态机与续跑只看状态与 attempt id，`evaluate_task` 只传 `first`，都未改。
- 证据：973 passed, 1 deselected（3 分 28 秒；上一条是 971，本条加 2 个用例、改 1 个）；回放不调模型，费用 0。
- 证据的边界：功效数字是按 sd 0.06～0.076 的正态近似算的，没有在 5 次以上的读数上核过；两版规则的差别只在这 5 + 4 次 W0 读数上看过。
- commit：未提交。

## 2026-10-04 v0.3 第 2 步：gate 统计与速度

- 做了什么：按 M3 §2.1、M5 §1～§2、M6 §2 的 v0.3 规则改 gate 的统计与运行的速度，不改第 1 步的运行壳。M3：`GatePlan` 加 `regression_drop`、`cost_beta0/1`、`min_tasks`，`TaskEvalPlan` 加 `control_pooling`、`confirmation_repeats`，`EvaluationReceipt` 加 `kind`（`full`/`confirmed`）——全部缺省 None、读作今日规则，`Record.payload` 与 `GatePlan.to_json` 不写缺省值，所以旧计划、旧收据的 ref 不变，同一张 `[freeze.task_plan]` 表再冻结是同一个 ref；v3 preset 带设计的值（pooled、1、0.5、0.10/2.0、25），`_beyond_preset` 只许加严。`judge` 按 (h) 的顺序判：合并对照的完整性按每题读数数（≥ repeats）而不是配对键；退步按“下降超过 `regression_drop`”（ER6-19 的 1.0 → 0.5 不计，设计稿的“≥”会计入，文档随之改）；成本项 `cost_subject ≤ cost_control × (1 + β0 + β1 × gain)`，缺价按 `missing_cost_policy`，收据 `metrics.cost_per_attempt {subject, control, allowed}`；确认读数并入候选的逐题均值，`first_reading`、`confirmation_gain`、`readings`、`control_readings`、`control_attempt_refs` 进 metrics。`evaluate_task` 在合并计划下有池就不跑对照臂，第一读数 accepted 且计划要求确认时告知 `confirming` 回调、再读 `confirm` 臂（candidate `confirmation`，自己的运行身份，续跑按自己的 attempt id 补缺），收据 kind `confirmed`。合并对照的归属（`Improvement.control_pool`）：读数的 RunManifest 去掉 candidate/role、计划 ref、预算 scope、release 与 R 后与本 gate 对照臂的相同，且读数自己的计划（有的话）条件相同；基线的 dev 运行、历次 gate 的对照臂、装入后的候选/确认臂都在池里。M5：状态机加 `confirming`（evaluated → confirming → evaluated），`execute_experiment` 向接受 `confirming` 的 evaluate 处理器递回调，临时 `evaluated` 带 `provisional: true`，续跑不重写；`_verify_bound` = 候选臂 × (1 + confirmation_repeats) + 没有池时的对照臂；`Releases.install` 对 `confirmation_repeats > 0` 的计划只认 `confirmed`；`MeteredProvider.rate_limited` 数未计费可重试的拒绝，`run_arm` 每臂记一条 `rate_limited` 事件，报告 `run_cost` 列出；`summarise` 与报告都给 `cost_per_attempt`；`task_eval.in_flight_cap` 进程内一个信号量；`Diagnosis.concurrency` 并发写笔记，经 `PolicyResearcher.note_with_record`（每次请求带自己的记录），缓存与顺序在主线程按冻结顺序；计量器的步骤标签按线程。v3 preset `concurrency` 8（`preset_concurrency`，没有 preset 仍 1），`ouroloop bench --concurrency N` 只覆盖本次运行。M6：`_cycle_slot` 先查两半各 ≥ `min_tasks`（`no_slot_pool`）、再查余量（`bench.slot_headroom`，按合并对照规则数起点 W 从未通过的内层题，`no_headroom`）、再查预算，结局进 `rsi/cycle.json` 与报告的 C4；内层 gate 带模板 gate 的全部条款；合并计划下公共输入多取一次起点 W 的内层 dev 读数并连 run manifest 复制进两支（`_copy_readings`）；两支的内层计划带 `control_pooling`/`confirmation_repeats`；v3 preset `worker_round_limit` 3、`min_headroom_tasks` 6。
- 结果：
  - 新测试 24 个函数、25 个用例（`test_task_gate.py` 12、`test_paid_run_safety.py` 3、`test_claude_code_provider.py` 1、`test_diagnosis.py` 1、`test_cycle.py` 5（余量那条参数化为 2）、`test_aa_replay.py` 2），改 2 个既有断言（v3 preset 的表按设计不再等于 v1 的；研究位现在会存一个与主线相同的 gate 对象）。
  - A/A 回放（`examples/automationbench/aa_replay.py`，用 `judge` 本身）：W0 的 5 次 dev 读数（36%、36%、36%、36%、48%）给 20 个有向对（候选读数、确认读数；对照为其余 3 次合并），新规则接受 **4/20**，旧规则 4/20，加成本项 4/20；都是 48% 的读数作候选（第一读数 +0.12 → 合并 +0.06 ≥ 0.05，确认读数 0 ≥ 0）。test 的 4 次读数 12 组：新 2、旧 3。五次 dev 读数每次尝试成本 $0.366～$0.390。**退出条件（≤ 1）未达成**。
- 因此改了什么设计：M3 §2.1(d) 的“≥”改为“超过”（ER6-19）；M3 §2.3 的缺省列改为 None/今日规则；M3 §2.1(a)(b)、M5 §1～§2、M6 §2 各加“实现”一段（归属规则、确认臂身份、`kind` 缺省、公共 dev 读数、余量的读法、在飞上限、`rate_limited` 事件、`note_with_record`）；M5 final 份额的并发检查与 M3 §2.2 的 best-of-k 臂标为未做；README 第 2 步与 M8 IT6-33/34/40 的测试名改为实现的名字；ROADMAP 第 2 步记实现与“退出条件未达成”。规则本身没有改：按第 2 步的约定，多于 1 组就回到 M3 §2.1 改规则，那是下一个决定。回放说明的是，确认读数“单独 ≥ 0”对一次偏高的读数没有约束力，合并点估计只把偏高减半。
- 证据：971 passed, 1 deselected（3 分 27 秒；第 1 步末是 946）；回放不调模型，费用 0。
- 证据的边界：规则的功效只在 5 次 dev 读数与 4 次 test 读数上看过；并发 8、`rate_limited` 与笔记并发没有在真实模型下跑过（第 5 步前的 16 路试跑才有数）。
- commit：未提交。

## 2026-10-04 v0.3 第 1 步：agent 程序与运行壳

- 做了什么：按 DESIGN v0.7 与 M0/M1 的 v0.3 规则，分四个切片把宿主从“跑固定循环的 agent”改成运行壳。A/B（`16bbbf4`）：包 API v2——manifest `harness_package.v2` 加一个 `agent` 条目（`agent/<file>.py:run`、声明的 roles），agent 代码进包身份并使包“运行代码”；五项单次尝试硬上限（`max_steps`、`max_model_calls`、`max_tool_calls`、`max_seconds`、`max_tokens`）进 Budget、冻结计划与 TargetHarness，新冻结缺一项拒绝、旧计划原样读；K 冻结 `max_effort`（preset high，配置只能调低）；worker 协议加 `run`/`smoke`、模型回调、贯穿整次尝试的 `serve`、`LimitHit`。C（`c44d75f`）：网关 `ctx.model`（角色核对、effort 截顶记请求值、先扣上限再调模型、宿主写 `assistant` 加 `gateway_call`——messages 以前缀+尾段、tools 以名字+摘要、`tool_specs` 在变化时一条）、broker `ctx.tool`（拒绝表、基础工具与只读检查、`broker_event`）、`ctx.skills`/`ctx.limits`/`ctx.view`/`ctx.mark`；今天的循环移植成 `agent/default_agent.py`（种子包，只用标准库与 ctx），v1 包由它运行——含代码的整个在 worker 里（加载时随包发入同一份字节），只含文本的在宿主进程内；每个含代码的草稿经 smoke（罐装模型一回合 tool_use）；worker 的 `load` 答 runner 摘要，与宿主不同即 `StaleWorker`、启动检查点名；只有 K 带 `max_effort` 的 release 走新壳，旧工作区继续 v1 循环。D（本条）：彩排的离线检验 `check_world.py` 改为每次尝试走网关、broker、默认 agent（hook 与包工具用干跑装入的同一份源码）；新 preset `automationbench_harness_v3`（v1 的数值 + API v2 + `agent` 种类 + `max_effort` high + 五项上限作 `[bench.budget]` 缺省，只能调低）；R 的包 API 文本改写为 v2（manifest、`agent` 条目、ctx 的形状、默认 agent 的兜底、`ctx.mark` 只是自述）；报告的能力包一节从宿主记录读出 agent 入口与放置（runner 新写的 `agent` 事件）、按角色的调用数与 token、effort 截顶次数、触顶维度；M0 §2、M1 §1/§4/§5、README、DESIGN §5、ROADMAP、REHEARSAL §8 对齐实现，过期的 `agent/loop.py` 行号改为按符号指向 `default_agent.py`/`gateway.py`/`runner.py`。
- 结果：
  - 彩排世界在新壳下原样复现 §10 的全部数字：W0 只过 9 道 P 题；A/B/D 的规则各翻转本类 3 题，C/E/F 的规则一题不翻；B 的技能、E 的技能、F 的包工具各翻本类 3 题；收尾检查 hook 翻 A～D 共 12 题；armed fault 记 `ProviderError`、重试通过、文件用尽；没有一次尝试触顶。每回合一条 `gateway_call`（hook 变体的 002 题 4 回合、2 条 `broker_event`、2 次 hook 答复、1 次回推），工具清单只写一次 `tool_specs`。
  - Docker 干跑（`dry_run.py`，v3 的 K）在新壳下走到 segment-0 装入、两支 W* 不同、外测 accepted、R1 装入与回用 verified，hook/package_tool/read_skill 事件都在，两次补跑有效。
  - 与设计的出入已写进文档：ctx 是五个名字加 `mark`（不是严格五个）；默认 agent 的字节不进镜像，随包在 `load` 时发入，等价由构造保证，镜像校验落在 runner 摘要；默认 agent 不保留 bash 重跑（业务工具面没有 bash，也没有模型可问）；`gateway_call` 不带 `cost_usd`，费用照旧按 `assistant` 的 usage 入账；smoke 覆盖每个含代码的草稿，不只带 `agent` 条目的；网关对形状不对的调用答错误、记 `protocol`，不抛到宿主。
- 因此改了什么设计：M1 §1 增“放置”“分派”“草稿 smoke”三段与 runner 校验的做法；M0 §2 的记录块按实现重写（前缀/尾段还原规则、`tool_specs`、`agent`/`end`/`protocol` 行、“包的自述”）；M1 §4 的 Inspection 条目落在报告而不在 `research/inspection.py`；`trajectory_ref`、`policies_read`、effort 逐次分布、PR7-10 的专门用例留第 2 步；v3 preset 暂只 Marketing，多域在第 5 步。
- 证据：946 passed, 1 deselected（含 Docker 的 IT6-23、PR7-01/08 真实镜像与彩排干跑）；`check_world.py` 的 JSON 由 `test_rehearsal_world.py` 六个测试核对。没有真实模型调用，费用 0。 真实出口检查：v3 preset 下 1 道 dev 题经运行壳跑通，7/7，记录形状与 M0 §2 一致，API 等价 $0.41。
- 证据的边界：全是离线与 fixture；运行壳下还没有跑过真实模型的一道题（ROADMAP 第 1 步退出条件里“真实模型下 `--phase smoke` 一题、网关日志对账到 `cli_cost_usd`”在 08 §3 另列，付费前做）。
- commit：`16bbbf4`、`c44d75f`，切片 D 见本条之后的提交。

## 2026-10-04 v0.2：完整活动，含研究位

- 做了什么：在 `workspace-claude-full`（名义根 $900）跑完整 cycle：基线、segment-0、研究位、segment-1；中途加入一臂内并发（`[bench] concurrency = 4`，`run_tasks` 线程池，共享状态加锁，审查 7 条全修）；final 两次。
- 结果：
  - W* = W0 + 写前规程（segment-0 第 1 轮，dev 48%→80%）+ 结束前对账 hook（segment-1 第 1 轮，dev 68%→84%）。
  - 研究位 inconclusive：R0 提出的 R1 按新规则换了改动位置，但两支在 12 题内层 gate 上都没有净增益，两支 W 相同，外测不跑，C4 无证据。
  - final-1 中止：$50 份额放不下 4 路并发的 4 份预留上界（实际只花 $23），51 次记 budget_exhausted。计量器改为预留被拒且有调用在飞时等待（新增测试）。同一比较以 final-2 在对象库副本工作区重跑。final-2 之后新测试时有失败，查出等待逻辑里的一处顺序错误：在飞计数先减、再结算，被唤醒的等待者会撞上未结算的预留而误判预算用尽；改为先结算再减计数，连跑 15 次通过。final-2 没有触发过这条路径（份额 $120，无一次拒绝）。
  - final-2：W0 47.8%、W* 52.2%，+0.043 [−0.109, +0.196]，C1 inconclusive。同一 W0 两次 final 相差 6 个点。两活动按题平均 +8.7 [−2.2, +20.7]。
  - 并发：每次尝试墙上时间 88 秒→27 秒，无无效尝试。
- 因此改了什么设计：M5 §2 并发与预留余量规则；final 份额按两臂预期花费加一份上界定；并发不进计划身份。
- 证据：882 passed, 1 deselected；747 次尝试、6,165 次模型调用、名义约 $373；`docs/RESULTS.md`。
- 证据的边界：dev 的 gate 读数（84%）与 test（52%）差距很大，gate 数字是过程量；23 题 test 只能看出大效应；C4 未测出，不是被否定。
- 下一步：让研究位的内层 gate 有足够尝试分辨小效应；考虑更大的 dev 或多次重复。
- commit：见本条之后的提交；版本 v0.2.0。

## 2026-10-04 v0.1：final 上的同模型增益

- 做了什么：按预先提交的计划（`663f930`）在 test 23 题上比较 W0 与 W*，每题 2 次，用 final 份额。
- 结果：
  - W0 通过率 41.3%、W* 54.3%，配对差值 +0.130，95% 区间 [+0.022, +0.261]：C1 成立。partial 从 0.844 升到 0.881。
  - 逐题：6 题变好，1 题变差（vendor_management 2/2→1/2），16 题持平。每次尝试的成本 $0.437 对 $0.425，轮数 9.5 对 8.8。
- 证据：final 92 次尝试、845 次模型调用、$39.64；全程（基线、segment-0、final）349 次尝试、3,114 次调用、名义 $153.30。见 `docs/RESULTS.md`。
- 证据的边界：一个模型、一个领域、一个被接受的改动；test 只有 23 题，区间宽。递归研究（C4）没有运行，属于下一版。
- 因此：写 `docs/RESULTS.md` 与两份 README 的结果一节；全套测试通过后合并进 main，打标签 v0.1.0，之后继续在 `m1-automationbench` 开发。
- commit：见本条之后的提交。

## 2026-10-04 segment-0：三轮演化，一次接受

- 做了什么：在 `workspace-claude` 跑 `--phase cycle --until segment-0`。R0（Claude Opus 5.5）读 train 失败的摘要写逐题笔记，汇总出提案，在 dev 上与对照比较。
- 结果：
  - 第 1 轮：截断 hook，gate 拒绝（36%→40%，增益 0.04 未过 0.05）。R 把失败摘要里的截断标记 `[... N characters cut ...]` 读成了 actor 漏读，而那是给 R 的摘要按条目截的，actor 拿到的是全文；hook 看视图里的 `truncated`（摘录比结果短），在 170 处给出“你没看到”的假提示。
  - 第 2 轮：把 `worker.finish` 的结束前检查改成五项核对清单（来源、截断、逐条指令对应工具调用、逐行处理、不留烂尾），gate 接受：dev 36%→64%，置信区间 [+0.12, +0.44]，无退步，成本基本不变。装包后重跑 30 道 train 失败题，13 道通过。
  - 第 3 轮（改正后的摘要格式）：R 转向“采信了不该采信的指令”，提出写前核对的 `before_tool` hook，gate 拒绝（60% 对 60%，2 题退步）。
  - W0 在 dev 上三次都是 36%，W* 两次是 64% 与 60%。
- 因此改了什么设计：
  - 摘要里的截断标记写明“是摘要截的，agent 有全文”；包 API 写明 hook 看到的结果是截短的，actor 拿到的是完整结果（`29fbc34`）。
  - 第 2 轮 gate 之后停进程，用新代码重启，第 3 轮起生效：这一段的第 1、2 轮用的是旧的摘要格式。
- 证据：segment-0 共 180 次尝试，无无效尝试；`bench/marketing/report.md`。
- 证据的边界：dev 的增益是 gate 选出来的，有选择偏差；对外数字看 final（test 23 题，计划已于 `663f930` 预先提交）。
- commit：`29fbc34`（截断说明）、`663f930`（final 计划），结果见本条之后的提交。

## 2026-10-03 Claude Opus 5.5 的 train+dev 基线

- 做了什么：在 `workspace-claude`（名义根 $300/$50）跑 `--phase cycle --until baseline`，即 W0 的 77 题；后台运行，108 分钟。
- 结果：
  - train 22/52（42.3%），平均 partial 0.827；dev 9/25（36.0%），平均 partial 0.830。77 次全部有效，无暂停。
  - 失败集中在最后的汇报：train 失败断言里 gmail 占 65 条。dev 的失败有三类：该发邮件时改发 Slack 或没发；新旧规则或追踪编号选错版本；数字、措辞不准。
  - 这些都是任务判断，train 与 dev 的失败形态相同，演化循环有东西可学。
- 证据：670 次模型调用，账本名义 $32.35，等于 CLI 报价；`bench/marketing/report.md`。
- 证据的边界：每题一次（repeats 1），dev 25 题；得分是“Claude Opus 5.5（effort medium）经 `claude -p`、JSON 工具调用”的，不与原生 tool use 的公开成绩直接比。
- 下一步待用户决定：在同一工作区跑 segment-0（`--until segment-0`，3 轮约 $110 名义），之后可跑 final；研究位要另开 $800 的工作区。
- commit：见本条之后的提交。

## 2026-10-03 Claude 四题基线（`--limit 2`）

- 做了什么：在冒烟副本 freeze 后跑 `--phase baseline --limit 2`。第一次 freeze 被拒：final 份额 $1 小于一次调用的预留上界 $7.28。名义根改为 $20（final $8）后通过。
- 结果：
  - 4 题 5 分 16 秒，无报错、无暂停；dev 1/2、train 0/2，partial 均在 0.8 以上。
  - 失败都是任务判断（邮件正文措辞、用了旧的政策编号、判定不能上线），不是 provider 的问题；需要发的邮件都正常发出。
  - 每题 API 等价 $0.22～0.50，均值约 $0.40；账本与 CLI 报价逐题相等。
- 因此：完整活动的名义根约需 $800（研究位每支约 $110，份额 15%），只跑 77 题基线约 $31；待用户决定。
- 证据：真实调用 4 题；本条 API 等价 $1.61。
- commit：见本条之后的提交。

## 2026-10-03 Claude 探针与一道题冒烟

- 探针（2 次调用，用户同意后运行）：
  - 普通问答与带假工具的结构化回复都成功，`modelUsage` 只有 claude-opus-5-5，工作目录仍为空。
  - 账本与 CLI 报价逐分相等；CLI 写的是 1 小时缓存，`[pricing]` 的缓存写改为 $8/M。
  - 模型看到的请求之外的内容只有 userEmail 一段，没有日期。
- 冒烟（`marketing.social_engagement_response`，独立工作区）：
  - 第一次 7/7 通过，但暴露两个问题：
    - 整段历史作一个文本块，前缀缓存对不上，一道题写缓存 25.4 万 token，API 等价 $2.15；
    - CLI 为结构化输出多要一次时轮数变多，被误判为截断，最终消息丢失。
  - 改了什么：actor 回合改为一条多块用户消息，经 `stream-json` 发送；结构化回复只按输出量判截断。
  - 先用 3 次小调用验证缓存：第二次起写入从 7 千降到 3 百。重跑同一道题：7/7 通过，10 轮，最终消息完整，写缓存 2.9 万、读 16.7 万，API 等价 $0.37。
- 证据：874 passed, 1 deselected；本条消耗合计 API 等价约 $3（探针两次各约 $0.01，冒烟 $2.15 + $0.37，缓存验证约 $0.08）。
- 下一步：在冒烟工作区 freeze 后跑 `--phase baseline --limit 2`（4 题），用实测重定预算，经用户确认。
- commit：见本条之后的提交。

## 2026-10-03 Claude 经订阅运行：claude_code provider

- 决定（用户）：先看本框架下 Claude 的任务得分。actor 与 R 都用 Claude Opus 5.5（`claude-opus-5-5`），effort medium，走用户 Max 订阅登录下的无头 Claude Code，不用付费 API key；GLM-5.3 与计价校准（ROADMAP §3 第 3 步）延后，不删。手动作答（本会话充当模型）不用于跑分：本会话不盲（能读到仓库、评分器与自己以前的回答），也承担不了上万次 actor 调用。
- 做了什么：新的 provider 类型 `claude_code`（规格见 M1 §1，步骤见 ROADMAP §3 第 2a 步）、启动检查、`examples/automationbench/claude.toml` 与探针脚本 `claude_probe.py`。
  - 每次调用起一个 `claude -p`：不开工具，不加载本地定制与设置文件，不保存会话，在空目录运行；系统提示换成框架给的，CLI 自己的重试与换模型都关闭。
  - R 的请求原样发送；actor 的回合把历史与工具写成 JSON，工具调用经 `--json-schema` 结构化输出。
- 对抗审查（3 个方向，27 条经核实成立，3 条被驳回）发现并已修正：
  - 订阅上限的真实措辞（“You've hit your session limit · resets …”）原先不被认作账号类错误，会把每次尝试记成基础设施失败、空耗预算；自称暂时的限流原先反被当成账号错误。
  - 拒答与输出触顶的真实形状（`stop_reason`、`api_error`）没有读；输出触顶后 CLI 会自行续写至多 3 次，`result` 只剩最后一段，原先会被当成完整回复。因此一次 CLI 调用最多 5 次请求，`max_output_tokens` 须至少为 `max_tokens` 的 5 倍。
  - CLI 自带重试默认 10 次，与计费层的重试叠加；拒答时默认换模型作答。
  - 启动检查原先接受按次计费的登录；effort 原先由 CLI 设置决定，没有 pin 住。
  - 调用并不全盲：订阅登录下 CLI 仍附上 Agent SDK 身份行、账号邮箱与当天日期，关不掉（在 CLI 2.1.288 本体中核实）。文档照实写明，actor 的格式说明写明它们不属于任务，探针核对模型实际看到什么。
  - 计价改为 claude-opus-5-5 标价，待探针用 CLI 报价核对；$300/$50 预算沿用 GLM 估算、Claude 下研究位不够，改为冒烟实测后在活动工作区打开前重定。
- 证据：874 passed, 1 deselected。离线测试用假 `claude` 程序；真实模型没有调用。
- 待用户：在 claude.ai 设置中确认 extra usage 已关闭（开着时套餐额度用完 CLI 会改用付费额度继续，不报错）；同意后跑第 2a 步的探针。
- commit：见本条之后的提交。

## 2026-10-03 R0 笔记保留接口事实与第四次彩排

- 做了什么：R0 的 diagnose 提示新增一句：汇总只看笔记，所以笔记逐字保留改动会用到的接口事实（调用的工具与端点、方法与字段名、错误原文、actor 结束消息里表明没有做完的原话），任务的名字、id 与取值仍不写。这是新的 R0（`ef6e865`；`test_diagnosis.py::test_r0s_note_prompt_asks_the_note_to_keep_the_interface_facts`）。然后经 CLI 做了一次到研究位为止的小彩排，作答只按各请求的系统提示，不再另加规定。
- 结果（`docs/REHEARSAL.md` §15）：
  - 笔记按提示带上了接口事实。
  - 包工具与 hook 只凭汇总请求写出，首检通过，被接受并被使用。
  - 研究位 accepted，外测 8/8 对 5/8。
  - 独立核对：§14 的通过条件全部满足，没有一条回复用到请求之外的世界事实；研究位 R1 与以前的措辞相近等小出入照实记录。
- 证据的边界：同前，答题人设计了世界；这次说明请求内容足够，不说明真实模型会这样写。
- 证据：850 passed, 1 deselected。真实模型没有调用。
- commit：`ba1e07b`（规格）、`ef6e865`（R0 提示），结果与存档见本条之后的提交。

## 2026-10-03 基础工具清单与第三次彩排

- 做了什么：
  - 按方案 2，研究上下文带上宿主的基础工具（名称、描述、参数 schema）。汇总请求以 `tools` 字段列出，接受它的修订步骤也拿到同一份清单；包 API 写明 `ctx.call_tool` 能调用的就是这些工具（`1c44670`；`test_cycle.py::test_the_summary_request_lists_the_hosts_base_tools`、`test_harness_loop.py::test_a_revision_is_shown_the_hosts_base_tools`）。
  - 然后经 CLI 做了一次到研究位为止的小彩排。作答逐条只用本条请求的内容：笔记写下本条失败过程里的接口细节，汇总只用汇总请求。
- 结果（`docs/REHEARSAL.md` §13）：
  - 包工具与 hook 都只凭请求写出，通过检查，被接受并被使用。研究位 accepted，外测 8/8 对 5/8。
  - 独立核对确认 13 条回复都没有用到请求之外的世界事实。
- 核对后补的：包 API 写明测试按文件路径加载、包根不在 sys.path 上；此前代码草稿的测试写法是答题人沿用前两次彩排的，请求里没有。
- 待决定：R0 的 diagnose 提示是否要求笔记写下接口细节（ROADMAP §2）。这次笔记写下这些细节，是作答纪律要求的，真实模型不一定会写。
- 证据：849 passed, 1 deselected。真实模型没有调用。
- commit：`af3e010`（规格）、`1c44670`（工具清单），其余见本条之后的提交。

## 2026-10-03 第二次彩排：技能、包工具与补跑

- 做了什么：先写规格（`docs/REHEARSAL.md` §10），再把彩排世界升到 `0.0.0-rehearsal.2`，然后经 CLI 再跑一个活动。R 作答 21 次。
  - 新增两类收尾：邮件列表只认读到的技能；通话备注只认包工具，因为端点要编码后的文本。
  - actor 可由操作者写入 `infra_once` 文件，让指定题的下一次尝试以基础设施失败一次。
  - 离线演练也随之更新，覆盖两次补跑。
- 结果：见 `docs/REHEARSAL.md` §11。
  - 装入了技能与包工具，下一轮都核到它们被提供与使用。
  - 主线 gate 与外测各发生一次基础设施失败，各补跑一次，配对完整。
  - 研究位 accepted，R1 装入，回用 verified。
  - 第一次没有注入的故障情形里，除笔记空回复外都已注入并得到预期。
- 偏差：segment-1 round 2 轮换到 D，不是注入计划设想的为 C 写 hook；修订用尽的注入改在形状检查上做，不在 Docker 中。
- 证据的边界：同第一次，答题人设计了世界，只证明接线与故障协议。
- 结果核对：独立核对员确认数字与结局都与记录吻合，但“作答只依据请求文件”不成立，事后核对发现三处例外：
  - segment-0 round 1 的包工具代码，用了只在笔记请求里出现的端点、字段名与基础工具名；
  - 候选支的 hook 按只在笔记请求里出现的字面串 `Not done:` 判断；
  - 研究位的 R1 正文逐字复用了第一次彩排的回复。

  前两处决定了工具与 hook 能否生效，所以“装入的工具与 hook 生效”只说明接线，不说明 R 能从请求里写出它们。另有 4 处措辞不准，已改。
- 由此看到的设计问题：汇总请求没有失败摘要，也没有基础工具的名称与参数，写代码组件所需的接口细节只能由笔记带过去，而 R0 的 diagnose 提示没有要求。已记入 ROADMAP §2，真实运行前决定是否处理。
- 证据：846 passed, 1 deselected。真实模型没有调用；占位计价下记账 $0.41。
- commit：`52ab218`（规格）、`9fa9c76`（世界第二版），结果与存档见本条之后的提交。

## 2026-10-03 付费前彩排

- 做了什么：按 ROADMAP §3 第 2 步，先写规格（`docs/REHEARSAL.md`），再建 fixture 世界与假 actor，然后经 CLI 走生产 cycle，R 由 Claude 通过 manual provider 作答。
  - 世界：`examples/rehearsal/upstream` 是 marketing 域的小型 AutomationBench，共 21 题。名称按 hash 落到指定划分，四类收尾步骤各有一道 train 题，在开发顺序的两半各有一道 dev 题。
  - 假 actor：只在规则出现时做收尾步骤。跟进任务只认收尾检查（on_finish hook）的推回，于是覆盖 Docker 草稿检查链，也让 R 的方法比较可能出现真实差异。
  - 离线检验（`tests/test_rehearsal_world.py`）：在独立进程里核对划分、每条规则只翻转本类的题，并用脚本研究员按预期走向跑完一遍 cycle。
- 结果：见 `docs/REHEARSAL.md` §9。
  - 主工作区：baseline → segment-0（装入 2 次，拒绝 1 次）→ 研究位 accepted，R1 装入 → segment-1 回用 verified。
  - K1、K2、K3a 都满足；免费 A/A 一致。
  - 故障注入表每行至少注入一种情形，结果都符合预期，分布在主工作区与故障工作区；没有注入的情形列入覆盖缺口。
  - actor 检查：Claude 作为被 pin 的 manual actor，在真实上游上完成了工具调用的往返。
- 证据的边界：
  - 答题人设计了这个世界，知道假 actor 认什么。这次运行只证明接线与故障协议，不说明 R 或任何模型的能力。
  - 作答依据请求文件，事后核对发现两处例外：主工作区研究位的回复引用了答题人自己先前写的放弃条件，这一条不在请求里；故障工作区研究位的回复复用了主工作区的正文，只用来检验形状。
  - R1 由答题人在研究位里提出，来源是 segment-0 中 C 的笔记被拒这一经验；研究裁决 accepted，同样只是开发采纳。
  - 这些工作区与报告不计入 C1～C4。
- 覆盖缺口：没有装入技能；没有包工具；外测补跑没有发生；两支份额占不住的路径没有发生；故障表中笔记全部读不出、笔记空回复与缺字段、汇总空回复、修订读不出与用尽没有注入。这几项只有离线测试。
- 找到并已修：
  - 研究位的 R 看到的 budget 是空的；拒答原因重复；manual 截断消息出现 “None tokens”（`ffe3aff`）。
  - 结果核对发现：研究裁决 accepted 而不显著时，收据一律写“区间含 0”，只有 1 个独立组时并不含 0；改为写明独立组不足。
- 结果核对：一名独立核对员把 §9 的每条说法与工作区原始数据对照，数字与结局全部吻合；措辞上的 2 处错误与 5 处不准已改正。
- 证据：844 passed, 1 deselected。真实模型没有调用；真实上游只在本地运行，没有读取任何 key。
- 运行数据：主工作区 R 作答 22 次、故障工作区 9 次、actor 检查 5 个回合；主工作区占位计价下记账 $0.27。请求与回复存档于 `examples/rehearsal/archive/`，回放测试尚未写。
- commit：`9b4810e`、`5a6c7cb`、`ffe3aff`，文档与存档见本条之后的提交。

## 2026-10-03 研究位前必修

- 做了什么：按 ROADMAP §3 第 1 步“研究位前”逐项实现。先由 5 名设计员各自复现问题、提出改法，对抗评审后合并，规格先写进文档（`80bbdca`），再写代码。
  - 研究裁决：
    - 按任务配对：外层任务的每次重复在两支都有有效尝试才计入，缺任一判 inconclusive。缺的尝试按失败类别记进 `metrics.incomplete`，配对的任务数记进 `metrics.tasks`。
    - 两支 W* 相同：不跑外测，判 inconclusive（`metrics.identical_arms`）；有多个 pair 时，后面的 pair 不再运行。
    - 外测补跑：基础设施失败的尝试按外测计划冻结的 retry_policy 补跑一次，原尝试保留，补跑记 supersedes。
  - 预算：
    - 外测份额（preset `outer_share` 0.08）冻结为 plan 的 `outer_budget`。它是外测 scope 的上限，并在公共测量与两支开跑前以不结算的预留占住（`BudgetAccount.hold`）。
    - 两支份额在公共测量之后同样预留，各支开跑时释放给自己。
    - 研究位开始时，根须能覆盖外测份额与两支份额（由 cycle 的研究员作答时，再加 R 一次调用），否则不问 R。
  - E0：研究位开始时主线经验冻结在配对里，两支的经验日志以它原样开头。实验记录去掉原始 gate 理由，汇总指标保留。
  - 闭包：`refs_in` 不再跟随嵌入记录自带的 ref（它等于其余字段的摘要），worker 段装过包后研究位不再 KeyError。不改任何对象的身份。
  - 研究位格式：声明前文是 R 的 worker 轮改包指令，本次调用改的是指令本身，以格式为准。
  - 读不出的研究位回复：
    - 记 invalid（stage=reply），不记 no_proposal；准入拒绝与构建失败分别记 stage=admission 与 stage=build。
    - 提案文本不再截到 8000 字符，超过 K 的提示长度上限时构建失败。
    - 没有研究收据时，C4 的 detail 按 cycle.json 写明研究位怎样结束。
- 对抗审查：4 个视角（正确性、预算与续跑、规格一致性、测试充分性）加逐条复核，14 条发现中 12 条成立，2 条驳回。修正后又对修正本身复查一轮，3 条发现中 2 条成立（其一是测试注释过时）。主要几条：
  - 只预留外测、不预留两支时，根不够的情况下先跑的对照支会把根花到不剩，R1 那支被饿死，比较照样得出裁决，而且偏向否定 R1。改为两支份额也预留，占不住就判 inconclusive。
  - 第一版改法是在两支开跑前检查一次根够不够。复查发现，有多个 pair 时，重启会对已跑完的支再查一次，可能把一个只是暂停的 campaign 记成最终的 inconclusive。改为预留，重启找回预留，不再检查。
  - 研究位只检查外测份额放得下，R 自己的这次调用之后，预留可能就放不下了。现在检查时连两支份额与 R 的这次调用一起算。
  - `proposals` 是单个对象时，研究位原先当作合法提案读，与规格不符。改为必须是列表。
  - 另有 7 处测试缺口：对照支缺尝试、某支完全没有的任务、丢一次重复、两支相同后的下一个 pair、外测跑过后重启、补跑又丢、研究位入口的边界。都已补测试。
  - 驳回的 2 条：一条是同一个 `proposals` 问题的另一视角的重复，另一条认为 ROADMAP §2 的状态未更新（本条随文档一并更新）。
  - 每项修正都做了变异实验：撤掉修正，对应的测试就失败。审查列出的存活变异，补测试后都被抓住。
- 调研中另记两个问题，见 ROADMAP §2：
  - 研究收据引用两支工作区里的 release，R1 装入后再开研究位会在 copy_closure 处崩溃。首个活动只有一个研究位，不受影响。
  - M6 §3 要求报告 research_dev 的访问历史，报告里还没有。
- 对彩排的影响（ROADMAP §3 第 2 步已写入）：
  - 假 actor 要在 segment-0 装包后，仍在两半开发家族上留有缺口，否则两支 W* 相同、外测不运行。
  - 研究位读不出的注入另开一个彩排工作区做。
  - 研究位部分的重跑从 `--until segment-0` 时复制的工作区开始。
- 证据：840 passed, 1 deselected。真实模型没有调用。
- commit：`80bbdca`、`35ab4ba`、`2966d73`、`1b5c5f6`、`9d586c2`、`55721d7`、`bef3215`、`e543852`，文档见本条之后的提交。

## 2026-10-02 阶段 3：worker 段前必修

- 做了什么：按 ROADMAP §3 第 1 步“worker 段前”逐项实现。
  - gate 收据：`metrics.regressions` 列出每道退步的任务，报告展示这份列表；经验里不带它，R 看不到。
  - 包工具：工具名须符合供应商的约束（`[a-zA-Z0-9_-]{1,64}`），输入 schema 须是对象类型且深度有界，`enabled` 必须是布尔值。供应商会拒收的工具在草稿检查里就失败。
  - 读不出的回复：截断、拒答、空回复、非 JSON 与形状非法，都不当作 R 的决定。本轮记为 invalid，不执行补采，计入轮次与 patience，不计入基础设施失败上限。拒答单列为 `ReplyRefused`。
  - 提案：
    - 不能指定 model 的 backend；改 model 必须带同一目标的训练条目。
    - 改提示的提案不能含 train 任务的字面量。
  - 暂停与结束：cycle 的每一步都按同一规则暂停或结束，不再以 traceback 退出。暂停写进 cycle.json 和段日志，CLI 以非零退出码返回。
  - 续跑：open 实验按冻结的 `eval_template_ref`、`budget_scope` 与 `parent_release` 完成；这些输入变了就记 `resume_refused` 并暂停。
  - CI：构建 worker 镜像，Docker 隔离测试在 CI 里真跑，缺镜像就失败。
- 对抗审查：三个视角加逐条复核，22 条发现中 17 条成立并已修正，5 条驳回。主要几条：
  - 冻结的 task plan 原本不带单次尝试上限。冻结后改 `[bench.budget]`，open 实验就会在新上限下完成并安装。改为冻结时写入全部上限，之后只从计划读取。
  - Anthropic SDK 的连接与超时错误在计费层重试用尽后原样抛出，halt_of 认不出，cycle 以 traceback 退出。改为计费层把供应商的任何异常包成 ProviderError。网关返回 200 却没有 choices 也算供应商故障。
  - 账号拒绝原本按异常文本判断，内部错误里带 "billing" 字样也会被当成账号问题。改为只读供应商的故障。预算不符单列为 `BudgetMismatch`，账本误用直接上抛。
  - 构建与续跑没有复查 model 规则，改为与准入共用同一检查。
  - 笔记读不出时原因被丢失，改为记下真实原因。已完成的段会被后续步骤的故障改记为暂停，改为保持原样。
  - 驳回的 5 条（复核员都做了复现，机制属实但不构成实质缺陷）：
    - 只有 requirements 的汇总回复判为 invalid；
    - 旧代码下暂停的 campaign 臂，升级后续跑被拒；
    - 工具条目加了 `enabled` 键，相同文件的包 ref 因此改变；
    - 续跑被拒后没有“关闭该实验”的入口；
    - 连续两次 invalid 以 patience 结束，被认为与无安装的负结果无法区分（记录上其实能区分）。
  - 其中第 2、3 条只影响旧代码留下的工作区；真实活动还没开始，没有这样的工作区。
  - 审查中发现一个本阶段之前就有的缺陷：worker 段安装过包后，研究位在 `copy_closure` 处必然 KeyError（嵌在 TaskEvalPlan 里的 GatePlan 从未存储）。这一条已列入 ROADMAP §3“研究位前”。
  - 每项修正都做了变异实验：撤掉修正，对应的测试就失败。
- 证据：794 passed, 1 deselected。真实模型没有调用。
- 事故：bc90a26 误提交了 `patch` 留下的两个 `.orig` 备份，已在 081ebc9 删除，`.gitignore` 也加了 `*.orig`/`*.rej`。
- commit：`f5b10ce`、`f04e75e`、`a71dd47`、`adadfc2`、`bc90a26`、`bf7e9eb`、`f1d5669`、`081ebc9`、`9a39e2d`、`969c681`、`d7c35fb`，文档见本条之后的提交。

## 2026-10-02 阶段 1：基线前必修

- 做了什么：按 ROADMAP §3 首个真实活动协议第 1 步“基线前”逐项实现。改动集中在 `bench.py`、`research/loop.py`、`evolve/execution.py` 与示例配置。
  - 基线与测量：
    - 基线的 train 与首轮测量共用一个运行身份（arm `measure`，candidate `current`）；首版活动的基线在冻结的 task plan 下运行。一个活动里 W0 的 train 只付费一次。
    - 测量先记账（`measurement_planned`，含任务清单与运行身份），重启时据此判断测量是否完整，不完整就按记账的任务补齐。
  - 补采：
    - 补采先记账（`collection_booked`）；中断的批次在重启时按原序号补完，只计一次额度。
    - 重建测量时纳入同一版本的补采结果。
  - 启动检查：
    - 按 pin 的 actor 与研究模型核对输出上限和 key，manual 免检。
    - 计划未命名时，付费阶段拒绝启动；有 `[improvement]` 时，`ouroloop freeze` 也冻结 preset 的 task plan 与 schedule。
    - cycle 拒绝多于 preset 的研究位。
  - 停止点：cycle 加 `--until`，可停在 `baseline`、`segment-N`、`segment-N/round-K` 或 `slot-N`。
  - final 分配：`[freeze.budget.final]` 在根上划出 final 的份额。
  - 示例配置：研究模型 32768 输出上限，预算 $300，其中 final $50。
- 对抗审查：三个视角（正确性、兼容、规格）加逐条复核，22 条发现全部成立，已修正。主要几条：
  - 我加的续跑计数，把“要重跑”的基础设施轮也算进失败额度。基础设施暂停后，即使修好也永远续跑不了（回归）。改为与 `completed_round` 同一口径。
  - `--until slot-0` 之后再跑，回用检查被算到 segment-0 上并写死。这本是“研究位前”的一项，停止点使它成为常规路径，于是在本阶段完成：待查项记下它对应的段，只在那一段非暂停结束时计算。
  - 停止点会遮住同时触发的 patience，或丢掉那一轮请求的补采。改为：停止点是优先级最低的停因，续跑时先补未执行的补采。
  - 显示给 R 的剩余预算没有扣除 final 份额。改为与预留共用同一套计算（`headroom`）。
  - 分配在 freeze、启动检查和打开根时都校验；根打开后，分配不能改、撤或补加。
  - 活动内若会冻结出别的计划（例如 preset 改了），cycle 拒绝运行。
  - 用改过的代码补完中断的测量或补采时，若运行身份变了就拒绝续跑。这条代替了原计划的字面量金样测试：运行身份含环境信息，换机器就会变。
  - 有几条规则删掉后全量测试仍通过，已补测试。新测试中挑了 6 个关键改动做变异实验，每个都能被对应的测试抓住。
- 证据：730 passed, 1 deselected。真实模型没有调用。
- commit：见本条之后的提交。

## 2026-10-02 复核、首个真实活动协议与付费前彩排

- 做了什么：对 HEAD `9270a7d` 做 11 个范围的实现复核。每个范围一名审计员读规格、代码与测试，一名复核员找反证，最后由综合与批评各一轮收口，人工抽查高危项。另做了 RSI 定位与业务落地评估，以及三版迭代计划的评审。全程只读，没有调用模型。复核全文见 `history/V0_6_REVIEW_2026-10-02.md`。
- 发现（复核后成立）：
  - 高危 4 项：
    - K 未冻结 methods 时，准入放行 R 提供的 `spec.backend`，构建预检经 `serving.py:42-44` 到 `config.py:77` import 任意对象。离线 PoC 已复现。
    - 两支 W* 相同时，研究裁决仍可 accepted；`test_campaign.py:96-108` 把它断言成了预期。
    - 外层比较按各支剩余的有效任务求均值，不配对（`campaign.py:341-348`）。
    - 续跑不读 Experiment 冻结的 `eval_template_ref` 与 `budget_scope`（`research/loop.py:346-356` → `bench.py:748-762`）。
  - 暂停只在 worker 轮循环里写入；collect、续跑、基线、研究位提案、公共测量与外测遇到账号错误或截断时，以异常退出。
  - 示例研究模型的输出上限 16384 不够：上限 16384 时，账本中 `linkcheck2-gate` 的两次汇总调用一次输出 16384 被截断，另一次输出 16293（只差 91）；上限提到 32768 后，同等提示规模的一次输出 13332。
  - 所有 improve/cycle/research 集成测试都注入脚本研究员；`cycle_pairs` 在测试中一律被替换；Docker 里的代码草稿检查链没有测试。
  - R 的可演化面只有 `research.diagnose` 与 `research.propose` 两段提示。preset 只计划一个研究位；cycle 没有拒绝调高的 research_slots，调高后 R1→R2 可达，但未测试、未计划。
  - 文档状态过时、行号过期；“20 次中 18 次无效”混用了口径（磁盘上 20 条 dev 记录中 19 条无效）。
- 因此改了什么设计：
  - ROADMAP §3 新增首个真实活动协议，内容包括：付费前必修清单（能落成测试的条目注明测试名）、付费前彩排、独立的计价校准、活动结构与预算（$300 根，其中 final 分配 $50）、gate 口径与免费 A/A、检查点 K1～K3 与止损、final 时机。
  - 付费前彩排采纳用户的提议，由 Claude 充当 LLM：
    - 通过 `[research.llm] type = "manual"` 读请求文件作答；
    - actor 是对包内容作出反应的确定性假 actor，任务世界是以 marketing 域名导出的小型 fixture 上游；
    - 经 CLI 走生产 cycle，按步骤注入截断、读不出、形状非法的回复；
    - 由工作区名与本日志标注为 synthetic（报告本身不带标记），不计入证据。

    它替代不了计价校准与真实运行。Claude 作答 actor 另在一个以 manual pin 的工作区里做。
  - M5 §3 按故障所在的层写定暂停与结束规则：账本额度耗尽是有界结束，不是暂停。还写定了续跑的冻结输入。M4 §1 按步骤写定读不出的回复如何处理，截断则暂停。08 §3 写入彩排的验收与边界，M6、M7 和 RESEARCH_AGENT 同步。
  - DESIGN §7 与 README 开头如实写明：首版 R 只改两段提示，属于有界、一阶的改进策略更新，对外表述为 bounded RSI 工程原型。
  - 修正 README（中英）、DESIGN、DEMO、RSI_REVIEW、ROADMAP、examples README 与 M0 中过时的状态和行号；README 的任务数由 10（7+3）改为 29（21+8）。
- 文档改动经三个视角（代码事实、跨文档一致性、协议可执行性）审查，每条发现再由一名复核员找反证：53 条中 47 条成立，均已修正。不采纳的一条修法：研究位的 system 提示不去掉 R 自己的 propose 文本，因为 R 正是用它提出对自身的修改；改由研究位格式声明以哪段为准。
- 证据：703 passed, 1 deselected（未改代码）。
- commit：见本条之后的提交。

## 2026-10-01 真实 gate 三次尝试，与首版需求审计

- 做了什么：在 `linkcheck2` 工作区新开一个 scope（`linkcheck2-gate`，$6 上限），dev 计划取冻结顺序的前 5 个任务（5 个家族），其中 1 个做筛选，gate 门槛沿用 preset。跑生产的 `bench.improve` 一轮，不设停止点；复用已测得的 train 失败（`campaign_launch_checklist`）。
- 第一次：R 看到了 W 当前的结束检查，没有再加重复的 skill，而是提议把它合并进 `worker.finish`。诊断也比上一次准：未走的那个分支上的交付物被当成“不适用”，应当无论走哪个分支都给请求者发一份状态总结。但 R 用了包补丁的形状写这个提示改动，构建判为 invalid。原因是格式只示范过包改动的形状，现已同时给出两种（`20a9ea8`）。2 次调用，$0.11。
- 第二次：GLM-5.3 把汇总调用的 16384 个输出 token 大多用在推理上，JSON 在 proposal 之前被截断；`parse_json` 遇到残缺文本返回 {}，回退逻辑把“什么也没读到”变成了 collect。现在被截断的研究回复会被拒绝：本轮以 repair_runtime 暂停，并提示调大 max_tokens；读不出的回复记为 invalid 轮；任一模型的 max_tokens 超过 `[pricing] max_output_tokens` 时启动检查拒绝（`0e9cbe8`）。3 次调用，$0.07。
- 第三次（研究模型 max_tokens 调到 32768）：
  - R 的提案有效，依次经过准入、冻结、构建、筛选和比较。
  - 从筛选的对照臂开始，智谱账号余额耗尽：每次调用都返回 HTTP 429，错误码 1113，“余额不足或无可用资源包,请充值”。
  - 20 次 dev 尝试中 18 次无效，配对不全，判为 inconclusive，未安装。这些失败按基础设施处理，没有算作候选的失败；429 的预留被释放。本 scope 合计 $0.50，没有 unknown 结算。
  - 修正：429 原先被当作限流重试 4 次，整轮比较又把每次尝试都耗掉。现在账号类错误（余额、配额、被拒的 key）单独识别，不重试；一出现就停止这一次尝试，本轮以 repair_runtime 暂停，实验保持打开，充值后重跑会从记录的状态继续，而不是判为 inconclusive。
- 需求审计（5 个审计加 5 个复核，逐项对照 docs/modules/00–09 与 ROADMAP/DEMO）：首版 354 项中，186 项实现且有测试，23 项实现但无测试，136 项部分实现，7 项缺失；约 77 项属于第二版，按设计推迟。审计还发现一个真实缺陷：研究位提案的 origin 写成了 "automatic"，不在允许值内，真实 R 的每个研究位提案都会在准入前报错，此前只有脚本研究员能走通，已修正（`8954b9f`）。明细见 `history/V0_6_AUDIT_2026-10-01.md`。
- 待用户：智谱账号需要充值后才能继续真实运行；计价口径（open.bigmodel.cn 的地址与 Z.AI 的美元标价）仍待确认。
- 证据：703 passed, 1 deselected（Docker 运行时）。
- commit：见本条之后的提交。

## 2026-10-01 真实失败路径：从失败任务到构建好的候选，修正它暴露的问题

- 做了什么：在新的独立工作区（`linkcheck2`，$5 上限，按修正后的 bootstrap 建立：K 冻结 package_api 与镜像 id，release pin TargetHarness 与 required_capabilities），依次测 train 任务，直到出现一个失败，然后对它跑生产的 `run_worker_round`。screen handler 换成停止点，所以不跑 dev、不安装。
- 真实运行：
  - 第一个任务 `marketing.campaign_launch_checklist` 就失败了：partial 0.75，4 条计分断言中 `gmail_message_sent` 未过；28 次工具调用，217 秒，$0.128。
  - 研究步骤：一次笔记调用 → 一次汇总调用，提出一个 skill（`finish_deliverable_audit`）。首稿被草稿检查拒绝（skill 条目写成了字符串），一次修订后通过。
  - 之后准入 → 冻结 Experiment → 构建候选 W（只比 W0 多了新包）→ 在 screening 停止。
  - 14 次调用，$0.210。包是纯文本，没有启动 Docker。
- 独立核对（5 个审查按链路找反证，另加一个完整性审查）：
  - 只凭记录重放 28 次工具调用，得到同样的 0.75。
  - 运行记录带 mode=collection、宿主契约与 actor 配置；冻结的实验引用的是检查过的包；什么都没安装，也没有 dev 尝试。
- R 的诊断质量（仅此一例）：笔记找对了失败的检查，机制具有一般性，不含任务答案。但根因写错：它说邮件被“无声漏掉”，实际上 agent 在最后一条消息里明确说明了不发的理由，而 R 看到的摘要只保留每条前 600 字符，恰好截掉了这段。R 也看不到 W0 已有的结束前复查指令，于是提出了与之重复的 skill。该任务本身也有矛盾：Legal 被撤销，但评分仍要求发一封含 “ready” 的邮件。
- 发现并修正（再经 3 个审查找反证，又修正了其中发现的问题）：
  - 供应商在计费层之下自行重试：GLM 客户端默认超时 120 秒、重试 4 次，Anthropic SDK 默认重试 2 次，这些请求都不入账。那次 236 秒的汇总调用按时长与缓存判断是两次请求、一次预留。
    - 改为计费层接管重试：每次请求单独预留，只重试可重试的错误，并在调用期间临时关掉供应商自己的重试，不永久改动共享的供应商对象。
    - 确认没有到达模型的失败（连接失败、429/503/529）释放预留，不再以最坏值记为 unknown。
    - 默认超时改为 600 秒。
  - 摘要改为保留每条的首尾，最后一条消息保留到任务文本的上限。
  - R 在 targets 下看到 W 当前的指令文本，提示要求修改或合并而不是重复。
  - 告诉 R 包 API：条目形状、各 hook 位置的事件字段与动作效果、ctx.view、ctx.call_tool、测试约定。拒绝信息写明需要的键；修订时给出父包。
  - 重复提案检查原先从未生效（history 从未填充）。现在按同一父版本上已有结论的实验判重，判为 invalid 而不是 repair_runtime；因基础设施失败或未能安装的实验不算重复。
  - 汇总调用读取笔记跑完之后的预算。
  - 每轮记录 R 的完整 Requirements、修订请求，以及汇总时看到的预算。
  - 复核中还发现我的一处回归：`retrieve()` 已按新到旧排序，我把 `[:10]` 改成 `[-10:]` 反而取到旧行，已改回，测试改为经过 `retrieve`。
- 仍未在真实模型下运行：gate、安装、任务中运行的包、Docker 中的代码草稿检查（R 按 preset 优先写文本组件），以及 R 的递归。
- 证据：696 passed, 1 deselected（Docker 运行时）。`linkcheck2` 中停在 screening 的实验已标为 cancelled，任何续跑都不会让它进入付费的 dev 比较。
- 观察（未改）：评分器按子串判断 Slack 中的 “ready”，一条写着“NOT ready to launch”的消息也算通过；partial credit 在这里高估了正确性。
- commit：见本条之后的提交。

## 2026-10-01 真实链路检查：GLM-5.3 跑通一条，修正它暴露的问题

- 做了什么：
  - 提交阶段 A～E（`07af0a6`）。
  - 启动 Docker Desktop，构建 worker 镜像；加了 `.dockerignore` 白名单，构建上下文只含 runner。IT6-23 首次在真实 daemon 上运行，发现包代码能看到基础镜像自带的环境变量（GPG_KEY 等，不来自宿主）。runner 改为经 `env -i` 启动，只给 PATH 与 LANG，`/proc/self/environ` 也只有这两个；另加两项只在 Docker 下运行的测试：挂住的包记为候选缺陷且容器被删除；按 K 冻结的镜像内容 id 运行（`142a3e1`）。
  - 在独立工作区（`linkcheck`，$5 上限）用真实模型跑一条：一个 train 任务，再对其结果跑研究步骤，在准入和 gate 之前停止。驱动按 `run_worker_round` 的顺序调用同样的构件。
- 真实运行：
  - 任务 `marketing.contact_data_cleanup`，GLM-5.3 通过：10 条计分断言全过，另有 5 条被评分器排除。17 轮、42 次工具调用、250 秒。prompt 251,014 token（缓存命中 222,272），输出 22,645，$0.198。
  - 研究步骤：没有失败，所以没有笔记调用。一次汇总调用返回 next_action=collect，理由是没有失败证据。
  - 合计 18 次模型调用、$0.204，全部结算，没有 unknown。
- 独立核对：5 个审查按链路逐项找反证。
  - 评分来自官方 scorer：15 条断言的判定只凭记录即可复现；什么都不做的得分为 0/10，所以 1.0 不是空通过。
  - 没有评分信息进入 agent。
  - 计费逐次复算一致。
  - R 的输入不含 dev/test 的任务 id 或评分器文本。
- 发现并修正：
  - 诊断记录的 scope 被工作集标签覆盖，笔记轮换实际从未生效。改为工作集单独成字段；R 另外拿到工作集的测量数与通过数。此前 R 把“0 个失败”读成了“还没测量”。
  - 每次调用按最坏情况预留 $1.47，但没有与预算核对。$5 预算下每个研究分支只有 $0.75，每次调用都会被拒。现在启动检查与 cycle 会在付费前拒绝。
  - R 看到的预算是阶段账户自身的空限额。现在给出按祖先范围计算的实际剩余。
  - RunManifest 未写 mode 与 target_harness_ref。bootstrap 现在创建 TargetHarness 并写入 release 的 required_capabilities，首版的运行记录 mode（train 为 collection，其余为 task_evaluation）、宿主契约和固定 actor 的配置。这些字段进入运行 hash，所以在真实数据产生前修正。
  - Docker 或镜像不可用时，K 会冻结一个空的镜像 id；允许代码组件时改为拒绝 bootstrap。
  - 包代码只挂载到合格的 worker，测试 worker 须显式声明 `for_tests`，草稿检查同样检查。
  - 由草稿生成的包记录镜像。
  - 每次研究请求记录输入摘要。
  - 测试：680 passed, 1 deselected（Docker 运行时，无跳过）。
- 未跑到的环节：因为任务通过，以下都还没有在真实模型下运行：失败摘要、逐题笔记、带补丁的汇总、草稿检查（含 Docker 中的 probe/test）、准入与 gate、安装、任务中运行的包，以及 R 的递归。
- 需要用户确认：配置用的是智谱开放平台 `open.bigmodel.cn` 的地址和 `ZHIPUAI_API_KEY`，而 `[pricing]` 引用的是 Z.AI 的美元标价。如果实际按国内平台计费，账上的美元数只是按 Z.AI 价换算的估计，不是账单。
- 观察到的行为（未改）：没有失败时 R 返回 collect，驱动会补采：优先重跑最近的失败，没有失败就重跑前几个 train 任务。文档没有规定零失败时该做什么。
- commit：见本条之后的提交。

## 2026-09-30 首版阶段 B～E 实现：隔离 worker、导出、递归比较与 cycle

- 做了什么：
  - B 代码组件与隔离（`agent/worker.py`、`agent/worker_runner.py`、`docker/worker.Dockerfile`）：
    - 包代码只在 worker 进程内执行，宿主经 stdin/stdout 协议通信。
    - DockerWorker 是唯一合格的 worker：无网络、只读根目录、tmpfs、uid 65534、去掉全部 capability、有 CPU/内存/PID 上限，按 K 冻结的镜像内容 id 运行。ProcessWorker 只供协议测试。
    - broker 只能调用本回合的基础工具，hook 的 broker 只读。
    - 宿主在 before_tool、after_tool、on_finish 三处调用 hook。before_tool 的 block 停掉本回合其余调用；after_tool 的反馈作为单独一条消息；on_finish 的回推与 StopGuard 共用上限，最后一回合不回推。
    - 包工具不得与基础工具或 read_skill 同名，草稿检查与挂载时都会拒绝。
    - 草稿检查扩展为 isolated_probe → package_test，包测试只在由 train 构造的世界里运行。
  - 故障归属：包代码抛异常、调 sys.exit、杀死或挂住 worker 时，如果新起的 worker 仍能正常应答，记为候选缺陷。只有新 worker 也不应答才算基础设施，并以 repair_runtime 暂停活动。worker 的 stderr 边读边丢弃，只留末尾；输出与单行长度都有上限；超时的容器会被删除。
  - C 导出（`release.export_deployment`、`app.py`、`ouroloop export`）：
    - 按白名单导出 W、actor 身份、包的运行部分和 K 的运行投影，不含 R、测试和证据。
    - 先在临时目录构建，扫描凭据和本地路径后再移入目标目录。
    - App 只在合格且镜像一致的 worker 上运行含代码的包。
  - D 递归比较（`research/campaign.py`、`bench.research`）：
    - 两支都跑首版循环，工作区按 campaign 分开。
    - W0 的 train 测量在 campaign 级只做一次，判分明细与轨迹复制进各支。两支第一轮的笔记请求完全一致时，笔记在 campaign 级只写一次，作为共同输入。
    - 各支逐轮记录已完成的轮次，崩溃后只补缺，崩溃前中断的实验先按记录续完。
    - 暂停的一支（worker 失联，或末轮因基础设施失败）修复后续跑到冻结轮数；暂停期间 campaign 判 inconclusive，不做外测。
    - 已出 receipt 的 campaign 不再重跑，接受后的安装只做一次。
    - R1 改过的 prompt 必须出现在候选支的实际请求里，否则判 invalid；只改 manifest 的 R1 在付费前拒绝。receipt 的成本包含两支、共同输入和外测。
    - 每轮记录加载的 R、context 和各步请求摘要（`research_steps` 事件）。研究请求按步骤（note/propose/revise/research_slot）分别记账。
  - E 用户入口（`bench.cycle`、`[improvement]`、preset `marketing_harness_v1`、`examples/automationbench/improvement.toml`）：
    - reader 只接受已实现的枚举，第二版方法按名拒绝。
    - preset 的 harness 数值是上限，`[harness_evolution]` 只能收窄；镜像由 preset 固定。
    - 允许的方法冻结进 K，准入时强制执行。
    - cycle 先冻结缺少的计划（不得放宽 preset，[rsi] 已给的计划同样检查），然后：基线一次 → worker 段 → research slot → 下一个 worker 段（检查 R1 是否真的被用上）→ 离线报告。无提案不会吞掉 slot；暂停与预算耗尽按规则停止；重跑只补缺，基线也只补缺。
    - 首版的 research slot 由当前 R 读取经验投影和自身的 prompt，提出 R 候选。
    - 报告新增：包的组成；实际技能读取、hook、包工具与 worker 时长；每次测量的范围；笔记覆盖；草稿检查；按步骤的研究费用；gate 门槛；cycle 与回用状态；公开数字背景表。首版的 C3 与蒸馏标为 not_run。
- 证据：
  - `.venv/bin/python -m pytest -q` → 667 passed, 1 skipped, 1 deselected（阶段 A 结束时为 611）。跳过的是 IT6-23 Docker 隔离测试：本机 Docker daemon 未运行，镜像也未构建。
  - 覆盖的首版用例（含阶段 A）：CT6-01/14、PR6-01/10/11/12、RL6-11/17/18/20/22/23/25/26、EX6-12、ER6-06/14/15、RC6-10/11、IT6-11/16/19/21/22/23/28、BR6-01/09/10/12/14/15/16/17。
  - fixture 上的完整 cycle：冻结计划 → 基线 → 段 0 无提案 → slot 中 R1 被接受并安装 → 段 1 加载 R1、按 R1 的 propose 文本发出请求 → 报告。重跑 cycle 不新增任何尝试或研究请求。
- 独立审查：B/C、D、E 三个审查共报 31 条（分别为 12、7、12 条，含低严重度项），复核后全部成立并已修正。需要 Docker 的三处（删除超时容器、docker 客户端环境、按镜像内容 id 运行）本机无法测试，其余都补了测试。最重要的几条：
  - 包代码自行退出或挂住 worker 会被当作基础设施，活动永久暂停。
  - worker 的 stderr 无人读取，输出多时会挂住。
  - 超时的容器不会被删除。
  - 两个 campaign 用同名配对时，后一个会复用前一个已完成的分支。
  - 分支与段的进度只在结束时写盘，崩溃后会多跑轮次。
  - 已完成的 campaign 重跑会再付一次外测费用，并在安装时失败。
  - slot 的研究门槛组数取自 worker gate，示例配置下 R1 永远无法安装。
  - slot 暂停后 cycle 仍继续下一段。
- 设计补充（写入 M7 §2）：research slot 的配对规则（开发家族按冻结顺序对半，前一半为内层 gate，后一半为外层）与每支预算比例由 preset 固定；`test_timeout_seconds` 用作 worker 每个请求的时限。
- 证据边界：全部为离线 fixture 与脚本研究员，只证明接线，不是收益。没有付费运行，没有真实模型的能力包演化或 C4 结果。Docker 隔离尚未在真实 daemon 上验证（构建命令：`docker build -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .`）。上一条记录中的未决问题（dev 回归被拒时的诊断信号）不因实现而改变。
- commit：未提交。

## 2026-09-30 首版阶段 A 实现：诊断输入与文本能力包

- 做了什么：按 ROADMAP §2 阶段 A 实现，不含 Docker、trainer 或 teacher。
  - 契约：既有记录加默认 None 的字段，旧 ref 与 v2 prompt_hash 不变；`from_json` 拒绝未知键与未知 schema_version；RunManifest 读取改为严格，新字段只在非 None 时写出。新记录 ModelIdentity、TargetHarness、HarnessPackage、CollectionRequest；新增 `generation` 绑定。
  - 目标：`worker.actor`、`worker.harness` 为可选目标，只在 K 列出时要求；完整性按 K 的 `core.targets` 校验，旧 core 与旧工作集照常绑定。`research.diagnose` 同时接受旧的 requirement_list 与新的 failure_summary.v1 → failure_note.v1；R 候选沿用父策略的契约。
  - 能力包（`agent/harness_package.py`）：固定目录形状与上限；运行判定与检查判定分开（tests 只影响检查）；K 不允许代码时任何 .py 都是结构错误；memory 未启用、清单外的技能文件、空 patch、与父包相同的 patch 均被拒绝；宿主把 memory 与技能索引放进系统提示，`read_skill` 作为宿主读取并记录。
  - 判分明细：`run_tasks` 写入 `grading.jsonl`，结果存储按冻结划分读取，默认只给 train。
  - 诊断（`research/diagnosis.py`）：只摘要 train 失败，含全部计分断言及其 passed，不含被排除的；逐题笔记的身份是渲染后的完整请求加模型配置；无效笔记不缓存；每轮读取数有上限，按全活动的诊断位置轮换；汇总调用只读笔记、覆盖、允许的种类、当前包，以及经验的固定投影（原因只保留枚举）。
  - 草稿检查（`research/drafts.py`）：冻结前检查形状、种类和字面量（邮箱/标识符/记录名，来自 train 断言 params 与 train 初始状态，按整词匹配，检查解码后的 manifest，回传只报类别数量）；初稿之后有界修订；每份草稿都存下来；空修订计为失败；重启后按记录的修订序号续跑，不再调用 R 重新提案；worker 不可用时暂停活动，草稿保持打开。
  - 驱动：v0.6 下每轮只测 train，改版后只重跑上次失败的任务，无安装则复用，重启时从记录重建；`collect` 记为 needs_collection 并指向 CollectionRequest，批次在重启后不重置，只有新执行的尝试算进展，没有后续轮次时不跑补采；预算耗尽时干净停止。
  - bench：`[harness_evolution]` 在 bootstrap 时把限值冻结进 K，把配置的模型固定为 actor（观测时间记为事件），装空包，用写笔记的 R0；release 固定的 actor 不被 `[llm]` 覆盖，前沿臂显式覆盖；基线对首版活动同时跑 train；K 允许代码或当前包含代码而没有 worker 时，smoke/freeze/各阶段在付费前拒绝；既有 v2 工作区加这个表会被明确拒绝。
- 证据：`.venv/bin/python -m pytest -q` → 611 passed, 1 deselected（原 554 条全部保留）。新增 `tests/test_harness_package.py`、`tests/test_diagnosis.py`、`tests/test_harness_loop.py`，覆盖 CT6-01/14、PR6-01/13/14、RL6-11/17–22/24–27、EX6-12、ER6-15、BR6-16/17、IT6-19/22（文本部分）/27/28。离线闭环用 fixture 世界与 scripted R：train 失败 → 笔记 → 汇总提文本包 → 首稿失败、修订通过 → gate 接受 → 安装 → 新会话挂载并读取技能，dev 通知任务由失败变通过。
- 独立审查：三个角度（规格、正确性、泄漏）共报 37 条，34 条经复核成立，已全部修正并补测试。其中最重要的几条：文本 K 下多余的 middleware 文件会挂起活动；字面量检查把普通词当答案；汇总调用读到的原始经验行含 dev 任务 id 与评分器报错；最终评测的前沿臂实际跑的是固定 actor；v2 release 的 prompt_hash 被改变。
- 证据边界：全部为离线 fixture，scripted R 只证明接线，不是真实收益；没有付费运行。（阶段 B～E 见上一条。）
- commit：未提交。

## 2026-09-30 对照 Harness-Zero 的 h* 产出流程补齐首版规格

- 做了什么：逐环节对照 Harness-Zero 的学生 harness 演化 skill（`.agents/skills/student-harness-evolve/SKILL.md`，@ aa63a77）与论文，把差距写成首版规格。诊断输入：失败的 train/fit 尝试给出失败摘要与逐条断言明细，串行做“逐题笔记（research.diagnose）→ 一次汇总（research.propose）”，产出至多一个 patch 或 next_action；dev 只给汇总，final 不进入。判分明细分存 `bench/<domain>/grading.jsonl`，由 M3 写入、M7 按冻结 split 读取。冻结前草稿检查与有界修订（初稿之后最多 3 次，纯文本草稿只查结构/路径/manifest）。组件优先级为 memory/skills → on_finish → before/after_tool → 包工具；纯文本包不需要 Docker。每轮 measure 在执行身份匹配时复用上轮 gate 的 dev 结果（只进 R 上下文），只为诊断重跑失败的 train 任务，dev gate 仍完整执行。交付分段改为 A 诊断输入与文本包、B 代码组件与隔离。首版 actor 定为 GLM-5.3；第二版换 Qwen3.5-9B 学生时 h* 在学生上重新演化。版本机制改为在既有记录上加默认 None 的字段（`Record.payload()` 跳过 None，`from_json` 拒绝未知键），不做 v3 reader 分派；RunManifest 与 RoundResult 的例外写明。
- 依据（读源码与论文得到）：Harness-Zero 每轮只评上一轮失败的 train 任务，不做回归；最多 10 个子 agent 逐题写笔记，主 agent 汇总、冒烟后才评测；组件在 agent 进程内 importlib 加载，任务环境在 Harbor 容器里；三个学生库共 12 个中间件、3 个 skill、3 个工具（工具只在 USPTO）。结果 base@h / base+h* / 蒸馏@h：SpreadsheetBench 31.0/39.0/44.0，AppWorld 26.8/48.2/58.9，USPTO 12.0/38.0/30.0。本项目现有研究上下文只给逐题 success/partial，不含断言和轨迹，且截到 12,000 字符（`research/context.py:50`、`:116-146`）。
- 一致性修正：ROADMAP 的测试数改为 554 passed, 1 deselected；那次 GLM 运行改为“14 条断言中 8 条计分、全部通过，另 6 条初始即成立、不计分”。IT6-18/20 标为第二版。新增用例 CT6-14、PR6-13、RL6-17～20、EX6-12、ER6-15、BR6-15/16、IT6-27/28；IT6-27/28 列入首版必做。
- 文档检查：本地链接/锚点、代码围栏与表格列数通过；159 个用例 ID 无重复；`git diff --check` 通过。用例是待实现规格，不是通过结果。
- 真实运行：GLM-5.3 的 train+dev 基线启动后，在第一个任务中途按“先写好文档再执行”的决定中止：结算 4 次模型调用（不到 $0.02），没有完成的尝试，不算基线；中断那次调用的预留按设计以最坏值留在预算账上。为这次运行做的判分明细存储代码已撤回，改为上面的待实现规格；本机 Docker daemon 未运行。
- 同日复核七项判断（用户确认并补充，随后逐条推敲、再经独立审查）：① 纯文本草稿也可修订，max_draft_repairs 指初稿之后最多 3 次；② 组件种类由 K 在 freeze 时固定（freeze/smoke 还核对当前包种类在 K 内、需要时确认 worker），运行中 worker 不可用按基础设施故障处理：重试后 infrastructure_failed，并以 repair_runtime 暂停活动，修复后以原草稿续跑。不中途收窄为文本菜单，也不结束 worker 段，否则同段前后与 M6 两支的条件或轮数会不一致；③ 上游评分器只排除“初始成立且结束时仍成立”或作者声明不计分的断言，被破坏的初始成立断言按失败计分，照常给 R（`automationbench/rubric/__init__.py` 的 free-assertion 分支）；④ 笔记上限改为每轮读取数（缓存加新生成），久未纳入的失败优先，覆盖按诊断工作集如实记录；⑤ tests/*.py 是代码：检查判定含 test bundle，运行判定不含，任务执行、安装与导出不因只有 tests 而要求 worker；⑥ 失败子集重跑只用于诊断，gate 每次重跑自身对照臂（`bench.py:571-591`），复用的 dev 结果只进 R 上下文并标明是被选中的抽样；每轮成本据此更正为约 $10–40、5–6 小时；⑦ 笔记身份为渲染后的完整请求摘要加模型配置；`research.diagnose` 改为逐题契约 failure_summary.v1 → failure_note.v1，旧 requirement_list.v1 路径保留，R0 形成新身份。另加：包文本字面量检查（防止把 train 断言中的具体值写进包）、草稿检查只在 fixture 或仅 train 构造的世界中运行、M6 第一轮笔记作为 campaign 级共同输入。
- 未决（待真实被拒候选出现后再讨论，不写入设计规则）：候选因 dev 回归被拒时，R 只能看到 dev 汇总；此前通过的 train 任务不重跑，所以没有 train 侧回归证据。同一议题还包括：已安装版本在未重跑 train 任务上的回归同样看不到，诊断工作集只会缩小；只重跑失败题时，偶然失败的任务回到均值，会被误记为修复。拒绝的具体原因与表现、补跑 train 能否提供有效信号，以及 gate 只给汇总这一机制本身是否需要调整，都尚不清楚。
- commit：未提交。

## 2026-09-30 首版收敛：受限能力包代码演化，训练完善后置

- 按本轮讨论调整交付边界：首版从任务失败自动生成/修改工具封装、中间件、技能和经验，验证后交付同 actor 的 h* 应用；第二版完善教师适配、蒸馏与新增训练。首版保留 R0/R1 比较、安装和下一真实改进调用回用，不把 RSI 核心交回人工。
- 编辑集中在交付范围和对应契约：M0 HarnessPackage/worker.harness、M1 固定 API/Docker worker/broker、M4 包内 patch、M5 隔离检查/预算/恢复、M3 回归/发布/同字节导出、M6 相同资源代码搜索与 M7 无训练依赖的 preset/报告。评分、权限、预算、发布核心、宿主 API/依赖不在自动修改范围内。
- 保留原训练/教学详细设计，仅标记第二版；保留旧 CLI、代码、历史证据与用例原义。同步设计入口、路线图、审查/演示与首/二版验收归属；新增 IT6-22～26 和相应模块边界用例。
- 文档检查：22 份 Markdown 的本地链接/锚点、围栏/表格与配置示例通过；134 个当前用例 ID 无重复，git diff --check 通过。用例是待实现规格，不是通过结果。
- 仅编辑相关文档，未改代码、未提交、未执行训练、模型付费调用或 benchmark；新包演化与真实递归收益仍待实现/验证。

## 2026-09-28 设计审查修订：新 harness、自动补采与教学源验证

- 根据定位对齐审查修复三处规格缺口，没有改变最小完整 RSI 的产品目标。只改文档，未改代码、未提交，未运行训练或 benchmark。
- 更换目标 harness：M3 准备保留 W/R/K 的受限新基线 B；M0 明确 parent 与 execution_base_ref，采集/训练绑定 B，系统 gate/CAS 仍对原生产 parent。补齐运行许可、无环引用、兼容失败与恢复规则，不允许仅替换配置就沿用旧基线。
- 自动补采：CollectionRequest 和 RoundResult 保留 needs_collection/next_action/action_ref，M4 的同一驱动在额度内采集、重建 context、继续提案；无进展/缺能力才停止当前 worker 段。M7 不吞掉后续获准 research slot；M6 两支共用该规则，重启不重置计数或重花钱。
- 教学源与发布分开：Experiment 冻结 purpose，source_validation 的 train 验证通过以 validated 完成并可立即适配，不必跑生产 dev gate、没有安装权；直接发布另建 deployment 实验。复用原执行器、评分与预算，不增加第二调度器。
- 同步总设计、路线图、研究协议、技术原则、模块接口/流程、审查和演示，新增跨模块 IT6-18～21 及相应边界用例。当前技术来源与领先模型默认仅引用公布数字的决定保持不变。
- 文档检查：22 份 Markdown 的本地链接/锚点、表格、代码围栏及配置示例通过；118 个当前 v0.6 用例 ID 无重复，撤销的 IT6-14 不计入。`git diff --check` 通过；新增规格仍待实现，未运行代码测试。

## 2026-09-28 定位修正：围绕最小完整 RSI 核心交付

- 修正上一条范围决定：用户要求减少外围功能，而非把自动发现交给人或取消 RSI 验收。框架输入是任务环境/目标/权限/初始资源，输出是可运行 app 和可继续演化的工作区；不要求用户逐轮准备优化工作流与配方。
- 主线改为自动发现 → 源方法真实验证 → 自动教学适配 → 学生在环 → 训练固化 → 无教师评测/发布 → 改进方法更新并实际回用。明确源 harness 可演化、目标 harness 在一次蒸馏内固定，首个模型/算法/后端不是框架定义。
- 实现继续复用 M0～M7，仅新增 M8。研究员保留为已有 provider 上的有界逻辑职责；首版复用 R0/R1 campaign 验证方法及下一调用的真实绑定，不新增研究平台。actor/controller 分离能力保留，应用可按效果关闭 controller。
- 更新总设计、技术选择、路线图、全部模块交接、演示/审查与中英文 README；撤销草案 IT6-14，增加零人工 source 启动、源方法实际执行、自动适配、递归伪阳性和 cycle 恢复用例。保留领先模型默认只引用公布数字的决定。
- 证据边界：只有文档变更；没有代码实现、付费调用、训练或新增 benchmark 结果。真实 C1/C4 仍待验证，不以 fixture 代替；本次未提交。
- 文档验证：22 份 Markdown 的本地链接/锚点、代码围栏、表格及 TOML/JSON 示例检查通过；95 个当前 v0.6 用例 ID 无重复，撤销的 IT6-14 不计入。`git diff --check` 通过；没有运行代码测试。

## 2026-09-28 范围收紧：研究员退出首轮交付（已被上条定位修正取代）

- 根据用户反馈：先完成一件可验证的事，暂停研究员新增开发。当前只做人工工作流/配方驱动的采集、actor 训练、无教师评测、发布；controller 之后单独决定，自动研究与递归比较不排期。
- 改了什么：整体流程图和路线图只保留 A～C；M7 人工入口直接复用 admission/executor，不依赖 `run_worker_round` 或研究 provider。M4/M6 新设计与用例标为暂缓，已有代码/CLI/完整 release 保持兼容，R ref 原样继承；C4 为 not_run。
- 验收边界：新增 IT6-14，要求缺少研究配置也能完成蒸馏全链，研究员构造/调用失败哨兵不被触发；研究、controller、Harbor 的后续用例与当前必做集合分开。
- 文档检查：22 份文档的链接/锚点、表格、代码围栏和配置示例检查通过；77 个保留/新增用例 ID 无重复；`git diff --check` 通过。
- 证据边界：只改文档，没有实现代码、训练或 benchmark 新结果；未提交。

## 2026-09-28 v0.6 设计：完整工作流蒸馏、独立决策与 RSI 回用

- 做了什么：修订整体设计、技术选型、路线图、研究协议、演示、审查和模块详细设计；保留 M0～M7，新增 M8 工作流蒸馏；新增 v0.5 实现快照，保留原测试和缺口记录。中英文 README 修正过时状态。
- 关键决策：先冻结目标 harness，以教师执行前 PASS/REPLACE 采集学生真实轨迹，按环境成功筛选后做 actor SFT，再无教师评测与发布；独立 controller 在同 actor 上验证增益；研究策略更新继续复用现有 campaign。Tinker 为首个生成训练后端，Harbor/DeepAgents 按需要可选；不建设另一套平台或应用市场。
- 开发交接：明确 schema 兼容、两类监督来源、模型实际绑定、token mask、模块所有权、错误/恢复/预算、部署白名单、实施顺序及新验收用例。保留最近“领先模型只引用公布数字”的决定，修正新 actor 路径下的 C3 对照映射。第三方只参考设计，不复制源码。
- 文档检查：22 份 Markdown 的本地链接/锚点、表格列数、代码围栏通过；TOML/JSON 示例可解析；76 个新增验收用例 ID 无重复；`git diff --check` 通过。未运行代码测试。
- 证据边界：本次仅文档与来源核对，没有代码实现、付费调用、训练或 benchmark 新结果；新用例均为待实现规格。已有真实单任务运行和历史工程验证不扩大为改进收益。
- commit：本次尚未提交。

## 2026-09-28 第一次真实模型运行：GLM-5.3 跑一个 Marketing dev 任务

- 做了什么：领先模型改为引用公布数字，不在本地运行（DESIGN §3.2、ROADMAP M2、模块 07）。来源两个：官方榜单（私有集）与上游 README 的公开集结果（含 GLM 5.2 max 26.17%，六域合计）。本地用智谱 GLM-5.3（OpenAI 兼容接口，thinking 为默认开启），经我们的 W0（含 finish check、业务停止视图、计量）跑 dev 任务 `marketing.product_adoption`（14 个断言，长度居中）：`ouroloop bench --config examples/automationbench/marketing.local.toml --phase baseline --task marketing.product_adoption`。
- 证据：通过（task_completed_correctly = 1，partial 1.0），15 轮、29 次工具调用、229 秒。prompt 共 225,196 tokens，其中缓存命中 197,568（88%）；输出 11,662，其中推理 6,441。
- token 从哪来：第一轮 prompt 只有 1,210 tokens（系统提示、工具定义与任务），之后每次请求都带上全部历史，平均每轮增加约 1.85k，到第 15 轮是 27,132。增长主要来自 `api_search` 返回的端点目录（top_k 10～15，每次约 2.5k～4k tokens）和 base64 编码的邮件正文；另有两轮花在未连接的 Salesforce、HubSpot（401），两轮花在回读已发邮件核对。
- 证据边界：一个任务、一次运行，不代表通过率。这次的 `cost_usd` 为空（运行时没有 `[pricing]`），费用由逐轮用量离线换算。
- commit：见本条记录的 Git 历史。

## 2026-09-28 付费运行前的补齐：计量、计费、续跑与计划冻结

- 做了什么：`MeteredProvider` 接到所有阶段，agent 与研究员的每次模型调用先预留、按实测结算，额度用尽的尝试记为 `budget_exhausted`（不补跑，比较记 inconclusive）；一个活动只有一个预算根，各阶段、campaign 各支与外层评测都是其子额度。每次尝试按 `[pricing]` 计算 `cost_usd`，先用 `normalize_usage` 统一供应商的用量词汇，缓存读写单独计价，缺价则 unknown。`evaluate` 与 `improve` 中断后从记录续跑：尝试 id 由计划、臂、任务、重复与重试决定，已记录的尝试原样保留，构建已完成的不重训，安装重放自身 operation 最多提交一次。补跑在写入结果库时就带上 `supersedes`。新增 `ouroloop freeze`，从 `[freeze.task_plan]`、`[freeze.schedule]`、`[freeze.budget]`、`[freeze.final]` 冻结对象并打印 `[rsi]` 引用。
- 更正：此前把 BR-05 记为已覆盖，实际 `evaluate` 恢复时会把全部尝试重跑一遍，只是不重复写状态文件；已按验收实现并在 07 文档里写明。
- 补齐过程中发现并修正的四处：其一，`settle` 把预留时没有上界的维度当作上界 0，第一次调用的 token 用量就会被判越界并停止一切付费操作——计量一接上，第一条集成测试就停在这里；其二，campaign 原先另开一个预算根并拿到同样的额度，等于把活动预算翻倍；其三，OpenAI 兼容接口报 `prompt_tokens`/`completion_tokens`，按 `input_tokens` 读会把费用算成 0，而 0 正是未知费用绝不能变成的数；其四，补跑的 `supersedes` 原本在结果写入之后才在内存里补上，只追加的结果库和据此生成的报告永远看不到它。
- 证据：`python -m pytest -q` → 553 passed, 1 deselected（新增 18 项在 `tests/test_paid_run_safety.py`）。模拟进程在最终评测第一臂中途死亡后再运行：8 个计划尝试各出现一次，崩溃前记录的全部保留；模拟在改进轮评测中途死亡后再运行：同一实验从 evaluating 续跑并安装，恰好一次安装事件。
- 证据边界：仍没有真实模型运行；计量与计费由夹具供应商的固定用量验证。
- commit：见本条记录的 Git 历史。

## 2026-09-27 删除贪吃蛇场景包与多代演示循环

- 做了什么：删除 `examples/snake/`（游戏、标签器、规则、场景、两份配置）、`tests/test_snake.py`（14 项）与 `ouroloop/generations.py`（场景包的多代循环）；`ouroloop demo` 只保留合成数据上的离线一轮，不再接场景包参数。`tests/test_counting.py` 去掉依赖 `generations._judged` 的那一项——同一处计数由它上面基于 `replay.summarize` 的用例覆盖；`tests/test_tde_compat.py` 原先从 `examples/snake/demo.toml` 读配方，改为内联一份同形状的小配方，并删掉只检查那两份示例配置的用例。
- 为什么：v0.5 的主线是 AutomationBench 业务闭环，贪吃蛇只作 smoke；场景包删除后 `generations.py` 没有可运行的住户，留着就是死代码。保留的是业务循环用得到的东西：`ouroloop/demo.py` 的合成离线一轮（四个测试文件在用）、`evolve/monitor.py`、`trainers/signature.py`、旧 decision profile 的全部路径。
- 证据：`python -m pytest -q` → 535 passed, 1 deselected（此前 550，减去 15 项贪吃蛇与世代计数用例）；套件耗时从约 90 秒降到约 55 秒。删除本身没有产生新的运行证据。
- 因此改了文档：README（中英）、DEMO、ROADMAP、DESIGN、RESEARCH_AGENT、TRAINING_HOSTS、modules/README、modules/05、modules/07 里指向贪吃蛇或 `generations.py` 的说法改为业务循环入口；本文件头部说明贪吃蛇数字已无法复算。历史条目原样保留。
- commit：见本条记录的 Git 历史。

## 2026-09-27 M0～M7 实现：完整改进闭环与研究策略回用

- 做了什么：按 v0.5 模块详细设计实现全部八个模块。新增 `evolve/contracts.py`（内容寻址身份、分区血缘、对象库与事件日志）、`policies.py`（七个注册目标、内置配置、装配、会话 pin、候选构建）、`research/inspection.py`、`research/context.py`、`evolve/feedback.py`、`evolve/task_gate.py`、`evolve/execution.py`、`research/experiment.py`、`research/campaign.py`、`report.py`；改造 `runtime.py`（`propose`：route `policy`，分别记录提议、执行与执行者）、`stop.py`（策略接线与 override 事件）、`agent/loop.py`（业务观察、工具事件、Observation/DecisionTrace）、`points/__init__.py`（`stop.business.v1` 视图）、`release.py`（完整 release、permit、preflight+CAS 安装、恢复、撤销）、`benchmark.py`（v2 字段不改旧 hash）、`evolve/build.py`、`evolve/pipeline.py`（`train_artifact` 无晋升副作用）、`research/loop.py`（`run_worker_round`）、`bench.py`（七个阶段、启动检查、`Improvement` 四个 handler、`research_slot`）、`cli.py`。
- 证据：全仓 `python -m pytest -q` → 547 passed, 1 deselected（此前 399 项既有测试全部保留通过）。新增 147 项按验收 ID 组织：CT-01～06、PR-01～07、DT-01～08、ER-01～10、RL-01～08、EX-01～08、RC-01～10、BR-01～08、IT-01～07。夹具为 `tests/fake_upstream` 新增的 `rsi` 域（更新后通知的要求写在请求末尾、合法 no-op、重复发送即回归）与脚本 worker/研究员/trainer；脚本 worker 依据请求、系统提示与工具结果响应，不看候选名字。IT-01：W0 漏通知（partial 0.5）→ R0 提出 finish check → 配对 dev 评测 accepted → 安装 → 新 session 的提示里确有该指令，该 dev 任务通过且其他任务不回归，`train_jobs` 为 0。IT-02：脚本 trainer 产出的决策器在“通知仍缺”时提出 no，StopGuard 实际打回，随后任务完成。IT-03：R0 提出 R1，两支隔离 campaign 在同起点同额度下比较各自产出的 W*，外层任务（两支内层都没碰过的家族）候选支 1.0 对照支 0.0，安装 R1，下一轮的研究请求 prompt hash 确为 R1。
- 实现中被测试推翻的四处：其一，只看训练池的一轮提不出缺口，于是每轮增加“当前版本在 dev 上的测量”，缺口必须来自将被评判的任务；其二，输入覆盖检查把从未出现的可选字段（本次没有截断，`truncation` 恒空）当成缺失而阻断训练提案，改为由 schema 声明 `required`；其三，verification 额度原先只挡别人不挡自己，改为双向限制，装不下验证的候选就不启动；其四，准入的反馈构造器默认空集合，会让所有训练提案因错误理由作废，改为取注册表。另修：经验记录的 `trained` 原本按“有 artifact”判断，prompt 候选会被写成训练过。
- 证据边界：**没有任何真实模型的业务成绩。** 夹具的数字由夹具定义；IT-03 的研究改动由脚本提出并标 `scripted`，不是自动发现；C1～C4 全部未运行。仍待交付：分支比较 builder、经济模式 campaign、进程级隔离、自动补跑与 calibration 接线，以及冻结计划下的付费实验。
- commit：2cc8f28, 2a255c0, 78eee0d, d10abb4, 4e1744a, 1f5015f, 550f60b, bf34d29

## 2026-09-27 v0.5 架构审查与模块开发交接

- 做了什么：对照 Raven 固定提交与当前源码审查 RSI 完整性，新增 RSI_REVIEW 和 modules 下的公共契约、七个实现模块及集成验收。明确代码所有权、依赖顺序、接口、状态、错误、预算、恢复和用例。
- 发现的缺口：业务 stop 输入截断、历史数据的 session 分组与重渲染、prepare_model 的旧门副作用、完整 W/R 授权安装、嵌套预算和研究 campaign 的隔离/回用都需要具体实现。本次补齐设计，未修改这些代码。
- 因此改了：首版只执行声明式策略；新 task/research profile 用完整 release 与单一 active；开发晋升与最终收益证明分开。家族签名测量保留，撤回“低重叠即可证明语义独立”的推断。
- 证据边界：源码与文档审查，没有新增模型、训练或 benchmark 运行；工程用例是待验收规格。完整框架实现与真实 C1～C4 证据仍待交付。
- commit：见本条记录的 Git 历史。

## 2026-09-27 完整 RSI 设计：任务策略与研究策略共同演化（v0.5）

- 做了什么：系统边界定义为任务策略 W、研究策略 R、经验 E 与实验内固定核心 K。任务和研究候选复用同一策略描述、评测、版本与安装机制；prompt、规则、workflow 与小模型都是载体。核心研究方向仍是从执行反馈中训练可复用决策策略，不扩建领域专家池。
- 设计依据：审查了 [Raven 固定提交的 Curator 设计](https://github.com/EverMind-AI/Raven/blob/40a6b456dcd13f9c551a0f78dc1361e55b90e4e4/experimental/docs/design.md)和反馈迭代源码，吸收行为要求、实际装配检查、机制活动、失败/回归继承及验证后安装；参考 [DGM](https://sakana.ai/dgm/) 明确更新后的研究能力可以参与后续自改进，不把递归限定为训练研究员权重。外部能力比较仍以领先模型为参照。
- 因此改了：DESIGN、RESEARCH_AGENT、ROADMAP、DEMO 及中英文 README 升至 v0.5。M5 先接固定 R 的任务改进；新增正式 M7，以相同起点、经验和预算的隔离 campaign 比较 R/R'，通过后回用于下一轮研究。C1～C3 继续报告任务层效果；新增 C4 报告研究质量或成本效果。工程回用链与收益证据分开验收。
- 实现状态：保留 M1 的真实上游契约检查、固定 split，以及 M3 完成度探针和脚本干预的全部已有记录；修正设计中仍称这些未实现的状态。断言数量支持完成判断的研究假设，不证明真实模型失败原因；路线图将独立同可靠率的数学示意与实测分开，并修正示意数值。
- 证据边界：本次仅更新设计文档，未修改运行代码、调用模型、训练或复跑 benchmark，未产生新成绩。新策略描述、实际装配快照、集成研究循环和 M7 均尚待实现；不将完整设计写成已实现 RSI 能力。以下历史条目保留原时点的说法，当前目标与状态以 v0.5 设计和路线图为准。

## 2026-09-27 一个任务是被漏掉一项要求输掉的

- 做了什么：在真实上游上核验 M1，按核验里量到的东西选定干预点，并实现 M3 的信号路径 1——`CompletionProbe`：每个 stop 决策处留一份世界快照，episode 结束后用官方 rubric 给每份快照打分，把"当时活干完了吗"写成 `env` 标签。评分只在运行结束后发生，官方评分器不做在线 checker。只在 train split 上挂；session 的 `held_out` 改成 `split != "train"`（dev 是裁判数据）。
- 证据（真实 checkout：`zapier/AutomationBench` @ `4a8e106`、package 1.0.6、Python 3.12.14、`api` 工具集，装了 `verifiers` 和 `openai-agents`，所以每项比较都真做了）：25 个 dev 任务全部通过契约检查——官方 rubric 入口是 `['partial_credit', 'task_completed_correctly']`、权重 `[1.0, 0.0]`，服务订阅门与上游 `compute_allowed_services` 逐任务一致，工具 schema 的参数名与必填项和上游 `agents.function_schema` 一致，快照独立性在真实 pydantic world 上逐任务成立，与断言无关的写入不移动分数，0 处 leak。split 已冻结：train 52 / dev 25 / test 23。
- 决定干预点的测量：dev 任务需要中位数 **12** 个计分断言（均值 14.1，最多 39）全部通过，没有任务靠什么都不做能通过（中位数另有 7 条断言初始就成立、被排除出分母）。`task_completed_correctly` 是合取，所以每项 90% 可靠只有约 31% 的任务通过率，95% 是 53%，98% 是 76%。**任务是被漏掉一项要求输掉的**，于是"活是不是真干完了"这个判断——当时状态可见、训练任务上可核验、`policy.ACTING` 本来就只放它执行——是有依据的第一个干预点，不是因为"二分类适合小模型"。
- 离线验收（不花 API）：同一个脚本模型同一个任务，点不执行时失败在 partial 0.5 且这次漏掉被记成 stop 的 `no`；一个有把握的"没干完"把 agent 送回去，任务通过 1.0；答案低于阈值就走默认、什么都不变。快照代价在最大的真实 world 上是每份 1 ms、0.1 MB。
- 因此改了：`ToolSurface.mutates` 由工具面回答"这次调用可能改了世界吗"——原先循环拿 write/edit/bash 这三个工作目录的工具名去问，业务工具面永远答不上来。这不是洁癖：`Budgeted` 包住 episode 却没透传它，改动计数一直是 0，探针于是把每个 stop 决策都按第一次停下时的状态打标签，一次真实采集的全部训练样本都会是错标签。现在探针构造时就拒绝答不出改动的工具面。家族判定也修了：只按工具与断言类型算签名时有 172 对任务重叠超过 0.8，读了两对之后发现那只是"读表格、发邮件提建议"同一个形状；把断言的期望值也算进签名后，100 个任务里任意两个的重叠都不超过 0.42——一任务一家族从此是有证据的结论。
- 尚未做：没有真实模型运行，因此没有任何成绩；信号路径 2（有限分支比较）未实现；`prompt_best` 对照未做，而 DESIGN §1 要求小模型必须先比它更好。
- commit：1e1b56f、（本条）

## 2026-09-27 M1：先有运行契约，再有分数

- 做了什么：`benchmark.py`（冻结的任务分组、运行 manifest、`TaskResult`、预算）、`adapters/automationbench.py`（延迟导入上游、`api` 工具集、官方评分、快照、契约检查与输入审计）、`evolve/task_eval.py`（一次尝试、有效性、预算）、`bench.py` 与 `ouroloop bench --phase smoke`、`examples/automationbench/marketing.toml`，以及 `tests/fake_upstream/`——上游的微缩替身，使 88 个新测试不需要真实上游。
- 读源码确认的上游事实（`zapier/AutomationBench` @ `4a8e106`，2026-08-04）：默认工具集是 `api`，只有三个函数（`api_search`、`api_fetch`、`base64_encode`），都是 pydantic `WorldState` 上的普通同步函数，所以评分和工具都不需要 `verifiers`；`rubric.partial_credit` 和 `task_completed_correctly` 是 state dict 上的纯函数，后者读前者缓存的值，因此调用顺序是契约的一部分。公开 Marketing 是 100 个任务，`info` 只有 `zapier_tools` / `initial_state` / `assertions` 三个键，**没有 family 或 variant 字段**——所以默认一任务一家族，只保证同一任务的尝试、分支与重复不跨 split；`near_duplicates` 报工具与断言重叠高的任务对供人工声明。`automationbench.tools` 会连带导入整个 Zapier 工具注册表，其中一个模块在模块级 `from openai import OpenAI`，所以哪怕只用 `api` 工具集也必须装 `openai`。`tools/zapier/chatgpt/completions.py` 在有 `OPENAI_API_KEY` 时会真的调 OpenAI，没有 key 时返回固定串；公开 Marketing 的 100 个任务都没有 seed 或授予 chatgpt，上游的服务订阅门也会拒，episode 仍在工具运行期间隐藏这个变量，双重保险。
- 证据：测试过程发现两个真实缺陷，都先写了会失败的测试。其一，`WorldState(**grading.initial_state)` 与评分时重建的初始世界共享嵌套结构，agent 改完 HubSpot 阶段后，"初始状态"里也变了，于是那条断言被判成"本来就成立"而被排除出分母，`partial_credit` 从 0.5 变回 0.0——上游 runner 自己 deep-copy 过初始状态，适配器没有；现在两份都是深拷贝。其二，leak 审计原先在 `json.dumps(payload)` 上做子串匹配，一条粘进 system prompt 的断言在那里是转义的（`\"type\"`），搜不到；现在遍历结构里的每个字符串和每个键。契约检查也因此发现自己的洞：`contract_check` 原来在 `task_ids` 为空时回退到全部任务，会去读 test 任务的初始状态得分，现在必须指名。
- 因此改了：`ids()` 必须指名 split（空调用报错，不返回全部也不返回空）；冻结的 split 文件被改派时报错，工作区已有结果时连 `--replace-splits` 也拒绝；未测到的费用一路保持 `null`，汇总另报"缺在几次尝试上"；infrastructure 类终止不能是有效尝试；test split 的 session 带 `held_out`，已有的建数据路径取不到；M2～M6 的 phase 名报错并指向路线图，不悄悄做别的事。
- 尚未做：没在真实 checkout 上跑过（适配器是按上游源码写的，`contract_check` 正是为此而写），没有真实模型运行，没有真实成绩，`zapier` / `limited_zapier` 工具集未接。
- commit：a10572a

## 2026-09-27 主线更新：AutomationBench 业务闭环与领先模型比较（v0.4）

**目标。** 在 AutomationBench Marketing 上跑通执行反馈、改进、可选训练、完整任务评测和版本更新。外部比较对象为实验启动时业界领先模型；同底座前后比较验证迭代收益，同一 prompt/workflow 下的默认策略与小决策模型比较验证训练的增量价值。

**设计调整。** 重写 DESIGN、ROADMAP、RESEARCH_AGENT、DEMO，并同步中英文 README 的目标与实现状态。保留训练作业、模型库、服务、版本和账本；补齐环境适配、任务家族划分、业务反馈构造、task profile、完整任务评测与研究接线。先验证失败可被干预，再训练；prompt-only 候选不强制训练；第一版不扩专家池或元研究模型。

**评价边界。** 主指标是完整任务通过率，部分完成率用于诊断；同时报告运行与累计改进成本。开发迭代与最终测试分开，公开自留测试与官方私有榜单分开。删去当前设计中逐代单调、固定次数内必须提升、无 key 即可证明真实业务收益等预设，不要求组件归因矩阵作为首个交付前提。

**证据。** 本次仅更新文档，未运行模型训练或 AutomationBench，未产生新成绩。代码核对显示已有模型训练管线主要按历史局部标签回放评价；独立研究循环尚未接入世代训练循环。新增模块与 bench 命令均标为目标接口。

**历史说明。** 以下 v0.3 及更早条目保留当时的决策与运行证据，旧主线、接口设想与验收要求已由 v0.4 设计及路线图替代，不作为当前待办。

## 2026-09-27 重切：训练线为主线，部署线冻结，研究员回到设计实验

**证据。** 离线 demo 6 代（`ouroloop demo snake --config examples/snake/offline.toml`，2026-09-26 晚重跑）：冠军 food 0.7～0.9，与规则蛇的括号值持平；第 3～5 代晋升的每个冠军都只接管了 2 局、2 局都受害、被监控撤销（下界 22.4% > 5%），合并受害率 6/6 = 100%。GPU 上 run4～run6：模型推翻规则的步只对 15%～26%。也就是说，从 9 月 26 日起加的每一层护栏（按 session 计风险、增益门、运行时监控、撤销冷却、准入）都工作正常，而蛇一局都没有多吃到。根因是签名模型看不见死路（`rules.signature` 只看四邻格与食物方位），这是特征问题，被当成了安全问题处理。

**外部。** TypeSafe 9 月 19 日发布 Jev（typed decision model，闭源权重，4000 万美元种子），48 小时内出现 pi-warden、typesafe-mcp、openjev、dspy-typesafeify 等项目；REFLEX（arXiv 2609.26532）用 Jev 做 agent 的 selective control，无门、无账本、不学习。"self-improving agent" 是 2026 年最拥挤的标签（Hermes 9.5 万星，OpenClaw 34.5 万星）。

**因此改了什么。** 四份文档重写为 v0.3：`docs/DESIGN.md`（两条线、概念表、模块职责表、五个循环、训练线 9 条不变量、六条可度量的主张 C1～C6、基准分层 T0～T3 与消融矩阵）、`docs/ROADMAP.md`（W1～W11，每项有文件级改动与命令级验收；冻结项与推迟项各带触发条件）、`docs/RESEARCH_AGENT.md`（研究员产出 `Experiment`，可改 features / arch / hparams / data / spec，单变量靠对照臂；保留"看不到测试对局、不改标签、不碰门、无凭据"四条边界）、`docs/DEMO.md`（替代 `DEMO_PLAN.md`：本地交互流程、90 秒脚本、业界对照、叙事规则）。设计上的决定：训练线用策略模式（冠军 argmax 打整局）与对局评测器（冻结种子、配对 bootstrap）；接管模式、风险预算、校准阈值、监控、撤销、发布三元组、契约、prompt 管线整体冻结；准入砍到预算与重复锁；去掉"一轮只改一个配方键""架构从父继承"。

**没有运行证据的部分。** 本条目只重写了文档，代码未动；假设 2（环境允许改进）由 W5 用人当研究员验证，不成立就先修场景或训练器。

**同日追加。** 对照业界对 RSI 的定义（改进者自己也要被改进）重新审了设计：到 W11 为止是自我改进的训练循环，递归只在"策略改变数据"这一条；补上研究决策上环（`worth_running`、`pick_arm`、`stop_line` 三个决策点，门的结论只对它们是标签）作为 W12 与主张 C7，元指标是每次接受的改进消耗的实验数与算力随代数下降。把提升机制写成四条通道（数据、表示、规格、分布）加门的单调性；决策点准入条件用 Kahneman 与 Klein 的两个条件（封闭标签集、可观察结果，再加分布稳定）。部署线改称"同一产品的下半场"，第一个实例定为 Claude Code hooks 上的 `tool_gate`。

**同日第二次追加（流程审查）。** 把设计的来历写成九条第一性原理（DESIGN §1.0：决策与推理分开、真相只来自环境和人、策略改变分布、改进者碰不到评测器、一个结论一个变量、每个数字落盘、数据是用户自己的、机器故障不是证据、概念与依赖最少），按它们核对每个循环与适配器的数据路径，记录在 DESIGN §十。改了八处：arena 种子由公开公式改为随机抽取只存记录（P4）；准入移到开记录之前（P9）；C3 的误接受表述补上 α 的对象（统计）；研究决策点由三个收成 `worth_running` 一个，排序不否决、带 ε（P9、P2）；适配器补记权限模式、`stop` 列为已知缺口、条款与隐私写进 P7（P2、P7）；`adapters/` 从部署层移到环境层，与场景包并列为两种住户（架构一致性）；策略模式下"默认错误"定义为"规则蛇在此会错"（P3）；确认研究员看冠军历史 arena 汇总不泄漏（P4）。目的与一句话重写：ouroloop 提升 agent 的决策质量，证明场是模拟环境，目的地是真实 harness。

## 2026-09-26 一次撤销只买一次训练，准入不再拒错轮次

- 做了什么：`evolve/plan.py` 的准入重写。撤销带来的"新数据"只算冠军执行过、还没有候选训过、且本轮不留作测试集的 session（`Champion.new_sessions`）。
- 证据：原来把冠军执行过的 session 的所有有标签行都算成新数据，包含本轮测试 session 和上个候选已经训过的行，于是一次撤销能一轮接一轮地提计划，直到训练预算用完。同时查出五处判反：`min_cal_sessions` 不足时 `calibrate_point` 给每个阶段的阈值都是 None，这样的数据却能准入；单个稀薄的点否决整轮，而门的 `decide_points` 并不为此通过或阻塞；预测增益取"目标 − 已达到"，站着的模型接近目标后残差低于测试集分辨率，再也没有一轮能准入；`inconclusive` 被算成假设失败，一次训练任务崩溃就挡住自己的重试；`risk = 0`（模板限制仍允许）让 `sessions_needed` 死循环。
- 因此改了：两条校准检查都只对能执行的点做；判不了的点从计划里拿掉，所有目标点都判不了才拒绝整轮；预测增益取"离目标的差"和"本轮测试集能看见的最小比例"（`_detectable`）里的大者；只有门真的量过并拒绝（`AGAINST`）才算假设失败；risk = 0 直接报错，是不训练的理由，不是搜索。
- commit：6bcf9ea、8ac06d9（合并）

## 2026-09-26 主机上的训练任务也在信号下取消

- 做了什么：`evolve/train.py` 的 `_wait` 在本机和 ssh 主机上也跑在 `_signals_raise()` 下（此前只有云实例如此）。
- 证据：停 run6 时发现的：`_wait` 对任何 `BaseException` 都会取消任务，但默认处理的 SIGTERM 直接结束进程，没有 `finally` 跑，训练任务成了孤儿，占着 GPU 没人回收，只能手工杀掉。而 systemd、`docker stop`、一个普通的 `kill` 正是无人值守的循环被停下来的常规方式。新测试在没有这个修改时会把测试进程自己打死（exit 143）。
- 因此改了：取消的保证不依赖调用方，而依赖信号转成 `SystemExit`。
- commit：486d609

## 2026-09-26 研究报告不给 agent 看校准 session

- 做了什么：`research/loop.py` 的 `build_report` 按门记录自己的 `cal_frac` 切分，只读 `train` 一侧。
- 证据：原来用默认的 `cal_frac = 0` 切，非测试行全落进 `train`，agent 连同渲染出来的文本看到了它的候选之后要在上面拟合阈值的那些行。新测试把行数钉在记录的切分上，不修就失败。
- 因此改了：Learn-then-Test 要求候选在看到校准数据之前就固定；看过校准行再挑的候选，阈值带的 Clopper-Pearson 界不再成立，门报的风险是乐观的。agent 能看的，就是这一轮能训的。
- commit：4803744

## 2026-09-26 四个角色：模型库和服务从训练里拆出来，部署与回滚

- 做了什么：`ouroloop/stores/`（dir 与 hf 两种实现，一个接口，返回不可变 ref）、`ouroloop/serving.py`（训练好的模型只经此被问）、部署探针、按血缘的回滚。
- 证据：没有运行证据，是四处设计缺陷的结果，它们都是"全部跑在一台 GPU 主机上"留下的痕迹：transport `direct` 让训练来决定模型住在哪；评测的后端来自训练器而不是服务；晋升只是翻一个指针，没有任何一次加载；冠军被撤销后 session 掉回默认，而不是掉到上一个好版本。
- 因此改了：worker 经模型库发布并取回 ref，卡片上模型只按 ref 指认（backend spec 里只有 ref，服务、校准、session 钉和撤销名单都只用它），作业目录和主机名仍记在卡片的 `job` 一栏里备查；门接受之后，服务先加载、报出的模型 id 必须和卡片一致，冠军才移动，部署失败则冠军不动（`ouroloop deploy <version>` 重试）；回滚落到最近一个被接受、已部署、未撤销的祖先（`Registry.rollback`），没有就回到默认。
- commit：f22c64c

## 2026-09-26 没有计划不训练，没有数据不成计划

- 做了什么：一轮改为 数据 → 诊断 → 假设 → 计划 → 准入 → 训练（`evolve/plan.py`），只有最后一步花 GPU；`pipeline.schedule` 删除，它的三条反射变成规则计划器的三条提案分支（`plan.plan`：还没训过、默认错误翻倍、冠军被撤销），提案之后还要过 `admit`，撤销那条另加"带来新数据"的条件（`Champion.rows >= MATERIAL * rows`）。
- 强制点：这条规则落在 `ouroloop/generations.py` 的世代循环里，不在 `pipeline.evolve_model` 里。`ouroloop evolve`（`cli.cmd_evolve`）仍然不看计划就训一个候选；世代循环里 `[demo] plant` 指定的那一代也有意跳过 `decide`——植入的坏候选按定义要走同一道门。
- 证据：run6（把执行后监控放进运行时之后）：坏模型在害了 2 个 session 时就被撤销，但日程按"撤销即重训"立刻重训，新候选看到的几乎是同一批数据（被撤销的模型只在 2 个 session 里执行过，失败几乎没留下新数据），于是以同样的方式失败，连着两次。
- 因此改了：撤销是证据，不是触发器；准入的决定性检查在训练前就能算出来——risk r 下一个阈值需要 `sessions_needed(r)` 个一条错都没有的 session（r = 0.05 时 59），而只有含默认错误的校准 session 才买得到一个，不够就说明这份数据训出来的模型永远不可能执行。`ouroloop plan` 把诊断、提议和准入结论连理由一起打出来。
- commit：b0a01d4、8ac06d9（合并；合并时把"目标只算真的部署了的模型"补进 plan.py）

## 2026-09-26 训练器变插件，通用 worker，主机自举，云 GPU

- 做了什么：`ouroloop/trainers/`（插件契约，tde 与 signature 两个插件按契约声明自己的 `RECIPE`，tde 另有 `PATH_KEYS`；两者都是可选项，不声明就用框架的 `DEFAULT_RECIPE` 和 `PATH_KEYS = {"init_from"}`）、`ouroloop/worker.py`（主机上 `python -m ouroloop.worker`，版本不符就拒绝）、`ouroloop/hosts/`（bootstrap 与检查）、`evolve/cloud.py`（RunPod / Vast.ai 每个任务一个实例，用完必销毁）、`docker/worker` 镜像。
- 证据：没有运行证据，来自取舍清单（9195962、ee328c5 两个计划提交）和一次评审：任务的父模型只有同一插件训的冠军才算，host.json 要和 venv 里的实际代码对；云侧 create 不盲目重发、等 `start.sh` 的标记、实例 TTL 与 SIGTERM/SIGHUP、实例消失立即失败。
- 因此改了：训练路径只剩一条（本地或 ssh 或云），只有 spec.json、train.jsonl、val.jsonl 过界；transport `hf` 让云实例可用。
- commit：b5105f1、485a0a7、85bddc0、e4f22c5、9195962、ee328c5

## 2026-09-26 运行时自己跑执行后监控

- 做了什么：`Runtime.session` 在每个新 session 固定发布之前调用 `evolve/monitor.watch`，从只读增量账本行维护的计数里判断，下界超预算就撤销冠军；`revoke()` 跨进程幂等。
- 证据：run5（8 代，默认回答者很天真，模型有发挥空间）：门两次晋升了在已记录状态上安全、上线后有害的模型，160 个 session 里分别害了 158 个和 78 个。监控两次都抓到了，但当时只有演示脚本每代调一次，坏模型先害完一整代；内核本身根本没有跑过监控。
- 因此改了：会改变自己后续输入的决策，在已记录状态上校准不等于在它自己造出来的状态上校准——监控必须在运行时里、按 session 触发；已经在跑的 session 从下一个决策起取默认。README 的架构图改为动图（`docs/assets/architecture.py`）。
- commit：3e3c330、9a04490

## 2026-09-26 一条结果可以写多个正确答案

- 做了什么：`outcome` 的标签可以是一组都对的标签，目标在它们上均匀；校准、监控、增益、默认错误统计一律问"答案在不在正确答案里"（`replay.right`）。
- 证据：贪吃蛇账本上，一个训练得好的模型推翻规则的 6348 步里，4920 步是同样好的一步，被算成了错。
- 因此改了：风险不再被高估，增益不再惩罚同样好的推翻，任意的平局规则不再是训练目标；把一个对的默认换成另一个对的答案，增益为零。贪吃蛇的 labeler 给出所有和最好一样好的方向，默认回答者换成 `rules.keep_going`（一直往前、要撞死才转），好让模型有发挥空间。
- commit：ab921d6

## 2026-09-26 配方 focus：训练花在默认出错的行上

- 做了什么：`pipeline.focused`，保留全部默认出错的训练行，其余按种子抽样，使错误行占到指定比例；校准和测试行不动。
- 证据：模型只能在默认出错的行上产生价值，而在长跑的循环里这些行是账本的一小部分——贪吃蛇里约 7%。
- 因此改了：训练量变小，且花在有增益的地方；研究候选可以设 focus。
- commit：7c1cbc1

## 2026-09-26 以默认为基准的价值：风险按 session 计，门比增益

- 做了什么：路由默认（规则回答者在路由前给出的答案）、按 session 的风险、按增益判定的门，三件事一次改到位。
- 证据：run4（6 代）当时风险按决策计：模型执行的步里 96%～99% 本来就是规则蛇会走的；决定输赢的是那少数几次推翻，只有 15%～26% 是对的，其中最多一半当步致命；18%～52% 的局死在模型手上，而按决策计的 1%～4% 错误率轻松过了 5% 的预算。门按全部测试行的 Brier 晋升，这些行大多是默认本来就对的。
- 因此改了：风险按 session（模型执行过的一步错，这个 session 就算受害），阈值拟合、监控、门都用这个单位；候选按对路由默认的增益（对 +1、错 −1）判；按决策计数的 `count`（`first`/`sampled`）删除。
- 旁证（同期两次对照，说明问题不在标签也不在混合两个策略）：同样的种子、只跑游戏逻辑时，labeler 自己每步都走，8×8 上吃到 32.7、0 次死亡，规则蛇 15.3；把 labeler 的步随机混进规则蛇，比例越高越好，单调。问题出在模型自己执行的那些步上。
- commit：cb8ee5c

## 2026-09-26 阈值用固定顺序检验

- 做了什么：`policy.fit_threshold` 从最有把握的截断值往下按固定顺序（Learn-then-Test）检验，遇到第一个不过就停，不再"留下最低的那个过了的截断值"。
- 证据：原来是几十个嵌套检验、每个都在 5% 的 alpha 上，靠运气挑出过的那个：一个实际错 6% 的模型对着 5% 的预算，100 个 session 时 200 次校准里有 52 次拿到阈值（300 个 session 时 29 次），`first` 和 `sampled` 都一样。改后 200 次里 9 次。离线贪吃蛇演示只变了一个阈值（第 5 代执行 3%，不是 18%）。
- commit：1653371

## 2026-09-26 抽样计数每个 session 只抽一次

- 做了什么：`sampled` 计数改为按 session id 和行序的哈希一次排好名次，每个截断值取看得到的行里名次最靠前的那条，跨截断值嵌套。
- 证据：原来每个截断值重抽一次，几千个截断值里几乎总有一个是幸运抽样：一个实际错 8% 的模型在 40 次试验里 40 次拿到 5% 的阈值（`first` 是 4 次）。改后 40 次里 1 次。
- commit：d8dfcb2

## 2026-09-26 离线贪吃蛇演示：一代 120 局，每代旁边是默认打的同一批局

- 做了什么：每代 120 局；每代的局用同样的 session id、种子和棋盘，由默认单独再打一遍放在 `<workspace>/baseline/`，括号里并排打印；每局指标进账本成 metrics 事件，每行输出成 generation 事件，测试从磁盘重算每一个数。
- 证据：60 局时校准 session 太少，第 6 代模型只在 8% 的步上执行；120 局时第 4 代起执行 24%、第 6 代 63%，汇总实测错误 1.7%（CP95 3.3%），约 70 秒。另一头：离线模型打的局和规则蛇一步不差（执行了 63% 的步，每一步都是规则蛇自己的步），第 5 代食物和步数跳高只是因为棋盘变大了，屏幕上看不出来。
- 因此改了：屏幕上的每个数都要有对照、都要能从磁盘重算；被撤销的冠军那一列标注 `(revoked)`；汇总行覆盖账本里的所有代，不只本次调用的。
- commit：4140aec、287b8f9、0442a18、456880c、d8dfcb2

## 2026-09-26 贪吃蛇 README 改成实际跑出来的样子

- 做了什么：删掉"棋盘变大会退化、撤销、恢复"的承诺，写明离线版本只展示机制。
- 证据：离线时什么也不会退化（小模型只读头部周围的格子）；一次 `offline.toml` 运行里它执行了 14,014 步，每一步都是规则蛇自己的步，所以离线的局等于基线；GPU 主机上这一段没有跑过。
- commit：8bff773、5585675、d2ec16b、493c54d、a21193f

## 2026-09-26 门记录：没用过的记录在新 session 会改变它的测试集时让位

- 做了什么：`gate.choose` 允许用新记录替换一个尚未使用的旧记录。
- 证据：GPU 主机上两次出现同一情况：第一份记录把最新的 session 拿去当测试集，剩下能训的太少，这一轮在推理之前就停了（525 条训练样本），而记录被一直沿用，后面每一轮都饿着。
- 因此改了：没用过的记录，其测试 session 没有被任何推理碰过，可以换成用更新的 session 的新记录，旧的那些回到训练里。
- commit：c28d4fe

## 2026-09-26 TDE 配置里的浮点数

- 做了什么：训练器配置写成 YAML 也读得回浮点数的形式。
- 证据：配置是 JSON，用 `yaml.safe_load` 读；PyYAML 把没有小数点的尾数（json 的 `2e-05`）当字符串，TDE 拿字符串乘浮点数，GPU 主机上每一次训练都失败。
- commit：d4e7ded

## 2026-09-26 训练任务：一个 worker，本地或 ssh，模型按 ref

- 做了什么：三条训练路径（Local/Remote TdeTrainer、tde_remote 后端）合成一条：写 spec、在新任务目录里起 `worker.py`；worker 记下 TDE 的 git HEAD，按 ref 解析父模型，训到截止时间，写 `result.json`。
- 证据：没有运行证据，来自 9195962、ee328c5 两个计划提交里的取舍。为无人值守保留的：两侧都有截止时间、状态读取失败算仍在运行、进程没了立即算丢失、等待被打断就取消任务、进程里第一次训练先清理本工作区的孤儿、ssh 调用有时限、decide 分块并在多于一块时重试。
- commit：e4f22c5

## 2026-09-26 贪吃蛇场景与多代演示

- 做了什么：`examples/snake`（游戏、棋盘文本、move 决策点、环境 BFS 标签、局即 session 的运行器、指标）、`providers/manual.py`（把 API provider 的接口和数据协议落成文件）、`evolve/monitor.py`、本地 TDE 路径、`ouroloop demo <scenario> --generations N`。
- 证据：随后的几次运行（run4-run6）就是这套装置量出来的；本条自身没有量化证据。
- 附带修的：满盘时吃到最后一个食物即胜（否则 labeler 绕着尾巴走）；manual provider 的 usage 允许任意对象（OpenAI 兼容服务端会嵌套 `prompt_tokens_details`）；`prepare_job` 打乱 val 切分（TDE 只用 `calibration.jsonl` 的前 `eval_limit` 行选 checkpoint，按账本顺序就是最旧的那些 session）。
- commit：e27a132、37cdca9、90d936c、e99d3fe

## 2026-09-26 M8：一个冻结的门，每轮一份记录

- 做了什么：`gate freeze` 由人写下门模板（risk 0.05、min_sessions 30、cal_frac、min_cal_sessions、检查项、测试集规则、session 预算）；每轮派生 `gates/<round>.json`（模板哈希、包哈希、账本位置、测试与 held-out session id）；`gate.run` 是模型、spec、配方、prompt 四种候选唯一的入口，一轮一个结论；注册表用比较交换晋升，session 固定在 (model, spec, harness) 三元组上。
- 证据：评审在真实账本上发现两处会白白耗掉测试 session 的问题——测试 session 可能没有标签（M6 账本的前 125 个 session 里有 2 个在探针之前就崩了，于是第一份记录的 30 个只剩 28 个可评分，门判 `insufficient_evidence`，记录和 session 一起作废）；新 session 不够时冻结的记录会被一直沿用。随后的绕门评审找出 15 个洞，每个都先写了会失败的测试（`tests/test_gate_bypass.py`）：删掉 `used` 标记让测试 session 重新算新；手改 `champion.json` 指向被拒的候选；手改 `calibration.json` 的阈值；配方候选把 `init_from` 或 `replay` 指向本轮校准数据；`test.jsonl` 被拷给训练器；`gate freeze --replace --set risk=0.5` 放宽之后所有的门；研究 agent 读到 held-out 结果；session id 里的路径穿越写坏账本。
- 因此改了：门记录、它的使用和结论都是账本里的事件，门从不读 `gates/` 里的副本（但 `gates/r_*.used.json` 是运行时判断"这个版本是不是门接受过的"的唯一入口，`release.Accepted`）；模板在 `gate.LIMITS` 的四个字段（risk、`min_sessions`、`n_boot`、`suite.min_items`）和 `cal_frac` 上只能收紧不能放宽；只有门记录接受过的版本能晋升，且只能压在它评过的那个父版本上；阈值必须与接受结论里列出的校准完全一致才执行；回滚同时撤回当初晋升它的那份门记录的接受。
- commit：2a862fa、117f56c、ba71c4c、88e86b9、905e19f、679a987、c5ee835、853308f、e150cd2、6c8ec1c、091fe8b、931f33e、4e80ae2、2ccd8f2、0217777、19730ad

## 2026-09-25 M7：决策点只按执行规则执行，出错走默认

- 做了什么：一个 `act` 开关取代 shadow/gate/auto 三种模式；每个阶段声明默认回答者（固定标签、`rule:` 或 `llm:`）；校准按（模型 id、spec 哈希、决策点、阶段）存；只有带门记录哈希的阈值才让点执行。
- 证据：对抗性安全评审找到六个洞，每个都先写了会失败的测试：单个 NaN/负数/无穷大概率被重新归一成有把握的答案；自定义后端返回未归一的分数（如 2.0）在温度为 1 时跳过归一，越过任何阈值；runtime 建不起来时 Claude Code hook 吞掉异常、跳过黑名单，`rm -rf /` 进了 Claude Code 自己的许可流程；NaN 阈值、温度 ≤ 0、空模型 id 都会误放行；spec 哈希没覆盖 view 的截断方式；带引号的 `act = "false"` 被当成打开。
- 因此改了：任何不是"在问题标签上的分布"的答案都记为 error 路由并取默认；只有有名字的模型能有阈值；`act` 必须是布尔；`max_view_chars` 与 `keep` 进 spec 哈希。
- commit：b8afca9、706b068、6fb7b48、cb3959e、9249f17、96d270e、05dda06、656e575、c444f31

## 2026-09-25 M6：阶段、session 事件、StopGuard 和探针

- 做了什么：决策点声明阶段（阶段 → 默认动作），每行记下阶段和默认；`Runtime.session` 写 session 事件；`ouroloop/stop.py` 的 StopGuard 取代 `Agent._pushback`；`agent/probe.py` 在每个 stop 决策处复制工作目录，会话结束后写入隐藏测试、跑检查、给当时的决策打标签（运行中的任何东西都看不到结果）。
- 证据：真实验收（在固定于 M6 提交的独立工作树里采集）：前 50 个 session 里 yes 和 no 两种探针标签都出现；按起始顺序前 85 个 session 共 326 个 stop 决策（325 个有探针标签），per_turn 阶段 153 个 no、129 个 yes，on_finish 阶段 41 个 yes、2 个 no——收益集中在 per_turn。（这组数可从 `examples/workspace-m6/ledger/` 复算；M6 当时的进度记录写的"前 86 个 session、327 个决策"复算不出来，前 86 个是 330 个决策、154 no / 131 yes / 42 yes / 2 no。）任务集从 10 个扩到 29 个（8 个 held-out），每个都"照原样失败、带解法通过"。
- 因此改了：评审又修三处——per_turn 判"完成"要先跑 checker 才结束（模型不推翻规则）；最后一轮之后不再问 per_turn（记下的默认动作根本不会发生）；探针复制不了或检查不了的状态保持无标签，而不是中断整次运行或让任务集崩溃。
- commit：74686e5、4c183b0、ea0c384、a3f31a9

## 2026-09-24 评测装置本身的四个缺陷

- 做了什么：门只读 development 切分；候选顺序在呈现时按决策种子打乱；候选描述长度对齐；账本在每个一致率旁边打印最好的常数答案。
- 证据：在真实决策集上量的——参考答案在 53 个选择里只有 3 次是最后一项（写在最后的通常是"放弃"），"永远不选最后一项"在打乱前得 94.3%；29 个选择里有 22 个参考答案的描述最长，中位数多 28 个字符，"选最长的"（既不读状态也不读问题）得 73.7%，对同一后端的 84.2% 做精确 McNemar 检验分不开（p = 0.344）。长度对齐后启发式掉到 44.7%（随机是 36.4%），后端 78.9%，配对检验 p = 0.0072，而后端的答案只在 38 项里变了 2 项——它从来不是在读长度，是这套集子显示不出来。公开的 38 项与保留的 36 项：同一后端 84.2% 对 80.6%，差别是披露而不是难度。
- 因此改了：能否决候选的切分属于选择程序，不是对这个程序产物的独立估计——held-out 照跑照记，但不参与接受或拒绝。
- commit：f2942b8、c0b6f7d、94e799b

## 2026-09-23 ouroloop 0.1.0

- 做了什么：第一版骨架：决策层（类型化决策点、带校准阈值的策略、探索与倾向、只追加的 JSONL 账本）、决策后端（mock、Jev API、TDE、LLM 兜底、HTTP 服务）、最小 agent（读写编辑与 bash，五个内置决策点）、evolve 流水线（建数据集、数据检查、训练器、校准、配对 bootstrap 的晋升门、带回滚的注册表）、研究 agent 接口、带隐藏检查的任务集、Claude Code hook 数据源。
- 证据：无，这是起点。
- commit：43d3811

---

## 还没建的

日志记的是改了什么、什么时候改的。当前未完成项以 [研究员状态表](RESEARCH_AGENT.md)与[实施路线图](ROADMAP.md)为准。
