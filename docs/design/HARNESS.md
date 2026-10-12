# harness：包模型、组件类型、种子与 vendor、包侧契约

> 阶段 1 设计文档。包 `ouroloop/harness/`。对应 [DESIGN](../DESIGN.md) §3、§4。来源：`agent/harness_package.py`（HEAD 与 `v6-agent` 分支）、`agent/seed_v6/`。2026-10-09 按阶段 1 的实现对齐。

## 1 职责与不做

**做：** 定义一个 harness 包是什么（manifest、组件类型、位置、上限）；构建种子（上游 vendor + 适配层 + 补丁栈 + 种子 hook）；检查形状、字面量、声明与 diff 的一致；定义包侧契约（ctx、hook 事件、工具入口、agent 入口）。

**不做：** 不运行包里的任何代码（runtime 的事）；不评分；不决定装不装（evaluate）；不生成候选（improver）。

## 2 文件

| 文件 | 内容 |
|---|---|
| `harness/package.py` | `Manifest`、`Package`、`Limits`、`classify`、`parse_manifest`、`check_shape`、`build`、`files_of`、`apply_patch`、`diff` |
| `harness/kinds.py` | 组件类型表：位置模式、manifest 条目、是代码还是文本、验收档、记功条件（§3） |
| `harness/contract.py` | 包侧契约：ctx 的名字与形状、hook 站点与事件、各站点返回形状与 `action_of`（把 hook 的答复归为 none/block/modify/feedback/pushback，越界即 `HookAnswerError`）、worker 协议的 op 与回调；`as_json()` 是 `worker_runner.py` 嵌入的常量 |
| `harness/literals.py` | 字面量索引与检查（相对种子改动的行） |
| `harness/seed.py` | `build_seed(name, core) -> Package`：从 `harness/seeds/<name>/` 构建 |
| `harness/vendor.py` | `PROVENANCE.json`、补丁栈的应用与提取、上游升级 |
| `harness/seeds/stirrup/` | `vendor/stirrup/**`、`agent/stirrup_agent.py`（v6 适配层，原 `default_agent.py` 的执行与 hook 分发并入）、`agent/http_fetch.py`、`middlewares/write_guard.py`（ledger、triage、anchor、verify 四个 hook）、`tools/plan_changes.py`、`prompt/system.md`（空）、`prompt/finish.md`（原 FINISH_CHECK）、`tests/*.py`、`patches/`（目前只有 README）、`manifest.json` |

## 3 组件类型与上限

```python
SITES = ("before_run", "before_model", "before_tool", "after_tool", "after_turn", "on_finish")
KINDS = ("agent", "vendor", "tool", "hook", "prompt", "skill", "memory", "model", "config", "test")
CREDITS = ("all", "invoked", "triggered", "cited", "read", "none")      # test 的记功是 none：它不在尝试里运行
CODE_KINDS = ("agent", "vendor", "tool", "hook", "test")
TEXT_KINDS = ("prompt", "skill")
HOST_KINDS = ("memory",)              # 宿主写、R 不能写、worker 不装载

PATTERNS = (                          # path -> kind；不匹配即 PackageError
    ("manifest", r"^manifest\.json$"),
    ("agent",    r"^agent/[a-z0-9_]+\.py$"),
    ("vendor",   r"^vendor/[a-z0-9_]+/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+$"),
    ("tool",     r"^tools/[a-z0-9_]+\.py$"),
    ("hook",     r"^middlewares/[a-z0-9_]+\.py$"),
    ("prompt",   r"^prompt/(system|finish)\.md$"),
    ("skill",    r"^skills/[a-z0-9][a-z0-9_/-]*\.md$"),
    ("memory",   r"^memory/snapshot\.jsonl$"),
    ("test",     r"^tests/[a-z0-9_]+\.py$"),
)

@dataclass(frozen=True)
class Limits:                         # K 的 harness.limits；R 不能放宽
    max_files: int = 96
    max_package_bytes: int = 786432   # 768 KiB：种子里的 Stirrup 约 550 KiB
    max_vendor_bytes: int = 655360
    max_memory_bytes: int = 262144
    max_prompt_chars: int = 8000
    max_skill_chars: int = 8000
    max_skills: int = 32
    max_tools: int = 16
    max_hooks: int = 12
    max_tool_description: int = 1024
    max_schema_depth: int = 16
    max_tests: int = 24
    kinds: tuple[str, ...] = KINDS
    sites: tuple[str, ...] = SITES
    config_ranges: dict[str, tuple] = ...   # {"max_turns": (10, 80), "summarize_at": (0.5, 0.9), "shown_k": (0, 5), "subagent_depth": (0, 2)}
    model_base: str | None = None           # K 的 model.identity
    max_effort: str = "medium"              # K 的 model.max_effort
    @classmethod
    def from_core(cls, core) -> "Limits"    # K 的 harness.limits、kinds、sites、config_ranges 与 model
```

