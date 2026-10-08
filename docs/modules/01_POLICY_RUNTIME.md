# M1：执行模型、决策模型与运行时

> 首版目标：发布哪个能力包就实际运行哪个包，actor 身份固定，生成代码隔离执行。改造 `policies.py`、`agent/loop.py`、`runtime.py`、`stop.py` 与 `providers/`；保留旧注册目标/legacy。执行前教师审阅与 Tinker sampling 是第二版规格，本文件保留其详细设计。
>
> v0.3（2026-10-04，DESIGN v0.7）：宿主从“跑固定循环的 agent”变成“运行壳”（runner shell，DESIGN §1）：一次尝试 = 一个 worker 会话里跑包的 `agent.run`，模型只经宿主网关、工具只经宿主 broker，宿主不再向模型问任何决策（完成由两条硬规则定，`tool_gate` 作为 K 的权限留在 broker，其余决策点都在 agent 程序里），今天 `agent/loop.py` 的循环移植为 K 自带的默认 agent（种子包）。`claude_code` provider 一节不变。M0 §2“包 API v2”定义 ctx 的形状，本文定义宿主怎样提供它。
>
> 2026-10-08（瘦身第一批完成，分支 `slim/1`，[PLAN](../PLAN.md) P1）：v1 循环（`agent/loop.py`）、v1 runtime（`runtime.py` 的 `ask`、阈值查找、冠军、pin）、编码决策点（`points/`）、`SessionBinder` 与 `Services.runtime` 已删，tag `archive-2026-10-08` 上可读。现状：
> - 每次尝试都走运行壳。没有 `max_effort` 的旧 K 也走运行壳，effort 不截顶（第二批改为拒绝）；旧工作区只在 tag 上续跑，下文 PR7-13 不再成立。
> - 一次尝试的 session 行与 `.release.json` 由薄的 `Workspace`（`workspace.py`）写，`release.spec` 是冻结常量 `BUILTIN_SPEC`。rule 类 `worker.stop` 绑定时不构造决策点，serving 只在要绑定 model 策略时才建。
> - broker 的拒绝表在 `agent/deny.py`。StopGuard 在 `ouroloop/train/stop.py`，v1 循环的 stop 上下文与轨迹（`_stop_ctx`、`_trace`）在 `ouroloop/train/trace.py`，决策运行时的通用半边（`record`、`propose`、`outcome`）在 `ouroloop/train/runtime.py`，Claude Code 适配器（含 `tool_gate` 点）在 `ouroloop/train/claude_code.py`，都在 `[train]` 里，热路径不 import（`tests/test_hot_path_boundary.py`）。
> - 运行壳下，hook 在运行时抛错、答出协议之外或弄死 worker，都算基础设施（`AgentRunError`/`WorkerCrashed`，§1），不算候选的缺陷；有界的 worker 调用（草稿、探针）仍按包的缺陷算。
> - 下文“今天 `agent/loop.py`”的说法是 2026-10-04 的写法，保留原文。
>
> v0.5（2026-10-07 合入；2026-10-08 现状）：运行壳的两处 v0.5 增量（`start` 位点的经验库检索，保留；方法的只读 view，随方法库拟删）见 §1“宿主作为运行壳”末段。

## 1. 目标与装配

| target | binding / 输出 | 装配位置 |
|---|---|---|
| `worker.actor`（新增） | generation / `ChatTurn` | Agent 使用的 ChatProvider，身份来自 session pin 的 W。（v0.3）agent 程序经网关 `ctx.model` 到达它，不直接持有 |
| `worker.harness`（新增） | config / `harness_package.v1`；（v0.3）或 `harness_package.v2` | 包的工具、hook、技能与经验；代码只在固定隔离 worker 中装配。（v0.3）v2 包还可带 agent 程序，它就是循环 |
| `worker.system_prompt` | config / prompt | 合并后的系统提示，继续使用现有规则。（v0.3）默认 agent 照今天的规则合并它；带自己 agent 的包经 `ctx.view.policies.system_prompt` 读到正文，用不用是循环的事，Inspection 记是否读过 |
| `worker.finish` | config / 内置工作流配置 | 完成请求处理。（v0.3）同上，经 `ctx.view.policies.finish` 读 |
| `worker.stop` | decision / yes、no、abstain | StopGuard 的 on_finish 路径。（v0.3）默认 agent 里保留其规则路径（finish_check 回推与 verify_command）；决策模型 controller 在 v0.3 没有挂点，见下文“宿主不问决策” |
| `research.diagnose/propose` | research_step / prompt | 既有研究 provider 实际加载被 pin 的 R 配置；首版 diagnose 改为逐题契约 failure_summary.v1 → failure_note.v1，propose 读笔记集合产出 Requirement 与至多一个 patch/next_action（[M4 §1](04_RESEARCH_LOOP.md)）。（v0.3）propose 改为有界编码会话、一轮至多 `max_candidates_per_round` 个候选，M4 §1（v0.3） |
| `research.retrieve/preflight` | config / rule 或 workflow | 既有受限检索与辅助检查；不可替代核心 admission |

v3 session 同时 pin release.target_harness_ref；装配检查它与 K 许可、plan 和 provider 能力一致。新的 harness preparation baseline 只能按 M3 签发的任务/mode scope 执行，不进入 active/champion；M1 为它检查 actor renderer、工具/消息表示、controller observation 和完成协议兼容，缺能力显式拒绝，不静默关闭 controller。B 的后续采集使用 M3 针对确切 B/DistillationPlan 签发的补充 v3 permit，作为显式装配参数。扩展现有 authorize 的严格校验分支，不能因为它不同于 B 创建时的 baseline permit 就误拒，也不能接受不匹配的 release/harness/scope/mode；旧 v2 行为不变。source 运行读取 source release 的契约；学生读 student release 的契约，不能由 agent_factory 临时换掉。

新增 actor registration；扩展 `bind_release`、`_bind_one`、`Services` 和 `SessionBinder`，不要在 bench 临时给 Agent 塞一个不同的 provider。v3 的配置 `[llm]` 仅用于 bootstrap 构造 actor；已有 actor 的 release 不允许被本地同名参数悄悄覆盖。显式覆盖需新 release/比较身份。

运行时接口目标：BoundRuntime.actor -> ChatProvider；现有 ChatProvider.chat 接口保留。首版 pin 现有 provider 与推理配置，actor 原定 GLM-5.3（已配置并计价的 OpenAI 兼容接口）；2026-10-03 起先跑 Claude Opus 5.5，经下面的 `claude_code` provider（ROADMAP §3 第 2a 步）。不要求训练服务。第二版 Tinker provider 属于本模块，负责官方 renderer/采样/action schema 解析；未训练底座与 adapter 共用序列化。（v0.3）网关是 `BoundRuntime.actor` 外面的一层，不是新 provider：它接 ctx.model 的请求、校验角色、把 effort 压到 K 的上限、调 `actor.chat`、记日志、扣上限；provider 的接口与 `claude_code` 的全部行为不变。

controller 与 actor 共享当时合法 observation，默认不把 controller 当成需要 actor 自发调用的工具。on_finish hook 保证调用位置；其输出和真正执行动作分别写 DecisionTrace。独立模型的价值由同 actor 完整任务评测决定。

源方法运行同样通过 bind_release 装配；包、system_prompt 与 finish 的候选内容必须在实际工具/hook/actor 输入中生效。旧 finish_check.v1 是提示正文，不能据此填“中间件已执行”。包外 module/未知宿主 API 在预检拒绝；包内入口由固定隔离 worker 解析。第二版 source_validation 与 reviewed collection/task evaluation 分开记录，源验证无 teacher。

