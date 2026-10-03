# M1：执行模型、决策模型与运行时

> 首版目标：发布哪个能力包就实际运行哪个包，actor 身份固定，生成代码隔离执行。改造 `policies.py`、`agent/loop.py`、`runtime.py`、`stop.py` 与 `providers/`；保留旧注册目标/legacy。执行前教师审阅与 Tinker sampling 是第二版规格，本文件保留其详细设计。

## 1. 目标与装配

| target | binding / 输出 | 装配位置 |
|---|---|---|
| `worker.actor`（新增） | generation / `ChatTurn` | Agent 使用的 ChatProvider，身份来自 session pin 的 W |
| `worker.harness`（新增） | config / `harness_package.v1` | 包的工具、hook、技能与经验；代码只在固定隔离 worker 中装配 |
| `worker.system_prompt` | config / prompt | 合并后的系统提示，继续使用现有规则 |
| `worker.finish` | config / 内置工作流配置 | 完成请求处理 |
| `worker.stop` | decision / yes、no、abstain | StopGuard 的 on_finish 路径 |
| `research.diagnose/propose` | research_step / prompt | 既有研究 provider 实际加载被 pin 的 R 配置；首版 diagnose 改为逐题契约 failure_summary.v1 → failure_note.v1，propose 读笔记集合产出 Requirement 与至多一个 patch/next_action（[M4 §1](04_RESEARCH_LOOP.md)） |
| `research.retrieve/preflight` | config / rule 或 workflow | 既有受限检索与辅助检查；不可替代核心 admission |

v3 session 同时 pin release.target_harness_ref；装配检查它与 K 许可、plan 和 provider 能力一致。新的 harness preparation baseline 只能按 M3 签发的任务/mode scope 执行，不进入 active/champion；M1 为它检查 actor renderer、工具/消息表示、controller observation 和完成协议兼容，缺能力显式拒绝，不静默关闭 controller。B 的后续采集使用 M3 针对确切 B/DistillationPlan 签发的补充 v3 permit，作为显式装配参数。扩展现有 authorize 的严格校验分支，不能因为它不同于 B 创建时的 baseline permit 就误拒，也不能接受不匹配的 release/harness/scope/mode；旧 v2 行为不变。source 运行读取 source release 的契约；学生读 student release 的契约，不能由 agent_factory 临时换掉。

新增 actor registration；扩展 `bind_release`、`_bind_one`、`Services` 和 `SessionBinder`，不要在 bench 临时给 Agent 塞一个不同的 provider。v3 的配置 `[llm]` 仅用于 bootstrap 构造 actor；已有 actor 的 release 不允许被本地同名参数悄悄覆盖。显式覆盖需新 release/比较身份。

运行时接口目标：BoundRuntime.actor -> ChatProvider；现有 ChatProvider.chat 接口保留。首版 pin 现有 provider 与推理配置，actor 原定 GLM-5.3（已配置并计价的 OpenAI 兼容接口）；2026-10-03 起先跑 Claude Opus 5.5，经下面的 `claude_code` provider（ROADMAP §3 第 2a 步）。不要求训练服务。第二版 Tinker provider 属于本模块，负责官方 renderer/采样/action schema 解析；未训练底座与 adapter 共用序列化。

controller 与 actor 共享当时合法 observation，默认不把 controller 当成需要 actor 自发调用的工具。on_finish hook 保证调用位置；其输出和真正执行动作分别写 DecisionTrace。独立模型的价值由同 actor 完整任务评测决定。

源方法运行同样通过 bind_release 装配；包、system_prompt 与 finish 的候选内容必须在实际工具/hook/actor 输入中生效。旧 finish_check.v1 是提示正文，不能据此填“中间件已执行”。包外 module/未知宿主 API 在预检拒绝；包内入口由固定隔离 worker 解析。第二版 source_validation 与 reviewed collection/task evaluation 分开记录，源验证无 teacher。

### `claude_code` provider

`[llm] type = "claude_code"`（`ouroloop/providers/claude_code.py`）：每次 `chat` 起一次无头 Claude Code，用本机 `claude` 的订阅登录。模型看到的是这一次的请求，外加 CLI 在订阅登录下关不掉的三样：系统提示前一行 Agent SDK 身份、账号邮箱、当天日期。

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

