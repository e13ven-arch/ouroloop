# ouroloop 设计

这份文档是权威设计。代码是最终依据：文档与代码不一致时，是文档错了。全文所写的每一条都指名它所在的文件；没有建的东西直接写"没有建"。

范围：`ouroloop/` 这个包。行数、测试数和各里程碑的进度只在 `docs/ROADMAP.md` 的"现在的位置"维护，这里不复述。

---

## 一、主线

一个 agent 每天要做大量小判断：这条命令要不要重跑？这段输出还要不要留在上下文里？这个动作要不要先问人？任务算不算做完了？

ouroloop 把每一个这样的判断变成一个**有类型的决策点**：一道 noul / choice / score 的题（`ouroloop/types.py`），一个把上下文渲染成文本的 view，以及一个**声明好的默认作答者**——固定标签、规则函数（`rule:module:function`）或 LLM（`llm:<backend>`）（`ouroloop/decision.py` 的 `DecisionPoint.phases`）。

一个小决策模型只有在两件事同时成立时才允许动手：它的**校准后置信度**过了阈值，**并且它的答案与路由默认不同**（`ouroloop/policy.py:route`）。其余一切地方，默认原样执行。环境里观察到的结果是训练标签，写进只追加的账本（`ouroloop/ledger.py`）。

这个环是递归的：被提升的模型会把 agent 带到默认作答者永远到不了的状态，那些状态上的结果就是下一轮的数据（`ouroloop/generations.py` 的世代循环是这个环的可执行形式）。

改的是**决策**，不是推理：商用 LLM 已经推理得够好了。

### 1.1 产品入口是契约

任何 agent 都能接进来，接口就是四条规则；ouroloop 自带的 harness（`ouroloop/agent/`）只是这份契约的参考实现。

1. **一个入口**：模型的每个回答都经过 `ask(point, state, candidates)`（`ouroloop/runtime.py:ask`）。标签集合是封闭的，在决策 spec 里声明；模型不执行时的默认动作也在那里，按阶段声明（`DecisionPoint.phases`）。调用时只能选一个已声明的阶段，不能传任意标签。
2. **规则可以否决模型，模型不能否决规则**：工具调用前先过死名单（`points.denied`），有 checker 时 stop 由 checker 的结论决定（`ouroloop/stop.py`）。规则的结论经 `veto=` 记一行，路线 `rule`；模型照常作答，但只被记录。
3. **只追加的账本**：每次 ask 记一行，无论走哪条路线（`act` / `default` / `shadow` / `rule` / `error`），都带原始上下文、模型版本、路线、阶段和默认动作；结果是之后的事件，按 decision id 关联。
4. **只有环境和人的结果能当训练目标**（不变量 6）。

两种接入方式能进化的东西不同：**契约接入**（SDK 或 HTTP）只进化模型轨道——决策模型、温度与阈值、决策点 spec 的措辞和标准（不能增删标签：增删标签要配上执行它的动作）；**用 ouroloop 的 harness** 还多一条 harness 轨道——prompt、工具、skill。

契约今天只有一半：`Runtime.ask` / `outcome` 和 `serve.py` 的 `/ask`、`/outcome` 在，`ouroloop/contract.py`、`points.toml` 和 `/v1/*` **没有建**（`docs/ROADMAP.md` §3.5）。

---

## 二、分层

### 2.1 控制面（control plane）

控制面负责判断、裁决与记录，**自己不直接花算力**。它持有：

| 组成 | 文件 |
|---|---|
| 账本（决策、结果、会话、门事件、监控事件） | `ouroloop/ledger.py` |
| 诊断与计划（准入） | `ouroloop/evolve/plan.py` |
| 校准（温度与阈值） | `ouroloop/evolve/calibrate.py`，`ouroloop/policy.py` |
| 冻结的门 | `ouroloop/evolve/gate.py` |
| 注册表（血缘、冠军指针、部署、回滚） | `ouroloop/evolve/registry.py` |
| 事后监控 | `ouroloop/evolve/monitor.py` |
| 发布三元组与撤销名单 | `ouroloop/release.py` |
| 一轮的编排 | `ouroloop/evolve/pipeline.py` |

控制面允许与执行层在同一台机器上（一台机器的布局，见 §3）；"控制面永不 import torch"这条约束已经取消（§6），因为 import 本来就是惰性的（`ouroloop/serving.py:_load`、`ouroloop/backends/tde.py`），而那条约束会直接否掉一台机器的布局。

### 2.2 执行层（execution layer）

执行层干活，**不含任何判断**。它内部是四条各自独立的流水线，彼此只共享两样东西：账本和门。它们的数据、候选形状与评价方式都不同，所以分开。

#### 流水线一：数据（ledger → rows → splits → datasets）

* 输入：账本里带有存档上下文（`ctx`）且有可训练结果的决策行（`ouroloop/evolve/replay.py:labelled`）。
* 输出：`<workspace>/models/<version>/data/` 下的 `train.jsonl` 与 `val.jsonl`，以及留在内存里的 calibration / test 两份（`ouroloop/evolve/build.py:build` 返回的 `Dataset`）。每条样本的字段：`id, source_id, dataset, split, primitive, state, question, candidates[{name, description}], target, template_id, meta{weight, truncated, ts, source_spec}`。
* 为什么分开：切分本身是安全属性，不是训练细节。测试会话由门记录在训练之前就定死（`GateRecord.split`），held-out 任务的会话永不进训练或校准（`replay.not_held_out`、`Ledger.sessions` 的 held_out 传染规则），trainer 拿到的目录里**只有**训练会话——阈值若拟合在选过 checkpoint 的行上就是乐观的（`build.build` 的 docstring）。行是用**当前 spec 重新渲染**的（`build.to_example`），所以换了 spec 的候选不必重新采集数据。

#### 流水线二：模型训练（job spec → worker → 模型库里的一个 ref）

* 输入：`TrainJob(version, data_dir, recipe, init_from)`（`ouroloop/evolve/train.py`）。
* 输出：`TrainResult(model_id, backend, job)`，其中 `model_id` 是**模型库给的不可变引用**（job id，或 `<repo>@<commit>`）。
* 路径：`JobTrainer` → `Compute`（本机或 ssh，`ouroloop/evolve/compute.py`）或 `cloud`（每个作业一台新实例，`ouroloop/evolve/cloud.py`）→ 主机上装好的 `python -m ouroloop.worker spec.json <job id>`（`ouroloop/worker.py`）→ 插件（`ouroloop/trainers/`）→ 模型库（`ouroloop/stores/`）。
* 为什么分开：训练是唯一会长时间占用 GPU、会失败、会被杀掉的一步。它的失败必须能与"候选不好"区分开——作业的任何失败都是 `TrainError`，门读作 inconclusive（`gate.INFRA`）。作业只通过文件通信（`spec.json`、`train.jsonl`、`val.jsonl`），shell 行里只有人设的路径和一个生成的 job id（`compute.Compute.line`）。

#### 流水线三：推理（store → serving → 按 ref 作答）

* 输入：卡上的 backend spec `{"type": "model", "model": <ref>, "trainer": <plugin>, "options": {...}}`。
* 输出：一个 `Backend`，它的每个 `Answer.model` 报告的就是那个 ref（`ouroloop/serving.py`）。
* 为什么分开：训练出模型的那个 trainer **不是**问它的那条路。会话、校准、门的打分、部署探针，全部从 `Serving` 取 backend（`serving.py` 的开头，`pipeline.prepare_model` 用 `rt.serving.backend(...)`）。这样才可能把服务放在与训练不同的机器上，也才可能在部署时用探针核对"它自报的 id 是不是卡上写的那个"。

#### 流水线四：harness / prompt 优化（prompt、tool、skill 候选 → 任务集运行 → 同一个门）