### `claude_code` provider

`[llm] type = "claude_code"`（`ouroloop/providers/claude_code.py`）：每次 `chat` 起一次无头 Claude Code，用本机 `claude` 的订阅登录。模型看到的是这一次的请求，外加 CLI 在订阅登录下关不掉的三样：系统提示前一行 Agent SDK 身份、账号邮箱、当天日期。

（v0.3，2026-10-05，第 0/1 阶段评审）每次调用的 `claude` 子进程在自己的 session 里起（`start_new_session`，`claude_code.run_child`）：终端的 Ctrl-C 发给前台进程组，子进程不在其中，所以只到父进程，父进程的中断处理（`task_eval.run_tasks`：在飞的尝试跑完并记录）让在飞的调用正常结束并结算，不再整批死在同一个信号下、以完整上界记成 unknown。父进程不再等的子进程由它自己杀：超时杀子进程的整个进程组（连同它起的进程），等它的线程被中断时（并发 1，尝试跑在主线程）同样，解释器退出时杀仍在跑的子进程。测试 `test_claude_code_provider.py::test_a_terminal_interrupt_reaches_the_parent_and_not_the_claude_child`、`test_a_timeout_kills_the_child_and_what_it_started`。


- 配置：
  - `model`：必填，如 `claude-opus-5-5`；用完整 id，作答模型的核对才生效。
  - `effort`：启动检查要求必须写（low、medium、high、xhigh、max），每次都传 `--effort`，不让 CLI 的设置决定它；W0 把它 pin 进推理配置。
  - `max_tokens`：一次模型请求的输出上限，经 `CLAUDE_CODE_MAX_OUTPUT_TOKENS` 传给 CLI。一次 CLI 调用最多发 5 次请求（输出触顶后 CLI 自行续写至多 3 次，关不掉；加一次结构化输出重试），启动检查要求 5 × `max_tokens` 不超过 `[pricing] max_output_tokens`。
  - `timeout`：每次调用的秒数，默认 600。
  - `binary`：默认 `claude`，从 PATH 找。`workdir`：默认系统临时目录下的 `ouroloop-claude-code`，必须为空。二者都写进 W0 的 actor 配置：写成 `/Users/`、`/home/`、`/private/` 下的绝对路径，release 导出会被拒。
  - `retries`：默认 2；计费层接管时置 0，由计费层逐次入账。
- 调用：`claude -p --output-format json --model M --effort E --tools "" --safe-mode --setting-sources "" --strict-mcp-config --no-session-persistence --disable-slash-commands --system-prompt S`，提示从 stdin 读入，工作目录不空则拒绝调用。
- 子进程环境：
  - 去掉全部 `ANTHROPIC_*`、`CLAUDE_CODE_*`、`CLAUDECODE*` 与 `MAX_THINKING_TOKENS`：环境里的 key 用不上，上层 Claude Code 会话传下的变量也不继承（`claude setup-token` 的 `CLAUDE_CODE_OAUTH_TOKEN` 因此不可用，须用 `claude auth login` 的本机登录）。
  - 设 `CLAUDE_CODE_MAX_RETRIES=0`（重试只在计费层，逐次入账；CLI 在首字节超时或连接失效时仍可能自行重发一次）、`CLAUDE_CODE_NO_MODEL_FALLBACK=1` 与 `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=1`（不换模型作答）、`CLAUDE_CODE_DISABLE_ATTACHMENTS=1`（不按 @ 附加文件、不加每轮提醒）、`MAX_STRUCTURED_OUTPUT_RETRIES=1`、`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`。
- 请求：
  - 只有一条用户消息、没有工具（R 的每一步）：提示原样作为这条消息，回复文本就是 `text`。
  - 其余（actor）：一条由多个文本块组成的用户消息，经 `--input-format stream-json` 发送（输出因此是 `stream-json --verbose`，读最后一行 result）。第一块是回复格式说明与所给工具的 JSON，说明里写明请求之外附上的邮箱与日期不属于任务；其后每条历史一块 JSON，最后一条之后不再有块。下一回合原样重发前面的块、只在末尾追加，所以前文从缓存读取，不再整段写入（冒烟实测：整段作一块时一道题写缓存 25 万 token、读 4 万）。JSON 里的 `@` 写成转义，CLI 不会把它读成要附加的文件。`--json-schema` 约束回复为 `{"text", "tool_calls": [{"name", "args"}]}`，工具名限于所给工具。调用 id 由 provider 生成；有工具调用时 stop 为 `tool_use`，否则 `end`。
- 用量：按 Anthropic 口径记 CLI 的 `usage`（`input_tokens`、`output_tokens`、`cache_read_input_tokens`、`cache_creation_input_tokens`），另记 `cli_cost_usd`（CLI 报告的 API 等价费用）。一次 CLI 调用内部的多次请求记的是总和。
- 结局：
  - 拒答（`stop_reason` 为 refusal）记 stop `refusal`；输出触顶（`api_error` 为 max_output_tokens，或 CLI 已自行续写：输出达到 `max_tokens`，或普通问答的 `num_turns` 多于 1）记 stop `max_tokens`。结构化回复的轮数不作判断：回复没按格式时 CLI 会再要一次，轮数变多但回复完整。与 API provider 相同，由调用方处理：续写时 `result` 只剩最后一段，不能当完整回复。
  - 订阅上限（“You've hit your … limit”）、额度用尽、未登录或凭据失效、HTTP 401/403、找不到 `claude`、工作目录不空：`ProviderAccountError`，活动暂停（[M5 §3](05_EXECUTION_BUDGET.md)）。
  - 实际作答的模型（`modelUsage`）不是所配模型：`ProviderAccountError`，活动暂停；这次调用已经发生，预留记 unknown，不释放。
  - 自称暂时的限流或刷新冲突（“not your usage limit”“usually transient”）、过载、429、5xx、超时、回复不合格式：可重试的 `ProviderError`，其中限流、过载与 429 记为未计费；其余：不可重试的 `ProviderError`。
- `supports_history_edits = True`：历史每次整段重发，没有需要原样回放的 thinking 块。
- 启动检查：`effort` 已写；`claude` 在 PATH 上；`claude auth status`（在空工作目录里运行）显示已登录，`authMethod` 为 `claude.ai`、`apiProvider` 为 `firstParty`、没有 `apiKeySource`。按次计费的 Console 登录或设置里的 key 都被拒绝。
- 与原生 API 的差别：工具调用经结构化输出，不是原生 tool use；历史整段重发，缓存命中多少由冒烟实测；请求带有上面三样附加内容。
- 账号若开了 extra usage，套餐额度用完后 CLI 改用付费额度继续，不报错：运行前由用户确认已关闭（ROADMAP §3 第 2a 步）。

### 宿主作为运行壳（v0.3，2026-10-04）

