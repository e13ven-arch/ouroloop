# ouroloop 方案 v0.2

> 目标：让第一个决策点真正执行动作，并且一代比一代好。RSI = 数据闭环 + 门闭环。产品入口是契约，研究 agent 驱动模型轨道，stop 是第一个训练并执行动作的决策点，一次发布是（模型、决策 spec、harness）三元组。
> 2026-09-25 起草，按评审修订；承接 `docs/PLAN.md`（v0.1）。本文没有改动的 v0.1 设计继续有效。

## 0. 定义

**RSI = 数据闭环 + 门闭环。**

- 数据闭环：晋升版本在生产中做的决策和观察到的结果，是下一轮训练和评测的证据。
- 门闭环：每个改变 POLICY 的改动，都要通过一个在这一轮开始前冻结的门。POLICY 包括模型权重、温度和阈值、决策点 spec、harness 的 prompt 和能力。

基础 LLM 固定，不训练。

产品入口是契约（§1）。任何 agent 都能通过契约接入；ouroloop 自带的 Python harness 是契约的参考实现，harness 轨道只在这里运行。研究 agent 做三件事：从业务目标出发设定训练目标；驱动写死的管线（build → check → train → calibrate → gate）；管线失败时像编码 agent 一样自己修。先做模型轨道，harness 随模型的变化一起进化。stop（任务级验证）是第一个训练并执行动作的决策点，v0.2 里其余决策点只记录（shadow）。一次发布是（模型版本、决策 spec 版本、harness 版本）三元组，每次只改一个分量。新发布只对新 session 生效。

```
新 session（固定一个三元组）→ 账本：每次 ask 记一行，结果之后按 decision id 关联
      ↑                                                   ↓
晋升（比较交换）← 冻结的门（没用过的新 session）← 研究 agent：定目标 → build → check → train → calibrate
```

**执行规则**（M7 起生效，全文只在这里定义）：模型的回答同时满足下面四条才执行，否则走声明的默认动作。

1. 这个决策点的 `act` 已打开。v0.2 只允许 stop 打开。
2. 标签不同于这次提问所在阶段（§1）的默认动作，并且温度校准后的置信度 ≥ 阈值。后端弃权、最高概率并列时不执行。
3. 阈值条目的键是（作答模型的 `model_id`，spec 哈希，决策点，阶段）。条目只由通过冻结门的晋升写入，并带着那份门记录的哈希。
4. 作答模型就是这个 session 固定的模型，而且不在撤销名单里（M8）。

**冷启动和默认回答者**：模型不执行时，由这个阶段声明的默认回答者作答。它可以是固定标签、一条规则，或者商用 LLM。冷启动时决策点就是这样工作的：规则或商用 LLM 在答，决策模型在 shadow 里跟着答、攒数据，过门之后再在风险预算内接管。默认回答者的答案走 `default` 路线，记下是谁答的，只用于评测，不当训练目标；它出错或弃权时，退回这个阶段的固定标签。

有把握的决策里按 ε 抽一部分走默认动作，并记下倾向分。阈值用 Clopper-Pearson 拟合，风险 5%，按 session 计数：对每个候选切点，每个 session 只计第一条会执行动作的行（标签不同于默认动作，且置信度 ≥ 切点）。切点按固定顺序检验（Learn-then-Test）：从最高的置信度往下，从第一个 session 数够多、可能通过的切点开始，遇到第一个不通过的就停，取停之前最低的那个。不这样做而在所有切点里取最低的通过者，就是在几十个嵌套的检验里挑运气：错误率 6% 的模型在 5% 的预算下，四次里有一次拿到阈值。一条都不错时，至少要 59 个这样的 session 才可能拿到阈值；错 1 条时要 93 个。

**运行位置**

- 开发机上不做模型训练和推理。离线测试只用 mock 和签名小模型。harness 和任务集可以在开发机上跑，但只调用 LLM API；决策点的推理一律发给 GPU 主机上的 serve，还没有 serve 时用 `rt.record` 只记上下文。
- 训练：v0.2 的无人值守路径（M11、M13）只在 GPU 主机上训练。RunPod 临时 pod 只作为手动目标：人启动 pod，再把 ssh 目标写进 `*.local.toml`。数据和 checkpoint 经 Hugging Face 中转，仓库必须是 private，推数据要在 `*.local.toml` 里显式打开。主机、仓库名和密钥只写在被 git 忽略的 `*.local.toml` 里。
- 服务：`ouroloop serve` 在 GPU 主机上运行，装在 TDE 的 venv 里，TDE 加载在进程内；harness 通过 `HttpBackend` 调用。模型一律用 `<hf id>@<commit sha>` 引用：sha 不可改写，分支名和标签可以。
- GPU 主机只有一张卡：训练时 serve 切到 CPU（`TdeBackend(device="cpu")`）或暂停新 session。`ouroloop loop` 每代严格按"采集 → 训练 → 评测"的顺序执行；需要边采集边训练时，训练放到 RunPod 上。
- build、门和注册表在账本所在的机器上运行，评测时的推理也走 serve。

**起点**：v0.1 的机制都在，但没有一个决策点能真正执行动作：配置打不开执行开关，阈值不区分模型版本，晋升只改内存，门参数每次从 TOML 重新读取。现有 stop 记录只有 11 条，全部是 yes 标签（只有最后状态被打上标签），没有 no。这些问题逐条归到下面的里程碑里。

## 1. 契约

四条规则：

1. **一个入口**：模型的每个回答都经过 `ask(point, state, candidates)`。标签集合是封闭的，在决策 spec 里声明，模型不执行时的默认动作也在那里声明。一个决策点可以按阶段声明多个默认动作，比如 stop 的 `per_turn = "no"`（每轮结束后）和 `on_finish = "yes"`（agent 想结束时）。调用时只能选一个已声明的阶段（`phase=`），不能传任意标签。每个阶段的默认回答者（固定标签、规则或商用 LLM，见 §0）也在 spec 里声明。
2. **规则可以否决模型，模型不能否决规则**：任何工具调用前先过黑名单；有 checker 时，stop 由 checker 的结论决定。规则的结论通过 `veto=` 记一行，路线为 `rule`；模型照常作答，但只记录。
3. **只追加的账本**：每次 ask 记一行，无论走哪条路线（`act`、`default`、`shadow`、`rule`、`error`），都带原始上下文、模型版本、路线、阶段和默认动作。结果是之后的事件，按 decision id 关联。
4. **只有环境和人的结果能当训练目标**：任何模型或 LLM 的回答，以及由它们推出的标签（包括门的结论），都只用于评测。

```python
s = client.session(task="lead-1042", held_out=False)                  # 固定这个 session 的发布三元组
d = s.ask("stop", state=view, candidates=["yes", "no"], phase="per_turn")
if d.action == "yes":
    finish()
client.outcome(d.id, "no", source="env")                                # 结果可以几天后才到
```

两种接入方式：

| 方式 | 怎么接 | 能进化什么 |
|---|---|---|
| 契约接入 | 已有的 agent 通过 SDK（Python）或 HTTP（`ouroloop serve`）调用 `ask` 和 `outcome` | 模型轨道：决策模型、温度和阈值、决策点 spec 的措辞和标准（不能增删标签：增删标签要配上对应的动作，属于 harness 轨道） |
| 用 ouroloop 的 harness | 内置的 agent loop 和决策点 | 模型轨道，加上 harness 轨道：prompt、工具、skill |

