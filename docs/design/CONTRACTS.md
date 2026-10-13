# 公共契约：对象、工作区、K、release、记录、错误、测试约定

> 阶段 1 设计文档。所有包共用的形状都在这里定；各包文档只引用。实现文件：`ouroloop/contracts.py`（由 `evolve/contracts.py` 收缩而来：身份、Record 与类型注册、对象库、事件、文件与 JSONL 助手、错误类）、`ouroloop/runtime/records.py`（transcript 行的 kind 与字段）。2026-10-09 按阶段 1 的实现对齐。

## 1 包的依赖方向

```
adapters/        环境适配器（automationbench）：任务、工具、评分            只依赖标准库与上游
harness/         包模型与包侧契约                                            ← contracts
runtime/         网关、broker、worker、egress、运行壳、App                   ← contracts, harness
evaluate/        划分、尝试、gate、final、发布                              ← contracts, harness, runtime, adapters
memory/          sidecar、规则、写入、检索、升级                             ← contracts, harness, runtime
improver/        证据、诊断、分层、会话、验收、筛选、档案、记功、台账、策略    ← contracts, harness, runtime, evaluate, memory
activity/        配置、工作区、阶段、预算、检查、报告、CLI                    ← 以上全部
train/           训练链                                                     ← contracts, evaluate（不被热路径 import）
```

硬规则（`tests/test_hot_path_boundary.py` 守住）：
- 一次尝试（`evaluate.run_attempt`）跑完，`sys.modules` 里没有 `ouroloop.improver`、`ouroloop.memory.sidecar`、`ouroloop.train`；例外只有 K 的节取默认值的 `ouroloop.improver.settings`（与包的 `__init__`），它们只用标准库。
- 控制面进程永远不 import 包里的代码（`vendor/`、`agent/`、`tools/`、`middlewares/`、`tests/`）；它们只在 worker 里跑。
- `adapters/` 不 import 本框架的其他包。

## 2 对象与引用

现有 `evolve/contracts.py` 的对象库保留，收缩成 `ouroloop/contracts.py`：

```python
def canonical(obj) -> bytes            # 规范 JSON（键排序、无空白、UTF-8）
def digest(obj) -> str                 # sha256(canonical(obj)) 的十六进制
def ref_for(kind: str, payload) -> str # f"{kind}:sha256:{digest(payload)}"（完整 64 位，与旧工作区的引用同形）
def parse_ref(ref) -> tuple[str, str]
def short(ref) -> str                  # 前 12 位，报告与日志用

class Record:                          # 所有冻结记录的基类：子类是 dataclass，payload() 去掉 None 与易变字段，to_json/from_json，ref 由内容算
def register(cls)                      # 类型注册：Objects.load(ref) 按 ref 的 kind 找类
class Objects:                         # 内容寻址的 JSON 对象库：<root>/rsi/objects/<kind>/<digest>.json
    def put(self, record_or_kind, payload=None) -> str
    def get(self, ref) -> dict
    def load(self, ref, cls: type[Record]) -> Record
    def has(self, ref) -> bool
class Events:                          # 只追加的 JSONL：<root>/rsi/events.jsonl，每行 {ts, event, ...}
    def append(self, kind, **fields) -> dict
    def read(self, kind=None) -> Iterator[dict]
def durable_write(path, data: bytes)   # 写临时文件再 rename
def copy_closure(src: Objects, dst: Objects, refs) -> set[str]   # 导出时拷贝引用闭包
# 文件助手（原 ledger.py）：append_jsonl、read_jsonl、write_json、locked、file_key、redact、now、new_id、load_dotenv、import_object
```

删掉：`PolicyDescriptor`、`PolicySet`、`SystemRelease`、`EvaluationPermit`、`Authorization`、`TargetHarness`、`CollectionRequest`。只有训练用的搬进 `train/`：`EvidenceRef`、`DataManifest`、`Observation`、`DecisionTrace`、`FeedbackRecord`、`FeedbackSet` → `train/records.py`；决策台账 `Ledger` 与 `SessionStore` → `train/ledger.py`；完成探针 `CompletionProbe`（写训练标签）→ `train/probe.py`。

保留并改名到各自包：`ExperimentProposal`/`Experiment` → `improver.Candidate`（阶段 3）；`EvaluationReceipt`/`InstallationReceipt` → `evaluate.release.GateReceipt`/`InstallReceipt`；`CoreManifest` → `activity.config.Core`；`HarnessPackage` → `harness.package.Package`；`DeploymentManifest` → `runtime.app.AppManifest`；`ModelIdentity` → `evaluate.model.ModelIdentity`。