* 输入：`{"system": ..., "tools": {name: description}}` 的改动（`ouroloop/agent/prompt.py:HarnessPrompt.apply`）。
* 输出：新的冠军 prompt（`<workspace>/harness/prompt.json`，带历史与 compare-and-swap）。
* 评价：候选与冠军在同一批里各跑一遍任务集，一次运行的 loss 是 `1 - 通过检查的比例`（`ouroloop/evolve/harness.py:prompt_evaluator`），这些运行产生的会话就是这次门的测试会话。
* 为什么分开：它的证据不是账本里的行，而是**新跑出来的任务运行**；它的候选不是模型也不是 spec；而且一次 prompt 改动会**移动路由默认**，从而作废增益基线和此前拟合的每一个阈值——所以计划层面禁止同一轮既改 harness 又训模型（这条见 §6，尚未在代码中强制）。skill 候选目前没有建：候选只有 `system` 与 `tools` 两个键。

### 2.3 研究 agent 的位置

研究 agent 在这一切**之上**，是训练过程的指挥：它决定这一轮要不要训、设计并排序实验、叫停不值得跑完的实验。它只产出一个计划，自己不执行任何东西。

代码现状要分两件事说清楚：

* `ouroloop/evolve/plan.py` 是计划这一侧：`diagnose → plan → admit`，`plan()` 的 docstring 明确说这就是研究 agent 要实现的同一个函数；现在装在里面的是**内置规则计划器**，不是 LLM。
* `ouroloop/research/`（`basic.py`、`loop.py`）是另一件事：一个每轮提候选（spec / recipe / prompt）并逐个过门的循环。它不是上面那个"要不要训"的指挥者。README 里那句"Not built yet: the research agent inside this loop"说的就是这个缺口。
* §6 里写定的研究 agent 设计（重复锁的指纹、用门自己的配对 bootstrap 半宽做功效检查、失败家族两次退休、边界清单）**尚未实现**；现在代码里的对应物是 `plan.repeated`（指纹只有 kind + change，且用训练行数比较）与 `plan._point_reasons`（用闭式的 `checks.min_detectable_difference`）。

---

## 三、四个角色与部署布局

四个角色，与机器数量无关，只由配置表选出（`ouroloop/config.py` 开头就是这么写的）：

| 角色 | 由哪张表选 | 取值 |
|---|---|---|
| 控制面 | 跑 ouroloop 的那个进程本身 | `[workspace] root` 指向它的工作区 |
| 训练 | `[trainer]`（外加 `[trainers.<name>]`） | 不写 `host`（本机本 Python）、`host = "local"`（本机 `~/.ouroloop/venv`）、`host = "user@box"`（ssh）、`[trainer.cloud]`（RunPod / Vast.ai，每个作业一台新实例） |
| 模型库 | `[store]` | `type = "dir"`（ref 是 job id，模型在训练它的机器上）或 `type = "hf"`（ref 是 `<repo>@<commit>`，任何地方可读） |
| 服务 | `[serving]` | 不写 `url`：进程内加载；写 `url`：由能读到同一个 `[store]` 的机器上的 `ouroloop serve` 作答 |

代码里强制这几条组合（都在 `ouroloop/evolve/train.py:JobTrainer.__init__`）：

* `host` 与 `[trainer.cloud]` 二选一。
* 云 + `ON_TRAINING_HOST` 的库（`dir`）被拒绝：实例随作业销毁，run 也就没了。
* 远程 host + `dir` 库且 `[serving]` 没有 `url` 被拒绝：模型留在那台机器上，这台机器永远拿不到它。
* `ouroloop serve` 自己的配置里 `[serving] url` 不能指向它自己（`ouroloop/serve.py:make_server`）。

### 布局 A：一台机器

`[trainer] type = "tde"`（不写 `host`，或 `host = "local"`）+ `[store] type = "dir", root = "~/.ouroloop/jobs"` + `[serving]` 不写 `url`。run 留在作业目录里不复制（`DirStore.in_place` 为真，`publish_s` 归零）。样例：`examples/snake/demo.toml`、`examples/snake/offline.toml`。

### 布局 B：控制面在笔记本 + 一台 GPU 主机

`[trainer] host = "user@gpu-host"` + `[store] type = "dir", root = "~/.ouroloop/jobs"`（在那台主机上）+ `[serving] url = "http://gpu-host:8765", token_env = "OUROLOOP_TOKEN"`。GPU 主机上另跑一份 `ouroloop serve`，它的配置有同一个 `[store]` 且不写 `url`。主机由 `ouroloop host init` 准备（`ouroloop/hosts/`），每个作业前训练器再核对一次，不对就自己重跑 init（`hosts.ensure`）。样例：`examples/ouroloop.toml`。

### 布局 C：云训练 + HF 模型库 + 一台服务主机

`[trainer.cloud] provider = "runpod" | "vast"` + `[store] type = "hf", prefix = "your-org/ouroloop-"` + `[serving]` 写服务主机的 `url`，或者干脆不写（HF 的 ref 在哪都能 fetch，就在控制面进程内加载）。发布到库需要的环境变量（`HfStore.ENV = ("HF_TOKEN",)`）从本进程环境传给实例，provider 的 API key 只进那一次请求的 Authorization 头（`cloud.Provider._key`）。

---

## 四、模块

### `ouroloop/types.py` —— 题型原语

* **职责**：一个原语覆盖三种题（`noul` / `choice` / `score`）：`Question`、`Request`、`Answer`、`Backend` 协议；概率归一（`normalize`）、温度缩放（`temper`）、`nll` / `brier`。
* **契约**：`Backend.decide(list[Request]) -> list[Answer]`，一一对应。`Answer.of` 归一化失败（没概率、全零、NaN、负数）就是**弃答**：概率置为均匀、`abstained=True`，永不据以行动。`decisive` = 未弃答 + 概率都是有限数 + 最高标签唯一。
* **不做什么**：不认识决策点、账本、阈值。`Question.presented` 只打乱 `choice` 的候选顺序——顺序是写题人的习惯，会被学成"永远别选最后一个"。
* **与主线的关系**：模型能不能动手，第一道闸就是 `decisive`。

### `ouroloop/decision.py` —— 决策点与它可演化的文本

* **职责**：`Spec`（instructions / criteria / view_params，即研究候选唯一能改的东西）、`DecisionPoint`、`Decision`。
* **契约**：
  * `phases: {phase: label}` 或 `{phase: {"default": "rule:module:function" | "llm:<backend>", "label": <作答者失败时的兜底>}}`。候选随上下文变化的点（如 Snake 的 move）可以不给 label，此时作答者失败就没有动作，宿主自己定。
  * `default_for(phase) -> (phase, 固定标签, 默认作答者)`。
  * `spec_hash` 覆盖：type、spec 的 JSON、`view_version`、`max_view_chars`、`keep`、各 phase 的固定标签，以及（若有）作为路由默认的 rule 作答者。规则的代码本身不入哈希——改了规则要手动 bump `view_version`。
  * `Spec.apply(change)` 只接受 instructions / criteria / view_params，dict 型 criteria 逐键合并。
  * `render(ctx)` 返回 `(文本, 是否被截断)`；`keep` 决定留头还是留尾。
* **不做什么**：不决定是否行动（那是 `policy.route`），不写账本（那是 `runtime`），不持有阈值（那是 `CalibrationStore`，按 `(model, spec_hash, point)` 键）。
* **与主线的关系**：阈值只对"某个模型 + 某个 view + 某个路由默认"有意义，`spec_hash` 就是这句话的实现。

### `ouroloop/points/` —— 五个内置点与死名单

* **职责**：`retry`、`compact`、`tool_gate`、`stop`、`route` 各自的 view、题面与 phase 默认；以及 `DENY`——八条正则，命中的命令任何模型都不能让它跑（`denied()`）。
* **契约**（`ouroloop/points/__init__.py`）：
  * `BUILTIN = {名字: 构造函数}`，`builtin_points() -> list[DecisionPoint]` 把五个都建出来。每个构造函数都用 `setdefault`，所以配置里给的 `phases` 覆盖这里的默认。
  * 各点的 phase 默认（phase → 这个 phase 的固定标签）：`retry {after_failure: "no"}`、`compact {context_full: "yes"}`、`tool_gate {before_call: "no"}`（不放行，宿主去问人）、`stop {per_turn: "no", on_finish: "yes"}`（`view_version = "2"`）、`route {before_step: "large"}`。
  * `denied(command) -> 命中的正则字符串 | None`。
