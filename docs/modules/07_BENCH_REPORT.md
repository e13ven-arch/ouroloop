# M7：任务后端、配置与成绩报告

> 首版用现有命令接通能力包自动演化、发布和研究方法回用；第二版增加蒸馏配置。保留七个历史 phase，另加 `cycle`。所有者 `bench.py`、`benchmark.py`、`report.py`、`cli.py` 与 adapters。首版配置（`[improvement]`、preset `marketing_harness_v1`、`[harness_evolution]`）与 `cycle` 已实现（2026-09-30）；第二版配置仍为待实现契约，reader 明确拒绝。
>
> v0.3（2026-10-04 决定，DESIGN v0.7，[ROADMAP §3 “v0.3”](../ROADMAP.md)）：交付物从“固定 agent 循环 + 能力包”改为“可演化的 agent 程序 + 最小运行壳 + 所 pin 的模型”，比较口径改称“同模型”（M0 §2（v0.3）：manifest `harness_package.v2` 增加 `agent` 组件，默认 agent 是今天的宿主循环移植成包）。M7 相应变化：`[bench]` 接受领域列表并按领域分别划分、合并使用；Marketing 的 test 已读过三次，对新主张声明为已耗尽；报告增加每次有效尝试成本（`cost_per_attempt`）、实验台账、余量、按领域分列，匹配成本的 best-of-k 对照臂只在发表结果时运行并并列报告（可选）；并发默认 8，每轮墙上时间单域 25 题 gate 约 10～15 分钟、多域合并 dev（约 125 题）约 45～60 分钟；配置示例换到 v0.3 的 preset。v0.1/v0.2 的配置、结果与 2026-10-03/04 的记录保持原义，不删除。本文标“（v0.3，2026-10-04）”的条目是新增或改动的规则，其余不变。

## 1. 阶段与端到端入口

| 既有 phase | v0.6 行为 |
|---|---|
| smoke | 首版检查 provider/工具/评分、包 API/计量，以及当前包组件种类在 K 内；K 的 components 含代码种类或当前包运行含代码时检查隔离 worker，不可用则不能以该配置启动；第二版启用方法才检查 review/训练。（v0.3，2026-10-04）另检查 `agent` 组件的入口能在 worker 里导入并经网关完成一次假调用（M0 §2（v0.3）：`run(ctx, task)`，ctx 只有 model/tool/skills/limits/view），以及 `[bench] domains` 列出的每个领域都能被上游装载 |
| baseline | 起点 actor（第二版为学生）及已选 prompt/workflow 的无教师基线；首版为所 pin actor 的 train+dev 基线（原定 GLM-5.3，2026-10-03 起为 Claude Opus 5.5，见 ROADMAP §3 第 2a 步），在首轮真实演化前运行；不默认跑领先模型。（v0.3，2026-10-04）多领域时是各领域 train+dev 合并清单上的一次测量，它同时是 W0 的第一条合并对照读数（M3 §2.1(a)（v0.3）：对照臂是同一冻结计划与模型身份下已安装 W 的全部有效读数的合并） |
| collect | plain 保持原行为；reviewed 调 M8，仅 train，须有已冻结合法 plan |
| improve | 首版 M4 run_worker_round 读取 train 失败摘要、逐题笔记后汇总诊断，生成包内 patch/策略候选，经冻结前草稿检查进入唯一 executor；第二版准备教学/训练。（v0.3，2026-10-04）一轮是 R 的一个编码会话（修订继续同一会话），产出至多 `max_candidates_per_round`（preset 3）个候选，先做 train 侧筛选，只有最好的一个付 dev gate；第一次读数清过 min_gain 0.05 与退步规则的候选，再按同一计划做一次确认测量，确认读数单独对合并对照也清过 min_gain 才安装（M4 §2（v0.3）筛选、M3 §2.1(b)（v0.3）确认，规则第二版 2026-10-04）；M7 不另写循环，只把 k 个候选、筛选与确认当作同一轮的子操作记账 |
| research | 用 M6 做有界 R0/R1 比较，条件满足才安装 R1。（v0.3，2026-10-04）研究位只在 dev 合并清单 ≥ 2 × `task_plan.gate.min_tasks`（50 题）时开位：前一半作两支内层 gate、后一半作外层比较，每一半不少于 25 题；不够时研究位记 `no_slot_pool`，C4 not_run——只含 Marketing 的活动（dev 25 题）因此不开位。开位前再做余量检查：起点 W 从未通过的内层任务少于 preset 的 `campaign.min_headroom_tasks` 时拒绝开位，记 `no_headroom`（M6 §2（v0.3））；原因是 v0.2 的研究位里 W* 在内层已到 67～80%，两支都无处可改，比较了个空 |
| evaluate | 方案锁定后的无教师 final，禁止据此继续选择或采集训练数据。（v0.3，2026-10-04）final 计划可选加一支匹配成本的 best-of-k W0 臂，只在发表结果时运行，与 W* 并列报告、不进 C1（M3 §2.2（v0.3）） |
| report | 纯离线读取，区分源验证/采集/dev/final、脚本/真实、应用/研究收益和费用。（v0.3，2026-10-04）增加 §4 列出的字段：每次有效尝试成本（`cost_per_attempt`）、实验台账、余量、best-of-k 臂（若运行）、按领域分列 |

无需用户手动拼 refs 或每轮执行七个命令。新增一个薄入口 `ouroloop bench --phase cycle`（已实现，`bench.cycle`）：按冻结 schedule 调用现有 phase handler，先预检和缺失基线，再自动 worker rounds/已计划 research slot/安装后下一轮回用，最后生成开发报告。不重写内层状态机、不默认自动运行 final；中断后复用同一 activity 和 operations。单独 phase 仍可用于排错和续跑。

cycle 按 M4 的结构化结果推进，而非看到 proposals 为空就退出：`next_action=collect` 先完成受限补采并重建 context，再继续剩余轮次；只有无方法、无法获得所需能力、补采无进展或限额耗尽才结束当前 worker 段。M4 driver 负责相同的分派规则，M7 提供现有 collect/执行 handler，不另写一份自动循环。

第二版源方法以 purpose=source_validation 通过后提供教学来源，适配/蒸馏不先走生产 gate；直接发布则创建 deployment 实验。换宿主时先准备新 baseline，再建相应 plan。首版包候选直接走 deployment。所有准备/补采/实验消耗原轮次/费用，阶段切换不重置。