包文件都是 UTF-8 文本（无 NUL、无孤立代理）；二进制文件拒绝（种子与上游 Stirrup 都是文本）。`memory/snapshot.jsonl` 单独计 `max_memory_bytes`，不计入文件数与包字节。memory 写快照时就按这个上限装（精简行，按记功排序取前缀，没装的记下：MEMORY §7.1），所以 library 阶段的快照总放得下；这里的检查是最后的守卫。

数值来自证据（V0_6 §8）：原 24 文件 / 256 KiB 放不下 vendor；worker 实测峰值 80 MB。

## 4 manifest

```json
{
  "api_version": "harness_package.v3",
  "agent":   {"entrypoint": "agent/stirrup_agent.py:run", "roles": ["act", "aux"]},
  "vendor":  [{"name": "stirrup", "provenance": "vendor/stirrup/PROVENANCE.json"}],
  "tools":   [{"name": "plan_changes", "description": "...", "input_schema": {...}, "entrypoint": "tools/plan_changes.py:run", "writes": false}],
  "hooks":   [{"name": "triage", "site": "before_tool", "entrypoint": "middlewares/write_guard.py:triage"}],
  "prompt":  {"system": "prompt/system.md", "finish": "prompt/finish.md"},
  "skills":  [{"name": "reconcile-tables", "path": "skills/reconcile-tables.md", "enabled": true}],
  "memory":  {"enabled": true, "snapshot": "memory/snapshot.jsonl"},
  "model":   {"base": "<K 的 model.identity>", "effort": "medium", "max_tokens": 32000, "artifact_ref": null},
  "config":  {"max_turns": 50, "summarize_at": 0.7, "shown_k": 2, "subagent_depth": 1},
  "tests":   ["tests/test_write_guard.py"]
}
```

规则：
- `api_version` 只有 v3；旧的 v1/v2 包不在 HEAD 上跑。
- `model.base` 必须等于 K 的 `model.identity`；`effort` ≤ K 的 `max_effort`；`artifact_ref` 只能指向训练链产物（EVALUATE §8），为 null 即 pin 的基座。
- 阶段 1 的 stirrup 种子不把 manifest 的 `model.effort` 传给 `ctx.model`（`CtxClient` 不带 effort），调用按 provider 固定的 K 的 effort（medium）走；候选改 `model.effort` 目前不生效。种子还按 `ctx.limits["act_calls"]` 给自己的回合封顶，所以用完回合的尝试记作 `finished`（`stirrup_end` 标记里 `finished: false`），而不是 `budget`/`act_calls`。
- `config` 的每个键在 `Limits.config_ranges` 内；种子构建时 `config` 用默认值补全。
- `agent` 必填（每个 v3 包自带循环）；`tools[]`、`hooks[]`、`skills[]` 可带 `enabled`；名字在三者间唯一，包工具不能用宿主工具名（`read_skill`、`recall`）。
- `prompt` 的每个键指向 `prompt/<key>.md` 且文件在包里；没被点名的 prompt、skill、test 文件拒绝；`vendor/<name>/` 与 `vendor` 条目一一对应。
- `memory.snapshot` 出现当且仅当包里有 `memory/snapshot.jsonl`。
- `tools[].writes` 由入口声明；broker 对 `writes=false` 的工具强制 `read_only`。
- `hooks[].site` ∈ K 的 `harness.sites`。
- `memory.snapshot` 只由 memory 包写（HOST_KINDS）；R 的候选改它即 `PackageError`。

