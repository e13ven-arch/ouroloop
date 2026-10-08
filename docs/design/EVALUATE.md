# evaluate：划分、硬限额、尝试、配对 gate、final 封存、发布与 model 绑定

> 阶段 1 设计文档。包 `ouroloop/evaluate/`。对应 [DESIGN](../DESIGN.md) §6.6（V3～V5）、§7、§8。来源：`benchmark.py`、`evolve/task_eval.py`、`evolve/task_gate.py`、`evolve/execution.py`（预算部分）、`release.py`、`ledger.py`（MeteredProvider 的计量部分）。

## 1 职责与不做

**做：** 冻结划分；定义一次尝试并跑它（经 runtime）；按冻结的计划做配对 gate 与确认；封存 final；预算簿；内容寻址的发布与 CAS 装入、回滚；model 组件的绑定点。

**不做：** 不诊断、不提候选（improver）；不编排阶段（activity）；不读 memory。

## 2 文件

| 文件 | 内容 |
|---|---|
| `evaluate/splits.py` | `TaskEntry`、`SplitManifest`、`assign_splits`、`stratified_splits`、冻结与漂移检查 |
| `evaluate/limits.py` | `Budget`（五项硬限额）、`HARD_LIMITS` 的维度名 |
| `evaluate/attempt.py` | `run_attempt`、`run_tasks`、`Attempt`、`in_flight_cap` |
| `evaluate/results.py` | `RunManifest`、`TaskResult`、`Results`（results.jsonl、runs、grading） |
| `evaluate/gate.py` | `GatePlan`、`TaskEvalPlan`、统计（配对 bootstrap、退步、成本项）、`judge`、`evaluate_task`、确认复测 |
| `evaluate/final.py` | `FinalPlan`、`seal`、`run_final`、`final_estimate` |
| `evaluate/budget.py` | `Budgets`、`BudgetAccount`、`Reservation`、`Pricing`、`MeteredProvider`、`Execution`（操作日志与续跑） |
| `evaluate/release.py` | `Release`、`Releases`（active、install、rollback、last_good、revoke）、`GateReceipt`、`InstallReceipt` |
| `evaluate/model.py` | `ModelIdentity`；训练产物绑进 `model` 组件的检查（§8） |

## 3 接口