无 worker 提案是该段结束原因，不自动取消活动中已冻结的 research slot；cycle 仍按固定 schedule 在有额度时执行方法比较及下一 worker 段。取消、全局预算耗尽或影响整个活动的能力故障才结束全活动；K 所需能力（如隔离 worker）运行中不可用时暂停活动（M0 §3），不进入 research slot，修复后以同一冻结条件续跑。cycle 每一步的暂停或额度耗尽写入 `rsi/cycle.json` 该步的 outcome/reason 与活动的 status；暂停时退出码为 1，额度耗尽为 0（M5 §3，2026-10-02）。M6 分支本来禁用 research slot，结束后将其最后正式通过 gate 的 W 返回外层。

`--phase research` 与 cycle 的 research slot 共用 M6，不能重复触发相同已完成操作。安装 R1 后 cycle 预留至少一次实际改进调用的额度/轮次验证回用；没有余量则记录回用未完成，不把 ref 更新当作完整验收。

人工模式仍将完整 proposal 直接接 admission/M5 executor，不伪装自动研究。自动/人工入口不能同时控制同一活动；origin 由入口赋值。scripted 模式仅用于旧兼容和 fixture，示例必须标明。`report` 不补模型请求，`evaluate` 只在用户锁定的 final 计划下运行。

（v0.3，2026-10-04）cycle 一轮的顺序与墙上时间目标：
- 顺序：逐题笔记（并发写，上限仍是 `max_failure_notes`）→ R 的一个编码会话产出至多 k 个候选（M4 §1（v0.3）：一轮一个会话，修订继续同一会话；R 在 worker 里以文件工具改包目录，`research_session.max_turns` 轮、`max_edits` 次编辑以内，工具只有文件工具、`read_trajectory` 与 `finish`，没有 shell）→ 每个候选的草稿检查与声明核对 → train 侧筛选（重跑该候选笔记来源的失败 train 题，加 `screen.passing_sample` 个固定的通过 train 题）→ 只有筛选最好的候选进 dev gate → 第一次读数清过 min_gain 与退步规则的候选再做一次同计划的确认测量 → 确认读数单独对合并对照也清过 min_gain 才安装（M3 §2.1(b)（v0.3），规则第二版 2026-10-04）。筛选与确认都是同一轮的子操作，计入同一预算与轮次，不是新的 phase。
- 并发：`[bench] concurrency` 在 v3 preset 下默认 8（`bench.preset_concurrency`；没有 preset 的配置与 v1 preset 仍是 1，写了就按写的；`ouroloop bench --concurrency N` 只覆盖本次运行。实现 2026-10-04，测试 `test_paid_run_safety.py::test_concurrency_is_a_machine_setting_the_frozen_plan_does_not_carry`，取代原拟的 `test_bench_phase.py::test_concurrency_defaults_to_eight_and_is_not_in_the_plan`），受订阅的速率限制约束：provider 把限流/过载/429 当未计费的可重试故障（M1 §1，`ouroloop/providers/claude_code.py:48-49`），用量上限仍让活动暂停（M5 §3）。并发不进任何冻结计划的身份，每次尝试的限额与每次调用的预留不变（M5 §2）。
- 目标：按 v0.2 实测（4 路并发时每次尝试墙上 27 秒；8 路约 12～15 秒，受限流时更长），单域 25 题 × 1 次的 gate 一臂约 5 分钟，加筛选（约 10 题）、确认测量（再 25 题）与笔记/编码会话，一轮约 10～15 分钟；多域合并 dev（§2 “多领域活动与划分”，约 125 题）时一臂约 30 分钟，一轮约 45～60 分钟。目标是衡量实现是否达到预期的数字，不是冻结计划的一部分：慢了不改计划，只改并发或 provider。

## 2. 用户配置、默认配方与冻结

新配置示意如下。v0.1/v0.2 的 reader 已实现 `marketing_harness_v1`，完整可运行的历史示例见 `examples/automationbench/improvement.toml`（GLM-5.3）、`claude.toml`（v0.1）、`claude-full.toml` 与 `claude-full-final.toml`（v0.2）；它们和它们冻结的 `[rsi]` ref 不改。其余环境路径、工具绑定、定价与硬预算沿用现有完整配置。preset 是代码中的配置模板（`bench.PRESETS`，`ouroloop/bench.py:1027-1050`），不是插件；它的 harness 数值是上限，`[harness_evolution]` 只能收窄或调低。

（v0.3，2026-10-04）示例换到新 preset `automationbench_harness_v3`。它不是 `marketing_harness_v1` 的改值，而是另一个 preset：旧 preset 锁定 `domain = "marketing"`（`bench.py:1080-1082`），其数值已冻结进 v0.1/v0.2 的 K，不能回头改。下面每个新键右侧注明定义它的模块；M7 只持有 preset 与 reader，不另定义语义。

```toml
[bench]
execution_backend = "automationbench" # 缺省保持直连；可选 harbor
domains = ["marketing", "sales", "support", "hr", "finance"]  # （v0.3）领域列表，取代单值 domain；旧配置的 domain 仍可用，等于一个元素的列表；（v0.3，2026-10-05）第 5 步用这五个，operations 不用（ROADMAP 第 5 步 2026-10-04 定，原示例列了六个）
split_rule = "stratified"            # （v0.3）没有冻结划分的领域按计分断言构成与工具族分层后再哈希；已冻结的划分（Marketing）原样沿用
spent = ["marketing"]                # （v0.3，2026-10-05）test 已耗尽的领域：新主张的 final 不读，报告单列
# [bench.frozen_splits] marketing = "aa7ff7ec189c0cbb"  # （v0.3，2026-10-05）按哈希规则划分并须得到此 hash 的领域（v0.2 的划分）
concurrency = 8                      # 一臂内同时跑的尝试数；机器的设置，不进冻结计划（M5 §2）；（v0.3）默认 8，受订阅限流约束

[bench.ratios]                       # （v0.3）新领域仍是 train/dev/test 三个分区；研究位的内外层来自合并 dev 的对半分（M6 §2）；Marketing 的冻结划分（52/25/23）原样沿用
train = 0.5                          # （v0.3，2026-10-04 定）：多给 train 无用（每轮最多读 30 条笔记），多给 test 直接换来更窄的 final 区间
dev = 0.25
test = 0.25

[improvement]
mode = "automatic"                   # manual/scripted 另有明确入口
preset = "automationbench_harness_v3"
allowed_methods = ["policy_edit", "harness_edit"]
max_candidates_per_round = 3         # （v0.3）每轮至多 3 个候选；reader 放行 1..preset、缺省取 preset（2026-10-05 已实现，test_bench_phase.py::test_max_candidates_per_round_up_to_the_preset_is_accepted）
max_collection_batches = 2           # 每个预先分配的 worker 段上限，重启不重置

[harness_evolution]
package_api = "harness_package.v2"   # （v0.3）M0 §2：增加 agent 组件；v1 包等于带默认 agent 的 v2 包
components = ["agent", "memory", "skills", "on_finish", "before_tool", "after_tool", "tools"]  # K 冻结的组件种类
sandbox = "docker"                  # 含代码的草稿检查与运行含代码的包使用（M0 §2）；agent 组件总含代码，所以 v3 preset 下 freeze/smoke 必须确认 worker
max_files = 24
max_package_bytes = 262144
test_timeout_seconds = 30           # 示例限额，freeze 后 R 不可提高
max_draft_repairs = 3               # 初稿之后的修订次数上限（最多 4 份草稿）
max_failure_notes = 30              # 每轮汇总读取的笔记数上限（缓存与新生成合计）
max_effort = "high"                 # （v0.3）M0 §2/§4：K 的 effort 上限，preset 为 high，这里只能调低；W0 pin 的默认档仍是 medium，包按调用在上限内选，超出的请求由网关截顶并记下请求值，不拒绝

[collect]
mode = "plain"                      # 首版收集 task_feedback，不要求教师或训练账号
```

