# evaluate：划分、硬限额、尝试、配对 gate、final 封存、发布与 model 绑定

> 阶段 1 设计文档。包 `ouroloop/evaluate/`。对应 [DESIGN](../DESIGN.md) §6.6（V3～V5）、§7、§8。来源：`benchmark.py`、`evolve/task_eval.py`、`evolve/task_gate.py`、`evolve/execution.py`（预算部分）、`release.py`、`ledger.py`（MeteredProvider 的计量部分）。2026-10-09 按阶段 1 的实现对齐。

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
# splits.py（现有 benchmark.py 的，哈希不变；多领域合并删）
class TaskEntry:      ...; @classmethod of(entry)          # 适配器交的是普通数据（dict），这里转成 TaskEntry
class SplitManifest:  freeze(path), load(path), hash, ids(*splits), split_of(task_id), family_of, entry, drift(entries), counts()
def assign_splits(families, ratios, seed) -> dict[str, str]
def stratified_splits(strata, ratios, seed) -> tuple[dict[str, str], dict]

# limits.py
@dataclass(frozen=True)
class Budget: max_steps=50; max_tool_calls=200; max_seconds=900.0; max_usd=None; max_model_calls=None; max_tokens=None
    def limit_hit(self, **used) -> str | None;  exceeded(...);  limits();  to_core(); from_core(limits)

# attempt.py
@dataclass
class Attempt: result: TaskResult; episode; score; agent: AgentResult | None
class Budgeted:   # episode 的工具面加上本次尝试的限额；代理放出去的请求经 proxied() 计入同一 max_tool_calls
def run_attempt(suite, task_id, make_agent, *, run: RunManifest, splits, attempt=0, arm="subject", retry_index=0,
                plan_ref="", release_ref=None, supersedes="") -> Attempt
    # 新 episode → make_agent(surface, task, attempt_id, split) → agent.run → episode.score() → TaskResult；
    # 钱的错误记 budget_exhausted；StaleWorker 与账户错误抛出
def run_tasks(suite, task_ids, make_agent, *, run, splits, results=None, repeats=1, order=None, concurrency=1, **kw)
    # 每次尝试完成即写 results 与评分行（train 才可读）；并发上限共享（in_flight_cap）
def make_agent_factory(*, provider, package: Package, core: dict, worker, root, pricing=None, memory=None,
                       release_ref=None, core_ref=None, arm="subject") -> Callable
    # memory(task, task_hash) -> TaskMemory | None（MEMORY §4）；task_hash(task) 是 task_id 与 contract 的摘要

# results.py（现有 benchmark.py 的，不变）
class RunManifest(...)      # candidate、model identity、limits、split hash、core_ref 摘要、package_ref
class TaskResult(...)       # task_id、family、split、attempt、metrics、usage、termination、valid、cost
class Results:              append、all、of_run、runs、put_run、put_grading、grading(attempt_id, splits=("train",))

# gate.py（现有 task_gate.py 的，去掉 economy / protected / per_domain）
@dataclass(frozen=True)
class GatePlan(Record): min_groups; min_gain; max_regression_rate; confidence_level; bootstrap_seed; bootstrap_replicates;
                        missing_cost_policy; regression_drop; cost_beta0; cost_beta1; min_tasks        # kind "gateplan"
@dataclass(frozen=True)
class TaskEvalPlan(Record): candidate_ref; control_ref; parent_release; core_ref; split_ref; task_ids; repeats; order_seed;
                            per_attempt_budget; retry_policy; gate_plan; price_ref; installs; control_pooling; confirmation_repeats   # kind "plan"
def judge(plan, subject, control, families, *, control_pooled=False, confirmation=None, first=None) -> (verdict, reasons, metrics, uncertainty)
def paired_bootstrap(subject, control, families, *, seed=0, replicates=2000, confidence=0.95) -> dict
def evaluate_task(plan, *, run, objects, families, results_sink=None, control_pool=None, confirming=None) -> GateReceipt
def refill_attempts(rows, run, arm, limit) -> list;  shortfall(plan, pool);  receipt_differences(receipt, objects)