### 首版能力包 API 与执行边界

拟增一个窄适配文件 `agent/harness_package.py`，负责 manifest 装配、固定 worker 协议和 tool/hook adapter；构建/调度仍由 M5 持有。不得为每个包生成另一份 Agent 循环或动态扩展注册表。

```text
package tool: async run(ctx, args) -> ToolResult
package hook: async run(ctx, event) -> {action: continue|block|feedback, message?}
ctx.call_tool(base_name, args) -> ToolResult       # 仅由宿主 broker 执行
ctx.view -> 当前任务/已接受历史/真实工具结果的只读投影
hook sites: before_tool, after_tool, on_finish
```

包工具是真正对 actor 暴露的 ToolSpec，名称/schema/描述由 manifest 固定；与基础工具或其他包工具重名拒绝。名称与 schema 在草稿检查时按 provider 的共同约束校验（M0 §2），不合约束是结构错误、回传 R，不留到 provider 返回 400 时记作基础设施故障；enabled=false 的工具不进入 ToolSpec。broker 只接 K 已授权的基础工具，不递归调用包工具，不创建新权限；每次内部调用走原权限/预算检查并记录真实事件，不能把多次调用藏成零成本的一次工具。generated code 不能直接持有 ToolSurface、provider、Runtime、对象库或评分器。

before_tool 可在外部动作执行前 block 并返回可见纠正信息；after_tool 读取真实结果并提供 feedback，不撤销动作、不覆写结果；on_finish 可按当前可见业务状态要求继续检查，仍受原 pushback/turn cap 限制。hook 查询仅开放 K 声明的只读工具；缺少只读声明则不允许调用。continue 只交回固定执行流程，不授权任务成功；所有硬权限、预算和最终环境评分继续由核心决定。hook 顺序/启用列表在包 manifest 中冻结，每次事件记录包 ref、hook、输入摘要、输出及实际效果。

before_tool 的 block/feedback 都阻止该外层动作并把信息返回 actor；on_finish 的 block/feedback 都映射为受限 pushback；after_tool 只允许 continue/feedback，feedback 追加独立可见消息，不修改原 ToolResult。阻止动作后不得执行同 turn 后续工具，保留已执行前缀及明确的未执行结果；不假装整 turn 回滚。hook 返回非法结构、超时或 worker 崩溃是明确错误，不默认 continue。

顺序为：合法 actor turn → 原结构/权限检查 → before_tool → 真实基础/包工具执行 → 原始结果落盘 → after_tool；完成请求先经过 on_finish，再交原 StopGuard/硬完成协议。hook 作用于 actor 可见的外层工具调用；broker 内部调用仍逐次执行硬权限/预算检查并记账，不再次触发包 hook，避免查询/复合工具递归进入自身。after_tool 仅接受 continue/feedback，不能 block 已完成动作。包错误明确记录，不能移除包后用旧行为继续拿成绩或重发动作；hook 状态从合法历史投影读取。

挂点覆盖：首版只开放上述三个 site。对照 Harness-Zero 学生库的中间件（[TECHNOLOGY §4](../TECHNOLOGY.md)），除 context_guard 外均可落到 before_tool/after_tool/on_finish：输出/提交/答案/完整性守卫、重试循环守卫与失败恢复提示按其作用对象选 site；turn_budget 以 after_tool feedback 近似，核心 turn cap 不变；Agent 循环中不带工具调用的回合就是完成请求（`agent/loop.py:166`），空回合守卫因此由 on_finish 处理（仅 stop 为 end/other 时经 on_finish，`loop.py:167`；因长度截断的空回合直接结束）；bootstrap 指令由宿主注入的 memory/skills 承担。改写上下文的组件（context_guard 类）按设计不支持：TargetHarness 固定上下文算法（§3），这是有意取舍，不为此新增挂点。组件优先级为 memory/skills → on_finish 守卫 → before_tool/after_tool 守卫 → 包工具，候选选择见 [M4 §2](04_RESEARCH_LOOP.md)。

