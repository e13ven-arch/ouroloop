# 研究 agent：训练过程的指挥者

> 代码是准绳。本文每条说法都注明它住在哪个文件；凡是设计好但还没有实现的，都单独标出来。
> 状态（2026-09-26）：**LLM 计划器没有实现**。今天仓库里存在的是 `ouroloop/evolve/plan.py` 里的内置规则计划器，以及任何计划器都必须通过的那套准入（`plan.admit`）。下面凡标「设计，未实现」的，是 LLM 计划器接进来时要补的部分，不是现状。

## 0. 现在有什么，没有什么

| 件 | 状态 | 位置 |
|---|---|---|
| 诊断 `diagnose`（只读账本、注册表、撤销名单） | 已实现 | `ouroloop/evolve/plan.py` |
| `Plan` 记录、计划器接口 `plan(rt, d)`、内置规则计划器 | 已实现 | `ouroloop/evolve/plan.py` |
| 准入 `admit`（阈值可达、门能判、headroom、功效、目标、预算、重复） | 已实现 | `ouroloop/evolve/plan.py` |
| 一轮的决策 `decide` 与 `ouroloop plan` 打印 | 已实现 | `ouroloop/evolve/plan.py`、`ouroloop/cli.py:cmd_plan` |
| 计划随卡片和经验日志落盘，门量到的增益与预测并排 | 已实现 | `ouroloop/evolve/pipeline.py:evolve_model`、`Plan.measured` |
| 撤销冷却期（撤销是证据，不是触发器） | **部分实现**：规则挂在冠军指针上，监控回滚之后就不触发（见 §4） | `plan.plan` + `Champion.new_sessions/rows` |
| 研究 agent 能看见的视图 `Workspace` / `Report` / `Candidate` | 已实现（给 spec、recipe、prompt 候选用） | `ouroloop/research/agent.py`、`ouroloop/research/loop.py` |
| LLM 计划器（实现 `plan(rt, d)` 的那一个） | **未实现** | — |
| 指纹式重复锁（点、类型、错误分组签名、规范化改动） | **未实现**，今天是 (kind, change, train_rows) | `plan.repeated` |
| 用门自己的配对 bootstrap 做功效检查 | **未实现**，今天是 `checks.min_detectable_difference` 的闭式近似 | `plan._point_reasons` |
| 失败族（点、错误分组签名、管线），两次失败退休 | **未实现** | — |
| 研究 agent 自身质量的统计（算力、消耗的新鲜测试 session） | **未实现**，原料齐了没人汇总 | 卡片 `train_seconds`、账本 `gate` 事件 |
| 写入范围白名单快照（`evolve/guard.py`，§8） | **未实现** | — |

## 1. 职责

研究 agent 是训练过程的指挥者，也只是指挥者：**它每一轮只产出一份 `Plan` 或者一个弃权的理由，别的什么都不产出。** 它决定这一轮要不要训练、攻哪个目标、候选是什么内容、按什么次序试，以及不值得试完的时候停在哪里。它不执行任何一步——建数据集、训练、校准、过门、部署，全在执行层，由框架跑（`ouroloop/evolve/pipeline.py`）。

LLM 在它里面只产出四样东西：

1. 攻哪个目标（哪个决策点的哪个错误分组）；
2. 候选的**内容**（配方改哪个键、spec 的哪句话、prompt 的哪一段）；
3. 候选之间的**次序**，外加一句写下来的理由；
4. 弃权时的理由。

**它不产出任何决定事情的数字。** 这是设计，**今天没有任何强制点**：`predicted_gain` 和 `cost_minutes` 是 `Plan` 的普通字段（plan.py:148），谁造 Plan 谁填；内置规则计划器在自己的私有函数 `_plan` 里从账本算出它们，但那个函数不在计划器与框架的边界上，边界是 `plan(rt, d) -> (Plan | None, str)`。而准入的功效检查直接吃这个数：`point.mde >= p.per_row(point)`，`per_row = predicted_gain × headroom / test_rows`。也就是说 LLM 计划器一接进来，它写在 `predicted_gain` 里的数字就直接决定这一轮能不能准入。要落地这条边界，得让 `admit` 自己从 `Diagnosis` 重算这两个数，忽略计划器给的值。

另一半是对的：`research/loop.py` 写进经验日志的 `p_pass`、`cost` 只是记录在案，不进任何判定——研究点没有开 act，`Decision.action` 取的是默认值，`basic.py` 因此不按 LLM 的数挑候选。