## 2. 里程碑

| 里程碑 | 目标 | 依赖 |
|---|---|---|
| M6 标签和数据 | 在新工作区里开始采集带探针标签的 stop 数据，后台持续进行 | 无 |
| M7 能执行，而且安全 | 决策点第一次能按阈值执行动作，出错和没把握时走默认动作 | 无 |
| M8 冻结的门 | 所有晋升走同一个事先冻结的门，每次用新 session，晋升持久化 | M7 |
| M9 stop 纵切 | 经 ouroloop 训练的 stop 模型在新 session 上执行动作 | M6、M7、M8 |
| M10 契约优先接入 | 非编码 agent 只通过契约接入模型轨道 | M7～M9 |
| M11 研究 agent | 定目标、驱动管线、修失败，写入范围由结果强制 | M8、M9 |
| M12 harness 随模型进化 | 候选对应动作，发布三元组一次只改一个分量 | M9、M11 |
| M13 无人值守多代运行 | 自动执行比例逐代上升，实际风险 ≤ 5% | M6～M12 |

M6 最先做。它不需要执行、校准或门，而采集是整个计划里等得最久的一步；M7、M8 和 M9 的训练路径在采集期间并行完成。M6～M13 全部通过验收，才算 v0.2 完成。

**session 预算**（写进门模板）

| 用途 | 新 session |
|---|---|
| M9 采集 | 约 450 个（非 held-out 约 330 个）；校准 session 到 120 个之后才打开 act |
| 每次过门 | 非 held-out 至少 30 个（`min_sessions = 30`）；同期的 held-out session 并列报告 |
| M9 执行对照 | act 组、shadow 组各 ≥ 100 个 |
| M11 过门 | 约 40 个 |
| M12 prompt 过门 | 30 个任务 × repeats × 2（冠军和挑战者都重跑） |
| M13 每代 | 约 40 个新 session，加上 act 组的 session |

测试集是上一份门记录冻结之后开始、从未过门的非 held-out session。M9 打开 act 之后跑出的 session 就是 M11 的新测试集，M11 晋升之后的 session 留给 M12 用。合计超过 1,000 次任务集运行，一个人做要几周，所以采集最先开始。

### M6 标签和数据

**目标**：stop 的每次提问都有一个与实际动作无关的环境标签，yes 和 no 两种都有；数据带上后面各里程碑要用的字段。

**改动**

- 探针：`agent/suite.py` `run_task` 给 agent 配一个探针。每次 stop 提问时，探针给工作目录拍一份快照（不含隐藏测试）；会话结束后，再给每份快照写入隐藏测试并运行检查，结果就是这次 stop 决策的 `env` 标签。
  - 目前没有 checker 时，只有最后一次改动之后的 stop 决策会在结束时得到标签（`agent/loop.py` `_finish` 第 308～316 行）。被发回的决策没有标签，所以"没做完"那一侧永远没有结果。
  - 探针不是 checker：StopGuard、view 和 agent 都看不到它的结果。
- view：`points/__init__.py` `stop_view` 去掉 `verification` 一行，`view_version` 升到 2。有 checker 时由 checker 决定（规则 2），这一行等于把标签写进了 view；模型要学的是没有 checker 时的判断。build 会按原始上下文重新渲染，所以改动自动生效。
- `ouroloop/stop.py` `StopGuard`：从 `agent/loop.py` `_pushback` 抽出来，顺序是 checker → 模型 → 发回（最多 2 次）→ 轮数上限。它在两个阶段提问：`per_turn`（每轮结束后，默认 no）和 `on_finish`（agent 想结束时，默认 yes）。M6 里两个阶段都只记录，用 `rt.record`，不调用模型。编码 agent 和 M10 的示例共用它。
- 阶段：决策点定义里声明各阶段的默认动作；`Runtime.ask` 和 `Runtime.record` 接受 `phase=`，每行记下阶段和默认动作。
- session 事件：新增 `Runtime.session(meta)`，写一条 session 事件。`Agent` 接受 `session_meta`，因为它目前自己生成 session id（`agent/loop.py` 第 86 行），拿不到任务信息。`run_task` 传入 `{task, held_out}`，`build_agent_factory` 补上档位，`SuiteRunner` 原样透传。
- 任务集：`evals/tasks` 从 10 个扩到约 30 个，其中约 8 个 held-out。每个任务都要通过 `ouroloop suite --validate`（原样不通过，参考答案全部通过）。
- 采集：用新工作区，不配 verify 命令。除了强档位，还要用弱档位和低轮数上限跑，才会有"没做完"的标签。

**验收**

- 离线（`tests/test_probe.py`）：
  - 每个 stop 决策都有探针标签，包括被发回的。
  - 运行期间，工作目录里没有隐藏测试。
  - view 里没有 checker 的结论。
  - session 事件带 task、held_out 和档位，每行带阶段和默认动作。
- 真实：在新工作区里后台开始采集，前 50 个 session 里 yes 和 no 两种标签都有。

**依赖**：无。

### M7 能执行，而且安全

**目标**：决策点第一次能按执行规则（§0）执行动作；出错或没把握时，一律走声明的默认动作。

**改动：配置和策略**

- `config.py` `build_runtime`：`[points.X]` 只读 `backend`、`define`、`act`、`explore` 四个键，其他键（包括 `risk` 和 `default`）一律报错；v0.2 里 stop 以外的决策点设 `act = true` 也报错。目前只读 `backend` 和 `define`（第 102、109～115 行），任何配置都没法让决策点执行动作。风险只来自门模板（M8），默认动作只在决策点定义里声明。
- `policy.py`：三种模式收成一个 `act` 开关，删掉 `gate` 模式（§4）、`Policy.mode` 和 `conservative`。
  - 路线分为 `act`、`default`、`shadow`、`rule`、`error` 五种。
  - shadow 决策点一律执行声明的默认动作，`route_explore` 和 `compact_explore` 都设为 0。
- 新增 `Decision.acted`，用它替换按字符串比较路线的地方：`agent/loop.py` 第 161、186、233～236、262、291 行，`adapters/claude_code.py` 第 113～118 行，以及 `Runtime.ask_question` 里的 `route="auto"`（`runtime.py` 第 104 行）。
- LLM 兜底改成声明的默认回答者：去掉 `Policy.fallback`、`Runtime.fallback` 和顶层的 `fallback` 配置（`config.py` 第 108 行），把 `runtime.py` 第 64～70 行的逻辑改写为：每个阶段在 spec 里声明默认回答者 `default = "<标签>" | "rule:<模块:函数>" | "llm:<后端名>"`。
  - 目前的问题不在于 LLM 作答，而在于它没有声明、没有记下是谁答的，出错时还会放行。改写后，LLM 默认回答者的答案走 `default` 路线，带上回答者；解析失败、超时或弃权时退回这个阶段的固定标签。
  - 它的答案和已有的 escalation 一样只用于评测，永远不当训练目标。
  - 冷启动就靠它：规则或商用 LLM 在答，决策模型在 shadow 里跟着答，过门并拿到阈值之后才接管。
