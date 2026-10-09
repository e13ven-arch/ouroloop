# memory：sidecar、规则、写入步、检索与 recall、快照、升级

> 阶段 2 的设计文档（接口随阶段 1 一起定；2026-10-09 开发前按 v6 的证据与阶段 1 的代码定稿）。包 `ouroloop/memory/`。对应 [DESIGN](../DESIGN.md) §5。来源：`v6-memory` 分支（`memory/{client,vel,track,retrieval,importing,sidecar/*}`）、`v6-gateway` 的 `bridge.py` 与 `trace.py`。

## 1 职责与不做

**做：** 从 train 尝试的轨迹提炼经验条目（ReMe 算子），按平台规则入库（出处、只用 train、字面量、可达性、去重）；按题检索给 agent（开场块与 `recall` 工具）；引用记功、退役、容量；整库快照作为 W 的候选组件；条目逐级升级为技能或原因簇；跨业务线导入。

**不做：** 不在尝试中重写或删除条目；不读 dev/test；不替 agent 决定用哪条；不保存对话记忆。

## 2 文件

| 文件 | 内容 |
|---|---|
| `memory/__init__.py` | 钉死的版本与哈希（ReMe 0.3.1.10 的轮子、bge-small 的修订与各文件 sha256）、sidecar 文件的路径、`snapshot_digest`、`default_venv`、`model_problems` |
| `memory/__main__.py` | `python -m ouroloop.memory setup|check`：按锁文件建 sidecar 的 venv、取模型、核对（v6 原样） |
| `memory/settings.py` | `Settings`（K 的 `memory` 节，§3）与 `retriever`、`backend` 两张钉表 |
| `memory/client.py` | `MemoryClient`：sidecar 的生命周期与 HTTP 调用（v6 原样；错误类是 `contracts.MemorySidecarError`） |
| `memory/sidecar/` | `serve.py`（ReMe HttpService + 我们的 op）、`ops.py`、`embed.py`（bge-small ONNX）、`flows.yaml`、`requirements-macos-arm64.lock`。只在 sidecar 的 venv 里跑，不 import 本框架（v6 原样） |
| `memory/bridge.py` | OpenAI 兼容桥：sidecar 里 ReMe 的模型调用经它到计量过的研究 provider（v6 `bridge.py`；provider 由 activity 建好传入，按鸭子类型检查） |
| `memory/rules.py` | 平台规则（v6 `vel.py`）：出处、train-only、字面量一次占位、引用记功、退役、容量、快照行、读数、`promotable` |
| `memory/track.py` | `StreamStep`：写入步，可断点续跑（v6 原样；轨迹改由 `runtime/trace.py` 给） |
| `memory/retrieval.py` | `LibraryReader`：开场块与 `recall`，留一；`TaskMemory` 是一次尝试拿到的那一份 |
| `memory/service.py` | `MemoryService`：一个活动进程里的 sidecar、桥与研究作用域的生命周期；给 stream 与读者 |
| `memory/snapshot.py` | 快照写进包的 `memory/snapshot.jsonl`，作为候选交给 improver 的阶梯 |
| `memory/promote.py` | 升级：条目 → 技能候选；条目 → 原因簇输入；`mark_superseded` |
| `memory/importing.py` | 跨业务线导入（v6 原样） |
| `runtime/trace.py` | `TraceStore`：transcript 的稳定引用、按 actor 所见重建轨迹、FTS 索引，只读 train（v6 `trace.py`，行格式改成阶段 1 的） |

## 3 Settings（K 的 `memory` 节）

数值取 v6 的证据（E0c、E0d 两次校准、S1 的展示计数），不是猜的：bge-small 下无关句子的相似度 0.53～0.56、相关的 0.67，近重复 0.907。