它存在的理由，是今天这套框架里唯一还没人做的判断：**决定不跑**。门判的是「这个候选好不好」，数据检查判的是「这批数据能不能训」，监控判的是「上线的模型还安不安全」。没有一个在候选存在之前问「这一轮值不值得花这些 GPU 分钟和这批新鲜测试 session」。研究 agent 问的就是这个。

## 2. 它驱动的一轮

一轮 = 数据 → 诊断 → 假设 → 计划 → 准入 → 训练，只有最后一步花 GPU（`plan.py` 模块文档；`plan.decide` 把前五步串起来）。

```
账本 / 注册表 / 撤销名单            （经验日志不在这里：它由准入的 plan.repeated 读）
        │  只读，不训练、不推理、不冻结任何东西
        ▼
   diagnose(rt) ──▶ Diagnosis（每个点的数据、冠军状态、预算、最近几个结论）
        │
        ▼
   plan(rt, d) ──▶ Plan | None（None 时给出理由）      ← 研究 agent 实现的就是这一个函数
        │
        ▼
   admit(rt, p, d, params) ──▶ (是否训练, 理由列表)
        │ 不过：这一轮记为 skipped，理由写明缺什么、补到多少才行
        ▼
   evolve_model(..., plan=p) ──▶ 门（本轮冻结的记录）──▶ 晋升 / 拒绝 / 无结论
        │
        ▼
   经验日志：计划的预测 + 门量到的增益 + 结论（Plan.measured）
```

一轮之上是一代（`ouroloop/generations.py`）：用当前发布跑新 session → 一轮 → 门 → 晋升或不晋升。**植入的坏候选（`plant`）不走计划**，它按定义要过同一道门，所以 `generations.py` 对它直接跳过 `decide`。

**准入今天只挂在 `generations.run` 上。** 另外两条通往训练的路都不经过计划与准入：`ouroloop evolve`（`cli.cmd_evolve` 直接调 `pipeline.evolve_model`）和研究循环本身（`research/loop.py:run_round` 把候选交给 `gate.run` → `recipe_evaluator` → `prepare_model`，全程不碰 `admit`）。所以 §0 里标成「已实现」的 `Workspace` / `Candidate` 那条路，并不过准入。这是「没有计划不训练」这条不变量目前的缺口。

## 3. 契约

### 3.1 它读到的：`Diagnosis`

`plan.diagnose(rt, points)` 只读，不训练、不推理、不冻结任何东西。本轮的测试 session 用 `gate.choose` 算出来（和真冻结记录时同一个函数），所以诊断说的门就是这一轮会遇到的门。

每个决策点（`PointData`）：`default_errors`（路由默认不在正确答案里的有标签行）、`sessions`、`train_rows`、`calibration_sessions`、`error_sessions`（其中至少含一条默认错误的——只有这种 session 能买到阈值，因为只有在默认错的行上模型才会执行）、`test_sessions`、`test_rows`、`headroom`（测试行里的默认错误）、`mde`。

全局（`Diagnosis`）：`champion`（版本、模型、是否被撤销及理由、执行过多少 session、害了多少、其中多少是新数据 `new_sessions` 和 `rows`）、`fresh`（本轮测试 session 是不是新鲜的：不新鲜时门只能 validate，不能晋升）、`min_sessions`、`trained` / `budget`、`last`（上一个真正训练过、非植入的卡片）、`recent`（最近三个候选的结论和理由）、`target` / `reached` / `accepted`、`default_errors` / `at_last_trained`、`minutes_per_row`（`train_seconds` ÷ 60 ÷ 训练行数，取倒着找到的第一张同时记了这两样的卡片）。

### 3.2 `Plan` 记录的字段

| 字段 | 含义 | 谁填 |
|---|---|---|
| `hypothesis` | 一句假设：这一轮为什么值得训练，预期它做到什么 | 计划器 |
| `kind` | 候选类型。今天是 `"recipe"`（改训练配方），之后是 `"spec"` | 计划器 |
| `change` | 改什么。`recipe` 时是超参数字典，过 `pipeline.check_change`：只能是训练器声明的超参数键，且类型与默认值一致；命中 `path_keys`（数据和 run 的路径，如 `replay`、`init_from`）直接报错 | 计划器 |
| `points` | 针对哪些决策点 | 计划器 |
| `predicted_gain` | 预测修好的默认错误比例（`(修对 − 弄错) / 默认错误`，和 `replay.summarize` 的 `fixed_share` 同一把尺） | **框架**，见 3.3 |
| `evidence` | 什么证据能证实或证伪：门在本轮测试 session 上的配对 bootstrap | 计划器 |
| `cost_minutes` | 估计花多少 GPU 分钟 = `minutes_per_row × train_rows`；没训练过时是 `None`（没有 wall time 可折算） | **框架** |
| `data` | 这一轮会训在什么数据上：`train_rows`、`min_test_sessions`、`default_errors`、`headroom` | 框架 |