一次尝试的流程：M3 的 `run_attempt`（`evolve/task_eval.py`）建 episode 与 Budgeted 工具面（同今天）→ 装配 W：pin 的 actor、包、硬上限（`agent/runner.py` 的 `PackageAgent`）→ 包带 `agent` 条目或含代码时起 worker 会话并 `load` 运行 bundle，没有时取 K 的默认 agent → 宿主向 worker 发 `{"op": "run", "entry", "task", "roles", "view", "limits", "skills"}`，worker 内 `run(ctx, task)` 开始 → 运行中 worker 以 `{"op": "model_call", id, role, effort, messages, tools}`、既有的 `{"op": "base_call", id, name, args, read_only}`、`{"op": "skill_read", name}` 与 `{"op": "mark", kind, data}` 向宿主要模型、工具、技能与记一笔自述，宿主以 `model_result`/`base_result`/`skill_result`/`mark_result` 答复，每条答复附 `limits` 快照 → `run` 返回字符串即 `{"op": "result", "value"}`，宿主记 `end` 事件（stop、turns；硬上限时另有 `limit`）、结束会话、评分（同今天 `episode.score()`）。协议仍是一行一个 JSON、有界消息；今天 `request_seconds` 限一条请求，v0.3 的 `run` 请求贯穿整次尝试，它的时限就是 `max_seconds`，内层每条 `model_call`/`base_call` 的时限沿用 provider 的 `timeout` 与 `WorkerLimits.request_seconds`。

（v0.3，2026-10-04，实现与设计的出入）**放置。** 包在哪跑由包定（`PackageAgent.placement`）：带 `agent` 条目的包，或 v1 包但含代码（tools/hooks，M0 §2 运行判定），**整个默认 agent 在隔离 worker 里运行**——宿主 `load` 时把 `agent/default_agent.py` 的同一份字节（`default_agent.seed_files()`）加进加载的 bundle，包身份不变（它不是包的文件，是 K 的）；只含文本的包（含 v0.1/v0.2 的 W*、空包）由默认 agent 在宿主进程内经 `HostContext` 运行。两种放置用同一组 Gateway、AttemptBroker、SkillReader、Marks，记录相同（PR7-07）。宿主在 `task` 之后写一条 `agent` 事件 `{entry, placement, roles}`，报告由它读出入口与放置（§4）。**分派。** 只有 K 冻结了 `max_effort` 的 release（v0.3 起的新冻结）走运行壳：`task_eval.make_agent_factory` 经 `agent/runner.py` 的 `k_max_effort`/`release_max_effort` 读 K，有则构造 `PackageAgent`，没有则照旧构造 v1 循环的 `agent.loop.Agent`（PR7-13：v0.1/v0.2 的工作区不迁移，`step_limit` 等旧 termination 继续出现）；smoke phase 不绑 release，由 `bench.smoke_max_effort` 读活动的 active release 或配置的 preset。导出的 app 同理，带 `max_effort` 则在运行壳下跑（`app.py`）。

**网关**（`ctx.model`，`agent/gateway.py` 的 `Gateway.model`）。宿主收到 `model_call`：先查形状——`messages` 是带字符串 `role` 的对象列表、assistant 条目的 `tool_calls` 是带字符串 `name` 与对象 `args` 的列表、tool 条目带结果列表、`tools` 是带字符串 `name` 与对象 schema 的列表（`gateway.malformed`），不对 → 答 `is_error`（包可捕获的错误），记一条 `protocol` 事件，不调模型、不入账、不抛到宿主——与 worker 的 `base_call_handler` 对畸形工具调用的处理相同，模型调用是同一种情形；角色不在 manifest `roles` 里、`effort` 不是 `EFFORTS` 里的名字 → 同样答 `is_error`、记 `protocol`（草稿 smoke 本应先抓到它，M0 §3）；`effort` 高于 K 的 `max_effort` → **压到 `max_effort`**，不拒绝、不报错，记录里 `effort` 写实际用的档、`requested_effort` 写包请求的（缺省 None，调用用 W0 pin 的默认档，记录里 `effort` 写 provider 自己 pin 的档）；然后按角色扣 `max_steps`（act）与 `max_model_calls`、查 `max_seconds`（都在调模型**之前**），调 provider 的 `chat(system, messages, tools[, effort])`——bench 里这个 provider 是 `MeteredProvider`，计量（M5 §2 的预留与入账）与今天逐次相同，`effort` 关键字只递给签名接受它的 provider（`claude_code`），三参数的 fixture/manual provider 不收——然后写两条记录：今天的 `assistant` 事件（加 `role`、`effort`、`requested_effort`、宿主生成的 `call_id`、`seq`），与一条 `gateway_call`（M0 §2，带 `attempt_id`、`model_identity_ref`、messages 的前缀计数与尾段、tools 的名字与 schema 摘要、response、usage、elapsed），全部 specs 在首次出现或变化时另写一条 `tool_specs`；累计输出 token，超过 `max_tokens` 的那次调用照常入账、记日志，响应不交给包（`LimitHit`）；最后把 `{text, tool_calls, stop, limits}` 还给包。`messages` 的 system 由包自己放在 messages 里（第一条 `role=system`），宿主不注入也不改写：compaction 属于默认 agent（`agent/default_agent.py` 的 `_compact`），不属于网关。网关不缓存、不合并、不重排消息；它看到什么就调什么、记什么。`aux` 调用照常计量、扣上限、记日志，并按角色累计（`Gateway.by_role`）。（第二版的教师在这一层介入，见 §2 的“第二版”一段。）

**broker**（`ctx.tool`，`agent/gateway.py` 的 `AttemptBroker.call`）。与今天完全相同（`agent/worker.py` 的 `Broker`）：只接 K 授权的基础工具，tool_gate 的规则否决（原 `agent/loop.py` 的 `_deny`，拒绝表）移到这里，然后是 `worker.Broker` 的基础工具与只读检查（hook 的调用 `read_only=True`）、Budgeted 工具面自己的权限与预算检查；每次调用写今天的 `tool` 事件与一条 `broker_event.v1`（`gate.decision` 为 `run`/`veto`/`refused`，拒绝时带 `veto` 或 `reason`），扣 `max_tool_calls`，输出按 `max_output_chars` 截断，刷新 `ctx.view["events"]`；工具面报预算耗尽时记下这次拒绝后以 `LimitHit` 结束尝试。包的工具封装不再是宿主暴露给模型的 ToolSpec（那是默认 agent 对 v1 `tools` 条目的做法），而是 agent 程序自己在 `ctx.tool` 外包一层代码；broker 看不到封装，只看到基础调用——所以“一个复合工具藏多次调用”不可能：每次基础调用都是一条 broker 事件、一次计数。

**宿主不问决策**。今天 `agent/loop.py` 顶部列的五个决策点，v0.3 的宿主一个都不问模型：`stop` 由完成协议取代——agent 返回即完成，硬上限到即终止，宿主不再问“任务真的完成了吗”（`agent/runner.py` 的 `PackageAgent.run`：返回，或 `LimitHit`）；`compact`、`route` 作用于传给 ctx.model 的 messages，`retry`（失败 bash 的自动重跑，v1 循环的 `_retry`）作用于 agent 自己发出的工具调用，三者都在 agent 程序里，网关与 broker 不做（默认 agent 的实际做法：compaction 保留为一条规则——`_compact` 按 `CONTEXT_CHARS`/`KEEP_RECENT` 丢旧的 read/bash 输出，不再问模型哪条还要；route 只剩一个模型、一个 tier；bash 重跑没有模型可问“yes”，不保留——业务工具面本就没有 bash；带自己 agent 的包随意）；`tool_gate` 留在 broker，是 K 的权限与规则否决，不是向模型要的决策。决策模型 controller（`worker.stop` 的 decision binding，C3）在 v0.3 没有挂点：ctx 没有决策入口（M0 §2 ctx 只有五个名字），加一个是宿主 API 的变化，要经工程契约；C3 本来就 not_run，其含义不变。默认 agent 作为 K 的代码运行在宿主进程里时，技术上仍可以照今天的路径调 `rt.ask("stop")`，但为了让“默认 agent 复制进包”与“默认 agent 在宿主里”行为一致（下文 PR7-07），默认 agent 的 v0.3 版只保留 StopGuard 的**规则**路径（finish_check 回推、verify_command、pushback 上限），不调决策模型；旧 v2 release（没有包或 v1 包且运行时仍是 v1 循环的工作区）不受影响。