## 5 包侧契约（`harness/contract.py`）

包里的 Python 只认这些名字；它们在 worker（基准）和生产里语义相同。常量与形状由本文件定义，`runtime/worker_runner.py` 与种子适配层都从这里取（runner 是标准库文件，所以常量以生成的 JSON 嵌入，构建时核对一致）。

### 5.1 ctx

```python
class Ctx(Protocol):
    def model(self, messages: list[dict], tools: list[dict] = (), *, role: str = "act", effort: str | None = None) -> ModelReply
    def tool(self, name: str, args: dict, *, read_only: bool = False) -> ToolReply
    skills: Skills                      # .index() -> list[{name, description}]; .read(name) -> str | None
    memory: Memory                      # .recall(query: str, k: int | None = None) -> list[Entry]；没有 memory 时返回 []
    limits: dict                        # {"act_calls", "model_calls", "tool_calls", "seconds", "tokens"} 剩余（tokens 是输出 token，与 LimitHit 的维度名一致）
    view: dict                          # 只读投影：system, base_tools, package, last_usage, events(最近 20 条), max_output_chars
    def mark(self, kind: str, **data) -> None

ModelReply = {"text": str, "tool_calls": [{"id", "name", "args"}], "stop": str}
ToolReply  = {"output": str, "is_error": bool}
Entry      = {"id", "when", "content", "score"}
```

### 5.2 hook

```python
def <hook>(ctx: Ctx, event: dict) -> dict | None
```

| site | event | 允许的返回 | 宿主记录的 action |
|---|---|---|---|
| `before_run` | `{site, task, system, messages}` | `{system?: str, messages?: list}` | modify |
| `before_model` | `{site, turn, role, messages, tools}` | `{messages?: list}`（非破坏：原 messages 不变） | modify |
| `before_tool` | `{site, turn, call_id, name, args}` | `{block: {reason}}` 或 `{args: dict}` | block / modify |
| `after_tool` | `{site, turn, call_id, name, args, output, is_error}` | `{output?: str, feedback?: str}` | modify / feedback |
| `after_turn` | `{site, turn, tool_results: [...]}` | `{message?: str}` | feedback |
| `on_finish` | `{site, turn, reason, final_text, pushbacks}` | `{pushback?: str}` | pushback |

返回 `None` 即无动作。返回形状之外的键是 `CandidateDefect`。hook 抛异常是 `CandidateDefect`（适配层抛 `HookError`，带 `ouroloop_defect` 标记，宿主记 `harness_error`）。每次**有动作的**触发由适配层 `ctx.mark("hook", site=, name=, action=, detail=, turn=)` 上报，宿主转写成 `hook` 行；action 为 none 的不记。

