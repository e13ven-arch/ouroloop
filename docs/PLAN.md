# ouroloop 方案 v0.1

> 一个轻量的 RSI 框架：LLM 提升小型决策模型（默认 TDE）的智能，决策模型再提升 agent harness。harness 里的判断交给决策模型，每次判断的真实结果写进账本。自动训练和 LLM 提出的改进都从账本出发，通过同一个晋升门才生效。
> 2026-09-22 起草。本文只定设计，代码从 M0 开始写。

## 0. 定位

### 核心主张

大家都认同 RSI 要让系统自己提升智能，但直接训练基础模型太难。ouroloop 换一个起点：**LLM 提升小型决策模型的智能，决策模型再提升整个 agent harness。**

```
业务 harness 运行 → 账本（决策 + 真实结果）→ LLM 提出假设和候选 → Jev 判断、排序 → 评测与晋升门 → 新的 TDE / spec / prompt 部署到业务 …
```

1. **改小模型，不改大模型**：一次专精训练在 8 GB 显卡上不到一小时，评测几乎免费，所以这个循环可以天天转。
2. **LLM 当研究员，不当标注员**：LLM 负责诊断错误、提出候选，训练标签来自真实结果。这样决策模型学到的是这个场景的真实情况，而不是模仿 LLM 的猜测；也不需要把 LLM 的回答当训练数据。
3. **领域数据越多越有用**：进入产业场景后，工单路由、审批、告警分诊这类决策都有真实结果，管线可以直接产出领域专用的决策模型。

起点证据来自 TDE 两天的实验：人定方向，Claude 做实验设计、找数据缺陷、排队训练。typed-decisions 从零样本 36.1% 提到专精后 70.9%（Brier 0.092；Jev 1.13 为 72.7% / 0.148），clinc150 全 150 类一次前向从 64.6% 提到 86.5%。这些提升来自数据修复、候选数课程、初始化和专精阶段，没有用 Claude 生成的标签。其中 Exp 006～011 的方向、排序和故障处理，都是人不在场时由 Claude 自己定的；人做的判断集中在前期，大多能写成 Jev 可以回答的问题（见 `docs/RESEARCH_LOOP.md` §6）。

| 做法 | 改进什么 | 局限 |
|---|---|---|
| 训练基础模型自我提升 | 大模型权重 | 算力和评测成本高，容易自我强化 |
| 改 agent 自己的代码或 prompt（Darwin Gödel Machine、gear） | 代码、prompt | 每个候选都要跑完整任务，改进主要不沉淀进模型 |
| 把 Jev 接进 agent（awesome-jev 上几十个项目） | 不改进，调用闭源 API | 模型不会随使用变好 |
| ouroloop | 决策模型和决策点定义，从而提升 agent 的决策能力 | 不提升推理能力，推理仍靠基础模型 |

这个取舍基于一个假设：商用基础模型的推理已经够强，真实业务里最关键的是决策，所以提升决策能力的投入产出比最高。

### 组成

| 部分 | 内容 |
|---|---|
| harness | Pi 式的最小 agent loop：LLM 加 read / write / edit / bash 四个工具，判断类的工作交给决策点 |
| 决策层 | 决策点、后端、策略、账本（§2） |
| 进化管线 | `evolve`：自动训练新模型，评测研究 agent 提出的 spec、训练配方和 prompt 候选；都要通过同一个晋升门才生效（§4） |
| 研究 agent | LLM 提出假设、执行实验，Jev 判断条件、排序、决定是否继续；可以换成自己的实现（§5） |

- **和 TDE 的关系**：TDE（`~/workspace/jev`）是研究仓库，负责模型结构、预训练数据和公开基准。ouroloop 依赖 TDE 的推理、训练、校准和评测函数（§11），负责部署之后从真实结果中持续进化。
- **规则**：Jev 和闭源 API 的输出不进训练数据。这条规则由代码强制执行（§2.4），不靠使用者自觉。

### RSI 的范围

| 层 | 谁改进什么 | 评判依据 | 版本 |
|---|---|---|---|
| 1 | 决策模型从 harness 的决策结果中学习（自动训练） | 留出时间段上的结果 | v0.1 |
| 2 | 研究 agent 改进决策点 spec、训练配方和 harness prompt | 同一个晋升门 | v0.1（prompt 在 M5） |
| 3 | 研究 agent 改进自己：研究决策也交给 TDE；提案 prompt 自改 | 同一个晋升门 | 研究 |

不做的主张：不声称实现了通用 RSI；任何一层都不让模型自己评判能否晋升。

## 1. 设计原则

1. **轻，但完整**：轻指概念少（决策点、后端、账本、候选、晋升门）、依赖少（默认只有 httpx）、所有状态都是文件，不指功能少。
2. **每个判断都要有结果**：拿不到可观测结果的决策点可以用，但不进入进化管线。
3. **晋升只看指标**：挑战者由留出集上的结果评判，不由任何模型评判。
4. **LLM 当研究员，不当标注员**：LLM 负责诊断和提出候选；训练标签来自环境结果和人，开源 LLM 的兜底回答只作低权重补充。
5. **本地优先**：账本默认不离开本机。
6. **可回滚**：每个模型版本都有谱系记录，随时可以退回上一个冠军。

## 2. 决策层

### 2.1 决策点

决策点是挂在 loop 固定位置的一道类型化问题，也是收集数据、校准和专精训练的基本单位。harness 会反复问同一个问题，这正是 TDE Exp 007 那种专精训练最有效的场景：5.4k 条数据、507 步，typed-decisions 从 62.1% 提到 70.9%。