第二版：把决策点（stop/retry/compact/route/tool_gate）挪到网关/broker 边界，让 controller 在 C3 有输入与真值时在那里挂上、由宿主问它；v0.3 不做，这一段是计划不是规格。

**硬上限**（M0 §2 的五个维度）由宿主在网关与 broker 计数，到任一上限：宿主不再答复 worker 的请求，取消在飞的 worker 请求（已发出的模型调用照常入账，它的响应记进日志、不交给包），结束会话；尝试的 termination 为 `budget`，`limit` 字段写 `act_calls/model_calls/tool_calls/seconds/tokens` 之一，最终消息为空串；环境照常评分（与今天 `surface.exhausted` → `budget` 一致，`task_eval.run_attempt`）。旧值 `step_limit` 只出现在旧记录里。实现：`worker.LimitHit` 从网关或 broker 的处理器抛出，`serve` 结束会话后再抛，`PackageAgent.run` 记 `end`（带 `limit`）并以空消息返回；进程内运行时它以 `HostFault`（BaseException）穿过 agent 代码，`except Exception` 吞不掉；worker 在 `max_seconds` 到点时的崩溃算 seconds 上限，不算基础设施。理由：让 agent 在上限前“优雅收尾”需要宿主给它余量，余量就是第六个上限；`ctx.limits` 已经把剩余量给了它，收不收尾是它的事。

**失败语义**。worker 崩溃（`WorkerCrashed`）或 `run` 在运行时抛出（worker 答 `{"op": "error", "kind": "package"}`）→ 尝试无效、failure_class `infrastructure`、termination `harness_error`，按 M5 §3 的唯一重试策略补跑一次（Leon 2026-10-04 的决定；今天包代码抛出记 `candidate_defect`，`task_eval.py:176-177`，v0.3 起这条只保留给装配期——包装不上、工具重名、manifest 与 bundle 不符仍是 `candidate_defect`，`task_eval.py:166-168`）；草稿检查期间抛出（import、接口 smoke、tests）→ 草稿失败，回传 R、占一次修订（M0 §3）；硬上限 → termination `budget`；供应商账号错误 → 照今天上抛暂停。确定性的崩溃由草稿 smoke 在付费前抓住，这是把运行时抛出记作基础设施的前提（M0 §3）。agent 返回的不是字符串（None、dict）→ 协议错误，等同抛出。

**默认 agent 即种子包**。今天的循环（`agent/loop.py` 的 `Agent.run`：compaction、单层 route、`provider.chat`、工具执行、三个 hook 挂点、StopGuard 的规则路径、pushback 上限、`HOOK_RESULT_CHARS` 截断）移植为 `agent/default_agent.py`，只用 ctx 的五个名字加 `ctx.mark` 写成、只用标准库、不 import ouroloop，随 K 发布。它有两个用途：没有 `agent` 条目的包（全部 v1 包，含 v0.1/v0.2 的 W*）由它运行——只含文本的在宿主进程内，含代码的整个在 worker 里（上文“放置”），经同一网关与 broker，行为与 v1 宿主相同；R 要改循环时，第一步把它复制进包的 `agent/` 再改，于是每个带 agent 的候选都有一份可 diff 的祖先。两种放置必须等价：同一道 fixture 任务、同一假 actor，进程内运行与 worker 内运行产生逐条相同的记录种类序列（id、时间与 worker 自己的 `worker_session` 费用除外，PR7-07）。**镜像与宿主同一份字节**的检查落在 runner 上（实现，2026-10-04）：worker 的 `load` 答复带 `runner_sha256`（它运行的 `worker_runner.py` 的摘要）与它会的 ops，宿主在每个会话的第一次 `load` 比对自己的 `worker_runner.py`（`agent/worker.py` 的 `stale_runner`），不同即 `StaleWorker`（`WorkerUnavailable` 的子类，是基础设施条件，不是包的缺陷），错误信息带重建命令 `docker build -f docker/worker.Dockerfile -t ouroloop-worker:py3.12 .`；启动检查 `bench.worker_problems` 在 K 允许代码或当前包含代码时起一个会话做同一比对（`config_harness_problems`、`harness_checks`、导出的 `App`），所以 **v0.3 的第一次 freeze 之前镜像必须重建**（K 按内容 id 冻结镜像；v0.1/v0.2 的工作区 pin 的是旧镜像 id，它们已结束，若要 resume 需要旧镜像，不迁移）。默认 agent 的字节**不进镜像**（`docker/worker.Dockerfile` 只装 runner）：宿主在 `load` 时把 `default_agent.seed_files()` 读到的自己这份文件随包文件一起发给 worker，所以两侧运行的是同一份字节这件事由构造保证，不需要再比对；设计里“镜像各带一份副本、字节不同是启动检查错误”在实现里落成 runner 的摘要校验加默认 agent 的随载发送。

**草稿 smoke**（M0 §3，实现，`agent/worker.py` 的 `check_package`）。每个运行含代码的草稿都 smoke，不只带 `agent` 条目的：带条目的经自己的入口，v1 含代码的包经默认 agent（与运行时一样加进 bundle）；worker 内的 `SmokeContext` 充当假模型——第一次带工具的调用答一回合 `tool_use`、对每个所给工具各调一次（必填参数按 schema 给最小占位值），之后每次答结束——工具经 fixture broker，技能为空，`mark` 不记；上限 `SMOKE_LIMITS`（act 5、模型调用 10、工具 40、token 20000、30 秒），超过即 `SmokeLimit`，连同未声明的角色、`run` 抛错、hook/工具答非协议，都作为草稿失败回传 R，不是基础设施。

**hook 作为约定**。`before_tool`/`after_tool`/`on_finish` 三个挂点、它们的语义（下节“首版能力包 API 与执行边界”）与截断规则，v0.3 起是默认 agent 遵守的约定，不是宿主契约：默认 agent 在它的循环里照今天的顺序调 manifest 里启用的 hook；复制了默认 agent 的候选继承这套约定，改掉它的候选不再有 hook，manifest 里的 hooks 条目仍要通过草稿检查，但 Inspection 会记 `hook_runs=0`。v1 的 `tools` 条目同理：默认 agent 把它们作为 ToolSpec 暴露给模型、在同一 worker 里执行；带自己 agent 的包里它们是普通模块。理由：hook 的价值在 v0.1/v0.2 的结果里（on_finish 对账 hook 被接受一次、拒绝两次），保留它让已装的 W* 原样运行；但它只是循环的一种写法，不该再是循环唯一能变的地方。

