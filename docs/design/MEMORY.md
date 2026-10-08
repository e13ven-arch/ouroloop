# memory：sidecar、规则、写入步、检索与 recall、快照、升级

> 阶段 2 的设计文档（接口随阶段 1 一起定，因为 harness、runtime、activity 都要接它）。包 `ouroloop/memory/`。对应 [DESIGN](../DESIGN.md) §5。来源：`v6-memory` 分支（`memory/{client,vel,track,retrieval,importing,service,sidecar/*}`）、`v6-gateway` 的 `bridge.py`。

## 1 职责与不做

**做：** 从 train 尝试的轨迹提炼经验条目（ReMe 算子），按平台规则入库（出处、只用 train、字面量、可达性、去重）；按题检索给 agent（开场块与 `recall` 工具）；引用记功、退役、容量；整库快照作为 W 的候选组件；条目逐级升级为技能或原因簇；跨业务线导入。

**不做：** 不在尝试中重写或删除条目；不读 dev/test；不替 agent 决定用哪条；不保存对话记忆。

## 2 文件

| 文件 | 内容 |
|---|---|
| `memory/client.py` | `MemoryClient`：sidecar 的生命周期与 HTTP 调用（v6 原样） |
| `memory/sidecar/` | `serve.py`（ReMe HttpService + 我们的 op）、`ops.py`、`embed.py`（bge-small ONNX）、`flows.yaml`、锁文件 | 
| `memory/bridge.py` | OpenAI 兼容桥：sidecar 的 ReMe 经它调计量过的研究 provider（v6 `bridge.py` 原样） |
| `memory/settings.py` | `Settings`（K 的 `memory` 节） |
| `memory/rules.py` | 平台规则（v6 `vel.py`）：出处、train-only、引用记功、退役、容量、确定性、快照行、读数 |
| `memory/track.py` | `StreamStep`：写入步，可断点续跑（v6 原样，改 §5.6 的升级钩子） |
| `memory/retrieval.py` | `LibraryReader`：开场块与 `recall`；留一 |
| `memory/snapshot.py` | 快照写进包的 `memory/snapshot.jsonl`，作为候选交给 improver 的阶梯 |
| `memory/promote.py` | 升级：条目 → 技能候选；条目 → 原因簇输入 |
| `memory/importing.py` | 跨业务线导入（v6 原样） |

## 3 Settings（K 的 `memory` 节）

```python
@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    reme_version: str = "0.3.1.10"; lock_sha256: str = "..."
    embed_model: str = "bge-small-en-v1.5"; collection_prefix: str = "ol"
    shown_k: int = 2; recall_k: int = 3; min_score: float = 0.2; rerank: bool = False
    traj_tool_chars: int = 2000; traj_total_chars: int = 60000
    when_chars: int = 300; content_chars: int = 1200; title_chars: int = 100
    min_other_tasks: int = 2; sibling_sim: float = 0.6
    store_dedup_threshold: float = 0.8; store_dedup_content_threshold: float = 0.85
    probation_shown: int = 3; retire_harm_tasks: int = 2; retire_utility: float = -0.2
    max_active: int = 500; retry: int = 1; abort_d: float = 0.0
    promote_fixes: int = 2; promote_sources: int = 2
    stream_batch: int = 4
```

## 4 接口

```python
# client.py（v6 签名）
class MemoryClient:
    def start(self, *, venv, chroma_path, settings, llm: dict, lock, timeout=180) -> "MemoryClient"   # llm = 桥的 base_url 与 token
    def stop(self); attach(connection); connection()
    def summarize(self, trajectories: list[dict]) -> dict            # ReMe summary 流水线
    def retrieve(self, query, collection, *, where=None, top_k=5, rerank=False) -> list[dict]
    def add(self, memories, collection) -> int;  get(ids, collection);  retire(ids, collection)
    def dump(self, collection) -> str;  load(self, jsonl, collection, *, clear=True) -> int
    def ensure_snapshot(self, snapshot_text, collection) -> int;  forget_snapshot(collection)
    def embed(self, texts, *, query=False) -> list[list[float]]

# bridge.py（v6 签名）
class Bridge:  start(); stop(); open_scope(kind="research", *, scope_id, step, provider: MeteredProvider, record, models, effort_cap, max_concurrency) -> Scope
class Scope:   base_url; token; set_step(step); calls(step) -> list[dict]; close()

# rules.py
@dataclass
class Entry: id: str; when: str; content: str; origin: str; sources: list[str]; stats: dict; provenance: dict; superseded_by: str | None
def require_train(split, source_line, heldout) -> None                 # 否则 TrainOnlyError
def stamp(memory: dict, *, line, source_line, split, task_hash, activity, release_ref, attempt_id, trace_ref) -> dict
def parse_citation(finish_reason, last_text) -> list[str] | None       # `library-used: <ids>`
def credit(entries, *, task_hash, shown, cited, r, b) -> None
def retire_reason(entry, settings) -> str | None
def evictable(entries, settings) -> list[str]
def snapshot_rows(store_rows, entries) -> list[dict]
def readout(readings, order, entries) -> dict                          # D、两半、记功秩相关
def snapshot_abort(readout, settings, *, empty) -> str | None
def promotable(entries, settings, causes_vocab) -> list[tuple[Entry, str]]   # (entry, "skill" | "cause")

# track.py
class StreamStep:
    def __init__(self, client, settings, *, ledger_path, live_path, collection, business_line, trace: TraceStore, literal_forbidden, train_texts, bridge_scope)
    def resume(self) -> dict
    def process_batch(self, items: list[tuple[task, first, retry, baseline]]) -> list[dict]   # §5 的七步
    def checkpoint(self) -> None;  finish(self) -> dict;  snapshot_text(self) -> str;  entries(self) -> dict

# retrieval.py
class LibraryReader:
    def __init__(self, client_of, settings, *, business_line, leave_one_out: bool, snapshot_text: str | None, live: dict | None)
    def opening_block(self, task_text) -> tuple[str, dict | None]         # 开场块 + library_retrieval 记录
    def recall(self, task_text, query, k=None) -> list[Entry]              # runtime.MemoryRecall 的 reader

# snapshot.py
def snapshot_candidate(parent: Package, snapshot_text: str, *, objects, limits) -> dict[str, str]   # 只改 memory/snapshot.jsonl 的文件集
def library_of(package: Package) -> list[Entry]

# promote.py
def skill_candidate(entry: Entry, parent: Package) -> dict[str, str]    # skills/<slug>.md，带来源 ref
def cause_input(entry: Entry) -> dict                                   # IMPROVER §4 的 Cause 条目（memory_entries）
def mark_superseded(entries, entry_id, by: str) -> None

# importing.py（v6 签名）
def import_entries(snapshot_text, *, source_release_ref, source_line, target_line, client, settings) -> dict
```

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