```python
from ouroloop import Noul, Policy

retry = Noul(
    "retry",
    question="Will rerunning this failed command, unchanged, succeed?",
    criteria={"true": "transient: timeout, network, lock, rate limit",
              "false": "needs a code, argument or environment change"},
    view=lambda ctx: ctx.render("last_command", "last_error", max_tokens=512),
    policy=Policy(mode="shadow", risk=0.05, fallback="llm", explore=0.05),
)

d = retry.ask(ctx)                                  # Decision(id, label, probs, route, propensity)
retry.outcome(d.id, label="yes", source="env")      # 结果出来后再记
```

`Choice` 和 `Score` 的用法相同。`Choice` 支持 2～255 个候选，候选可以在运行时生成，比如当前可用的工具；`Score` 是 2～10 个有序等级。

| 字段 | 说明 |
|---|---|
| `name` | 稳定 ID，数据、温度、阈值都按它分组 |
| `question` / `criteria` / `candidates` | 格式与 TDE `Decider` 一致 |
| `view(ctx)` | 从 agent 状态构造 state 文本。token 上限要和模型的 state 长度匹配（TDE 目前是 1024）；账本记录实际 token 数和是否截断 |
| `policy` | 见 §2.3 |
| `spec_hash` | question、候选和 view 版本的哈希。问题一改就算新版本，evolve 默认只用当前版本的数据 |

### 2.2 后端

```python
class Backend(Protocol):
    name: str
    version: str
    def decide(self, requests: list[Request]) -> list[list[float]]: ...
```

| 后端 | 说明 | 能否产生训练标签 |
|---|---|---|
| `tde` | 在本地加载冠军模型，包装 `Decider`；候选超过 40 个时改用 `predict_chunked` | 决策本身不当标签 |
| `jev` | 调用 TypeSafe 的 Jev API，问题格式沿用 JevBench 的约定，与 TDE `Decider` 一致；要固定模型版本，因为阈值依赖它 | 输出不作为训练目标（§2.4） |
| `http` | 调用另一台机器上的 `ouroloop serve` | 同上 |
| `llm` | 兜底：让 LLM 从候选里选一个。开源模型如果返回 logprobs，就能得到软分布 | 来源记为 `open_llm` 或 `frontier_llm` |
| 插件 | verdict-2.0、GLiClass 等 | 同 `tde` |

默认所有决策点共用一个模型、一个 checkpoint，温度和阈值按决策点分开。如果回归检查发现决策点之间互相干扰，再考虑给每个决策点单独加 LoRA。

### 2.3 策略：从概率到动作

1. **规则优先**：先跑确定性规则，比如 tool_gate 的危险命令黑名单。模型只处理规则没覆盖到的部分。
2. **按置信度分流**：模型给出分布 p，置信度取 max p。达到该决策点的阈值 τ 就自动执行，否则交给兜底：LLM、人或默认动作。
3. **探索**：以概率 ε（默认 5%）把本来可以自动执行的决策也交给兜底复核，同时记下倾向分，也就是这次动作在当前策略下被选中的概率。不这样做的话，被拦下的那一侧永远没有结果，数据会越学越偏，以后也没法做离线策略修正。
4. **三种模式**：
   - `shadow`：只记录，宿主照原样执行。
   - `gate`：只能往保守方向改，即拦截或升级；放行仍按宿主原来的规则。
   - `auto`：两个方向都按阈值执行。

   新决策点一律从 `shadow` 开始，不可逆操作不允许用 `auto`。

阈值 τ 在校准集上拟合：取最小的置信度，使自动执行部分的错误率上界（Clopper-Pearson 95%）≤ r，r 默认 5%。有限样本保证更强的 conformal / Learn-then-Test 留到研究阶段，与 TDE 第二期共用。

### 2.4 账本

账本文件是 `ledger/YYYY-MM-DD.jsonl`，只追加，有三种事件：

```json
{"type":"decision","id":"d_01JB7…","ts":"2026-09-22T10:00:00Z","session":"s_42","ctx":"s_42#17","agent_model":"qwen3-coder-30b","point":"retry","spec_hash":"a1b2","backend":"tde","model":"m_0007","view":"$ pytest -x\nE   ConnectionResetError …","view_tokens":312,"truncated":false,"candidates":["yes","no"],"probs":[0.91,0.09],"route":"auto","explored":false,"propensity":0.95,"action":"yes"}
{"type":"escalation","decision":"d_01JB7…","by":"llm:qwen3-32b","probs":[0.7,0.3],"source":"open_llm"}
{"type":"outcome","decision":"d_01JB7…","ts":"2026-09-22T10:00:09Z","label":"no","source":"env","weight":1.0,"delay_s":9.1,"note":"same error on retry"}
```

`ctx` 指向原始上下文（session 和步号）。view 函数改了以后，可以据此离线重新渲染（§4.8）。

标签来源决定能否进训练：

| source | 含义 | 默认进训练 |
|---|---|---|
| `env` | 环境结果：重试成败、测试结果、内容是否被重新读取 | 是 |
| `human` | 人的批准、拒绝、纠正 | 是 |
| `open_llm` | 开源权重模型给出的兜底回答 | 是，权重低于前两种 |
| `frontier_llm` | 闭源 API 给出的兜底回答 | 否，只进评测 |
| `jev` | Jev 的输出 | 否 |