（v0.3，2026-10-05，第 3 步）worker 协议多一个 `work` 操作（`agent/worker_runner.py`）：R 编码会话的 `/work` 树——`package/`（当前包，只读）、`inputs/`（本轮输入，只读）、`candidates/<dir>/`（R 写）——放在 worker 里，宿主驱动模型循环并把 R 的每次文件调用转成 `work` 的 `init/list/read/write/edit/delete/export`；写只允许 `candidates/<dir>/<file>`，第一次写新 `<dir>` 时以包的副本建目录；这个操作不运行任何包代码，所以它的失败是 worker 的（WorkerCrashed），不在 `PACKAGE_OPS` 里；`read_trajectory` 由宿主答（worker 没有 transcripts）。会话用的是 K 的隔离 worker（与草稿检查同一个），没有 worker 时本轮 infrastructure_failed、活动暂停（M0 §3）。规格在 M4 §1（v0.3）。测试：`test_package_worker.py::test_the_work_tree_seeds_a_candidate_from_the_package_and_writes_under_candidates_only`、`test_a_work_action_is_never_a_package_error_and_runs_no_code`。

包拿不到什么由三层保证：ctx 上只有五个名字（M0 §2）；worker 镜像与挂载里没有评分器、provider、凭据、对象库、结果存储、transcripts、冻结划分、账本与宿主仓库（下文隔离条目；`test_it6_23_package_code_in_the_docker_worker_cannot_reach_the_host` 已覆盖今天的清单，v0.3 加 transcripts 与 `rsi/cycle.json`）；网关日志与 broker 事件由宿主写，worker 没有写入口。默认 agent 在进程内运行时同样只经 ctx，不持有 Runtime、Objects、评分器句柄：它是 K 的代码，但照包的规矩写，否则 PR7-07 的等价不成立。

（v0.5，2026-10-07，[V0_5_PLAN](../design/V0_5_PLAN.md) §1.1、§1.4、§6；2026-10-08 现状）运行壳多两样，都只在 K 带相应的表时出现：
- **`start` 位点的经验库检索（保留）。** `PackageAgent.run`（`agent/runner.py:139-147`）在组系统提示时调 `research.library.retrieval_block`：宿主代码按任务文本从包里的停用技能 `skills/library/` 检索至多 `shown_k` 条、接在系统提示后，按来源哈希留一，写一条 `library_retrieval` 事件；包里没有库时系统提示逐字节不变（`test_library.py::test_with_the_start_site_off_the_system_prompt_is_byte_identical_to_the_package_without_a_library`）。库的重试另有一段附注，经 `extra_system` 接在后面，只有经验库的流给。v0.4 尝试的第一条系统提示与 v0.5 之前逐字节相同（金样 `test_v05.py::test_a_v04_attempts_first_system_prompt_is_the_same_bytes_as_before_v05`）。按瘦身第三批的规则，经验库保留为 preset 开关，这一段留下。
- **方法的只读 view（随方法库拟删）。** K 有 `methods` 表时，`attempt_view(methods=)`（`runner.py:35`）与 `k_methods`（`runner.py:287`）让默认 agent 把 `m_` 前缀的包工具当作方法：只读 broker、每次调用至多 `methods.method_calls` 次基础调用（`agent/default_agent.py:278-281`）。方法库按瘦身第三批拟删（用户可否决），这一段随之删。

### 首版能力包 API 与执行边界

（v0.3，2026-10-04：本节是 v1 API——包工具与 hook——的规格；v0.3 起它由默认 agent 实现与遵守，成为“约定”，宿主契约只剩上节的网关、broker、硬上限与完成协议。文字保留，供默认 agent 与 v1 包对照；其中“宿主”“核心”在 v0.3 读作“默认 agent 与网关/broker”。）

拟增一个窄适配文件 `agent/harness_package.py`，负责 manifest 装配、固定 worker 协议和 tool/hook adapter；构建/调度仍由 M5 持有。不得为每个包生成另一份 Agent 循环或动态扩展注册表。（v0.3）“不得为每个包生成另一份 Agent 循环”改为：宿主不生成循环；循环由包带来或由默认 agent 提供，宿主只运行。

```text
package tool: async run(ctx, args) -> ToolResult
package hook: async run(ctx, event) -> {action: continue|block|feedback, message?}
ctx.call_tool(base_name, args) -> ToolResult       # 仅由宿主 broker 执行；（v0.3）即 ctx.tool 的 v1 名字，两名并存
ctx.view -> 当前任务/已接受历史/真实工具结果的只读投影
hook sites: before_tool, after_tool, on_finish
```

hook 看到的结果都是截短的，actor 拿到的始终是完整结果。after_tool 的 `result` 至多是前 4000 字符（`agent/default_agent.py` 与 `agent/loop.py` 同名的 `HOOK_RESULT_CHARS`）；视图事件的 `result_excerpt` 是结果的前一段，`truncated` 只表示这段摘录比结果短。包 API 的说明照此写给 R：首个 Claude 活动里，R 把它和失败摘要的截断标记都读成了 actor 漏读，写出一个在每个长结果后提示“你没看到”的 hook，被 gate 拒绝。

包工具是真正对 actor 暴露的 ToolSpec，名称/schema/描述由 manifest 固定；与基础工具或其他包工具重名拒绝。名称与 schema 在草稿检查时按 provider 的共同约束校验（M0 §2），不合约束是结构错误、回传 R，不留到 provider 返回 400 时记作基础设施故障；enabled=false 的工具不进入 ToolSpec。broker 只接 K 已授权的基础工具，不递归调用包工具，不创建新权限；每次内部调用走原权限/预算检查并记录真实事件，不能把多次调用藏成零成本的一次工具。generated code 不能直接持有 ToolSurface、provider、Runtime、对象库或评分器。

before_tool 可在外部动作执行前 block 并返回可见纠正信息；after_tool 读取真实结果并提供 feedback，不撤销动作、不覆写结果；on_finish 可按当前可见业务状态要求继续检查，仍受原 pushback/turn cap 限制。hook 查询仅开放 K 声明的只读工具；缺少只读声明则不允许调用。continue 只交回固定执行流程，不授权任务成功；所有硬权限、预算和最终环境评分继续由核心决定。hook 顺序/启用列表在包 manifest 中冻结，每次事件记录包 ref、hook、输入摘要、输出及实际效果。

before_tool 的 block/feedback 都阻止该外层动作并把信息返回 actor；on_finish 的 block/feedback 都映射为受限 pushback；after_tool 只允许 continue/feedback，feedback 追加独立可见消息，不修改原 ToolResult。阻止动作后不得执行同 turn 后续工具，保留已执行前缀及明确的未执行结果；不假装整 turn 回滚。hook 返回非法结构、超时或 worker 崩溃是明确错误，不默认 continue。

顺序为：合法 actor turn → 原结构/权限检查 → before_tool → 真实基础/包工具执行 → 原始结果落盘 → after_tool；完成请求先经过 on_finish，再交原 StopGuard/硬完成协议。hook 作用于 actor 可见的外层工具调用；broker 内部调用仍逐次执行硬权限/预算检查并记账，不再次触发包 hook，避免查询/复合工具递归进入自身。after_tool 仅接受 continue/feedback，不能 block 已完成动作。包错误明确记录，不能移除包后用旧行为继续拿成绩或重发动作；hook 状态从合法历史投影读取。