方法：`per_row(point)` 把预测的比例折算成门量的单位（测试行上的均值，`predicted_gain × headroom / test_rows`）；`to_json()` 落到卡片；`measured(card)` 把预测、门量到的 `fixed_share`（取计划涉及各点里最小的一个）和结论一起写进经验日志；`line()` 给 `ouroloop plan` 打印。

### 3.3 计划器接口，以及内置规则计划器的规则

```python
def plan(rt, d: Diagnosis) -> tuple[Plan | None, str]:      # ouroloop/evolve/plan.py
    """本轮的计划，和理由；或者 None 和不提计划的理由。"""
```

研究 agent 实现的就是这一个函数，读同一份 `Diagnosis`，给出同一种 `Plan`，过同一套 `admit`。内置的规则计划器只是它的一个实现。`decide(rt, points) -> (Plan | None, bool, reasons, Diagnosis)` 把诊断、计划、准入串起来，`ouroloop plan` 打印它。

**规则计划器的规则，逐条按代码的次序**（`plan.plan`）：

1. `d.last` 为空（还没有一个真正训练过、非植入的候选）→ 提计划，理由 `"no candidate trained yet"`。
2. 上一个训练过的卡片没有记默认错误数（`at_last_trained is None`）→ 提计划，理由是无从比较。
3. 默认错误自上一个训练过的候选以来至少翻了一倍（`now and now >= 2 * before`）→ 提计划。这是「数据涨到值得再试一次」。
4. 冠军被撤销，**并且**它执行过的 session 带来了真正的新数据：`d.champion.rows >= MATERIAL * rows`，其中 `MATERIAL = 0.1`，`rows` 是上一个训练过的卡片 `data` 里 `train` 与 `val` 的和 → 提计划。不够则 `None`，理由写明「撤销是证据，不是训练的理由」。
5. 其余 → `None`，理由是 `"default errors {now}, not yet twice the {before} at {version}"`。

计划本身（`_plan`）：`kind="recipe"`、`change={}`（规则计划器什么都不改，它的假设是关于数据的）、`points=list(d.points)`；`predicted_gain = round(max(max(target − reached, 0), _detectable(d)), 4)`。`_detectable` 是本轮各测试集里最容易的那一个能看见的最小比例（`mde × test_rows / headroom` 取最小，再乘 1.05，上限 1.0）。**低于它就不预测**：一个站着的模型越接近目标，剩下的残差越小，再按「离目标还差多少」来预测，就再也没有一轮能准入——看不见的是预测，不是数据。

### 3.4 准入

`admit(rt, p, d, params) -> (bool, reasons)`。每条理由都说明缺什么、补到多少才行；任何一条不过就不训练，这一轮记为 skipped。

今天已实现的检查：

- **阈值可达**（只对 `act` 已打开的点查）：含默认错误的校准 session 数 ≥ `sessions_needed(risk)`，即满足 `cp_upper(0, n, alpha) ≤ risk` 的最小 n（risk 5% 时 59，20% 时 14）；且校准 session 数 ≥ 模板的 `min_cal_sessions`（默认 120，低于它 `calibrate_point` 给每个阶段的阈值都是 `None`）。不够时，这份数据训出来的模型永远不可能执行，训练是可证明的浪费。`risk = 0` 是模板允许、却没有任何 n 能满足的取值，`sessions_needed` 直接报错，这一轮以此为理由不训练，而不是数着 session 死循环。
- **门能判**：该点的测试 session ≥ `min_sessions`（默认 30），否则它不能让候选通过（`gate.decide_points` 把这样的点记进 `thin` 并跳过 bootstrap）。注意它**仍然能否决**：事后监控那条规则在薄厚判断之前先跑，任何点上 `cp_lower > risk` 都会让整个候选被 rejected；`plan._point_reasons` 里那句 "neither a pass nor a block" 就这后半句而言与代码不符。
- **headroom**：测试行里得有默认错误，否则没有东西可修，量不出增益。
- **功效**：测试集能看见的差异要小于计划预测的（今天是 `point.mde < p.per_row(point)`）。
- **判不了的点不否决整轮**：这样的点从 `p.points` 里拿掉并写进理由；只有所有目标点都判不了才拒绝这一轮。一个偶然多出来的第二个点，不能让主点永远训不上。
- **新鲜度**：`d.fresh` 为假时门只能 validate，永远不能晋升。
- **目标已达 / 预算用完**：`reached >= target`、`trained >= budget`（模板默认 `fixed_share 0.5`、`max_trainings 10`）。
- **重复**：`repeated(rt, p)`，见下。