Jev 的输出（概率和选择）不作为训练目标。Jev 做出决策之后观测到的环境结果照常当标签，因为这学的是环境，不是 Jev。冷启动期攒下的数据因此可以留给开源模型用（§5.5）。

隐私：写入账本前先按规则脱敏（密钥、token 等）。账本默认只存在本机；用户想主动分享时用 `ouroloop ledger export --redact`。

### 2.5 内置决策点和结果采集

| 决策点 | 类型 | 问题 | 标签从哪来 | 缺失的那一侧怎么补 |
|---|---|---|---|---|
| `retry` | noul | 这个失败的命令原样重跑能成功吗 | 实际重跑的结果 | 判"不重跑"时没有结果，靠探索 |
| `compact` | noul，逐条判断 | 这段历史以后还会用到吗 | 被删的内容之后有没有被重新读取或重新执行 | 只有被删的一侧有标签。探索时随机删掉少量置信度居中的条目；删错了最多多读一次，代价很小 |
| `tool_gate` | noul | 这条命令能不问人直接执行吗 | 人批准还是拒绝；执行后被撤销的记为负（git restore / revert，或用户要求撤销） | 自动放行的没有人工标签，靠抽检；黑名单先于模型 |
| `stop` | noul | 任务做完了吗 | 每次准备结束时跑的验证命令（退出码 0 记为正）；结束时用户确认（只标在最后一次改动之后的 stop 决策上，因为之前的决策看到的不是最终状态） | 没有验证命令、用户也不回答时没有标签；用户不说话不代表做完了 |
| `route` | choice | 下一步用哪个模型档位 | 用小档位跑过的步骤，拿整个任务的成败作弱标签（权重 0.5）：做成了说明小档位够用，没做成记为需要大档位 | 大档位跑的步骤没有结果（不知道小档位行不行）；shadow 模式下按 ε（默认 10%）把一部分步骤交给小档位，让它也有结果 |

M1 先做 `retry`、`compact`、`tool_gate` 三个：前两个完全不需要人参与，tool_gate 的标签来自人本来就要做的批准。`stop` 和 `route` 随后完成。积累速度粗估（按个人重度使用）：tool_gate 每天几百条，retry 每天几十条。

`stop` 的动作：准备结束时，验证命令没通过就把输出发回给 agent，让它接着干；没有验证命令时，stop 在 auto 模式下有把握地判"没做完"也会发回。每个任务最多发回 `max_pushbacks` 次（默认 2），验证通过时模型判"没做完"不会推翻验证结果。有验证命令的任务给 stop 攒标签，训练出的模型用在没有验证命令的任务上。

## 3. 最小 agent

- **Loop**：消息 → LLM → 工具调用 → 执行 → 结果追加进历史 → 重复，直到 LLM 结束这一轮。决策点挂在五个位置：

  | 挂点 | 决策点 |
  |---|---|
  | 调用 LLM 前 | `route` |
  | 执行工具前 | `tool_gate` |
  | 工具报错后 | `retry` |
  | 上下文接近上限时 | `compact` |
  | 准备结束时 | `stop` |

- **工具**：read、write、edit、bash，和 Pi 一样。
- **模型接口**：支持 OpenAI 兼容接口（vLLM、Ollama、OpenRouter、智谱等）和 Anthropic Messages API，只抽象出 `chat(system, messages, tools)` 和 `complete(system, prompt)` 两个方法。
- **扩展**：配置里写导入路径（`module:name`），就能换成自己的决策点、决策后端、模型接口、研究 agent 和训练器，不需要插件目录。自己的 harness 通过 Python 的 `rt.ask(...)` 或 HTTP 的 `ouroloop serve` 调用决策点。
- **会话**：`sessions/<id>.jsonl` 保存完整记录，账本通过 session id 关联。
- v0.1 不做 TUI，终端流式输出就够了。

## 4. evolve 管线

```
ledger → build → check → train → eval → calibrate → promote
```

每一步都对应这两天在 TDE 上手动做过或踩过坑的事。

### 4.1 build：建数据

- 关联 decision、escalation、outcome 三种事件。超过结果时间窗仍然没有结果的决策丢弃。
- 按来源过滤（§2.4）后生成 target：`env` / `human` 用 one-hot，多个信号冲突时取加权分布；`open_llm` 用软分布，权重低。
- 转成 TDE 的 `DecisionExample`：`dataset` 填决策点名，`source_id` 填 session id，`meta` 记 spec_hash、模型版本、route、倾向分、来源、agent_model。
- **切分**：最近一段时间的数据（默认最后 15%）作测试集，之前的按 session 哈希分成训练 85% / 校准 15%。同一个 session 不跨切分，对应 TDE"按源样本 id 切分"的规则；按时间留出，测的是模型在将来数据上的表现。
- **回放**：按比例 α 混入 TDE 的通用训练数据（默认 1:1，M2 时实测调整）。这是为了防止 Exp 010 那种回退：训练混合变了之后，v0.1 测试集从 88.7% 掉到 83.4%。
- 倾向分只记录，v0.1 默认不做重要性加权。

### 4.2 check：数据检查

任何一项阻断没通过就不训练。