挂点覆盖：首版只开放上述三个 site。对照 Harness-Zero 学生库的中间件（[TECHNOLOGY §4](../TECHNOLOGY.md)），除 context_guard 外均可落到 before_tool/after_tool/on_finish：输出/提交/答案/完整性守卫、重试循环守卫与失败恢复提示按其作用对象选 site；turn_budget 以 after_tool feedback 近似，核心 turn cap 不变；Agent 循环中不带工具调用的回合就是完成请求（`agent/default_agent.py` 的 `run`，同 v1 循环），空回合守卫因此由 on_finish 处理（仅 stop 为 end/other 时经 on_finish；因长度截断的空回合直接结束）；bootstrap 指令由宿主注入的 memory/skills 承担。改写上下文的组件（context_guard 类）按设计不支持：TargetHarness 固定上下文算法（§3），这是有意取舍，不为此新增挂点。组件优先级为 memory/skills → on_finish 守卫 → before_tool/after_tool 守卫 → 包工具，候选选择见 [M4 §2](04_RESEARCH_LOOP.md)。（v0.3，2026-10-04）这段取舍被推翻：context_guard 类的组件、planner/executor 结构、判断前的证据表、发送前的第二遍核对、按题在 `max_effort` 内选 effort、关键决策的多采样，都在 agent 程序里写，不需要新挂点——这正是把循环放进包的理由（M0 开头）。组件优先级不再作为规则（M4 §2（v0.3）），R 按自己的诊断选载体；宿主不要求候选含代码，也不按载体种类区别对待草稿。

首个隔离实现使用固定镜像 digest 的 Docker worker，以 stdin/stdout 的有界消息协议连接宿主 broker；日志走独立输出。非特权运行、无网络/daemon socket/宿主仓库/工作区（bench/<domain>/ 下的 results.jsonl、grading.jsonl，以及 rsi/objects 对象库）挂载，包只读挂载，临时目录与 CPU/内存/PID/时间/输出限额由 K 固定。（v0.3）不挂载的清单加上 `transcripts/`（网关日志与 broker 事件所在）与 `rsi/cycle.json`。依赖来自预构建镜像，候选不安装 pip 包。控制面不 import 包，包括 import smoke 与包测试也在 worker 内；AST/lint 只筛明显错误，不充当隔离。只有运行含代码（M0 §2 运行判定）的包在任务执行时要求 Docker worker，含代码的草稿（检查判定，含 test bundle）在草稿检查时要求 worker；只含 skills/memory 的包由宿主按下段装配，不启动隔离 worker。（v0.3）带 `agent` 条目的包两种判定都为真。worker 缺失或能力不足时按 M0 §3 处理：K 允许代码或当前包运行含代码时 freeze/smoke 拒绝启动；活动中草稿检查记 infrastructure_failed、当前版本运行预检记 repair_runtime，二者都暂停活动；均不回退 in-process exec，运行不含代码的包照常运行。首版据此先交付文本组件，再交付代码组件与隔离（[ROADMAP §2](../ROADMAP.md) 阶段 A/B）。

预检读取完整包身份，包括继承的代码/manifest 条目与 test bundle，不能把“本次只改文本”当作纯文本包：草稿检查用检查判定（含 test bundle），任务执行、当前版本预检、安装与导出用运行判定（不含 test bundle）。组件种类由 K 冻结，不因运行中缺少 worker 而收窄；草稿检查时 worker 不可用按 M0 §3 的基础设施故障处理。若当前已采用版本运行需要 worker 而不可用，则在任务运行前报告 repair_runtime 并暂停活动（M0 §3），active 保持原值，不通过卸载代码或换包继续取分。

技能采用一个固定索引加受限 read_skill 宿主入口，经验材料按 manifest/长度上限投影进 actor 输入；记录实际注入/读取的 ref。首版不建向量库或开放任意文件读取。生成/删除材料属于下一轮新包版本，业务 session 不改 immutable bundle。材料只用 train/fit 轨迹形成通用规则，不保存 dev/final 答案，部署不导出原任务日志。（v0.3）`read_skill` 工具与 memory 注入是默认 agent 对 `ctx.skills` 的用法；带自己 agent 的包直接读 `ctx.skills`，每次 `read` 仍由宿主记 `skill_read` 事件，长度上限不变。

## 2. 执行前审阅钩子

本节归第二版。首版 package middleware 是部署时能力，TurnReviewer 是采集时教师，两者不能混为一种 hook；首版没有教师也必须完成包演化/发布与 RSI 验收。第二版（v0.3，2026-10-04 记）：教师的位置从“Agent 循环里 provider 返回之后、追加历史之前”改为**网关**——对 `role=act` 的每次 ctx.model 调用，PASS/REPLACE 在响应交给包之前完成，结果以 `review_ref` 记在同一条网关记录里，包与它的循环对此不可见；`aux` 不经 reviewer、不进数据。下面的步骤与规则因此读作网关内的顺序，文字保留。v0.3 实现的只有网关记录里的 `role` 字段，其余是第二版计划（M2）。

拟新增窄接口 `TurnReviewer.review(observation, proposal, step_key, budget) -> ReviewRecord`，由 M8 提供实现。它仅存在于 `mode=reviewed_collection` 的 agent 构造参数，不写进生产 W。`mode=deployment/task_evaluation` 如配置 reviewer，启动即拒绝。

每一步必须按以下顺序：

1. 从固定历史渲染输入，调用 actor 得到 proposal；只写私有草稿审计。
2. 调 reviewer（普通运行跳过）；校验其引用的是当前 proposal、当前 observation 和当前 plan。
3. 校验 accepted ChatTurn：合法工具、参数、调用 ID、目标 schema、完成请求、长度上限；不能放宽原工具权限。
4. 只有接受的完整 assistant turn 追加到学生历史和 provider 所需 history；再按原顺序执行其工具。
5. 实际 tool results 追加学生历史，写 accepted/executed 对应关系；下一步只能看这一历史。

现有 Agent 在 provider 返回后会直接追加 assistant；改造必须把 review 放在这之前。禁止先执行、再替换日志；禁止把原 proposal 加到历史后仅在训练导出时删除。多工具调用以一个 assistant turn 整体 PASS/REPLACE，替换必须给出完整调用集合；所有调用执行前完成整体权限/结构检查。外部工具中途失败保留真实部分执行结果，不假装回滚。

教师替换后的 thinking/签名不能套用原模型私有签名。provider 需声明接受合法历史编辑的能力；不支持时 reviewed collection 预检失败。第一路径只训练可公开表示的 assistant 文本/工具动作，不强制提取隐藏 reasoning。Tinker/OpenAI 兼容名称本身不代表能力已验证。

## 3. 完成、回退与上下文

模型请求 finish 不等于环境任务成功。StopGuard 的 no 可以把可见检查信息返回 actor；yes 仍服从显式 verifier、硬权限和预算。检查器错误、预算耗尽、turn cap、pushback cap 用明确 termination reason 记录；不能因循环结束生成成功轨迹。最终 success 只来自环境 verifier。（v0.3，2026-10-04）宿主的完成协议只剩两条：`run` 返回即完成（termination `finished`，stop 为包返回时网关最后一次 act 调用的 stop），硬上限到即终止（termination `budget`，`limit` 写明维度）。StopGuard 的回推、turn cap、pushback cap 是默认 agent 内部的事，它们的触发以 `pushback` 事件记在 transcripts 里（默认 agent 经 `ctx.mark("pushback")`，宿主写成 `source: "package"` 的 `pushback` 行；v1 循环的 `Agent.run` 直接 `_emit`），但不再是 termination reason；`verify_command` 的检查器错误同理记事件。

策略不适用、abstain 或已声明的暂时服务错误走冻结 fallback；模型 artifact 错误、schema 不兼容和无法装载属于候选缺陷，不允许回退到旧 actor 再把成绩归新模型。正式 actor 是否允许供应商故障回退由 plan 固定，若发生必须单独报告，不混称纯模型收益。

