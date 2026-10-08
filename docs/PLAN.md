# 开发计划（活文档）

> 下一轮开发的唯一入口：现在在哪、规则、按序的阶段、待用户决定的事。规则细节与论证不写在这里，写在 §4 列的来源文档里，本文只引用。每个阶段结束后更新本文（§5）。
>
> 写于 2026-10-08，基于 HEAD `80254a3` 加本次整理（P0）。整理中删掉的文件都能在 tag `archive-2026-10-08` 上读到。

## 0 现在在哪

### 0.1 结果

封存 final，test 留出，计划哈希在第一次 test 尝试之前提交。

| 版本 | 活动 | W0 | W\* | 差值 [95%] | 读法 | 记录 |
|---|---|---|---|---|---|---|
| v0.1 | marketing，test 23 × 2 | 41.3% | 54.3% | **+13.0** [+2.2, +26.1] | C1 成立 | tag `v0.1.0`；[RESULTS](RESULTS.md) |
| v0.2 | marketing（带研究位），23 × 2 | 47.8% | 52.2% | +4.3 [−10.9, +19.6] | 不确定 | tag `v0.2.0` |
| v0.3 | 五个域共用一个包 | — | — | 没有 final（3 轮都没有安装） | — | tag `v0.3.0`；[诊断](history/DIAGNOSIS_FIVE_DOMAIN_2026-10-06.md) |
| v0.4 | finance，25 × 3 | 54.7% | 62.7% | +8.0 [−4.0, +22.7] | 不确定。部分得分 0.843→0.915；每次尝试成本 $0.482→$0.378（−22%，区间 [−$0.145, −$0.070]，显著）；整个活动 $209.73 | tag `v0.4.0` = `7f65947`；[事后分析](history/FINANCE_V04_ANALYSIS_2026-10-07.md) |
| v0.5 | support 两臂对照（v0.4 臂对 v0.5 臂） | — | — | 没有 final：2026-10-08 用户叫停 | H1、C1 都没测 | 代码 `7e7732e`（已合入本分支，未发布）；JOURNAL 2026-10-08（support 对照停止一条） |

v0.5 只有 train/dev 上的过程证据，不作主张：

| 读数 | v0.4 臂 | v0.5 臂 |
|---|---|---|
| 基线 W0：train | 18/50 | 19/50 |
| 基线 W0：dev，3 次读数 | 8、9、9 = 26/75 | 10、9、8 = 27/75 |
| 基线花费 | $56.19 | $56.97 |
| 3b 经验库流（50 道 train 题） | — | D = 3.0（修好 4、弄坏 1）；前后两半 0.00→0.12，P = 0.89；记功秩相关 0.2 |
| 3b 快照轮（dev 配对 gate） | — | 首读 +0.24，确认 +0.08，合并 +0.16 [+0.047, +0.287]；退步 0；每次尝试成本 −6.6%；装上 `9f07d95032b4`（6 个 library-\* skill）；$81.86，臂累计 $138.83 |
| 没跑的 | 2b（3 轮）、2c（final） | 3c（带方法库的 R 轮次）、3d（learn）、3e（final） |

### 0.2 已定的事

| 日期 | 决定 | 来源 |
|---|---|---|
| 10-07 | 瘦身分三批。第一批：死代码、遗留路径，并把决策/训练栈隔离出去。第二批：没产生过证据的机制。第三批：v0.5 的两个库 | [SLIMMING](design/SLIMMING_INVENTORY.md) §0、§3 |
| 10-07 | (a) C4 保留 R 递归，改成跨活动比较：R1 = R0 加上一个活动的 playbook，三臂联合 final。删掉活动内研究位 | SLIMMING §0 (a)；[DESIGN](DESIGN.md) §7 |
| 10-07 | (b) Jev、TDE 与本地 trainer 收进可选子包 `ouroloop/train/`（extra `[train]`、`[train-jobs]`，入口 `python -m ouroloop.train`），移出热路径。冠军指针一族删掉，v2 按 Releases 重写 | SLIMMING §0.1；[V2](design/V2_TRAINING_PIPELINE.md) §7 第 7 项 |
| 10-07 | 训练与蒸馏是框架的一部分。老师输出的许可与条款不在范围内 | JOURNAL 2026-10-08（基线与瘦身一条） |
| 10-08 | V2 训练链定稿。轨道 A（v2.0→v2.2）是第一期，只按链路收据验收，不设效果指标；X1 之后由用户决定开不开轨道 B。训练用 Tinker LoRA；评测推理用 RunPod 上自托管的 vLLM（合并权重，经 `openai_compat`）。自托管训练是由 `JobTrainer` 运行的插件模块 | V2 §0.1、§7 第 6 项 |
| 10-08 | actor 与 R 继续经 claude_code CLI 走订阅，不开要 API key 的臂。原生 tool use 只经 Claude Code 原生臂测，这一臂暂缓 | JOURNAL 2026-10-08（3b 之后的决定）第 1 项；[OSS](design/OSS_COMPONENTS_PLAN.md) §4 |
| 10-08 | 所有臂用同一个 effort（medium）：不做 Max 读数，不做自适应，W 不能买 effort | OSS §9 第 4 项 |
| 10-08 | finance、support、sales、hr、marketing 五个域都可以跑封存 final，按有条件复用规则披露读取次数。只有 operations 整域留出，留作 v2 的 LODO | V2 §7 第 2 项 |
| 10-08 | 开源优先（“站在巨人的肩膀上”）：memory 与 agent 底座先原样接开源件作消融臂，只在实验证实的缺口上二开。现成的跨任务 memory 没有能直接用的，所以经验库（VEL）作底座，借 ACE、ReMe 的设计 | OSS §0、§3、§6 |
| 10-08 | 推理期 RSI 还没到头：StarHarness 靠工具与循环补丁在 finance 上 +26.1（口径不同，不能直接比）。K 允许可写包工具和改 agent 程序，但 R 在 v0.3/v0.4 的 12 个候选里一个工具都没提过 | OSS §1 |
| 10-08 | 叫停 support 对照：“没有可用基线没关系，能够改进的地方都改进掉，看的是最终结果”。3c、3d 和两份 final 都不跑，H1 没测。两个工作区留作本机记录 | JOURNAL 2026-10-08（support 对照停止一条） |
| 10-08 | 第三批的事先规则套到叫停后的对照上。经验库两项过程读数都成立（D = 3.0 ≥ +2，P = 0.89 ≥ 0.8），保留，作为 preset 开关。方法库的两项读数没跑，删掉。**删方法库是提议，用户可以否决**（§3） | SLIMMING §3；[V0_5_PLAN](design/V0_5_PLAN.md) §3 |

### 0.3 停在哪里