| 检查 | 默认阈值 | 为什么 |
|---|---|---|
| 截断率 | > 5% 阻断 | Exp 005 的多跳题 66% 超过 448 token，被截断后题目根本没法答 |
| 标签和候选位置的相关性 | 某个位置的标签占比偏离均匀分布 > 10 个点告警，> 25 个点阻断 | HotpotQA 的支撑段落固定排在前面，96% 可以靠位置猜对 |
| 标签平衡 | 少数类 < 5% 告警 | |
| 跨切分重复（按 view 哈希） | 告警，报告比例 | 真实日志里同一个报错会在不同 session 反复出现，这是部署时的分布，不算泄漏；合成或改写的数据才需要为零 |
| 与评测集重叠 | > 0 阻断 | 同 `scripts/contamination_check.py` |
| 样本量 | 训练集不到 1,000 条，或测试集检不出 3 个点的差异，就不训练，继续攒数据 | 按 `min_detectable_difference` 算，检出 3 个点大约要 900～1,750 条测试决策，取决于两个模型的分歧率（10%～20%） |
| spec 版本 | 同一决策点混有多个 spec_hash 时告警，默认只用当前版本 | |

### 4.3 train：训练

- 从当前冠军 `init_from` 做专精阶段，训练数据是各决策点的数据加回放数据。损失沿用 TDE 的默认设置（soft-CE，score 加 RPS，再加 perm-KL），样本按来源加权。
- 显存配置按显卡自动选。Exp 008 起 state 是 1024 token，在 8 GB 卡上 batch 4 会 OOM，改成了 batch 2 × 累积 16 + 梯度检查点。
- 可以在本地跑（CUDA / MPS），也可以在 GPU 主机上跑（rsync + ssh）。任务队列用 pid 文件管理，不用 `pkill -f` 按模式杀进程，因为模式会匹配到 ssh 命令本身。

### 4.4 eval：评测

- **主终点**：每个决策点在时间留出测试集上的 Brier。挑战者对比冠军，按 session 分组做配对 bootstrap（`paired_bootstrap(groups=...)`）。Brier 差（挑战者减冠军）的 95% CI 上界 < 0，才算挑战者更好。
- **产品指标**：风险 r 下的自动执行比例。阈值在校准集上拟合，在测试集上报告实际风险和覆盖率。这是 RSI 最终要展示的曲线。
- **回归检查**：
  - 其他决策点的 Brier 不能显著变差。
  - TDE v0.1 测试集（6,000 条）的准确率最多下降 1 个点。
  - JevBench 公开题只作参考：hard 层只有 111 题，CI 约 ±9 个点，四次运行的 28.8%～34.2% 全在噪声范围内。
- **控制项**：沿用 TDE 的控制块，包括无 state 对照（看模型有没有用到 view）和候选换序一致性。
- **回归集**：高置信判错的决策和失败的任务进入回归集。回归集按版本封存，从下一轮 evolve 才开始用，进行中的评测不改。受保护样本由规则或人来标，不由提改动的模型决定。

### 4.5 calibrate：校准

- 每个决策点用 `fit_temperature` 在校准集上拟合自己的温度。TDE 的 `BucketTemperature` 是按原语和 K 分桶的，ouroloop 自己维护一张按决策点的温度表，TDE 那边不用改。
- 再按 §2.3 拟合阈值 τ。每次训练后都要重做：Exp 007 专精后 ECE 从 0.098 升到了 0.125。

### 4.6 promote：晋升

模型、spec、prompt 三类候选共用一个晋升门，按顺序检查：

1. **证据完整**：计划内的样本都有有效结果。缺失的不按 0 分算，这时判"证据不足"，不判"拒绝"。
2. **受保护样本全部通过**：硬约束，不参与平均。比如 tool_gate 的危险命令必须拦住。
3. **不退步**：各决策点、各切片不能显著变差；通用回归集的下降在容忍度以内（§4.4）。
4. **有显著改善**：主指标配对 bootstrap 的 95% CI 不含 0。没有带来变化的改动不替换冠军。

没通过的候选不替换冠军，但留在注册表里，可以当下一个实验的起点（§5.1）。评测用的数据对提改动的一方始终不可见。
- **注册表**：`models/<version>/` 存 checkpoint、温度、阈值和 `card.json`。`card.json` 记父版本、数据时间窗、账本哈希、全部指标和晋升决定，所有版本连起来就是谱系树。`ouroloop models rollback` 一步回退。
- **部署**：harness 热加载新冠军。v0.1 离线评测通过后直接切换；在线 canary（新旧模型并行跑一段时间）放到以后做。
- **触发**：手动运行 `ouroloop evolve`，或者某个决策点新增 N 条有结果的记录（默认 500）后自动触发。

### 4.7 冷启动

- 从 TDE 的通用 checkpoint（Exp 010 或之后的版本）出发，新决策点先零样本运行，并处于 `shadow` 模式。
- 规则明确的决策点（比如 tool_gate 的危险命令）可以用规则生成数据，做法同 TDE 的 synth_policy。
- 可选：用公开的 agent 轨迹，里面有重试记录和最终测试结果。只用开源模型生成的轨迹，并检查许可；闭源模型生成的轨迹默认不用。

### 4.8 候选的三种类型

研究 agent（§5）提出的候选分三类，晋升规则都同 §4.6：

| 候选 | 例子 | 怎么评测 | 成本 |
|---|---|---|---|
| 决策点 spec | 改问题措辞、criteria 或 view，比如把长堆栈压缩后再放进 view | 账本离线重放：按 `ctx` 重新渲染 view，过一遍当前模型，比 Brier | 几乎为零 |
| 训练配方 | 数据过滤规则、来源权重、回放比例、训练步数 | 按新配方训练挑战者，按 §4.4 评测 | 一次训练，不到一小时 |
| harness prompt | system prompt、工具描述、skill | 在任务集上运行，按通过的检查项比例打分；先用小子集筛，最好的一个再跑全量和 held-out | 高，要跑 agent |