- 研究决策点：`ask_question` 也按执行规则处理。`research.pick_target` 和 `research.worth_trying` 没有打开，路线为 `shadow`，动作取默认值：
  - 目标取最大的错误分组（M11 起改取 `objective.json`）。
  - worth_trying 默认 yes，预算内的候选都试。
  - 目前研究循环直接按 judge 的回答选目标，并按 `p_pass` 给候选排序（`research/basic.py` 第 87～90、213～221 行），等于有两个由 LLM 回答的决策点在执行动作。

**改动：阈值和校准**

- `policy.fit_threshold` 和 `evolve/calibrate.py` `calibrate_point`（第 46～51 行）：
  - 按执行规则按 session 计数。目前按行计数，同一个 session 的几十行被当成独立样本，风险上界偏乐观。
  - 按阶段分开拟合，只用标签不同于默认动作的行，因为只有这些行执行后会改变行为。现在所有行混在一起拟合，大量本来就等于默认动作的"每轮判 no"会让阈值几乎约束不到真正结束会话的那些 yes。
- 校准按模型存储：`CalibrationStore` 按（`model_id`，spec 哈希，决策点）存温度，阈值再按阶段分开。
  - `model_id` 就是后端作答时报告的 `Answer.model`。训练结果和模型卡上都加 `model_id`；晋升时向候选后端发一次探测请求，要求两者一致。目前晋升按 `cand.version`（如 `m_0003`）存校准，后端报告的却是 `signature:m_0003` 或 `tde:<目录名>`，两边的键永远对不上。
  - `Runtime.temperature`、`replay.predict` 的默认路径（`replay.py` 第 65～66 行）和 `Runtime.set_calibration` 都改用这个键。`set_calibration` 不再直接写 `policy.threshold`。
  - 目前校准按决策点存（`runtime.py` 第 22 行），注册时直接写进 policy（第 36～38 行）；`registry.rollback`（第 49～56 行）只回退冠军指针。改为按模型存储后，回滚会自动带回旧阈值。换了模型、spec 或 view，旧阈值都作废，决策点走默认动作，直到重新校准并过门。
  - 旧格式（按决策点存）的条目一律丢弃，不做迁移。没有门记录哈希的阈值不能让任何决策点执行，所以 M8 合入之前，没有决策点会执行动作；验收用例用测试夹具写入阈值。
- `cli.py` `cmd_calibrate`（第 154～165 行）目前绕过门直接写入阈值，改成只报告、不写入。

**改动：出错走默认动作**

- `Runtime.ask` 把渲染、提问、后端调用、校准和路由整个包起来。任何一步抛出异常，都记一行 `error` 路线（带原始 state），执行默认动作。
- 出错放行的三处：
  - LLM 的输出解析失败时，返回均匀分布（`backends/llm.py` 第 48～49 行）。
  - `types.normalize` 在概率缺失或全为 0 时也返回均匀分布（第 88～89 行）。HTTP、TDE 和签名后端都调用它。
  - `Answer.label` 遇到平局取第一个标签（第 70～71 行），也就是 yes。

  改为：前两种情况设置 `Answer.abstained`，走默认动作；最高概率并列时也不执行。

**改动：规则和标签来源**

- 黑名单：
  - 目前 `agent/loop.py` 只在 `gated_tools` 里的工具调用前查黑名单（第 209、223～228 行），拒绝时也不记账。改为每次工具调用前都先查；注册了 tool_gate 时，用 `veto="no"` 调 `rt.ask`，记一行 `rule`。
  - Claude Code 适配器的黑名单分支（第 104～106 行）和 StopGuard 的 checker 分支，也经 `veto` 记录。
  - 删掉 `_allowed` 里 gate 模式那条路径。tool_gate 在 v0.2 保持 shadow。
- `adapters/claude_code.py` `_pre`：删掉返回 `allow` 的分支（第 115～116 行），只可能返回黑名单的 `deny`，或者不返回。
  - 目前在 live 模式下，模型放行的调用会绕过用户自己的权限规则，并在 `_post`（第 125～126 行）里被记成 `human` 的 yes，等于拿模型自己的回答当人的标签。
  - 被用户自己的权限规则放行的调用，照旧记为 `human`。
- `ledger.py`：
  - `SOURCE_WEIGHTS` 只保留 `env` 和 `human`；`open_llm` 移入 `EVAL_ONLY`，另加一个只用于评测的来源 `gate`。
  - `Row.target` 不再使用 escalation。
  - `research/loop.py` 第 95、101 行把门的结论记成默认来源 `env`，改记为 `gate`。
- 文档：删掉 `README.md` 第 11 行括号里的半权重说明；改掉 `providers/__init__.py`（第 4、28 行）和 `providers/openai_compat.py`（第 44 行）里"`open_llm` 可以当训练标签"的注释。

**验收（离线）**：`tests/test_act.py`

- 执行开关和阈值：
  - stop 设 `act = true`，并有为模型 A 拟合、带门记录哈希的阈值：A 作答时执行，B 作答时不执行。
  - `view_version` 从 1 升到 2 后，为 v1 拟合的阈值不再生效。
  - 回滚后，温度和阈值恢复为上一个冠军的值。
  - 旧格式 `calibration.json` 里的阈值、没有门记录哈希的阈值，都不能让决策点执行。
- 按 session 计数：
  - 来自 5 个 session 的 100 行拟合出的阈值，不比 100 个独立 session 拟合出的宽松。
  - 59 个 session 全对时有阈值，58 个时没有。
  - 校准集里 95% 是正确的 no，而 yes 里有 30% 是错的：`per_turn`（默认 no）阶段拿不到阈值。
- 出错走默认动作：LLM 解析失败、后端抛错、HTTP 后端返回空概率、渲染抛错时都走默认动作，并在账本里各记一行。
- 配置和标签来源：
  - `risk = 0.2`、tool_gate 的 `act = true`、顶层的 `fallback` 都在启动时报错。
  - 默认回答者是 LLM 时：它的答案成为动作，账本里这一行的路线是 `default`，带上回答者；它不出现在 `training_rows` 里；LLM 解析失败时退回固定标签。
  - `ouroloop calibrate` 不修改任何文件。
  - `open_llm` 的 escalation 和 `gate` 来源的结果都不出现在 `training_rows` 里。
  - judge 对某个候选答 no，这个候选照样被试，那一行的路线是 `shadow`。
- 规则：
  - `gated_tools=()` 时，黑名单里的 bash 命令照样被拦下，并留下一行 `rule`。
  - 适配器在任何路线下都不返回 `allow`。
- 改写 `test_core`、`test_loop`、`test_agent`（7 处 `Policy(mode="auto")`）和 `test_integrations` 中依赖旧模式、兜底或 open_llm 权重的用例。

**依赖**：无。

### M8 冻结的门

**目标**：所有晋升都走同一个在这一轮开始前冻结的门；每次过门都用没被用过的 session；晋升持久化，并以比较交换的方式写入；运行中的 session 固定在自己的发布三元组上。

**改动：门记录**

