# runtime：网关、broker、worker 与运行器、egress、运行壳、App

> 阶段 1 设计文档。包 `reharness/runtime/`。对应 [DESIGN](../DESIGN.md) §3.4、§3.5、§8。来源：`agent/gateway.py`、`agent/worker.py`、`agent/worker_runner.py`、`agent/runner.py`、`app.py`，以及 `v6-gateway`（egress、usage view）、`v6-agent`（worker 资源从 K 读）、`v6` 合并分支在 WIP 之前的 `0f33550`（egress 的请求配额与 `poll`、`LazyPool`、`EgressMeter`）。2026-10-09 按阶段 1 的实现对齐。

## 1 职责与不做

**做：** 跑一次尝试：起 worker（资源与网络按 K）、装载包、调包的 `agent.run`，回答它的 `model_call` / `base_call` / `skill_read` / `memory_recall` / `mark`，每次都计量、截顶、记录；硬限额到顶就结束；导出的 App 用同一套壳跑。

**不做：** 不评分、不读划分（evaluate 给它 task 与 split 标签）；不写 memory；不知道候选与 gate。

## 2 文件

| 文件 | 内容 |
|---|---|
| `runtime/gateway.py` | `Gateway`：硬限额、effort 截顶、role 校验、用量与按价目的成本、记录；`normalize_usage`（各 provider 用量词表归一，计量与网关共用） |
| `runtime/broker.py` | `AttemptBroker`：环境工具调用的权限、只读、硬限额、记录；`SkillReader`；`MemoryRecall`；`Marks`；`HostContext` |
| `runtime/records.py` | transcript 行的写入与 kind 常量（CONTRACTS §6） |
| `runtime/worker.py` | `Worker`、`ProcessWorker`、`DockerWorker`、`WorkerSession`、`WorkerResources`、`docker_worker`、`usable_worker`；V1 的草稿检查 `check_package`（probe、smoke、包测试） |
| `runtime/chat.py` | `ToolSpec`、`ToolCall`、`ChatTurn`、`ScriptedChat`（原 `chat.py`） |
| `runtime/worker_runner.py` | worker 里唯一运行的程序（标准库），协议见 §4 |
| `runtime/egress.py` | `EgressPool`、`EgressSession`、`LazyPool`（v6-gateway 原样） |
| `runtime/egress_addon.py` | mitmproxy addon（v6 原样，构建进 egress 镜像） |
| `runtime/runner.py` | `PackageAgent`：一次尝试的运行壳 |
| `runtime/app.py` | 导出的 App：从导出目录装配运行壳 |

## 3 接口