v3 preset 固定、且 `[freeze.*]` 只能收紧的其余数值（（v0.3，2026-10-04）；语义归括号内模块，数值来自 2026-10-04 的 gate 功效分析，ROADMAP §3 第 2a.9 步）：

| 键 | 值 | 定义 |
|---|---|---|
| `task_plan.tasks` / `repeats` | `dev` / 1 | 不变；dev 为各领域 dev 的合并清单（本节“多领域活动与划分”） |
| `task_plan.gate.min_gain` | 0.05（不变） | M3 §2.1(c)（v0.3）：门槛仍是 0.05（25 题 × 1 次约 1 个净任务，不足配对差值的 1 个 sd 0.06～0.076）；抗噪不靠提高门槛，靠安装前的确认测量（下） |
| `task_plan.gate.regression_drop` | 0.5 | M3 §2.1(d)（v0.3）：退步规则改为幅度制，一题的均值下降超过 0.5（1 次重复时 1.0→0.0；2 次时 1.0→0.5 不计）才计退步；原“严格小于”在 A/A 里 20% 触发 |
| `task_plan.gate.cost_beta0` / `cost_beta1` | 0.10 / 2.0 | M3 §2.1(e)（v0.3）：候选每次有效尝试的平均成本至多高出合并对照 β0 + β1·gain（+10%，每点通过率增益再 +2%），不满足记为拒绝理由；收据 `metrics.cost_per_attempt` |
| `task_plan.gate.min_tasks` | 25 | M3 §2.1(f)（v0.3）：任何 gate 计划的任务数少于它就拒绝冻结；研究位沿用对半分，前一半作两支内层 gate、后一半作外层比较，每一半都要 ≥25 题，所以 dev 合并清单 ≥50 题才开位，否则记 `no_slot_pool`（M6 §2（v0.3））；v0.2 的 12 题内层 gate 空候选通过率约 1/3 |
| `task_plan.confirmation_repeats` | 1 | M3 §2.1(b)（v0.3）：第一次读数清过 min_gain 与退步规则的候选，按同一计划再测一次候选臂（对照仍是合并读数）；确认读数单独对合并对照也清过 min_gain 才安装（规则第二版，2026-10-04；第一版的 ≥0 在回放里接受 4/20）。收据种类只有 `full` 与 `confirmed`：确认失败也是 `kind = "confirmed"`、verdict rejected、保留 `first_reading` |
| `campaign.worker_round_limit` | 3 | M6 §2（v0.3）：原 2 |
| `campaign.min_headroom_tasks` | 6 | M6 §2（v0.3）：起点 W 在内层 dev 合并对照里从未通过的任务少于它时不开位，记 `no_headroom` |
| `task_plan.control_pooling` | `"pooled"` | M3 §2.1(a)（v0.3）：对照臂是已安装 W 在同一计划与模型身份下的全部有效读数合并，不足的题由对照臂补齐；缺省（旧计划）读作 `fresh` |
| `concurrency` | 8 | M5 §2（v0.3）：v3 preset 下 `[bench] concurrency` 的缺省；机器设置，不进计划身份，`--concurrency N` 只覆盖本次运行 |
| `research_session.max_turns` / `max_edits` | 40 / 24（2026-10-05 已进 preset，`[harness_evolution.research_session]` 只能调低，且 ≥ 1） | M4 §1（v0.3）：R 编码会话（一轮一个，修订继续同一会话）的模型调用次数（含 `finish`）与 `write_file`/`edit_file`/`delete_file` 次数上限 |
| `research_session.max_trajectory_bytes` / `max_trajectory_bytes_total` | 65536 / 524288（2026-10-05 已进 preset） | M4 §1（v0.3）：R 只读的原始 train 轨迹工具单次与累计的字节上限 |
| `max_candidates_per_round` / `screen.passing_sample` / `screen.min_net_flips` | 3 / 5 / 1（2026-10-05 已进 preset：`PRESETS["automationbench_harness_v3"]["max_candidates_per_round"]` 与 `harness.screen`） | M4 §2、§5（v0.3）；reader 放行 `max_candidates_per_round` 1..3、缺省 3；`[harness_evolution.screen]` 的 `passing_sample` 只能调低（≥ 0），`min_net_flips` 是下限、只能调高（调低放宽筛选，拒绝）；v1 preset 没有这几项，仍是一轮一个候选 |
| `[freeze.final] repeats` | 3（定值，2026-10-04 定） | M3 §2.2（v0.3）：原 2；配置值，不在 PRESETS 里 |
| `[freeze.final] comparisons` 的 `role = "matched_cost"` 项（可选） | `best_of = max(2, round(成本比))` | M3 §2.2（v0.3）：只在发表结果时加一支 best-of-k 的 W0 臂（oracle 语义），k 由 W* 与 W0 每次尝试成本之比算出，与 W* 并列报告，不进入 C1 的裁决 |
| `schedule` | `{worker_rounds = 3, research_slots = 1, patience = 2, max_infrastructure_failures = 2}` | 不变 |

数值是 preset 的默认与上限，freeze 写入 K 后 R 不能改；活动内不重新冻结（ROADMAP §3 第 4 步）。

### 多领域活动与划分（v0.3，2026-10-04）

