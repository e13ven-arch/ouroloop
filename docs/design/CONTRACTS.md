# 公共契约：对象、工作区、K、release、记录、错误、测试约定

> 阶段 1 设计文档。所有包共用的形状都在这里定；各包文档只引用。实现文件：`ouroloop/contracts.py`（由 `evolve/contracts.py` 收缩而来）、`ouroloop/records.py`（记录行的 kind 与字段）。

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
- 一次尝试（`evaluate.run_attempt`）跑完，`sys.modules` 里没有 `ouroloop.improver`、`ouroloop.memory.sidecar`、`ouroloop.train`。
- 控制面进程永远不 import 包里的代码（`vendor/`、`agent/`、`tools/`、`middlewares/`、`tests/`）；它们只在 worker 里跑。
- `adapters/` 不 import 本框架的其他包。

## 2 对象与引用

现有 `evolve/contracts.py` 的对象库保留，收缩成 `ouroloop/contracts.py`：

```python
def canonical(obj) -> bytes            # 规范 JSON（键排序、无空白、UTF-8）
def digest(obj) -> str                 # sha256(canonical(obj)) 的十六进制
def ref_for(kind: str, payload) -> str # f"{kind}:{digest(payload)[:16]}"
def parse_ref(ref) -> tuple[str, str]
def short(ref) -> str                  # 前 12 位，报告与日志用

class Record:                          # 所有冻结记录的基类：frozen dataclass，to_json/from_json，ref 由内容算
class Objects:                         # 内容寻址的 JSON 对象库：<root>/objects/<kind>/<digest>.json
    def put(self, kind, payload) -> str
    def get(self, ref) -> dict
    def load(self, ref, cls: type[Record]) -> Record
    def has(self, ref) -> bool
class Events:                          # 只追加的 JSONL：<root>/events.jsonl，每行 {ts, kind, ...}
    def append(self, kind, **fields) -> dict
    def read(self, kind=None) -> Iterator[dict]
def durable_write(path, data: bytes)   # 写临时文件再 rename
def copy_closure(src: Objects, dst: Objects, refs) -> set[str]   # 导出时拷贝引用闭包
```

删掉：`PolicyDescriptor`、`PolicySet`、`SystemRelease`、`EvaluationPermit`、`Authorization`、`TargetHarness`、`CollectionRequest`、`DecisionTrace`/`Observation`/`FeedbackRecord`（最后三个搬进 `train/`，只有训练用）。

保留并改名到各自包：`ExperimentProposal`/`Experiment` → `improver.Candidate`；`EvaluationReceipt`/`InstallationReceipt` → `evaluate.GateReceipt`/`evaluate.InstallReceipt`；`CoreManifest` → `activity.Core`；`HarnessPackage` → `harness.Package`；`DeploymentManifest` → `runtime.AppManifest`；`ModelIdentity` → `evaluate.ModelIdentity`。

## 3 工作区布局

一个活动一个根目录（git 忽略）。已有的目录不动，新包按下表加：

```
<root>/
  core.json                     K 的冻结副本（activity 写），core_ref = ref_for("core", core.json)
  splits/<domain>.json          冻结的划分（evaluate.SplitManifest.freeze）
  objects/                      Objects
  events.jsonl                  Events
  releases/                     active.json 与 release 记录（evaluate.release）
  budget/book.json              预算簿（evaluate.budget.Budgets）
  results/<domain>/             results.jsonl、runs/、grading.jsonl（evaluate.results）
  transcripts/<session>.jsonl   每次尝试一份（runtime.records）
  index/trace.sqlite            trace 的 FTS 索引，派生、可重建（improver.evidence）
  rounds/<round_id>/            一轮改进器的产物：causes.jsonl、assignments.json、session.jsonl、candidates/、verdicts/
  ledger/experience.jsonl       改进器台账（improver.ledger）
  memory/<line>/                chroma/、live.jsonl、ledger.json、snapshots/（memory）
  egress/<attempt>.jsonl        代理出站记录（runtime.egress）
  report.json、report.md        报告（activity.report）
```

## 4 K：固定核心

K 是配置经 preset 补全、按“只能收紧”规则合并后冻结的一份 JSON（`core.json`），`core_ref` 覆盖它的全部字节，并含 `code_version`（框架源码的摘要），所以改任何 `.py` 都是新 K（PLAN 规则 13）。