**研究 agent 额外的两条硬检查（设计，未实现）**。之所以只有两条：一个需要四套独立机制来拦同一种已知失败的设计，是过度设计。两条都能被任何人从账本里重算出来，不依赖任何 LLM 的输出。

**（一）重复锁。** 指纹 = (决策点, 候选类型, 错误分组签名, 规范化后的改动)。一个已经失败过的指纹，在这个点攒够新的有标签行之前不可准入。

- 指纹**不包含数据窗口，也不包含父引用**。包含了，这条规则就是死代码：每一轮的数据窗口和父模型都不一样，指纹永远不会重合，锁永远不会响。
- 今天 `plan.repeated` 用的是 (kind, change) 精确相等加上「训练行数不少于上次」。它和设计的差别是：没有点、没有错误分组签名，所以两个针对不同错误分组的同名改动会被当成同一个；而「更多数据就是新假设」这一半已经在。
- 只有门**真的量过候选并拒绝**才算这个假设失败过（`AGAINST = ("rejected",)`）。`inconclusive` 是机器坏了不是候选坏了（`gate.INFRA`），`blocked`、`insufficient_data`、`insufficient_evidence` 都没量到候选，原样重试才是对的；`accepted` 和 `validated` 根本没失败。

**（二）功效。** 取父冠军的**按 session 的增益向量**，用门自己的配对 bootstrap（`gate.paired_bootstrap`，`n_boot` 来自模板）在本轮测试集的规模上跑一遍，取区间半宽，作为这一轮能看见的最小差异。

- 不用闭式的最小可检出差异（`checks.min_detectable_difference(n, discordance=0.15) = (1.96 + 0.84) × sqrt(discordance / n)`）：它是个近似，而且方向是反的——discordance 越低它越小，于是「模型越少执行、越少和默认分歧」反而让检查越松；它算的还是按行的差异，和门真正判定用的按 session 配对 bootstrap 不是一个单位。
- 用门自己的那把尺，检查和判定就一定同源：准入说「看得见」，门就真看得见。

## 4. 撤销冷却期

**一次撤销是冷却期，不是封禁，也不是触发器。**

来历：一次撤销只是冷却，见 `docs/JOURNAL.md` 2026-09-26「没有计划不训练，没有数据不成计划」（撤销即重训，新候选看着几乎同一批数据，连着两次以同样的方式失败）。

规则（`plan.plan` 第 4 条 + `Champion.new_sessions` / `rows`）：

- 冷却期按**该点新增的有标签行**计，**绝不按执行过的 session 数**计。被撤销的模型不再执行，按执行过的 session 计会直接死锁：它永远不会再涨。
- 「新数据」的定义很窄：被撤销的冠军执行过的 session 里，**不在本轮测试集里**、且**晚于上一个训练过的候选的门记录位置**的那些。本轮要留着当测试集的、上一个候选已经训过的，都不算。
- 门槛是 `MATERIAL = 0.1`：这些新数据的行数至少占上一次训练行数的 10%。
- 因此**一次撤销最多换来一次训练**：候选训完之后 `Champion.rows` 归零，这条分支自己闭嘴，不会每一代都重训到预算用光。要再训，得数据自己涨上来（翻倍规则）。

`tests/test_gate_record.py::test_a_revocation_is_evidence_not_a_reason_to_train` 把这条从头到尾走了一遍：2 个 session → 不训练；再攒 40 个 → 其中 30 个成了本轮测试集，只有 12 个是新数据，训一次；训完再问 → 不训练。

**这条规则今天挂在冠军指针上，无人值守的循环里不会触发。** `plan.plan` 第 4 条判的是 `d.champion.revoked`，而 `d.champion` 永远是当前冠军指针指向的那张卡（`_acting` / `_champion`），`revoked` 只有当这张卡本身在撤销名单上才非空。运行时的监控却是撤销与回滚一次做完的：`monitor.watch` 撤销之后立刻调 `_roll_back` → `Registry.rollback`，指针被移到 `last_good`——一个被接受、已部署、**未被撤销**的祖先，没有就移到 None。所以监控撤销之后冠军已经不是被撤销的那个模型，`d.champion.revoked` 为 None，第 4 条不会触发，`acted_sessions` / `harmed` / `rows` 描述的也是回滚到的那个祖先。唯一能命中的是「已撤销但还是冠军」这个残局，而 `watch` 自己在下一个 session 钉住之前就把它修掉。`generations.run` 正是先打完一代 session（每个 session 都跑 `watch`）再调 `decide`。上面那个测试能走通，是因为它直接调 `release.revoke` 而不做回滚，指针仍指着 `m_0001`。