- `[bench] domains` 是领域列表，`domain` 继续可用并等于一个元素的列表（`BenchConfig.domain`，`bench.py:60`，改为派生字段）。上游 AutomationBench 除 marketing 外有 sales、support、hr、finance、operations（各 100 题，`~/workspace/AutomationBench/automationbench/domains/`），由上游自己的装载器读取（`adapters/automationbench.py:371-403`，按领域各调用一次）。原因：单领域 23 题的 test 看不出 +5～8 点的效应，无论重复多少次（ROADMAP §3 第 2a.9 步）。
- 划分按领域各做一份 `SplitManifest`（`benchmark.py:108`，一份一领域），各自冻结在 `bench/<domain>/`；活动的 train/dev/test 是各领域同名 split 的合并清单，顺序为列表顺序再按各领域冻结顺序。新领域的划分仍是 `SPLITS` 的三个分区（`benchmark.py:34`，train/dev/test），`[bench.ratios]` 0.5、0.25、0.25（每领域 50/25/25 题；2026-10-04 定）。研究位的内外层来自合并 dev 按冻结顺序对半分（前一半两支内层 gate、后一半外层比较，M6 §2（v0.3）），每一半不少于 25 题，所以开位要求 dev 合并清单 ≥50 题：5 个新领域合并 dev 约 125 题，够；只含 Marketing 的活动 dev 25 题，不开位。v0.3 考虑过另立 `research_dev` 分区给外层比较，不做：合并 dev 对半分已各够 25 题，少一个分区。结果与判分明细仍按领域存在 `bench/<domain>/`（`Results`、`grading.jsonl`），按冻结 split 过滤读取的接口不变：合并清单的 dev/test 明细同样不进入 R（§4 第一段）。任务 id 带领域前缀（上游 `task_name`），合并不会撞名；撞名按 `UpstreamError` 拒绝。
- 实现（v0.3，2026-10-05，与上文的出入）：
  - 配置：`[bench] domains`（列表，`domain` 与之同时给出时须相同，单值 `domain` 读作一个元素的列表）、`split_rule`（`hash` 缺省 / `stratified`）、`spent`（test 已耗尽的领域；单领域活动不能标 spent，全部领域都 spent 也拒绝）、`[bench.frozen_splits]`（领域 → 冻结划分的 16 位 hash：该领域按哈希规则划分并须得到这个 hash，否则拒绝并提示把原工作区的 manifest 拷进 `bench/<domain>/splits.json`）。`BenchConfig.domain` 改为派生字段：单领域是领域名本身（旧配置的路径、manifest 与 hash 一概不变），多领域是按列表顺序以 `+` 连接的合并名（如 `marketing+sales+support+hr+finance`）。读取在 `bench._domain_keys`。
  - 存放：每个领域的划分仍冻结在 `bench/<domain>/splits.json`；合并清单是一份 `family_rule = "pooled"` 的 `SplitManifest`（`benchmark.pool_manifests`），冻结在 `bench/<合并名>/splits.json`，它的 `upstream` 记下每个领域的 hash、规则、种子、计数与分层计数以及 spent 领域，所以合并 hash（核心的 `split_ref`、每次运行的 `data`）钉住了全部：某个领域重划、换顺序、取消 spent 都是另一个合并划分，同一工作区里按 `Frozen` 拒绝。**结果与判分明细不按领域分目录，而是存在合并名下的一个目录**（`bench/<合并名>/results.jsonl`、`grading.jsonl`、`runs/`）：一个臂的一次读数是一个 RunManifest、跨所有领域，按领域拆目录就要给 runs/grading 的每个读者加路由；按领域读只需按 task id 的前缀过滤（`benchmark.task_domain`），报告的按领域分列就是这样算的（§4）。判分明细的读取接口不变，默认只给 train（`test_grading_of_a_pooled_split_still_returns_train_only`）。
  - 装载：`bench.load_domains` 按领域各调一次上游装载器，任一领域装不上即以 `UpstreamError` 点名拒绝；合并的 suite 由 `ab.pool_suites` 拼成，任务 id 不带本领域前缀或两个领域撞名都拒绝；`[bench.families]` 多领域时按 task id 前缀分给各领域。smoke、freeze 与每个 phase 打开活动时都经它（§1 smoke 行“每个领域都能被上游装载”由此成立）；`startup_checks` 另核对各领域磁盘上的划分与合并划分所记的 hash 一致、核心的 `split_ref` 等于合并 hash（`bench.domain_problems`），并对每个领域的第一道 dev 题做评分器契约检查。
- 新领域的划分分层（`split_rule = "stratified"`）：每题的层由两个静态特征决定，计分断言的类型构成（`Grading.assertions` 的 `type` 多重集，`adapters/automationbench.py:322-331`）与工具族（授予工具所属的服务，`task_signature` 的 `svc:` 项，`:423-437`）；层内按原哈希规则分配（`assign_splits`，`benchmark.py:80-106`），层的 train/dev/test 计数向整体比例取整。两个特征都来自任务定义，不读任何运行结果，所以划分不需要跑模型，也不是 train 血缘之外的信息；冻结后 manifest 记下每层的计数。原因：25/23 题的哈希划分碰上一个偏的种子是现实风险（SURVEY §4 #10）；v0.2 的 dev–test 差距主要是失败类型分布不同（W* 修的那一类在 dev 失败断言里占 58%、test 只占 31%）。禁止按任何模型的成绩调划分：划分在基线之前冻结，`drift` 检查照旧。
- 分层的实现（v0.3，2026-10-05，与上文的出入）：层键是 `<断言种类集合>|<工具族>`（`ab.stratum`）。断言构成取**种类的集合**而不是类型的多重集：种类按天花板实验的读法由类型名定（`_not_` → forbidden_write，`count` → count，`contains` → content，其余 missing_write，`ab.assertion_kind`），只算任务定义里计分的断言（定义里标了不计分或排除的不算；评分器自己的“初始已成立”排除要给状态评分，不算）；原因：上游每个领域 100 题有 56～98 种不同的类型多重集，按多重集分层每层一题，什么都平衡不了。工具族取两类：任务世界订阅了办公服务（gmail、google_drive、google_sheets、google_calendar、slack）之外的业务应用记 `app`，否则 `office`（`ab.OFFICE_SERVICES`）。分配（`benchmark.stratified_splits`）：家族按层键、层内按原哈希规则的哈希点排序，整条序列用“落后最多者先得”的序列定各分区名额，所以整个领域的计数正好是比例（100 题 0.5/0.25/0.25 即 50/25/25），每层与其份额相差不到 2；层内按哈希点依 train、dev、test 的顺序填（哈希规则的顺序，分界放在计数上而不是固定比例上）。同一 seed 两次得到同一 manifest，列出顺序不影响结果。manifest 新增可选字段 `split_rule` 与 `strata`（每层的 train/dev/test 计数），为 None 时不进哈希载荷，所以哈希规则冻结的旧 manifest（含 Marketing 的 v0.2 划分）hash 不变（`test_a_frozen_hash_split_is_kept_when_the_rule_changes`）。真实上游（seed 1）：sales、support、hr、finance 各 50/25/25，分层 4～10 层；Marketing 按 `frozen_splits` 复现 `aa7ff7ec189c0cbb`（52/25/23）；合并 train 252、dev 125、test 123（其中新主张可读 100），合并 hash `5e68b39fca26a675`（2026-10-05 离线核对）。
- 已冻结划分在规则改变后保留（v0.3，2026-10-05）：磁盘上已有的 manifest 照原样使用，配置的 `split_rule` 与它的规则不同时只打印一条说明、不重划（改规则要新工作区）；`frozen_splits` 钉住的领域即使 `split_rule = "stratified"` 也按哈希规则划分并核对 hash。
- Marketing 的划分原样冻结（`claude.toml` 的 seed 1，52/25/23），`split_rule` 对已冻结的 manifest 无效。它的 test 已被读过三次（v0.1 final、v0.2 final-1、final-2），对新主张声明为已耗尽：Marketing 可留在列表里给 train/dev 用，它的 test 结果在报告里单列并标 `spent`，不进入 final 的主张区间，也不与新领域的 test 合并计算；原因是据 test 结果继续优化后不能再声称独立测试（DESIGN §8）。新主张只来自新领域的 test。
- preset 的领域锁改为“列表里的每个领域上游都能装载”，不再锁定 marketing（`bench.py:1080-1082` 的改动点）。实现（v0.3，2026-10-05）：v3 preset 的 `domain` 为 None（无锁），`improvement_settings` 只对带 `domain` 的 preset（v1 的 marketing）检查列表；“每个领域都能装载”由 `load_domains` 在每次打开活动时执行（`test_the_v3_preset_checks_every_listed_domain_loads`）。
- 第 5 步的分阶段运行（v0.3，2026-10-05，用户决定）：`claude-v3.toml` 的 `[freeze.schedule]` 是一个 segment、6 轮、`research_slots = 0`、patience 2，`[harness_evolution] max_effort = "medium"`（只能调低，冻进 K），另有顶层 `[estimate] cost_per_attempt`（final 估算在臂还没有计价读数时用的计划数字，`bench.estimate_settings`，不是限额、不进任何计划）。运行手册在 ROADMAP 第 5 步。
- Marketing test 的 `spent`（v0.3，2026-10-05 实现）：`SplitManifest.spent()` 读合并划分所记的 spent 领域，`claim_ids` 是去掉它们之后的 id；`[freeze.final] tasks = "test"` 解析为新领域的 test，显式列表含 spent 领域的 test 题拒绝冻结（`bench._task_ids`），`--phase evaluate` 对含这些题的计划在付费前拒绝；报告单列并不进主张区间（§4）。
- 并发按合并清单调度，不按领域分批：领域间任务世界互不相干，同一臂内的并发规则不变（M5 §2）。