| 节 | 字段 | 谁用 |
|---|---|---|
| `bench` | `domain`、`upstream`（版本与承诺哈希）、`toolset`、`ratios`、`seed`、`split_rule`、`frozen_split_hash`、`near_duplicate_threshold` | evaluate.splits、adapters |
| `limits` | 五项硬限额：`max_steps`、`max_model_calls`、`max_tool_calls`、`max_seconds`、`max_tokens`；`max_output_chars` | runtime、evaluate |
| `model` | actor：`provider`（claude_code）、`identity`（模型名）、`effort`、`max_tokens`、`timeout`；`max_effort`（= effort） | runtime.gateway |
| `research_model` | R 与 aux 调用的 provider、identity、effort、max_tokens、timeout | improver、memory |
| `pricing` | 名义价目：`input_per_1k`、`cache_read_per_1k`、`cache_write_per_1k`、`output_per_1k`、`max_input_tokens`、`max_output_tokens` | evaluate.budget |
| `harness` | `seed`（种子名）、`api_version`、`kinds`（允许的组件类型）、`sites`（允许的 hook 站点）、`limits`（HARNESS §3 的 Limits 字段）、`config_ranges`（每个 config 键的范围） | harness、improver |
| `worker` | `image`、`image_id`、`cpus`、`memory`、`memory_swap`、`pids`、`tmpfs_mb`、`network`（`"none"` 或 `{mode:"proxy", allow:[{host,port,methods}]}`）、`env` | runtime.worker、runtime.egress |
| `memory` | MEMORY §3 的 Settings 字段；`enabled` | memory |
| `improver` | IMPROVER §2 的 Settings：会话上限、`k`、筛选、分层、阶梯、档案、反膨胀的 N | improver |
| `gate` | `min_gain`、`min_tasks`、`max_regression_rate`、`regression_drop`、`confirmations`、`cost_beta0/1`、`control_readings`、`control_pooling` | evaluate.gate |
| `schedule` | `rounds`、`patience`、`max_infrastructure_failures` | activity |
| `budget` | `usd`（根）、`final_usd`（预留份额）、每轮份额 | evaluate.budget、activity |
| `final` | `repeats`、`tasks`（"test"）、`arms`、`claims` | evaluate.final |

preset 与配置的关系：preset 给出每个字段的默认值与上限；配置只能把数值改小、把集合改窄、把 effort 改低（“只能收紧”，`activity.config.merge`）；超出即启动错误。preset 本身在代码里（`activity/presets.py`），新 preset 名随版本号。

## 5 Release 三元组与收据

```python
@dataclass(frozen=True)
class Release(Record):               # kind "release"
    parent_ref: str | None           # 上一个 release
    package_ref: str                 # W（harness.Package 的 ref）
    improver_ref: str                # R 的策略（improver.ImproverPolicy 的 ref：估值、剧本、提示）
    core_ref: str                    # K
    receipt_ref: str | None          # 装入依据：GateReceipt（种子 release 为 None）
    reason: str                      # "seed" | "gate" | "rollback" | "import"

@dataclass(frozen=True)
class GateReceipt(Record):           # kind "receipt"：一次配对 gate 的读数（EVALUATE §5）
@dataclass(frozen=True)
class InstallReceipt(Record):        # kind "install"：装入或回滚的事实（expected_parent、operation_id、outcome）
```

`releases/active.json` 指向当前 release；装入是 CAS（EVALUATE §7）。部署导出只带 `package_ref` 的闭包、运行壳需要的 `core` 子集（limits、model、worker、harness.limits）与验证摘要，不带 R、台账、轨迹、凭据。

## 6 记录：transcript 行

每次尝试一个 session，`transcripts/<session>.jsonl`，只追加，宿主写，包写不到。每行 `{"kind", "ts", "seq", ...}`；`seq` 单调，`<session>:L<line>` 是稳定引用。