首个隔离实现使用固定镜像 digest 的 Docker worker，以 stdin/stdout 的有界消息协议连接宿主 broker；日志走独立输出。非特权运行、无网络/daemon socket/宿主仓库/工作区（bench/<domain>/ 下的 results.jsonl、grading.jsonl，以及 rsi/objects 对象库）挂载，包只读挂载，临时目录与 CPU/内存/PID/时间/输出限额由 K 固定。依赖来自预构建镜像，候选不安装 pip 包。控制面不 import 包，包括 import smoke 与包测试也在 worker 内；AST/lint 只筛明显错误，不充当隔离。只有运行含代码（M0 §2 运行判定）的包在任务执行时要求 Docker worker，含代码的草稿（检查判定，含 test bundle）在草稿检查时要求 worker；只含 skills/memory 的包由宿主按下段装配，不启动隔离 worker。worker 缺失或能力不足时按 M0 §3 处理：K 允许代码或当前包运行含代码时 freeze/smoke 拒绝启动；活动中草稿检查记 infrastructure_failed、当前版本运行预检记 repair_runtime，二者都暂停活动；均不回退 in-process exec，运行不含代码的包照常运行。首版据此先交付文本组件，再交付代码组件与隔离（[ROADMAP §2](../ROADMAP.md) 阶段 A/B）。

预检读取完整包身份，包括继承的代码/manifest 条目与 test bundle，不能把“本次只改文本”当作纯文本包：草稿检查用检查判定（含 test bundle），任务执行、当前版本预检、安装与导出用运行判定（不含 test bundle）。组件种类由 K 冻结，不因运行中缺少 worker 而收窄；草稿检查时 worker 不可用按 M0 §3 的基础设施故障处理。若当前已采用版本运行需要 worker 而不可用，则在任务运行前报告 repair_runtime 并暂停活动（M0 §3），active 保持原值，不通过卸载代码或换包继续取分。

技能采用一个固定索引加受限 read_skill 宿主入口，经验材料按 manifest/长度上限投影进 actor 输入；记录实际注入/读取的 ref。首版不建向量库或开放任意文件读取。生成/删除材料属于下一轮新包版本，业务 session 不改 immutable bundle。材料只用 train/fit 轨迹形成通用规则，不保存 dev/final 答案，部署不导出原任务日志。

## 2. 执行前审阅钩子

本节归第二版。首版 package middleware 是部署时能力，TurnReviewer 是采集时教师，两者不能混为一种 hook；首版没有教师也必须完成包演化/发布与 RSI 验收。

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

模型请求 finish 不等于环境任务成功。StopGuard 的 no 可以把可见检查信息返回 actor；yes 仍服从显式 verifier、硬权限和预算。检查器错误、预算耗尽、turn cap、pushback cap 用明确 termination reason 记录；不能因循环结束生成成功轨迹。最终 success 只来自环境 verifier。

策略不适用、abstain 或已声明的暂时服务错误走冻结 fallback；模型 artifact 错误、schema 不兼容和无法装载属于候选缺陷，不允许回退到旧 actor 再把成绩归新模型。正式 actor 是否允许供应商故障回退由 plan 固定，若发生必须单独报告，不混称纯模型收益。

沿用 `stop.business.v1` 的完整请求、消息/工具事件与剩余额度；保留明确 truncation。TargetHarness 固定上下文裁剪方法，训练投影复现当时实际 actor 输入。不得训练完整无限历史、部署却临时截断。若新增 compact 规则，更改 harness/context 身份并重新验证，第一轮不同时改多种上下文机制。

## 4. Inspection 与故障

扩展 describe/Inspection：首版记录 actor 身份、package/API ref（含代码时另记 worker image ref）、有效工具 schema、hook 顺序/触发/效果、技能/经验实际读取、工具 broker 调用/资源、fallback/termination；第二版增加 teacher/checkpoint/renderer 字段。没有观察时 observed=false，不能填 0 冒充一次零调用运行。

装配失败发生在任务工具运行前。预算阻断不变成 no/yes 标签。teacher 超时或非法替换由 M8 决定重试/终止，M1 不执行被拒动作；生产 actor 故障按 M5 既定失败分类处理。

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

PR6-02～05 的教师路径归第二版；首版重点覆盖旧兼容、包实际执行及上述边界，PR6-01 的未训练 provider pin 仍需验证。PR6-13 属阶段 A，不需要 Docker。

验证落点：`test_agent.py`、`test_policy_runtime.py`、`test_policies.py`，扩展 provider renderer 往返测试。M1 不实现 teacher 提示策略或 SFT 数据导出。