```python
@dataclass(frozen=True)
class Settings:
    enabled: bool = True                  # 活动跑 memory 流；包是否用 memory 看它自己的 manifest
    shown_k: int = 2                      # 开场块的条数
    recall_k: int = 3                     # recall 一次最多给几条
    fetch_factor: int = 5                 # 先取 shown_k × 它，留一与 min_score 之后再截
    min_score: float = 0.62               # E0d
    sibling_sim: float = 0.95             # 与来源题这么像的 train 题也算来源（留一用）
    batch_dedup_threshold: float = 0.85   # ReMe 批内去重（E0c）
    store_dedup_threshold: float = 0.9    # 与库内比：when 对 when
    store_dedup_content_threshold: float = 0.85   # 再比 content，够近才算同一条
    validation_threshold: float = 0.6     # ReMe 的 MemoryValidation
    success_threshold: float = 1.0        # 轨迹分 ≥ 它算成功（ReMe 的 success 提炼）
    origins: tuple = ("success", "failure", "comparative")
    max_active: int = 500
    probation_shown: int = 3              # 观察期
    retire_harm_tasks: int = 2; retire_harm_min_shown: int = 5
    retire_min_shown: int = 3; retire_mean_credit: float = -0.2
    min_other_tasks: int = 2              # 可达性：when 要检索到来源外这么多道 train 题
    when_chars: int = 400; content_chars: int = 1600
    traj_tool_chars: int = 4000; traj_total_chars: int = 60000
    stream_batch: int = 4
    cite: bool = True                     # 开场块要 agent 在 finish 里写 library-used:
    retry: bool = True                    # 首遇失败重试一次，带失败断言的类别
    abort_d: float = 0.0
    heldout_domains: tuple = ("operations",)
    imports: tuple = ()                   # [{release_ref, snapshot_digest}]
    llm_timeout_s: float = 600.0
    promote_fixes: int = 2; promote_sources: int = 2
    retriever: dict                        # 模型、修订、各文件 sha256、维度、池化、query_prefix（钉死）
    backend: dict                          # reme 版本、轮子、锁文件与 flows 的 sha256（钉死）
```

与早先草稿的差别：`rerank`、`title_chars`、`collection_prefix` 删掉（没有证据、ReMe 的任务记忆没有标题、集合名是固定规则）；`reme_version`、`lock_sha256`、`embed_model` 收进 `backend`、`retriever`；研究调用的并发不进 K，用活动的并发（机器设置，同 `[bench] concurrency`）。

合并规则（`activity/presets.py` 的 `RULES`）：`enabled`、`retry`、`imports` free；`shown_k`、`recall_k`、`max_active`、各字符上限、`llm_timeout_s`、`sibling_sim` cap；`min_score`、`validation_threshold`、`min_other_tasks`、`abort_d`、`promote_*` floor；`origins` subset；`heldout_domains` superset（新规则：只能多留出）；其余 fixed。

## 4 接口