## 3 工作区布局

一个活动一个根目录（git 忽略）。阶段 1 沿用已有的 `rsi/`、`bench/`、`transcripts/` 三个目录，新包按下表加：

```
<root>/
  core.json                     K 的冻结副本（activity 写，带 ref），core_ref = 对象库里 core 对象的 ref
  rsi/objects/                  Objects
  rsi/events.jsonl              Events（core_frozen、installed、evaluation_completed、final_sealed、test_read……）
  rsi/active.json               工作线的 release 指针（evaluate.release，CAS）；rsi/released.json 发布线（v1.2）
  rsi/operations/、revoked.json  装入的操作记录、撤销表
  rsi/budget/book.json          预算簿（evaluate.budget.Budgets）
  rsi/execution/                可续跑步骤的日志（evaluate.budget.Execution）
  rsi/finalization/<id>.json    封存的 final 与它的状态
  rsi/cycle.json                cycle 的进度
  bench/<domain>/               splits.json（冻结的划分）、results.jsonl、runs/、grading.jsonl（evaluate.results）
  transcripts/<attempt>.jsonl   每次尝试一份（runtime.records；attempt id 里的 / 写成 __）
  index/trace.sqlite            trace 的 FTS 索引，派生、可重建（阶段 3，runtime/trace.py）
  rounds/r<i>/                  一轮改进器的产物（阶段 3）：round.json（每步的输出）、candidates/<dir>/、session 记录
  rounds/notes.jsonl            诊断的缓存
  ledger/experience.jsonl       改进器台账（阶段 3）；ledger/estimates.json、ledger/playbook.md
  memory/<line>/                memory（阶段 2）：stream.json（流的台账）、live.jsonl、chroma/、bridge_calls.jsonl
  report.json、report.md        报告（activity.report）
```

## 4 K：固定核心

K 是配置经 preset 补全、按“只能收紧”规则合并后冻结的一份 JSON（`core.json`；对象是 `activity.config.Core{preset, sections, split_hash, attempt_version, improver_version}`），`core_ref` 覆盖它的全部字节，并含两项代码摘要（v1.2：`attempt_version` 是一次尝试走到的代码，`improver_version` 是其余；见下文），所以改任何 `.py` 都是新 K（PLAN 规则 13）。每个字段的合并规则（cap、floor、subset、superset、ranges、effort、bytes、network、fixed、free）写在 `activity/presets.py` 的 `RULES`。

| 节 | 字段 | 谁用 |
|---|---|---|
| `bench` | `domain`、`upstream_path`、`toolset`、`ratios`、`seed`、`split_rule`、`frozen_splits`、`families`、`near_duplicate_threshold`、`smoke_tasks`、`concurrency`；冻结划分的哈希在 K 顶层 `split_hash` | evaluate.splits、adapters |
| `limits` | 五项硬限额：`max_steps`、`max_model_calls`、`max_tool_calls`、`max_seconds`、`max_tokens`；`max_output_chars` | runtime、evaluate |
| `model` | actor：`type`/`provider`（claude_code）、`model`/`identity`（模型名）、`effort`、`max_tokens`、`timeout`；`max_effort`（= effort） | runtime.gateway |
| `research_model` | R 与 aux 调用的 provider、identity、effort、max_tokens、timeout | improver、memory |
| `pricing` | 名义价目：`input_per_1k`、`cache_read_per_1k`、`cache_write_per_1k`、`output_per_1k`、`max_input_tokens`、`max_output_tokens` | evaluate.budget |
| `harness` | `seed`（种子名）、`api_version`、`kinds`（允许的组件类型）、`sites`（允许的 hook 站点）、`limits`（HARNESS §3 的 Limits 字段）、`config_ranges`（每个 config 键的范围） | harness、improver |
| `worker` | `image`、`image_id`、`cpus`、`memory`、`memory_swap`、`pids`、`tmpfs_mb`、`network`（`"none"` 或 `{mode:"proxy", allow:[{host,port,methods}]}`）、`env`、`request_seconds`、`max_output_chars` | runtime.worker、runtime.egress |
| `memory` | MEMORY §3 的 Settings：`enabled`、检索（`shown_k`、`recall_k`、`fetch_factor`、`min_score`、`sibling_sim`）、写入（去重与验证阈值、`origins`、字符上限、`stream_batch`、`retry`、`cite`）、记功与退役、容量、`abort_d`、`heldout_domains`、`imports`、升级阈值、`retriever` 与 `backend` 钉表 | memory |
| `improver` | `k`、会话上限、诊断案例数、探索与 L5 名额、筛选、`round_share`（阶段 1）；IMPROVER §2 的其余字段随阶段 3 加入 | improver |
| `gate` | `min_gain`、`min_groups`、`min_tasks`、`max_regression_rate`、`regression_drop`、`confirmations`、`control_readings`、`control_pooling`、`repeats`、`order_seed`、`missing_cost_policy`、`max_infrastructure_retries`；成本项 `cost_beta0/1` 默认不判（PLAN P6） | evaluate.gate |
| `schedule` | `rounds`、`patience`、`max_infrastructure_failures` | activity |
| `budget` | `usd`（根）、`final_usd`（预留份额）；每轮的研究份额是 `improver.round_share` | evaluate.budget、activity |
| `final` | `tasks`（"test"）、`repeats`、`claims`；臂（W0、W\*）在封存时由活动定 | evaluate.final |

