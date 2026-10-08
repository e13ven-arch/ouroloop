# 瘦身清单（2026-10-07）

> **第一批已完成（2026-10-08，分支 `slim/1`，[PLAN](../PLAN.md) P1，JOURNAL 2026-10-08 瘦身第一批一条）。** §2.1、§2.3、§2.6 里标 1 的行都已做，各行就地标了步号；与“做法”列不同的地方列在 §2.1 末。实测：框架 `ouroloop/**/*.py` 35,253 → 30,126 行，其中 `[train]` 3,042、`[train-jobs]` 1,697 行（另有 sh 与 Dockerfile）；测试（不含 fake_upstream）25,554 → 23,265 行（原写 23,237，少算了文档提交加的一个测试）。评审之后（`3da9a0c`）框架 30,113 行（`[train]` 3,025），测试 23,529 行；`evals/`（3,091 行）、DEMO、RESEARCH_AGENT、`history/V0_5_IMPLEMENTATION.md` 与 `docs/assets/`（画架构动图的脚本与两张动图）已删。
>
> **用户决定（2026-10-08）：不写金标身份测试。** 用户问：“有必要吗？在我们这个场景”。不必要：现有活动都不会在 HEAD 上续跑（support 对照已停，下一轮活动在新 preset 与新 K 下重开），旧工作区在 tag `v0.4.0`、`archive-2026-10-08` 上重渲染或续跑。所以与现有工作区的哈希身份不再要求，§1“身份不动”、§3 第 0 步的金标测试、各批“必须守住的身份”与 §4 的身份漂移一行都按此放宽（就地加注）。只为保旧哈希而留的字段（RunManifest 的 decisions、model_ref、policy、`input_schema_refs=(stop_view,)`，`_host_contract` 的字面量，只为旧 ref 留着的 REGISTRY `worker.stop` 与内置点，K 的 `capabilities.training`），在能让死代码一起删时可以删；纯外观的字段删除不花工夫。必须守住的三条：(1) 读兼容：HEAD 的读取器（Results、grading、transcripts、session 行、`library_stream.json`、当作普通 JSON 的冻结对象）照样读得了 workspace-claude-v4-finance、-v3、-v5-support、-v4-support 的 train/dev 记录（v2 的数据管线与 PLAN P4-S1 要读它们），由 `tests/test_record_compat.py` 守住；(2) 行为：在 `tests/fake_upstream` 上的一次尝试，模型调用序列、工具调用、结果与评分都与改动前相同（id、哈希、时间戳归一化后），由 `tests/test_attempt_behaviour.py` 守住；(3) 新活动里的资金安全：预算根与份额、预留、按 attempt id 续跑、测量复用、五项硬限额、`_verify_bound`、启动检查的行为与测试都不变。会破坏这三条的改动不做，并记下原因。

> **现状（2026-10-08，support 对照停止后）：** 现行。用户已停止 support 两臂对照（JOURNAL 2026-10-08）：两份 final 没跑、H1 未测，页首“时机”与 §3、§4 里“等两份 final 评完、H1 算完”的条件作废，三批现在就按顺序在 worktree 里做、逐批合入。第三批按预注册的单库过程读数套用（H1 没测，不是“不确定”）：经验库两项成立（D = 3.0 ≥ +2，P = 0.89 ≥ 0.8；记功秩相关 0.2），保留为 preset 开关、不进默认（留 `library.py`、`fixed.py`、`library_track`、runner 的检索、报告的库证据）；方法库两项没跑（3c、3d 未跑），按规则删，这是提议，用户可否决（删 `methods.py`、`method_kit/`、`learn` 与 `_refit_*`、方法分支、`test_methods` 与 `test_v05_phases` 的 learn 部分）；playbook 例外照旧。“在 tag v0.5.0 上复算 H1”改为在 tag `archive-2026-10-08` 上重渲染两个 support 工作区（两臂跑的就是这份代码）；support 的对照噪声已量到（v0.4 臂 dev 8/9/9，v0.5 臂 10/9/8），供 §2 `control_readings` 一行用。文中引其他文档的 file:line，凡被 2026-10-08 整理挪动的，已改成整理后的行号；引代码的行号以 `80254a3`（tag `archive-2026-10-08`）为准，那之后没改过 `.py`。
>
> 用户（2026-10-07）："先整理瘦身清单，对照臂先不加。" 本文只给清单与分批计划，不改代码，不加对照臂，也不加新机制。
>
> **依据。** HEAD `e414f7c` 的静态阅读（import 与调用链、grep、ast）；`docs/JOURNAL.md`、`RESULTS.md`、`ROADMAP.md`、`history/FINANCE_V04_ANALYSIS_2026-10-07.md`；另外只读查看了工作区记录（v4-finance 的 ledger、events、report.json，以及各工作区的冻结计划）。没跑测试，没调模型，没读 `.env`。
>
> **时机。** support 两臂正在主检出里付费运行，主检出里的代码就是两臂的代码。所以每一批都等两份 final 评完、H1 算完之后才合入；在那之前只能在隔离的 worktree 里准备。
>
> **口径。** 行数用 `wc -l` 统计，含注释与文档字符串。"可省"是估计值，误差约 ±10%。"当前路径"指以下组合：
> - CLI 阶段：smoke / freeze / cycle / improve / evaluate / report / export / library / learn；
> - preset：`automationbench_harness_v3`（v0.4 的配置用它，数字已是 v0.4 的；仓库里没有单独的 v4 preset）或 `automationbench_harness_v5`；
> - 提供方：`claude_code`。
>
> **修订（2026-10-07，同日）。** 用户看过第一版后说："模型训练和蒸馏最终是要加入进去的……agent 在处理业务问题的时候，有大量的判断和决策环节……所以这个训练链路也是要有的。" 据此改写：决策/训练栈不再整体删除，只删确已过时的部分（依附 v1 Agent 循环的、冠军指针一族、无对端的协议、旧入口），其余收进可选子包并移出热路径；C4 保留能力，换实现。新增 §0.1、§0.2，§0、§2、§3、§4、§5 相应改动。修订依据为 HEAD `198799a` 的静态阅读与 ast 跨度，另只读查看了 `~/workspace/jev`（HEAD `3d4dbbe`）。
>
> **修订（2026-10-08）。** 用户对 `V2_TRAINING_PIPELINE.md` §7 说"按默认"，八项全部按默认值确认，该文定稿。本文据此改了三件事：两个 extra 按其第 7 项改名为 `ouroloop[train]`（子包 `ouroloop/train/`）和 `ouroloop[train-jobs]`，入口 `python -m ouroloop.train`，取代原名 `ouroloop[decision]`/`ouroloop[decision-jobs]`，代码只搬一次；§0.1 第 5 点、§0.2 第 10、12 项标为已定，各处"归属待定"的说法随之改掉；§0.2 加一行指向该文。行数与判定不变。同日复核后另补：§0 (a) 与 C4 联合 final 的 test 池注、§0.2 末条的 C3 注、§3 第一批的改注释一步、各处 file:line 引用与 (a)(b) 已确认的标注。

## 0. 结论

| | 现在 | 删（第一、二批） | 移入可选 extra（第一批） | 待定（第三批：v0.5 两库） | 瘦身后主包 |
|---|---|---|---|---|---|
| 框架 `ouroloop/**/*.py` | **35,253** | **≈ 8,800（25%）** | ≈ 4,400：`[train]` ≈ 2,400，`[train-jobs]` ≈ 2,000 | ≈ 4,300 | **≈ 17,800**（两库删）～ **22,100**（两库留） |
| 其中 `bench.py` | 5,400 | ≈ 1,700，另有版本史注释 ≈ 400 | — | ≈ 640 | ≈ 2,650～3,300 |
| 测试 `tests/*.py` | 26,209（另有 fake_upstream 437） | ≈ 7,400 | ≈ 3,100：子包 ≈ 1,300，jobs 1,766 | ≈ 1,900 | ≈ 13,800～15,700 |
| 示例与评测数据（已跟踪） | — | ≈ 7,300（ceiling 跑法与包、rehearsal 世界与存档、`evals/`） | 主机脚本 207 行、训练主机 Dockerfile 66 行 | — | — |
| 文档 `docs/**/*.md` | 11,171 | 删 ≈ 30（DEMO），移入 history ≈ 1,400 | TRAINING_HOSTS（233）随 jobs | 移入 history 700～2,300 | — |

**按建议分（框架行数）：**
- **删**：约 8,800 行。
  - 约 5,100 行是决策栈里确已过时的部分：依附 v1 Agent 循环的、冠军指针一族（已被 Releases 和 task_gate 取代）、无对端的协议（`serve`、`backends/http`）、旧入口（demo、旧 CLI、旧决策研究）。逐模块见 §0.1 E。
  - 约 440 行是第一批的其他死代码：ManualProvider、frontier 臂、task_gate 经济模式、无调用者的函数、v0.3 兼容。
  - 约 3,300 行是第二批：活动内研究位的编排（≈ 1,500）、多领域合并、采集批、段延长、旧轮次路径、api_errors，以及若干简化。
- **移入可选 extra**：约 4,400 行，不删，移出热路径（§0.1 A–C）。`ouroloop[train]`（子包 `ouroloop/train/`）装类型化决策契约、标签与数据集、`train_artifact`、后端、存储与 serving、StopGuard；`ouroloop[train-jobs]` 装 GPU 作业基础设施；入口 `python -m ouroloop.train`。命名按 V2_TRAINING_PIPELINE §7 第 7 项（2026-10-08），v2 的训练链代码进同一个子包。
- **原位保留（懒用）**：约 700 行，留在核心模块里，热路径不执行（§0.1 D）。
- **降为可选**：代码几乎不删。成本项与 `control_readings` 改在 preset 和配置里调；两个 API 提供方（208 行）本来就是懒加载的可选件；参考方法（565 行）移出包，这部分算在第三批。
- **待定**：约 4,300 行，即 v0.5 的方法库与经验库。
- **保留**：主包约 17,800～22,100 行（不含 extra）。

**对结果：没有影响。** 要删和要移走的代码，在 v0.4 finance（当前路径上唯一跑过 final 的活动）和 support 两臂里，要么没执行过，要么执行了但输出恒为空或恒为常量。以下逐条核过：
- v3、v4、v5 的全部配置都写 `research_slots = 0`。`cycle()` 在 `_cycle_slot` 之前就 break（bench.py:4483）。finance 的 report.json 里 C2、C3、C4 都是 `not_run`，其中 C4 写的是 "deferred"。
- v4-finance 的 ledger 有 477 行，全是 `session`，没有一行决策或结果。`sessions/` 下 477 个 release pin 全是 `{model: null, champion: null}`。
- 在 workspace-claude、-full、-v3、-v4-finance 的 events 里，`collection_booked` 一次都没出现。
- 没有任何配置有 `[trainer]` 或 `[frontier]`。当前配置里写着 `default_backend = "mock"  # the first version runs no decision model`。
- 11 个工作区共 54 份冻结计划，经济模式、protected 需求、成本比上限一次都没用过。
- 代码里没有 bandit、档案（archive）或 agent 内 RL 的实现，这些只存在于设计稿。唯一的"合并"是经验库的条目合并，属于 v0.5，待定。
- 移入 extra 的代码在当前路径上也不执行，只是被 import：bench.py:503-511 顶层导入 feedback、build、points；gateway.py:36 导入 points；绑定 W0 的 rule 类 `worker.stop` 时会构造 `business_stop_point()`（policies.py:594-597）。第一批先解耦，再用边界测试守住（§3）。

**对可信度：不会降低，有一处会提升。** 现在大多数续跑、暂停、预算测试跑在非生产路径上：
- `tests/rsi.py` 起的 K 没有 `harness.max_effort`，所以尝试走的是 v1 Agent 循环。
- `test_cycle` 的 `fixture_preset` 复制的是 `marketing_harness_v1`，所以 R 走的是旧的总结调用轮。

真正走生产路径（runner shell 加编码会话）的，只有约 58 个 bench 级测试和 21 个 CodingSession 单测。瘦身的前提是把夹具迁到 v3 preset 和 `SessionScript` 上；迁完之后，资金安全测试测的就是真正在跑的路径。

**对资金安全：全部保留。** 预算根与份额、预留、按 attempt id 续跑、测量复用、五项硬限额、`_verify_bound` 都留下。final 的三处预算检查合并成一处，取最严的规则。

**需要用户拍板的两件事（2026-10-07 按用户反馈改写）。** 两件都只改定位，不动任何已报告的数字。