* **不做什么**：v0.2 里内置点中只有 `stop` 允许 act（`policy.ACTING`，在 `Runtime.register` 与 `config.build_runtime` 两处拒绝）；其余等它们各自的触发条件。死名单是规则不是模型：规则在前，模型在后。
* **与主线的关系**：这些就是 agent 循环里那些"小判断"的具体形态。

### `ouroloop/policy.py` —— 执行规则与阈值拟合

* **职责**：`route()`（执行规则）、`cp_upper` / `cp_lower`（单边 Clopper-Pearson）、`harmed()`、`fit_threshold()`。
* **契约**：
  * `route(policy, ans, default, threshold, rng)`：`act` 没开 → `shadow`；答案不 decisive、等于路由默认、没有阈值（NaN 阈值也永远过不了）、或置信度不到阈值 → `default`；否则按 `explore` 的概率仍取默认（记为 `explored`，并记下 propensity），其余为 `act`。
  * `fit_threshold(confidences, correct, sessions, risk, alpha)`：从最有把握的一端往下走（并列一起纳入），**先**走到第一个"哪怕零错误也能过"的切点才开始算数（`cp_upper(0, n, alpha) <= risk`），然后取第一个失败之前的最后一个切点。失败之后哪怕又有过的切点也不要——几十个嵌套检验里挑运气好的，会让一个超风险的模型远比 alpha 更常拿到阈值（Learn-then-Test 的固定序列）。
* **不做什么**：不看账本、不知道门、不落盘。
* **与主线的关系**：风险是**按会话**算的。一个会话只要有任何一次"模型动手且错了"就算被害，预算约束的是"它动手的那些会话里被害的比例"。一个决策在一个会话里问很多次时，按次计的预算什么也不说明。

### `ouroloop/ledger.py` —— 只追加的账本与三个存储

* **职责**：`Ledger`（按天分文件的 JSONL）、`SessionStore`（原始上下文，供换了 view 之后离线重渲染）、`CalibrationStore`、`SpecStore`，以及对一切写盘内容生效的密钥脱敏（`redact`）。
* **契约**：
  * `decision(...)`、`escalation(...)`、`outcome(decision, label, source, weight, **extra)`。`label` 是**一个字符串、一个互不相同的字符串列表（几个答案都对），或 None（没有对的答案）**。
  * `SOURCE_WEIGHTS = {"env": 1.0, "human": 1.0}`；`EVAL_ONLY = {"frontier_llm", "open_llm", "jev", "gate"}`。`Row.target()` 只数 `SOURCE_WEIGHTS` 里的来源，冲突的信号合成加权分布，一个列出多个答案的结果把权重均分。
  * `Ledger.sessions()` 返回每个会话的 `start / points / labelled` 以及它的会话事件字段；held-out 是**永久的**：任何一条事件说了就算，且传染给同一 task 的所有会话。
  * `CalibrationStore` 按 `(model id, spec_hash, point)` 存；文件变了就重读，写入时在锁里重读再改一个条目；旧格式直接丢弃不迁移。
  * `SpecStore.set(point, spec, expected=...)` 是 compare-and-swap，冲突抛 `PromotionConflict`。
  * `SessionStore.path` 校验会话 id（最多 200 个字符，字母数字与 `_ . @ : -`），否则 `serve` 的客户端可以用 `../ledger/<day>` 把行写进账本里。
* **不做什么**：不删、不改、不聚合。门记录、它的使用和裁决都是账本里的事件，`gates/*.json` 只是副本：删掉它们不改变任何记录、不释放任何测试会话、也不撤销任何一次使用——**门从不读它们**（`gate.GateRecord` 的 docstring）。但运行时读：`gates/r_*.used.json` 是 `release.Accepted` 判断"哪个版本被哪份记录接受过、接受了哪几份校准"的唯一来源，删掉它冠军就建不起 backend（`runtime.champion`）、阈值全部为 None（`runtime.threshold`），也就不再作答、不再动手。
* **与主线的关系**：结果就是标签，账本就是训练集，所以它只追加。

### `ouroloop/release.py` —— 发布三元组、接受记录、撤销名单

* **职责**：`Release(model, spec, harness)`；`code_version()`（本包所有会被安装的文件的哈希）；`Accepted`（从 `gates/r_*.used.json` 读出"哪个门记录接受了哪个版本、以及它接受的那几份校准"）；`Revoked`；`revoke()`。
* **契约**：`Accepted.promoted(card, standing=True)` 要求卡上的 `gate_record` 确实接受了这个 `version` 且未被回滚撤回；`Accepted.calibration(record, model, spec, point)` 给出那次记录接受的温度与阈值。
* **不做什么**：不做权限、不做签名。它只是让"手写的冠军指针 / 卡 / 校准"无法生效。
* **与主线的关系**：这是"只有过了门的东西才能作答、才能动手"这条不变量的落点。

### `ouroloop/runtime.py` —— 决策点、backend、默认作答者与账本的粘合

* **职责**：注册决策点、开会话并**钉住**这次会话跑什么（`Pin`）、`ask` / `record` / `outcome`、`champion()` / `drop()`、温度与阈值的读取。
* **契约**：
  * `register(point)`：被提升的 spec（`harness/specs.json`）覆盖代码里的；每个 phase 的固定标签必须是该点的候选之一；`llm:` 作答者必须在 backends 里；`rule:` 作答者必须能 import；内置点除 stop 外 act 必须关着。
  * `session(session, meta, harness) -> Release`：**在这个会话的任何决策之前**固定它的发布与校准快照，并落盘到 `<sessions>/<id>.release.json`（进程重启后同一会话仍跑同一模型）。新会话固定之前先跑一次事后监控（`monitor.watch`）。
  * `ask(point, ctx, *, session, step, phase, veto)`：phase 的默认作答者若是 rule，**先让规则作答**，它的答案就是路由默认（所以与规则一致的模型永远不动手），也是模型不动手时的动作；否则路由默认是固定标签，LLM 作答者只在模型不动手时才被问。`veto` 是规则的裁决（一个标签，或一个吃进"将要执行的动作"返回覆盖标签的函数），命中则 route 为 `rule`。
  * 路上任何一步出错（view、题面、backend、校准、路由、规则）都记为 route `error`，带上原始上下文，并取默认。
  * 冠军作答时还多两道核对：这个会话钉住的模型 id 必须与它自报的一致，以及 `revoked.hit(version, pinned model, answering model)` 为真时阈值直接置 None。
  * `record(...)` 与 `ask` 同签名，但不问模型（影子采集）；`ask_question(...)` 是不属于任何点的一次性提问，同样落账，且永远不动手。
* **不做什么**：不训练、不提升、不决定什么时候训练。
* **与主线的关系**：主线上"每次判断都落账、默认照常跑"的那一半，全部在这里。

### `ouroloop/serving.py` —— 服务契约

* **职责**：一个训练出来的模型**唯一**的被问路径。
* **契约**：
  * `[serving]` 只认四个键：`url`、`token_env`、`timeout`、`device`（`KEYS`，别的键报错）。
  * `Serving(table, store, plugins)`。`backend(spec)`：`spec["type"] != "model"` 时按普通配置表建 backend（`config.build_backend`）；否则——有 `url` 就走 `HttpBackend`，没有就 `store.fetch(ref)` 后交给插件的 `backend(run, ref, options)` 加载，并缓存。
  * `deploy(spec) -> 它自报的 model id`：加载模型并问它一道探针题（`PROBE`）；模型服务器上就是 `POST /load`。
  * `drop(spec)`：放掉一个（被撤销的）模型，连同它占的显存（backend 的 `close()`）。
  * `release()`：训练作业要用 GPU 之前放掉所有常驻模型；模型服务器上是 `POST /release`。
* **不做什么**：不从 trainer 手里直接拿模型对象；不解释"模型是什么"（架构、键函数都从 run 里读，`options` 只说怎么加载）。
* **与主线的关系**：部署探针之所以能核对 id，就是因为门、校准、会话与撤销全都按这个 ref 键。

### `ouroloop/stores/` —— 模型库契约