preset 与配置的关系：preset 给出每个字段的默认值与上限；配置只能把数值改小、把集合改窄、把 effort 改低（“只能收紧”，`activity.config.merge`）；超出即启动错误。preset 本身在代码里（`activity/presets.py`），新 preset 名随版本号。

2026-10-10 修订：`gate` 节多 `partial_min_gain`（floor，可为 None）。

2026-10-11 修订（v1.2）：
- K 的版本拆成两项：`attempt_version`（`runtime/`、`harness/` 含种子、`evaluate/{attempt,results,limits,splits}.py`、`adapters/`、`providers/`、`contracts.py` 的摘要）与 `improver_version`（`improver/`、`memory/`、`activity/`、`evaluate/{gate,final,budget,release}.py`、R 的提示词的摘要）；`Core{..., attempt_version, improver_version}`，`code_version` 不再单独存在。启动检查分别比对两者。
- `AttemptIdentity = digest(attempt_version, package_ref, limits, model, worker.image_id, split_hash)`，写在 `core.json` 与基线的 run manifest 里；两个工作区的 AttemptIdentity 相同时基线可导入（EVALUATE §8、ACTIVITY §4）。
- `improver` 节按 IMPROVER §2 v1.2（`packages_per_round`、`require_code_component`、`l5_per_round`、`hook_aux_calls`、`accept_*`、`holdout_share`、`round_share`）；`gate_every` 删除。`schedule.rounds` 上限 12，`patience` 3。
- `rsi` 节多 `baseline_from`（旧工作区路径，可选）。v1.3：再多 `seed_from`（旧工作区路径，可选：从它的发布线 W\* 起步，DESIGN §6.7）。
- v1.6（阶段 4g，DESIGN §6.16）：`improver` 节多 `audit_additive_only`（true，fixed）、`audit_max_false_alarm`（0.25，cap）；hook 契约的 `RETURNS` 在 `after_turn` 与 `on_finish` 多 `missing`（`action_of`：非空的字符串列表 → feedback / pushback；worker_runner 的内嵌契约同步）；台账阶段多 `audited`、`single_package`。
- v1.5（阶段 4f，DESIGN §6.15）：`improver` 节多 `failures_max_cases`（30，cap）、`failures_case_chars`（4000，cap）；`causes` 的 preset 多 `rule_not_applied`、`vocabulary_mismatch`、`omitted_report`，`mechanizable` 多 `vocabulary_mismatch`（superset 规则不变）。
- v1.4（阶段 4e，DESIGN §6.14）：`bench` 节多 `domains`（free，域名列表；给了它 `domain` 就是它们连起来的串，K 里两者都有）；`final` 节多 `domains`（free，`bench.domains` 的子集，默认全部）；`improver.stream_screen_max` 的 preset 值 30（cap）；`schedule.stream_batch` 的 preset 值 30（cap；跨域的 150 题分 5 批）。
- v1.3：`schedule` 节多 `stream_batch`（10，cap）与 `stream_splits`（`["train"]`，free；只接受 `["train"]` 或 `["train", "test"]`，本版只实现 train 段，含 test 的配置在 `--phase stream` 处拒绝）；`improver` 节多 `audit_hook_share`（0.3，floor）、`stream_screen_max`（20，cap）；`packages_per_round` 的 preset 值改为 3。trace 的 split 守卫在流式的 test 段按"已计分"放行：`stream_scored` 事件记下每道计过分的题，`TraceStore` 与 `Results.grading` 对这些题放行。
- v1.8（阶段 4i，DESIGN §6.18）：`improver` 节多 `regression_evidence`（`research`；counts / abstract / research）、`regression_research_sample`（6，cap）、`frontier_max`（4，cap）、`merge_per_round`（1，cap）、`frontier_min_fixes`（1，floor）、`frontier_max_attempts`（2，cap）、`text_additive_only`（true，fixed）、`heuristic_terms_min`（3，floor）；`replay_max_false_trigger` 的默认 0.2。工作区多 `rounds/frontier.json` 与 `rounds/r<i>/regressions.json`。
- preset `automationbench_v2`：以上默认值，`memory.enabled = false`。