> **用户已确认（2026-10-07）：** 同意下面的 (a)、(b)，以及冠军指针一族（§0.1 E）按过时删、v2 重写。训练归属已定（同日）：蒸馏和 Jev 类决策模型的训练是 ouroloop 框架的一部分，所以 `[train-jobs]` 保留，§0.2 第 12 项按“在 ouroloop 训练”走（先修 C 里的漂移，补 Qwen decider 插件）；数据管线、试验设计与训练管线另写设计文档 `V2_TRAINING_PIPELINE.md`，老师标签路径（第 10 项）在那里定。执行时机不变：support 两份 final 评完、H1 算完之后。
- **(a) C4（R0/R1 递归）：保留能力，换实现。**
  - 删活动内研究位的编排（≈ 1,500 行）。它在单领域活动里结构上开不了：要求内外两个 gate 各至少 `min_tasks` 题（bench.py:4636-4650），单领域 dev 只有 25 题，一开位就是 `no_slot_pool`；v3/v4/v5 的配置全写 `research_slots = 0`。唯一一次运行（v0.2）两臂封存了同一个 W\*，外层比较没跑。
  - 抽出 `research/recursion.py`（≈ 200 行）：`judge_research`（campaign.py:382-485）、`carries/unused_changes`（:322-348）、`CampaignPair` 的不重叠检查、`discovery` 标签。不抽 `basic.propose_research`：它只服务 v0.1/v0.2 的 note-contract R（experiment.py:396-402），会话 R 没有这条路径。
  - C4 改成跨活动比较。R0 臂就是该领域普通的 C1 活动；R1 臂 = R0 加上前一个活动产出、经台账核对的 playbook，所有字节由系统写出；同一划分、根和日程；W0、W\*\_R0、W\*\_R1 联合 final。边际成本是每领域多一个 R1 活动和一个 final 臂。新接线约 100–200 行（从冻结的 R1 起活动、跨工作区载入发布做联合 final、C4 脚本），随下一个新领域活动做，不算瘦身。（2026-10-08：按 V2_TRAINING_PIPELINE §7 第 2 项，本基准已无可供 v1 联合 final 的新 test：marketing 已用尽，sales、hr 留给 v2，operations 留作 LODO。跨活动 C4 要么在 finance/support 按有条件复用规则跑并披露读取次数（会占用 C5 的 test 池），要么记 not_run；由用户定。）（同日用户改为全部域可跑 final：联合 final 可以放在五个域里任何一个，按有条件复用规则披露读取次数；operations 仍整域留出。）
  - v4 对 v5 不是 R0 对 R1：两臂的 K、R0 文本、固定阶段都不同，而且都是人写的。H1 仍叫"框架 v0.5 对 v0.4"。
  - 功效照实写：25 题 × 3 次的 final 在真实 +8 点时功效约 30%；预期的 R 层效应只有 +0.75～+1.5 题/25，预注册"不确定"为最可能的结论。
- **(b) Jev、TDE 与本地决策 trainer：不撤回能力，改写承诺。**
  - 收进可选子包 `ouroloop[train]` 和 `ouroloop[train-jobs]`，从热路径解耦，带着自己的测试；只删确已过时的部分（§0.1 E），这部分在 tag v0.4.0 上仍可读。
  - 撤回的只有"v1 Agent 循环的决策点与冠军发布继续可用"这一半。
  - 从 runner shell 到决策点和训练数据的桥现在不存在，列为 v2 前置（§0.2），本次不做。
  - 第一版的后备方案"整体移进 `ouroloop/legacy/`"作废：那样会把过时部分和可复用部分一起留下，热路径上的耦合也还在。

## 0.1 用户反馈（2026-10-07）：训练链与决策模型要保留

**直接回答：这条能力要有，用户是对的；但现有代码只有一部分是它的地基。** 两件事要分开看。

| 问题 | 结论 | 依据 |
|---|---|---|
| 从 RSI 完整性看，要不要有权重这一层（训练、蒸馏、决策模型） | 要 | 改程序（W）和改权重（θ）是同一条路线的前后两段：v1 对标的 Harness-Zero 先演化 h\*，再蒸馏进学生（基线 23.3% → 挂 harness 41.7% → 蒸馏后去掉 harness 44.3%）。本基准上判断环节是真实的：v0.4 finance 的 W\* 在 test 上失败的 36 条断言里有 25 条是"不得做"（FINANCE_V04_ANALYSIS:294）。M2 已写明在旧代码上扩展：`train_artifact` 加 role 分派，复用 `test_evidence_training`、`test_train_config`、`test_jobs`（02_EVIDENCE_TRAINING.md:51、:134）；controller 训练用"现有 trainer/TDE 作业与 serving"（TECHNOLOGY.md:15）。第一版清单要整删的正是这些 |
| 现有代码是不是地基 | 一部分是：约 3,100 行收进子包或原位保留，约 2,000 行作业设施保留、归属后定为 ouroloop（§0.2 第 12 项），约 5,100 行确已过时 | 下表 A–E |

有五点要照实说：
1. **没有证据。** 这套栈在 AutomationBench 上没产出过 held-out 结果，每个 test 数字都来自包演化（v0.1 +13.0、v0.2 +4.3、v0.4 finance +8.0）。唯一测过的训练决策模型是贪吃蛇：冠军与规则持平，模型推翻规则的那些步只有 15–26% 是对的（JOURNAL 2026-09-27“重切：训练线为主线”与 2026-09-26“以默认为基准的价值”两条）。保留是对 v2 计划下注。
2. **它不训练、也不服务蒸馏出的语言模型。** 训练路径假设模型是分类器：`Backend.decide` 输出标签概率，`_probe_artifact` 问写死的 stop 问题（pipeline.py:441）。actor 蒸馏按计划走 Tinker（TECHNOLOGY.md:14）。"蒸馏垂域模型"的代码基本要新写（§0.2 第 11 项）。
3. **它和现在的 agent 路径是断开的。** runner shell 不问任何决策点（agent/runner.py:115-116）；回推发生在包内（default_agent.py:224-235），宿主只看到 `run` 返回一次；导出的 app 没有 serving（app.py:27），model 策略在那里会 BindError（policies.py:605-606）。
4. **用户举的两个决策点都还没设计。** 唯一注册的决策 target 是 `worker.stop`（policies.py:66）。"要不要发邮件"有环境真值，即 `gmail_email_not_sent_to`、`*_not_exists` 这类"不得做"断言；"要不要联系人工"在本基准上没有真值：工具只有 `api_search`、`api_fetch`、`base64_encode`（automationbench.py:19-21），没有交接工具，也没有奖励升级的断言。
5. **标签契约排除了 LLM 老师。** `FeedbackRecord.source` 只收 env/human（contracts.py:682），`ledger.EVAL_ONLY` 含 `frontier_llm`、`jev`（ledger.py:22）。外部常见做法（子报告引用的 Intercom 转人工模型）是用 LLM 老师标注；要走这条路，得在 02 里给决策加一条老师标签路径，参照 actor 侧的 PASS/REPLACE（§0.2 第 10 项）。**已定（2026-10-08，V2_TRAINING_PIPELINE §2.5，定稿见 §7）：** 加老师标签路径；`source` 不再作闸门，只作血缘（`env:*`、`human:*`、`model:<identity_ref>`），拟合默认收 env 与 model，阈值只收 env，评测收 env 与 human；评分信息永不进 actor、teacher、judge 的输入，也不进 SFT 输入。

**逐模块判定。** 只算决策/训练栈，也就是 §2.1 第一批里相关的各行。行数是 `wc -l` 或 ast 跨度，误差约 ±10%。

**A. 收进 `ouroloop[train]`，原样或小改（≈ 2,100）**

| 模块 | 行 | 改动 | 证据 |
|---|---|---|---|
| `types.py`、`decision.py`、`policy.py` | 416 | policy 去掉 `HARNESS`/`may_act` 对内置点的锁（policy.py:24-29） | noul/choice/score 与 Jev/TDE 同构，含 nll（brier 没有调用者，2026-10-08 评审后删了，policy 的 `cp_lower` 同）；按 session 的 Clopper-Pearson LTT 阈值 `fit_threshold`（policy.py:107）。三个文件只互相依赖 |
| `evolve/feedback.py` | 161 | 标签集和 `kind` 改为取自 builder（≈ 15 行） | 只认 env/human，unknown 不等于 no，冲突要标出。现在标签写死 `("yes","no")`（:21、:91），`kind` 写死 `completion_snapshot`（:146），写门类 builder 的标签会被静默丢掉 |
| `evolve/build.py` 任务半边，连同 `candidates`、`to_example` | ≈ 215 | 删 :17 的 replay 导入（任务半边不用它） | `build_task_dataset`（:164）在 :212 调 `to_example`（:34-52），后者用 `candidates`（:23-31），三段要一起走。按族分 fit/val/cal、每题上限、血缘检查（:272-289）；导出格式与 TDE 的 `DecisionExample` 一致 |
| `train_artifact`、`TrainingBuild`、`_probe_artifact`、`full_recipe`（pipeline.py:79-88、:340-454），加 train.py 的契约 `TrainJob`、`TrainResult`、`Trainer`、`recipe_defaults`、`path_keys`（train.py:44-77） | ≈ 175 | 探针不再写死 stop 问题（pipeline.py:441），按 point 取 | 先预留预算再提交；续跑时用 `attach` 查询，不重复提交；加载探针核对模型身份和标签 |
| `evolve/checks.py` | 95 | 加 ≈ 10 行 `DatasetBuild` 适配 | 纯函数，无内部 import。截断、答案位置偏斜、13-gram 重叠可以直接用；`check(ds)` 读的是旧 `ds.splits`（:42、:75-87）。`min_train=1000` 保留为"数据不够"的真实信号（每域只有 50 道 train 题）；基于 test 的最小可测差在任务剖面里只作提示，判定归配对 task gate |
| `calibrate.fit_temperature` | 11 | — | calibrate.py 其余部分绑着 replay 和 CalibrationStore（:12、:63、:92），删 |
| `backends/{jev,llm,mock}` | 168 | jev 在 key 为空时放行（jev.py:40-42） | `jev.py` 改 url 就能接 tde-serve 和 decider serve（jev `tde/serve.py` 讲 `/v1/systemone`）。`llm.py` 的答案是独热的（llm.py:49-50），置信度恒为 1，做不了按置信度回退。`mock.KeywordBackend` 是 12 个测试文件的夹具 |
| `stores/`、`serving.py` | 318 | serving 删 url 模式（:52-54 经 `HttpBackend`），或改走 `JevBackend` | 按 ref 加载模型。HF store 的 job id 不许带点（stores/__init__.py:36、hf.py:61），读不了 jev 的发布名（如 `tde-general-v0.2`），v2 要加 hub ref |
| `trainers/__init__.py`，`trainers/tde.py` 的 serving 半边 | ≈ 85 | — | 插件契约与数据格式无关 |
| `points` 的 `business_stop_view/point`、`_keep` | ≈ 60 | DENY 移到 `agent/` | 业务视图最长 12,000 字符（points/__init__.py:144） |
| runtime 的通用半边：`record`（shadow）、`propose`、`outcome`、`_default`，以及去掉 SpecStore 与 `may_act` 的 `register` | ≈ 150 | 拆出 | `record` 走 `asking=False`，不碰 pin（runtime.py:242-247）。任务路径的 `propose` 写死 `probs=None, explored=False, propensity=1.0`（:356-361），从不调 `policy.route`（§0.2 第 4 项） |
| `stop.py`（StopGuard） | 108 | 无策略的分支改用 `record` | 不 import 任何 v1 模块（stop.py:1-2："Any harness can use it"）。负责 checker 否决、回推上限、override 行（:76-93）；有绑定策略时走 `rt.propose`。stop controller 生效时，不管挂在哪里都要它 |
| `agent/loop.py` 的 `_stop_ctx`、`_trace`（:294-345） | ≈ 50 | 改写成子包里的函数 | 全仓只有这里构造 `Observation` 和 `DecisionTrace`（loop.py:320、:327），是桥的起点 |
| `config.build_backend/build_trainer`；旧 CLI 的 smoke/decide | ≈ 130 | 搬成 `python -m ouroloop.train smoke --jev`（与 V2 §5 的链路 smoke 同一子命令，按参数区分） | 除自定义的 `module:Class` 以外，`build_trainer` 是 `train_artifact` 唯一的真 trainer 来源（config.py:124-138） |

**B. 收进 `ouroloop[train]`，要改造（≈ 300）**

| 模块 | 行 | 要改什么 |
|---|---|---|
| `backends/tde.py` | 118 | 依赖 TDE 的私有内部（`_question_to_example`、`collate`、`dtok`），最容易漂移。改走 tde-serve 的 `/v1/systemone`，或在 TDE 里加公开的批量 API |
| `adapters/claude_code.py`，加上 `tool_gate` 点 | ≈ 175 | 唯一已实现的人工标签来源：权限弹窗 → `source="human"`（:126-153），对应"要不要找人"。它不依附 v1 循环，但用的是 `rt.ask/record`（:107-108），要改到 `record/propose/outcome`（≈ 20 行）。`tool_gate` 视图是编码用的 workdir/command/file，业务版要重写 |

**C. 收进 `ouroloop[train-jobs]`（≈ 2,000 行 Python，另有 sh 207 行、Dockerfile 66 行）**

| 模块 | 行 | 说明 |
|---|---|---|
| `evolve/train.py`（契约以外）、`cloud.py`、`compute.py`；`ouroloop/worker.py`；`hosts/`；`trainers/tde.py` 的作业半边（:120-226）；`trainers/signature.py`；`docker/worker/Dockerfile` | ≈ 2,000 | 价格上限、截止时间、续跑不重付，这些重建很贵；M2 点名复用 `test_jobs`（02:134）。`signature.py` 是离线测试走真实作业路径用的纯标准库 trainer，test_jobs、test_cloud、test_hosts 都用它，`replay.signature` 的默认键函数要一起搬来。依赖 `[train]` 的 stores、serving、trainers |
| 已知问题（登记，不修） | — | 与 jev 的 `tde/train.py`、`ops/decider_lora_train.py`、`hf_push_run.sh` 重复。`trainers/tde.ARCHITECTURE`（:40）没有 `topology`/`max_total`：以 `topology="set"` 的父模型续训时会默默变成 jev 的默认 `seq`（jev tde/train.py:66，以 `strict=False` 加载，:266）。RECIPE 的 `max_state_tokens=1024`（:32）与 jev 默认的 448（tde/train.py:67）不一致。编码器 TDE 的 1,024 token 放不下约 3k token 的业务视图，只有 Qwen 版 decider（32k）放得下，而 ouroloop 没有它的训练插件 |