- 门模板：由人用 `ouroloop gate freeze` 冻结。内容包括：风险（0.05）、`n_boot`、`min_sessions`（30 个非 held-out session）、`cal_frac`（stop 取 0.4）、`min_cal_sessions`（120）、检查阈值、测试集规则，以及每代的 session 预算。
  - 目前门参数每次都从 TOML 重新读取（`cli.py` 第 147 行，`pipeline.py` 第 135～138 行，`config.py` 第 157、161、170 行），操作者或研究 agent 可以放宽 `min_items` 再试一次。冻结之后，TOML 里出现 `[research] min_items`、`[suite] min_items` 或 `[checks]` 都直接报错。`examples/ouroloop.toml` 里的 `[checks]` 也删掉。
  - `gate.decide_points` 的 `min_items` 改为 `min_sessions`，按 `len(set(groups))` 计数；目前按条数计（第 51 行）。
  - `evolve/checks.py` 的默认值 `min_train=1000`、`max_mde=0.03` 会让任何小账本都判为 `insufficient_data`。模板里改成单个决策点能达到的值（200 和 0.10）。真正的保护是门，检查只是为了不白跑训练。
- 门记录：每轮开始时，框架从模板生成 `gates/<round>.json`，内容包括：
  - 模板的哈希；
  - 代码版本：整个 `ouroloop` 包的哈希（或者要求工作树干净的 git commit），加上 serve 报告的推理代码版本；
  - 这一轮的测试 session id；
  - M11 起还有目标文件的哈希。
- 测试集和判定：
  - 测试 session 在冻结时选定：上一份门记录冻结之后开始、从未过门的非 held-out session。第一份记录取最新的一批。
  - 新 session 不够 `min_sessions` 时，只能在旧 session 上验证，结果记为 `validated`，不能自动晋升。
  - 门仍用按 session 重抽的配对 bootstrap。模型候选的判定规则是：它服务的决策点里至少一个显著改善，并且没有一个显著变差。
  - held-out 任务的 session 不进训练和校准，门只看非 held-out session；同期 held-out 任务的 Brier 在卡上并列报告，不参与判定。
- 记录什么时候算用掉：
  - 一旦对测试 session 发出第一个推理请求，这份记录和这批 session 就算用掉，即使之后的结果是 `inconclusive`。
  - 在那之前失败（checks 挡住、`insufficient_data`、训练报错）不消耗记录，重跑沿用同一份。
  - 门判为 `rejected` 就结束这一轮，下一轮用新 session 再试。
  - 测试集的预测和中间指标只写在研究 agent 读不到的地方，门给出结论之前不给它看。
- 统一入口 `gate.run(record, ev)`：
  - `evolve_model`，以及 `research/loop.py` `run_round` 里的 spec、prompt、recipe 三类候选，全部经过它。目前 `run_round` 用自己的 `_gate` 判定并直接晋升（第 45～53、81～85 行）。在接入 `gate.run` 之前，`run_round` 只评估、只记录，不调用 `apply`。
  - `Registry.seen_sessions()` 从门记录汇总，不从模型卡汇总，因为 spec 和 prompt 的门不写模型卡。
  - spec 候选晋升时，同时为（当前模型，新 spec）重新校准，一起写入。
- prompt 候选过门时，冠军和挑战者在同一批里都重新跑，记下 session id。目前 `SuiteRunner` 按（prompt、agent、任务、第几次）缓存运行结果，冠军的运行跨轮复用（`evolve/harness.py` 第 7、31、39～48 行）；以后缓存只用于筛选。
- 基础设施失败算 `inconclusive`，包括训练报错、ssh、HF 和网络故障。它不算负面证据，在经验记录里单独标出。

**改动：晋升和发布**

- 注册表：
  - 评估之前先写一张状态为 `candidate` 的卡；目前卡要等门判完才写（`pipeline.py` 第 141 行）。卡上记下 `model_id`、门记录的哈希和测试 session id。
  - `Registry.promote(version, expected_parent)` 做比较交换：冠军指针已经不是 `card.parent` 时，晋升失败。
- 晋升持久化：目前 `promote_model` 只在内存里把决策点切到 champion（`pipeline.py` 第 91～93 行），重启后映射只看 TOML。改为冠军卡记下它服务哪些决策点，`build_runtime` 从注册表恢复映射，TOML 只提供初始映射。
- 发布三元组：新增 `ouroloop/release.py` `Release(model, spec, harness)`：
  - model 是 `model_id`（M9 起为 `<hf id>@<sha>`）；
  - spec 是已注册决策点 spec 的哈希，含 `view_version`（M10 起，契约接入改用 `points.toml` 的哈希）；
  - harness 是 prompt、工具和 StopGuard 配置的哈希。
- session 固定：每次开 session，`Runtime.session(meta)` 都重新读取 `models/champion.json` 和 `harness/calibration.json`，把三元组记进 session 事件；这个 session 之后的 ask 都用三元组里的模型和校准。目前 serve 进程只在启动时读一次（`config.py` 第 96～99 行，`ledger.py` 第 177 行），别的进程做了晋升，要重启 serve 才生效。
- 撤销名单 `harness/revoked.json`：每次 ask 都查。session 固定的模型被回滚或撤销，或者 act 被关掉时，这个 session 之后的决策走默认动作（照常记录），不会换成新模型。回滚时，被退下的版本会写进名单。

**验收（离线）**：`tests/test_gate_record.py`

- 冻结后在 TOML 里放宽 `[research] min_items` 或 `[checks]`：启动报错，门的判定不变。
- 在同一批测试 session 上第二次过门：结果为 `validated`，冠军不变。
- `run_round` 里的 spec、prompt、recipe 候选都走门记录；没有未用过的记录时，不晋升任何候选；prompt 过门用到的冠军运行都是本轮新跑的。
- 两个候选出自同一个父版本时，第二个的晋升因比较交换失败。
- 晋升之后：
  - 重建 runtime，映射和阈值来自注册表；
  - 长时间运行的 serve 不用重启，新开的 session 就用新模型；
  - 晋升时正在运行的 session 继续用旧模型。
- 回滚后，固定在旧版本上的运行中 session 不再执行动作。
- 训练器在测试阶段之前抛出 `TrainError`：结果为 `inconclusive`，记录和测试 session 都没用掉。测试推理开始之后才失败：两者都算用掉。
- 卡上有 `model_id`、门记录的哈希和测试 session id；session 事件里三个分量都有，模型晋升只改变 model 分量。

**依赖**：M7。

### M9 stop 纵切：经 ouroloop 训练的模型执行动作

**目标**：stop 模型在 GPU 主机上经 ouroloop 训练，通过冻结的门，由 serve 按 `<hf id>@<sha>` 加载，在新 session 上执行动作。

**改动：数据**

- `evolve/build.py` `to_example`（第 26 行）：候选改按 `presented(Random(decision id))` 的顺序写出，与 `predict` 和线上保持一致。目前按定义顺序写，带来两个问题：位置偏斜检查实际量的是标签不平衡，会挡住任何标签不平衡的 choice 决策点；训练和线上看到的候选顺序也不一样。
- 切分：build 从训练 session 里按哈希再切出一份 `val`，只交给训练器选 checkpoint。ouroloop 的 calibration 和 test 既不发给训练器，也不推到 HF。
  - 目前 TDE 按 `calibration.jsonl` 上的 NLL 选 `best.pt`（`tde/train.py` 第 349～357 行），而 ouroloop 拟合温度和阈值用的也是这份数据，这样拟合出的风险上界偏乐观。
  - `prepare_job` 还会把 `test.jsonl` 拷到训练机器上（`evolve/train.py` 第 105～107 行）。