沿用 `stop.business.v1` 的完整请求、消息/工具事件与剩余额度；保留明确 truncation。TargetHarness 固定上下文裁剪方法，训练投影复现当时实际 actor 输入。不得训练完整无限历史、部署却临时截断。若新增 compact 规则，更改 harness/context 身份并重新验证，第一轮不同时改多种上下文机制。（v0.3，2026-10-04）上述三句改为：上下文裁剪属于 agent 程序，TargetHarness 不再固定它（M0 §2）；训练投影复现“当时实际 actor 输入”的方法是读网关日志的 `messages`，包怎么裁剪都在里面；“部署却临时截断”不可能发生，因为部署跑的是同一份包；一个候选同时改多种上下文机制不再被禁止，但它仍是一个候选、过一次 gate、在台账里一条声明（M0 §3 的 `declaration_ref`），其 `mechanism` 要写明改了哪几种。

## 4. Inspection 与故障

扩展 describe/Inspection：首版记录 actor 身份、package/API ref（含代码时另记 worker image ref）、有效工具 schema、hook 顺序/触发/效果、技能/经验实际读取、工具 broker 调用/资源、fallback/termination；第二版增加 teacher/checkpoint/renderer 字段。没有观察时 observed=false，不能填 0 冒充一次零调用运行。（v0.3，2026-10-04）另记：agent 入口（`default` 或包内 entrypoint）与放置（宿主进程/Docker）、manifest 声明的角色、按角色的模型调用数与输出 token 数、每次调用的 effort 分布、`trajectory_ref`、`limit`（终止维度）、`policies_read`（agent 是否读过 `ctx.view.policies` 的 system_prompt/finish）、`hook_runs` 与 `skill_reads`（0 表示读到了但没发生，observed=false 表示没有观察）。有效工具 schema 一项在带自己 agent 的包上记的是 agent 传给 ctx.model 的 `tools` 清单的去重集合（从网关日志派生），不再由 manifest 派生。这些都从网关日志与 broker 事件派生，不从包的自述读。（实现，2026-10-04）这些落在报告的能力包一节，不在 `research/inspection.py` 的 Inspection（它只读 bound 与决策 trace，没有 transcripts）：`report._harness_evidence` 的 `behaviour.gateway`（`report.gateway_summary`）从宿主写的记录统计——`attempts`（有 `gateway_call` 的尝试数）、`entries`/`placements`（runner 写的 `agent` 事件）、`by_role`（`assistant` 事件的 `role`，调用数与输出 token，与 `Gateway.by_role` 同一口径）、`efforts` 与 `effort_caps`（`requested_effort` 有值且 ≠ `effort` 的次数）、`limits_hit`（`end` 事件的 `limit` 按维度计数）；每个 release 的包摘要另记 `agent`（manifest 的入口，没有则默认 agent 的）与 `roles`。v1 循环的尝试不在这里计任何数（没有观察不填 0）。`trajectory_ref`、`policies_read`、effort 的逐次分布与有效工具 schema 的去重集合尚未实现，留给第 2 步。

装配失败发生在任务工具运行前。预算阻断不变成 no/yes 标签。teacher 超时或非法替换由 M8 决定重试/终止，M1 不执行被拒动作；生产 actor 故障按 M5 既定失败分类处理。（v0.3）硬上限终止不是故障，不进任何失败类；`run` 运行时抛出按 §1“失败语义”记 infrastructure。

## 5. 用例