因此它拦住的那次失败（撤销即重训）今天仍然敞着。要落地，这条分支得改成从 `harness/revoked.json` 读最近一次被撤销的模型和它执行过的 session，而不是读冠军指针。

## 5. 失败族（设计，未实现）

一个失败族是 (决策点, 错误分组签名, 管线)。同一族连续失败 2 次就退休，这条线不再提。

- 只有 `rejected` 和**被监控撤销**算失败。`inconclusive`、`failed`、`validated`、`conflict` 和任何基础设施失败都不算——否则一次 GPU 打嗝就能让一条正确的探索方向退休。
- 三元组里带管线，是因为同一个错误分组在模型轨道上失败，不说明在 harness 轨道上也会失败。
- 不做十二值的失败分类学：经验日志已经带着门的结论和理由（`experience.jsonl` 的 `gate`、`reasons`、`verdict`），再加一层人造标签只会多一处要维护的真相。

## 6. 它读什么，它写什么

**读**（都只读）：`Diagnosis` 的全部内容；`Report`（`research/loop.py:build_report`）——每个点在**本轮训练 session** 上的 n / brier / accuracy，以及按 (正确答案, 预测, 错误签名) 分组的错误和几个例子；经验日志（`experience.read`），其中 `details` 里以 `held_out` 开头的键被剥掉（`research/agent.py:Workspace`）；当前的 spec（`ws.spec`）、决策点类型、上下文里的配方和允许的键、任务集结果。

**写**：只有它的计划。计划随候选写进卡片和经验日志（`pipeline.evolve_model`），门给结论之后把预测和量到的增益并排记下来（`Plan.measured` 的 `prediction`）。凭据（HF 写令牌、GPU 主机的 ssh 密钥）它一律没有。

「账本一律不写」是设计边界，不是今天的事实：今天唯一存在的 agent 接口就会写账本——`Workspace.ask`（`research/agent.py`）走 `Runtime.ask_question`，后者往账本追加一条 decision 行；`run_round` 随后为这些决策写 outcome 行（`rt.outcome(..., source="gate")`）。这些行的来源属于 `EVAL_ONLY`，所以永远不会变成训练标签。真正一行都不写的是门记录、注册表、冠军指针和校准库。

`build_report` 取的是 `record.split(...)["train"]`，用的是这条记录自己的 `cal_frac(name)`：**测试 session 不给看，校准 session 也不给看**。校准 session 本来是能学到东西的，之所以也挡掉：按它挑出来的候选，等于是在它自己的阈值将要拟合的那批行上挑的，阈值带的 Clopper-Pearson 界就不再成立（Learn-then-Test 要求候选在看见校准数据之前就固定下来），门报出来的风险会偏乐观。**agent 能看的，就是这一轮能训的。**

今天的边界靠接口划定，不靠隔离：`Workspace` 只公开 `report`、`experience`、`kinds`、`context` 和 `spec` / `point_type` / `ask` / `llm`，runtime 放在私有的 `_rt` 上，agent 与框架在同一个进程里。§8 末尾写定的白名单快照（`evolve/guard.py`）**没有实现**。

计划这条路连接口层的遮挡都没有：§3.3 要研究 agent 实现的是 `plan(rt, d)`，第一个参数就是 runtime，从它能拿到账本、`gate.records` / `gate.choose` 的测试 session id、注册表、校准库和撤销名单。所以 §8 的「看不到」「不能写」「无凭据」在那条路上全都是设计约定，落地要靠 `evolve/guard.py`，或者给计划器一个只读的诊断视图。

## 7. 它自己的质量怎么衡量（设计，未实现）

两个数，**分开报，不做比值**：

1. **每晋升一个冠军花掉的算力**（GPU 分钟，原料是卡片上的 `train_seconds`）；
2. **每晋升一个冠军消耗掉的新鲜测试 session**（原料是账本里的 `gate` 事件：`frozen` 冻结了哪些 `test_sessions`，`used` 把它们用掉了）。

不做比值，是因为这两种资源不可互换：算力可以加卡加钱买，新鲜测试 session 只能等业务跑出来，是这套循环里真正稀缺的东西。除以彼此会让「多烧一倍算力换少用一个 session」看起来像改进。

**这两个数不给 agent 看。** 给它看，它就有了一个可以优化的指标，而优化它的最短路是少提计划、专挑稳的改动，或者干脆不动——正是这个设计要防的那种「看起来很安全」的退化。这两个数是给人看的，用来回答「这个研究 agent 值不值得继续用」。