**D. 原位保留，懒用（≈ 700）**

| 位置 | 行 | 说明 |
|---|---|---|
| contracts 的 `EvidenceRef`、`DataManifest`、`Observation`、`DecisionTrace`、`FeedbackRecord`（contracts.py:261-277、:608-686） | ≈ 100 | v2 的数据契约 |
| policies 的 `StopDecider`、`_bind_stop`、`parse_inference`、`Services.serving`、`build_candidate` 的训练循环 | ≈ 150 | rule 类 `worker.stop` 绑定时不再构造 `DecisionPoint`（policies.py:594-597），否则子包仍在热路径上 |
| ledger 的 `Row`、`Ledger`、`SessionStore`（ledger.py:107-259） | ≈ 150 | `session` 行照写 |
| release 的 `Revoked`、`Releases.revoked/revoke/last_good/rollback`（release.py:103-137、:436-503） | ≈ 100 | 吊销与回滚原语 |
| `CompletionProbe`（automationbench.py:856-929） | 74 | 现在只由 v1 循环的 `Improvement.collect` 调用；补一个直接单测 |
| 训练准入：`check_label_availability`、`registered_builders`、`model_change_reasons`、`core_checks` 的训练条款（experiment.py:103-114、:233-254、:286-307） | ≈ 70 | 保留的 `build_candidate` 会 import 它（policies.py:924）。第二批简化 context 时也不删 `check_label_availability` |
| `bench._train`、`training_options`（bench.py:1509-1523、:1575-1605） | 46 | 继续被 K 的 `LATER_METHODS` 拒绝（bench.py:1825）；对 pipeline 的 import 已经是懒的（:1577） |

**E. 删：确已过时（≈ 5,100，tag v0.4.0 上可读）**

| 类别 | 内容 | 行 |
|---|---|---|
| 依附 v1 Agent 循环 | `agent/loop.py` 的其余部分；tools、harness_package、chat、task_eval 的 v1 分支；编码决策点（retry、compact、tool_gate(file)、route、stop(file)）；`agent/{prompt,suite,probe}`；`evolve/harness.py`；bench 的 `collect` 阶段与 `Improvement.collect` | ≈ 1,380 |
| 冠军指针一族（v0.1/v0.2，已被 Releases 和 task_gate 取代） | `gate.py` 455、`registry.py` 113、`replay.py` 273、`monitor.py` 163、`plan.py` 410；pipeline、build、calibrate 的旧半边；runtime 的 `ask`、阈值查找、champion、pin、`_open`（内含 `monitor.watch`）；CalibrationStore、SpecStore、PromotionConflict；Release 三元组、Accepted、`resolve_profile`、`Releases.defaults/recover`；SessionBinder 与只有它调用的 `fixed_defaults`；`Services.runtime/probe_models` | ≈ 2,400 |
| 无对端 | `serve.py` 116、`backends/http.py` 90：自定义的 `/decide` 协议，jev 里没有任何服务实现它 | ≈ 200 |
| 旧入口 | `demo.py` 与 MockProvider；旧 CLI（smoke/decide 除外）；research 里的旧决策研究（训练准入除外）；config 的 `build_runtime/build_research/build_agent_factory` | ≈ 1,100 |

monitor（上线后按实际伤害算 CP 下界并自动吊销）、replay（付费 gate 之前的免费离线筛选）、`calibrate_point`、runtime 的 `ask` 与阈值查找，这几样概念都对，但代码绑着冠军指针和旧 ledger 行的形状。登记为 v2 按 Releases 和任务剖面重写（§0.2 第 6、9 项）。

**合计**：A + B ≈ 2,400（`[train]`），C ≈ 2,000（`[train-jobs]`），D ≈ 700，E ≈ 5,100，共约 10,200。第一版记的约 10,700 行里，"运行时枢纽"一行多算了约 240 行（各文件 `wc -l` 之和是 1,823，不是 2,060），其余差额在 ±10% 的误差内。

**日落规则**：到 v1.0 tag 时，子包里没被任何 v2 模块文档（02、09，或 01 的 v2 段）点名的模块删掉。点名清单在第一批合入时写进 02。

## 0.2 v2 前置：缺的桥（本次不做，只登记）

> **v2 以 `V2_TRAINING_PIPELINE.md` 为准（2026-10-08 定稿）。** 下表是瘦身时登记的缺口，不再更新；v2 的范围、做法、里程碑和行数以该文 §3.8、§5 为准。

| # | 项 | 内容 | 约行数 |
|---|---|---|---|
| 1 | 写门挂点 | 在 `AttemptBroker.call`（gateway.py:340）的写操作之前问 `write_gate`。默认 shadow，只记录；开启后能拒绝调用、返回错误输出。用 `surface.mutates`（automationbench.py:604）识别写操作，先修"POST 查询被算成写"。挂在宿主侧，C3 在可演化的 agent 程序下才有定义（DESIGN:89） | 150–250 |
| 2 | stop 生效 | 回推在包内（default_agent.py:224-235），worker 协议是一次尝试一个 `run`，宿主在返回点只能记录。要生效必须改包 API：要么 `ctx.decide`，要么宿主强制的完成协议（`ctx.finish`，宿主可以回推）。两者都会改变 runner 的 sha 和 worker 镜像 | 150–300 |
| 3 | 通用 target 与 judge 绑定 | `worker.stop` 推广为 `worker.decide.<point>`，去掉 stop 的硬编码（policies.py:66、bench.py:1580-1588、pipeline.py:441）。加一种 `judge` 绑定，给零样本或提示词 decider 用：现在只允许 `rule` 和 `model` 两种（policies.py:66），model 必须来自训练条目（experiment.py:250-253）。judge 要说清用哪个 provider、怎么计量（网关现在只有 actor 一个 provider）、身份怎么算 | 200–350 |
| 4 | 决策行字段 | 任务路径记录 probs、explored、propensity，并接入 `policy.route`；inference schema 加温度字段（policies.py:173-194 现在没有）。这会改变策略身份 | 100–160 |
| 5 | 打标器 | stop 最终时刻的标签 = 本次尝试是否成功，现有记录就能免费补；`CompletionProbe` 接到 runner（40–60）；用 must-not 断言参数匹配写调用参数（120–180）。"应写而未写"要反事实重放（250–350，可后做；上游用 uuid4 生成 ID 是难点）。"联系人工"在本基准上没有真值，只能用代理断言（工单标签、指派），或部署中的人工审批日志（B 里 claude_code 的模式） | 160–240 |
| 6 | 校准与阈值 | `build_task_dataset` 的 cal 组 → `fit_temperature` + `fit_threshold` → `parse_inference`；`calibrate_point` 按任务剖面重写 | 60–120 |
| 7 | 部署 | `App.bind` 加 serving（app.py:27），导出物钉住模型版本 | 50–100 |
| 8 | 外部模型导入 | 由 jev 训练时，需要一个 `module:Class` Trainer 适配器：导出 `DecisionExample` JSONL，导入模型 ref，返回 `TrainResult`；再给 store 加 hub ref | 80–150 |
| 9 | 上线监控与离线筛选 | `monitor` 按 `Releases.revoke` 和任务剖面的决策行重写；`replay` 按新的行形状重写 | 150–250 |
| 10 | 标签来源契约 | 决策蒸馏只用 env/human 标签，还是加一条老师标签路径（参照 actor 的 PASS/REPLACE）；写进 02。**已定（2026-10-08）**：加老师标签路径，来源只作血缘、按用途选择（V2_TRAINING_PIPELINE §2.5，定稿见 §7；§0.1 第 5 点） | 文档 |
| 11 | actor 蒸馏 | SFT 投影、网关上的教师 PASS/REPLACE、Tinker trainer、R 放开 `decision_train`/`distill_actor` | 1,000–1,600 |
| 12 | 作业归属 | 决定决策模型在 jev 还是 ouroloop 训练。在 ouroloop 训练：先修 C 里的漂移，补 Qwen decider 插件。在 jev 训练：`[train-jobs]` 可以删，只留第 8 项。**已定**：在 ouroloop 训练（用户 2026-10-07），`[train-jobs]` 保留；漂移在 v2.1 修（`tde` 插件），Qwen decider 插件是轨道 B 的 `decider_lora`（V2_TRAINING_PIPELINE §3.3）；命名按该文 §7 第 7 项 | — |

决策桥（第 1–8 项）合计约 1,000–1,700 行，其中约 500–700 行搬自子包。

**数据与收益上限**（子报告在 finance 的 train/dev 记录上统计，未逐一复核）：
- 已有记录的量与可用性以 `V2_TRAINING_PIPELINE.md` §2.1 的投影器计数为准（4 域 train：有效尝试 868、成功 415；W0 包的成功轨迹约 104 条，脱敏输出要靠重放回填）。原先这里的 309/148 是粗计。条款问题按用户决定（2026-10-07）不在设计范围内：交付的是开发者用自己的模型跑通的链，Claude 只用来验收链路。
- 对逐步决策标签不够：没问过问题，所以没有 question/answer/propensity；每一步没有世界状态快照；断言和"哪一次写"之间没有关联；Opus 的行为基本是确定的，看不到"不发"的后果。只能用 train 的记录，dev 是 gate。
- 一个完美的"别写"闸门，最多救回 train 失败的 8.1%（37/456）、dev 失败的 16.0%（116/725）（按 `V2_TRAINING_PIPELINE.md` §0.3 更正；旧数 14%/20% 的两组分母样本范围不同）。其余是该写没写或写错，属于生成能力。用同一模型在发送前把关已有两次反面结果：ceiling 修好 8 道里的 1 道、弄坏 2 道；v0.5 的 send_review 让 6 道原本通过的题坏了 2 道（m_router.py:17-19）。
- 所以现实的顺序是：先用零样本或提示词 decider（第 3 项的 judge），在 train 上校准阈值，再用配对 dev gate 测 C3。专门训练小模型，要等有部署流量或多领域数据。actor 换成 9B 学生之后，独立 controller 更可能有用。（已被 V2_TRAINING_PIPELINE §0.3、§4.1、§4.3 取代：C3 在学生上测，是 final 里的测量臂，不走 dev gate；G 是 decider 或规则门，judge 只作离线标签与 shadow 参照。）

## 1. 原则

- **留什么。** 提升效果、提升结果可信度、保护资金（预算、续跑不重付），三者占一样就留。三样都不沾的删掉。
- **只按证据判断。** 在有记录的运行（v0.1 到 v0.4 finance）里执行过、并且改变过决定或保护过钱的，留。从没执行过，或执行了但没改变过任何一个决定的，删或降为可选。
- **v0.5 两库等 support 的结果。** 现在一行不动，等结果出来后按 V0_5_PLAN §3 预注册的读法决定（见 §3 第三批）。
- **身份不动。** 已存记录引用的字段、字面量、preset 数值一律不删，只让对应逻辑失效。清单见 §4。（2026-10-08 用户定，放宽：现有活动都不在 HEAD 上续跑，哈希身份不必保持；守住的是读兼容、尝试行为与新活动的资金安全，见页首。）
- **先迁测试，后删代码。** 一个机制唯一的测试，和这个机制一起删。如果某项保证（续跑、暂停、预算）只在将要删的路径上被测过，先在生产路径上补测。
- **旧版本靠 tag，不靠 HEAD 兼容。** 已有 v0.1.0、v0.2.0、v0.3.0，再补 v0.4.0 和 v0.5.0。

## 2. 逐模块清单

"批"指 §3 的分批：1 = 第一批，2 = 第二批，3 = 第三批。"测试"列写测试文件和直接相关的测试数。

### 2.1 删（CUT）