| ID | 条件 | 预期 |
|---|---|---|
| PR6-01 | W1 换 actor，安装后启动 session；已有 session 正运行 | 新 session 实际加载 W1，旧 session 继续 pin W0 |
| PR6-02 | 学生提出写操作，teacher REPLACE 为查询 | 原写操作从未执行；历史仅包含查询及真实返回 |
| PR6-03 | 多工具替换含一个未知工具或重复 ID | 整 turn 拒绝，所有工具均未执行 |
| PR6-04 | provider 不能接收替换历史 | reviewed collection 预检失败，不造假签名 |
| PR6-05 | 正式评测注入 reviewer 或私有教学材料 | 启动拒绝；provider 输入审计中没有教师内容 |
| PR6-06 | controller yes 但 verifier 否决；或触发 turn cap | 继续/终止按固定规则；不记录假成功 |
| PR6-07 | controller 未调用、abstain、实际 no 三条路径 | 分别记录，不用“模型存在”代替其实际作用 |
| PR6-08 | 自动生成的 source 修改 finish 工作流 | 实际提示/处理和工具行为改变；仅改变 ref 的假接线失败 |
| PR6-09 | 新 h 与 actor/controller 的消息或 observation 不兼容 | preparation 在任务执行前失败，active 不动，不自动换模型或裁掉控制器 |
| PR6-10 | 包增加分页工具、完成检查及技能/经验材料 | 真实工具调用/hook 阻止结束/材料读取可查，文件存在或提示追加不能冒充执行 |
| PR6-11 | before_tool block、after_tool 异常、on_finish 反复 feedback | 前者动作未执行；后者真实结果保留且不重发；pushback/turn cap 仍生效，错误不静默卸载包 |
| PR6-12 | 包试图调用未授权工具、hook 写操作或读取宿主核心 | broker/隔离层阻止，原权限/预算不变；无可用隔离 worker 时不使用 in-process fallback |
| PR6-13 | 无可用隔离 worker 时分别处理只含 skills/memory 的包、运行 bundle 含代码的包、只有 test bundle 含 tests/*.py 的包 | 第一类正常运行，注入/读取事件可查；第二类不装配运行，按 M0 §3 拒绝启动或暂停活动；第三类可运行（test bundle 不挂载），但其草稿检查需要 worker；均不 in-process 执行 |
| PR6-14 | patch 只修改 memory/skills，但完整草稿继承父包 tools/hooks 或 Python 文件 | 仍按含代码包预检，需要合格隔离 worker，不能按文本 patch 绕过 |
| PR6-15 | 当前已采用包运行需要 worker，而 worker 不可用 | 任务运行前报告 repair_runtime 并暂停活动，不再调度后续段与 research slot；worker 恢复后续跑；active 不变，不删除代码、换旧包或 in-process 执行 |
| PR7-01（v0.3） | 带 `agent` 条目的包运行一次尝试 | 宿主起 worker 会话、`load` 运行 bundle、发 `run`；`run` 返回的字符串是最终消息；会话随尝试结束 |
| PR7-02（v0.3） | agent 调 `ctx.model`，含一次 `role="aux"` | 每次经网关到 pin 的 actor；宿主写 `gateway_call.v1`，`role`、`effort`、`messages`、`tools`、`response`、`usage` 齐全；aux 条目计入 `max_model_calls`，不计 `max_steps` |
| PR7-03（v0.3） | agent 调 `ctx.tool`，一次是 K 未授权的工具，一次是规则否决的工具 | 前者 `is_error`、无事件外的副作用；后者 tool_gate 记 veto；授权调用各一条 `broker_event.v1`，计入 `max_tool_calls` |
| PR7-04（v0.3） | `effort` 高于 `max_effort`；manifest 未声明 `aux` 却以 aux 调用 | 前者按 `max_effort` 调用、照常入账，记录里 `effort` 是上限、`requested_effort` 是请求值；后者网关拒绝、不调模型、不入账，草稿接口 smoke 对同一包先报草稿失败 |
| PR7-05（v0.3） | 五个硬上限各自先到 | termination `budget`，`limit` 写对应维度；在飞的模型调用入账并记日志，响应不交给包；最终消息为空；环境照常评分 |
| PR7-06（v0.3） | `run` 运行时抛出；`run` 返回 None；worker 被包的代码杀死；同一代码在草稿 smoke 里抛出 | 前三者 failure_class `infrastructure`、termination `harness_error`、补跑一次；第四者是草稿失败、回传 R、占一次修订 |
| PR7-07（v0.3） | 同一 fixture 任务、同一假 actor（REHEARSAL §3），默认 agent 分别在宿主进程内与复制进包后在 Docker 内运行 | 两份网关日志与 broker 事件逐条相同（时间与 id 除外）；宿主与镜像里的 `default_agent.py` 字节不同时启动检查拒绝 |
| PR7-08（v0.3） | v1 包（含 before_tool/after_tool/on_finish hook、包工具、skills、memory）在 v2 宿主下由默认 agent 运行 | PR6-10/11/12 的全部预期不变；hook、包工具、read_skill、memory 注入的事件可查，与 v0.2 的 W* 在 v1 宿主下的 transcripts 形状一致 |
| PR7-09（v0.3） | 包代码尝试 import 评分器/provider、打开 transcripts、results.jsonl、grading.jsonl、`rsi/objects`、`rsi/cycle.json`，或访问网络 | Docker 内全部失败（IT6-23 的清单扩展）；ctx 上没有第六个名字，`getattr` 任何其他属性是 AttributeError |
| PR7-10（v0.3） | 带自己 agent 的包改掉 hook 约定、不读 `ctx.view.policies`，但 manifest 仍有启用的 hook 与 W 仍有 `worker.finish` 文本 | 草稿检查通过（manifest 合法）；Inspection 记 `hook_runs=0`、`policies_read` 为空；gate 照常比较，不因“文本没被读”而拒绝或放行 |
| PR7-11（v0.3） | 一次尝试后读 Inspection | 含 agent 入口与放置、角色、按角色的调用数与 token、effort 分布、`trajectory_ref`、`limit`；没有观察的项 observed=false，不填 0 |
| PR7-12（v0.3） | agent 自己写“已核对全部收件人”的笔记并放进最终消息 | 记录里只有网关日志与 broker 事件；R 的轨迹工具、训练投影、gate 收据都不读该笔记作为事实 |
| PR7-13（v0.3） | 旧 v2 release（无包、或 v1 包）在 v1 循环的工作区继续运行 | 行为与 v0.2 相同：决策点仍在循环里，`step_limit` 等旧 termination 继续出现；不迁移旧工作区。（2026-10-08 瘦身第一批：v1 循环已删，这条用例不再成立；这样的工作区在 tag `v0.3.0`～`v0.4.0` 或 `archive-2026-10-08` 上续跑。v1 包在 HEAD 上由默认 agent 经运行壳运行，见 PR7-08） |

PR6-02～05 的教师路径归第二版；首版重点覆盖旧兼容、包实际执行及上述边界，PR6-01 的未训练 provider pin 仍需验证。PR6-13 属阶段 A，不需要 Docker。（v0.3）PR7 用例的测试大多在 `ProcessWorker`（真实 runner 与协议、子进程、不隔离）上跑，不需要 Docker；真实镜像下的一份（PR7-01/08）与 IT6-23 一样在 `OUROLOOP_TEST_REQUIRE_DOCKER` 下运行，缺镜像即失败（同 2026-10-02 的 CI 规则）。彩排（[REHEARSAL.md](../REHEARSAL.md) §8）随之更新：假 actor 经网关作答（它是 `[llm] type` 指向的 provider，网关调它与调 `claude_code` 走同一路径），离线检验的每次尝试都走运行壳；给草稿接口 smoke 用的 fixture 世界仍是草稿检查既有的那个，彩排世界没有另加任务。

验证落点：`test_agent.py`、`test_policy_runtime.py`、`test_policies.py`，扩展 provider renderer 往返测试（2026-10-08 瘦身第一批：这三个文件里的 v1 循环与冠军测试已删；StopGuard 与 PR-01、PR-02、PR-03、ER-03 的规则改在 `tests/train/test_stop.py` 里直接测）。M1 不实现 teacher 提示策略或 SFT 数据导出。（v0.3，测试名以实现为准，2026-10-04）PR7 用例的测试：PR7-01/08 `test_package_worker.py::test_the_default_agent_runs_a_v1_package_through_run_in_the_qualified_worker`（Docker）与 `test_the_v02_w_star_shape_runs_under_the_default_agent_in_the_worker`；PR7-02/03/12 `test_package_worker.py::test_it6_29_an_agent_component_runs_in_the_worker_and_every_model_and_tool_call_goes_through_the_host`，另有 `test_the_gateway_names_the_pinned_models_identity_and_writes_the_tool_specs_once_per_change`、`test_a_malformed_model_call_is_answered_as_an_error_the_agent_may_catch_and_never_raised_on_the_host`、`test_a_mark_cannot_set_the_records_own_fields`；PR7-04 `test_package_worker.py::test_effort_above_max_effort_is_capped_on_the_record_and_the_request_is_kept`、`test_an_undeclared_role_is_refused_by_the_gateway_without_a_model_call_and_recorded`、`test_effort_reaches_the_provider_through_the_meter_and_the_metered_call_is_booked`；PR7-05 `test_package_worker.py::test_each_hard_limit_ends_the_attempt_as_budget_and_names_itself_and_the_worker_is_gone`、`test_the_tool_calls_and_seconds_limits_end_the_attempt_through_the_shell_and_the_refusal_is_on_record`、`test_the_gateway_pre_checks_the_act_limit_before_calling_the_provider`；PR7-06 `test_package_worker.py::test_it6_43_a_crash_at_run_time_is_infrastructure_a_crash_in_the_draft_check_is_a_draft_failure_and_a_hard_limit_is_budget`、`test_an_agent_that_raises_at_run_time_is_infrastructure_not_a_candidate_defect`、`test_an_undeclared_role_or_a_crash_in_the_smoke_is_a_draft_problem`；PR7-07 `test_package_worker.py::test_a_text_only_v1_package_runs_in_process_and_its_records_match_the_worker_placement`（两种放置）与 `test_a_worker_running_another_runner_is_refused_at_load_and_named_at_startup`（字节不同即拒绝）；PR7-08 `test_package_worker.py::test_it6_29_a_v1_package_under_the_default_agent_makes_the_host_loops_calls`、`test_a_hooks_broker_calls_are_read_only_under_the_default_agent`；PR7-09 `test_package_worker.py::test_pr7_09_ctx_has_the_five_names_and_mark_and_nothing_else`（ctx 的名字）与 `test_it6_23_package_code_in_the_docker_worker_cannot_reach_the_host`（Docker，隔离清单）；PR7-11 `test_report.py::test_pr7_11_the_report_reads_the_agent_entry_placement_roles_effort_caps_and_limits_from_the_gateway_log`；PR7-13 `test_harness_loop.py::test_startup_refuses_a_pinned_actor_effort_above_k_max_effort`（旧 K 不设上限、不走网关的那一半）与 `test_cycle.py::test_a_new_freeze_needs_all_five_limits_and_an_old_plan_is_read_with_its_three`；彩排 `test_rehearsal_world.py::test_the_fake_actor_answers_through_the_gateway`（2026-10-08 随彩排一起移出 HEAD，tag `archive-2026-10-08` 上可读）。尚无专门测试的：PR7-10（带自己 agent 的包不调 hook 时报告的 hook 计数为空，由报告路径覆盖，专门用例留第 2 步）。