* **职责**：模型存在哪里，与"在哪训练"和"在哪服务"分开。
* **契约**（`ouroloop/stores/__init__.py`）：一个类型就是本包下一个模块，导出 `STORE`：

  | 名字 | 含义 |
  |---|---|
  | `TYPE` | `[store] type` |
  | `ON_TRAINING_HOST` | 模型是否留在训练它的机器上 |
  | `STORE(table)` | 不认识的键一律 `ValueError` |
  | `.table` | 随作业的 `spec.json` 传给 worker 的那份表 |
  | `.job(ref)` | 这个 ref 对应的 job id，不是本库的 ref 就返回 None |
  | `.publish(run, job_dir, job_id) -> ref` | worker 侧，一次性发布 |
  | `.fetch(ref) -> 本机目录` | 非本库的 ref 是 `ValueError` |
  | `.here()` | 从本机看这个库（路径绝对化） |
  | `.in_place(jobs_root)` | 发布后是否原地不动（不用留上传时间） |
  | `ENV` / `REQUIREMENTS` | 发布需要的环境变量 / pip 依赖 |
  | `probe()` / `ready(record)` | 在训练机器上记进 `host.json` 的东西 / 为什么发布不了 |

  `dir`：`<root>/<job id>/run`，ref 就是 job id；trainer 若把 run 放在别处直接报错。`hf`：一个私有仓库 `<prefix><job id>`，一次 commit，ref 是 `<repo>@<commit>`，run 在仓库根、作业的 `spec.json`/`train.jsonl`/`val.jsonl` 在 `job/` 下。
* **不做什么**：不做自己的内容哈希（ref 本身就定位了一个 run），不在主机之间拷贝模型，不覆盖已存在的 ref。
* **与主线的关系**：不可变的 ref 是卡、校准、会话钉、撤销名单唯一持有的东西。

### `ouroloop/trainers/` —— 训练器插件契约

* **职责**：一种决策模型就是一个插件；框架对所有插件做同样的事（建数据、跑作业、发布、校准、过门、部署、按 ref 服务）。
* **契约**（`ouroloop/trainers/__init__.py`；`load()` 强制 `validate`、`train`、`backend` 三个可调用）：

  | 名字 | 侧 | 含义 |
  |---|---|---|
  | `NAME` | | `[trainer] type` |
  | `REQUIREMENTS` | 主机 | 除 ouroloop 外的 pip 依赖；插件自己的包可由选项 `source` 给（本地 checkout 或 pip 需求串） |
  | `SUPPORTS_WEIGHTS` | | 默认 False：不加权时框架只给它权重 1.0 的行 |
  | `NEEDS_PARENT` | | 默认 False：必须有父模型，没有的作业在启动前就被拒 |
  | `RECIPE` | | 自己的配方键与默认值，叠在框架的 `min_weight / focus / seed / init_from` 之上；别的键一律拒绝 |
  | `PATH_KEYS` | | `RECIPE` 里指向主机文件的键，只能由人在 `[recipe]` 里设 |
  | `validate(spec, job_dir) -> dict` | 主机 | dry run 检查什么，不训练 |
  | `train(spec, job_dir, deadline) -> run 目录` | 主机 | 必须是 `job_dir/run`；读 `train.jsonl` / `val.jsonl`；父模型用 `worker.parent_run`；起进程一律走 `ouroloop.worker.run`（到 deadline 自动停） |
  | `version()` | 主机 | 可选，记进 `result.json` 与 `host.json` |
  | `check(options) -> dict` | 主机 | 可选，host init 装完后调用；`"ok": False` 则主机不算就绪 |
  | `backend(run, ref, options) -> Backend` | 服务 | 进程内加载，作答时报告 `ref` |
  | `release()` | 服务 | 可选，放掉显存 |

  `tde`（`ouroloop/trainers/tde.py`）：`NEEDS_PARENT = True`、不加权、`PATH_KEYS = {"replay"}`；把配方映射成 TDE 的 `TrainConfig`，架构键（backbone / readout / …）从父 run 的 `config.json` 继承，TDE 在 `val.jsonl` 上选 checkpoint，**绝不**在 ouroloop 的校准划分上选。`signature`（`ouroloop/trainers/signature.py`）：只用标准库，run 自描述（`model.json` 里同时存键函数与计数表），离线测试与 demo 走它。
* **不做什么**：插件不碰门、不碰校准、不碰账本、不决定谁被提升；run 目录必须自描述（`options` 只说怎么加载）。
* **与主线的关系**："训练什么模型"是可换的，"怎么判断它能不能动手"是不可换的。

### `ouroloop/worker.py` —— 主机上的一个作业

* **职责**：在 `<jobs_root>/<job id>/` 里跑一个作业：核对代码版本 → 载插件 → 记主机清单 → validate（`check_only` 到此为止）→ train 到 `deadline - publish_s` → 发布 → 写 `result.json`（`os.replace`）。
* **契约**：worker 是**装在主机上的**，不随作业拷贝；`spec.ouroloop.code` 与本机 `code_version()` 不一致就拒跑（退出码 3）。插件的子进程走 `run(args, deadline, ...)`：自己的进程组、到点先 TERM 再 KILL，Linux 上还设 `PR_SET_PDEATHSIG`（worker 被 `kill -9` 时子进程跟着死）。
* **不做什么**：不判断好坏，不写账本。
* **与主线的关系**：作业与控制面之间只有文件和退出码，所以作业的失败永远不会被读成"候选不好"。

### `ouroloop/evolve/compute.py` 与 `ouroloop/evolve/cloud.py` —— 作业跑在哪里

* **职责**：`Compute` 把"本机"和"一台 ssh 主机"抹平成同一套 `launch / poll / cancel / unfinished`；`cloud` 把 RunPod 与 Vast.ai 抹平成 `start / ready / stop / running`。
* **契约**：作业就是一个目录加一个脱离的进程组：`nohup sh -c '<python> -m ouroloop.worker spec.json "$0" > log 2>&1; echo $? > exit' <job id>`。leader 与 worker 都把 job id 作为最后一个参数，所以一个被复用的 pid 不会被当成这个作业。`poll` 三态：`running` / `exited`（退出码、`result.json`、日志尾巴）/ `lost`（没有退出码且进程没了）。**读不到状态（ssh 失败、笔记本睡了）算 running。** `unfinished(tag)` 按 `spec.json` 里的 workspace 过滤。
* **不做什么**：不做 GPU 锁（一台机器一次一个训练由操作者保证），不清理作业目录，不在作业失败后重连——那一轮直接重来。
* **与主线的关系**：这些是让无人值守的环能一直跑下去的东西，不是运维功能。

### `ouroloop/evolve/train.py` —— 插件即作业

* **职责**：`JobTrainer`：准备 `spec.json`、（非云作业时）让同机的服务先放掉显存、启动、等、读结果；云作业时外面套一层"一作业一实例"的生命周期。
* **契约**：`train(job) -> TrainResult`；`result.json` 里的 ref 必须是**这个作业**在库里的 ref，否则报错。任何失败（非零退出、lost、超过 `deadline_s` 两个 poll、`result.json` 不成形、主机 init 失败、云 provider 失败）都是 `TrainError`。等待过程中以任何别的方式结束（异常、KeyboardInterrupt）都会取消作业；一个进程的第一个作业先清掉同一 workspace 被杀掉的进程留下的作业与实例。
* **不做什么**：不选候选、不判定。
* **与主线的关系**：`_signals_raise()` 把 SIGTERM / SIGHUP 变成 `SystemExit`——无人值守的循环通常就是被 systemd 或 `docker stop` 这样停掉的，不这么做，训练作业会变成孤儿并继续占着 GPU。

### `ouroloop/evolve/build.py` 与 `ouroloop/evolve/checks.py` —— 数据与训练前检查

* **职责**：账本 → 样本（上面的数据契约）；训练前的数据体检。
* **契约**：`build(rt, out_dir, rows=...)` 只把 train / val 写盘，calibration 与 test 留在返回值里。`check(ds, ...)` 分三档：`blocking`（截断率超限、候选位置严重倾斜、与评测集共享 13-gram）、`insufficient`（训练样本太少、测试集能分辨的差异太大）、`warnings`。
* **不做什么**：不修数据、不重标注。
* **与主线的关系**：每一条检查都对应一次被悄悄浪费掉的 TDE 实验（文件开头列了出处）。

### `ouroloop/evolve/replay.py` —— 离线重放与增益