- 样本权重：TDE 训练器忽略每条样本的权重。改为训练器声明 `supports_weights`；不支持时，build 只收权重为 1.0 的行，不把弱标签当强标签训练。

**改动：训练**

- `evolve/train.py` `prepare_job`（第 108～117 行）：`out_dir` 也要做 `expanduser`。目前 `remote_repo = "~/tde"` 时，checkpoint 会写进一个名字就叫 `~` 的目录，因为 TDE 的 `tde/train.py` 第 239 行直接用 `Path(out_dir)`。
- 新增 `HubTdeTrainer`：
  1. 把 train 和 val 推到 private 的 HF 数据集仓库。
  2. 通过 ssh 在 GPU 主机上运行 job 脚本：从 HF 下载数据和父模型（TDE 的 `init_from` 只认本地目录），训练，导出，再上传到 private 的模型仓库，记下 HF 返回的 commit sha。
  3. `TrainResult.backend` 为 `{"type": "http", "model": "<hf id>@<sha>"}`，卡上的 `model_id` 与之相同。
- 删掉 `tde_remote` 后端和 `RemoteTdeTrainer`，由 `HubTdeTrainer` 取代。`tde_remote` 每次决策都要做 ssh 和 rsync，并重新加载整个模型（第 287、306～324 行），而 `build_runtime` 会把它装成冠军（`config.py` 第 98～99 行）。
- `local_tde` 训练器只能在 GPU 主机上用，在其他机器上选它就报错；它的产物同样上传到 HF，由 serve 加载。
  - `TdeBackend` 在 ouroloop 的 venv 里 import `tde`（`backends/tde.py` 第 20 行），但那个 venv 里没装 tde。
  - 以后开发机上的 ouroloop 不再 import tde。

**改动：服务和执行**

- `serve.py` `/decide` 接受 `model` 参数：
  - 只允许 `*.local.toml` 里配置的模型仓库，只按 sha 加载。
  - 按 LRU 最多驻留 2 个版本，超出时按需重新加载，不拒绝请求。
  - `HttpBackend` 加上 `model` 参数。
- `Answer.model` 改用 `<hf id>@<sha>`，目前是 `tde:<目录名>`。它与 session 固定的模型不一致时，按 `error` 路线走默认动作。没晋升的版本可以加载来评测，但它没有阈值，永远不会执行。
- StopGuard 打开执行：
  - `per_turn` 阶段有把握地判 yes，就结束会话，防止无限探索。
  - `on_finish` 阶段有把握地判 no，就把 agent 发回去继续干，最多 2 次。
  - 有 checker 时，模型推翻不了 checker 的结论。
- 执行后监控：对每个模型版本，持续计算执行类决策的实际错误率，每个 session 只计第一条执行的行，用探针标签判对错。Clopper-Pearson 下界超过 5% 时，自动把这个版本写进撤销名单，并通知人。

**验收（离线）**：`tests/test_stop_slice.py`，用签名小模型，ssh 和 HF 都是假的

- build 写出的候选顺序与 `predict` 一致；80% 的决策选同一个标签的 choice 决策点，不再被位置检查挡住。
- `remote_repo` 以 `~` 开头时，生成的配置里 `out_dir` 是绝对路径。
- 推给训练器和 HF 的数据里，没有 calibration 和 test 的 session；不支持权重的训练器拿不到权重小于 1 的样本。
- 在假 HF 上走完全流程：推数据 → 训练 → 上传 → 卡上记下 `<hf id>@<sha>` → serve 按 sha 加载 → 新 session 用新模型。配置之外的模型仓库被 serve 拒绝。
- StopGuard：发回不超过 2 次，轮数上限生效；有把握的 yes 能在中途结束会话；有 checker 时，模型推翻不了它。
- 执行错误超出预算的版本被自动撤销，之后它的 session 都走默认动作。

**验收（真实，需 GPU 主机）**

- M6 的采集达到约 450 个 session（非 held-out 约 330 个），stop 的 yes 和 no 两种标签都有。
- `ouroloop evolve` 从通用 TDE checkpoint 出发，在 GPU 主机上训练 stop，数据和 checkpoint 都经过 HF。
  - 门在新 session 上判为 `accepted`，卡上并列报告 held-out 任务的 Brier。
  - 校准 session 至少 120 个，`per_turn` 阶段的 yes 拿到阈值；`on_finish` 阶段的 no 如果拿不到阈值，就不执行。
  - 门拒绝时，再采至少 40 个新 session 重试，最多 3 轮。3 轮都被拒，就停下来复盘 stop 的 view 和标签，不进入 M11。
- 设置 `[points.stop] act = true`。act 组和 shadow 组各跑至少 100 个新 session，按任务配对，做按任务重抽的 bootstrap：
  - 任务分数差（act 减 shadow）的 95% 下界 ≥ −0.05；平均轮数差的 95% 上界 ≤ 0。
  - 另外报告：风险 5% 下的自动执行比例、各阶段按 session 计的实际风险上界、撞到轮数上限的比例。

**依赖**：M6、M7、M8。

### M10 契约优先接入

**目标**：已有的 agent 不用 ouroloop 的 harness，只通过契约就能接入模型轨道。用一个非编码的示例来证明。

**改动**

- `ouroloop/contract.py`：
  - 决策 spec 文件 `points.toml`：写明每个决策点的类型、问题、封闭的标签集合、各阶段的默认动作，以及哪些结果来源能当训练目标（比如 CRM 回调记为 `env`，人工复核记为 `human`）。这个文件的哈希就是决策 spec 的版本。
  - 接口：`Client`（进程内或 HTTP）、`Client.session(meta)`、`Session.ask(point, state, candidates=None, phase=None, veto=None)`、`Client.outcome(decision, label, source)`。候选必须是已声明标签的子集，阶段必须已经声明，否则报错。
  - SDK 在客户端生成 decision id。连不上 serve 或者超时的时候，执行默认动作，把这一行（路线为 `error`）写进本地缓冲；恢复连接后补发到 `/v1/ask?record_only=1`，serve 按 id 去重。
- `serve.py`：
  - 用 `/v1/session`、`/v1/ask`、`/v1/outcome` 取代现有的 `/ask` 和 `/outcome`。现有的 `/ask` 只能问服务端已定义的决策点，也不固定 session。
  - `/v1/outcome` 拒绝 `points.toml` 里没声明的来源。目前任何客户端都能自称 `env` 或 `human`。
  - `/decide` 只给 ouroloop 自己的 runtime 和门用，令牌单独发。契约客户端的令牌只能调 `/v1/*`，否则它可以绕过 `ask`，直接拿到概率自己执行。