hook 拿到的 ctx 的 `tool` 一律只读：hook 只看不做，V2b 才能在记录的事件上回放它。v1.2（DESIGN §6.4）：`after_turn` 与 `on_finish` 的 hook 可以调 `ctx.model(..., role="aux")` 审核 agent 的工作，一次尝试内所有这类 hook 合计不超过 `ctx.view["hook_aux_calls"]`（K 的 `improver.hook_aux_calls`）次，超出或在其他站点调用都是 `CandidateDefect`；这样的 hook 在声明里标 `uses_model`，没有 V2b（回放不了模型），V1 的 smoke 用脚本化模型答一次。每次 aux 调用由适配层 `ctx.mark("hook_aux_call", ...)` 上报。各站点的语义：`before_run` 在系统提示合成后、第一次调用前，hook 看到前面 hook 加的消息；`before_model` 对 act 调用和 hook 的 aux 调用都过（摘要的 aux 调用不过）：审核 hook 的 aux 模型和 actor 一样被 CLI 告知真实日期，日期锚这类约束要一起跟过去（finance-v5 第 1 轮，JOURNAL 2026-10-12）；`before_tool` 第一个 block 结束本回合（同回合其余调用与 finish 都“Not executed”），`args` 改写按 manifest 顺序传给下一个 hook；`after_tool` 的 output 改写串联、feedback 汇总成回合结果后的一条 user 消息；`after_turn` 只在主 agent 的回合末触发（子 agent 的回合不触发）；`on_finish` 的 `reason` 是 "finish" 或 "implicit"（第 N 次纯文本答复转成的 finish），第一个 pushback 生效，最多 `MAX_PUSHBACKS`（2）次且最后一次 act 调用不退回。

### 5.3 工具入口

```python
def run(ctx: Ctx, args: dict) -> dict      # {"output": str} 或 {"output": str, "is_error": true}；纯字符串也算 output
```
入口在 worker 里运行；世界只经 `ctx.tool`；工具的 ctx 也没有 `model`；`writes=false` 的工具，适配层给它的 `ctx.tool` 强制只读。工具抛异常或答复不是工具结果，适配层抛 `PackageToolError`（`ouroloop_defect`）。

### 5.4 agent 入口

```python
def run(ctx: Ctx, task: str) -> str        # 返回最终文本；硬限额由宿主以 LimitHit 结束
```

### 5.5 toolcheck 脚本（只给检查，不进包）

```python
def check(ctx: Ctx, tools: dict[str, Callable]) -> None   # 以 tools.get(name)(args) 调候选的工具入口；世界经可写 broker
```

## 6 接口

```python
# package.py
@dataclass(frozen=True)
class Manifest: ...                                      # §4 的字段，from_json/to_json，校验在 parse_manifest
@dataclass(frozen=True)
class Package(Record):                                   # kind "package"；存储的 payload 是 {manifest, files: {path: file 对象的 ref}}
    manifest: Manifest
    files: dict[str, str]                                # path -> 文本（只收文本）
    @property
    def kinds(self) -> set[str]
    def component(self, kind: str) -> list[dict]
def classify(path: str) -> str
def parse_manifest(text: str, files: dict[str, str], *, limits: Limits) -> Manifest
def check_shape(files: dict[str, str], *, limits: Limits, seed: Package | None, parent: Package | None = None) -> list[str]
    # 形状、上限、vendor 保护文件（与 seed 相同）、HOST_KINDS 未改（与 parent 相同；memory 包构建时不给 parent）
def build(objects: Objects, files: dict[str, str], *, limits: Limits, seed: Package | None, parent: Package | None = None) -> Package
def load(objects: Objects, package_ref: str) -> Package
def files_of(objects: Objects, package_ref: str) -> dict[str, str]
def run_files(package: Package) -> dict[str, str]        # 装进 worker 的文件：去掉 tests/ 与 memory/
def apply_patch(parent: Package, patch: dict[str, str | None]) -> dict[str, str]   # None 即删除
@dataclass(frozen=True)
class Diff:
    changed: dict[str, str]                              # path -> "added" | "modified" | "deleted"
    kinds: set[str]
    sites: set[str]
    vendor_touched: bool
def diff(parent: Package, files: dict[str, str]) -> Diff
def needs_worker(package: Package) -> bool               # 永远 True（种子带 agent）；留作接口

# kinds.py
@dataclass(frozen=True)
class Kind:
    name: str; code: bool; host_only: bool; ladder: tuple[str, ...]; credit: str   # credit ∈ {"all", "invoked", "triggered", "cited", "read"}
KIND_TABLE: dict[str, Kind]

# literals.py
def literal_index(rows: Iterable[dict]) -> LiteralIndex     # 只从 train 建：行 {task_id, assertions, state?}；断言是评分器自己的扁平 dict（type 之外的字段）或尝试记下的 {type, params}
def forbidden(index: LiteralIndex, *, exempt: set[str] = frozenset()) -> set[str]
def changed_lines(text: str, base: str | None) -> set[int]
def findings(files: dict[str, str], seed: Package | None, forbidden: set[str], *, allowed: set[str] = frozenset()) -> list[dict]
    # 只扫相对种子改动的行与新文件（含路径、manifest 里新出现的解码字符串）；seed=None 扫全部；返回 [{path, line, literal, kind}]
def describe(found) -> str                                       # 给 R 的检查输出

# seed.py
def build_seed(name: str, *, core: dict, objects: Objects) -> Package
def seed_dir(name: str) -> Path
def protected_files(name: str) -> dict[str, bytes]      # vendor/<name>/{LICENSE, PROVENANCE.json}

# vendor.py
@dataclass(frozen=True)
class Provenance: name: str; version: str; url: str; tag: str; sha256: str; license: str; files: int; hashes: dict; license_sha256: str
def read_provenance(files: dict[str, str], name: str) -> Provenance
def verify(files: dict[str, str], name: str) -> list[str]      # 每个文件的 sha256 与 PROVENANCE 相符、没有多出的文件、LICENSE 相符
def apply_unified(files, patch_text, *, name) -> dict[str, str]  # 统一 diff 的应用（PatchError）
def apply_patches(vendor_files: dict[str, str], patches_dir: Path) -> tuple[dict[str, str], list[str]]   # 失败的补丁名
def extract_patch(seed: Package, installed: Package, name: str) -> str | None   # 装入后把 R 对 vendor 的改动抽成补丁文本
def upgrade(seed_dir: Path, new_source: Path, provenance: Provenance) -> list[str]   # 换上游、重放补丁；返回失败的补丁
```