今天没有任何代码汇总这两个数，原料齐了而已。

## 8. 不做什么

每一条后面是它拦住的那次失败。

**看不到的**

- **看不到测试行、测试 session，也看不到门记录里的 session id。** 看得到，一次性的结论就变成了可以瞄准的目标：候选会向那批 session 收敛，门量到的增益不再是它在新 session 上的表现。
- **看不到校准行。** 看得到，阈值就是在挑候选时已经看过的行上拟合的，Clopper-Pearson 界失效，门报的风险偏乐观。（今天 `build_report` 用 `record.cal_frac(name)`；这个口子的来历见 `docs/JOURNAL.md` 2026-09-26「研究报告不给 agent 看校准 session」。）
- **看不到 held-out 结果。** `Workspace` 剥掉经验日志 `details` 里以 `held_out` 开头的键：按 held-out 挑候选，held-out 就成了选择的一部分，不再是对产出的估计。

**不能写的**

- **不写账本、不写门记录、不写注册表、不写冠军指针、不写校准库，也不持有任何凭据。** Darwin Godel Machine 的 114 号节点删掉了它自己检测器要读的标记；STOP「为了效率」关掉了沙箱开关。**agent 够得着的检查器，迟早会被优化掉。**
- **不定义、不打分、不重标任何结果。** 标签只来自环境观察和人（`ledger.SOURCE_WEIGHTS = {"env": 1.0, "human": 1.0}`）；任何模型和 LLM 的答案，包括门的结论，都是 `EVAL_ONLY`，只用于评测，永不作为训练目标。能改标签，就能把自己的失败改成成功。
- **不能要求门「再看一眼」一份记录。** 一份记录判一个候选，一次。允许重看，就是在同一批 session 上做多重检验，挑运气。

**这三条的落地方式（设计，未实现）。** 今天它们只是接口上的约定（§6），要让约定变成强制，需要三件：

- **隔离**：agent 的 read / write / edit 限定在 `research/` 下，bash 以单独的系统用户或在容器里跑；它读到的只有本轮冻结时导出的训练 session（`research/export/`，只读），测试和校准 session 根本不在文件系统上出现；它没有 HF 写令牌，也没有训练主机的 ssh 密钥——需要凭据的步骤由框架进程代做。它自己那个 agent loop 用独立的 runtime 根目录，所有决策点只记录，session 带 `role=research`，不进 build 也不进门。
- **写入范围按白名单判定**（`evolve/guard.py`）：每次 write / edit / bash 前后给工作区和包目录拍一次快照（逐文件哈希），差异里任何不在白名单上的改动都算越界。它可写的只有自己的计划；`goals.toml`、`points.toml`、`ouroloop.toml`、`gates/`、`models/`、`harness/` 下的冠军文件，以及整个 `ouroloop` 包（研究 agent 自己的代码和 prompt 就在 `ouroloop/research/`）一律不可写。框架自己写的路径（`ledger/`、`sessions/`、`transcripts/`、`models/`、`gates/`、`experience.jsonl`、`research/runs/`、`research/export/`、`harness/runs.jsonl`）不做差异比较，改为检查不变量：账本只能追加（前缀哈希不变），`champion.json` 只能经管线记录过的晋升改变。越界时把白名单之外被改动的文件恢复原样（账本文件永远不恢复），这一轮记为 `violation`：不晋升，也不算证据。
- **门在 agent 够不着的地方跑**：门和晋升在框架启动的独立进程里执行，读的是本轮开始时复制出来的只读代码和门记录；判定之前重算 `ouroloop` 包与门记录的哈希，和框架在本轮开始时留存的值（放在 agent 读写不到的地方）比对，不一致记为 `violation`。

**不能设的**

- **不设 risk、不设测试集大小、不设 `cal_frac`、不设门模板的任何字段。** 模板由人冻结（`gate.freeze_template`），`gate.LIMITS` 规定了每个字段的上下界，任何模板都不能放宽：risk 至多 0.05，`min_sessions` 至少 30，`n_boot` 至少 1000，`suite.min_items` 至少 10；`gate.validate` 还禁止 `cal_frac` 变大（变大就会把已晋升模型训练过的 session 吸进校准集，阈值随之偏乐观）。自己能调门，就等于没有门。
- **不产出任何决定事情的数字。** 没有一个 LLM 给出的数可以当准入输入、终止判据或晋升输入；计划里不写 agent 自己填的预测增益和执行率字段。一个自评 1–10 分的量表，不阈值化就是装饰，阈值化就是让模型给自己发许可证。

**一轮的形状**