- `examples/leadgen/`：一个小型线索开发模拟器，只用标准库，按种子生成数据。每条线索有隐藏的意向，回复要过几个模拟日才到。
  - 示例 agent 只通过 HTTP 契约调用两个决策点：`stop`（这条线索处理完了吗；执行动作，用 StopGuard）和 `next_step`（choice：发邮件、打电话、等待、放弃；只记录）。
  - stop 的标签必须与这次决策执行的动作无关。模拟器按提问时刻的隐藏状态打标签（这条线索此刻是否还有待回复或可转化的意向），标签几个模拟日后按 decision id 送达。如果改用事后的业务结果打标签，执行过的决策会自己确认自己：判了 yes 就不再跟进，线索变冷，"确认流失"又被记成 yes。
  - 真实业务里，执行了 yes 的决策只从两处取训练标签：按 ε 走了默认动作的那部分，以及人的确认。其余只用于评测。
  - 每轮 build 之前，模拟器把时钟推进到所有已开线索都有结果；build 只用已经有结果的决策。这里测的是迟到的结果能否按 id 正确关联，不是标签成熟度（见 §3）。
- README：补上契约接入的上手说明，写明契约接入只进化模型轨道。

**验收（离线）**：`tests/test_contract.py`

- 模拟器通过 HTTP 连本机的 serve（签名小模型），跑 200 个 session，结果迟到，按 id 正确关联。
- 请求没声明的候选或阶段、上报没声明的结果来源，都返回 400；契约客户端的令牌调 `/decide` 返回 401。
- 跑到一半杀掉 serve：恢复连接后，每次 ask 在账本里都正好有一行。
- `evolve` 能从这个账本建数据，并完成过门。

**验收（真实，需 GPU 主机）**：模拟器对 GPU 主机上的 serve 跑完一代：stop 模型用线索的结果训练，通过门，在新 session 上执行动作。这项不与 M9、M13 的真实运行同时进行。

**依赖**：M7、M8、M9（serve 按版本加载、StopGuard）。

### M11 研究 agent

**目标**：研究 agent 从业务目标出发设定训练目标，驱动写死的管线，像编码 agent 一样修复失败。它能写什么，由对结果的检查来强制，不靠它自觉。

**改动：三件事**

- `ouroloop/research/coder.py`：用 ouroloop 自己的 agent loop（read / write / edit / bash），配上研究 prompt 和一个 `pipeline` 工具。它做三件事：
  1. **定目标**：读人写的 `goals.toml`（业务目标，比如"减少空转的轮次""少漏跟进的线索"），在门记录冻结之前写出 `research/objective.json`，写明哪个决策点、哪个训练切片、样本权重，以及报告哪个业务指标。
  2. **驱动管线**：`pipeline` 工具在框架进程里按固定顺序执行 build、check、train、calibrate、gate，日志写进 `research/runs/<round>/`。
  3. **修失败**：读日志，修改训练配方、数据预处理或训练配置，然后重跑。
     - 过门之前最多重跑 3 次，每次都记录。
     - 过门之前的迭代，只看从训练 session 按哈希切出来的 dev 切分。
     - 每轮只过一次门。
- 目标只决定训练什么，不决定怎么判：
  - 门的判定规则和测试集规则只来自人冻结的门模板，目标里的切片不作用于测试集。
  - 业务指标只在晋升后的新 session 上报告。
  - 门记录只保存目标文件的哈希，供追溯。
- 数据预处理钩子 `research/prep.py`：
  - 只作用于训练切分，在独立子进程里运行，只拿到训练样本，读不到账本。
  - 输出样本的 id 必须是输入样本 id 的子集。
  - 校准集和测试集由冻结的代码构造，不经过这个钩子。
  - 配方里的 `replay`、`init_from` 和数据路径由管线固定，不在研究 agent 能写的键里。
  - 研究轮里，`test_seen_in_train > 0` 从告警改为阻断。

**改动：护栏（核心代码）**

- **隔离**：
  - read、write、edit 限定在 `research/` 目录下。bash 以单独的系统用户或在容器里运行。
  - 研究 agent 能读到的只有本轮冻结时导出的训练 session（`research/export/`，只读）。测试和校准 session 读不到，它也没有 HF 写令牌和 GPU 主机的 ssh 密钥。需要凭据的步骤由 `pipeline` 工具在框架进程里代做。
  - 研究 agent 自己的 agent loop 用独立的 runtime 根目录，所有决策点只记录；它的 session 带 `role=research`，不进 build，也不进门。
- **写入范围按白名单判定**：研究 agent 每次调用 write、edit、bash 的前后，`evolve/guard.py` 都给工作区和包目录拍一次快照（逐文件哈希）。前后差异里，任何不在白名单上的改动都算越界。
  - 可写：`research/` 下的 `objective.json`（仅在冻结之前）、`recipe.toml`、`prep.py`、`train_config.toml`，M12 起再加上 `candidates/`。
  - 其余一律不可写，包括 `goals.toml`、`points.toml`、`ouroloop.toml`、`gates/`、`models/`、`harness/` 下的冠军文件，以及整个 `ouroloop` 包。研究 agent 自己的代码和 prompt 就在包内的 `ouroloop/research/`。
  - 框架自己写的路径不做差异比较，改为检查不变量：账本只能追加（前缀哈希不变）；`champion.json` 只能经管线记录过的晋升改变。这些路径是 `ledger/`、`sessions/`、`transcripts/`、`models/`、`gates/`、`experience.jsonl`、`research/runs/`、`research/export/` 和 `harness/runs.jsonl`。
  - 越界时，把白名单之外被改动的文件恢复原样（账本文件永远不恢复），这一轮记为 `violation`：不晋升，也不算证据。
- **冻结的门**（M8）：门和晋升在框架启动的独立进程里运行，读的是本轮开始时复制出来的只读代码和门记录。判定之前，重新计算 `ouroloop` 包和门记录的哈希，与框架在本轮开始时留存的值比对；留存的值放在研究 agent 读写不到的地方。不一致记为 `violation`。
- **基础设施失败**算 `inconclusive`（M8）：研究 agent 可以修好再跑，但不能把它当成"这个方向不行"的证据。

**验收（离线）**：`tests/test_research_guard.py`，使用脚本化的假 LLM

- 越界：
  - 用 bash 改门记录：这次调用一结束就被发现，这一轮记为 `violation`，不晋升。
  - 用 bash 写生产账本、推 HF、ssh 到 GPU 主机，都失败。
  - 研究 agent 跑完一轮后，产品账本里 stop 的行数不变。
- 正常路径：
  - 改训练配方：允许，并走完整个管线。
  - 假训练器第一次报 OOM：agent 读日志、调小 batch 后重跑，第二次成功。
  - 基础设施失败：结果为 `inconclusive`，经验记录里不算负面结果。
- 数据和目标：
  - `prep.py` 丢掉一半样本：校准集、阈值和测试集都不变。
  - `prep.py` 把一条测试 state 复制进训练集：结果为 `blocked`。
  - `objective.json` 指定一个很窄的切片：门的测试 session id 不变。

**验收（真实，需 GPU 主机）**：从一个业务目标出发（stop：减少空转的轮次），在轮开始时注入一个已知的失败，比如把 recipe 的 `batch_size` 设成在 GPU 主机上必然 OOM 的值，或者在 `prep.py` 里留一个类型错误。研究 agent 要无人干预地修好它并跑完这一轮，结果由冻结的门判定。

**依赖**：M8、M9。M10 可以提供第二个业务目标的例子，但不是必需的。

### M12 harness 随模型进化

**目标**：harness 的改动和模型的改动走同一个门，每次只改发布三元组的一个分量。