```python
# gateway.py（EFFORTS 在 harness/contract.py）
class Gateway:
    def __init__(self, provider, *, roles, max_effort, limits: dict, record, surface=None, call_prefix="", attempt_id=None, pricing=None)
        # provider 在活动里是 evaluate.budget.MeteredProvider：钱的预留与结算在它那里，每次调用一对；网关管五项硬限额、
        # effort 截顶、role、记录，并按 pricing 算每次调用的成本（AgentResult.cost）
    def model(self, messages, tools=(), role="act", effort=None) -> dict   # {text, tool_calls, stop, usage, limits}；未声明的 role、
        # 坏形状、未知 effort 答错误（agent 可捕获）并记 protocol 行，不调 provider
    def limits(self) -> dict                         # 剩余 act_calls / model_calls / tool_calls / seconds / tokens
    def last_usage(self) -> dict                     # 本次尝试归一后的用量合计；每次调用的用量随答复给 ctx.view["last_usage"]
def cap_effort(requested, max_effort) -> str
def malformed(messages, tools) -> str | None         # 形状检查，坏的消息不发给 provider

# broker.py
def denied(command) -> str | None                     # 拒绝表（原 agent/deny.py；train 也用它）
class Broker:        # 尝试之外的基础工具：fixture 或 train 世界，按 surface 判只读；草稿检查与 toolcheck 的 base_call 用它
class AttemptBroker:
    def __init__(self, surface, *, record, gateway, view=None, call_prefix="", max_output_chars=20000)
    def call(self, name, args, *, read_only=False) -> tuple[str, bool]   # 拒绝表、只读、surface 的权限与 tool_calls 限额、记 tool + broker_event
    def tool_calls(self) -> int
class SkillReader:   def __init__(self, package: Package, *, record);  index;  def read(self, name) -> str | None
class MemoryRecall:  def __init__(self, reader: Callable[[str, int], list[Entry]] | None, *, record, max_k: int);  def recall(self, query, k=None) -> list[Entry]
class Marks:         def __init__(self, record);  def mark(self, kind, data) -> None   # 包的自述，source=package；kind "hook" 写 hook 行
class HostContext:   view, skills, memory, limits, model, tool, mark  # 进程内的 ctx，供 toolcheck 与测试用

# worker.py
@dataclass(frozen=True)
class WorkerResources:                 # K 的 worker 表；未知键拒绝
    image: str; image_id: str | None; cpus: str = "1"; memory: str = "512m"; memory_swap: str | None = None
    pids: int = 128; tmpfs_mb: int = 64; network: dict | str = "none"; env: dict[str, str] = ...
    request_seconds: float = 30.0; max_output_chars: int = 20000
    @classmethod
    def from_core(cls, core: dict) -> "WorkerResources"
def docker_worker(resources, *, egress_pool=None) -> DockerWorker | None   # 镜像（K 钉的 id）不在就是 None
class Worker(Protocol):
    qualified: bool
    def start(self, egress: EgressSession | None = None) -> WorkerSession
    def egress_session(self, attempt_id, *, read_only=False, max_requests=None) -> EgressSession | None
    def runner_problem(self) -> str | None          # runner sha256 不符即 StaleWorker
    def image_id(self) -> str | None
    def describe(self) -> dict
class ProcessWorker(Worker): ...                   # 协议测试；qualified = False
class DockerWorker(Worker):
    def __init__(self, resources: WorkerResources | None = None, *, egress_pool: EgressPool | None = None)
class WorkerSession:
    def load(self, files: dict[str, str]) -> dict                     # 返回 {runner_sha256, ops}
    def serve(self, message: dict, *, handlers: dict, deadline) -> object   # 一个 op 连同它的回调；run 的 handlers: model_call/base_call/skill_read/memory_recall/mark
    def request(self, message: dict, broker=None) -> object   # 有界 op：probe/call_tool/hook/test/smoke/check/work，base_call 经 broker
    def work(self, action: str, **fields) -> dict                     # R 的工作树（IMPROVER §6）
    def close(self, force=False) -> None
class EgressPool / EgressSession / LazyPool        # v6-gateway 的签名不变（session(attempt_id, read_only, max_requests)、poll、close）

# runner.py
@dataclass
class AgentResult: text; termination; limit; usage; cost; hooks: list[dict]; marks: dict[str, int]; turns; stop; session; error; tools_called; skills_read; tool_calls
def compose_system(base: str, package: Package, opening: str = "") -> str   # 任务自带的 system + prompt/system.md + prompt/finish.md + 技能索引 + memory 开场块
class PackageAgent:
    def __init__(self, *, provider, package: Package, core: dict, surface, worker: Worker, transcript: Transcript,
                 system: str = "", attempt_id: str, task_meta: dict | None = None, memory=None, pricing=None)
    # memory：memory.retrieval.TaskMemory（opening、event、recall）或 None；event 记成紧跟 task 行的 library_retrieval{site:"start"}
    def run(self, task: str) -> AgentResult        # 钱的错误（BudgetError）、provider 的账户错误、StaleWorker 写完 end 行后抛出

# app.py
@dataclass(frozen=True)
class AppManifest(Record): package_ref: str; core_subset: dict; validation_ref: str; image_id: str | None; release_ref: str | None   # kind "app"
class App:
    def __init__(self, directory)
    def agent(self, *, provider, surface, worker, attempt_id, system="", memory=None, pricing=None, task_meta=None) -> PackageAgent
    def describe(self) -> dict
def export(objects, release, core: dict, destination: Path, *, validation: dict) -> AppManifest   # 导出 package 闭包、validation、app 三类对象与 app.json
```

## 4 worker 协议（`worker_runner.py`）

一行一个 JSON。宿主 → worker 的 op：

| op | 字段 | 答 |
|---|---|---|
| `load` | `files` | `{runner_sha256, ops}` |
| `run` | `entry, task, roles, view, limits, skills` | 过程中 worker 发 `model_call` / `base_call` / `skill_read` / `memory_recall` / `mark`；最后 `{op:"result", value:<final text>}` 或 `{op:"error", kind, message}` |
| `smoke` | `entry, roles, task, deadline, view` | 同 run，但模型由 worker 本地脚本化（每个工具各调一次再结束）；小限额 |
| `hook` | `entry, event, view` | `{op:"result", value}`（回放与草稿检查用） |
| `call_tool` | `entry, args, view` | 同上 |
| `probe` | `entries` | 导入每个入口，报错即 `CandidateDefect` |
| `test` | — | 跑 `tests/*.py`，返回 `{passed, failed, output}` |
| `check` | `script, tools: [{name, entrypoint, writes}], view` | 跑 toolcheck 脚本（HARNESS §5.5），世界经宿主的可写 broker；答 `{ok, called}` |
| `work` | `action, ...` | R 的工作树：init / list / read / write / edit / delete / export |