| 路径 | 行 | 用途 | 当前路径上？（证据） | 贡献 | 建议·批 | 做法（含连带依赖） | 测试 | 可省 | 风险 |
|---|---|---|---|---|---|---|---|---|---|
| `evolve/{plan,harness}.py`；`pipeline.py`、`build.py`、`calibrate.py` 的旧半边；bench 的 `collect` 阶段与 `Improvement.collect` | ≈ 1,120 | v1 规则规划器；编码套件上的提示进化；冠军流水线（数据、检查、训练、校准、gate record、冠军指针）；旧 ledger 行到训练文件 | 否。只经由 `cli evolve/deploy/plan/calibrate/suite/research` 和 `--phase collect`（JOURNAL 与 v0.4/v0.5 的 runbook 里都没跑过） | 无 | CUT·1（已做，P1 第 3d、4b、5c、5e 步） | 删，连带删除 cli 对应命令和 `config.build_research/build_agent_factory`。**移走，不删**：`train_artifact`、`TrainingBuild`、`_probe_artifact`、`full_recipe`、build 的任务半边与 `candidates/to_example`、`checks.py`、`fit_temperature`、train.py 的契约、`config.build_trainer` → `[train]`；train.py 其余部分、`cloud.py`、`compute.py` → `[train-jobs]`（§0.1 A、C）。`bench._train/training_options` 原位保留（§0.1 D）。bench.py:504/511 的 import 改成懒导入 | test_evolve 13 删；test_jobs、test_cloud、test_tde_compat、test_evidence_training、test_train_config 随 extra 保留；test_research_round 的训练提案保留 | 1,120 | 低 |
| `serve.py` | 116 | HTTP 决策服务（自定义 `/decide` 协议） | 否。只经由 `cli serve`。jev 里没有服务实现这个协议，tde-serve 讲的是 `/v1/systemone` | 无 | CUT·1（已做，P1 第 3c；`backends/http.py` 5d 步） | 删，连同 `backends/http.py`（计入运行时枢纽一行）。**移走，不删**：`hosts/`、`ouroloop/worker.py`、`trainers/signature.py`、`trainers/tde.py` 的作业半边、`docker/worker/Dockerfile`、`bootstrap.sh`、`start.sh` → `[train-jobs]`；`trainers/__init__.py` 与 tde 的 serving 半边 → `[train]`。`release.PACKAGE_DATA` 和 pyproject 的 package-data 不动。注意 `ouroloop/worker.py` 不是 `agent/worker.py`，后者是隔离 worker | test_gate_bypass 15 随 gate 删；test_hosts 随 jobs；test_roles 拆分（§2.6） | 116 | 低 |
| `demo.py` 与 `providers.MockProvider` | 121 | 在合成数据上跑一轮旧的决策研究 | 否。只有 `cli demo` 调用。JOURNAL 记录它留到现在，只是因为"四个测试文件用它" | 无 | CUT·1（已做，P1 第 3b 步） | 连同 `cmd_demo`、`docs/DEMO.md` 一起删。test_tde_compat 保留，它的 `seed_ledger` 改用子包测试夹具 | test_act、test_evolve、test_loop、test_tde_compat 里的 `seed_ledger` | 121 | 低 |
| `agent/{prompt,suite,probe}.py` 与 `evals/` | 298 | v0.1 的本地编码任务套件、冠军提示存储、停止标签探针 | 否。只有 `cli suite/run/research` 和 `evolve/harness.py` 调用 | 无 | CUT·1（已做，P1 第 3d、3f 步） | 删除，连同 README 的 "Task suites" 一节和 `evals/`（3,090 行） | test_harness 7、test_probe 5 | 298 | 低 |
| `cli.py` 的旧命令：decide、smoke[Jev]、replay、demo、run、suite、research、evolve、deploy、plan、calibrate、serve、host、hook、gate、models、ledger，以及它们的辅助函数 | ~550 | v1 决策模型产品的命令行 | 否。v0.4/v0.5 的 runbook 只用 `bench`、`freeze`、`export` | 无 | CUT·1（已做，P1 第 3a 步） | 删除，旧版本在 tag 里仍可用。smoke/decide（≈ 45 行）搬成 `python -m ouroloop.train smoke --jev`（与 V2 §5 的链路 smoke 同一子命令，按参数区分） | 大多在整删的测试文件里 | 505 | 低；README 的快速入门会失效 |
| 运行时枢纽的冠军半边：`runtime.py` 的 `ask`、阈值查找、champion、pin、`_open`（换成约 25 行的 `Workspace(root)`）；`backends/http.py`；`points/` 的编码决策点；config 的 `build_runtime` 等构建器 | ≈ 480 | v1 决策运行时的冠军路径、自定义 HTTP 后端、编码决策点 | 只用作簿记。`open_activity` 调 `config.build_runtime`，会构建 KeywordBackend、Serving、dir store 和 BUILTIN 决策点（bench.py:422-423）。`PackageAgent.run` 调 `rt.session`，后者再调 `monitor.watch` 和 `Registry.champion()`，返回 None；这在每次付费尝试里都会持全局锁空转一次。ledger 里只有 `session` 行，pin 全为 null。`gateway.py:345` 的 `denied()` 只拦名叫 bash 的工具，而 AutomationBench 没有这个工具 | 无 | CUT·1（已做，P1 第 2、4b、5c 步） | 薄对象只提供 `.root` 和 `session()`，照写同样的 `session` 行和 `.release.json`；其中 `release.spec` 现在是 5 个内置点的 `spec_hash`（release.py:32-33、runtime.py:124、:137），改写成冻结常量。`import_object` 移到 config，`_digest` 移到 release。`make_agent_factory/PackageAgent/App` 改为接收这个薄对象。**移走，不删**：`decision`、`policy`、`types`、`serving`、`stores/`、`backends/{jev,llm,mock,tde}`、`business_stop_view/point`、runtime 的通用半边、`build_backend` → `[train]`（§0.1 A、B）；DENY 列表移到 `agent/`。**保留** `bench._host_contract` 里的字面量 `'ouroloop.agent.loop'` 和 `'points.compact'`，它们是身份内容 | test_live_jev 1 删；test_core、test_act、test_counting 拆分，通用部分随子包；test_backends 5 随子包；12 个文件用 KeywordBackend 当夹具，改从子包导入 | 480 | 中：每次尝试都经过这里。验收：在副本里比对 transcript、results 和 `session` 行，要求逐字节相同 |
| `evolve/{gate,replay,registry,monitor}.py` | 1,004 | 决策模型的冻结 gate、离线回放、模型注册与回滚、上线后的风险监视 | 空转。`build_runtime` 只调用 `template_path().exists()`，结果为 False | 无 | CUT·1（已做，P1 第 5c 步） | 删除。monitor、replay 的概念登记为 v2 重写（§0.2 第 9 项）；`replay.signature` 的键函数搬进 `trainers/signature.py`。**移走，不删**：`feedback.py` → `[train]`。`Improvement.measure` 每轮调的 `build_feedback`（bench.py:792、849、1075、1584、3810、4905）读 `rows('stop')`，结果恒为空；改成只在 K 绑了非 rule 的决策 target 或 `capabilities.training` 为真时才跑，不跑时写同样键名、同样形状的空值，R 的 `context_ref` 不变 | test_gate_record 31、test_gate_bypass 15、test_loop 8 删；test_counting 拆分 | 1,004 | 低 |
| v1 Agent 相关：`agent/loop.py`（`_stop_ctx/_trace` 除外）、`tools.py` 里的 Tools/SPECS、`harness_package` 的 mount 半边、`chat.ChatProvider`、`task_eval` 的 v1 分支与 probe 参数 | ≈ 765 | v1 Agent 循环、编码工具、包的 v1 挂载 | 否。K 带 `harness.max_effort` 时（v3 和 v5 的 preset 都带），`make_agent_factory` 走 `PackageAgent`（task_eval.py:386-398）。v1 分支只服务 v0.1/v0.2 的 K 和没有 preset 的测试夹具 | 无 | CUT·1（已做，P1 第 5a～5c 步） | `AgentResult` 移到 runner。v1 分支改为拒绝，并在提示里指向对应 tag。`smoke_max_effort` 在没有 preset 时回退到 `DEFAULT_MAX_EFFORT`。**移走，不删**：`stop.py`（StopGuard）和 `_stop_ctx/_trace` → `[train]`；`CompletionProbe` 原位保留，补直接单测（§0.1 D） | test_agent 12/15、TestTheInterventionWorks 删；TestCompletionProbe 改写成直接单测；另有约 25–40 个测试经 `tests/rsi.py` 跑在 v1 循环上，需要迁移 | 765 | 中：这是最大的一次测试迁移 |
| research 里的旧决策研究：`research/agent.py`、`ask.py`、`loop.py:1-127`、`basic.BasicResearcher` 及其常量、`loop._refused_model_changes`、`research/__init__` 的急切导出 | ≈ 430 | 旧决策剖面的研究循环 | 否。只有 `cli research/demo` 调用。但 `research/__init__` 是急切导入的，所以每次跑 bench 都会把 gate、replay、types 加载进来 | 无 | CUT·1（已做，P1 第 5c 步） | 删除，并把 `__init__` 改为懒导入。**原位保留**训练准入（`check_label_availability`、`registered_builders`、`model_change_reasons`、`core_checks` 的训练条款，≈ 70 行）：保留的 `build_candidate` 依赖它（policies.py:924） | test_loop 8、test_harness 7 | 430 | 低 |
| 散在保留模块里的冠军遗留：`ledger.CalibrationStore/SpecStore/PromotionConflict`；`release` 的 Release 三元组、Accepted、`resolve_profile`、`Releases.defaults/recover`；`policies` 的 SessionBinder、只有它调用的 `fixed_defaults`、`Services.runtime/probe_models` | ≈ 235 | v1 发布三元组、按 session 换绑、校准与 spec 存储 | 否。SessionBinder 在 policies.py 之外没有调用者。校准与 spec 存储只服务冠军路径 | 无 | CUT·1（已做，P1 第 5c 步） | 删除。**保留** REGISTRY 的全部目标（含 `worker.stop`）、BUILTINS、SCHEMAS 条目、PROFILES/ORIGINS，以及所有保留记录的全部字段。**原位保留**（懒用，§0.1 D）：ledger 的 `Row/Ledger/SessionStore`；`Revoked`、`Releases.revoked/revoke/last_good/rollback`；policies 的 `StopDecider`、`_bind_stop`、`parse_inference`、`Services.serving`、`build_candidate` 的训练循环；contracts 的 `EvidenceRef/DataManifest/Observation/DecisionTrace/FeedbackRecord`。`bootstrap_workspace` 移到 tests | test_policy_runtime、test_policies 里 SessionBinder 的部分删；test_task_gate 的回滚、吊销测试保留 | 235 | 低 |
| `providers/manual.py` 的 ManualProvider、parse_turn | 95 | 人工用文件应答的提供方，只用于彩排 | 否。只有 rehearsal 的配置用它。ROADMAP:313 记录会话路径上的人工彩排没有跑，已换成离线 dry_run | 无 | CUT·1（已做，P1 第 3e 步） | 和 rehearsal 一起删。`request_json`（11 行）移到 `claude_code.py` | test_manual 3 | 95 | 低 |
| frontier 臂：`bench.frontier_configs/FRONTIER_KEYS/actor_override`，以及 evaluate 里逐比较的 `llm` 与定价 | 50 | 在本地跑前沿模型作对照臂（C2） | 否。没有配置有 `[frontier]`。2026-09-28 已决定不在本地跑前沿模型，C2 引用官方榜单 | 无 | CUT·1（已做，P1 第 6a 步） | 删除。report 的 C2 改成固定的 not_run（见下面的研究位一行） | test_report 3 | 50 | 低 |
| `task_gate` 的经济模式、protected 需求、`max_runtime_cost_ratio/_ratio`、v1 dev `screen()`，以及 `bench._screen` 处理器 | 85 | 非劣且更便宜的判决、受保护需求的否决、成本比上限、预声明 dev 题的廉价筛选 | 逻辑上从未生效。54 份冻结计划里：economy 0 份，protected 非空 0 份，cost_ratio 非空 0 份，`screen_ids` 非空只有 linkcheck2（本地冒烟）。`_screen` 遇到空列表立即返回 pass | 无 | CUT·1（已做，P1 第 6b 步） | 删逻辑，**字段全部保留**，因为计划哈希依赖它们。`execute_experiment` 的 screening 步骤删掉，STATES 保留，以便读旧日志 | test_task_gate ~6、test_real_run_findings 2、test_paid_run_safety 1 例 | 85 | 低：现有计划的判决不会变 |
| 没有调用者的函数：`Improvement.train_state_literals`、`diagnosis.train_literals`、`automationbench.assertion_class`、`contracts.ArtifactRef/Requirement/combine`、`experience.verdict/summary_record/requirement_status`、`execution.spend/open_reservations/_takes`（只有测试在用） | ~150 | — | 否。`ouroloop/` 内没有调用者 | 无 | CUT·1（已做，P1 第 6c 步） | 删除。用到它们的测试改用 `reserve/settle` | test_contracts 2、test_research_round 若干 | 150 | 无 |
| v0.3 兼容代码：`session.passing_sample`、`ScreenSettings.v04` 开关、`pick_best` 的 int 路径、floors=0 路径、`session_inputs` 的 v0.3 分支 | 60 | 让 v0.3 的 K 照旧运行 | 否。v3 和 v5 的 preset 都带 v0.4 的数字；v0.3 的工作区已经结束 | 无 | CUT·1（已做，P1 第 6d 步） | 删除。workspace-claude-v3 只能在 tag v0.3.0 上续跑 | test_research_round 少数几个 | 60 | 低 |
| **研究位 / R0–R1 战役（C4）**：`research/campaign.py`；bench 的 `research()`、`_install_research`、`_campaign_*`、`_run_campaign_arm`、`_arm_outcome`、`_evaluate_outer`、`_cycle_slot`、`research_slot`、`slot_headroom`、`never_passed`、`per_domain_counts`、`cycle_pairs`、`halve_dev`、`_reuse_fact`、`_copy_*`、`_starting/_seed_experience`、`common_inputs_estimate`（bench.py:3521-4153、4616-4958）；`cycle()` 里的研究位分支；preset 的 campaign 表；`RoundLog.watch/requests`；`basic.propose_research`；`loop.pause_of`；experiment 的 research 分支；`Results.copy_grading`；`contracts.copy_closure`；report 的 C4、复用、余量、延长部分（C2/C3 改为固定 not_run，各附一句理由） | ~1,700 | R0 提出 R1；两个隔离臂各跑一遍 worker 循环；外层比较判定 R1 是否更好；安装 R1 并验证复用 | 否。v3/v4/v5 的全部配置都写 `research_slots = 0`（例如 claude-v4-finance.toml:162），`cycle()` 在 `_cycle_slot` 之前 break（bench.py:4483）。`--phase research` 自 v0.2 之后没再跑过。唯一还在用的是常量 `campaign.PAUSES`（loop.py:546） | 无。v0.2 结论不确定：两臂封存了同一个 W\*，没有做外层比较（RESULTS:289/297）。v0.3、v0.4 都推迟了。v0.5 改用 `support_h1.py` 做跨活动比较（对照停止，H1 没有算；脚本 2026-10-08 已删，tag `archive-2026-10-08` 上可读） | CUT·2（编排）；C4 能力保留 | 删活动内编排。**抽出保留** `research/recursion.py`（≈ 200 行）：`judge_research`、`carries/unused_changes`、`CampaignPair` 的不重叠检查、`discovery` 标签，连同从 test_campaign 移植的测试。`basic.propose_research` 照删：它只服务 note-contract R（experiment.py:396-402）。`PAUSES` 移到 loop.py。**保留字段** `FinalizationPlan.research_campaign_plan_ref` 和 `Schedule.research_slots`。C4 改为跨活动比较（§0 (a)） | test_campaign 29（`judge_research` 等的测试移到 test_recursion，≈ 200 行）、test_cycle ~30、test_rsi_integration ~16、test_real_run_findings 2–5、test_report 2；其余 5 个文件各 1 个；合计约 85 个测试、约 1,900 行 | 1,500 | 路径上低风险；C4 的新接线（≈ 100–200 行）随下一个新领域活动做，不算瘦身 |
| **多领域合并**：`benchmark.pool_manifests/POOLED_RULE/spent/claim_ids`、`automationbench.pool_suites`、`bench.load_domains` 的多领域分支、`pooled_name`、`_one_dev_task_per_domain`、`domain_problems`、smoke 里逐领域的部分、`task_gate.per_domain`、`experience.gate_by_domain/strip_dev_task_ids`、`diagnosis.select_failures` 的领域交错、report 的 `_per_domain/_final_by_domain` | ~380 | 让多个领域共享一个包的合并活动 | 否。这些代码都以 `len(domains) > 1` 为前提，而三份当前配置各只列一个领域。ROADMAP v0.4 的决定是"每个新领域一个单领域活动" | 无。v0.3 的五领域活动三轮都没有候选通过 gate | CUT·2 | `domains=[x]` 作为 `domain` 的别名继续接受。`bench/<domain>` 路径不变，单领域的划分哈希不受影响 | test_bench_phase ~5、test_report 4，另有 4 个文件各 1 个（约 400 行） | 380 | 低：v0.3 报告重渲染时会少逐领域表，但数字已写进 RESULTS |
| **采集批**：`Improvement.collect_batch/_run_collection/finish_collections/_collection_batches`、improve 里 collector 的接线、`run_worker_rounds` 的 collect 分支、`unanswered_collections`、session 的 needs_collection 结束条件、`max_collection_batches` | 130 | R 回答 collect 时，额外付费跑一批 train 尝试 | 接线在，但从没触发过：四个真实工作区里 `collection_booked` 都是 0 次 | 无。每轮测量本来就会重跑失败的 train 题 | CUT·2 | collect 回答当作 no_proposal 处理。**保留** `Schedule.collection_batches` 字段和它的值 2，因为冻结的日程 JSON 里有这个值 | test_cycle 9 处、test_research_round 5、test_harness_loop 3 | 130 | 低 |
| **段延长**：`_extension_of/_record_extension`、improve 和 RoundLog 里的延长分支、cli 的 `--reason/--rounds`、report 的 `extensions` | 85 | 在冻结日程之外记录在案地续跑一个已结束的段 | 只在对 cycle 活动直接执行 `--phase improve` 时才会走到。只用过一次（v0.3 第 3 轮，被拒）。v0.4/v0.5 的预注册明确禁止续跑同一条管线（ROADMAP v0.4 §4） | 无 | CUT·2 | 对 cycle 活动执行 `--phase improve` 改为拒绝 | test_bench_phase 2、test_harness_loop 1 | 85 | 低 |
| **旧轮次路径**：v2 的无包轮（diagnose→propose）和 v0.1/v0.2 的总结调用轮，包括 `loop.py:189-273`、`_after_drafts/_admit_and_run/_has_patch`、`experiment.propose_experiment/note_contract`、`basic.diagnose/propose/summarize/revise` 及其格式常量、`drafts.resolve` 一族；另外还有 bench 的 `marketing_harness_v1` preset、没有 preset 时的 `[harness_evolution]` 路径、`round_inputs` 的无 harness 分支、`package_api` 的 v1 分支、LATER_METHODS | ~550 | 编码会话出现之前的 R 轮次 | 否。K 有 `research_session` 时，`round_inputs` 一定构建会话输入（bench.py:1138-1140）；v3 preset 设了它（bench.py:1863），v5 复制了 v3；`run_worker_round` 在 loop.py:186-188 就直接返回 `run_session_round` | 对当前结果无贡献，但它是大多数 loop 和 cycle 测试的底座 | CUT·2 | **先**把夹具迁到 v3 preset 加 `tests/rsi.SessionScript`（见 2.6），**再**删代码。`run_worker_round` 简化为只调 `run_session_round` | 直接相关约 30 个；作为底座约 150 个 | 550 | 中：约 120–150 个测试要改写；改完后资金安全测试测的是生产路径 |
| `research/api_errors.py`（R 的输入文件 api_errors.json） | 180 | 从 train 记录按端点汇总调用、错误、重试、未读的下一页、空结果 | 在路径上：`session_inputs` 生成它，并作为 `inputs_files['api_errors.json']` 交给 R | 看不出贡献。FINANCE_V04_ANALYSIS §4 1.2 评为"部分"：R 从没打开过这个文件，只引用过一个数字；主信号 401 来自服务门，21 次里 0 次落在计分服务上 | CUT·2（support 结束后） | 两臂都把它交给 R，所以 support 结束前不能动。如果 v0.5 保留，`_calls`（20 行）移到 methods.py。R0 会话文本里提到它的那一句也删掉；这会给新活动一个新身份，已冻结的活动不受影响 | test_diagnosis 2 | 180 | 低 |