- **一份计划只改一个变量、只针对一个目标、只在一条管线里。** 同一轮里改了两处，门的结论就归因不到任何一处。这条**尚未在代码里强制**，而且随仓库发货的规则计划器直接违反它：`_plan` 把 `points=list(d.points)`，一次瞄准全部决策点，`admit` 只把判不了的点剔掉，从不检查只剩一个目标；`change` 那一侧 `pipeline.check_change` 只查键名、路径键和类型，不查改了几个键。所以「一个目标」现在是对 LLM 计划器的要求，不是代码事实。
- **不在同一轮里既改 harness 又训模型。** harness 是发布三元组的一个分量（`release.Release` = 模型 + spec 哈希 + harness 哈希），改了它，session 的状态分布就换了：增益的基线（路由默认在这些状态上的表现）和此前拟合的每一个阈值，都是在另一个分布上得到的。今天代码层面保证的只有「一份记录一个结论」（`research/loop.py:run_round`：第一个用掉记录的候选给出本轮唯一的结论并结束这一轮）；「不混改」这一条**尚未在代码里强制**。
- **不做训练候选的预演档位（rehearsal rung）。** 在一台主机上跑完一次完整训练是分钟级的，预演报出来的执行率和正式跑的是同一个数，多一层只多一处会漂的近似。

## 9. 借鉴与否决

调研对象的行为来自各项目的公开论文与代码，不在本仓库内可核；此处按结论记录，不再重新论证。

| 来源 | 它怎么做 | 我们 |
|---|---|---|
| AI Scientist v1 | 唯一真正的否决是新颖性检查；1–10 的自评分在代码里**没有任何地方被阈值化** | **否决**自评分当判据：不阈值化是装饰，阈值化是让模型给自己发许可证。**借鉴**「在跑之前就有一道否决」这个位置本身 |
| AI Scientist v2 | 去掉了新颖性否决；能杀死一个节点，但杀不死一个想法 | **否决**只能杀节点的停止能力：run6 里连着两个候选以同样的方式失败，杀节点一个都拦不住，要拦的是「这个想法在这批数据上还不该再试」——这正是重复锁和失败族 |
| Agent Laboratory | 没有新颖性、可行性、成本或放弃规则，唯一的刹车是人 | **否决**「靠人看着」：这套循环的前提是无人值守，人只做一件事——冻结门模板 |
| Curie | 没有想法级的否决，但有状态转移的校验器和分层写权限 | **借鉴**分层写权限（第 8 节「不能写的」）和「校验器在 agent 够不着的地方运行」；**否决**「把校验器做厚」代替「决定不跑」 |
| DGM / STOP | DGM 的 114 号节点删掉了自己检测器读的标记；STOP 为效率关掉了沙箱开关 | **借鉴**为唯一的结论：agent 够得着的检查器会被优化掉。所以门在框架里跑、模板由人冻结、agent 无凭据 |
| 预算与早停文献 | 固定预算、事前功效分析、序贯检验与 alpha 分配、successive halving 一类的分层筛选 | **借鉴**固定训练预算（模板 `budget.max_trainings`）和**事前功效分析**（准入的功效检查，但用门自己的 bootstrap，不用闭式近似）；**否决**为候选次序做 Learn-then-Test 式的 alpha 分配——一份记录只给一个结论，没有多重性可花；**否决**分层筛选档位，理由同第 8 节 |

**我们自己在综合过程中否决掉的设计，不要再提**（这一节和上面那张对照表是这些结论的唯一出处，`docs/DESIGN.md` §六只留一句结论并指到这里）：

- 任何由 LLM 产出的数字当准入输入、终止判据或晋升输入；
- 计划里由 agent 填写的预测增益、执行率字段；
- 训练候选的预演档位；
- 候选次序上的 Learn-then-Test 框架；
- 十二值的失败分类学（经验日志已经带着结论）；
- `e_min`、`g0`、`a0` 这类常数——等攒够几轮真实数据再谈，现在定就是凭空定；
- 曾经提出的八条停止规则，砍到只剩上面两条硬检查。

## 10. 与主线的关系

主线见 `docs/DESIGN.md` §一。

这条线的瓶颈不是算力，是**新鲜的测试 session**：一份门记录判一个候选、一次、在它冻结时就定死的那批 session 上。GPU 可以买，新鲜 session 只能等。run6 的教训是这条线最容易断的地方——一次撤销触发一次重训，新候选看着几乎相同的数据以相同的方式失败，一代一代把测试 session 烧光，RSI 曲线上什么也没长出来。

研究 agent 就坐在这个位置上：**没有通过准入的计划，就不训练**（这条不变量今天只在 `generations.run` 里强制，缺口见 §2）。它是这条线上唯一负责「不花」的部件，也因此，衡量它的是它每换来一个冠军花掉了多少算力、烧掉了多少新鲜 session——这两个数分开看，而且不给它自己看。