# final.py
@dataclass(frozen=True)
class FinalPlan(Record): experiment_id; tasks; repeats; arms: dict[str, str]; claims; split_hash; core_ref;
                         max_infrastructure_retries; sealed_at（易变）            # kind "finalization"
def seal(plan: FinalPlan, root, objects) -> dict           # 写对象、状态文件与 final_sealed 事件；计划哈希要在第一次 test 尝试之前提交
def final_estimate(rows, plan, *, plan_ref, execution_of, pricing=None, concurrency=1, fallback=None) -> dict
def share_problem(estimate, share_usd, *, stuck=2) -> str
def run_final(plan, *, root, objects, run_arm, recorded, out) -> dict   # 记 test_read 事件；每臂跑全部 test 任务 × repeats；续跑只跑缺的；先估费由 activity 做

# budget.py（现有 execution.py / ledger.py 的，不变；execute_experiment 的状态机删，improver 用 Execution 自己驱动）
class Budgets:  account(scope_id, limits, parent) -> BudgetAccount; usage; headroom; reserve; settle
class BudgetAccount: child; reserve(operation_id, bound) -> Reservation; settle(reservation_id, measured) -> BudgetReceipt; allocate; remaining; affordable
class Pricing: bound(); measure(usage); cost(usage)
class MeteredProvider: chat(system, messages, specs, **kw)   # 预留→调用→结算；重试只对可重试的 ProviderError
class Execution: advance(status); operation(step, input_refs); finish(op, status); rebuild()   # 幂等续跑
def call_bound_problem(pricing, limits, what) -> str;  check_pricing(pricing, limits) -> str;  unfinished(root) -> list

# release.py
def attempt_version() -> str   # 一次尝试走到的代码的摘要（runtime/、harness/ 含种子、evaluate/{attempt,results,limits,splits}.py、adapters/、providers/、contracts.py）
def improver_version() -> str  # 其余代码的摘要（improver/、memory/、activity/、evaluate/{gate,final,budget,release}.py、R 的提示词）
def code_version() -> str      # 两者合一的摘要，只给报告
def attempt_identity(*, attempt_version, package_ref, limits, model, image_id, split_hash) -> str   # AttemptIdentity（CONTRACTS §4）
class GateReceipt(Record): plan_ref; subject_ref; control_ref; parent_release; results_ref; verdict; reasons; metrics; uncertainty; costs; kind   # kind "receipt"
class InstallReceipt(Record): operation_id; expected_parent; target_release; status; observed_pointer; reason   # kind "install"
class Releases:
    def active(self) -> Release;  active_ref();  pointer();  load(ref);  installed() -> set
    def seed(self, package_ref, improver_ref, core_ref) -> Release
    def candidate(self, package_ref, receipt_ref, *, parent=None) -> Release      # 父 release 换掉 W，R、K 不变
    def register_receipt(self, receipt_ref, **fields);  registered_receipt(ref) -> bool   # 只有完成的 gate 登记过的收据能装入
    def install(self, candidate_release: Release, receipt_ref, *, expected_parent: str, operation_id: str) -> InstallReceipt   # CAS；同一操作重放只提交一次
    def rollback(self, reason, *, operation_id) -> InstallReceipt   # 退到 last_good，没有就 NotAuthorized、什么都不撤销
    def last_good(self, ref=None) -> str | None                     # 装过、没撤销、同一 K 的最近祖先
    def revoke(self, ref, reason) -> bool;  revoked(ref) -> bool;  history() -> list[Release];  recover(operation_id)