**第一批的执行（2026-10-08，分支 `slim/1`，[PLAN](../PLAN.md) P1，提交与读数见 JOURNAL 2026-10-08 瘦身第一批一条）。** 上表标 CUT·1 的行都已做，括号里是 P1 的步号。与“做法”列不同的地方：
- **v1 Agent 一行**：“v1 分支改为拒绝”没做。没有 `max_effort` 的 K 现在走运行壳、effort 不截顶；改为拒绝要先把约 60 个无 harness 的夹具迁到 v3 preset，归第二批。
- **serve 一行**：`trainers/signature.py` 与 tde 的作业半边没有移进 `jobs/`，两个插件整个留在 `ouroloop/train/trainers/`。一个插件是一个模块，`validate`、`train`、`backend` 缺一不可，而加载插件的 serving 在 `[train]`；插件里只在 worker 侧跑的函数懒导入 `jobs.worker`。`release.PACKAGE_DATA` 与 pyproject 的 package-data 改指 `train/jobs/hosts/`：身份不必再守（§3 第 0 步的注）。`backends/http.py` 随 serving 的 url 模式一起删（5d）；别的机器上训练的模型改为发布到 `hf` 库。
- **cli 一行**：`host` 搬进 `python -m ouroloop.train host`，没删：`hosts/` 与它的测试随 `[train-jobs]` 留着，worker 与 TDE 的报错也指向它。`evolve/harness.py` 与 `config.build_agent_factory` 提前到第 3 步删，因为它们在模块顶层导入 suite 和 prompt。
- **plan/harness 一行**：`Improvement.collect` 留着，第二批的 v2 轮次与研究位还用它；删的只是 `--phase collect`。
- **冠军遗留一行**：`Releases.recover` 留着（IT-04 与 `test_task_gate` 用它读安装记录）；`Releases.defaults` 删了，所以 `rollback` 只退到一个好的祖先，没有可退的祖先时先拒绝、什么都不撤销（原来可能把 active 指回刚撤销的那个）。`release.Revoked` 与 `revoke` 按 §0.1 D 留着，现在没人调用（评审后有了往返测试）。
- **运行时枢纽一行**：`release.spec` 写冻结常量 `BUILTIN_SPEC`（`workspace.py`）；`_host_contract` 的字面量 `ouroloop.agent.loop` 没动（只是外观）。
- **命名**：模块名保留 `decision.py`、`feedback.py`，没按 §2.3 改成 point、labels。Claude Code 适配器在 `train/claude_code.py`，去掉了 `live` 模式，`settings()` 要显式给 hook 命令（`ouroloop hook` 已删）。
- **[train] 里的一处行为改动**：`train_artifact` 失败时原来只留原因的前 400 字符，现在留头和尾；失败作业的原因在日志末尾。
- **评审之后（2026-10-08，JOURNAL 验收与评审一条）**：`train/types.py` 的 `brier`、`train/policy.py` 的 `cp_lower` 删了（没有调用者，没有 V2 段落点名）；`tests/test_core.py` 移进 `tests/train/`；`checks.py` 与 `fit_temperature` 随 test_evolve 失去的测试补回 `tests/train/test_checks.py`；`release.Revoked`/`revoke` 有了往返测试。
- **不再有测试的保证**：hook 入口的失效保护（随 `ouroloop hook` 删）；PR-05（撤销后重新绑定 session，一次运行臂现在只绑一次）；CT-05（遗留冠军与安装的 release 并存）；HTTP 后端的重试与分块；“训练出的 decider 改变 agent 的行为”（§2.6 已写明，要等宿主重新问决策点，V2 §3.8）。

### 2.2 简化（SIMPLIFY，模块保留，只删内部）

| 路径 | 行 | 用途 | 当前路径上？ | 贡献 | 建议·批 | 做法 | 测试 | 可省 | 风险 |
|---|---|---|---|---|---|---|---|---|---|
| `research/context.py`、`inspection.py`，以及 `experiment.check_input_coverage/check_label_availability` 和 R0 preflight 里对应的两项 | 279 | 投影出 R 能读到的内容 | 每轮都在构建，但在会话路径里 R 一样都不读：会话状态来自笔记、直方图、台账和轨迹。所有 AutomationBench 工作区都没有 trace 目录，所以 input_coverage 恒定警告 "no trace coverage"；这条警告是 v4-finance 和 v3 全部 6 份实验记录里唯一的 open_question | 名义上的可信度。真正有用的只有 `check_evidence`（约 15 行，越界证据引用触发 AccessDenied） | SIMPLIFY·2（support 结束后） | 只保留 `ResearchContext(scope, goal, release, experience_ref, budget, evidence)` 加 `check_evidence`。**`check_label_availability` 不删**：它是训练准入（§0.1 D） | test_evidence_training 2、test_research_round 2，另有 3 个文件各 1 个 | 250 | 低到中：R0 的 preflight 配置在 R0 策略集哈希里，新活动会得到新的 `research_ref`；已冻结的不受影响 |
| final 的预算检查：`final_share_problem`（freeze 时）、`final_plan_problem`（freeze 时）、evaluate 里的内联检查、`_final_estimate_at_freeze` | 221→~170 | 付费前确认 final 份额够用 | 在路径上 | 资金安全 | SIMPLIFY·2 | 合并成一个 `final_fits(activity, plan, share_left)`，取最严的规则：期望花费 + 并发数 × 单次上界 + STUCK_BOUNDS × 单次上界；freeze 和 evaluate 都调用它 | test_paid_run_safety ~7 作为规格 | 50 | 低到中：现有的拒绝测试一个不删 |
| bench 和 cli 的小分支：`_five_limits/limit_problems(five=False)`；`run_phase` 的 NOT_YET/IMPLEMENTED；`RsiConfig.research_plan_ref/profile/train/initial_release_ref/core_ref`；`open_activity` 的 v2 bootstrap（`capabilities['training']` 键**保留**）；`startup_checks` 的 openai 键检查和 v2 检查；`Improvement` 的 trainer/serving/probe_train/actor_override/owner 字段；cli 的 `--repeats/--reason/--plan`；`cycle()` 拉直成 baseline → segment-0 → report | 每处 10–30 | — | 在路径上，但这些分支不执行 | 无 | SIMPLIFY·2 | 逐项删除。`cycle` 保留计划身份记录、`guarded()` 暂停处理，以及 `--until baseline` / `segment-0/round-K` | run_phase 5、RsiConfig 3 | ~180 | 低；旧 cycle.json 里多出的键会被忽略 |
| `bench.py` 的文档字符串和纯注释行（677 + 369 行，占 27%，多是带日期的版本史） | ~1,046 | — | — | 只有不变量对审阅者有用 | SIMPLIFY·收尾 | 每处只留一行不变量和设计引用，带日期的理由移到 JOURNAL。单独一个提交，放在最后做 | — | ~400 | 无 |

### 2.3 降为可选（MAKE OPTIONAL）