**改动**

- 候选对应动作：
  - 决策点的每个候选标签，都要映射到 harness 能执行的一个动作（工具或 skill）。
  - 新增一个候选时，必须在同一个改动里加上实现它的工具或 skill，两者作为一个单元过门。
  - `harness/release.json` 记录三元组。启动时检查每个标签都有对应的动作，否则拒绝启动。
- 新的候选类型 `capability`：spec 新增一个标签，同时在 `research/candidates/` 下加上它的实现，用任务集评测。晋升后由框架复制进 `harness/`；冠军所在的 `harness/` 只由晋升写入。
- `release.py`（M8）加上约束：一个候选只能改一个分量（`capability` 那一对除外），门拒绝同时改两个分量的候选。模型卡和经验记录都记下改动前后的三元组。
- 解耦：
  - `evolve/harness.py` 目前直接 import 编码 agent 和编码任务集（第 18～19 行），`config.build_research` 也一样。改为依赖一个 `Suite` 协议：`tasks()` 和 `run(task, release) -> Run`，`Run` 带分数、session 和 held_out。编码任务集是这个协议的一个实现。
  - `agent/loop.py` 里写死的决策点逻辑拆成钩子：StopGuard（M6），加上黑名单和 tool_gate、retry、compact、route。换一个 harness 也能组合使用这些钩子。
- 沙箱：任务集在容器或单独的系统用户下运行，看不到 `evals/tasks` 里的 hidden 和 solution，也看不到 ouroloop 的工作区和账本。候选 skill 只在沙箱里执行，不进入评测进程。
- 模型晋升之后，研究 agent 在新发布的 session 里找新的失败分组，比如任务失败、发回次数用完、撞到轮数上限、默认动作占比高，据此提出 harness 候选。
- v0.2 里只有 stop 执行动作，而 stop 的标签固定是 yes 和 no；给其他决策点加的标签只会落在 shadow 决策点上，不改变任何行为。所以 `capability` 只做离线验收，真实运行推迟（§3）。

**验收（离线）**：`tests/test_release.py`

- 某个标签没有对应的动作时，发布拒绝启动。
- 在玩具任务集里放一个执行动作的 choice 决策点（测试里直接构造 Runtime，把它打开）：`capability` 那一对作为一个单元过门；同时改模型和 prompt 的候选被拒绝。
- `evolve/harness.py` 不再 import `ouroloop.agent`；一个玩具级的非编码任务集能走完 prompt 评测。
- 一个读取 `evals/tasks` 的 skill 候选，读不到隐藏测试。
- 晋升后的报告只取新发布的 session。

**验收（真实，需 GPU 主机）**：在 M9 或 M11 的一次模型晋升之后，研究 agent 根据新的失败分组提出一个 prompt 候选，在任务集上评测，并由门判定。晋升或者被拒，都算跑通。

**依赖**：M9、M11。

### M13 无人值守多代运行和 RSI 曲线

**目标**：连续多代无人干预地运行，拿到 v0.1 的 M4 没拿到的证据：自动执行比例一代比一代高，而实际风险 ≤ 5%。

**改动**

- `ouroloop loop --generations N`：每一代依次是：用当前发布跑新 session → 研究 agent 跑一轮 → 冻结的门 → 晋升或不晋升。下一代只用新 session。全程在 GPU 主机上按"采集 → 训练 → 评测"的顺序执行。
- 看门狗：GPU 小时、LLM 花费、连续 `inconclusive` 的代数都设上限，超限就停下来并通知人。M9 的执行后监控照常运行，超预算时自动撤销版本。
- 指标定义：
  - 实际风险：执行动作的决策中出错的比例，每个 session 只计第一条执行的行。每一代报告点估计和 Clopper-Pearson 上界，另外报告合并各代后的上界。
  - 上升：在同一份冻结的回放快照上，第 k+1 代的自动执行比例高于第 k 代。
  - 对照组（冻结的通用 TDE、始终走默认动作、Jev）都用离线回放，在同一批带探针标签的行上计算；探针标签不依赖实际执行的动作。只有冠军跑在线 session。
- 离线回放：冻结一份账本快照，用每一代的模型重放一遍，得到可以复现的曲线。只发布 leadgen 模拟器生成的合成账本，真实账本不发布，因为 `redact()` 只遮住密钥，挡不住路径、用户名和主机名。

**验收（离线）**

- 在合成账本里埋一个随时间变化的信号，用签名小模型跑 3 代：每代过门都用新 session，曲线上升。
- 把标签随机打乱跑 20 次，误晋升不超过 1 次。

**验收（真实，需 GPU 主机）**

- 无人干预地连续跑至少 3 代（比如一周）。
- stop 的自动执行比例在回放快照上逐代上升；每一代实际风险的点估计 ≤ 5%，并报告合并后的上界；结果优于冻结基线。
- 每次晋升都有冻结的门记录和新的测试 session。
- 曲线和合成回放账本一起发布。

**依赖**：M6～M12。

## 3. 推迟项

| 项 | 触发条件 |
|---|---|
| 训练 tool_gate 并执行动作 | stop 连续 2 代执行动作且实际风险 ≤ 5%，并且 tool_gate 攒到约 2,000 条人的批准或拒绝，来自至少 200 个 session |
| retry | stop 连续 2 代执行动作且实际风险 ≤ 5%，并且 agent 自己原样重跑的结果攒到至少 500 条 → 开一个 retry 探索阶段 |
| compact | stop 连续 2 代执行动作且实际风险 ≤ 5%，并且 harness 用的模型接口支持编辑历史 → 开一个 compact 探索阶段 |
| route | stop 连续 2 代执行动作且实际风险 ≤ 5%，并且两个档位成为常规配置 → 开一个 route 探索阶段 |
| 把记忆操作作为决策点；拆分 POLICY 和 STATE | 某个 harness（我们自己的或用户的）需要持久记忆，而三元组表达不了记忆的状态 |
| stop 之外的验证级联：步骤级和论断级验证、LLM 那一级、对"完成"按 ε 复核 | stop 已经在执行动作，执行后误判为"完成"的错误集中在某一类问题上，并且步骤级或 LLM 检查能抓到这类错误 |
| 结果时间分级和标签成熟度 | 在一个真实（非模拟）部署里，过门时最新 session 中超过 20% 的决策还没有结果 |
| 离线策略修正（IPS / DR） | 第二个决策点开始执行动作；或者某个执行动作的决策点，默认动作那一侧靠 ε 攒不够结果 |
| 专精模型（每个决策点单独的 LoRA 或单独的模型） | 回归检查显示，共用一个模型的决策点互相干扰 |
| 改进者自我改进（v0.1 的第 3 层）及其版本戳 | 研究 agent 攒到至少 10 轮带门结论的记录，有了晋升率的基线 |
| DSH 适配器 | 契约 v1 稳定下来（M10 之后没有破坏性改动），并且有用户提出需求 |
| `capability` 候选的真实运行 | 第一个 choice 决策点开始执行动作 |
| RunPod pod 自动启停 | GPU 主机上排队等待的训练超过一代的时长 |

## 4. 删掉的