* **职责**：用（改过的）spec 重渲染历史上下文并打分。
* **契约**：
  * `right(target)`：目标里权重最高的**所有**标签。一次结果可以列出多个都对的答案，风险、增益、默认错误全都问"它在不在这个集合里"。
  * `routing_default(rt, point, row)`：规则作答者在存档上下文上的答案（规则是确定性的，所以更早的行也读得出同样的结果），规则失败则取固定标签。
  * `gain(probs, target, default, threshold)`：会动手且错 → −1；会动手、对、且默认是错的 → +1；其余 0（没动手，或把一个对的默认换成另一个对的答案）。
  * `scored(...) -> (冠军 loss, 挑战者 loss, 会话, 是否默认错误)`，loss 即 `-gain`。冠军用它**现在**的 backend、现在的 spec、以及运行时真会用的温度与阈值（必须是过了门的校准，所以 base、未过门、被撤销的模型都不会动手）；挑战者没有新校准就一次也不动手。
  * `summarize(...)`：每点的 rows / sessions / 默认错误数，以及两边各自的 acted / fixes / harms / harmed_sessions / sessions_acted / fixed_share。
* **不做什么**：不训练、不提升、不碰门记录。
* **与主线的关系**：模型加的东西只在它逆着默认动手的地方；对全部行算 Brier 会被"默认本来就答对"的行淹没。

### `ouroloop/evolve/calibrate.py` —— 温度与阈值

* **职责**：在校准划分上按 NLL 拟温度，再按 phase 拟阈值。
* **契约**：`calibrate_point(...) -> PointCalibration(point, model, temperature, thresholds, n, accuracy, sessions)`。阈值只在"会动手的那些决策"上拟（decisive 且标签≠路由默认）；一个点在 k 个会动手的 phase 上共享风险，各拿 `risk/k`、在 `0.05/k` 的水平上（union bound）；校准会话数不足 `min_cal_sessions` 时所有 phase 都没有阈值，这个点不能动手。`cal.gated(spec_hash)` 是"门记录接受的那份校准"的形状。
* **不做什么**：不决定模型能不能被提升；`apply()` 写下的阈值在拿到门记录哈希之前只用于报告。
* **与主线的关系**：阈值是"模型只在有把握的地方动手"这句话的唯一实现。

### `ouroloop/evolve/gate.py` —— 冻结的门

* **职责**：模型、spec、recipe、prompt 四种候选共用的**唯一**一道门。
* **契约**：
  * 人跑一次 `ouroloop gate freeze` 写下模板（`gates/template.json`）。`LIMITS` 是任何模板都不能放松的下限/上限：`risk <= 0.05`、`min_sessions >= 30`、`n_boot >= 1000`、`suite.min_items >= 10`；而且 `cal_frac` 只能变小不能变大——校准划分按会话 id 哈希切，变大就会吃进已经训练过的会话。
  * 框架每轮取本轮的**门记录**（`gate.open_record`）：上一份若还没被用掉、且是从当前模板与当前代码冻出来的，就沿用它（推理之前失败后的重跑用的是同一份）；只有它被用掉了，或模板/代码变了，或更新的会话会改变它的测试集，才派生并冻结一份新的。记录本身是账本里的事件，`gates/<round>.json` 只是副本。记录在任何训练开始之前就定死了这一轮的测试会话（`choose`）：最近的、非 held-out 的、在"上一份用掉了自己新鲜会话的记录"冻结之后开始的、且从未过过门的会话，至多 `budget.sessions_per_generation`；不够就退回更早的会话，那时的裁决最多只能是 `validated`。
  * `record.consume(by)` 在**第一次对测试会话做推理之前**调用，之后这份记录与这些会话就用掉了，无论后面发生什么。
  * `run(record, ev)` 是每一次提升的唯一入口。裁决顺序：证据是否齐全 → 受保护样本是否通过 → 有没有显著回退 → 有没有显著改善。`INFRA`（`TrainError`、`OSError`、`SubprocessError`、`httpx.HTTPError`）一律 `inconclusive`。被接受时先 `record.close("accepted", ...)`（连同这次提升会存下的那几份校准），再执行 `ev.apply()`；compare-and-swap 输了是 `conflict`。
  * `decide_points(...)`：先用事后监控那条规则（`cp_lower(害掉的会话, 动手的会话) > risk`）否掉有害的候选；再要求至少一个点显著变好、且没有点显著变坏。会话数不足 `min_sessions` 的点**不能让候选通过**（它被记进 `thin` 并跳过 bootstrap），但**仍然能否决**：事后监控那条规则在薄厚判断**之前**先跑（gate.py:408 在 gate.py:410 之前），任何一个点上 `cp_lower > risk` 都会让整个候选被 rejected。`plan._point_reasons` 里那句 "neither a pass nor a block could come from this point" 就这后半句而言与代码不符。
* **不做什么**：门参数只来自记录，绝不来自候选（`run` 的 docstring）；配置里若还留着门参数，冻结之后直接报错（`config.check_frozen`）。
* **与主线的关系**：一份记录，一个候选，一次裁决，在新鲜会话上，按对默认的增益。

### `ouroloop/evolve/registry.py` —— 血缘、冠军指针、回滚

* **职责**：每个训练出来的候选一张卡（在评测之前就写下），一个只由 compare-and-swap 改动的冠军指针，以及回滚。
* **契约**：`promote(version, expected_parent)` 三重前提：卡上的父就是当前冠军、门记录接受过它且未被撤回、它不在撤销名单上；`last_good(version)` 沿卡链往上找**最近的**被接受、已部署、未撤销的祖先；`rollback(reason, expected)` 先把退役的版本放进撤销名单并撤回它那份门记录的接受，**再**移动指针（不存在"既不是冠军也没被撤销"的瞬间）。
* **不做什么**：不删卡、不重写历史。
* **与主线的关系**：回滚的目标是"最近的那个好祖先"，不是"上一个版本"。

### `ouroloop/evolve/monitor.py` —— 事后风险监控

* **职责**：把冠军放在它自己造出来的状态上重新审判。
* **契约**：`Tally` 从账本上次读到的位置往后增量读（每个文件只读新追加的字节，半行留到下次，坏行跳过并计数），维护每个模型"动手过的会话数 / 被害的会话数 / 动手过的点"。`watch(rt, tally, alpha=0.05)` 在**每个新会话被钉住之前**跑：已被撤销却还是冠军的，说明有一次回滚没做完，先把它做完；否则一旦 `cp_lower(bad, n, alpha) > risk` 就撤销 + 回滚 + 让服务放掉它。
* **不做什么**：不触发训练（一次撤销是证据，不是扳机），不改校准。
* **与主线的关系**：门是在**日志里的状态**上判的；动手会改变随后的状态，所以在日志上安全的模型在线上仍可能有害。每次看都是 95% 的下界，但每个会话都看一次，所以一个正好卡在预算上的模型被撤销的概率远高于 alpha——重复看的偏差指向"撤销"，也就是安全的那一侧（文件开头给了具体数量级）。

### `ouroloop/evolve/pipeline.py` —— 一轮的编排

* **职责**：`prepare_model`（建数据 → 检查 → 训练 → 记卡 → 校准 → 用掉记录 → 在测试会话上给冠军和候选打分）、`deploy`、`promote`、`promote_model`、`deploy_version`、`evolve_model`、`recipe_evaluator`、`dry_run_model`。
* **契约**：
  * `deploy(rt, registry, card)`：服务加载卡上的模型并问探针，**它自报的 id 必须等于卡上的 `model_id`**；失败是基础设施问题不是裁决，卡上记 `deploy_error`，什么也不变。
  * `promote(...)`：先部署，后换冠军指针（CAS），再把那几份校准连同门记录哈希写下去——顺序不能反。
  * `focused(rt, point, rows, focus, seed)`：训练行里保留**全部**默认错误，其余按种子抽样，使默认错误占到 `focus`。
  * `parent_ref(...)`：只有"同一个插件训练的、且在这个 trainer 的库里"的冠军才能当父；换了 `[trainer] type`、换了 `[store]`、或把 `dir` 库换了机器之后，第一个模型从配置说的地方开始。
* **不做什么**：不决定这一轮要不要训（那是 `plan.py`）。
* **与主线的关系**：模型在被部署之前什么也不服务，这条是在这里落实的。