| 对象 | 行 | 证据 | 做法 | 风险 |
|---|---|---|---|---|
| 成本项 `cost_beta0/1`（task_gate） | ~20 | 从没决定过一次结果。finance v0.4 第 2、3 轮没过成本项，但已经因增益为负被拒（FINANCE_V04_ANALYSIS:408）；v0.3 各轮也都因增益被拒。v3 以后的每份冻结计划都带着它，包括 support 两臂 | 代码保留，因为删掉会改变已封存计划的判决。下一版 preset 去掉它；等没有可续跑的计划再带它时，再删这 20 行 | 现在删是高风险，只改 preset 则无风险 |
| `control_readings = 3` | 0（配置） | finance 里没改变任何决定（§4 1.6(c)：任何一次单独读数都超过 min_gain），多花了 $21.85。分析的建议是等量出 support 的噪声再定 | support 结束后，如果噪声小，下一版配置设为 1–2 | 无 |
| `research/method_kit/reference/` 和 `methods.reference_files` | 565 | 没有任何阶段会触达它，只有 test_methods 调用。V0_5_PLAN §1.2 写明它只作夹具和可选消融 | 移到 `tests/fixtures/method_reference/`（第三批）；如果方法库被删，就一起删 | 低 |
| `providers/anthropic.py`、`openai_compat.py` | 208 | 当前配置不用。它们本来就是懒加载，anthropic SDK 已经是 extra；GLM-5.3 是"推迟，不删"（JOURNAL 2026-10-03） | 保持可选，在 README 里标成"API 提供方" extra | 无 |
| `ouroloop[train]`：新子包 `ouroloop/train/`（types、point、policy、runtime 通用半边、views、labels、dataset、train、calibrate、checks、backends、serving、stores、trainers 契约、StopGuard、trace、adapters/claude_code）。**已建（2026-10-08，P1 第 4 步）**：实际 3,042 行，模块名与点名见 modules/02 §3c | ≈ 2,400 | 用户 2026-10-07：训练链与决策模型要保留；M2 要在 `train_artifact` 上扩展（02:51、:134）。当前路径不执行（§0） | 懒导入，自带测试（≈ 1,300 行），`pip install ouroloop[train]`，入口 `python -m ouroloop.train`（命名按 V2_TRAINING_PIPELINE §7 第 7 项）。逐模块见 §0.1 A、B。日落规则见 §0.1 | 测试常驻 CI；`worker.stop` 在 REGISTRY、SCHEMAS 里的身份要一直守着 |
| `ouroloop[train-jobs]`：`ouroloop/train/jobs/`（train、cloud、compute、worker、hosts、trainer 的作业半边、signature、训练主机 Dockerfile）。**已建（2026-10-08，P1 第 4 步）**：实际 1,697 行，`evolve/train.py` 成了 `jobs/trainer.py`；两个插件留在 `train/trainers/`（§2.1 末的注） | ≈ 2,000，另有 sh 207、Dockerfile 66 | M2 点名复用 `test_jobs`（02:134）；云计费安全逻辑重建很贵 | 依赖 `[train]`。归属已定：在 ouroloop 训练（§0.2 第 12 项）；已知漂移在瘦身里登记不修，v2.1 修（§0.1 C） | 测试 1,766 行常驻 CI；与 jev 的作业链重复 |
| 原位保留（懒用）：contracts 决策记录、policies 的 Decider 与模型绑定、ledger 行、`Releases.revoke/rollback/last_good`、`CompletionProbe`、训练准入、`bench._train` | ≈ 700 | §0.1 D | 留在原模块，热路径不执行；rule 类绑定不构造 `DecisionPoint` | 低 |

### 2.4 待定（第三批：v0.5 两库，等 support 的结果）

只在 preset `automationbench_harness_v5` 下才会触达（`claude-v5-support.toml` 跑过；对照 2026-10-08 停止）。v0.4 的 K 下，`method_settings()` 和 `library_settings()` 都返回 None。

| 路径 | 行 | 用途 | 触达 | 测试 |
|---|---|---|---|---|
| `research/methods.py` 和 `method_kit/` 核心（不含 reference） | 1,941 | 方法库：R 写的只读 `m_` 工具、路由与表、kit 补齐与检查、触达检查、learn 孪生与拟合、剧本检查 | Drafts.methods/reach、session 的 TOOLS_V5、`Improvement.method_inputs`、`--phase learn` | test_methods 33 |
| `research/library.py` | 640 | 经验库：提炼、检索、记功与退役、整库过 gate | `--phase library`、`drafts.complete`、runner 的 `retrieval_block` | test_library 12 |
| `research/fixed.py` | 293 | 固定代码的轮次（快照、重拟），可按序号续跑，不重复付费 | learn、library_track | test_v05_phases 22 |
| bench 的 v0.5 部分：`learn`、`library_track`、`_phase_work`、`_fixed_context`、`_refit_*`、`_v5_preset`、v5 的 K 表、`Improvement.method_settings…method_inputs`、run_arm 的 explore 表守卫 | 640 | 两个只在 train 上跑的阶段及其输入 | 只在 v5 下 | test_v05 10、test_v05_phases |
| report 的 `v05_evidence`、`m_tool_writes`、`_v05_lines`；session/drafts/runner 的 v0.5 分支 | ~240 | 过程证据；宿主对只读方法契约的审计 | 只在 v5 下 | test_v05_phases 3、test_methods 1 |

### 2.5 保留（KEEP）

| 路径 | 行 | 用途 | 当前路径上？（证据） | 贡献 | 内部可省 |
|---|---|---|---|---|---|
| `bench.Improvement` 核心（services、run_identity、run_arm、control_pool、measure、train_measurement、prior_readings、`_same_run`、round_inputs、session_inputs、train_screen、handlers、`_begin/_build/_evaluate/_install/_verify_bound/_preflight`） | 717 | 每领域贪心环的接线 | 是。调用链：`cli bench --phase cycle` → `cycle` → `baseline/improve` → `improvement()`；`run_worker_rounds` → `handlers()`。finance 的三轮都经过这里（JOURNAL 2026-10-06） | 结果；资金（按 attempt id 续跑、测量复用、gate 前的 `_verify_bound`、`_same_run` 拒绝）；可信度（按执行身份合并对照、确认臂） | 30 |
| `bench.improve()`、`cycle()` 及 `RoundLog` | ~510 | 一个 worker 段：测量→诊断→R 会话→构建→筛选→gate→安装；可续跑；分段停 | 是。finance 全程用 `--phase cycle --until baseline` 和 `segment-0/round-K` | 结果；资金（续跑、停点）；可信度（`_beyond_preset` 拒绝更松的 gate） | 计入 2.1、2.2 |
| bench 的冻结、封存、评测、报告（FinalizationPlan、seal、evaluate、freeze、report） | 322 | 在读 test 之前封存 final；评测不重抽；坏的臂照样保留 | 是。support 两臂 2026-10-07 已 freeze；finance 的 final（W\* 62.7% 对 W0 54.7%） | 可信度、资金 | 30 |
| bench 的预算、限额、启动检查、冒烟（activity_budget、freeze_task_plan、`_attempt_budget`、limit_problems、startup_checks、isolated_worker、harness_checks、smoke） | ~750 | 付费前拒绝：漂移、计分契约、模型表、五项硬限额、max_tokens 对预留上界、Docker worker、effort 上限；冒烟查泄漏和近重复族 | 是。每个付费阶段都调 `require(startup_checks(...))` | 资金（v0.2 的第一次 final 死在预留上）；可信度（隔离在这里核验，镜像按 id 钉住） | 计入 2.1、2.2 |
| bench 的配置与开活动（BenchConfig、freeze_splits、presets/settings、open_activity、_host_contract、_default_researcher、_Completions、attempt id 辅助、baseline） | ~1,030 | 冻结划分；只紧不松的 preset 冻进 K；钉住 actor、镜像 id 和宿主契约；R 的调用记在活动账上；W0 基线 | 是。每个阶段都走 `open_activity`；baseline 是 cycle 的第一步 | 可信度、资金 | 计入 2.1、2.2 |
| `cli` 的 bench/freeze/export；`config.load/build_provider` | ~155 | 当前唯一使用的入口 | 是 | 资金（付费阶段的 `--checks-only` 守卫）；export 是交付物 | — |
| `benchmark.py` 核心 | ~625 | 运行契约：按族划分、运行身份哈希、有效性、只追加存储 | 是，所有阶段 | 可信度（无效不等于失败，缺成本不等于 0）、资金（续跑） | 0；`RunManifest.payload` 一字不改 |
| `report.py` 核心 | ~780 | 从记录重算报告；C1 只取自封存 final 的配对区间；W0 的 pass@k 参考 | 是，`--phase report` 和 cycle 收尾 | 可信度 | 0 |
| `evolve/task_gate.py` 核心 | ~590 | 配对 dev gate：合并对照补读、确认读数、按族自助、判决与收据 | 是。调用链：`_evaluate` → `evaluate_task` → `judge` | 可信度（A/A 回放 0/20 的确认规则）；资金（`shortfall` 不重付对照） | 0；字段一个不删 |
| `evolve/execution.py` | 973 | 实验状态机、单一预算账（预留、分配、余量）、MeteredProvider、Schedule | 是 | 资金 | 计入 2.1 |
| `evolve/{contracts,experience,task_eval}.py` 的保留部分 | ~1,440 | 身份与记录、E 日志与 R 的台账、跑尝试并分类 | 是 | 可信度、资金 | 计入 2.1 |
| `release.py` 核心、`policies.py` 核心、`ledger.py` 的 I/O 工具 | ~1,300 | 发布、带预检的 CAS 安装、导出；注册表、绑定、W0/R0/K 默认值；原子写、锁、脱敏 | 是 | 可信度、交付物 | 计入 2.1 |
| `research/session.py` | 1,457 | R 的编码会话，加上固定的草稿链、准入、train 筛选、一次 dev gate | 是。调用链：loop.py:186-188 → `run_session_round`；finance 的记录用遍了每个工具 | 结果（每个候选都来自这里）；可信度（R 只读 train，dev 只以失败类直方图的形式出现） | 计入 2.1 |
| `research/{loop 驱动, experiment 准入, diagnosis, drafts 检查链, basic 会话子集}` | ~1,840 | 续跑与停止规则；准入绊线；逐题笔记与缓存；字面量检查；R 的计量调用 | 是 | 资金（不重提、不重跑）；结果（FINANCE §3：8 条规则里 7 条一一对应记过笔记的题）；可信度（字面量检查修好后 finance 0/8 误拒，v0.3 是 14/20） | 计入 2.1 |
| `agent/{runner, gateway, worker, worker_runner, default_agent}.py`、`harness_package` 核心、`tools.ToolResult`、`chat` 类型 | ~3,190 | runner shell、宿主侧限额、Docker 隔离（无网络、只读根、丢弃能力、runner 摘要核验）、种子默认 agent、包格式 | 是。调用链：run_arm → make_agent_factory → PackageAgent | 结果；资金（调用前查硬限额）；可信度（生成代码不在控制进程里运行） | 0 |
| `providers/claude_code.py` 和错误分类 | ~490 | 订阅登录的 headless Claude Code；账户错误时暂停，而不是记成尝试失败 | 是。三份当前配置的 `[llm]` 和 `[research.llm]` 都用它 | 结果、资金 | 0 |
| `adapters/automationbench.py` 核心、`app.py` | ~900 | 基准契约与计分；导出包的加载器 | 是；app 只在 test_export 里用到 | 结果、可信度、交付物 | 计入 2.1 |

### 2.6 测试、示例与文档