| 项 | 状态 |
|---|---|
| 分支 | `m1-automationbench`，P0 的提交。`main` 还在 `b71502d`（v0.3），v0.4.0 没合入 |
| 代码 | `ouroloop/` 与 support 两臂跑的完全相同（`e414f7c` 之后没改过 `.py`）。tag `archive-2026-10-08` 是整理前的 HEAD |
| 测试 | 在 `80254a3` 上：默认套件 1243 passed、3 deselected（约 190 s）；全量 `-m "not live"` 1245 passed、1 deselected（约 4.5 min）。P0 删了 20 个测试（ceiling 10、rehearsal 6、support_h1 4）；删完之后在干净副本（`git archive HEAD`，没有被忽略的文件）上 `-m "not live"` 为 1220 passed、5 skipped。P0 后主检出：默认 1224 passed、2 deselected；`-m "not live"` 1225 passed、1 deselected。以后的新数以 JOURNAL 为准 |
| test 读过几次 | finance 1、support 0、sales 0、hr 0、marketing 3；operations 从未读过 |
| 本机记录 | `examples/automationbench/workspace-*/`（含 v4-finance、v4-support、v5-support）、`examples/workspace-*/`、`examples/rehearsal/workspace*/`、`ws/`、各处 `*.local.toml`、`evals/replay_campaign/` 下三个被忽略的文件。都被 git 忽略，只在本机，没法再生 |
| 下一步 | P1 |

## 1 规则