## 5 Release 三元组与收据

```python
@dataclass(frozen=True)
class Release(Record):               # kind "release"
    parent_ref: str | None           # 上一个 release
    package_ref: str                 # W（harness.Package 的 ref）
    improver_ref: str                # R 的策略（improver.ImproverPolicy 的 ref：估值、剧本、提示）
    core_ref: str                    # K
    receipt_ref: str | None          # 装入依据：GateReceipt（gate 发布才有；种子、screen、decision 为 None）
    screen_ref: str | None           # 留出筛选的 ScreenResult（reason screen 才有）
    reason: str                      # "seed" | "screen"（留出筛选接受，工作线；叠装的也是） | "gate"（活动末发布） | "decision"（无 dev 的活动按决定发布，阶段 4c） | "rollback" | "import"（seed_from 的种子，v1.3）

@dataclass(frozen=True)
class GateReceipt(Record):           # kind "receipt"：一次配对 gate 的读数（EVALUATE §5）
@dataclass(frozen=True)
class InstallReceipt(Record):        # kind "install"：装入或回滚的事实（expected_parent、operation_id、outcome）
```

`rsi/active.json` 指向工作线的当前 release，`rsi/released.json` 指向发布线（v1.2，EVALUATE §7）；装入与发布都是 CAS。部署导出只带 `package_ref` 的闭包、运行壳需要的 `core` 子集（limits、model、worker、harness.limits、memory.recall_k）与验证摘要，不带 R、台账、轨迹、凭据。

## 6 记录：transcript 行

每次尝试一个 session，`transcripts/<session>.jsonl`，只追加，宿主写，包写不到。每行 `{"kind", "ts", "seq", ...}`；`seq` 单调，`<session>:L<line>` 是稳定引用。

| kind | 字段 | 写者 |
|---|---|---|
| `task` | `task_id`、`split`、`task_hash`、`attempt_id`、`release_ref`、`core_ref`、`package_ref`、`arm`、`text` | runner |
| `gateway_call` | `call_id`、`attempt_id`、`model`、`role`、`effort_requested`、`effort_sent`、`messages_prefix`（与上一条相同的前缀条数）、`messages_tail`、`tools_digest`、`reply`（text、tool_calls、stop）、`usage`、`ms` | gateway |
| `tool_specs` | `digest`、`specs`（变化时才写） | gateway |
| `tool` | `call_id`、`name`、`args`、`output`（截 `EVENT_CHARS`）、`is_error`、`read_only`、`ms`、`after`（之前那次模型调用的 call_id） | broker |
| `broker_event` | `call_id`、`name`、`mutates`、`refused`、`reason`、`output_digest`、`output_chars`、`read_only` | broker |
| `skill_read` | `name`、`found`、`chars` | skills |
| `library_retrieval` | `query`、`shown`（ids）、`scores`、`site`（"start" \| "recall"） | memory reader / MemoryRecall |
| `hook` | `site`、`name`、`action`（"block" \| "modify" \| "feedback" \| "pushback"；"none" 不记）、`detail`（截断）、`turn`、`source: "package"` | runner（由 worker 的 mark 转写） |
| `mark` | `mark`（包给的 kind）、`data`、`source: "package"` | marks |
| `egress` | `method`、`host`、`port`、`status`、`bytes`、`decision` | egress |
| `protocol` | `error`、`role`、`effort_requested`（网关在调用前拒绝的模型调用） | gateway |
| `worker_session` | `seconds`、`requests`、`worker`（describe）、`egress`（代理时） | runner |
| `end` | `termination`（"finished" \| "budget" \| "harness_error" \| "infrastructure"）、`limit`、`error`、`final_text_digest`、`usage`、`cost`、`turns`、`stop` | runner |

## 7 身份规则