| 对象 | 行 | 建议·批 | 做法 |
|---|---|---|---|
| 整个文件删除的旧测试，共 6 个：test_evolve、test_gate_bypass、test_gate_record、test_loop、test_probe、test_live_jev；另加 conftest 里的 v1 夹具 | ~1,800 | CUT·1（已做；test_probe 的两个 stop 点测试最后落在 `tests/train/test_stop.py`，test_live_jev 随 JevBackend 留下、移进 `tests/train/`） | 和它们所测的模块放在**同一个提交**里删，不能提前删，因为它们是那些模块唯一的测试。test_live_jev 删掉后没有 live 测试了，`live` marker 的定义照 `slow` 留着，两条测试命令照旧（PLAN P1“标记”一行） |
| 拆分：test_act、test_core、test_counting、test_roles、test_policy_runtime（共 1,782 行） | 删 ~1,100，留 ~600 | CUT·1 / 移入 extra（已做：test_act 删，留下的部分进 test_core 与 `tests/train/test_integrations`；test_counting → `tests/train/`；test_roles 的库、serving 与 JobTrainer 一半 → `tests/train/jobs/`；PR 用例 → `tests/train/test_stop.py`） | 冠军、pin、校准存储、deploy 的部分删；stores、serving、`record/propose/outcome`、Activity 计数的部分移到子包测试 |
| 随 `[train]` 移走：test_backends 86、test_tde_compat 221（没有 `TDE_REPO` 时跳过）、test_train_config 15、test_integrations 120、test_evidence_training 269 | ~710 | 移入 extra（已做，`tests/train/`；IT-02 改写，见下面 test_policy_runtime 一行） | 加上拆分来的 ~600，子包测试约 1,300 行 |
| 随 `[train-jobs]` 移走：test_jobs 674、test_cloud 630、test_hosts 462 | 1,766 | 移入 extra（已做，`tests/train/jobs/`；gate 与 registry 的用例改写到 `train_artifact` 上，共享夹具在 `jobs_fixtures.py`） | 照常跑；归属已定为 ouroloop（§0.2 第 12 项） |
| test_harness、test_manual，以及 test_agent 里 12 个 v1 Agent 测试 | ~595 | CUT·1（已做） | 3 个提供方测试并入 test_providers |
| test_policy_runtime、test_policies 里 SessionBinder 和冠军的部分；test_automationbench 的 TestTheInterventionWorks；test_task_gate 的经济模式、protected、screen | ~700 | CUT·1 | TestCompletionProbe 改写成在 `Episode` 上直接调用的单测（≈ 40 行）。IT-02 改写，终点改为 `build_candidate` 加预检绑定（feedback → build → `train_artifact` → 绑定的 model 策略），决策行经 `Runtime.propose` 直接写。test_research_round 的训练提案、test_task_gate 的回滚与吊销保留。约 15 个上下文分区和注册表的测试保留，迁到 v3 的 K 上。**明说**：在 §0.2 第 1–2 项建成之前，"训练出的 decider 改变 agent 行为"没有测试。**已做（2026-10-08）**：IT-02 的终点是标签 → 数据集 → 预算账上训练出的产物 → `worker.stop` 绑定该模型的候选，过预检、在 gate 上比较（它赢不了对照）；TestCompletionProbe 改成在 episode 上的直接单测；economy、protected、screen 的测试换成一条“这些条款被拒绝、留下的字段不改计划 ref”的测试 |
| `tests/rsi.py` | 512→~340 | SIMPLIFY·1（已做，P1 第 5b 步：冻结一个带 `max_effort` 的 v3 式 harness 表，126 个原走 v1 循环的测试换到运行壳上） | 删 probe；PhraseDecider、PhraseTrainer、UnknownJobTrainer 移到子包测试的夹具。`workspace()` 改为冻结一个带 max_effort 的 v3 式 harness 表，去掉 `rt`。这样约 25–40 个测试从 v1 循环换到 runner shell 上跑 |
| test_cycle 的 `fixture_preset` 等夹具，迁到 v3 preset 加 `SessionScript` | 0（改写约 120–150 个测试） | SIMPLIFY·2 | 先迁夹具，再删 2.1 的旧轮次路径。`test_br6_09…` 改写成没有研究位的整条 cycle 端到端测试 |
| test_campaign（`judge_research`、`carries`、不重叠检查的测试除外，这些移到 test_recursion，≈ 200 行），以及各文件里的研究位、多领域、采集批、段延长、C2/C3/C4 测试 | ~2,600 | CUT·2 | gate 的对照臂和候选臂测试保留（这里的"臂"指 gate 的臂，不是研究位的臂） |
| test_ceiling、ceiling_run.py、claude-ceiling.toml、`ceiling/{package,wstar_package}` | ~2,300 | CUT·1 | 实验已经结束（RESULTS v0.3，判为 "training"，花费 $45.28）。保留 `ceiling/README.md` 和 `tasks.json`（RESULTS 链接了它们），并加一行说明"在 tag v0.3.0 上运行"。**已做（2026-10-08）**：已 `git rm`，tag `v0.3.0`、`v0.4.0`、`archive-2026-10-08` 上可读；README 已加那一行 |
| test_rehearsal_world 和 `examples/rehearsal/`（156 个存档 JSON、第二个迷你上游、answer/check_world/dry_run/actor 脚本、2 个 toml） | ~2,450 | CUT·1 | 这是首次付费前的彩排，已被真实付费运行取代。它的 dry_run 走研究位。Docker 隔离仍由 test_package_worker 的 4 个测试覆盖。**已做（2026-10-08）**：跟踪的文件已 `git rm`，tag `v0.4.0`、`archive-2026-10-08` 上可读；被 git 忽略的 `workspace-rehearsal*`、`workspace-actor-manual` 与 `*.local.toml` 留在本机原处，不删 |
| test_aa_replay 和 aa_replay.py | 277 | KEEP | 它是确认规则的证据。加上 skip-if-absent：它依赖 gitignore 掉的 workspace-claude 记录，在干净检出和 CI 上会失败（JOURNAL 2026-10-07）。**已做（2026-10-08）** |
| v0.5 测试和 support_h1.py | 2,024 | PENDING·3 | test_support_h1 和 support_h1.py 保留到 RESULTS 写完为止。2026-10-08：对照停止，H1 没有算，这两个文件已 `git rm`（tag `archive-2026-10-08` 上可读）；其余 v0.5 测试照第三批 |
| 其余当前路径的测试（约 750 个） | ~13,800 | KEEP | — |
| HEAD 上已经跑不起来的旧配置（只算跟踪的）：claude.toml、claude-full.toml、improvement.toml、marketing.toml、claude-v3.toml、examples/ouroloop.toml，以及已删的 claude-ceiling.toml、claude-full-final.toml、claude-v3-pilot.toml、zhipu.toml。只删跟踪的；`*.local.toml` 是本机记录，不删（PLAN 规则 12） | — | CUT·2 | 留着会让人以为还能跑。RESULTS 里的链接和 Reproduce 改指对应 tag。2026-10-08 已先删其中没有测试读的 claude-ceiling.toml、claude-full-final.toml、claude-v3-pilot.toml、zhipu.toml（连同 `examples/__init__.py`），其余随第一、二批 |
| 文档：DEMO（30）删；TRAINING_HOSTS（233）随 `[train-jobs]` 保留；modules/06_RESEARCH_CAMPAIGN（121）改写成跨活动 C4 | 30 | CUT·1/2 | 第一批的部分已做（2026-10-08）：DEMO、RESEARCH_AGENT、`history/V0_5_IMPLEMENTATION.md`、`docs/assets/` 已 `git rm`（tag `archive-2026-10-08` 上可读），TRAINING_HOSTS 改到新路径；modules/06 随第二批 |
| 文档移入 history：REHEARSAL（373）；V0_4_SHARDED_RSI（545）和 RSI_PLAN（497），这两份已自标"已被取代" | 1,415 | 1 | 2026-10-08 改为：V0_4_SHARDED_RSI 与 RSI_PLAN 直接删（tag `archive-2026-10-08` 上可读）；REHEARSAL 留在原处、只加页首说明，因为 JOURNAL、ROADMAP、modules 与 test_harness_loop 引它的路径 |
| 两份 README（2026-10-08 整理时已重写，只剩 :37 的“遗留代码”一条随本批改；原来点名的 README:9/19/97、README.zh-CN:19 与 smoke/decide/run/demo、hook、evolve/TDE/serve、suite 各段已不在）；DESIGN:133（§6）、:172（§7）、:222（§9）（第一版写的“§5/§9”指错了；这三处与 TECHNOLOGY:15 已于 2026-10-08 改写，TECHNOLOGY:10、:94 仍引 v1 的 `agent/loop.py`、`research/loop.py`，随本批改）；ROADMAP:52/60/297/566；TECHNOLOGY.md:10/15/94；modules 02 的决策学习路径；04/05/07 里研究位、多领域、frontier 的段落 | 改写 | 1/2 | 决策与训练统一成一句："保留 Jev/TDE 兼容的类型化决策契约、决策记录和 controller 训练链（可选 extra `ouroloop[train]`、`[train-jobs]`）；v1 Agent 循环的决策点与冠军发布已移除（tag v0.4.0）；决策标签按血缘选择：拟合可收 env 与 model 标签，阈值只收 env，评测只收 env 与 human。” C4 统一成：“R 递归保留，C4 以跨活动的 R0/R1 比较测量。” (a)、(b) 已于 2026-10-07 确认。第一批的部分已做（2026-10-08）：两份 README、DESIGN、ROADMAP、TECHNOLOGY、TRAINING_HOSTS、modules 00～05、07、08 与 README，加上 02 的日落点名清单（§3c） |
| V0_5_AGENT_RL（717，未采纳）；如果两库被删，再加上 V0_5_METHOD_LIBRARY（860）和 V0_5_EVOLVING_SEARCH（753） | 717～2,330 | 3 | 移入 history。2026-10-08：V0_5_AGENT_RL 已删（tag `archive-2026-10-08` 上可读）；经验库保留，V0_5_EVOLVING_SEARCH 留；V0_5_METHOD_LIBRARY 随第三批删（若方法库删） |

## 3. 分批执行计划

**第 0 步：准备。** 现在就可以在隔离 worktree 里做，但不合入主检出。
1. 打 tag：在 `7f65947`（finance final）打 `v0.4.0`；support 两份 final 评完、H1 算完之后打 `v0.5.0`。
2. 先加以下金标测试并全部通过，再删任何东西：
   - v3 和 v5 preset 的 `harness_settings` 输出，也就是 K 的 harness 表，逐字节一致；
   - finance 和两份 support 冻结计划（TaskEvalPlan、GatePlan、Schedule、FinalizationPlan）的 ref 能载入，哈希不变；
   - 由 v4/v5 配置算出的 RunManifest 负载和 attempt id 不变；
   - W0 和 R0 的策略集 ref 不变；
   - 用 finance 的记录重渲染 report.json，与现有文件逐字节相同。第二批允许的唯一差异是 C2/C3/C4 的说明文字。

   （2026-10-08 用户定，取代本条：不写金标测试，理由见页首。第一批第 1 步改为在删任何东西之前加两组测试，`2dbd935`：`tests/test_record_compat.py` 在 `OUROLOOP_RECORDS_ROOT` 下读四个工作区的 train/dev 结果、运行清单、评分行、抽样的 transcript、session 行与 `.release.json`、release 与对象闭包、`library_stream.json`，读前读后对每个文件求哈希，工作区不缺一个文件、大小与 mtime 不变，缺了工作区就 skip；`tests/test_attempt_behaviour.py` 加 `tests/fixtures/attempt_behaviour_v3.json`，在 v3 preset 的 K 下用脚本化的 actor 跑一次 dev、一次 train 尝试，钉住 actor 收到和答出的内容、transcript、结果字段与评分。两组都先在没改过的 `8434c03` 代码上通过（行为夹具就是在那里录的），此后每个提交都通过。）
3. test_aa_replay 加 skip-if-absent。
4. 每批一个分支：`slim/1`、`slim/2`、`slim/3`。每批单独合并，也能单独 revert。

**第一批：死代码、遗留路径与决策栈隔离（删约 5,500 行框架代码，移入 extra 约 4,400 行；删约 4,800 行测试，约 3,100 行测试随 extra；删约 7,300 行示例与评测数据）。** 对结果没有风险；机械风险集中在热路径解耦、Runtime 薄化和夹具迁移三处。
- **前提**：用户确认 §0 的 (b)（新版：收进子包，不撤回能力）（已确认，2026-10-07）。
- **顺序**：按 import 图从叶子到根。
  1. **热路径解耦**，单独一个提交，行为不变：
     - `open_activity` 改用薄 `Workspace(root).session()`，不再调 `build_runtime/register_builtin_points`（bench.py:422-423）；照写同样的 `session` 行和 `.release.json`，`release.spec` 写冻结常量；
     - rule 类 `worker.stop` 绑定时不构造 `DecisionPoint`（policies.py:594-597）；
     - bench.py:503-511 的 feedback、build、points 导入和 gateway.py:36 的 points 导入改懒导入，DENY 移到 `agent/`；
     - `build_feedback` 只在 K 绑了非 rule 的决策 target 或 `capabilities.training` 为真时才跑，否则写同形状的空值；
     - `AgentResult` 移到 runner，`research/__init__` 改懒导入；
     - 加边界测试：跑 v3、v5 的 bench 路径（`--checks-only`，加假上游上的一次尝试）之后，`sys.modules` 里没有 `ouroloop.train`、`evolve.feedback`、`points`。
  2. **删入口**：旧 CLI 命令（smoke/decide 搬进子包）、demo、serve、suite/prompt/probe、evals、ceiling 跑法、rehearsal 和 ManualProvider（ceiling 跑法与 rehearsal 已于 2026-10-08 提前删除）。
  3. **建子包**：按 §0.1 A、B 把模块移进 `ouroloop/train/`，C 移进 `ouroloop/train/jobs/`，对应测试一起移；`tests/rsi.py` 的 PhraseDecider/PhraseTrainer 改作子包测试的夹具；`trainers/signature.py` 带上 `replay.signature` 的键函数；pyproject 加两个 extra `[train]`、`[train-jobs]`，入口 `python -m ouroloop.train`（命名按 V2_TRAINING_PIPELINE §7 第 7 项，v2 的代码进同一个子包，不再搬第二次）。
  4. **删冠军指针一族和 v1 循环**（§0.1 E）：gate、registry、replay、monitor、plan、harness，pipeline/build/calibrate 的旧半边，runtime 的冠军半边，`backends/http`，编码决策点，v1 Agent 循环与 v1 分支，bench 的 `collect` 阶段，ledger、release、policies 里的冠军遗留。同一提交里：把 `tests/rsi.py` 迁到 v3 的 K；改写 IT-02（见 §2.6）；给 `CompletionProbe` 补直接单测。train 已移进 jobs，删 gate 不受 `train.TrainError` 的影响。
  5. **顺带删**：frontier 臂、task_gate 的经济模式、无调用者的函数、v0.3 兼容代码。
  6. **改注释**：providers/__init__.py:4、claude_code.py:235、anthropic.py:45、openai_compat.py:44、backends/llm.py:1-3、runtime.py:372（随 `[train]` 搬）、ledger.py:19-20 这 7 处 “evaluation only / never training targets (DESIGN invariant 6)” 一类的注释，改为指向新的不变量 6（数据血缘与选择，DESIGN §8），写明闸门与默认值在 v2.0 改（V2_TRAINING_PIPELINE §2.5、§3.8、§7）；`providers/manual.py:87`、`research/loop.py:99` 随本批整删。