- spec 和 prompt 的冠军存在工作区的 `harness/` 下（JSON，带历史），工作区本身可以用 git 管理。
- 任务运行按（prompt、模型、任务、第几次）只记一次，之后复用：重跑不算新样本，也不能挑最好的一次。
- 每个候选在 `experience.jsonl` 里记一条：假设、diff、逐样本配对结果、结论（改善、退步、有好有坏、持平、证据不足、没跑成）。没晋升不等于没用。

## 5. 研究 agent

研究 agent 把 TDE 实验里"人定方向、Claude 执行"的工作自动化：LLM 接替 Claude，提出假设、执行实验；Jev 接替人，判断条件、排序、决定是否继续。训练出来的 TDE 用在业务里，至少一开始不回到研究循环。默认实现针对 ouroloop 自己的循环，也就是改进决策模型；开发者也可以把它用在自己的研究场景里，比如蛋白质折叠，只需提供自己的候选类型和评测函数。

### 5.1 一轮的流程

| 步骤 | 谁来做 | 内容 |
|---|---|---|
| 1. 看报告 | 框架 | 汇总最新评测：各决策点的 Brier 和自动执行比例、按原因分组的判错样本（只取训练段和开发段）、经验记录 |
| 2. 选方向 | 决策模型（`pick_target`） | 从错误分组里选这一轮要修的一组 |
| 3. 提候选 | LLM | 读这组样本和相关经验，写几个候选（改写问题、改训练配方或改 prompt），每个附上修改假设和资源估计 |
| 4. 筛候选 | 决策模型（`worth_trying`） | 逐个判断值不值得花评测预算，只评值得的，另按探索率随机抽几个 |
| 5. 评测 | 框架 | 按 §4.8 的方式评测 |
| 6. 晋升 | 框架 | 走 §4.6 的晋升门 |
| 7. 记录 | 框架 | 写经验记录；把 agent 自己的决策和结果（候选有没有晋升）写进账本 |

第 7 步把 Jev 的每个判断和后来的结果都记下来。v0.1 里 TDE 不接管研究决策；以后研究决策的数据攒够了，可以让 TDE 在这些点上接管 Jev（§9）。

除了这两个固定的决策点，LLM 在任何一步遇到需要判断的地方，都可以通过问题转换（§5.3）去问决策模型。

从 TDE 实践来的三条规则（详见 `docs/RESEARCH_LOOP.md`）：

- **起点不等于冠军**：评测过的候选都可以当下一个实验的起点。Exp 010 在 v0.1 上回退 5 个点，当不了冠军，却是 Exp 011 最好的起点。
- **一次只改一处**：Exp 010 同时改了底座、state 长度和数据版本，只能再排 Exp 008 / 009 拆开。候选默认只改一处；合并几处改动时，自动排上拆分对照。
- **好得反常也要审计**：HotpotQA 96% 是位置泄漏，不是好消息。报告里异常高和异常低的切片都要标出来。

### 5.2 研究决策点

| 决策点 | 类型 | 问题 | 标签 |
|---|---|---|---|
| `pick_target` | choice | 修哪个错误分组最可能带来晋升 | 这一轮针对它的候选有没有晋升 |
| `worth_trying` | noul | 这个候选能通过晋升门吗 | 评测后的晋升结果；没评的靠探索补 |

这两个点由 Jev 判断，相当于 TDE 实验里人做的事。每次判断连同结果都记进账本。判错只会浪费评测预算，不影响晋升。

### 5.3 问题转换

LLM 的判断需求通常是一句自然语言，比如"接下来先清洗数据还是先调学习率"。问题转换模块把它变成决策模型能回答的格式：

- 给 LLM 一个 `ask` 工具，参数就是 typed question：压缩后的 state、问题、类型（noul / choice / score）和候选。LLM 调用这个工具，转换就完成了。
- 框架负责检查和补全：候选必须互斥；自动加一个"都不合适"的候选，因为 Jev 不能弃权；state 超过后端上限时截断并记录。
- 每次询问都写进账本。LLM 给每个问题标一个类别，同一类别反复出现后，就能像固定决策点一样用结果来训练和校准。

### 5.4 可替换的接口

```python
class ResearchAgent(Protocol):
    def step(self, ws: Workspace) -> list[Candidate]: ...
```

- `Workspace` 只提供四样东西：报告（训练段和开发段）、经验记录、`ask()`（经问题转换后调用决策后端）、`llm()`（调用推理模型）。测试段和 held-out 数据不在里面。
- 评测、晋升、记录都由框架负责。agent 只能提出候选，绕不过晋升门。
- 默认实现 `BasicResearcher` 就是 §5.1 的流程。开发者可以在配置里换成自己的类（`[research] agent = "mypkg:MyResearcher"`），换推理模型、决策后端、探索策略。
- 候选只改声明式的内容（问题文本、配方参数、prompt 文本），不改代码。需要改代码的探索，由开发者在自己的 agent 里实现。
- 自定义候选类型（比如蛋白质折叠里的实验方案）要同时提供评测函数，晋升规则仍同 §4.6。

### 5.5 决策后端与冷启动