- 开场：`opening_block(task_text)` 检索 `shown_k` 条，附在系统提示里；记 `library_retrieval{site:"start"}`。
- 尝试中：`recall(query)` 经 runtime 的 `MemoryRecall` → `LibraryReader.recall`；记 `library_retrieval{site:"recall"}`；同一留一规则。
- dev/test 从不是来源，所以在 dev/test 上没有排除；快照是冻结的，live 只在 train 流里更新。

## 7 流、快照与阶梯

- `library` 阶段（ACTIVITY §4）：对 train 逐题跑首遇（带库）、失败则重试一次（`retry`，带失败断言的类别）；每批后写入；最后 `readout`。
- `snapshot_abort`：D ≤ `abort_d` 不付 gate；否则 `snapshot_candidate` 产出只改 `memory/snapshot.jsonl` 的候选，交 improver 的阶梯从 V3 起（V0 的形状与字面量在写入时已过）。
- 装入后 dev/test 只见快照。

## 8 升级（DESIGN §5.6）

每轮改进器开始前 `promotable(entries)`：
- `fixes ≥ promote_fixes`、来源 ≥ `promote_sources`、不可机械化、正文是多步程序 → `skill_candidate`，作为固定代码候选进本轮阶梯（V0 字面量、V2c 可达、V3 起付费）；
- 可机械化（诊断词表里的 cause 能对上）→ `cause_input`，进本轮的簇；
- 升级候选装入后 `mark_superseded`。

## 9 不变量

- 宿主只用 httpx；ReMe、chroma、onnxruntime 只在 sidecar 的 venv 里；`tests/test_hot_path_boundary` 守住一次尝试不加载 `memory.sidecar`。
- 每次 sidecar 的模型调用都经桥、都计量；桥只服务 research 作用域。
- 任何非 train 的来源入库即 `TrainOnlyError`。
- 条目的 `provenance` 带 trace ref，可回到原始轨迹。
- 库内无 6 架上限；排序确定（名字作次键）。

## 10 与现有代码的对应

| 现有 | 处理 |
|---|---|
| `v6-memory` 的 `memory/*` | 搬入，`vel.py` 改名 `rules.py`，加 `promotable`；`track.py` 的 trace 输入改用 `improver.evidence.TraceStore`（trace 先搬到 improver，memory 只读它） |
| `v6-gateway` 的 `bridge.py` | → `memory/bridge.py`（唯一使用者在 memory） |
| `research/library.py`（HEAD 的 VEL） | 删 |
| `tests/test_memory_sidecar.py`、`test_memory_track.py`（v6） | → `tests/memory/` |

trace 的归属：`TraceStore` 在 `improver/evidence.py`；memory 依赖 improver 的这一个模块会违反 CONTRACTS §1 的方向，所以 `TraceStore` 放 `runtime/trace.py`（它只读 transcript，属于证据的读取面），improver 与 memory 都 import 它。IMPROVER §4 据此写。

## 11 测试

- sidecar：锁文件哈希不符拒绝启动；健康检查；`summarize` 走桥且每次调用有记录（变异：绕过桥的调用被 `_check_replies` 抓到）。
- 规则：train-only；引用记功与平分；观察期；退役两条；容量淘汰确定；快照行合并出处。
- 写入步：七步各一例；断点续跑（中途杀掉再 resume 不重复入库）；字面量占位符豁免。
- 读取：留一（来源含本题不给）；`recall` 经 runtime 记录。
- 快照：只改一个文件；readout 的 abort。
- 升级：两种条件各一例；装入后 superseded。
- 变异检查在隔离副本（v6 做过 15 项，照迁）。

## 12 开发顺序

搬入 → `settings.py`、`rules.py` → `retrieval.py` 接 runtime 的 `MemoryRecall` → `track.py` 接 `runtime/trace.py` → `snapshot.py`、`promote.py` → activity 的 `library` 阶段 → 测试。