collect.mode 是单独 phase 默认输入；自动补采由 CollectionRequest.purpose 决定。首版 task_feedback 运行合法 plain train attempt、保存任务结果，不要求中间决策标签 builder；decision_feedback 才要求状态真值。第二版 actor_imitation 才用合法教学 plan/reviewed。不能因为配置里有未来训练项就主动检查 Tinker/teacher。

第二版显式加入 distill_actor/decision_train 后才读取 distillation.trainer=tinker、recipe=lora_sft_v1、collect.mode=reviewed 等未来参数，沿用 M2/M8 详细规格。首版与旧 CLI reader 遇到尚未支持的新方法明确拒绝，不伪造运行。

以上为输入便利层，freeze 生成 K、W0/R0、schedule、配方目录等不可变对象后，以 refs 为权威。reader 只接受已实现枚举；未知 preset/算法/参数、能力不足、缺预算在付费前失败。preset 提供 bootstrap prompt、改进/适配模板、参数边界和默认配方，以及段 ID、补采限额和后续提案额度的默认 schedule；它不能替用户扩大工具权限或凭空提供模型账户。首版 harness preset 还固定允许的组件种类 components、max_draft_repairs（默认 3，初稿之后）、每轮 max_failure_notes（示例 30）、失败摘要的条目长度上限及现有 worker GatePlan 的 min_gain/max_regression_rate（沿用原字段，不新增回归配置层），freeze 写入 K 后 R 不能提高；草稿检查/修复与笔记调用逐次计入同一预算。首版 preset 还固定 research slot 的配对规则：当前 W 为两支共同起点，train 家族为内层训练池，开发计划的家族按冻结顺序对半分，前一半作两支内层 gate，后一半作两支都不接触的外层比较（本项目的 split 只有 train/dev/test；v0.3 起这一半叫外层池，`research_dev` 只剩代码里的枚举名，M6 §2（v0.3））；每支预算为冻结活动预算的固定比例（示例 15%），外测额度也是固定比例（示例 8%，含一次补跑），在公共测量与两支开跑前预留；研究位开始时的主线经验冻结为两支共同的 E0（实验记录去掉原始 gate 理由）。开发家族少于 2 个时 research slot 拒绝启动。（v0.3，2026-10-04）v3 preset 保留对半分，但两半都读合并对照（M3 §2.1(a)（v0.3））且每一半不得少于 `task_plan.gate.min_tasks`（25，M3 §2.1(f)（v0.3））：dev 合并清单少于 2 × min_tasks（50 题）时研究位不开位，记 `no_slot_pool`，报告的 C4 为 not_run（M6 §2（v0.3））——只含 Marketing 的活动（dev 25 题）因此不运行研究位；`worker_round_limit` 2→3；开位前的余量检查见 §1 的 research 行；两支仍只由下游 W* 裁决（M6 §3（v0.3））。v0.1/v0.2 preset 的数值不改。

首版只要求 actor/R，二者可共用已配置供应商；模型、推理配置、计价和限额在 freeze 摘要列明。首版 actor 原定 GLM-5.3（已配置并计价的 OpenAI 兼容接口），2026-10-03 起先跑 Claude Opus 5.5（订阅额度，计价为名义值，见 ROADMAP §3 第 2a 步）；第二版换 Qwen3.5-9B 学生时，h* 须在学生上重新演化并验证。第二版 teacher 配置显式指定或引用能力允许的 provider。任何路径都不能默默选择新收费供应商，正常循环不要求用户每轮写代码/方案/超参数。（v0.3，2026-10-04）`[llm]` pin 的是模型身份与推理配置的上限（含 `max_effort`），包的 agent 组件在上限内按题选 effort（超出上限的请求由网关截顶为上限值发出，并记下请求值，不拒绝）、按角色（act/aux）调用网关；比较的口径因此是“同模型”而不再是“同 actor”，C1～C4 的含义不变（M0 §2（v0.3））。`claude_code` provider 与计量不变（M1 §1）。