## 11. 来历：人驱动时的同一个循环（TDE，2026-09-21 ～ 22）

> 依据 TDE 仓库那两天的提交记录和它的 `docs/RESULTS_*.md`。TDE 是另一个项目，下面的数字在本仓库里复算不出来，和 §9 一样按结论记录；唯一能在本仓库复算的是最后那次回放。

当时的分工是人定方向、Claude 做实验设计、找数据缺陷、排队训练。北京时间 09-21 18:48～23:11 和 23:20～次日 09:50 人没有发过消息，这段时间里 Exp 006～011 的方向、排序、数据修复和 OOM 处理都是模型自己定的：循环在人不在场时已经能转。缺的是两样——提方案和判方案是同一个模型，判断没有校准的概率；判断没有留下结构化记录。这两样就是门、准入和账本今天要补的位置。

**诊断归类**是 LLM 计划器「攻哪个目标」那一步真正要做的选择。当时归出六类，每类指向不同的改动：

| 观察 | 归类 | 指向的改动 | 当时的例子 |
|---|---|---|---|
| 指标掉到无 state 控制组的水平 | 结构缺陷 | 改结构 | branch v1 停在 42% |
| 某一类题系统性失败 | 能力缺口 | 扩数据（新来源或规则生成，配污染检查） | hard 层里规则、多跳、时间数值题弱 |
| 某个切片反常地高或低 | 数据缺陷 | 先审计再修数据 | HotpotQA 96% 是支撑段落固定排在前；多跳 54% 是 66% 的 state 超过 448 token 被截断 |
| 超出训练见过的规模 | 外推失败 | 推理端技巧或训练课程 | 训练只见过 ≤12 个候选，全标签集外推差 |
| 在通才混合里占比太小 | 专项差距 | 专精阶段 | typed-decisions 只占混合的 1/13，比 Jev 低 10 个点 |
| 说不清是哪个改动起的作用 | 变量混杂 | 拆分对照 | Exp 010 一次改了底座、state 长度和数据版本 |

**七条教训，以及今天它们落在哪**：

| 教训 | 今天在哪 |
|---|---|
| 起点不等于冠军：Exp 010 在 v0.1 上回退 5 个点当不了冠军，却是 Exp 011 最好的起点 | **没有落地**：`pipeline.parent_ref` 只从冠军（或人在 `[recipe] init_from` 里写死的模型）出发，被拒的候选不会成为下一次的父 |
| 一次只改一处：Exp 010 三个变量一起改，只能再排 Exp 008 / 009 拆开 | §8「一份计划只改一个变量」，**尚未在代码里强制** |
| 先看检出下限再下结论：JevBench hard 只有 111 题，CI 约 ±9 个点，四次运行的 28.8%～34.2% 全在噪声里 | 准入的功效检查（§3.4） |
| 好得反常也要审计：HotpotQA 96% 不是好消息，是位置泄漏 | `evolve/checks.py` 的候选位置倾斜与 13-gram 污染检查 |
| 便宜的先试：推理端分块加锦标赛不用训练就收回一半差距 | spec 候选走离线重放（`evolve/replay.py`，几乎零成本）；prompt 候选先在筛选子集上跑（`evolve/harness.py`） |
| 负结果也有用：Exp 003 五个 arm 持平，省掉了后面对损失函数的投入 | 经验日志（`evolve/experience.py`）与重复锁（§3.4） |
| 资源约束本身就是一个分支：OOM 和「要 24 GB」都改变了实验计划 | `Plan.cost_minutes` 与模板的 `budget.max_trainings` |

**人做过的判断里，哪些能交给决策模型。** 那两天人的判断大多是对给定情况做选择、排序、判断是否继续——「这个方向继续，还是推迟到算力扩充以后」「这批合成数据是否标签可程序验证、模板与标签独立」「步速下降是否超出正常波动」「这项调研略读还是细读」——都能写成 typed question。真正留给人的是三样：目标、权限（主机密码、接受服务条款）和对外说法（开源时数据来源怎么讲）。这和 §9 的结论一致：无人值守的循环里，人只做一件事——冻结门模板。

这些判断里能写成 typed question 的，加上 3 个由结果证实的研究决策，整理成了 `evals/replay_tde/decisions.jsonl`。回放结果：jev-1.13.0 在这 11 条上 11/11 与参考答案一致，参考答案的平均概率 0.87。state 是事后整理的，所以这是上限，不是公平估计（`evals/replay_tde/README.md`）。