### `ouroloop/evolve/plan.py` —— 诊断、计划、准入

* **职责**：`diagnose`（只读账本、注册表与撤销名单，不训练、不推理、不冻结任何东西）、`plan`（内置规则计划器）、`admit`（训练之前的硬检查）、`decide`。经验日志不由 `diagnose` 读，它的唯一读者是准入里的 `plan.repeated`（`experience.read`，plan.py:393）。
* **契约**：
  * 关键事实在训练之前就能算出来：风险 r 且零错误时，一个阈值至少需要 `sessions_needed(r)` 个会话（r = 0.05 时是 59），而且只有**含默认错误的**会话才买得到一个，因为只有那里模型才会动手。校准会话里含默认错误的不够这个数时，这份数据上训出来的任何模型都永远不能动手——训练是可证明的浪费。
  * `admit` 的逐点检查：能不能拟出阈值（`error_sessions >= need`、`calibration_sessions >= min_cal_sessions`，仅对 act 开着的点）、有没有门判得动的测试集（`test_sessions >= min_sessions`）、测试集里有没有东西可修（headroom）、预测的增益是否大到看得见。所有目标点都过不了才拒这一轮；个别点过不了就从计划里剔掉（门本身对样本太薄的点也不会让候选通过——但它仍可能因为那条事后监控规则否掉候选，见 `gate.decide_points`）。
  * 全局检查：测试会话不新鲜（门只能 validate）、目标已达成、训练预算用尽、`repeated(rt, p)`（同样的改动在同样多的数据上已经被门**拒绝**过；`inconclusive` / `blocked` / `insufficient_*` / `failed` 都不算证据）。
  * 撤销是冷却，不是扳机：只有当被撤销的冠军动手过的、且还没有候选训练过的会话带来的**已标注行**超过上一个候选训练行数的 `MATERIAL`（10%）时，才会成为提计划的理由；并且只算一次（`Champion.rows` 一旦被某个候选训练过就归零）。
* **不做什么**：不训练、不冻结、不写账本。
* **没有建的部分**：§2.3 与 §6 里写定的研究 agent 版本——重复锁的指纹应是 `(point, kind, 错误组签名, 规范化后的改动)`、功效检查应用门自己的配对 bootstrap 在本轮测试规模上的区间半宽、失败家族 `(point, 错误组签名, pipeline)` 两次后退休——都**没有实现**。现在的 `repeated` 用 `(kind, change)` 加训练行数，`_point_reasons` 用闭式的 `checks.min_detectable_difference`。
* **与主线的关系**："没有通过准入的计划就不训练"这条不变量的全部内容。

### `ouroloop/evolve/harness.py` 与 `ouroloop/agent/prompt.py`、`ouroloop/agent/suite.py` —— harness 流水线

* **职责**：`HarnessPrompt` / `PromptStore`（冠军 prompt 与历史，CAS 提升）；`Task` / `run_task`（一个 TOML 一个任务：`prompt`、`checks`、`files`、`hidden`、`solution`、`held_out`、`max_turns`、`verify`）；`SuiteRunner`（按 `(prompt, agent 身份, task hash, repeat)` 记录运行）；`prompt_evaluator`。
* **契约**：门只看开发划分（dev split）；held-out 任务照跑照记，但**不参与**接受或拒绝——一个会否决候选的划分就是选择程序的一部分，不再是对这个程序产出的独立估计。筛选（screen）是省钱手段不是有资格的过滤器：只挡住在筛选子集上严格更差的候选，而且可以用缓存的运行；门本身**从不**用缓存。运行本身崩了（API 失败）不是关于 prompt 的证据，候选记 inconclusive。
* **不做什么**：不改 agent 的工具实现，只改 `system` 与工具描述；`suite_context` 给研究 agent 的视图里永远没有 held-out 任务。
* **与主线的关系**：harness 改动会移动路由默认，所以它必须和模型走同一道门，而且不能和模型改动挤在同一轮。

### `ouroloop/research/` —— 候选提议循环（与 §2.3 的指挥者不是一回事）

* **职责**：`Workspace`（研究 agent 能看到和能做的一切）、`Candidate` / `Report`、`ask.normalize`（题面校验，choice 补一个 `none_fit`，因为 Jev 否则无法弃答）、`BasicResearcher`、`run_round` / `run_rounds`。
* **契约**：`build_report(rt, record)` 只用本轮的**训练会话**：绝不用记录的测试会话，绝不用校准会话，绝不用 held-out 任务的会话——一个通过看校准行挑出来的候选，它自己的阈值随后就拟合在那些行上，Clopper-Pearson 的保证会失效。`run_round` 每轮一份记录：第一个把记录用掉的候选给出这一轮唯一的裁决并结束这一轮；被提前挡住的候选（no-op、筛选、数据检查、推理之前的失败）把记录留给下一个。agent 只产出候选，框架负责评测、提升与记录。
* **不做什么**：`Workspace` 里够不到 test split；经验记录交给 agent 之前会滤掉所有 `held_out*` 字段；研究点（`research.pick_target`、`research.worth_trying`）都没有开 act，答案只记录、默认才是动作；门给出的裁决写回账本时来源是 `gate`，属于 `EVAL_ONLY`，永远不会变成训练目标。
* **与主线的关系**：这是"LLM 提议、框架裁决"的边界在代码里的样子。

### `ouroloop/agent/`、`ouroloop/stop.py`、`ouroloop/chat.py`、`ouroloop/providers/` —— 最小 agent 与它的 LLM

* **职责**：`Agent`（read / write / edit / bash 四个工具，外加 route、deny、tool_gate、retry、compact、stop 六处判断）、`Tools`、`StopGuard`、`Probe`、provider 无关的聊天类型与三个 provider（`anthropic`、`openai_compat`、`manual`）。
* **契约**：每处判断的动作都是 `Decision.action`——模型动手时是它的标签，否则是 phase 的默认，所以在一个点被打开并拿到阈值之前，这里什么也不会改变 agent 的行为（`agent/loop.py` 开头）。`StopGuard` 的顺序是：模型 → 检查器 → 退回 → 轮数上限；规则可以推翻模型，反过来不行，规则的裁决记成这次决策的 veto（route `rule`）。`Probe` 在每个 stop 决策处拷一份工作目录，会话结束后把隐藏测试写进副本再跑检查，用来给那个 stop 决策打标签——它不是检查器：运行中的任何人（StopGuard、view、agent）都看不到它的结果，它也不会让一次运行失败。provider 的 `source` 是 `frontier_llm` / `open_llm`，属于 `EVAL_ONLY`。
* **不做什么**：这个 agent 不追求强大，它是采集与验证用的最小宿主。
* **与主线的关系**：这些就是主线第一段里那些判断的实际发生地。

### `ouroloop/backends/` —— 谁来回答

* **职责**：`JevBackend`（REST，同一个 state 的多道题合一次请求，429/529 退避重试）、`TdeBackend`（进程内加载 TDE 的 run，最多常驻两个，跨 state 批量前向）、`HttpBackend`（`ouroloop serve` 的客户端）、`KeywordBackend`（离线替身）、`LlmBackend`（把一个 provider 当作答者）。
* **契约**：唯一的接口是 `Backend.decide(list[Request]) -> list[Answer]`（`types.py`），一一对应，并且每个 `Answer.model` 报告自己是谁。哪个表建哪个 backend 由 `config.build_backend` 认：`mock` → `KeywordBackend`、`jev` → `JevBackend`、`tde` → `TdeBackend`、`llm` → `LlmBackend(build_provider(provider))`、`signature` → `SignatureBackend(path, signature=...)`、`http` → `HttpBackend`、`model` → 交给 `Serving`（见 `serving.py`）；别的 `type` 当成 `module:Class` import。表里剩下的键原样当构造参数传进去。
* **不做什么**：backend 不知道阈值、不知道门。任何 backend 自建的 `Answer` 都要经过 `ans.over(q.labels())` 的核验（`runtime._decide`）。
* **与主线的关系**：`Answer.model` 是全链路的键，所以每个 backend 都必须老实报告自己是谁。

### `ouroloop/serve.py`、`ouroloop/adapters/claude_code.py` —— 把宿主接进来