## 7 种子 `stirrup` 的构建

1. `vendor/stirrup/**` 来自 Stirrup 0.2.0 的 sdist（`PROVENANCE.json`：version、tag、sdist 的 sha256、license=MIT、每个文件的 sha256、文件数）；构建时逐文件校验 sha256，不符即 `SeedUnavailable`。
2. `patches/NNN-<slug>.patch` 依次重放（当前为空：七处修正都在适配层里）。
3. 适配层、守卫、`plan_changes`、prompt、tests 从 `seeds/stirrup/` 拷入。
4. `manifest.json` 的 `model.base` 填 K 的 identity，`config` 取默认，`memory.enabled` 取 K。
5. `check_shape` 通过，`build` 入库；种子 release 由 activity 写。

适配层职责（`agent/stirrup_agent.py`，沿用 v6-agent 的实现并扩站点）：`LLMClient` → `ctx.model`；`Tool` → 基础工具经 `ctx.tool`、包工具经入口、`read_skill`、`recall`（manifest 的 memory 开着才提供）；六个站点的分发与 `mark`；finish 的七处修正；`config.max_turns`、`summarize_at` 生效，子 agent 只在 `config.subagent_depth ≥ 1` 时提供；摘要用 role `aux`。

种子的写入守卫（DESIGN §4.2）：`ledger`（after_tool，记写操作、计划与读到的最新日期，第一次写之后不再读日期）、`triage`（before_tool，未调 `plan_changes` 的第一次写阻止一次）、`anchor`（before_model，读到日期后，在第一次知道这个日期的 act 调用末尾放一条日期注记，之后每次调用都把它留在原位；更新的日期另放一条；不改历史。注记不挪位置，所以每次调用的前段就是上一次调用，提供方的提示缓存能读到——v4 的读数下挪到末尾会让每次尝试的成本约 ×3.5）、`verify`（on_finish，第一次 finish 退回一次，列出计划、台账与排除项）。种子测试里的示例日期不取任何 train 题的值（smoke 的种子全量字面量扫描会查）。

### 7.1 日期锚的规则（2026-10-10 修订）