宿主 harness 选择来自 K 目录，默认使用当前 h；能力包代码候选在该宿主的固定扩展 API 内生成，不需要每个包 hash 预先进入目录。不得由 CLI 直接在控制面 import 候选入口。只有换宿主才准备 B；报告保留 parent、实际 package/有效工具 schema/hook 和 candidate，发生宿主切换时另外列新基线。

freeze 时 source/teaching/训练 artifact 可能尚不存在；只冻结方法目录、目标范围、数据、限额与模板。运行期各依赖产物生成后，再冻结具体 SourceWorkflow、DistillationPlan 和 Experiment。严禁占位 ref 混入可执行对象或事后改写已冻结实验。

首版交付完整、无密钥、可 freeze 的自动闭环示例（建议 examples/automationbench/improvement.toml）：actor（GLM-5.3）/R、任务家族、基础工具权限、包 API/固定隔离镜像及文件/资源限额、轮数/research slot/总预算；无需 trainer/teacher 字段。第二版再给完整蒸馏示例。默认 final 单独锁定，预采集仅在身份匹配时复用。

v3 RunManifest 增加 mode、actual actor identity、source/target harness refs、distillation_plan_ref（采集时）、dataset/training provenance、teacher present、任务后端/version，以及关联的实际 R/experiment、experiment purpose、parent/execution base 和 pending/completed collection refs。旧 run 无字段时保持旧算法，不冒充完整 v3。人工输入不能绕过 M4 的固定 method 准入；actor 采集前不要求伪造两类决策标签。

首版还冻结 package_ref、有效工具 schema/hook 清单、package API/image/限额与代码来源；teaching/training 字段未启用时为 None（序列化时省略，不以空串/空列表占位，旧 RunManifest hash 不变），不填假 dataset/checkpoint。RunManifest 新字段分版：首版启用 mode、actual actor identity、target harness ref（当前宿主）、任务后端/version、package_ref/有效工具 schema/hook 清单/package API/image/限额、实际 R/experiment、parent 与 pending/completed collection refs；distillation_plan_ref、dataset/training provenance、teacher present、source harness、execution base 与 experiment purpose 第二版启用，首版保持 None 不写出（purpose None 即 deployment，M0 §3）。构建检查、实际 hook/工具行为、安装及下一包迭代分别引用 operation/事件。

最小 app 导出由 M3 实现，M7 为现有 release/CLI 增加一个窄入口（命令拼写由实现随帮助文档一起冻结），输入已安装 release 和目标目录，输出 DeploymentManifest。开发示例须提供精确导出/装载命令；不要只给设计 API 便称用户可用。（v0.3，2026-10-04）导出的 app = agent 包 + 运行壳（网关、broker、权限、硬限额）+ 模型服务描述（M3 §4（v0.3））；R、评分器、任务数据与凭据照旧不随 app 导出。

## 3. 任务执行后端

抽出当前 AutomationBench 调用处实际需要的窄边界：

```text
TaskBackend.capabilities() -> 能力声明
TaskBackend.run_attempt(frozen_attempt, agent_factory, budget) -> TaskResult + trajectory refs
```

冻结任务发现/评分配置复用现有 manifest，别抽象所有 benchmark 的业务语义。`agent_factory` 由 M1 装配 pin 的 W 和可选 reviewer，后端不得自选另一模型。（v0.3，2026-10-04）`agent_factory` 返回的是运行壳：带 `agent` 组件的包由宿主在 worker 会话里调用它的 `agent.run(ctx, task)`，没有代码的 v1 包由默认 agent 在进程内运行；网关与 broker 在宿主侧（M1 §1（v0.3）），宿主今天不问任何决策点（stop 是 agent 返回或硬限额，tool_gate 留在 broker 作权限），后端看到的接口不变；事件/轨迹记录由宿主从网关与 broker 调用写出，包写不了，后端也不替包写。AutomationBench adapter 保持现有直接模拟世界、工具契约与官方 scorer；不能为“统一接口”改变有效任务/服务权限。

可选 `adapters/harbor.py` 将 Harbor 的任务 ID、环境/trial 配置、verifier 结果和 agent 轨迹映射回来。Harbor 管环境创建、清理与 trial 内调度；ouroloop 管实验、配对与全局预算。只用官方扩展接口；固定实际依赖版本。能力声明至少包含 review_before_execute、snapshot_for_completion、resume_trial；不支持某能力就禁止对应模式。

Harbor 任务仍须固定 revision/family/split，费用/time/trace 可查；可选接入先验证一个原生任务的评分映射。未选择 Harbor 不初始化其环境；代码演化所需 Docker worker 是独立能力，不以 Harbor 安装与否决定，也不把 AutomationBench 强制迁移进 Harbor。

## 4. 报告与失败可见性

结果存储（`benchmark.py`）新增判分明细读取接口：按冻结 split 过滤，默认只返回 train，非 train（dev/test）返回空；这是“dev/final 明细不进入 R”的边界。report 可离线汇总成功/失败，但不把 dev/final 断言明细投影给 R，也不写入报告正文。

原有 comparisons 与成本报告扩展以下字段：actor base/checkpoint、controller 是否加载/触发、target harness、教师辅助标识、来源 proposal 类型、独立任务/家族数、PASS/REPLACE、有效/排除轨迹和训练 tokens、无教师任务结果、采集/训练/推理支出、安装与下一 session 回用事实；source 验证、适配映射、实际 loaded R、R0/R1 外层比较及下一改进调用的 refs。

首版主表为同 actor 起点、有效 prompt/workflow、演化能力包；补包代码/材料摘要、实际工具/hook/技能/经验使用、固定 dev 配对结果与观察到的退化和构建/隔离、草稿检查/修复及逐题笔记成本。训练指标归第二版，首版未运行 C3/蒸馏明确 not_run；包通过测试不当任务成功，包 hash 变化不当 C1/C4 提升。

M4 的失败 train 子集重跑单列诊断覆盖，未重跑任务标明历史版本；不与新版本子集结果拼成完整 train 成绩，也不把 dev 回归写成所有 train 成功任务已复测；展示实际冻结的 min_gain/max_regression_rate 与 verdict，容忍范围内的退化也保留：逐题列出退化任务及两臂均值，早于该清单的收据显示 not recorded。复用测量引用原 TaskResult/版本/attempt 与匹配依据，不计作新增独立样本；正式主表和发布结论仍依据 M3 的冻结完整评测计划。

v3 冻结比较计划显式绑定本地 C1/C3（及可选同题 C2）的 control/candidate ref：C1 比学生起点与改进后的完整 W，包含工作流和模型变化；若单独主张训练收益，另固定其他 W 内容；C3 必须是同 actor、同提示下 controller 开/关。当前 `report.py` 把 evolved 与 prompt_best 用作 C3，新增 actor 训练后不能继续自动套用，否则会把 actor 训练收益误记为决策模型收益。v2 历史报告保持原映射，v3 缺 C3 配对则 not_run。