```

## 4 一次尝试与评分

- 尝试 = 新 episode + 新 agent（runtime.PackageAgent）+ `agent.run` + `episode.score()`。agent 从不知道分数；评分器不在运行中被问。
- `TaskResult.valid`：termination ∈ {finished, budget, tool_refused, harness_error}（旧记录还有 step_limit）为有效；`infrastructure` 无效，不进 gate，按日程计数。`harness_error` 是包自报的缺陷，按现状评分，失败类 `candidate_defect`：gate 的完整性检查因此判该次比较 invalid。
- 评分行（断言逐条）只对 train 可读（`Results.grading(splits=("train",))`）；dev 只给汇总；test 只在 final 之后由 report 读汇总。

## 5 配对 gate（V4）

- 计划冻结在候选第一次尝试之前（`TaskEvalPlan` 入库，ref 进 K 的 `gate`）。
- 任务为单位：重复先平均；区间按家族重抽样。
- 规则：点估计 ≥ `min_gain`、退步率 ≤ `max_regression_rate`、成本项 `ΔC ≤ β0 + β1·ΔS`（有 pricing 时）、有效家族数 ≥ `min_groups`；任务数 ≥ `min_tasks` 在冻结 gate 模板时检查（`activity` 的 freeze 拒绝，`judge` 不再查）；首读通过后确认复测（`confirmations` 次），合并读数再判。对照读数按 `control_pooling`（fresh 或 pooled）与 `control_readings`。
- 收据 `GateReceipt{plan_ref, subject_ref, control_ref, parent_release, results_ref, verdict, reasons, metrics, uncertainty, costs, kind}`；各臂的全部尝试（含失败）在 results 对象里；逐题差值由 `receipt_differences` 从收据读出（`fixed / regressed / still_failing` 的 dev 题号只在收据里，不给 R）。

### 5.1 部分得分的接受规则（2026-10-10 修订）

`GatePlan` 多一个可选字段 `partial_min_gain`（K 的 `gate.partial_min_gain`，floor；None 即只按严格规则）。设了它时，`judge` 另算部分得分（`partial_credit`）的配对点估计与家族 bootstrap 区间，接受条件改为二选一：

1. 严格：`gain ≥ min_gain`（原规则）；
2. 部分得分：`partial_gain ≥ partial_min_gain` 且区间不含 0，并且 `gain ≥ 0`。

退步率、成本项、完整性不变；有确认读数时，确认读数单独也要满足所选的那一条（严格的按 `min_gain`，部分得分的按 `partial_min_gain`）。收据的 `metrics` 多 `partial_gain`、`partial_interval`、`accepted_by`（"strict" | "partial"）；报告两者并列，对外主张仍只用严格口径。

### 5.2 活动末的发布 gate（v1.2）

v1.1 每轮一次 gate；v1.2 活动内的接受由改进器的留出筛选决定（IMPROVER §9），dev 只在活动末读一次：

- 被测臂 = 工作线 W_work（`rsi/active.json`），读 `gate.repeats` 次（preset 2）；对照 = W0 在 dev 上的池化读数（基线的 `control_readings` 次，或导入的基线）。
- 规则不变（§5 的严格规则，或 §5.1 的部分得分规则）；第二次读数就是确认读数，单独也要满足所选的那条。
- 通过 → `Releases.promote(W_work, reason="gate")`：发布线 W\*，之后 `freeze` 封存 final；不通过 → 没有发布，活动结束。
- `GateReceipt.kind` 多一种 `release`；报告并列工作线的留出曲线（每轮的留出通过数）与这一次 dev 读数。
- **没有 dev 的活动（阶段 4c）**：`[bench] ratios` 的 dev 为 0 时，`freeze` 不写 gate 模板（`gate.min_tasks` 的检查只对有 dev 题的 K），round 的启动检查不要求模板，`--phase gate` 回答 `nothing_to_gate: no dev tasks`；发布走 §7 的按决定发布。

## 6 final（V5）

- `FinalPlan` 封存：任务 = 全部 test、`repeats`、臂（W0 与 W\*，可选 R1）、主张；臂名进每个 attempt id（即 session id），所以计划里 W\* 写作 `W_star`，不能进 attempt id 的臂名在构造时就拒绝；`seal` 写对象与事件，哈希由用户提交到 git（规则 6）。活动只在有发布线（gate 通过或按决定发布，W\* ≠ W0）之后封存（ACTIVITY §4）。
- 对外的数字（2026-10-12）：final 两臂各自的绝对通过率与部分得分是 README 的主数（W\* 得了多少，W0 臂并列），配对差与区间作为第二个数披露；只用公开集的 test 份，与官方榜的私有集不可比，README 要注明口径（题数、effort、repeats）。
- 跨域活动（v1.4，DESIGN §6.14）：final 计划只取 `[final] domains` 的 test 题，`release` 只补这些域里 W\* 缺的 train 读数，C1 的族重采样只在这些域上；别的域的 test 不读。
- 流式的数（v1.3，DESIGN §6.13）：`--phase stream` 的每道题只计首遇那一次；报告按批给首遇通过率、部分得分、每次尝试成本，整段的首遇通过率与前后半段之差是学习曲线。test 段流式跑时，这就是 final：计划先封存，题按序计分，计过分的题才对 R 可读；不再另跑两臂的 final。
- `run_final` 先打印预计花费与 final 的空间（份额加根的空余，§9），装不下就拒绝；每臂每题每次都是独立尝试；续跑只跑缺的；完成后 `report` 读汇总，披露该域 test 的读取次数。

## 7 发布与装入

- `Release` 三元组（CONTRACTS §5）。装入 = `Releases.install(candidate_release, receipt_ref, expected_parent=active.ref)`，CAS：active 不是期望的父就拒绝；`operation_id` 幂等。v1.2：`reason` 有 `seed`、`screen`（留出筛选接受，工作线）、`gate`（活动末发布，`promote`）、`rollback`、`import`；`rsi/active.json` 是工作线，`rsi/released.json` 是发布线（final 与导出只读它）。
- **按决定发布（阶段 4c）**：`Releases.promote(work, receipt_ref=None, reason="decision", operation_id)` 把工作线的 release 提升为发布线，不带收据；只在 K 没有 dev 题时允许（有 dev 的活动必须过 gate）。`--phase release` 调它之前，先在 train 上补齐 W\* 的包还没有读数的题（研究部分；留出部分筛选时已读过），在 W\* 自己的 release 下跑（role `measure`，候选名 `release:<ref>`），这样 final 的核价读得到 W\* 的带价读数（§6 的估算按 release 执行的 (package, core) 找读数）；工作线就是种子时拒绝（没有可发布的）。重复调用是同一个发布。
- 回滚只退到 `last_good`（装过且没被撤销的祖先）；没有可退的就拒绝、什么都不撤销。
- 种子 release：`reason="seed"`，`receipt_ref=None`。v1.3：`[rsi] seed_from` 时种子 release 的 `reason="import"`，`package_ref` 是旧工作区发布线 W\* 的包（闭包复制进来），`parent_ref=None`；启动检查里"种子 K 建出的就是种子 release 的 W0"一条改为核对导入事件（`seed_imported`：来源工作区、来源 release、包 ref）。
- 叠装（IMPROVER §9.4）：`install_screened` 接受 `outcome="stacked"` 的 ScreenResult，它的 `package_ref` 是叠出的包，`accepted` 为真，`stacked_on` 记它叠在哪个 release 上。

## 8 model 组件的绑定点

- 训练链（`train/`，V2）产出 `artifact_ref`（对象库里的 `train_artifact` 记录：产物位置、父模型、配方、数据 manifest、链路收据）。
- 候选把它写进 manifest 的 `model.artifact_ref`；`evaluate.model.check_binding(manifest, core, objects)`：`base` 等于 K 的 identity、产物的父模型等于 base、产物的数据 manifest 只含 train 血缘。阶段 1 的实现读产物记录自己的 `splits` 字段，还没有调用方；训练链接上时（阶段 5）改由 `train.dataset.check_lineage` 判血缘，并在候选进阶梯前调用。
- 这样的候选从 V3 起走阶梯；评测时 actor provider 由 `serving`（V2 §3.5，RunPod 上的 vLLM 经 `openai_compat`）按 `artifact_ref` 构建，仍经网关计量。
- 训练与 harness 改动不在同一轮同时进 gate（DESIGN §7）。
- v1.2：`AttemptIdentity`（CONTRACTS §4）相同的两个活动共享 W0 的基线读数：`import_baseline` 把旧工作区的 train / dev 读数与 transcript 引用复制进新工作区（只读旧工作区，记来源），gate 的对照池与第 0 轮的 measure 直接用它。

## 9 不变量与必须一直绿的测试

- 计划先冻结，读数后判；test 在封存前不读；dev 只给汇总；grading 只对 train。
- 每次付费调用先预留后结算；一个活动一个预算根；final 的份额事先留出；续跑不重复付费。
- 份额是下限不是上限（2026-10-12）：`final_usd` 是别的阶段动不了的钱；final 自己花完份额后可以继续用根里没分配的剩余，根的 `usd` 才是天花板。封存（freeze）与开跑（evaluate）前的检查按 final 的"空间"算——份额加根的空余（`phases.final_room`，`BudgetAccount.remaining(allocation="final")` 的 `effective_remaining`）——预期花费 + 并发份在飞预留 + 2 份卡住预留要装得下。finance-v5 的教训：份额 $200 按"约 $150"估，没算预留；预留按定价上限算（1M 输入 token × cache write 价，一次 $11.28），是实付的百倍，8 并发就 $113。
- `test_paid_run_safety` 的全部用例迁到 `tests/evaluate/`，名单（现有）：预留与结算一一对应、一个根、研究步计量、无法核价的候选不开始、中途耗尽作 budget_exhausted、尝试带成本、用量词表归一化、只在有界的维度上判超、崩溃后续跑只跑缺的、final 用预留的份额、完成的 final 再跑不付费、refill 记前因、中断的改进续跑、无 ref 的实验关闭不猜、freeze 的四条、价目含缓存、尝试保留缓存计数、gate 少于 min_tasks 拒绝、并发是机器设置、并发上限共享、两次确认与 economy 拒绝、速率限制记录、速率计数跨账户错误、研究会话每回合计量、final 份额装不下拒绝、final 先打印预计花费、final 估算读 W\* 的 gate 读数、份额装不下不开始、不封存、验证界核价、checks-only 在付费阶段拒绝。
- 阶段 1 已在 `tests/evaluate/test_paid_run_safety.py`（加上 `test_evaluate_budget.py`、`test_evaluate_gate.py`）：除下面几条外的全部。等 improver（阶段 3）的：研究步计量、无法核价的候选不开始、中断的改进续跑、无 ref 的实验关闭不猜、研究会话每回合计量、验证界核价，记在 IMPROVER §13。速率限制目前只在 meter 上计数并测试（`MeteredProvider.rate_limited`），还没有写进事件或报告。
- 失败的调用在重试前等 5 s、20 s（`RETRY_SECONDS`：断开的连接要等一会儿才回来）。请求没到模型（限流、过载，或 API 以 400 拒绝的畸形请求：空白的文本块、提示过长）释放；模型答了而答复不能用（不是一个回合、结构化输出放弃、换了模型）且提供方报了用量，按用量结算；其余（超时、子进程崩溃、没报用量）按上界记 unknown。
- 价目的两条保守规则：一次调用的上界按输入侧最贵的单价算（缓存写入可能比普通输入贵）；某类 token 没有声明价格的调用，账本按它的上界扣，不记 $0（该尝试自己的 `cost_usd` 仍是 None，即未知）。
- `test_record_compat`：HEAD 读得了四个旧工作区的 train/dev 记录（Results、RunManifest、transcript、release 与对象闭包）。

## 10 与现有代码的对应

| 现有 | 处理 |
|---|---|
| `benchmark.py` | 拆成 `splits.py`、`limits.py`、`results.py`；`V2_FIELDS`/`V06_FIELDS` 的兼容字段只保留读 |
| `evolve/task_eval.py` | → `attempt.py`；去掉 `bound`、policies |
| `evolve/task_gate.py` | → `gate.py`；删 economy / protected / `_ratio`；`screen()` 由 improver 的 V3 代替 |
| `evolve/execution.py` | 预算、Execution、Pricing、MeteredProvider → `budget.py`；`execute_experiment` 的状态机由 improver 的 round 用 Execution 自己驱动；`Schedule` → activity |
| `ledger.py` | `Ledger`（决策行）与 `SessionStore` 搬进 `train/ledger.py`；`_append_jsonl`、`redact`、`now`、`new_id` → `contracts.py` |
| `release.py` | → `release.py`；记录改三元组；`export_deployment` → `runtime.app.export` |
| `workspace.py` | 删（session 行由 runner 写 `task`/`end`，`.release.json` 不再需要） |

## 11 开发顺序

`limits.py`、`splits.py`、`results.py`（搬）→ `budget.py`（搬）→ `release.py`（改记录）→ `attempt.py`（接 runtime）→ `gate.py` → `final.py` → `model.py` → 迁 `test_paid_run_safety`、`test_record_compat`。