TDE 和 Jev API 都可以直接接入，接口相同（§2.2），每个决策点可以单独选后端：

- **研究循环**：研究决策点和 `ask` 的问题都交给 Jev。
- **业务决策点**：开始时可以先交给 Jev；TDE 在某个决策点上攒够结果，在同一测试段上比当前后端更好（过 §4.6 的晋升门），就接管这个点。
- 留在 Jev 上的决策点，权重训不了，可以改进问题和 prompt；换到 TDE 的决策点，权重、问题、prompt 都能改。

### 5.6 端到端走一遍

以 retry 为例：

1. 报告显示，retry 在开发段的判错中有一组是"缺依赖被判成可重试"（`ModuleNotFoundError` 一类）。
2. `pick_target` 从三个分组里选中这一组。
3. LLM 提两个候选：A 改写 criteria，把"缺模块、缺命令"写进 false 一侧；B 把 view 里的堆栈压到最后 20 行。
4. `worth_trying` 判 A 值得试、B 不值得；B 按探索率被抽中，也照样评测。
5. 两个候选都走离线重放：按 `ctx` 重新渲染 view，过一遍当前模型，在测试段上和冠军比 Brier。
6. A 通过晋升门成为新 spec，B 被拒。
7. 写两条经验记录；账本记下 `pick_target` 的标签"晋升了"，`worth_trying` 对 A 是"通过"、对 B 是"没通过"。

冷启动时第 2、4 步由 Jev 回答；如果 retry 这个决策点本身也用 Jev，第 5 步重放时也调用 Jev。流程不变。

## 6. 怎么证明 RSI 有效

- **主实验**：日常使用 2～4 周。retry、compact、tool_gate 先用 shadow 模式，每攒够 N 条结果，研究 agent 自动跑一轮。按决策点画出"风险 5% 下的自动执行比例随进化代数的变化"，同时报告实际风险，必须 ≤ 5%。
- **对照**：冻结的通用 TDE（不进化）；全部交给 LLM（作为成本基线）；手写规则（比如"超时就重试"）。
- **离线回放**：冻结一份账本快照，用每一代模型重放一遍，得到可复现的曲线。开源发布时附一份脱敏或合成的账本。
- **成本**：报告省下的 LLM 调用次数、费用和延迟。
- **领域案例**（v0.1 之后）：选一个有真实结果的领域场景，比如工单路由或告警分诊，验证领域数据下的效果。

## 7. 轻量约束

```
ouroloop/
  types.py  decision.py  policy.py  ledger.py  runtime.py  chat.py  config.py  serve.py  cli.py  demo.py
  backends/     mock.py  jev.py  tde.py  llm.py  http.py
  providers/    anthropic.py  openai_compat.py
  agent/        loop.py  tools.py  prompt.py  suite.py
  points/       retry、compact、tool_gate、stop、route 和命令黑名单
  adapters/     claude_code.py
  evolve/       build.py  checks.py  train.py  replay.py  gate.py  calibrate.py  registry.py  pipeline.py
                harness.py  experience.py
  research/     agent.py  basic.py  ask.py  loop.py
```

| 项 | 约束 |
|---|---|
| Python | ≥ 3.11（用标准库 `tomllib` 读配置） |
| 默认依赖 | 只有 `httpx` |
| `[tde]` | TDE（git 依赖）、torch、transformers |
| `[train]` | `[tde]` 再加 datasets、scikit-learn |
| 配置 | 一个 `ouroloop.toml`，写模型、决策点、策略 |
| 命令 | `run "任务"`、`suite`、`decide`（手动测试决策点）、`serve`、`ledger`、`research`、`evolve`、`calibrate`、`models`（含 `--rollback`）、`hook`、`demo`、`smoke`、`replay` |
| 代码量 | 不设硬上限。每个模块要能一口气读完（一般不超过 300 行）；完整的 v0.1 预计 5,000～6,000 行，不含测试 |

## 8. 里程碑与验收

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **M0 决策层 + 端到端骨架** | 决策点 API；策略（三种模式、阈值、探索、倾向分）；JSONL 账本和事件关联；`tde` / `jev` / `llm` 后端；问题转换 `ask`；`ouroloop decide`；用 mock LLM 和合成账本搭一个贯通全流程的骨架 | 一条命令用 mock 跑通 §5.6 的一轮；换研究 agent 或决策后端只改配置；pytest 不下载任何东西就能跑；在 GPU 主机上用 Exp 010 checkpoint 对三种原语调用 decide；`jev` 后端通过真实 API 冒烟测试（key 从环境变量读取）；Jev 的输出和 `frontier_llm` 的回答都进不了训练目标 |
| **M1 最小 agent** | loop、四个工具、两种模型接口、挂点、retry / compact / tool_gate 三个决策点和结果采集，默认 shadow | 用开源模型（Ollama / vLLM 上的 Qwen）或 Claude 完成约 10 个脚本化的小编码任务；每个决策点都有决策和结果；`ledger stats` 能显示各决策点的数量、标签覆盖率、shadow 一致率 |
| **M2 evolve + 研究 agent** | §4 的六步、注册表、回滚、在 GPU 主机上执行；`BasicResearcher`（§5）提出 spec 和训练配方候选 | 埋了信号的合成账本能晋升；埋了缺陷（截断、位置泄漏、跨切分重复）的账本被 check 挡住；标签随机打乱的账本跑 20 次，误晋升不超过 1 次；研究 agent 在真实账本上自己跑完一轮，并把自己的决策写进账本 |
| **M3 接入** | `serve`；Claude Code hooks 适配器（Stop、PreToolUse、PostToolUse）；上手文档。Pi 扩展以后再做 | 从 `pip install` 到记下第一条决策不超过 5 分钟 |
| **M4 首个 RSI 结果** | §6 的主实验 | 研究 agent 无人干预连续运行（比如两周）；至少两个决策点的自动执行比例随代数上升，实际风险 ≤ 5%，并且优于冻结基线 |
| **M5 prompt 进化** | §4.8 的 prompt 部分：任务集（从 M1 的小任务扩展）、先筛后比、经验记录 | 跑通一轮"生成 → 筛选 → 全量加 held-out → 晋升或拒绝"；只改空白字符这类无效改动不会被晋升 |
| **最后：回放测试** | 用 `evals/replay_tde` 的 11 个真实决策点，在 Jev 上跑一遍 | 跑通；报告一致率和参考答案的平均概率（样本太少，只作可行性参考） |