保留 2026-09-28 的默认决定：领先模型数字只取官方榜单/上游公开结果，不在本地跑。新增报告输入 `external_references`，每项保存 source_url、retrieved_at、model、effort、score、metric、task_set、domain 和 protocol_notes；作为不可变引用材料，不能伪造成 TaskResult/本地 attempt。

v3 最终计划区分 C2 的 `mode=published_reference`（默认）与 `paired_run`（未来可选）。引用模式只生成背景表；本地配对主张标 inconclusive、evidence_kind=external_reference，并说明不能作同题胜负判断。`seal/report` 不能沿用“必须存在两个本地 frontier 臂”去阻断引用模式。只有 paired_run 明确启用时才按已冻结范围要求实际领先模型运行；广义多模型 C2 沿用至少两个供应商，单模型结果只声明相对该模型。旧 v2 报告语义保持原样。

主表只放正式无教师结果；教师辅助采集单独展示，用来诊断可学上限，不当作模型本身的分数。官方外部榜单单独展示任务集/日期/来源，不进入本地数值差。引用模式明确写未做本地同题比较；可选 paired_run 的缺测如实保留，不能将缺配置当失败。

报告必须保留无收益、拒绝、成本未知、训练失败、预算耗尽与未运行项。source 的 validated 单列为“教学来源验证通过”，不计 accepted/installed 数量；补采仅记数据进展。某个 worker 段结束不等于全活动失败，仍展示后续 research slot 的真实状态。

（v0.3，2026-10-04）报告新增以下内容，都是离线从账本、收据与对象库读出，`report` 仍不发模型请求：
- **每次有效尝试的成本**：每臂（W0、W*、gate 两臂、研究位两支，以及运行了的 best-of-k 臂）给 `cost_per_attempt`（与收据同名，M5 §2（v0.3））、每次尝试的模型调用数与 act/aux 分开的 token（网关日志按角色记，M0 §2（v0.3））；gate 行写出成本项的裁决（β0 + β1·gain 的上限、候选实际值、是否因此拒绝，M3 §2.1(e)（v0.3））。原因：v0.2 的 W* 每次尝试 $0.452→$0.570，gate 里没有一项能看见。
- **gate 行**：除现有 gain/min_gain/退步清单外，写出合并对照的读数数量与来源（哪几次测量，M3 §2.1(a)（v0.3））、确认测量的读数与合并估计（收据 `kind = "confirmed"`；确认失败的同样是 `confirmed`、verdict rejected、`first_reading` 保留，M3 §2.1(b)（v0.3））。退步按幅度制清单列出（均值下降超过 0.5 的任务及两臂均值，M3 §2.1(d)（v0.3））；早于该规则的收据照原样显示旧比例并标 `rule=strict`。
- **实验台账**（M4 §5（v0.3）的引擎自有台账，在 E 里）：每个候选的声明（mechanism、site、carrier、predicted_affected train 任务数、trigger_condition、differs_from）；筛选结果（净翻转、筛选结局 `selected`/`not_selected`/`screened_out`/`invalid`/`lost`、`lost_to`、`thin_screen`）；gate 收据 ref；gate 后逐题 fixed/regressed/still_failing（从收据的逐题差值读出）；准入因 `repeated_mechanism` 拒绝的次数。报告按候选列出；dev 任务 id 不出现在台账的 R 投影里，报告正文也只给计数。（实现，2026-10-05）`report._harness_evidence` 的 `ledger`（`candidates` 为 `experience.ledger_projection` 的行、`carriers`、`outcomes`、`repeated_mechanism_refusals`）与 `sessions`（每轮 `research_session` 事件的回合、编辑、轨迹读取次数与字节、`finish` 次数、结束原因）；markdown 的能力包一节逐候选一行。
- **余量**：每个研究位的 `headroom`（起点 W 从未通过的内层任务数、preset 阈值、是否因此拒绝开位，M6 §2（v0.3））；每段开始时同样给 W 在 dev 上从未通过的任务数，作为“还有多少可改”的记录。C4 的 not_run 原因因此多两种：`no_slot_pool`（dev 合并清单少于 50 题，研究位不开，只含 Marketing 的活动如此）与 `no_headroom`（余量不足、未问 R），与下文 `cycle.json` 的四种结局并列写出。
- **best-of-k 臂**（可选，M3 §2.2（v0.3））：只在发表结果时运行；运行时 final 表里与 W0、W* 并列：k = max(2, round(W* 与 W0 的 `cost_per_attempt` 之比))、oracle 语义（k 次里任一通过即通过）、每题成本、通过率与配对差值的区间（W* 对 best-of-k、W* 对 W0 分别给）；不进入 C1 的裁决。未运行时 final 表该臂写 not_run。原因是演化花的钱是额外的搜索预算，匹配成本的 test-time scaling 对照是这类主张被问到的第一个问题（SURVEY §5 #10）。
- **按领域分列**：任务效果表与 final 表按领域再分一层，合并行在上；Marketing 的 test 行标 `spent`，不进入主张区间（§2 “多领域活动与划分”）；每领域给划分计数与分层计数。实现（v0.3，2026-10-05）：多领域时 `identity.domains`（每领域的 hash、规则、计数、分层计数、`spent`）与 `identity.spent`；每个 run 的 `per_domain`（题数、通过、通过率、`cost_per_attempt`，spent 领域的 test 行带 `spent`），`run_cost[run].per_domain` 为各领域的 `cost_per_attempt`；`final_by_domain`（各角色按领域的通过率、evolved−base 差值，spent 领域标出，`claim` 行只合并非 spent 领域，best-of-k 臂没跑时 `best_of_k: not_run`）；C1～C3 的 final 区间去掉 spent 领域的题（`report._final_interval`）；gate 行带收据的 `metrics.per_domain`（多领域 gate 的按领域两臂均值与差值，只有均值没有题号；单领域收据没有这一项，经验日志也不存它，R 看到的仍是总 gain；任务 id 不是 `领域.题名` 形式时也没有这一项，2026-10-05 评审后）；台账的 `by_domain`（gate 后 fixed/regressed/still_failing 按领域计数）；`harness.headroom`（每个研究位的 headroom，多领域时另有 `by_domain` 与内外两半的 `halves_by_domain`，都是计数）。单领域的报告不变。
- **速度**：每步的墙上时间、并发度、被限流重试的调用数与暂停次数；对照 §1 的目标（单域约 10～15 分钟、多域约 45～60 分钟）写“达到/未达到”，不进任何裁决。