* **职责**：`serve.py` 是一个标准库写的小 HTTP 决策服务（`/health`、`/decide`、`/load`、`/drop`、`/release`、`/ask`、`/outcome`），既是别的机器上的 harness 的入口，也是"模型服务器"这个角色本身；`claude_code.py` 是 Claude Code 的 hooks 适配器，把普通的 Claude Code 会话变成 ouroloop 的决策与结果。
* **契约**：`/decide` 里的 `model` 必须是 `[store]` 的合法 ref，别的一律 400（请求永远说不出一个路径或一个模块名）。非回环地址必须有 token，因为 `/outcome` 写的是训练标签。适配器默认只记录、不问模型、不改变 Claude Code 的任何行为；`tool_gate` 的标签来自**人**（弹过授权框且随后跑了 → yes；被规则放行 → yes，权重 0.5；弹过框但这一轮结束时没跑 → no）。
* **不做什么**：适配器从不放行任何调用——每次调用仍然走用户自己的权限规则；开了 `--enforce-deny-list` 时它只会**拒绝**。
* **与主线的关系**：主线要求标签来自环境和人，这两个模块是它们进来的口子。

### `ouroloop/hosts/` —— 主机准备

* **职责**：`ouroloop host init|check`：把这份 checkout 打成 sdist、连同插件本地源码、`requirements.txt`、`plan.json`、`bootstrap.sh` 一个 tar 过 ssh 发过去，在那边跑幂等的 `bootstrap.sh`，写 `~/.ouroloop/host.json`；`stale(m, trainers, store)` 说清楚一台主机为什么还跑不了这份 ouroloop 的作业。
* **契约**：`read()` 在同一次 ssh 里既读 `host.json`，也问 venv **现在**实际跑的 `code_version()`——venv 坏了或被改过的主机不会被当成就绪的。shell 行里只有固定路径和一个生成的 id，要装什么都以文件形式过去。
* **不做什么**：不管密钥（HF token 在主机上自己设），不生成环境清单以外的东西。
* **与主线的关系**：主机上跑的必须**就是**控制面这份代码，否则作业和它产出的模型就不是这份代码所定义的东西。

### `ouroloop/config.py`、`ouroloop/cli.py` —— 四个角色的配置与命令行

* **职责**：`config.py` 从 `ouroloop.toml` 建出 runtime、backends、LLM、trainer、store、serving 与研究 agent；`cli.py` 是 `run | suite | decide | smoke | demo | research | plan | evolve | deploy | calibrate | gate | models | serve | host | hook | ledger | replay`。
* **契约**：`[points.X]` 只认 `backend / define / act / explore`——风险来自门模板，默认来自点的定义。配置是**唯一**的开关：点的定义不能把自己打开（`build_runtime` 里最后覆盖 `point.policy`）。门冻结之后，配置里再出现 `[research] min_items`、`[suite] min_items` 或 `[checks]` 一律报错（`check_frozen`）。
* **不做什么**：不做门参数的第二个来源。
* **与主线的关系**：§3 的三种布局都只是这几张表的不同取值。

### `ouroloop/generations.py`、`ouroloop/demo.py` —— 世代循环与场景包契约

* **职责**：`generations.run` 是整个环的可执行形式：每一代用当前发布打 `games` 局 → 一轮（诊断 → 计划 → 准入 → 训练/不训练 → 门 → 提升）→ 下一代用这一轮提升出来的东西去打。`demo.py` 是完全离线、无模型的单轮演示。
* **场景包契约**（内核只通过这一个接口认识场景包）：

  ```
  [demo] scenario = "module:Class"，选项来自 [demo.options]，path 指定模块从哪里 import
  Class(**options).play(rt, session, seed, generation) -> {metric: number | bool | str}
      一局即一个 ouroloop 会话：用 rt.session 打开它，问它的决策点，写环境给的结果，返回这一局的指标
  ```

  `examples/snake/scenario.py` 是实现：`move` 点的候选是当前合法的走法（所以没有固定兜底标签），路由默认是规则蛇（`rule:examples.snake.rules:keep_going`），每一步都由环境打标签，标签可以是**一组都对的走法**。
* **契约的其余部分**：`baseline/` 里用同样的 session id、同样的 seed、同样的世代再打一遍，全部由默认作答，指标并排打印——环境变了、换了种子或者只是运气，在那边也会显示出来。打印的每一个数字都在盘上（每局的 `metrics` 事件、每代的 `generation` 事件）。`plant` 指定的那一代，候选在**打乱过的标签**上训练（`Shuffled`），走同一道门——门必须拒绝它。
* **不做什么**：内核不知道 Snake，也不知道任何场景的规则；场景包不碰门、不碰注册表。
* **与主线的关系**：这是"一轮只训练计划准入放行的东西"唯一被真正执行的地方（见下）。

---

## 五、不变量

每一条后面是它在代码里被强制的位置。

1. **模型只在它有把握、且与路由默认不同的地方动手。**
   `ouroloop/policy.py:route`（`act` 开着 + `ans.decisive` + `ans.label != default` + `confidence >= threshold`，四者缺一不可）。阈值只从"带着接受它的那份门记录哈希、且与那份记录接受的温度阈值完全一致"的校准里取（`ouroloop/runtime.py:threshold` → `release.Accepted.calibration`）。规则作答者会先答，它的答案就是被比较的那个默认（`runtime._decide`）。

2. **风险按会话计。** 一个会话只要有任何一次模型动手且错了就算被害；预算约束的是"它动手的那些会话里被害的比例"。
   `ouroloop/policy.py:harmed` 与 `fit_threshold(sessions=...)`；`ouroloop/evolve/calibrate.py:calibrate_point`（按会话拟）；`ouroloop/evolve/gate.py:decide_points`（按会话否决）；`ouroloop/evolve/monitor.py:Tally._event`（按 `(model, session)` 计数）。按决策计的那个选项已经删掉了（§6）。

3. **阈值由 Clopper-Pearson 在固定序列里拟（Learn-then-Test），只在校准行上，绝不在测试行上，也绝不在研究 agent 看过并据以挑选候选的行上。**
   固定序列：`ouroloop/policy.py:fit_threshold`。校准行：`ouroloop/evolve/gate.py:GateRecord.split`，`calibrate.calibrate_point(rows=parts["calibration"])`。trainer 的目录里根本没有校准与测试行：`ouroloop/evolve/build.py:build`。研究 agent 只看训练会话：`ouroloop/research/loop.py:build_report`（`record.split(..., record.cal_frac(name))["train"]`）。

4. **人冻结门模板；框架派生本轮的记录，记录在任何训练之前就定死它的测试会话。一份记录判一个候选，一次，在新鲜会话上，按它对路由默认的增益。**
   `gate.freeze_template`（人）、`gate.open_record` + `gate.choose`（派生，写进账本）、`GateRecord.consume`（第一次推理之前用掉）、`gate.run`（唯一入口；用的不是新鲜会话时最多 `validated`）、`replay.gain` / `scored`（按增益判）。

5. **基础设施的失败永远不是候选的不利证据。**
   `ouroloop/evolve/gate.py:INFRA` 与 `run()` 里对它的捕获 → `inconclusive`；`plan.AGAINST = ("rejected",)`，所以重复锁也只认真正被拒绝过的。

6. **任何 LLM 的答案都不是训练标签。** 标签来自观察到的结果和人。
   `ouroloop/ledger.py:SOURCE_WEIGHTS` / `EVAL_ONLY`，`Row.target()` 只数前者；默认作答者（含 `llm:`）的答案记在决策行的 `answerer` 字段里，不是 outcome（`runtime._default`）；门自己的裁决写回时来源是 `gate`（`research/loop.py`）。

7. **模型在被部署之前什么也不服务：门接受 → 服务加载它、它自报的 id 必须与卡一致 → 冠军指针才动。**
   `ouroloop/evolve/pipeline.py:deploy` 与 `promote`（顺序写死）；`ouroloop/runtime.py:champion`（`accepted.promoted(card)` 为假的版本根本不建 backend，那些点就取默认）；`ouroloop/evolve/registry.py:promote`（CAS + 三重前提）。

