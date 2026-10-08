# harness：包模型、组件类型、种子与 vendor、包侧契约

> 阶段 1 设计文档。包 `ouroloop/harness/`。对应 [DESIGN](../DESIGN.md) §3、§4。来源：`agent/harness_package.py`（HEAD 与 `v6-agent` 分支）、`agent/seed_v6/`。

## 1 职责与不做

**做：** 定义一个 harness 包是什么（manifest、组件类型、位置、上限）；构建种子（上游 vendor + 适配层 + 补丁栈 + 种子 hook）；检查形状、字面量、声明与 diff 的一致；定义包侧契约（ctx、hook 事件、工具入口、agent 入口）。

**不做：** 不运行包里的任何代码（runtime 的事）；不评分；不决定装不装（evaluate）；不生成候选（improver）。

## 2 文件

| 文件 | 内容 |
|---|---|
| `harness/package.py` | `Manifest`、`Package`、`Limits`、`classify`、`parse_manifest`、`check_shape`、`build`、`files_of`、`apply_patch`、`diff` |
| `harness/kinds.py` | 组件类型表：位置模式、manifest 条目、是代码还是文本、验收档、记功条件（§3） |
| `harness/contract.py` | 包侧契约：ctx 的名字与形状、hook 站点与事件、工具与 agent 入口的签名；`worker_runner.py` 与适配层共用的常量 |
| `harness/literals.py` | 字面量索引与检查（相对种子改动的行） |
| `harness/seed.py` | `build_seed(name, core) -> Package`：从 `harness/seeds/<name>/` 构建 |
| `harness/vendor.py` | `PROVENANCE.json`、补丁栈的应用与提取、上游升级 |
| `harness/seeds/stirrup/` | `vendor/stirrup/**`、`agent/stirrup_agent.py`、`agent/http_fetch.py`、`middlewares/write_guard.py`、`tools/plan_changes.py`、`prompt/*.md`、`tests/*.py`、`patches/`、`manifest.json` |

## 3 组件类型与上限

```python
SITES = ("before_run", "before_model", "before_tool", "after_tool", "after_turn", "on_finish")
KINDS = ("agent", "vendor", "tool", "hook", "prompt", "skill", "memory", "model", "config", "test")
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
```

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
- `config` 的每个键在 `Limits.config_ranges` 内。
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
    memory: Memory                      # .recall(query: str, k: int | None = None) -> list[Entry]
    limits: dict                        # {"act_calls", "model_calls", "tool_calls", "seconds", "output_tokens"} 剩余
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

返回 `None` 即无动作。返回形状之外的键是 `CandidateDefect`。hook 抛异常是 `CandidateDefect`（该次尝试记 `harness_error`）。每次触发由适配层 `ctx.mark("hook", site=, name=, action=)` 上报，宿主转写成 `hook` 行。

### 5.3 工具入口

```python
def run(ctx: Ctx, args: dict) -> dict      # {"output": str} 或 {"output": str, "is_error": true}
```
入口在 worker 里运行；世界只经 `ctx.tool`；`writes=false` 的工具调 `ctx.tool(..., read_only=True)`。

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
class Package(Record):                                   # kind "package"
    manifest: Manifest
    files: dict[str, str]                                # path -> 文本（二进制以 base64 前缀）
    @property
    def kinds(self) -> set[str]
    def component(self, kind: str) -> list[dict]
def classify(path: str) -> str
def parse_manifest(text: str, files: dict[str, str], *, limits: Limits) -> Manifest
def check_shape(files: dict[str, str], *, limits: Limits, seed: Package) -> list[str]   # 形状、上限、vendor 保护文件、HOST_KINDS 未改
def build(objects: Objects, files: dict[str, str], *, limits: Limits, seed: Package) -> Package   # 全部检查通过才入库
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
def literal_index(grading_rows: Iterable[dict]) -> LiteralIndex     # 只从 train 的评分行建
def forbidden(index: LiteralIndex, *, exempt: set[str]) -> set[str]
def changed_lines(text: str, base: str | None) -> set[int]
def findings(files: dict[str, str], seed: Package, forbidden: set[str], *, allowed: set[str] = frozenset()) -> list[dict]
    # 只扫相对种子改动的行；返回 [{path, line, literal}]

# seed.py
def build_seed(name: str, *, core: dict, objects: Objects) -> Package
def seed_dir(name: str) -> Path
def protected_files(name: str) -> dict[str, bytes]      # vendor/<name>/{LICENSE, PROVENANCE.json}

# vendor.py
@dataclass(frozen=True)
class Provenance: name: str; version: str; url: str; tag: str; sha256: str; license: str; files: int
def read_provenance(files: dict[str, str], name: str) -> Provenance
def apply_patches(vendor_files: dict[str, str], patches_dir: Path) -> tuple[dict[str, str], list[str]]   # 失败的补丁名
def extract_patch(seed: Package, installed: Package, name: str) -> str | None   # 装入后把 R 对 vendor 的改动抽成补丁文本
def upgrade(seed_dir: Path, new_source: Path, provenance: Provenance) -> list[str]   # 换上游、重放补丁；返回失败的补丁
```

## 7 种子 `stirrup` 的构建

1. `vendor/stirrup/**` 来自 Stirrup 0.2.0 的 sdist（`PROVENANCE.json`：version、tag、sha256、license=MIT、文件数）；构建时校验 sha256。
2. `patches/NNN-<slug>.patch` 依次重放（当前为空：七处修正都在适配层里）。
3. 适配层、守卫、`plan_changes`、prompt、tests 从 `seeds/stirrup/` 拷入。
4. `manifest.json` 的 `model.base` 填 K 的 identity，`config` 取默认，`memory.enabled` 取 K。
5. `check_shape` 通过，`build` 入库；种子 release 由 activity 写。

适配层职责（`agent/stirrup_agent.py`，沿用 v6-agent 的实现并扩站点）：`LLMClient` → `ctx.model`；`Tool` → 基础工具经 `ctx.tool`、包工具经入口、`read_skill`、`recall`；六个站点的分发与 `mark`；finish 的修正；子 agent 深度受 `config.subagent_depth`；摘要用 role `aux`。

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
| `agent/default_agent.py` | 删 |
| `tests/test_harness_package.py`、`test_write_guard.py`、`test_worker_resources.py`（v6） | 迁到 `tests/harness/` |

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