| 东西 | 身份 |
|---|---|
| 包 | `package:<digest>`，覆盖 manifest 与全部文件字节（含 vendor、memory 快照） |
| 候选 | 同包的 ref；候选目录导出成包后才有 ref |
| 声明 | `declaration:<digest>`，随候选存档 |
| 尝试 | `attempt_id = f"{run}/{task_id}/r{repeat}/{arm}#{retry}"`（`evaluate.results.attempt_id`）；`arm` 只用字母、数字与 `_ . -`（final 的 W\* 写作 `W_star`） |
| session | 等于 `attempt_id`，用于 transcript 文件名（`/` 写成 `__`） |
| 轮 | `round_id = f"{scope_id}/r{index}"` |
| 运行 | `RunManifest`（现有）：candidate、模型、limits、split 哈希、core_ref 的摘要 |

## 8 错误分类

| 异常 | 含义 | 循环怎么处理 |
|---|---|---|
| `ContractError` | 形状、schema、引用不合法 | 配置或代码错误，立刻停 |
| `CandidateDefect` | 包自己的错：hook 抛错、答非协议、工具抛错、smoke 崩溃。运行中由包抛出的、带 `ouroloop_defect` 标记的异常是 `RunDefect`：尝试记 `harness_error`，按现状评分，失败类 `candidate_defect` 使该次 gate 比较 invalid | 候选在该档失败，退回 R 修订或作废；不算基础设施 |
| `Infrastructure`（`WorkerUnavailable`、`WorkerCrashed`、`EgressUnavailable`、`MemorySidecarError`；`ProviderError` 留在 `providers/`） | 宿主侧故障 | 尝试标 `infrastructure`，按日程的 `max_infrastructure_failures` 计数；不进 gate 读数 |
| `LimitHit(limit)` | 五项硬限额之一到顶 | 尝试正常结束，termination `budget`，世界按现状评分 |
| `BudgetError`（`BudgetExhausted`、`BudgetMismatch`） | 预算簿拒绝 | 付费步骤不开始；活动结束 `budget_exhausted` |
| `TrainOnlyError` | 要读 dev/test 的内容 | 立刻停，记事件；这是防泄漏的硬边界 |
| `StaleWorker` | 镜像的 runner 与宿主不一致 | 启动检查失败 |

## 9 配置文件形状（TOML）

```toml
[workspace]  root = "workspace-<name>"
[bench]      domain = "finance"; upstream_path = "~/workspace/AutomationBench"; split_rule = "stratified"; seed = 1
[bench.frozen_splits] finance = "4fd91eaf9fb8daf5"
[limits]     max_steps = 50; max_model_calls = 200; max_tool_calls = 200; max_seconds = 900; max_tokens = 400000
[model]      type = "claude_code"; model = "claude-opus-5-5"; effort = "medium"; max_tokens = 32000; timeout = 600
[research_model] 同上，timeout = 900
[pricing]    input_per_1k = 0.004; cache_read_per_1k = 0.0002; cache_write_per_1k = 0.008; output_per_1k = 0.02
[harness]    preset = "automationbench_v1"; seed = "stirrup"        # preset 必填；其余字段只能收紧
[worker]     network = "none"; memory = "512m"; pids = 128           # 只能收紧
[memory]     enabled = true
[improver]   k = 3
[schedule]   rounds = 3; patience = 2
[budget]     usd = 450.0; final_usd = 160.0
[rsi]        scope_id = "finance-v1"; core_ref = ""; split_ref = ""; final_plan_ref = ""   # freeze 打印后填入
[final]      repeats = 3; tasks = "test"                              # 只在要封存时写
```

旧表名（`[llm]`、`[research]`、`[improvement]`、`[harness_evolution]`、`[freeze]`、`[estimate]`、`[trainer]`）不再读；读到就报错并指出新名。四个旧工作区的配置不在 HEAD 上跑（规则 15），已从 `examples/` 删除（在 tag 上）。

## 10 测试约定

- 两套命令不变：默认 `.venv/bin/python -m pytest -q`；全量 `OUROLOOP_TEST_REQUIRE_DOCKER=1 ... -m "not live"`。
- 夹具：`tests/fake_upstream`（`rsi`、`marketing`、`sales`、`support` 四个迷你域）；`ScriptedChat`（脚本化模型）；`ProcessWorker`（协议测试，`qualified=False`）；`tests/fixtures/attempt_behaviour_<seed>.json`（一次尝试的行为夹具，换种子时重录）。
- 必须一直绿：`test_record_compat`（读四个旧工作区的 train/dev 记录，只读）、`test_attempt_behaviour`、`test_paid_run_safety`（名单见 EVALUATE §9）、`test_hot_path_boundary`。
- 每个模块文档的“测试”一节列出必须有的测试；每项改动都要有一个去掉改动就失败的测试；变异检查只在隔离副本里做（PLAN 规则 7）。
- 不为原则加审查（PLAN 规则 19）。