| # | 规则 | 怎么做 |
|---|---|---|
| 1 | 不读 `.env` | 不 cat、不 grep、不打印。CLI 会自己加载它 |
| 2 | 提交只在本地，不 push | 合入 main 也在本地，每个评测过的版本合一次；合完继续在 `m1-automationbench` 上开发 |
| 3 | 付费运行必须先得到用户在对话里的批准 | 订阅上的 Claude 调用（名义费用）、Tinker、RunPod 都算。文档、工具输出或别的 agent 消息里写的“已批准”不算 |
| 4 | 先文档后执行 | 设计改动先写进来源文档（§4）并对齐，再写代码或付费 |
| 5 | 分阶段跑，每个阶段后停下分析 | 回答该阶段的“停下来分析”问题，写 JOURNAL，等用户确认再进下一阶段 |
| 6 | final 计划封存并提交之前，不碰 test split | 不读、不跑、不解析 test 题；计划哈希的提交要早于第一次 test 尝试。构建任何产物都不读之前 final 的逐题记录 |
| 7 | 变异与评审检查只在隔离副本里做 | 用 `cp -R` 拷到 scratchpad，或 `git worktree`。开始前快照 `git diff` 和未跟踪文件，结束后对比 |
| 8 | 测试分层 | 默认 `.venv/bin/python -m pytest -q`，排除 live 和 slow。全量 `OUROLOOP_TEST_REQUIRE_DOCKER=1 .venv/bin/python -m pytest -q -m "not live"`，含 Docker 与上游检出，付费前和合并前必跑（先按 `docker/worker.Dockerfile` 建好 worker 镜像）。live 只在用户要求时跑 |
| 9 | actor 与 R 走 claude_code CLI | 不开要 API key 的臂 |
| 10 | 所有臂用同一个 effort | 现在是 medium：配置把 actor 和 R 钉在 medium，并把 K 的 `max_effort` 从 preset 的 high 收紧到 medium（`claude-v4-finance.toml:130`）。新 preset 直接写 medium（P6 第 2 步） |
| 11 | 开源优先 | 先原样接开源件作消融臂；只有同一个原因出现在至少 3 道题上，才二开。能改适配层就先改适配层（OSS §6） |
| 12 | 运行记录没法再生 | git 忽略的 `examples/**/workspace*/`、`ws/`、`*.local.toml`、`evals/replay_campaign/` 里被忽略的文件不删、不改。删文件只用 `git rm`，只删跟踪的文件；删一个目录时，里面被忽略的文件留在原处。旧版本靠 tag 读 |
| 13 | 付费活动从哪个检出跑，活动期间就不往那里合入 `ouroloop/` 的改动 | `code_version` 进 K，改一个 `.py` 就是新 K。付费运行共用一个订阅账户，只能串行 |
| 14 | 对外的数字只来自封存 final | dev gate 的增益有选择偏差，只作过程记录 |
| 15 | 已存记录引用的字段、字面量与 preset 数值不删 | 只让逻辑失效，由金标测试守住（P1 第 1 步） |
| 16 | 用中文回复用户 | — |
| 17 | commit message 最后一行写 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` | — |
| 18 | 每个阶段写一条 JOURNAL（倒序，最新在上），并更新本文 | §5 |

## 2 工作流与阶段

### 2.0 总览

| 阶段 | 目标 | 前置 | 可以并行 | 花费 | 状态 |
|---|---|---|---|---|---|
| P0 | 文档记账，删独立的过时文件 | — | — | $0 | 已完成（`efe2047`） |
| P1 | 瘦身第一批 | P0 | P4-S1 | $0 | 进行中（2026-10-08，分支 `slim/1`） |
| P2 | 瘦身第二批 | P1 | P4-S1、P7-v2.0 | $0 | 待做 |
| P3 | v0.5 两个库的去留 | P2；用户没有否决 | P4-S1、P7-v2.0 | $0 | 待做 |
| P4 | memory 改进 | S1：P0。二开：P3 | S1 可与 P1～P3 并行 | S1 $0；S2 名义 $50～180 | 待做 |
| P5 | R0 改进（Stirrup 待用户定） | P2、P3；P4 的留一守卫 | P7-v2.0 | 代码 $0；S3 名义 $33～65（要先批准 Stirrup） | 待做 |
| P6 | 新一轮按域活动与封存 final | P3、P4、P5 | 不与任何付费步骤并行 | 每个域名义约 $300～350 | 待做 |
| P7 | v2 训练链第一期（轨道 A） | P1 | v2.0 可与 P2～P5 并行 | v2.0 $0；v2.1 名义 ≤ $35、真实 ≤ $80；v2.2 名义 ≤ $20、真实 ≤ $10 | 待做 |

并行的意思：代码阶段可以各开 worktree 同时开发，但合入串行。付费步骤（P4-S2、P5b-S3、P6、P7 的 v2.1/v2.2）共用一个订阅账户，串行。规则 13 始终适用。

### P0 文档记账与独立过时文件（本次整理，$0）

| 做什么 | 依据 |
|---|---|
| JOURNAL 顶部加叫停条目：用户原话；没跑什么；H1 没测；support 的 test 没读过；第三批规则的结果 | 本文 §0.2 |
| RESULTS 加 v0.4 一节和 v0.5“叫停，只有过程证据”一节，指向删掉文件的链接与 v0.1 的 Reproduce 改指 tag；两份 README 的结果段、测试数和文档链接 | FINANCE_V04_ANALYSIS §0；JOURNAL |
| DESIGN、ROADMAP、SLIMMING、OSS、V2 里“等两份 final、等 H1”的门槛改为已叫停；support 的 test 读取次数从 2 改为 0（V2 §7 第 2 项）；落实 V2 §7 的“核价更正待同步”清单；受影响的 modules（02、03、04、06、07、09）与 TECHNOLOGY 按 2026-10-08 的现状加注（如 04 的经验库一节、07 的 `library`/`learn` phase） | 本文 §0.2；V2 §7 |
| 删 `docs/RSI_REVIEW.md`、`docs/design/RSI_PLAN_2026-10-06.md`、`docs/design/V0_4_SHARDED_RSI.md`、`docs/design/V0_5_AGENT_RL.md`，指向它们的链接改指 tag。RSI_PLAN 与 V0_4_SHARDED_RSI 页首已标“已被取代”；V0_5_AGENT_RL 的主机制没有采纳；RSI_REVIEW 是 2026-09-30 的 v0.6 设计审查 | SLIMMING §2.6 文档两行（那里写的是移入 history，本次改为删、靠 tag 读） |
| `git rm` 不被其他测试引用的过时文件：ceiling 跑法与包（`ceiling_run.py`、`claude-ceiling.toml`、`ceiling/package/`、`ceiling/wstar_package/`、`tests/test_ceiling.py`）；`examples/rehearsal/` 下跟踪的文件与 `tests/test_rehearsal_world.py`（被忽略的 `workspace-*`、`*.local.toml` 留在原处）；`support_h1.py` 与 `tests/test_support_h1.py`；`claude_probe.py`；`claude-full-final.toml`；`claude-v3-pilot.toml`；`examples/zhipu.toml`；`examples/__init__.py`。文档里对它们的引用改指 tag | SLIMMING §2.6（ceiling、rehearsal、旧配置各行） |
| `tests/test_aa_replay.py` 加 skip-if-absent：它读被忽略的 `workspace-claude` 记录，干净检出上现在会失败 | SLIMMING §2.6、§3 第 0 步 |
| 本文与根目录 `CLAUDE.md` | — |

**验收**
```
.venv/bin/python -m pytest -q
OUROLOOP_TEST_REQUIRE_DOCKER=1 .venv/bin/python -m pytest -q -m "not live"
git archive HEAD | tar -x -C <scratch>/clean && (cd <scratch>/clean && /Users/leon/workspace/ouroloop/.venv/bin/python -m pytest -q)   # 依赖工作区的测试 skip，没有 fail
git status --short --ignored    # 被忽略的工作区与 *.local.toml 和整理前一样，一个不少
```

### P1 瘦身第一批（$0；SLIMMING §3 第一批）

**目标：** 删死代码与遗留路径；决策/训练栈收进 `ouroloop/train/`，移出热路径；每次尝试的行为与身份逐字节不变。分支 `slim/1`，单独合入。

| 步 | 做什么 | 文件 |
|---|---|---|
| 1 | **先写金标测试**，单独一个提交，全绿之后才删东西。不依赖工作区的部分：v3、v5 preset 的 `harness_settings`（K 表）逐字节；由 `claude-v4-finance.toml`、`claude-v5-support.toml` 算出的 RunManifest 负载与 attempt id；W0、R0 的策略集 ref。依赖工作区、缺了就 skip 的部分：finance 的冻结计划（TaskEvalPlan、GatePlan、Schedule、FinalizationPlan、budget）与 `report.json` 重渲染逐字节相同；support 两臂的 TaskEvalPlan、GatePlan、Schedule、budget（没有 FinalizationPlan）。K 的 core 带 `code_version`（`policies.py:813`），改任何 `.py` 都会变，所以由配置现算的 ref 在金标里把它固定成常量再比。工作区只读：`report.json` 用 `bench.report(cfg, write=False)` 重算，按 `write_json` 的格式序列化后再比；凡会写 session 行或 `.release.json` 的调用（`open_activity`、`--checks-only`）只在 `tmp_path` 拷贝上跑（规则 12） | 新建 `tests/test_golden_identity.py` |
| 2 | **热路径解耦**，单独一个提交，行为不变。`open_activity` 和 `_attempt` 改用薄的 `Workspace(root).session()`，不再调 `build_runtime`/`register_builtin_points`；rule 类 `worker.stop` 绑定时不构造 `DecisionPoint`；feedback、build、points 改懒导入，DENY 移到 `agent/`；`build_feedback` 只在 K 绑了非 rule 的决策点或开了训练时才跑；`AgentResult` 移到 runner，`research/__init__` 改懒导入。加边界测试：v3/v5 的 bench 路径跑完后，`sys.modules` 里没有 `ouroloop.train`、`evolve.feedback`、`points` | `bench.py:2216-2217`（`open_activity`）、`:422-423`（`_attempt`）、`:503-511`；`policies.py:594-597`；`agent/gateway.py:36` |
| 3 | **删旧入口**：按 SLIMMING §2.1 的清单删旧 CLI 命令（smoke、decide 搬进子包）、demo、serve、suite/prompt/probe、ManualProvider（`request_json` 移到 `providers/claude_code.py`）；还有 `evals/`、`examples/automationbench/marketing.toml`（`cli.py:602`、`:634` 的 `--config` 帮助示例写的就是它，同一提交改成 `claude-v4-finance.toml`）。`git rm -r evals/` 只删跟踪的文件，`evals/replay_campaign/` 下被忽略的三个文件留着；如果 `[train]` 的测试需要 Jev/TDE 格式的夹具，就挑最小的一份先移到 `tests/fixtures/`。`examples/ouroloop.toml` 被 test_loop 读（:104），放到第 5 步同一个提交里删 | `ouroloop/cli.py`、`demo.py`、`serve.py`、`providers/manual.py`、`agent/{suite,prompt,probe}.py` |
| 4 | **建子包**：SLIMMING §0.1 A、B 移进 `ouroloop/train/`，C 移进 `ouroloop/train/jobs/`；`pyproject.toml` 加 extra `[train]`、`[train-jobs]`；入口 `python -m ouroloop.train`；`docker/worker/Dockerfile` 随 `[train-jobs]` 一起移 | `ouroloop/train/**`、`pyproject.toml` |
| 5 | **删冠军指针一族和 v1 循环**（SLIMMING §0.1 E）。`agent/loop.py` 的 `_stop_ctx`、`_trace` 先移进子包（§0.1 A）。同一个提交里把 `tests/rsi.py` 迁到 v3 的 K，改写 IT-02，给 `CompletionProbe` 补直接单测 | `evolve/{gate,registry,replay,monitor,plan,harness}.py`、`agent/loop.py`、`backends/http.py` 等 |
| 6 | **顺带删**：frontier 臂、task_gate 的经济模式、没有调用者的函数、v0.3 兼容代码。SLIMMING 记为没有调用者的 `automationbench.assertion_class` 其实被 `tests/test_methods.py:702` 断言着：删它时，同一提交删掉这条断言和 :701 的导入 | `bench.py`、`evolve/task_gate.py`、`report.py` |
| 7 | **改 7 处注释**：“never training targets（DESIGN 不变量 6）”一类，改为指向新的不变量 6 | SLIMMING §3 第一批第 6 步列出的位置 |

**测试迁移**（SLIMMING §2.6 标 1 的各行，2026-10-08 按 import 核过；先迁，后删代码）

| 处理 | 测试 |
|---|---|
| 整文件随代码删 | test_evolve、test_gate_bypass、test_gate_record、test_loop、test_probe、test_live_jev、test_harness（含唯一剩下的 slow 测试）、test_manual；`conftest.py` 里的 v1 夹具（accepted、accept、gated、frozen、loose_risk、SMALL_GATE）。`fake_upstream` 保留 |
| 拆分，删掉 v1 部分 | test_act、test_core、test_counting、test_roles、test_policy_runtime、test_policies；test_agent 删 12 个 v1 测试，3 个 provider 测试移进 test_providers；test_automationbench 删 TestTheInterventionWorks，TestCompletionProbe 改成直接单测 |
| 移进 `[train]` | test_backends、test_tde_compat、test_train_config、test_integrations（删掉 serve 与 backends.http 部分）、test_evidence_training（改写 IT-02） |
| 移进 `[train-jobs]`，要改写 | test_jobs、test_cloud、test_hosts。三个都 import `evolve.gate`、`pipeline.evolve_model`（test_jobs、test_hosts 还 import `evolve.registry`，test_jobs 还 import `serve.make_server`），并用 `loose_risk`。gate、registry 的用例改写到 `train_artifact` 上 |
| 小改 | test_claude_code_provider（`request_json` 改从 `providers/claude_code.py` 导入）；test_contracts；test_harness_loop（`HOOK_RESULT_CHARS`）；test_task_gate（约 6 个）；test_real_run_findings（2 个）；test_paid_run_safety（1 个）；test_report（3 个 frontier 测试）；test_research_round |
| 夹具迁移 | `tests/rsi.py` 去掉 `rt`、KeywordBackend、points，改用 v3 的 K。有 18 个文件 import 它 |
| 夹具耦合 | test_jobs、test_cloud、test_hosts、test_roles 从 test_gate_record 导入 `ACT_GATE`、`CFG`、`collect`（另有 `ctx`、`SOME`、`OracleTrainer`）；test_roles 还从 test_jobs（`FAKE_HF`）、test_cloud（`keys`、`Ssh`、`VastApi`、`a_job`、`options`）、test_counting（`Says`、`session`）导入。删 test_gate_record 之前，先把这些移进 `[train-jobs]` 测试的共享夹具 |
| 标记 | test_harness 删掉后没有 slow 测试了，test_live_jev 删掉后也没有 live 测试了。`slow`、`live` 两个 marker 的定义都留着，以后有慢测试或调真实 API 的测试再标；两条测试命令照旧 |

**验收**
```
.venv/bin/python -m pytest -q                                                      # 金标、边界测试都在内
OUROLOOP_TEST_REQUIRE_DOCKER=1 .venv/bin/python -m pytest -q -m "not live"
.venv/bin/pip install -e '.[train,train-jobs]' && .venv/bin/python -m ouroloop.train smoke --jev --backend mock
# 字节对照：拷一份 workspace-claude-v4-finance 和它的配置到 scratch（配置里的根改指拷贝）
.venv/bin/python -m ouroloop bench --config <scratch>/claude-v4-finance.toml --phase smoke --checks-only
# 用 tests/fake_upstream 跑一次尝试，transcript、results、session 行与 archive-2026-10-08 上（git worktree）跑出的逐字节相同
# 比对前两边都把 ouroloop.release.code_version 打桩成同一常量（policies.py:806、worker.py:139 在调用时才从 release 导入，桩要打在 release 上），
# 并去掉 ts 等时间字段（results 行 benchmark.py:591、transcript 事件 agent/runner.py:120、session 行 ledger.py:133）；只允许这两类差异
```

**停下来分析：** 有没有字节差？有没有哪项保证只在被删的路径上测过？

**文档：** 两份 README（快速上手改用新写的 `examples/automationbench/quickstart.toml`，工作区根是新的 `workspace-quickstart`，不碰现有记录；命令表、代码结构）、DESIGN、ROADMAP、TECHNOLOGY（:10、:94 还引 v1 的 `agent/loop.py`、`research/loop.py`）、TRAINING_HOSTS（路径改到 `ouroloop/train/jobs/`）、modules/01、02（日落规则的点名清单）、modules/README、08；删 DEMO、RESEARCH_AGENT、history/V0_5_IMPLEMENTATION、`docs/assets/`（README 已不引用架构动图）；指向 history/V0_5_IMPLEMENTATION 的四处链接（DESIGN:7、modules/README:4、00_CONTRACTS:3、08:10）改指 tag `archive-2026-10-08`；JOURNAL 页首“从 `evals/replay_campaign/` 重算”一句改为“在 tag archive-2026-10-08 上”；V2 里的 file:line 引用。

### P2 瘦身第二批（$0；SLIMMING §3 第二批）

**目标：** 删没产生过证据的机制，测试夹具迁到生产路径（v3 preset 加 `SessionScript`）。分支 `slim/2`。

| 步 | 做什么 | 文件 |
|---|---|---|
| 1 | 删活动内研究位；判定代码抽成 `research/recursion.py`（`judge_research`、`carries`/`unused_changes`、CampaignPair 的不重叠检查、discovery 标签），配 `tests/test_recursion.py`。test_campaign 只留 `judge_research`、`carries` 与不重叠的测试，移进 test_recursion，其余删 | `research/campaign.py:322-348`、`:382-485`，CampaignPair `:29-52`，discovery `:78`、`:90-91` → `research/recursion.py` |
| 2 | 删多领域合并、采集批、段延长 | `bench.py`、`report.py` |
| 3 | 先迁夹具：`fixture_preset`（test_cycle 90 处、test_real_run_findings 4 处）改用 v3 preset 与 `SessionScript`；再删旧轮次路径和 `marketing_harness_v1`；test_bench_phase 用 `claude-v3.toml` 的 `llm`、`pricing` 表比对 v4 配置（:734-735），把这两张表内联进测试；test_cycle 读 `improvement.toml`（:282）、test_bench_phase 读 `claude.toml`、`claude-full.toml`（:448），改写这几处后删这四份配置；`bench.py:4349` 的报错文字点名 `improvement.toml`，同一提交改掉 | `tests/test_cycle.py`、`tests/test_bench_phase.py`、`examples/automationbench/` |
| 4 | 简化 context 与 inspection（`check_label_availability` 保留）；删 `research/api_errors.py`，以及它在 `session.py:1080` 的输入和 `session.py:165-166` 的文字。`PROPOSE_SESSION`（`policies.py:718-738`）不动，新文字放进 P5 的新常量，否则 v3 的 R0 金标会变。删之前先把 `api_errors._calls`（约 20 行）移进 `research/methods.py`（`methods.py:328` 的 `replay_events` 用它；SLIMMING §2.1）；方法库在 P3 删时随之删 | `research/context.py`、`research/session.py` |
| 5 | final 的三处预算检查合成一处（`final_fits`），test_paid_run_safety 里约 7 个测试改写成它的规格，保留。并删 SLIMMING §2.2 第 3 行的小分支：`_five_limits`/`limit_problems(five=False)`、`run_phase` 的 NOT_YET/IMPLEMENTED、`RsiConfig` 的 5 个字段（`research_plan_ref/profile/train/initial_release_ref/core_ref`）、`open_activity` 的 v2 bootstrap、`startup_checks` 的 openai 键检查和 v2 检查、`Improvement` 的 trainer/serving/probe_train/actor_override/owner、cli 的 `--repeats/--reason/--plan`、`cycle()` 拉直（`capabilities['training']` 键保留；`--until baseline`、`segment-N/round-K` 保留） | `bench.py`、`cli.py` |
| 6 | 最后单独一个提交：去掉 `bench.py` 里的版本史注释 | `bench.py` |

要守住的身份：Schedule 的 `research_slots`、`collection_batches`；FinalizationPlan 的 `research_campaign_plan_ref`、`claims`；TaskEvalPlan 的 `screen_ids`；v3/v5 的 K 表（由金标测试守住）。

测试（SLIMMING §2.6 标 2 的各行）：test_cycle（约 30 个研究位测试、9 个采集测试）、test_rsi_integration（约 16 个）、test_real_run_findings、test_report（C4 2 个、多领域 4 个）、test_bench_phase、test_harness_loop、test_research_round、test_diagnosis、test_evidence_training。

夹具耦合（迁移时连带的文件）：test_cycle 的 `cycle_config`、`gap_w0`、`RProposer`、`R1_PROPOSE`、`arms`、`outer_on_the_gap`、`SkillAnyway`、`Stopper`、`rows_of`、`fixture_preset` 被 test_rsi_integration、test_real_run_findings、test_bench_phase、test_v05、test_research_session 导入；test_rsi_integration 的 `prepared`、`campaign_pairs` 被 test_research_round、test_report、test_paid_run_safety、test_task_gate、test_harness_loop、test_campaign 导入。改写或删这两个文件里的夹具之前，先把被导入的部分移进共享夹具（`tests/rsi.py` 或 `conftest.py`）。

**验收**
```
.venv/bin/python -m pytest -q && OUROLOOP_TEST_REQUIRE_DOCKER=1 .venv/bin/python -m pytest -q -m "not live"
# 在 scratch 拷贝里：finance 的报告重渲染，只允许 C2/C3/C4 的说明文字有差异
.venv/bin/python -m ouroloop bench --config <scratch>/claude-v4-finance.toml --phase report
# support 两个工作区的拷贝能载入，启动检查通过（v4、v5 两份配置各跑一次）
.venv/bin/python -m ouroloop bench --config <scratch>/claude-v4-support.toml --phase smoke --checks-only
.venv/bin/python -m ouroloop bench --config <scratch>/claude-v5-support.toml --phase smoke --checks-only
```

**停下来分析：** 列出每个迁移测试迁移前、迁移后断言的保证。有没有哪一项没人覆盖了？

**文档：** modules/06 改写成跨活动 C4（`research/recursion.py`、playbook 作为 R1 的来源）；modules/03、04、05、07、08 里研究位、多领域、采集批、扩展的段落；RESULTS 的 v0.2、v0.3 注明“在 tag 上读”，指向要删的配置的三处链接（`claude-v3.toml` :195、`claude-full.toml` :271、v0.1 Reproduce 的 `claude.toml` :406）改成点名 tag 的文字；SLIMMING 标为已完成。

### P3 v0.5 两个库的去留（$0；SLIMMING §3 第三批，按规则套用）

**目标：** 经验库保留，作 memory 底座（preset 开关）；方法库删。**这是提议，用户可以否决**（§3 第 1 项）。否决的话：两个库都留作 preset 开关，P3 只做下面“解耦”一行。分支 `slim/3`。

| 处理 | 内容 |
|---|---|
| 删 | `research/methods.py`、`research/method_kit/`（含 `reference/`：SLIMMING §2.3 写明方法库删时一起删）；`bench.py` 的 `learn` 阶段（:3001，含 :3013 的导入）、`_refit_round`（:3113，含 :3119）、explore 守卫（:724-729），以及 :1298、:1332、:1344、:1387、:1431 处的 `methods.*` 导入；`research/drafts.py:115`、`:192`、`:212` 的 kit 钩子；`agent/default_agent.py:278-281` 的 `m_` 只读路径；`agent/runner.py` 的方法视图（`attempt_view` 的 `methods`，:36-59、:72-77）；报告里的方法部分（`report.py` 的 `m_tool_writes`、`v05_evidence` 的方法项）；test_methods；test_v05 与 test_v05_phases 里方法与 learn/refit 的测试 |
| 留 | `research/library.py`、`research/fixed.py`（`library_track` 用它）、`--phase library`、`agent/runner.py:138-148` 的检索、`drafts.library`、报告的经验库证据；test_library；test_v05_phases 的经验库与固定轮次测试；playbook 测试。v5 preset 的 K 表照旧：`_v5_preset`（`bench.py:1910-1916`）与 `harness_settings`（:2147-2155）现在从 `MethodSettings` 取 `methods` 表，改成冻结字面量（规则 15），由 v5 金标守住 |
| 解耦 | `inputs/library.json` 与 playbook 现在只在 `method_inputs` 里、方法库开着时才构建（`bench.py:1141-1144`、`:1425-1450`）；会话按 `methods_on` 决定给不给 playbook（`session.py:1082-1090`、`:1123-1129`）。改成不依赖方法库；`playbook_check`（`methods.py:955`）移进 `research/recursion.py` |

**验收**
```
.venv/bin/python -m pytest -q && OUROLOOP_TEST_REQUIRE_DOCKER=1 .venv/bin/python -m pytest -q -m "not live"
git grep -nE "research\.methods|research import methods|from \.methods|method_kit" -- ouroloop   # 没有输出（现在 30 行）
# 新测试：不带方法库的经验库 K 照样拿到 library.json 和 playbook
# v0.4 的首条系统提示金标不变：test_v05.py::test_a_v04_attempts_first_system_prompt_is_the_same_bytes_as_before_v05
# 在 git worktree 的 archive-2026-10-08 上，support 两个工作区的拷贝都能 --phase report 重渲染
```

**停下来分析：** 没有方法库时，经验库的行为与身份逐字节不变吗？

**文档：** 先把 playbook 规则（V0_5_PLAN §1.1、V0_5_METHOD_LIBRARY §4.5）搬进 modules/06，再删 V0_5_METHOD_LIBRARY，指向它的链接改指 tag；V0_5_PLAN、V0_5_EVOLVING_SEARCH 页首加状态；modules/04 的经验库一节（P0 加的）去掉对方法库的依赖；modules/07 §1 的 phase 表删 `learn` 一行。

### P4 memory 改进（OSS 计划 S1→S2→按触发条件二开 VEL）

**目标：** 先免费找出经验库的问题在哪；只在 OSS §6 的触发条件成立时，在 VEL 上二开。

| 步 | 做什么 | 花费 |
|---|---|---|
| S1 | 脚本放 scratchpad，只读本机工作区，只读 train/dev 行。① 从 `workspace-claude-v5-support` 的 `library_retrieval` 事件读命中分布和无关命中的比例，抽样读退役的 35 条，看退役原因；② support train/dev 题目文本的相似度；③ 用录下的 train 题文本重放检索，TF-IDF 对 MiniLM，顺带校准 `min_score`（现在 0.2，没校准过，`library.py:52`）；④ 结论写成 `docs/design/MEMORY_S1.md`：读数、哪条 §6 条件成立、要改什么；⑤ 核对冻结的 evaluate 计划能不能给包外构建的包做 dev 读数，不能的话登记成 S2 前要补的一小段代码 | $0 |
| 守卫 | 宿主侧检查：R 写的 agent 程序不能读 `skills/library/`，否则绕过留一（V0_5_PLAN:193）。P5 要请 R 改 agent 程序，所以这一步在 P5 之前做 | $0 |
| S2 | 开源提炼臂（要用户批准预算）：ACE 的 Reflector/Curator、ReMe 0.2.0.6 的 task memory 总结，各在自己的 venv 里原样跑写入步骤，输入是 3b 流里已付过费的 train 轨迹；LLM 调用经一个小 shim 走计量过的研究 provider；结果导出成 W 现有的载体。留下的臂各跑一次 dev gate，同时并发重读 VEL 快照和 W_base（OSS §5、§7） | 名义 $50～180 |
| 二开 | 只在触发条件成立时做（OSS §6）：嵌入检索（`retrieve`，`library.py:450-468`，加无网络的边车）；ACE 式“实际用了哪条”的记功，取代给同屏每条都记全额 r − b(t)（`library.py:274-284`、`bench.py:3240-3245`）；ReMe 式的失败提炼与 α/β 删除规则（`ORIGINS` 在 `library.py:33`，`DISTILL`/`MERGE` 在 :489/:499）；合并与删除 | $0 代码 |

**验收：** 每处改动有测试（去掉改动测试就失败）；新数值只出现在新 preset 下；v5 preset 的金标不变；`.venv/bin/python -m pytest -q` 通过。

**停下来分析：** 退役是记功噪声，还是条目真有害？问题出在检索还是内容？哪条触发条件在至少 3 道题上成立？S2 有没有臂胜过 VEL？

**文档：** `docs/design/MEMORY_S1.md`（新）；OSS 计划的状态行与 :49（记功不是“平分”，是每条展示的条目都记全额 r − b(t)）；modules/04 的经验库一节。

### P5 R0 改进（$0 代码；Stirrup 待用户定）

**目标：** 让 R 明确考虑可写的结构化工具、循环补丁和改 agent 程序（参照 StarHarness：写前先分诊、日期锚定到沙箱时钟、算术和表格交给确定性工具），并修 finance 分析里找到的筛选与输入问题。

| 步 | 做什么 | 文件 |
|---|---|---|
| 1 | 新常量 `PROPOSE_SESSION_V6`：去掉“先看 `api_errors.json`”一步，明确列出可写工具（普通包工具本来就是 `read_only=False`，`default_agent.py:283`）和 agent 程序编辑。旧常量逐字节不动 | `policies.py:718-761`；协议的载体词表 `research/session.py:105-106`、`:178-180`；`PACKAGE_API`（`research/basic.py:253`） |
| 2 | 不依赖方法库的 playbook 条款 | `policies.py`（原 `METHODS_PARAGRAPH` 第 (7) 句，:757-760） |
| 3 | 新 preset 选新文字 | `bench.py:2261-2264` → `default_research_set`（`policies.py:774-797`） |
| 4 | FINANCE §5.2 的筛选改动，作为 preset 数值（要用户批准，§3 第 11 项）：hook 只在它起作用的题上记修复；按修复率排序（`session.py:884-894` 的 `pick_best` 现在按净翻转排）；dev 按断言类型给余量计数（:811）；重提被拒的同一类要有新证据（`differs_from`，:182-184）；标出共模退步 | `research/session.py` |
| 5 | FINANCE §5.5 的 R 输入检查（同上，要批准）：第一次写之前先有一次失败读数；通过的读数轮换；字面量检查在 `re.I` 下不分大小写；台账记下哪条规则起了作用 | `research/session.py`、`research/drafts.py` |
| 5b | 只在用户批准 Stirrup 时做：`agent/stirrup_agent.py`（150～250 行）；新 preset 和镜像（`bench.py:2016-2019`）；worker 内存从 K 读（`agent/worker.py:192`、`:255`）；挡住 Stirrup 的 skill 加载器读 `skills/library/`；S3：W0′-Stirrup 对 W0，dev 25 × 3（名义 $33～65），按非劣规则读（OSS §7）。v2 第一期仍用现在的 W0 | `ouroloop/agent/`、`docker/` |

**验收**
```
.venv/bin/python -m pytest -q                       # v3 的 K、R0 金标 ref 不变
# 在 v4-finance 台账上离线重放新的 pick_best：选 policy_routes，不选 send_scope（FINANCE_V04_ANALYSIS:523）
# 新测试：新 preset 的 default_research_set 给出新的 R0 ref，v3、v5 的 R0 ref 不变
```

**停下来分析**（在 P6 第一个域的第 1 轮之后读，约 $40）：有没有至少一个工具或 agent 候选到达筛选？没有的话，停下，先改 R 的文字再付第 2 轮。

**文档：** modules/04（R 可改的载体、可写工具）；DESIGN §7；OSS 计划状态；Stirrup 的决定写进 JOURNAL。

### P6 新一轮按域活动与封存 final（付费，每一步都要用户批准）

**目标：** 在新 preset 下逐域跑单领域活动，有确认的安装就封存 final，得到最终结果。

| 步 | 做什么 | 文件 / 命令 |
|---|---|---|
| 1 | 报告的 `spent` 布尔值换成按域的已读次数（finance 1、support 0、sales 0、hr 0、marketing 3），每份报告都显示；operations 不进任何活动 | `ouroloop/report.py:44-80` |
| 2 | 新 preset（暂称 v6，名字待定）：v3 作底，加 VEL 开关（默认值待定，§3 第 9 项）、P5 的新 R0 与筛选改动；去掉 `cost_beta0/1`（`bench.py:1886-1887`）；`control_readings` 按噪声规则定（SLIMMING:231、FINANCE:571）；preset 的 `max_effort` 改成 `"medium"`（现在是 `"high"`，`bench.py:1863`） | `ouroloop/bench.py` |
| 3 | 配置 `examples/automationbench/claude-v6-{finance,support,sales,hr,marketing}.toml`，用已冻结的划分（finance `4fd91eaf9fb8daf5`、support `3da11232f5d5ff38`，其余取 v3 工作区）；不写 `spent` | `examples/automationbench/` |
| 4 | 第一个新 final 之前，预注册五域合并的 C1（§3 第 8 项） | 写进配置注释与 JOURNAL |
| 5 | 每个域（命令前缀 `.venv/bin/python -m ouroloop`）：`freeze --config <cfg>` → 提交 → `bench --config <cfg> --phase cycle --until baseline` → 停 → `--phase library`（VEL 开时）→ 停 → `--phase cycle --until segment-0/round-K`（3 轮，patience 2；K = 0、1、2，轮次从 0 数，每轮后停）→ 有确认的安装时：填 `[freeze.final]`，`freeze`，提交计划哈希，`--phase evaluate`（该域全部 test 题 × 3，并列 W0 的 pass@k），`--phase report` | — |
| 6 | 可选的 C4 R1 臂（§3 第 7 项）：R1 = R0 加上一个域活动的 playbook，同一划分、根与日程；W0、W\*\_R0、W\*\_R1 联合 final（每个 final 臂约 +$35，加 R1 活动本身）。比较脚本是新代码，要在读数旁边并列打印缺了几道题、几次尝试 | `examples/automationbench/` 下新脚本 |
| 7 | 每个 final 之后：写 RESULTS 和 README，合入 main（本地） | — |

**花费（名义，订阅；真实 $0）：** 每个域约为基线 $56 + library $46～98 + 3 轮 $120 + final $72 ≈ $300～350；五个域 ≈ $1.5～1.75k。每个域单独开预算根，开之前要用户批准。

**验收：** `freeze` 打印的划分哈希与上面一致；`--phase smoke --checks-only` 通过；过程检查成立（字面量误拒为 0；每个候选都有准入认可的轨迹读取；检索的留一审计为 0）；`git log` 里计划哈希的提交早于第一次 test 尝试；`--phase report` 跑两次结果相同；报告里有已读次数。

**停下来分析：** 基线之后：余量多少、噪声多大？每轮之后：过程检查成立吗？3 轮没有安装：这个域停，照实报告，不续跑同一条管线。final 之后：每个域和合并的 C1 是多少？test 有没有被用来选过任何东西？

**文档：** 每一步一条 JOURNAL；每个 final 之后更新 RESULTS、README、ROADMAP。

### P7 v2 训练链第一期：轨道 A（V2 §5）

**目标：** 链路端到端跑通，只按链路收据验收，不设效果指标。代码在 P1 之后的 `ouroloop/train/` 里。

| 里程碑 | 做什么 | 花费 | 验收 |
|---|---|---|---|
| v2.0 | 离线：在 v4-finance 的 train 记录和 `tests/fake_upstream` 上，用脚本化的 provider 加 `signature`、`mock_sft` 走完整条链（新写或修改约 2.2～3.2k 行） | $0 | V2 §5 的命令（`python -m ouroloop.train plan/project/label/build/check/fit/threshold/eval/smoke --offline`）全部能跑；V2 §5 的 14 条测试断言；`.venv/bin/python -m pytest -q` 通过 |
| v2.1 + X1 | 第一次付费之前，先在 RunPod 上用一张 H100 跑约 20 分钟冒烟（约 $1～2）。决策链：Claude 作 judge 给写打标 → `tde` → 写门先 shadow 后 act → 导出。actor 链：`tinker_lora` 训出 S_F → 合并权重 → RunPod vLLM 接回 → 在 W0 下经 runner shell 跑。X1：在 train 的 val+cal 族上比 S0 与 S_F，同时离线读写门上限。冻结 X1 计划之前先定学生思考档位（§3 第 3 项） | 名义 ≤ $35，真实 ≤ $80 | `smoke --real` 收据的每个环节都有 ref；全程不碰 dev/test；X1 按 V2 §4.2 的读法写进收据 |
| v2.2 | 网关的 role → provider 映射；只作用于 act 调用的审阅钩子；ReviewRecord；在 finance train 上审阅 10～20 次学生尝试，建 `reviewed_student` 集并训一次 | 名义 ≤ $20，真实 ≤ $10 | V2 §5 v2.2 的测试；收据齐全，轨道 A 结束 |

**停下来分析：** K0（离线套件或投影复现不过，就不进任何付费步骤）；X1 的 K1、K1b 与写门上限。这些读数只供用户决定开不开轨道 B，不是第一期的验收条件。

**文档：** V2 §7 的同步清单里剩下的项；modules/02、09；TECHNOLOGY；JOURNAL。

## 3 待用户决定

> **2026-10-08 用户：“按默认，开始P1”。** 下表 14 项全部按“建议的默认”定下（第 14 项版本号与 preset 名没有默认，仍暂称 v6，到 P6 再定）。P0 已完成（`efe2047`）；按第 10 项把 P0 合入 main（本地）；P1 开始。

| # | 事项 | 影响 | 建议的默认 | 来源 |
|---|---|---|---|---|
| 1 | 方法库删不删（否决与否） | P3 | 按事先规则删 | SLIMMING §3；V0_5_PLAN §3 |
| 2 | Stirrup 作 agent 底座 | P5b、P6 | OSS 计划认为可行，但底座本身预计约 0 增益，价值在于 R 能改的面更大；v2 第一期仍用现在的 W0 | OSS §4、§9 第 3 项 |
| 3 | 学生的思考档位（关，还是 low/medium） | P7 X1 | 现行默认“关”（V2 §3.7）。同一比较里各臂用同一档位，不做“开、关各跑一次取较好” | V2 §7 第 9 项 |
| 4 | 各付费阶段的预算 | P4-S2、P5b、P6、P7 | P4-S2 名义 $50～180；P5b-S3 名义 $33～65；P6 每个域名义 $300～350，C4 R1 另计；P7 v2.1 名义 ≤ $35、真实 ≤ $80，v2.2 名义 ≤ $20、真实 ≤ $10；轨道 B 名义 $0.8～1.3k（以后再定） | OSS §5；V2 §4.6；JOURNAL 2026-10-08 |
| 5 | S2、S3 跑不跑，还是把改动直接放进 v6、只在 final 里量 | P4、P5b | S2 跑（它就是开源优先的消融，输入是已付过费的轨迹）；S3 跟着 Stirrup 的决定走 | OSS §5；叫停时的原话 |
| 6 | 域的顺序，以及每个域的预算根 | P6 | finance → support → sales → hr → marketing。finance 噪声最小（25 道 dev 里只有 1 道不稳），也有 v0.4 的 final 作参照；marketing 的 test 已读 3 次，放最后 | ROADMAP §3 v0.4 第 2a 步 |
| 7 | C4 的 R1 臂跑不跑、放哪个域 | P6 | 第一个域结束后，在第二个域跑 | SLIMMING §0 (a) |
| 8 | 跨域合并 C1 的预注册 | P6 第一个 final 之前 | 按 FINANCE §5.1 的提议：单位是题，纳入所有新的单领域 final，合并规则在看到第一个新 final 之前写死 | FINANCE_V04_ANALYSIS §5.1 |
| 9 | VEL 在 v6 preset 里默认开不开 | P6 | 开（过程读数成立，也符合叫停时“能改进的都改进掉”）；开关保留 | SLIMMING:361 写的是“开关，不进默认” |
| 10 | 版本记账：v0.4.0 合入 main 用哪个提交；打不打 v0.5.0 | P0 之后 | 把 P0 的提交合入 main（本地，RESULTS 与 README 在那里写着 v0.4，v0.5 代码标为未发布）；不打 v0.5.0（没有评测过的版本），support 工作区在 `archive-2026-10-08` 上重渲染 | ROADMAP v0.4 第 5 步；SLIMMING §3 第 0 步 |
| 11 | FINANCE §5.2 的筛选改动与 §5.5 的 R 输入检查 | P5 第 4、5 步 | 采纳 | FINANCE_V04_ANALYSIS §5.2、§5.5 |
| 12 | support 的 W\* 怎么算：v0.5 臂只过了 dev gate 的 VEL 快照 `9f07d95032b4`，没有 final。拿它当 v2 老师的 K，还是当作 support 没有 W\*；support 的 train 记录现在进不进 X1 | P7、轨道 B | 当作没有 W\*；X1 照 V2 §4.2 用 finance、sales、hr、marketing，support 暂不加入 | V2 §2.6 第 6 项、§4.2、§7 第 4 项 |
| 13 | Claude Code 原生臂什么时候重新评估（原来的触发点“support 两份 final 之后”已经没有了） | — | P6 第一个域的 final 之后 | OSS §4 |
| 14 | 下一版的版本号与 preset 名（本文暂称 v6） | P6 | — | — |

## 4 文档地图

| 问题 | 以哪份为准 |
|---|---|
| 现在做什么、下一步、谁来定 | 本文 |
| 发生过什么：每次改动、量到的数、花费、commit | [JOURNAL](JOURNAL.md)（倒序） |
| 对外的结果 | [RESULTS](RESULTS.md)；README 的结果段 |
| 设计目标、主张 C1～C5、不变量 | [DESIGN](DESIGN.md) |
| 历次活动的顺序、费用与预注册读法 | [ROADMAP](ROADMAP.md) §3 |
| 模块规则、契约与集成用例 | [modules/](modules/README.md) 00～09 |
| 瘦身逐模块的判定与分批 | [design/SLIMMING_INVENTORY](design/SLIMMING_INVENTORY.md) |
| v2 训练链 | [design/V2_TRAINING_PIPELINE](design/V2_TRAINING_PIPELINE.md) |
| memory 与 agent 的开源底座、S1～S5、二开的触发条件 | [design/OSS_COMPONENTS_PLAN](design/OSS_COMPONENTS_PLAN.md) |
| v0.4、v0.5 的规则（预注册记录，不再改正文） | [design/V0_4_PLAN](design/V0_4_PLAN.md)、[design/V0_5_PLAN](design/V0_5_PLAN.md) |
| 经验库的设计输入 | [design/V0_5_EVOLVING_SEARCH](design/V0_5_EVOLVING_SEARCH.md) |
| 技术选型；训练主机 | [TECHNOLOGY](TECHNOLOGY.md)；[TRAINING_HOSTS](TRAINING_HOSTS.md) |
| 外部 RSI 框架调研 | [SURVEY_RSI_FRAMEWORKS](SURVEY_RSI_FRAMEWORKS.md)、history/survey-2026-10/ |
| 事后分析与演练记录 | history/FINANCE_V04_ANALYSIS_2026-10-07、history/DIAGNOSIS_FIVE_DOMAIN_2026-10-06、[REHEARSAL](REHEARSAL.md) |
| 怎么跑基准 | [examples/automationbench/README](../examples/automationbench/README.md) |
| 运行记录 | `examples/automationbench/workspace-*/`（本机，git 忽略） |
| 已删的文件 | tag `archive-2026-10-08`；更早的版本在 `v0.1.0`～`v0.4.0` |

文档之间冲突时：规则以来源文档为准，顺序与状态以本文为准，发生过什么以 JOURNAL 为准。发现冲突先改文档，再继续执行。

**新 agent 的阅读顺序：** 根目录 `CLAUDE.md` → 本文 §0～§1 → §2 里当前阶段那一段 → JOURNAL 最上面三条 → 该阶段引用的来源文档 → 要改的代码对应的 modules 文档。

## 5 怎么更新本计划

| 什么时候 | 做什么 |
|---|---|
| 一个阶段（或阶段内的一个停下点）结束 | ① JOURNAL 顶部加一条：做了什么、量到的数、花费、commit；② 本文 §0 和 §2.0 的状态改掉，做完的阶段压成一行，链到 JOURNAL；③ 本地提交 |
| 用户做了新决定 | 原话写进 JOURNAL；改对应的来源文档；把这一项从本文 §3 移到 §0.2；改受影响的阶段 |
| 阶段的顺序或范围变了 | 在 JOURNAL 写明原因，再改 §2 |
| 本文变长 | 规则细节和论证只放来源文档，这里只放引用；本文超过约 400 行时，删掉已完成阶段的细节（JOURNAL 里都有） |