（v0.3，2026-10-05，第 0/1 阶段评审）final 之前的主张（没有封存计划或没有 final 读数时 `_pair_claim` 的退路）只比两种角色的 dev 读数：W 的 train 侧筛选与 train 测量也是 `evolved`/`base` 角色，但是另一组题，原来被平均进去；有 final 时的区间路径不变。测试 `test_report.py::test_a_claim_before_the_final_reads_the_dev_readings_only`。

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
| BR6-18 | `[bench] domains` 列出 Marketing 与两个新领域，Marketing 已有冻结划分（v0.3，2026-10-04） | Marketing 的 manifest 原样沿用；两个新领域各冻结一份三分区的分层划分；活动的 train/dev/test 是三份同名 split 的合并清单（合并 dev 75 题 ≥50，研究位可开，内外层各约 37 题）；结果与判分明细仍按领域存放（v0.3，2026-10-05 实现：划分按领域存放，结果与判分明细存在合并名下的一个目录，见 §2“实现”） |
| BR6-19 | 分层划分的输入（v0.3，2026-10-04） | 层只由计分断言类型构成与工具族决定，划分过程不读任何 TaskResult/grading 行；同一上游、同一 seed 两次得到同一 manifest；每层的 train/dev/test 计数写入 manifest |
| BR6-20 | 新主张的 final 清单含 Marketing 的 test（v0.3，2026-10-04） | Marketing 的 test 行单列并标 `spent`，主张区间只用新领域的 test；报告不把两者合并成一个区间 |
| BR6-21 | `[improvement] max_candidates_per_round = 3`，preset 上限 3；另一次设 4（v0.3，2026-10-04） | 3 被接受，一轮至多 3 个候选、只有筛选最好的一个付 dev gate；4 在付费前拒绝 |
| BR6-22 | 旧配置（`claude.toml`、`claude-full.toml`）在 v0.3 代码下读取（v0.3，2026-10-04） | 单值 `domain`、preset `marketing_harness_v1`、`harness_package.v1` 与冻结的 `[rsi]` ref 原样可读；报告按旧规则显示旧收据（`rule=strict`、无成本项） |
| BR6-23 | 离线 report 面对 v0.3 的收据与台账（v0.3，2026-10-04） | 每臂有 `cost_per_attempt`；gate 行有合并对照读数数、确认测量与成本项裁决；台账按候选列出（声明、筛选、fixed/regressed/still_failing）且不含 dev 任务 id；研究位有 headroom，只含 Marketing 的活动的研究位记 `no_slot_pool`、C4 not_run 写明；final 表的 best-of-k 臂运行了才列出，否则 not_run；按领域分列 |
| BR6-24 | `[bench] concurrency` 省略；另一次 provider 连续回 429（v0.3，2026-10-04） | v3 preset 下默认 8（没有 preset 仍 1），不进计划身份；429 作未计费重试，报告的速度一节记下重试数；用量上限仍暂停活动 |

扩展 `test_bench_phase.py`、`test_benchmark.py`、`test_report.py`；Harbor 使用 fake trial 默认测试，真实环境 smoke 单列。

（v0.3，2026-10-04）验收命令与测试。命令：`ouroloop freeze --config <v3 配置>` 打印每个领域的划分计数与分层计数；`ouroloop bench --config <v3 配置> --phase smoke --checks-only` 确认每个领域可装载与 agent 组件可导入；`--phase report` 输出 §4 的新字段。测试（都是新增）：
- （第 5 步，已实现 2026-10-05）`test_benchmark.py::test_a_stratified_split_balances_assertion_mix_and_tool_family`（BR6-19）、`test_a_stratified_split_reads_no_result`（BR6-19）、`test_a_frozen_hash_split_is_kept_when_the_rule_changes`（BR6-18 的 Marketing 分支）、`test_grading_of_a_pooled_split_still_returns_train_only`（合并清单下 §4 第一段的边界）；
- `test_bench_phase.py::test_max_candidates_per_round_up_to_the_preset_is_accepted`（BR6-21；已实现 2026-10-05）与 `test_nested_harness_tables_tighten_per_key_and_are_validated_without_a_preset`（已实现 2026-10-05：`research_session` 四项与 `screen.passing_sample` 只能调低，`screen.min_net_flips` 是下限、只能调高）；`test_paid_run_safety.py::test_concurrency_is_a_machine_setting_the_frozen_plan_does_not_carry`（BR6-24；已实现 2026-10-04，原拟名 `test_concurrency_defaults_to_eight_and_is_not_in_the_plan` 未采用）；（第 5 步，已实现 2026-10-05）`test_bench_phase.py::test_a_domains_list_freezes_one_split_per_domain_and_pools_them`（BR6-18）、`test_a_single_domain_is_a_list_of_one`（BR6-22）、`test_the_v3_preset_checks_every_listed_domain_loads`（§2 领域锁的改动）、`test_an_old_config_reads_under_the_new_reader`（BR6-22）；
- `test_report.py::test_each_arm_shows_its_cost_per_attempt`（已实现，第 2 步）、`test_the_ledger_is_listed_per_candidate_without_dev_task_ids`（已实现 2026-10-05）；研究位结局在报告里的写法由既有 `test_c4_says_how_a_slot_without_a_campaign_ended` 覆盖（`no_slot_pool`/`no_headroom` 两行，第 2 步）。**待实现**（2026-10-05 核对，尚无同名测试）：`test_a_gate_line_states_pooled_readings_and_the_confirmation`（gate 行的合并读数数与确认测量）、`test_a_slot_shows_its_headroom_or_no_slot_pool`（headroom 数字在报告里）、`test_an_old_receipt_shows_the_strict_rule_and_no_cost_term`（BR6-22）；随第 5 步的多域报告（已实现 2026-10-05）：`test_the_final_table_lists_the_best_of_k_arm_only_when_it_ran`、`test_marketing_test_rows_are_marked_spent_and_kept_out_of_the_claim`、`test_task_effect_is_broken_down_per_domain`（BR6-23），另有 `test_it6_36_the_report_lists_cost_per_attempt_the_ledger_and_headroom_per_domain`（IT6-36）。2026-10-05 评审后另加：`test_task_gate.py::test_a_receipt_over_several_domains_carries_per_domain_means`（真实的多领域 gate 收据带 `metrics.per_domain`）、`test_bench_phase.py::test_a_domain_list_changed_after_bootstrap_is_refused_at_startup`（领域列表在启动后改变，启动检查拒绝；某领域的划分在磁盘上被换掉，打开活动时拒绝）、`test_a_schedule_without_the_presets_research_slot_reports_c4_deferred`（研究位推迟时 C4 的理由）。
限流重试与用量暂停的 provider 行为由 M1 的 `test_claude_code_provider.py` 覆盖，M7 只验证报告里的计数（BR6-24）。