进度：

- M0 已完成（2026-09-22）：22 个离线测试，加一个真实 Jev 冒烟测试。尚未完成：在 GPU 主机上用 TDE checkpoint 做真实推理检查。
- M1 的 harness 已完成（2026-09-22）：agent loop、四个工具、Claude 和 OpenAI 兼容接口的工具调用、retry / compact / tool_gate 及其结果采集，共 31 个离线测试。尚未完成：用真实 LLM 跑约 10 个小编码任务，需要先有 LLM 的 key，或在 GPU 主机上部署开源模型。
- Claude 这类要求历史只追加的接口，compact 只记录决策、不删内容；以后改用服务端的上下文编辑。
- M2 已完成（2026-09-22）：
  - 训练管线：建数据、数据检查、三种训练器（签名小模型、本机 TDE、远程 TDE）、远程批量推理、校准、按决策点判断的晋升门、模型注册表和回滚。
  - 研究 agent：训练配方候选、多轮运行，以及让 LLM 把问题转成 typed question 交给 Jev 回答。
  - 离线 demo 同一轮里改写 spec 和训练模型都能晋升，共 44 个离线测试。
  - 尚未完成：在 GPU 主机上跑通一次真实的 TDE 训练。
- 真实检查（2026-09-22）：在 GPU 主机的 CPU 上用 Exp 010 checkpoint 做远程批量推理，三种原语都返回正确格式；M0 欠的这一项已完成。当时 Exp 008 正占用 GPU，所以没跑真实训练。
- M3 的核心部分已完成（2026-09-22）：
  - `ouroloop serve`（HTTP 决策服务）加 `http` 后端，让 TDE 常驻在 GPU 主机上实时回答。
  - Claude Code hooks 适配器：可选的数据来源，默认只记录，不改变 Claude Code 的行为；标签来自权限弹窗和原样重跑。
  - 共 48 个离线测试。Pi 扩展以后再做。
- stop / route 已完成（2026-09-22）：
  - stop：准备结束时先跑验证命令（`ouroloop run --verify`），没通过就发回给 agent；验证结果和结束时的用户确认都是 stop 的标签。
  - route：配了两个档位（`--small-model` 或 `[llm_small]`）才会问；auto 模式按模型选档位，shadow 模式按 ε 试用小档位，任务成败是这些步骤的弱标签。
  - Claude 接口打开了自动 prompt 缓存，agent 每轮重发的历史走缓存读取。
  - 配置里只写了部分决策点时，其余内置决策点照常注册；内置决策点的 `define` 可以省略。
  - 共 55 个离线测试。
- M5 已完成（2026-09-22）：
  - 任务集：一个任务一个 TOML 文件，隐藏测试在 agent 跑完后才写入；`ouroloop suite --validate` 不调用 LLM，检查每个任务原样不通过、参考答案全通过。
  - `evals/tasks` 里写了 10 个小编码任务（7 个开发、3 个 held-out），全部自检通过；它们也用作 M1 的验收任务。
  - harness prompt（system prompt 和工具描述）的冠军存在工作区里，`ouroloop run` 和任务集都用它。
  - prompt 候选评测：只改空白字符的直接拒绝 → 前几个开发任务上筛 → 全部任务（含 held-out）配对比较、按任务分组做 bootstrap，过晋升门；held-out 退步显著也拒绝。
  - 研究 agent 根据开发任务的失败情况（任务、最后几步工具调用、没过的检查、agent 的收尾话）提出 prompt 候选；held-out 任务不给它看。
  - 任务跑完时的检查结果作为这次运行里 stop / route 决策的 env 标签。
  - 共 63 个离线测试。