8. **监控在每个会话打开时检查冠军，一旦它被害会话比例的下界高过预算就撤销；冠军回滚到最近的那个被接受、已部署、未撤销的祖先。**
   `ouroloop/runtime.py:_open` → `ouroloop/evolve/monitor.py:watch`（`cp_lower(bad, n, 0.05) > risk`）→ `release.revoke` + `registry.rollback` + `rt.drop`；`registry.last_good` 给出那个祖先；已经在跑的会话从它的下一次 ask 起取默认（`runtime._decide` 里的 `revoked.hit`）。

9. **没有通过准入的计划就不训练。**
   `ouroloop/evolve/plan.py:decide` → `ouroloop/generations.py:run`（`train` 为假这一代就记成 skipped，不训练）。**要说清楚的是**，这条不变量有两个缺口：
   * 它只在世代循环里被强制。`ouroloop evolve`（`cli.cmd_evolve` → `pipeline.evolve_model`，cli.py:143-154 直接调 `evolve_model`，不经 `plan.decide`）无论计划怎么说都会跑完一轮（README.md 第 17 行明说了这一点）。`ouroloop plan` 是只读的。
   * 世代循环自己还有一个有意的例外：`plant` 指定的那一代跳过 `decide`，无条件训练一个用打乱标签训的候选（`proposed, train, reasons, _ = (None, True, ["planted"], None) if planted else decide(rt)`，generations.py:194）——它按定义要走同一道门，门必须拒绝它。

10. **一次结果的标签可以是一组都对的答案；风险、增益与默认错误都问"它在不在这组里"。**
    `ledger.Ledger.outcome` 接受 `str | list[str] | None`；`replay.right`、`replay.gain`、`replay.default_error`、`monitor.acted`、`generations._judged` 全部用 `right(target)`。

---

## 六、已定的取舍

这一节记的是**已经砍掉、不要再提**的东西，以及为什么。

### 计算与托管

砍掉：GPU 锁与 CPU 兜底服务（一块 GPU 一次一个训练由操作者保证）；作业目录的清理（操作者的事）；我们自己的内容哈希（ref 本身就定位了一个 run：一个 job id，或 `repo@sha`，而 git commit 本来就是内容寻址的）；在主机之间拷贝模型；经 HF 进数据；重启后重新挂上还在跑的作业（那一轮直接重来）；额外的环境清单；"控制面永不 import torch"（import 本来就是惰性的，而且这条会直接否掉一台机器的布局）；"边界两侧不共享文件或锁"；作业种类（job kinds）；卡上的 eval job plan；单独的 `serve --models-only` 模式；`ComputeError` 这个类；`jobs --logs`。

保留，因为它们是让**无人值守的环一直跑下去**的东西（不是运维功能）：两侧都有的截止时间（`worker` 的 `deadline_s` 与 `train.JobTrainer._wait` 的 `deadline_s + 2 * poll_s`）；状态读失败算作仍在运行（`compute.Compute.poll`）；识别出进程已经没了的作业（`Status("lost")`）；取消本 workspace 留下的孤儿作业与云实例（`train.JobTrainer._sweep` / `_stop_leftovers`）；幂等调用的重试（`backends/jev.py`、`backends/http.py`、`providers/openai_compat.py`、`cloud.Provider._call`，其中 create 绝不盲目重发，见 `cloud.Provider._create`）。

### 契约与策略

* **`gate` 策略模式**（v0.1 的 shadow / gate / auto）砍掉：模型自动判"否"和交给兜底最后都走到同一处问人，它从来没有自动化过任何事。策略收成一个 `act` 开关加上声明的默认动作（`ouroloop/policy.py`）。
* **未声明的 LLM 兜底路径**砍掉，并入声明的默认作答者：LLM 仍然可以在冷启动作答，但必须在 spec 里声明、在行上记下是谁答的、出错退回这个阶段的固定标签，而且永远不当训练目标。
* **三层 RSI 的命名**（v0.1 的"RSI 的范围"）砍掉，换成数据闭环 + 门闭环；第 3 层（改进者自我改进）是推迟项，触发条件见 `docs/ROADMAP.md` §5。
* **v0.1 的"五种角色"提法**砍掉：它不对应任何代码。今天的四个角色是配置表选出来的（§三），谁做什么由 §1.1 的契约和研究 agent 两节说清。
* **"宿主必须执行模型的动作"这条规则**砍掉：契约只有 §1.1 的四条，是否执行由 `act` 开关和阈值决定。
* **把延迟和成本写进契约**砍掉：由 harness 自己测量。
* **证据回执**砍掉：账本的行、冻结的门记录和模型卡已经构成证据。
* **按候选归因**砍掉：一次只改发布三元组的一个分量，归因自然清楚。
* **"把 TDE 当成被复用的库"**（v0.1 列过一张清单：推理、训练、数据格式、校准、指标、泄漏检查都调 TDE）砍掉：校准、指标、配对 bootstrap、数据检查都在本包里（`evolve/calibrate.py`、`evolve/gate.py:paired_bootstrap`、`evolve/checks.py`），TDE 只是一个训练器插件（`ouroloop/trainers/tde.py`），换一种决策模型就是换一个插件。

### 风险与门

这一节只记"砍掉了什么、因此定下什么"；量到的数字与运行编号都在 `docs/JOURNAL.md` 里，不在这里重复。

* 按决策计的风险选项已经删掉：按次计的错误率过得了预算，被杀掉的局却过不了。风险因此改为按会话（不变量 2），门改判**对默认的增益**（`replay.gain`），配方多了 `focus`（保留全部默认错误行，其余按种子抽样，使默认错误占到指定比例，`pipeline.focused`；那些行在当时只占账本的一小部分）。证据见 `docs/JOURNAL.md`「以默认为基准的价值：风险按 session 计，门比增益」和「配方 focus」。
* 门不再用"全部测试行上的 Brier"：那个数被默认本来就答对的行淹没（`gate.py` 开头）。
* 一次结果可以列出多个都对的答案：一个"一样好的另一步"在旧口径下算错，风险被高估、增益惩罚了正确的覆盖。证据见 `docs/JOURNAL.md`「一条结果可以写多个正确答案」。
* 监控必须在**运行时**跑，而不是每代调一次：在日志状态上安全的模型在它自己造出来的状态上仍可能有害，而一代一调意味着坏模型先祸害完整整一代。现在 `Runtime._open` 在每个新会话钉住之前跑（不变量 8）。证据见 `docs/JOURNAL.md`「运行时自己跑执行后监控」。
* 一次撤销不是训练的扳机：撤销即重训，新候选看到的几乎是同一批数据，于是以同样的方式失败。现在撤销是冷却与证据（`plan.plan` 的撤销分支需要 `MATERIAL` 那么多**新的已标注行**，并且只算一次）。证据见 `docs/JOURNAL.md`「没有计划不训练，没有数据不成计划」。

### 研究 agent（综述与批评之后定下的形态，**尚未实现**）

它是一个计划者：每轮产出一个 Plan 或者弃权，此外什么也不产出；LLM 只给目标、候选内容、次序加一句理由、以及弃权的理由，**不产出任何一个能决定事情的数字**。

准入只加两条硬检查，任何人都能从账本重算：**重复锁**（指纹 = `(point, kind, 错误组签名, 规范化后的改动)`，不含数据窗口与父 ref）和**功效**（用门自己的配对 bootstrap 在本轮测试规模上取区间半宽，不用闭式的最小可检测差）。曾经提过八条叫停规则，砍到只剩这两条：要用四套独立机制去拦同一个已知失败，那是过度设计。

其余的边界、每条挡住的那次失败，以及失败家族、撤销冷却的计数口径，见 `docs/RESEARCH_AGENT.md` §3.4、§4、§5、§8；同领域对照（AI Scientist v1/v2、Agent Laboratory、Curie、DGM / STOP）与我们据此否决掉的设计见该文 §9。研究 agent 的实质以那份文档为唯一出处，这里只留结论。

### 数据与切分

研究 agent 看到的行必须和这一轮能训的行是同一批：`build_report` 用 `record.cal_frac(name)` 切，并且只取 `["train"]`（`ouroloop/research/loop.py:build_report`）。来历见 `docs/JOURNAL.md`「研究报告不给 agent 看校准 session」。