- `gate` 策略模式：模型自动判"否"和交给兜底都会走到 `_ask_person`（`agent/loop.py` `_allowed` 第 233～236 行），所以它从来没有自动化过任何事。策略收成 `act` 开关加上声明的默认动作。
- 未声明的 LLM 兜底路径：并入声明的默认回答者（§0）。LLM 仍然可以在冷启动时作答，但必须在 spec 里声明、记下是谁答的、出错时退回固定标签，而且永远不当训练目标。
- 三层命名（v0.1 的"RSI 的范围"）：换成数据闭环加门闭环；第 3 层推迟。
- 五种角色的提法：它不对应任何代码。谁做什么，契约和研究 agent 两节已经说清楚了。
- 宿主必须执行模型动作的规则：删掉。契约只有 §1 的四条规则；是否执行，由 act 开关和阈值决定。
- 把延迟和成本作为契约字段：由 harness 自己测量，不写进契约。
- 证据回执：账本记录、冻结的门记录和模型卡已经构成证据。
- 按候选归因：每次只改一个分量，归因自然清楚。

## 5. 风险

| 风险 | 应对 |
|---|---|
| 30 个编码任务不够，stop 学到的是具体任务 | held-out 任务不进训练和校准，它们的 Brier 在卡上并列报告；优先增加任务数量，而不是增加重复次数 |
| "没做完"的标签太少 | 用弱档位和低轮数上限跑；两个阶段都提问，由探针打标签 |
| 数据量撑不起阈值，stop 一直不执行 | 按 session 计数，一个阶段至少要有 59 个会执行的 session；stop 的 `cal_frac` 取 0.4，校准 session 到 120 个才打开 act；采集最先开始 |
| 每次过门都要新 session，代与代之间变慢 | session 预算写进门模板；旧 session 只能用来验证 |
| 研究 agent 绕过写入范围（用 bash 写别处、改环境、在判定前后把文件改回去） | 单独的系统用户或容器；每次工具调用前后都查快照；门在框架进程里运行，判定前核对代码和门记录的哈希；越界的那一轮作废 |
| 研究 agent 借数据预处理偷看测试集 | 它读不到测试和校准 session；prep 只拿到训练样本；训练集和测试集的 state 哈希重合时阻断 |
| 执行动作的 stop 过早结束没做完的任务 | 阈值按阶段拟合、按 session 计数；按 ε 走默认动作；执行后监控超预算时自动撤销；业务场景里的"完成"要有业务结果或人的确认 |
| 分支名或标签被改写，没过门的权重被加载 | 只按 commit sha 加载；阈值按 `model_id` 存，没晋升的版本永远不执行 |
| GPU 主机上训练和 serve 抢显存 | 训练时 serve 切到 CPU 或暂停新 session；每代按顺序执行；需要并行时训练放到 RunPod 上 |
| 固定在旧版本上的 session 让 serve 同时驻留多个模型 | 按 LRU 最多驻留 2 个版本，超出时按需重新加载 |
| 每轮都问 stop，harness 变慢 | 走局域网内的 HTTP；超时走默认动作，并记一行 |
| HF 或 pod 故障 | 算 `inconclusive`；数据和 checkpoint 都固定 sha，重跑可以复现 |
| 任务集里的 agent 用 bash 读到隐藏测试 | 探针在会话结束后才写入隐藏测试；M12 起任务集在沙箱里运行 |
| 公开仓库泄露私有信息 | 主机、仓库名和密钥只放在 `*.local.toml` 里；HF 仓库设为 private；只发布合成账本；方案和示例里只写"GPU 主机" |

## 6. 进度

- M6 已完成（2026-09-25）：
  - 代码：阶段（每行记下阶段和默认动作）、session 事件、StopGuard（`ouroloop/stop.py`）、探针（`ouroloop/agent/probe.py`）、stop view v2。评审修掉三个问题：per_turn 判"完成"时不跑 checker，会让模型推翻规则；探针复制目录失败会中断整次运行；探针打标签时的异常会让任务集崩溃。
  - 任务集从 10 个扩到 29 个，其中 8 个 held-out，全部通过 `suite --validate`。去掉了一个与 `duration_units` 几乎重复的任务。
  - 真实验收通过：采集在固定于 M6 提交的独立工作树里运行，主干后续的改动不影响它。前 50 个 session 里 yes 和 no 两种探针标签都有。前 86 个 session 共 327 个 stop 决策：per_turn 阶段 153 个 no、129 个 yes，on_finish 阶段 41 个 yes、2 个 no。per_turn 的 yes 是任务已经完成、agent 还在继续的时刻，M9 的收益主要在这里；on_finish 几乎不会判错，"发回"那一侧大概拿不到阈值，按执行规则不执行。
- M7 已完成（2026-09-25）：执行开关、按（模型、spec、决策点、阶段）存的阈值、按 session 计数的 Clopper-Pearson、出错走默认动作、声明的默认回答者（固定标签、规则或商用 LLM）、规则否决、只用于评测的来源。对抗性安全评审找到并修掉六个洞，每个都先写了会失败的测试：
  - 单个 NaN、负数或无穷大概率会被重新归一成有把握的答案，越过阈值执行；
  - 自定义后端返回未归一的分数（比如 2.0）时，温度为 1 会跳过归一，置信度超过任何阈值；
  - runtime 建不起来时，Claude Code hook 吞掉异常，跳过黑名单；
  - NaN 阈值、温度 ≤ 0、空的模型 id 都会让校准条目误放行；
  - spec 哈希没有覆盖 view 的截断方式；
  - 带引号的 `act = "false"` 会被当成打开。
  - 共 99 个离线测试。
- M8 已完成（2026-09-26）：门模板和每轮的门记录、新 session 规则、比较交换的持久晋升、发布三元组和 session 固定、撤销名单，以及 M7 安全评审留下的四项。评审修掉两个会让 M9 在真实账本上白白耗掉测试 session 的问题（测试 session 可能没有标签；新 session 不够时冻结的记录会被一直沿用）。绕过门的对抗评审找到并修掉 15 个洞，每个都先写了会失败的测试（`tests/test_gate_bypass.py`），例如：删掉 `used` 标记让测试 session 重新算新；手改 `champion.json` 指向被拒的候选；手改 `calibration.json` 的阈值；配方候选把 `init_from` 或 `replay` 指向本轮的校准数据；`test.jsonl` 被拷给训练器；`gate freeze --replace --set risk=0.5` 放宽之后所有的门；研究 agent 读到 held-out 结果；session id 里的路径穿越写坏账本。共 129 个离线测试。
- 顺序调整（2026-09-26，见 `docs/DEMO_PLAN.md`）：演示改为贪吃蛇（框架的 demo），行业场景延后。接下来依次是：
  1. GPU 主机上的训练和服务：M9 里训练路径（经 Hugging Face、按 sha 加载的 serve）那一半，用贪吃蛇来验证，而不是编码 stop。
  2. 贪吃蛇场景包和 `ouroloop demo`：环境、决策点、环境前瞻打标签、默认的规则蛇、指标。
  3. M11 研究 agent，先用 `manual` provider 驱动；它和 API provider 的数据协议完全一致。
  4. 在 GPU 主机上跑多代，得到贪吃蛇的 RSI 曲线（M13）。

  延后：编码 stop 的执行对照验收（M9 的另一半，也省下 GLM 调用）、M10 的行业示例、M12。