```python
# settings.py
class Settings: ...;  def from_core(k: dict) -> Settings;  def to_json() -> dict

# client.py（v6 签名）
class MemoryClient:
    def start(self, *, venv, chroma_path, settings, llm=None, workdir=None, lock=LOCK, timeout=180, scope=None) -> "MemoryClient"
    def stop(self); kill(); restart(); attach(connection); connection()
    def summarize(self, trajectories: list[dict]) -> dict      # {memory_list, validated, replies, unparsed}
    def retrieve(self, query, collection, *, where=None, top_k=5, query_mode=True, expected_count=None) -> list[dict]
    def add(self, memories, collection) -> int;  get(ids, collection);  retire(ids, collection)
    def dump(self, collection) -> str;  load(self, jsonl, collection, *, clear=True) -> int
    def ensure_snapshot(self, snapshot_text, collection, *, rows=None) -> int;  forget_snapshot(collection)
    def embed(self, texts, *, query=False) -> list[list[float]]

# bridge.py（v6 签名）
class Bridge:  start(); stop(); open_scope(kind="research", *, scope_id, step, provider, record, models, effort_cap, max_concurrency) -> Scope
class Scope:   base_url; token; set_step(step); calls(step) -> list[dict]; close()
# provider：有 chat()、pricing、operation、max_retries == 0 的计量 provider（activity 的 research_provider，重试全关）

# rules.py
def require_train(split, source_line, heldout) -> None                 # 否则 contracts.TrainOnlyError
def stamp(memory: dict, *, line, source_line, split, task_hash, activity, release_ref, sessions, trace_refs, origin, born_at, source_score, heldout_domains, imported_from="") -> dict
def literal_pass(fields: dict, forbidden, allowed) -> tuple[dict | None, list]   # 一次占位（<value>）后再查
def parse_citation(finish_reason, last_text) -> list[str] | None       # `library-used: <ids>`
def credit(entries, *, task_hash, shown, cited, r, b, stable_pass) -> dict
def retire_reason(entry, settings) -> str | None
def evictable(entries, settings) -> list[str]
def snapshot_rows(store_rows, entries) -> list[dict]
def readout(readings, order, entries) -> dict                          # D、B、各来源的 D、两半与学习曲线、记功秩相关
def snapshot_abort(readout, settings, *, empty) -> str | None
def promotable(entries, settings, mechanizable) -> list[tuple[dict, str]]   # (entry, "skill" | "cause")

# track.py
class StreamStep:
    def __init__(self, client, settings, *, ledger_path, live_path, collection, business_line, activity, heldout_domains,
                 train_texts: dict, forbidden_literals, allowed_literals=(), trajectory_of, bridge_calls_of, set_step)
    def resume(self) -> dict
    def process_batch(self, items: list[tuple[task, first, retry, baseline]]) -> list[dict]   # §5
    def checkpoint(self) -> None;  finish(self) -> dict;  snapshot_text(self) -> str;  live_view(self) -> dict
    def import_snapshot(self, snapshot_text, *, source_release_ref, source_line) -> dict
# task = {task_hash, text, b, stable_pass}；尝试 = {session, reward, passed, release_ref, shown, final_reason, final_text, trace_refs}

# retrieval.py
class LibraryReader:
    def __init__(self, client, settings, *, business_line, snapshot_text: str | None = None, live: dict | None = None)
    def for_task(self, task_text, task_hash, *, note: str = "") -> TaskMemory
@dataclass
class TaskMemory:  opening: str;  event: dict | None;  def recall(self, query, k=None) -> list[dict]   # [{id(8 位), when, content, score}]

# service.py
class MemoryService:
    def __init__(self, root, line, settings, *, venv=None, scope_id)
    def start(self, *, research=None) -> "MemoryService"    # research：计量 provider；有它才开桥与研究作用域
    def stream(self, *, train_texts, forbidden, allowed, trajectory_of, activity) -> StreamStep
    def reader(self, *, snapshot_text=None, live=None) -> LibraryReader
    def stop(self)

# snapshot.py
def snapshot_candidate(parent: Package, snapshot_text: str) -> dict[str, str]   # 只改 memory/snapshot.jsonl 与 manifest 的 memory 节
def stream_files(parent: Package) -> dict[str, str]                              # memory 开、没有快照（流期间读 live）
def library_of(package: Package) -> list[dict]

# promote.py
def skill_candidate(entry: dict, parent: Package) -> dict[str, str]    # skills/<slug>.md，带来源（条目 id、trace ref）
def cause_input(entry: dict) -> dict                                   # IMPROVER §4 的 Cause 条目（memory_entries）
def mark_superseded(entries, entry_id, by: str) -> None

# importing.py（v6 签名）
def import_entries(snapshot_text, *, source_release_ref, source_line, target_line, client, collection, heldout_domains, fix=None) -> dict
```

一次尝试怎么拿到 memory（RUNTIME §3、EVALUATE §3）：`make_agent_factory(memory=...)`，`memory(task, task_hash) -> TaskMemory | None`；`PackageAgent` 把 `TaskMemory.opening` 接在系统提示后、把 `event` 记成 `library_retrieval{site:"start"}`（紧跟 task 行），`recall` 交给 `MemoryRecall`。包的 manifest 里 `memory.enabled` 为真时，种子才给 agent `recall` 工具。没有 memory 的包（W0）什么都不起，热路径不变。

条目 id：agent 看到的都是 8 位（开场块 `[lib <id8>]`，recall 返回 `id` 也是 8 位）；`start` 行记完整 id；记功时按 8 位前缀回到完整 id。

## 5 写入步（`StreamStep.process_batch`）