finance-v2 的 27 道 train 失败里 5 道是 real_date，三道的锚点本身就是真实日期：QuickBooks 的 `MetaData.CreateTime` / `LastUpdatedTime`、Xero 的 `UpdatedDateUTC` 由模拟世界用真实时钟盖章，锚取"读到的最新日期"就取到了它们。规则改为：

1. **系统盖章的字段不算日期**：字段名含 create / created / updated / modified / last_updated / edited / meta / internal / sync 一类词的，是记录系统的审计时间，不是业务日期（同时去掉 DATE_WORDS 里的 created）。
2. **等于或晚于 worker 当日的日期不算**：数据里不可能有"今天"或未来的业务日期，只有时钟能产生它；生产里今天就是真实日期，少这一个锚点不损失什么。
3. **写之后仍然读日期**，只是写调用自己的结果不读（原来第一次写之后就不再读，ap_aging_report 的首个动作是建草稿，锚从未出现）。
4. **hook 的 aux 调用也带锚**（2026-10-12）：`anchor` 对 role `aux` 的调用（after_turn / on_finish 的 hook 经 `ctx.model` 做的审核）把 DATE_MESSAGE 作为最后一条 user 消息附上；每次 aux 调用是独立的对话，不记位置。finance-v5 第 1 轮的审核 hook 按 CLI 给的真实日期算 90 天续约窗口，把一道通过的题改坏（JOURNAL 2026-10-12 第 1 轮一条）。

## 8 不变量

- 包 ref 覆盖全部字节；任何文件变即新 ref。
- 种子的 vendor 保护文件逐字节不变；候选改它即 `PackageError`。
- `memory/` 只由 memory 包写；`tests/` 不装进 worker。
- 字面量检查只扫相对种子改动的行；种子自身另做一次全量扫描（构建时）。
- 控制面不 import 包代码。

## 9 与现有代码的对应

| 现有 | 处理 |
|---|---|
| `agent/harness_package.py`（HEAD 723 行；v6 加 vendor/library） | 拆成 `package.py`、`literals.py`、`kinds.py`；`library` kind 改名 `memory`；`PackageView`/`prompt_block` 移到 runtime（宿主合成系统提示） |
| `agent/seed_v6/**`（v6-agent） | 搬到 `harness/seeds/stirrup/`；hook 站点扩到六个；manifest 升到 v3 |
| `agent/default_agent.py` | 删；它的执行与 hook 分发并入 `stirrup_agent.py` |
| `tests/test_harness_package.py`、`test_write_guard.py`（v6） | 重写到 `tests/harness/`（`test_harness_package.py`、`test_harness_literals.py`、`test_harness_seed.py`）；`test_worker_resources.py` 归 `tests/runtime/` |

## 10 测试

- 形状：每个 PATTERN 一例合法、一例非法；上限每项一例超限；HOST_KINDS 被候选改动即拒绝；vendor 保护文件改动即拒绝。
- manifest：v1/v2 拒绝；`model.base` 不等于 K 拒绝；effort 超上限拒绝；config 越界拒绝；hook 站点不在 K 内拒绝；`writes` 缺省为 false。
- 字面量：改动行命中报告、未改动的上游行不扫；`allowed` 豁免。
- diff：kinds/sites/vendor_touched 正确；声明与 diff 的一致由 improver 测。
- 种子：构建可重复（同输入同 ref）；sha256 不符即 `SeedUnavailable`；补丁重放失败列出名字。
- 适配层（在 worker 里，ProcessWorker）：六个站点各一例触发与记录；finish 单独一回合；摘要走 aux；子 agent 深度受限；`recall` 经 ctx。
- 契约常量：`contract.py` 与 `worker_runner.py` 嵌入的 JSON 一致（测试比对）。

## 11 开发顺序

`contract.py` → `kinds.py` → `package.py`（含 manifest）→ `literals.py` → `vendor.py`、`seed.py` → 搬入种子并扩站点 → 测试。runtime 的 worker_runner 协议同步改（RUNTIME §4）。