- 真实 LLM 流程测试（2026-09-22，智谱 GLM 加 Jev）：
  - `ouroloop run`：glm-5.3 为大档位、glm-5.3-flash 为小档位，决策点交给 jev-1.13.0。agent 5 轮修好一个失败的测试，用时 35 秒，验证通过；route、tool_gate、retry、stop 都由 Jev 在 shadow 模式下作答并记账，stop 由验证命令打上标签。
  - M1 验收：glm-5.3 在 10 个任务上全部满分（开发 7/7，held-out 3/3），多数 4 轮完成。11 个 stop 决策全部由检查结果打上标签。Jev 在 shadow 模式下的 stop 判断与结果一致 8/11：错的 3 个都是已完成的任务被判"没做完"（p=0.38～0.49），view 里写着 verification: none，动作记录里还有中途报过的错。由此把 stop 的 view 改成最有信息的部分在前，动作记录放最后。
  - M5 流程：用弱 agent（glm-5.3-flash，关闭思考，每个任务最多 2 轮）做基线，开发任务平均 0.857，held-out 0.750。研究 agent（glm-5.3）根据失败的任务提出 3 个 prompt 候选，Jev 估计的通过概率为 0.61、0.46、0.43。评测了 2 个：都通过了筛选，但全量运行分别让 2 个和 1 个原本满分的任务退步，晋升门都拒绝了，冠军 prompt 不变。这正是晋升门该做的事：听上去有道理的改动，没有证据就不上线。
  - 据此给经验记录补上每个候选改了哪些句子、在各开发任务上的分数变化（held-out 不给研究 agent 看），以及 Jev 的估计和是否为探索，让下一轮的研究 agent 能从失败里学。
  - 第二轮：研究 agent 读到上一轮的记录后，明确避开"加探索指引会退步"的方向，只加了一句"结束前运行入口或测试来验证"。全量运行所有任务分数不变，以"no change on any item"拒绝。两轮下来冠军 prompt 没变。
  - 在每个任务 2 轮的限制下，这个弱 agent 真正需要的是把读文件和改文件合并进同一轮：60 轮里已有 24 轮一次调用两个工具，研究 agent 还没找到这个方向。循环本身跑通了：提出假设 → 评测 → 记录 → 下一轮据此调整。
- 发布准备（2026-09-22）：MIT 许可证；英文 README 为主，中文 README 另存；版本 0.1.0，补齐打包元数据；GitHub Actions 在 Python 3.11～3.13 上跑测试；代码和示例里去掉私有主机信息（自己的主机配置放在被 git 忽略的 `*.local.toml`）；`serve` 绑定本机以外的地址时必须设置 token。TDE 是单独的项目，之后另行开源，本仓库只记录它的训练方式。
- 回放测试已完成（2026-09-22）：jev-1.13.0 在 11 个真实决策点上 11/11 与参考答案一致，参考答案的平均概率 0.87。state 是事后整理的，所以这是上限。
- 训练链路按"不真跑训练、保证代码正确"验收（2026-09-22）。"训练、看结果、再设计实验"这个循环已由 TDE 两天的实验证明可行，所以不再单独跑真实训练。验收内容：
  - 生成的三种题型的数据，都通过 TDE 自己的 schema 校验；
  - 生成的配置只用 `TrainConfig` 里真实存在的字段；
  - 本地训练器配上假的 TDE 仓库，端到端跑通；
  - `ouroloop evolve --dry-run` 在 GPU 主机上完成演练：组装回放数据，由 TDE 校验配置和数据，确认父模型 checkpoint 存在，全程不训练、不占 GPU；
  - 共 51 个离线测试。

## 9. 研究方向（v0.1 之后）

1. **提案方式自改**：LLM 改进自己的提案 prompt 和数据检查规则，用"提出的候选有多少被晋升"来评判。
2. **TDE 接管研究决策**：研究决策点的数据攒够后，TDE 在这些点上与 Jev 比较，通过晋升门就接管。
3. **反事实学习**：用倾向分做 IPS / doubly robust 训练和离线评估。
4. **和 TDE 第二期对接**：conformal / Learn-then-Test 的有限样本保证；state 可缓存的不对称注意力，让一个 state 同时回答多个决策点。
5. **社区数据**：用户自愿、脱敏后共享账本，用来训练社区版决策模型。

## 10. 风险

| 风险 | 应对 |
|---|---|
| stop、compact 的隐式标签噪声大，而且有延迟 | 按来源加权、用软标签，时间窗过后才结算 |
| 被拦下或没被选中的一侧没有结果 | 探索率加倾向分；新决策点先 shadow |
| 个人使用的数据量有限，第一次进化可能要等几周 | 冷启动数据；所有决策点共用一个模型 |
| 模型更新后 agent 行为改变，决策分布跟着漂移 | 按时间留出测试集；每次进化都重新拟合阈值 |
| tool_gate 误放行危险命令 | 黑名单先于模型；不可逆操作不允许 `auto` |
| 评测集太小，晋升等于掷硬币 | 样本量检查；按 session 分组做配对 bootstrap |
| view 里含有闭源模型生成的内容（命令、代码），能否用来训练取决于各家条款 | 账本记录 `agent_model`，evolve 可以按它过滤；发布前逐家核对条款 |
| 服务条款 | 标签来源由代码强制过滤；Jev 的输出不作为训练目标 |

## 11. 复用 TDE 的清单

| ouroloop 需要 | TDE 现有 |
|---|---|
| 推理 | `tde.inference.Decider.decide_batch`、`tde.train.predict_chunked` |
| 训练 | `tde.train.train(TrainConfig(init_from=...))` |
| 数据格式 | `tde.schema.DecisionExample` 及 JSONL 读写 |
| 校准 | `tde.calibration.temperature.fit_temperature` |
| 指标 | `tde.calibration.metrics` 里的 `brier`、`nll`、`ece_equal_mass`、`coverage_at_risk`、`aurc`、`paired_bootstrap(groups=)`、`mcnemar_exact`、`min_detectable_difference` |
| 泄漏检查 | `scripts/contamination_check.py` |
| 远程执行 | 参照 `scripts/remote.sh`、`scripts/run_queue.sh` 的做法 |

许可证拟用 Apache-2.0，与 TDE 一致（待确认）。