1. 由 trace 重建每题的轨迹（基线、首遇、重试），按 Settings 裁剪，计数裁剪；
2. 标 bridge 的 step，让 sidecar 的 ReMe 并发总结一批题；
3. 核对每个总结的模型调用都在桥的记录里（都是该 step 下 200 的调用；绕过桥的不算）；非 200 的停步，通过的留在台账；
4. 每条记忆：长度、字面量（含 ReMe 元数据，一次占位符豁免）、可达性；
5. 与库内去重（when 与 content 两个阈值）、按 id 去重；重复只加来源；
6. 盖出处，按冻结顺序串行入库；
7. 首遇尝试按引用记功，再退役；
8. 检查点：live 文件先于台账。

## 6 读取

- 开场：`for_task(task_text, task_hash)` 检索 `shown_k` 条附在系统提示里；记 `library_retrieval{site:"start", query, shown, scores, …}`。
- 尝试中：`recall(query)` 经 runtime 的 `MemoryRecall` → `TaskMemory.recall`；记 `library_retrieval{site:"recall"}`；同一留一规则。
- 留一总是开：来源含本题（含 `sibling_sim` 以上的近亲）的条目不给。dev/test 从不是来源，所以在 dev/test 上什么也不排除；快照是冻结的，live 只在 train 流里更新。
- 任务身份：`evaluate.attempt.task_hash(task)`（task_id 与上游 contract 的摘要），即 transcript `task` 行的 `task_hash`。近亲按任务文本（`task.trigger`）的 bge 相似度。

## 7 流、快照与阶梯

`library` 阶段（ACTIVITY §4）：
- 流的包：W_base 的文件加 manifest `memory: {"enabled": true}`（没有快照文件，读 live）。尝试记在 W_base 的 release 下，run 的 `package_ref` 是流的包，候选名 `library`。
- 顺序：train 题按 `Random(f"library:{gate.order_seed}:{domain}")` 打乱，冻结在台账里。
- b(t)：W_base 在这道题上 train 读数（基线）的有效尝试的平均成功率；`stable_pass` = b == 1；没有读数的题不进 D。
- 每批 `stream_batch` 道题：首遇（arm `first`）并发跑；失败且 `retry` 开着的再跑一次（arm `retry`，开场块后接失败断言的类型与参数，train 的评分行）；首遇按规则补跑后仍无效的题记作 lost（不总结、不记功）；然后 `process_batch`，基线那次尝试的轨迹一起总结。
- 结束：`finish()` → 读数、快照、`snapshot_abort`。D ≤ `abort_d` 或库空就不付 gate；否则 `snapshot_candidate` 产出只改快照与 manifest memory 节的候选包，入对象库、记事件 `library_snapshot`，交 improver 的阶梯从 V3 起（阶段 3；V0 的形状与字面量在写入时已过）。
- 续跑：已记录的尝试按 attempt id 复用、不重付；台账是真相，`resume()` 对齐 store；挂起的 batch 不重新总结。
- 装入后 dev/test 只见快照。

工作区：`<root>/memory/<domain>/` 下 `stream.json`（台账）、`live.jsonl`（live 库的规范 JSONL）、`chroma/`（store，sidecar 独占锁）、`bridge_calls.jsonl`（桥的调用记录，train 数据）。sidecar 的工作目录在仓库外（`~/.cache/ouroloop/memory-run/<scope>`）。venv：`OUROLOOP_MEMORY_VENV`，否则 `default_venv()`（机器设置，不进 K；K 钉的是锁文件与模型的哈希）。

钱：sidecar 的每次模型调用经桥，记在 `<scope>:research:memory` 账户；流的尝试记在 `<scope>:library` 账户。

## 8 升级（DESIGN §5.6）

每轮改进器开始前 `promotable(entries, settings, mechanizable)`：
- `fixes ≥ promote_fixes`、来源题 ≥ `promote_sources`、`mechanizable(entry)` 为假、正文是多步程序（≥ 2 个编号或列表行，或 ≥ 2 句）→ `skill_candidate`，作为固定代码候选进本轮阶梯（V0 字面量、V2c 可达、V3 起付费）；
- 计数满足且 `mechanizable(entry)` 为真（诊断词表里的 cause 能对上；阶段 2 里没有词表，恒为假）→ `cause_input`，进本轮的簇；
- 升级候选装入后 `mark_superseded`：条目 `status = "superseded"`，live store 退役它，不再展示。