```python
# splits.py（现有 benchmark.py 的，不变）
class SplitManifest:  freeze(path), load(path), hash, ids(*splits), split_of(task_id), family_of, drift(entries), counts()
def stratified_splits(strata, ratios, seed) -> dict[str, str]

# limits.py
@dataclass(frozen=True)
class Budget: max_steps=50; max_model_calls=200; max_tool_calls=200; max_seconds=900; max_tokens=400000
    def limit_hit(self, **used) -> str | None

# attempt.py
@dataclass
class Attempt: result: TaskResult; transcript: str; cost: float | None
def run_attempt(suite, task_id: str, *, make_agent: Callable[[episode, task, system], PackageAgent], run: RunManifest,
                splits: SplitManifest, results: Results, transcripts: Path, budget: BudgetAccount, system_of: Callable) -> Attempt
    # 新 episode → agent.run → episode.score() → TaskResult（termination、usage、cost、valid）→ results.append；评分行写 grading（train 才可读）
def run_tasks(suite, task_ids, *, make_agent, run, repeats: int, concurrency: int, **kw) -> list[TaskResult]   # 并发上限共享
def make_agent_factory(*, provider, package: Package, core: dict, worker, memory_reader, pricing) -> Callable

# results.py（现有 benchmark.py 的，不变）
class RunManifest(...)      # candidate、model identity、limits、split hash、core_ref 摘要、package_ref
class TaskResult(...)       # task_id、family、split、attempt、metrics、usage、termination、valid、cost
class Results:              append、all、of_run、runs、put_run、put_grading、grading(attempt_id, splits=("train",))

# gate.py（现有 task_gate.py 的，去掉 economy / protected）
@dataclass(frozen=True)
class GatePlan(Record): min_gain: float; min_tasks: int; max_regression_rate: float; regression_drop: float | None
                        cost_beta0: float | None; cost_beta1: float | None; confirmations: int; control_pooling: str
@dataclass(frozen=True)
class TaskEvalPlan(Record): tasks: tuple[str, ...]; repeats: int; order_seed: int; gate: GatePlan; control_readings: int
def judge(plan, subject: list[TaskResult], control: list[TaskResult], families) -> Verdict   # Verdict{accepted, point, interval, regressed, cost_delta, reason}
def paired_bootstrap(subject, control, families, *, replicates=2000, seed=1) -> tuple[float, float, float]
def evaluate_task(plan, *, run_candidate, run_control, objects, budget, execution) -> GateReceipt   # 首读 + 确认，续跑用 Execution

# final.py
@dataclass(frozen=True)
class FinalPlan(Record): tasks: tuple[str, ...]; repeats: int; arms: dict[str, str]; claims: tuple[str, ...]; sealed_at: str; split_hash: str
def seal(plan: FinalPlan, root, objects) -> dict           # 写 objects、events；计划哈希要在第一次 test 尝试之前提交
def run_final(plan, *, activity, out) -> dict              # 每臂跑全部 test 任务 × repeats；先估费、先核份额；续跑只跑缺的
def final_estimate(activity, plan) -> dict

# budget.py（现有 execution.py / ledger.py 的，不变）
class Budgets:  account(scope_id, limits, parent) -> BudgetAccount; usage; headroom; reserve; settle
class BudgetAccount: child; reserve(operation_id, bound) -> Reservation; settle(reservation_id, measured) -> BudgetReceipt; allocate; remaining; affordable
class Pricing: bound(); measure(usage); cost(usage)
class MeteredProvider: chat(system, messages, specs, **kw)   # 预留→调用→结算；重试只对可重试的 ProviderError
class Execution: advance(status); operation(step, input_refs); finish(op, status); rebuild()   # 幂等续跑

# release.py
class Releases:
    def active(self) -> Release
    def seed(self, package_ref, improver_ref, core_ref) -> Release
    def install(self, candidate_release: Release, receipt_ref, *, expected_parent: str, operation_id: str) -> InstallReceipt   # CAS
    def rollback(self, reason, *, operation_id) -> InstallReceipt | None   # 退到 last_good，没有就拒绝
    def last_good(self, ref=None) -> str | None
    def revoke(self, ref, reason) -> bool
    def history(self) -> list[Release]
```

## 4 一次尝试与评分

- 尝试 = 新 episode + 新 agent（runtime.PackageAgent）+ `agent.run` + `episode.score()`。agent 从不知道分数；评分器不在运行中被问。
- `TaskResult.valid`：termination ∈ {finished, budget, tool_refused, harness_error} 为有效；`infrastructure` 无效，不进 gate，按日程计数。
- 评分行（断言逐条）只对 train 可读（`Results.grading(splits=("train",))`）；dev 只给汇总；test 只在 final 之后由 report 读汇总。

## 5 配对 gate（V4）

- 计划冻结在候选第一次尝试之前（`TaskEvalPlan` 入库，ref 进 K 的 `gate`）。
- 任务为单位：重复先平均；区间按家族重抽样。
- 规则：点估计 ≥ `min_gain`、退步率 ≤ `max_regression_rate`、成本项 `ΔC ≤ β0 + β1·ΔS`（有 pricing 时）、任务数 ≥ `min_tasks`；首读通过后确认复测（`confirmations` 次），合并读数再判。对照读数按 `control_pooling`（fresh 或 pooled）与 `control_readings`。
- 收据 `GateReceipt{plan_ref, subject_run, control_runs, per_task, verdict, confirmations[], cost}`；逐题差值进收据（`fixed / regressed / still_failing` 的 dev 题号只在收据里，不给 R）。

## 6 final（V5）

- `FinalPlan` 封存：任务 = 全部 test、`repeats`、臂（W0 与 W\*，可选 R1）、主张；`seal` 写对象与事件，哈希由用户提交到 git（规则 6）。
- `run_final` 先打印预计花费与份额，份额不够就拒绝；每臂每题每次都是独立尝试；续跑只跑缺的；完成后 `report` 读汇总，披露该域 test 的读取次数。