| kind | 字段 | 写者 |
|---|---|---|
| `task` | `task_id`、`split`、`task_hash`、`attempt_id`、`release_ref`、`core_ref`、`package_ref`、`arm` | runner |
| `gateway_call` | `call_id`、`role`、`effort_requested`、`effort_sent`、`messages_prefix`（与上一条相同的前缀条数）、`messages_tail`、`tools_digest`、`reply`（text、tool_calls、stop）、`usage`、`ms` | gateway |
| `tool_specs` | `call_id`、`specs`（变化时才写） | gateway |
| `tool` | `call_id`、`name`、`args`、`output`（截 `EVENT_CHARS`）、`is_error`、`read_only`、`ms` | broker |
| `broker_event` | `call_id`、`name`、`mutates`、`refused`、`reason`、`output_digest` | broker |
| `skill_read` | `name`、`chars` | skills |
| `library_retrieval` | `query`、`shown`（ids）、`scores`、`site`（"start" \| "recall"） | memory reader |
| `hook` | `site`、`name`、`action`（"none" \| "block" \| "modify" \| "feedback" \| "pushback"）、`detail`（截断） | runner（由 worker 的 mark 转写） |
| `mark` | `kind`、`data`、`source: "package"` | marks |
| `egress` | `method`、`host`、`port`、`status`、`bytes`、`decision` | egress |
| `end` | `termination`（"finished" \| "budget" \| "infrastructure"）、`limit`、`final_text_digest`、`usage`、`cost` | runner |

## 7 身份规则

| 东西 | 身份 |
|---|---|
| 包 | `package:<digest>`，覆盖 manifest 与全部文件字节（含 vendor、memory 快照） |
| 候选 | 同包的 ref；候选目录导出成包后才有 ref |
| 声明 | `declaration:<digest>`，随候选存档 |
| 尝试 | `attempt_id = f"{run}:{task_id}:{repeat}:{arm}[:r{retry}]"`（现有 `benchmark.attempt_id`） |
| session | 等于 `attempt_id`，用于 transcript 文件名 |
| 轮 | `round_id = f"{scope_id}/r{index}"` |
| 运行 | `RunManifest`（现有）：candidate、模型、limits、split 哈希、core_ref 的摘要 |

## 8 错误分类

| 异常 | 含义 | 循环怎么处理 |
|---|---|---|
| `ContractError` | 形状、schema、引用不合法 | 配置或代码错误，立刻停 |
| `CandidateDefect` | 包自己的错：hook 抛错、答非协议、工具抛错、smoke 崩溃 | 候选在该档失败，退回 R 修订或作废；不算基础设施 |
| `Infrastructure`（`WorkerCrashed`、`EgressUnavailable`、`ProviderError`（可重试）、`MemorySidecarError`） | 宿主侧故障 | 尝试标 `infrastructure`，按日程的 `max_infrastructure_failures` 计数；不进 gate 读数 |
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
[harness]    preset = "automationbench_v1"; seed = "stirrup"        # 其余字段只能收紧
[worker]     network = "none"; memory = "512m"; pids = 128           # 只能收紧
[memory]     enabled = true
[improver]   k = 3
[schedule]   rounds = 3; patience = 2
[budget]     usd = 450.0; final_usd = 160.0
[rsi]        scope_id = "finance-v1"; core_ref = ""; split_ref = ""   # freeze 打印后填入
[final]      repeats = 3; tasks = "test"                              # 只在要封存时写
```

旧表名（`[llm]`、`[research.llm]`、`[improvement]`、`[harness_evolution]`、`[freeze.*]`）不再读；读到就报错并指出新名。四个旧工作区的配置不在 HEAD 上跑（规则 15）。

## 10 测试约定

- 两套命令不变：默认 `.venv/bin/python -m pytest -q`；全量 `OUROLOOP_TEST_REQUIRE_DOCKER=1 ... -m "not live"`。
- 夹具：`tests/fake_upstream`（`rsi`、`marketing`、`sales`、`support` 四个迷你域）；`ScriptedChat`（脚本化模型）；`ProcessWorker`（协议测试，`qualified=False`）；`tests/fixtures/attempt_behaviour_<seed>.json`（一次尝试的行为夹具，换种子时重录）。
- 必须一直绿：`test_record_compat`（读四个旧工作区的 train/dev 记录，只读）、`test_attempt_behaviour`、`test_paid_run_safety`（名单见 EVALUATE §9）、`test_hot_path_boundary`。
- 每个模块文档的“测试”一节列出必须有的测试；每项改动都要有一个去掉改动就失败的测试；变异检查只在隔离副本里做（PLAN 规则 7）。
- 不为原则加审查（PLAN 规则 19）。