阶段 2 交付这三个函数与测试；接进轮次是阶段 3。

## 9 不变量

- 宿主只用 httpx；ReMe、chroma、onnxruntime 只在 sidecar 的 venv 里；`tests/test_hot_path_boundary` 守住一次尝试不加载 `memory.sidecar`，memory 只 import contracts、harness、runtime（providers 只在函数里）。
- 每次 sidecar 的模型调用都经桥、都计量；桥只服务 research 作用域；桥与计量都不重试。
- 任何非 train 的来源入库即 `TrainOnlyError`；留出的业务线不跑流、不被导入。
- 条目的 `provenance` 带 trace ref，可回到原始轨迹。
- 库内无 6 架上限；排序确定（名字作次键）。

## 10 与现有代码的对应

| 现有 | 处理 |
|---|---|
| `v6-memory` 的 `memory/*` | 搬入；`vel.py` 改名 `rules.py`，`V6Settings` 改成 `settings.Settings`（§3 的字段名），加 `literal_pass`、`promotable`；`research.library` 的 `literal_pass`、`rank_correlation`、`stream_readout` 的学习曲线并进 `rules.py`；`retrieval_block_v6` 改成 `LibraryReader` |
| `v6-gateway` 的 `bridge.py` | → `memory/bridge.py`（唯一使用者在 memory）；`research_provider` 移到 activity |
| `v6-gateway` 的 `trace.py` | → `runtime/trace.py`；行格式按阶段 1（`reply`、`mark` 行、`hook` 行、broker 的 call id）；任务的划分按冻结划分里的 task_id |
| `research/library.py`（VEL） | 已在阶段 1 删除（tag `archive-2026-10-09` 可读） |
| `tests/test_memory_*.py`、`test_trace.py`、`test_bridge*.py`、`fake_openai_server.py`（v6） | → `tests/memory/`、`tests/runtime/test_trace.py` |

trace 的归属：`TraceStore` 放 `runtime/trace.py`（它只读 transcript，属于证据的读取面），improver 与 memory 都 import 它，不违反 CONTRACTS §1 的方向。IMPROVER §4 据此写。

## 11 测试

两层：
- 默认套件：规则、写入步、读取、快照、升级、library 阶段在一个进程内的假 client（`tests/memory/fake_memory.py`：确定的词袋向量、脚本化的总结、桥的调用记录）上跑，不要 venv；
- memory 套件：真 sidecar（ReMe、chroma、bge-small）加脚本化的 OpenAI 服务（`fake_openai_server.py`），要 `OUROLOOP_MEMORY_VENV`；没有就跳过，`OUROLOOP_TEST_REQUIRE_MEMORY=1` 时不跳而失败。合并与付费前必跑。

清单：
- sidecar：锁文件哈希不符拒绝启动；健康检查；`summarize` 走桥且每次调用有记录（变异：绕过桥的调用被 `_check_replies` 抓到）。
- 规则：train-only；引用记功与平分；观察期；退役两条；容量淘汰确定；快照行合并出处；一次占位。
- 写入步：七步各一例；断点续跑（中途杀掉再 resume 不重复入库）；字面量占位符豁免。
- 读取：留一（来源含本题不给）；`recall` 经 runtime 记录；开场行紧跟 task 行。
- 快照：只改快照与 manifest 的 memory 节；readout 的 abort。
- 升级：两种条件各一例；superseded 后不再展示。
- library 阶段：假上游上端到端（假 client 一例，真 sidecar 一例）；续跑不重付；首遇无效的题记 lost。
- 变异检查在隔离副本（v6 做过 15 项，照迁）。

## 12 开发顺序

搬入（sidecar、client、bridge、importing、`__main__`）→ `settings.py`、`rules.py` → `runtime/trace.py` → `retrieval.py` 与 runtime/evaluate 的接线（`TaskMemory`、开场行）→ `track.py` → `service.py` → `snapshot.py`、`promote.py` → activity 的 `library` 阶段、preset 与启动检查 → 测试。