worker → 宿主的回调：`model_call{id, role, effort, messages, tools}` → `model_result{id, text, tool_calls, stop}` 或 `{id, is_error, error}`；`base_call{id, name, args, read_only}` → `base_result{id, output, is_error}`；`skill_read{id, name}` → `skill_result{id, text}`；`memory_recall{id, query, k}` → `memory_result{id, entries}`；`mark{id, kind, data}` → `mark_result{id}`。每个答复可带 `limits` 与 `view`（变化的键）。id 按 kind 单调；错位即协议错误。

`load` 的答复里的 `runner_sha256` 必须等于宿主的副本；不等即 `StaleWorker`（启动检查与每次 load 都查）。

错误的 kind：`protocol`（宿主答错了，包捕获不了）、`package`（包代码的异常：run 中是 `AgentRunError`，基础设施；有界 op 中是 `PackageCallError`，候选缺陷）、`defect`（异常带 `reharness_defect`：run 中是 `RunDefect`，尝试记 `harness_error`）。worker_runner 嵌入的 `CONTRACT` 等于 `harness.contract.as_json()`，测试比对。

## 5 一次尝试的流程（`PackageAgent.run`）

1. 记 `task` 行。合成 `system`（prompt/system.md + skills 索引 + memory 的开场检索块，由 evaluate 传入）。
2. 若 K 的 `worker.network` 是 proxy：开 egress session（配额 = 剩余 tool_calls）；起 worker；`load(run_files(package))`。
3. `run(entry, task, roles, view, limits, skills, handlers)`；handlers 把 `model_call` 交给 gateway，`base_call` 交给 broker，`skill_read` 交给 SkillReader，`memory_recall` 交给 MemoryRecall，`mark` 交给 Marks（`kind == "hook"` 的转写成 `hook` 行）。每个答复附剩余 limits 与 `last_usage`。
4. 结束：`result` → termination `finished`；handler 抛 `LimitHit` → `budget`（维度写 `limit`）；`RunDefect`（包自报的缺陷）或包工具与基础工具重名 → `harness_error`；worker 崩溃或超时 → `infrastructure`（到 `max_seconds` 则算 seconds 限额）；`AgentRunError` → `infrastructure`（草稿 smoke 已挡确定性崩溃）。
5. egress 的行并入 transcript；记 `end` 行；关 worker。

## 6 不变量

- 每次模型调用先预留再结算；effort 超上限截顶，记请求值与发出值；manifest 未声明的 role 拒绝。
- 每次环境工具调用过拒绝表与只读规则；`writes=false` 的包工具发出的 base_call 一律 read_only。
- worker：无宿主挂载、只读根、丢弃 capability、资源与网络按 K；proxy 模式只在 internal 网络上起，否则 `EgressUnavailable`。
- transcript 只追加，包写不到；`mark` 标 `source: package`，不是证据。
- `WorkerResources` 的每个字段都来自 K；代码里没有写死的 256m / 64。

## 7 与现有代码的对应

（2026-10-09 迁移时的代码对应表与开发顺序已清理；需要时见 tag `archive-2026-10-09` 上的版本。）

## 8 测试

- 协议：每个 op 一例正常、一例错位 id、一例超长行；`load` 的 sha256 不符即 StaleWorker。
- 网关：effort 截顶与记录；未声明 role 拒绝；预留/结算一一对应（`test_paid_run_safety` 的计量测试迁到这里）；`malformed` 拒绝坏消息。
- broker：拒绝表；`read_only` 对写方法的拒绝；tool_calls 限额；记录字段完整。
- worker：资源全部来自 K（变异：改 K 的 memory 字段，`docker inspect` 读到的值变）；proxy 只在 internal 网络起；egress 配额与 tool_calls 联动（v6 `test_worker_resources`、`test_egress` 迁入）。
- 运行壳：`test_attempt_behaviour` 在新种子上重录夹具；四种结束方式各一例；hook 行由 mark 转写。
- App：导出目录只含白名单对象；镜像 id 不符拒绝；无 provider 拒绝。

## 9 开发顺序

（2026-10-09 迁移时的代码对应表与开发顺序已清理；需要时见 tag `archive-2026-10-09` 上的版本。）