- **删的测试**：2.6 中标 1 的各行。
- **文档**：删 DEMO；TRAINING_HOSTS 留（随 jobs）；REHEARSAL、V0_4_SHARDED_RSI、RSI_PLAN 移入 history（2026-10-08 已提前处理：后两份删除，REHEARSAL 留在原处、加页首说明）；README、DESIGN、ROADMAP、TECHNOLOGY、modules/02 统一改成 §2.6 的那句话；02 写进日落规则的点名清单和 §0.2；RESULTS 的 v0.1 Reproduce 改成"在 tag v0.1.0 上"。
- **必须守住的身份**：
  - `RunManifest` 的负载，包括遗留字段 decisions、model_ref、policy，以及 `input_schema_refs=(stop_view,)`；
  - `_host_contract` 里的两个字面量；
  - REGISTRY、BUILTINS、SCHEMAS；
  - W0 里的 `worker.stop` 规则描述符；
  - K 的 `capabilities.training: False` 键；
  - `session` 行与 `.release.json` 里的 `release.spec`（冻结常量）；R 的 `context_ref`（feedback 写同形状空值）；`release.PACKAGE_DATA`。

  （2026-10-08 用户定，放宽：上面这些不再是必须守住的身份，只为保旧哈希而留的，能让死代码一起删时可以删。第一批实际只改了 `release.PACKAGE_DATA`（指向 `train/jobs/hosts/`）；其余照旧，`session` 行与 `.release.json` 照写、`release.spec` 写冻结常量。K 带 `code_version`，第一批改过 `.py`，新活动本来就得到新的 K。）
- **验收**：金标测试全部通过；全量测试全绿，含子包与 jobs 的测试（没有 `TDE_REPO` 时 test_tde_compat 跳过）；边界测试通过；在副本里用 v4 配置跑一次 `--checks-only`，并用假上游跑一次尝试，transcript、results 和 `session` 行与改动前逐字节相同。（2026-10-08 实际验收：金标换成读兼容与尝试行为两组测试，每个提交都通过；边界测试 `tests/test_hot_path_boundary.py` 在新解释器里跑 v3、v5 的 smoke 检查、一次 smoke 尝试、基线与测量，`ouroloop.train` 一个模块都没被加载；默认套件、带 Docker 的全量套件与干净副本都通过，数字见 JOURNAL。“用 v4 配置在 finance 副本上跑 `--checks-only` 逐字节对照”没做：它要真实上游检出，v3、v5 的 checks-only 与尝试路径已由假上游上的边界与行为测试覆盖。）

**第二批：没产生过证据的机制（约 3,300 行框架代码，约 2,600 行测试）。**
- **前提**：用户确认 §0 的 (a)（已确认，2026-10-07）；support 已经结束，因为 api_errors 和 context 都是两臂 R 的输入。
- **顺序**：
  1. 研究位：删活动内编排，抽出 `research/recursion.py` 和 test_recursion。support_h1.py 推广成跨活动的 C4 脚本时，在 H1 旁边并列打印缺失的题数和尝试数：它现在取两臂题目的交集，会悄悄去掉缺尝试的题。这不改变预注册的读法。（2026-10-08：support_h1.py 已删，tag `archive-2026-10-08` 上可读；跨活动 C4 脚本改为新写，这条要求照旧。）
  2. 多领域合并。
  3. 采集批和段延长。
  4. 把 test_cycle 等夹具迁到 `SessionScript`，然后删旧轮次路径和 `marketing_harness_v1`。
  5. 简化 context 和 inspection（`check_label_availability` 保留），删 api_errors。
  6. 预算检查合一，加上 bench 和 cli 的小简化。
  7. 最后单独一个提交，去掉 bench 的版本史注释。
- **删的测试**：2.6 中标 2 的各行。
- **文档**：modules/06 改写成跨活动 C4；改 04/05/07；RESULTS 的 v0.2、v0.3 段落注明"在 tag 上读"；删旧配置。
- **必须守住的身份**：
  - Schedule 的字段：`research_slots`、`collection_batches`；
  - FinalizationPlan 的 `research_campaign_plan_ref` 和 `claims`；
  - TaskEvalPlan 的 `screen_ids` 等字段；
  - v3/v5 preset 的 K 表逐字节一致（由金标测试保证）。

  （2026-10-08：页首的决定同样适用于第二批，理由相同：现有活动都不在 HEAD 上续跑。上面的字段只在能让死代码一起删时才删；要守住的仍是读兼容、尝试行为与资金安全三条。）
- **说明**：R0 的 preflight 和会话文本变化，只影响新活动的 `research_ref`。
- **验收**：金标测试全部通过；finance 的报告重渲染后只在 C2/C3/C4 的说明上有差异；support 两个工作区能载入、能续跑（只跑 `--checks-only`）。

**第三批：v0.5 两库，按 support 的结果决定（0～4,300 行）。** 判定完全照 V0_5_PLAN §3 的预注册读法，不追加读数：
- **H1 成立**：两库都保留，成为下一版的骨干。
- **H1 否定**：两库都删。包括 methods、method_kit、library、fixed、bench 的 learn/library 阶段与 v5 preset、report 的 v0.5 部分、runner/session/drafts 的 v0.5 分支，以及 test_methods、test_library、test_v05、test_v05_phases。
- **H1 不确定**（预期最常见），两库分别看自己的过程读数：
  - 方法库：只有"R 能造出可用的方法"和"学会了何处用"两项都成立才保留；否则删。
  - 经验库：只有"经验库在 train 上有效（D ≥ +2）"和"学习曲线（P ≥ 0.8）"两项都成立才保留；否则删。
  - 保留下来的那一个库作为 preset 开关（降为可选），不进默认值。
- 无论结果如何：参考方法移到 `tests/fixtures/`；V0_5_AGENT_RL 移入 history；support_h1.py 和两个工作区的记录保留到 RESULTS 写完。（2026-10-08：V0_5_AGENT_RL 与 support_h1.py 已删，tag `archive-2026-10-08` 上可读；两个工作区留作本机记录。）
- **例外**：playbook 路径（session.py:1082-1129 的 playbook 注入、`methods.playbook_check`，methods.py:955，约 50 行）即使方法库被删也保留，并改成不依赖 `inputs.methods`。它是跨活动 C4 里 R1 的来源（§0 (a)）。
- **必须守住**：在 tag `v0.5.0` 上，support 两个工作区都能重渲染、能复算 H1。（2026-10-08：H1 不再算，support 两个工作区在 tag `archive-2026-10-08` 上重渲染；HEAD 上只要求读得了它们的 train/dev 记录。）

## 4. 风险与回退

| 风险 | 场景 | 缓解 | 回退 |
|---|---|---|---|
| 改动波及正在跑的活动 | 在主检出里改代码，导致 support 两臂的代码中途变化 | 两份 final 评完、H1 算完之前一律不合入；在 worktree 里准备；遵守"变更在隔离副本里审" | — |
| 身份漂移，导致续跑拒绝或重复付费 | 删了某个记录字段、REGISTRY 目标、preset 数值，或改了 RunManifest 负载 | 第 0 步的金标测试；只让逻辑失效，数据全部保留。保留清单：RunManifest（含遗留字段和 `stop_view`）、TaskEvalPlan、GatePlan、FinalizationPlan、Schedule、CoreManifest、ExperimentProposal、PolicyDescriptor 的字段；K 的 `schema_registry`；`_host_contract` 的字面量；成本项的代码。（2026-10-08 用户定，取代这一格：现有活动都不在 HEAD 上续跑，旧工作区在 tag 上续跑，所以 HEAD 上的身份漂移不会造成续跑拒绝或重复付费。剩下的风险是 HEAD 读不了旧记录，由 `tests/test_record_compat.py` 守住；新活动里的重复付费由资金安全测试守住，`test_paid_run_safety` 等） | revert 该批的合并 |
| 旧工作区在 HEAD 上读不了 | v0.1/v0.2 的 trace 和 CampaignPlan 对象、v0.3 的多领域报告、v0.1–v0.3 的续跑 | 这些工作区都已结束，数字已写进 RESULTS；用 tag v0.1.0/v0.2.0/v0.3.0 读；RESULTS 的链接改指 tag | 检出对应 tag |
| RESULTS 引用的记录丢失 | ceiling/README、tasks.json；本地的 workspace-claude\*（没有提交，aa_replay 也要用） | 这些文件和本地工作区保留，不删；rehearsal 和 ceiling 的工作区留在本机原处，不删、不移（PLAN 规则 12） | — |
| 测试迁移漏掉某项保证 | 把 rsi.py 和 fixture_preset 迁到生产路径时，原来覆盖的续跑、暂停、预算保证没有被新测试覆盖 | 迁移前列出每个被改写测试断言的保证，迁移后逐项核对；先迁移，再删代码 | revert |
| 每次尝试的路径变了 | Runtime 薄化后 `rt.session` 的行为不同 | 在副本里用假上游跑尝试，transcript 和 results 必须逐字节相同；`context_ref` 的变化事先写明。（2026-10-08 实际：`tests/test_attempt_behaviour.py` 钉住假上游上一次尝试的调用、结果与评分，第一批的每个提交都通过；第 2 步另比对过 v3 基线的 session 行、`.release.json`、transcript、results、评分与文件列表，相同） | revert 第一批的第 3 步 |
| 定位变化 | C4 换实现；Jev/TDE 承诺改写为可选 extra | 先由用户确认 §0 的 (a)、(b)（已确认，2026-10-07） | 从 tag 恢复 |
| 保留的子包悄悄烂掉，或与 v2 的真实需求对不上 | 业务决策很可能是结构化动作（发给谁、写哪个字段），不是 yes/no；v2 首选的 Qwen decider 走 systemone 线格式，不走进程内 Decider。真到那时，子包里约一半可能还要重写 | 只留契约和纯函数；子包测试常驻 CI；IT-02 改写后守住 feedback → build → `train_artifact` → 绑定这条链；日落规则（§0.1） | 删掉未被点名的模块 |
| jobs 与 jev 重复，漂移测试抓不到 | `trainers/tde` 的 topology、`max_state_tokens` 与 jev 不一致（§0.1 C）；TDE 胶水已多日没同步 | 登记为已知问题，不在瘦身里修；归属已定为 ouroloop（§0.2 第 12 项），漂移在 v2.1 修（V2_TRAINING_PIPELINE §3.3） | — |

不需要付费验证：所有验收都是离线的，包括测试、金标测试、记录重渲染和 `--checks-only`。（2026-10-08：金标测试换成读兼容与尝试行为两组测试，见页首。）

## 5. 不删的理由

1. **每领域贪心环**（Improvement、improve、cycle、loop 驱动）：它是唯一产出过 held-out 增益的机制。v0.1 +13 点、v0.2 +4 点；finance 的 W\* 62.7% 对 W0 54.7%，结论不确定，但每次尝试的成本降了 22%，且显著。
2. **R 的编码会话、逐题笔记、准入**：finance 装上的候选来自会话；8 条规则里有 7 条一一对应到记过笔记的题；NoteCache 省了 14 次笔记调用；准入的绊线很便宜，作用是防止候选靠论证过关。
3. **dev gate 的核心**（合并对照、确认读数、按族自助）：确认规则是 A/A 回放验证过的。没有确认时，第一版在 20 次 A/A 里接受了 4 次；加上确认后 dev 0/20、test 0/12。`shortfall` 避免重付对照读数。
4. **封存 final 与报告**：C1 只来自读 test 之前封存的计划，坏的臂照样保留；没有它，任何数字都不可信。
5. **预算账、预留、份额、五项硬限额、`_verify_bound`、按 attempt id 续跑**：保护钱。v0.2 的第一次 final 死在预留上，之后才有了份额规则。
6. **Docker worker、runner 摘要、字面量检查**：保证生成的代码不在控制进程里运行，train 的答案进不了共享组件。字面量检查修好后，finance 里 0/8 误拒。
7. **启动检查和冒烟**：在付费之前拒绝漂移、计分契约不符、泄漏、限额不可执行等问题。
8. **benchmark 运行契约**（包括 RunManifest 的遗留字段）：它们在每一个运行哈希里，删掉会让续跑和合并对照匹配不上。（2026-10-08：遗留字段这一半按页首的决定放宽，旧工作区在 tag 上续跑；运行契约本身照留。）
9. **claude_code 提供方和错误分类**：三份当前配置的 actor 和 R 都用它；账户错误会暂停，而不是记成失败。
10. **AutomationBench 适配器核心**：保证测的就是上游测的东西（契约检查、输入审计、按族分层）。
11. **export 和 app.py**：export 是交付物，app.py 证明导出的包能在钉住的镜像和 runner 下重绑并运行同一个 W。
12. **成本项代码和 `control_readings`**：不是因为有用，而是因为已封存的计划里带着它们；只在 preset 和配置层面调整（见 2.3）。
13. **anthropic 和 openai_compat 提供方**：体量小，懒加载，GLM 路线是"推迟，不删"。
14. **aa_replay**：它是确认规则的证据，只需要补一个 skip。
15. **v0.5 两库**：现在不动，按预注册的读法决定（第三批）。
16. **决策/训练链的可复用部分**（`ouroloop[train]`、`[train-jobs]`、原位保留的契约）：用户 2026-10-07 定为必须保留，v2 的决策模型与蒸馏要在它上面扩展（02_EVIDENCE_TRAINING.md:51、:134；TECHNOLOGY.md:15）。保留是对 v2 计划下注，不是已有证据支持（§0.1）。
17. **C4 的判定代码**（`research/recursion.py`）和 playbook 路径：R 递归是 v1 定位的一部分（DESIGN:212、ROADMAP:66）；删掉"系统自己写出下一代 R"这条唯一路径，产品就退化成自动 harness 工程。