## 7 发布与装入

- `Release` 三元组（CONTRACTS §5）。装入 = `Releases.install(candidate_release, receipt_ref, expected_parent=active.ref)`，CAS：active 不是期望的父就拒绝；`operation_id` 幂等。
- 回滚只退到 `last_good`（装过且没被撤销的祖先）；没有可退的就拒绝、什么都不撤销。
- 种子 release：`reason="seed"`，`receipt_ref=None`。

## 8 model 组件的绑定点

- 训练链（`train/`，V2）产出 `artifact_ref`（对象库里的 `train_artifact` 记录：产物位置、父模型、配方、数据 manifest、链路收据）。
- 候选把它写进 manifest 的 `model.artifact_ref`；`evaluate.model.check_binding(manifest, core, objects)`：`base` 等于 K 的 identity、产物的父模型等于 base、产物的数据 manifest 只含 train 血缘（`train.dataset.check_lineage`）。
- 这样的候选从 V3 起走阶梯；评测时 actor provider 由 `serving`（V2 §3.5，RunPod 上的 vLLM 经 `openai_compat`）按 `artifact_ref` 构建，仍经网关计量。
- 训练与 harness 改动不在同一轮同时进 gate（DESIGN §7）。

## 9 不变量与必须一直绿的测试

- 计划先冻结，读数后判；test 在封存前不读；dev 只给汇总；grading 只对 train。
- 每次付费调用先预留后结算；一个活动一个预算根；final 的份额事先留出；续跑不重复付费。
- `test_paid_run_safety` 的全部用例迁到 `tests/evaluate/`，名单（现有）：预留与结算一一对应、一个根、研究步计量、无法核价的候选不开始、中途耗尽作 budget_exhausted、尝试带成本、用量词表归一化、只在有界的维度上判超、崩溃后续跑只跑缺的、final 用预留的份额、完成的 final 再跑不付费、refill 记前因、中断的改进续跑、无 ref 的实验关闭不猜、freeze 的四条、价目含缓存、尝试保留缓存计数、gate 少于 min_tasks 拒绝、并发是机器设置、并发上限共享、两次确认与 economy 拒绝、速率限制记录、速率计数跨账户错误、研究会话每回合计量、final 份额装不下拒绝、final 先打印预计花费、final 估算读 W\* 的 gate 读数、份额装不下不开始、不封存、验证界核价、checks-only 在付费阶段拒绝。
- `test_record_compat`：HEAD 读得了四个旧工作区的 train/dev 记录（Results、RunManifest、transcript、release 与对象闭包）。

## 10 与现有代码的对应

| 现有 | 处理 |
|---|---|
| `benchmark.py` | 拆成 `splits.py`、`limits.py`、`results.py`；`V2_FIELDS`/`V06_FIELDS` 的兼容字段只保留读 |
| `evolve/task_eval.py` | → `attempt.py`；去掉 `bound`、policies |
| `evolve/task_gate.py` | → `gate.py`；删 economy / protected / `_ratio`；`screen()` 由 improver 的 V3 代替 |
| `evolve/execution.py` | 预算、Execution、Pricing、MeteredProvider → `budget.py`；`execute_experiment` 的状态机由 improver 的 round 用 Execution 自己驱动；`Schedule` → activity |
| `ledger.py` | `Ledger`（决策行）搬进 `train/`；`SessionStore`、`_append_jsonl`、`redact`、`now`、`new_id` → `runtime/records.py` |
| `release.py` | → `release.py`；记录改三元组；`export_deployment` → `runtime.app.export` |
| `workspace.py` | 删（session 行由 runner 写 `task`/`end`，`.release.json` 不再需要） |

## 11 开发顺序

`limits.py`、`splits.py`、`results.py`（搬）→ `budget.py`（搬）→ `release.py`（改记录）→ `attempt.py`（接 runtime）→ `gate.py` → `final.py` → `model.py` → 迁 `test_paid_run_safety`、`test_record_compat`。
