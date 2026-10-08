# improver：证据、诊断、分层、编码会话、验收阶梯、筛选、档案、记功、台账、策略

> 阶段 3 的设计文档（接口随阶段 1 一起定）。包 `ouroloop/improver/`。对应 [DESIGN](../DESIGN.md) §6。来源：`research/{session,drafts,experiment,diagnosis,context,inspection}.py`（拆分重写）、`evolve/experience.py`、`v6-gateway` 的 `trace.py`（→ `runtime/trace.py`）。

## 1 职责与不做

**做：** 一轮改进：从 train 证据诊断出原因簇；按估值给簇指派组件类型；在自己的编码会话里实现候选；逐档验收；V3 筛选；交 evaluate 的 gate；装入与存档；条件记功；维护估值与剧本。

**不做：** 不跑 dev gate 的统计（evaluate）；不写 memory（只读它的升级输入）；不调 Claude Code 的循环；不读 dev/test 的内容。

## 2 Settings（K 的 `improver` 节）

```python
@dataclass(frozen=True)
class Settings:
    k: int = 3                                   # 每轮候选上限
    session_max_turns: int = 40; session_max_edits: int = 32; session_max_read_bytes: int = 65536; session_max_read_total: int = 1048576
    diagnose_max_cases: int = 60; diagnose_successes: int = 6; diagnose_cache: bool = True
    causes: tuple[str, ...] = CAUSES             # 词表，可增项（≥ 2 条案例）
    priors: dict[str, tuple[float, float]] = {"tool": (0.3, 0.3), "hook": (0.3, 0.3), "skill": (0.08, 0.1), "prompt": (0.08, 0.1), "memory": (0.1, 0.1), "agent": (0.1, 0.3), "config": (0.1, 0.2), "model": (0.0, 0.5)}   # (均值, 不确定度)
    explore_slots: int = 1; l5_slots: int = 1; code_guarantee: bool = True; stall_rounds: int = 3
    toolcheck_min_pass: int = 2; replay_min_triggers: int = 2; reach_min_tasks: int = 2
    screen_passing_sample: int = 6; screen_min_net_flips: int = 0; screen_min_fixes: int = 1; screen_max_sample_regressions: int = 1
    removal_zero_credit_rounds: int = 3
    round_share: float = 0.25                    # 每轮研究份额占根的比例上限
```

## 3 一轮（`round.py`）

```python
@dataclass(frozen=True)
class RoundResult: round_id: str; causes: int; assignments: int; candidates: int; ladder: dict[str, int]; screened: str | None; gate: dict | None; installed: str | None; stop: str | None; cost: dict
def run_round(activity, index: int, *, researcher: MeteredProvider, out) -> RoundResult
```

步骤与状态（`Execution` 记录，幂等续跑）：`evidence → diagnosed → allocated → implemented → verified → screened → gated → installed|rejected → credited`。每步的产物写 `rounds/<round_id>/`，输入 ref 相同就复用。

## 4 证据（`runtime/trace.py` + `improver/evidence.py`）

```python
# runtime/trace.py（v6 trace.py 原样搬入）
class TraceStore:
    def __init__(self, workspace_root, split_of: Callable[[str], str])
    def ref(session, line) -> str;  get(ref) -> dict;  events(session) -> list[dict]
    def trajectory(session, *, annotations=True) -> list[dict]       # actor 看到的消息序列（跨摘要重建）
    def calls(session) -> list[dict]
    def search(query, *, kind=None, tool=None, task_hash=None, limit=20) -> list[dict]   # FTS5，只限 train
    def index(self) -> dict;  rebuild(self) -> dict
    # 非 train 的 session：get/events/trajectory/search 抛 TraceAccessError

# improver/evidence.py
@dataclass(frozen=True)
class AttemptEvidence: attempt_id; task_id; task_hash; session; result: TaskResult; grading: list[dict]; hooks: list[dict]; tools_called: set[str]; entries_cited: list[str]; skills_read: list[str]
class Evidence:
    def __init__(self, activity, trace: TraceStore)
    def failed(self, run: str) -> list[AttemptEvidence]               # 本轮 train 测量里失败的
    def passed(self, run: str) -> list[AttemptEvidence]
    def best_successes(self, run: str, n: int) -> list[AttemptEvidence]
    def features(self, ev: AttemptEvidence) -> dict                    # 确定性特征：断言类型、工具错误、终止原因、写了几次、触发了哪些 hook
```

## 5 诊断（`diagnose.py`）

```python
CAUSES = ("excluded_mentioned", "missed_action", "wrong_target", "missing_recipient", "unread_value", "arith_mismatch",
          "real_date", "wrong_location", "premature_stop", "tool_error_loop", "context_overflow", "finish_protocol", "other")
MECHANIZABLE = {"excluded_mentioned", "arith_mismatch", "real_date", "wrong_location", "tool_error_loop"}   # 初值；词表增项时标注

@dataclass(frozen=True)
class Case(Record):  attempt_id; task_id; cause: str; mechanizable: bool; evidence_refs: tuple[str, ...]; assertion_kinds: tuple[str, ...]; tool_errors: tuple[str, ...]; termination: str; note: str
@dataclass(frozen=True)
class Cause(Record):  cause_id: str; name: str; cases: tuple[str, ...]; tasks: tuple[str, ...]; memory_entries: tuple[str, ...]; mechanizable: bool
def diagnose(evidence: Evidence, failed: list[AttemptEvidence], *, researcher, settings, cache: NoteCache, memory_inputs: list[dict]) -> list[Cause]
    # 每个失败尝试一次 aux 调用（输入：特征 + 轨迹的失败段 + 评分行的断言类别与参数）→ Case；按 cause 聚簇；memory 的升级输入并入
def vocabulary_extension(cases: list[Case]) -> list[str]            # 模型提出的新 cause 名，≥ 2 条案例才进词表（写进台账，下一轮生效）
```

缓存：`NoteCache` 按 (attempt_id, prompt digest) 命中则不再调模型。词表外的 cause 一律 `other`。

## 6 分层（`allocate.py`）

```python
@dataclass
class Estimate: line: str; cause: str; kind: str; fixes: float; cost: float; n: int; mean: float; unc: float
class Estimates:                    # ledger/estimates.json；跨活动可导入
    def get(self, line, cause, kind) -> Estimate      # 没有就用 priors
    def update(self, line, cause, kind, *, fixes: float, cost: float) -> None
    def stalled(self, line, kinds, rounds) -> bool
@dataclass(frozen=True)
class Assignment(Record): cause_id: str; kind: str; slots: int; exploration: bool; reason: str
def allocate(causes: list[Cause], estimates: Estimates, *, settings, line: str, round_index: int, history: Ledger) -> list[Assignment]
```

规则：
1. 每个簇的候选层 = 该簇可用的 kind（可机械化 → tool/hook/skill/prompt；否则 skill/prompt/memory；簇指向循环 → agent/config/vendor）。
2. 分数 = `mean + unc`（上界）；按簇大小排序取前 `k` 个指派，每簇一个 kind。
3. 探索：`explore_slots` 个名额给该 cause 上 `n == 0` 的 kind（优先代码类）。
4. 代码保证：有 `mechanizable` 簇而指派里没有 tool/hook → 用最后一个名额换成代码类；本轮结束时若仍没有代码候选到达 V2，台账记 `no_code_candidate`，下一轮该簇的代码名额加倍。
5. L5（agent/vendor/config）每轮 ≤ `l5_slots`，只在簇 ∈ {context_overflow, finish_protocol, tool_error_loop} 时。
6. L6（model）：只在 `stalled(line, 代码与文字类, stall_rounds)` 且训练链可用时指派一个名额（走 train/，不进本轮会话）。

## 7 编码会话（`session.py`）

```python
@dataclass(frozen=True)
class Declaration(Record): dir: str; cause_id: str; kind: str; site: str | None; predicted_affected: tuple[str, ...]; trigger: str; differs_from: str | None; scripts: dict[str, str]   # scripts: task_id -> toolcheck 脚本
class WorkTree:                      # worker 的 `work` op：init(package, inputs) / list / read / write / edit / delete / export(dir)
class CodingSession:
    def __init__(self, *, researcher: MeteredProvider, work: WorkTree, trace: TraceStore, checker: Callable[[dict, Declaration], Verdict], settings, k: int, prompts: Prompts, inputs: dict)
    def start(self) -> SessionResult
    def revise(self, verdict_text: str) -> SessionResult               # 验收退回的修订，续写同一会话
@dataclass
class SessionResult: candidates: list[tuple[str, dict[str, str], Declaration]]; turns: int; edits: int; ended: str
TOOLS = ("list_files", "read_file", "write_file", "edit_file", "delete_file", "read_trajectory", "search_trace", "run_tests", "run_check", "finish")
```

- 输入 `inputs`：指派（簇、kind、名额、簇的证据 ref 与案例摘要）、成功轨迹 ref、当前包的文件清单、台账的 train 投影（试过的机制与结果、归因记分板）、研究剧本、dev 失败类型直方图（枚举与计数）。
- `run_check(dir)` 对候选目录跑 V0～V2 并返回 Verdict 文本；`run_tests(dir)` 跑包测试；两者都不付费。
- 上限：`session_max_turns`、`session_max_edits`、读取字节；超限即结束，已有候选照常验收。
- 声明与 diff 不一致 → 退回一次修订；仍不一致 → 该候选 invalid。
- 提示文本在 `improver/prompts/{diagnose,propose,playbook}.md`，哈希进 `ImproverPolicy`。

## 8 验收阶梯（`verify/`）

```python
@dataclass(frozen=True)
class Verdict(Record): candidate_ref: str | None; dir: str; steps: dict[str, dict]   # {"V0": {"passed", "detail"}, ...}
    def passed(self, step) -> bool
def ladder_for(kind: str) -> tuple[str, ...]      # tool: V0,V1,V2a; hook: V0,V1,V2b; skill/memory: V0,V2c; prompt: V0; agent/vendor/config: V0,V1(smoke); test: V0
def run_ladder(files: dict[str, str], decl: Declaration, *, parent: Package, seed: Package, core, worker, evidence, suite, objects) -> Verdict

# verify/static.py   V0
def static_check(files, decl, *, parent, seed, limits, forbidden_literals) -> list[str]
    # check_shape；kind ∈ K.kinds；site ∈ K.sites；config 范围；字面量（相对种子改动行）；memory/ 未改；声明与 diff 一致；禁止 import（socket、subprocess、os.system、ctypes、pickle）
# verify/unit.py     V1
def unit_check(files, *, worker, timeout) -> dict          # worker 的 test op；agent/vendor/config 改动另跑 smoke op
# verify/toolcheck.py  V2a
def toolcheck(files, decl, *, suite, worker, broker_factory, evidence) -> dict
    # 对 decl.scripts 的每道 train 题：新 episode → worker 装候选 → `check` op 跑脚本（世界经可写 broker）→ episode.score()
    # 要求：broker 事件里新工具被调用；通过题数 ≥ toolcheck_min_pass；只回传 {task_id: {"passed", "failed_kinds"}}
# verify/replay.py   V2b
def replay(files, decl, *, worker, evidence, claimed: list[str], passed_sample: list[str]) -> dict
    # 从每道题的 transcript 重建六个站点的事件序列（gateway_call / tool / end 行），逐个经 `hook` op 调候选的 hook；
    # 触发 = 返回非 None；声称题触发 ≥ replay_min_triggers；报告通过题的误触发率
# verify/reach.py    V2c
def reach(files, decl, *, train_texts, settings) -> dict  # when/标题能检索到 ≥ reach_min_tasks 道来源外 train 题
```

## 9 筛选 V3（`screen.py`）

```python
@dataclass(frozen=True)
class ScreenResult(Record): candidate_ref: str; tasks: tuple[str, ...]; fixed: tuple[str, ...]; regressed: tuple[str, ...]; conditioned: dict[str, bool]; net_flips: int; cost: float; thin: bool
def screen(candidates: list[tuple[Package, Declaration, Verdict]], *, activity, run_tasks, settings) -> list[ScreenResult]
def pick_best(results: list[ScreenResult], settings) -> ScreenResult | None
```

- 任务集 = 声称的失败 train 题 ∪ 簇的题，∩ 最近一次 train 测量仍失败的；加固定通过样本 `screen_passing_sample`。
- 条件记功：tool 只在 `tools_called` 含它的题上记修复；hook 只在 `hooks` 触发过的题上；memory 在引用过的题上；skill 在读过的题上；prompt/agent/config 全部题。
- 净翻转 = 条件修复 − 退步；`min_net_flips`、`min_fixes`、`max_sample_regressions` 判；最优者进 V4（evaluate.gate）。

## 10 档案、记功、台账、策略

```python
# archive.py
class Archive:   # rounds/<round_id>/candidates/<dir>/ 与 objects
    def put(self, round_id, dir, files, decl, verdict, screen: ScreenResult | None, receipt_ref: str | None) -> str
    def rows(self, *, line=None) -> list[dict]
    def for_prompt(self, line, cause_id, n=10) -> list[dict]          # R 的输入：试过什么、结果、归因

# credit.py
@dataclass(frozen=True)
class Credit(Record): release_ref; component: str; kind: str; task_id: str; conditioned: bool; delta: float
def credit(release: Release, *, results: list[TaskResult], evidence: Evidence, control: list[TaskResult]) -> list[Credit]
def removal_candidates(credits_by_round: list[list[Credit]], package: Package, *, zero_rounds: int) -> list[dict[str, str]]   # 固定代码生成的删除候选文件集

# ledger.py
STAGES = ("diagnosed", "allocated", "declared", "verified", "screened", "gated", "installed", "rejected", "credited", "removal_proposed", "no_code_candidate", "vocabulary_added")
class Ledger:    # ledger/experience.jsonl
    def row(self, stage, **fields) -> dict
    def projection(self, *, line, train_only=True) -> list[dict]     # 给 R：去掉 dev 题号
    def scoreboard(self, line) -> dict                                # 归因：predicted_affected 命中 / 未命中 / 未预测的退步
class Estimates: ...                                                  # §6

# policy.py
@dataclass(frozen=True)
class ImproverPolicy(Record): prompts_ref: str; estimates_ref: str; playbook_ref: str; settings_ref: str
def update_playbook(ledger: Ledger, playbook: str, *, researcher) -> str    # 轮末一次 aux 调用：只保留台账核对过、带证据 ref 的教训
def export_policy(activity) -> ImproverPolicy;  def import_policy(activity, policy_ref) -> None   # 跨活动（C4 的 R1）
```

## 11 不变量

- R 读到的一切都来自 train：trace 的 split 守卫、grading 只对 train、台账投影去 dev 题号、dev 只有失败类型直方图。
- R 写的任何字节都在 worker 的工作树里；候选经 `export` 回宿主后才入对象库。
- V0～V2 不调付费模型；V3 起每次尝试先预留后结算，记在本轮的研究份额。
- 一轮的每一步幂等；同输入 ref 复用产物。
- 有 `mechanizable` 簇而无代码候选到达 V2 → 台账 `no_code_candidate`。

## 12 与现有代码的对应

| 现有 | 处理 |
|---|---|
| `research/session.py` | `CodingSession`、`WorkTree`、`TrajectoryReader` 的形状保留，改输入与工具；`ScreenSettings` → Settings；`FINISH_V5`、方法库相关删 |
| `research/drafts.py` | 草稿检查链 → `verify/static.py` + `verify/unit.py`；`Drafts.resolve` 的修订循环 → `CodingSession.revise` |
| `research/experiment.py` | `admit`/`declaration_reasons`/`grounding_reasons` → `verify/static.py` 的声明检查；`duplicate_experiment` → `archive` 的重复检查；其余删 |
| `research/diagnosis.py` | `failure_summary`、`NoteCache`、`Diagnosis.run` → `diagnose.py`（输出改 Case） |
| `research/context.py`、`inspection.py` | 删（投影由 `Ledger.projection` 与 `Evidence` 给） |
| `evolve/experience.py` | → `ledger.py`（行的形状改 STAGES） |
| `v6-gateway` 的 `trace.py` | → `runtime/trace.py` 原样 |
| 方法库 toolcheck 的前身（`research/methods.py` 的 `reach_check`、`values_in`、`replay_events`） | 思路进 `verify/toolcheck.py`、`verify/replay.py`，代码不搬 |
| `tests/test_research_session.py`、`test_research_round.py`、`test_diagnosis.py`、`test_cycle.py`、`test_rsi_integration.py`、`test_real_run_findings.py` | 改写到新接口，迁到 `tests/improver/`；脚本化 R（`tests/rsi.py` 的 `SessionScript`）保留 |

## 13 测试

- 证据：非 train 的 session 抛 `TraceAccessError`；search 只索引 train；特征提取确定。
- 诊断：脚本化 aux 给出案例；词表外归 other；增项需 ≥ 2 案例；缓存命中不调模型。
- 分层：上界排序；探索名额落在 n=0 的代码类；代码保证触发；L5 只在三类簇；L6 只在停滞。
- 会话：上限各一例；`run_check` 返回与 `run_ladder` 一致；声明与 diff 不符退回一次。
- 阶梯：V0 每条规则一例；V1 smoke 抓确定性崩溃；V2a 在 fake_upstream 上一个脚本通过、一个未调用工具被拒；V2b 触发计数与误触发率；V2c 可达。
- 筛选：条件记功（变异：把条件去掉，结果变）；thin；pick_best 的并列规则。
- 记功与反膨胀：零功组件 N 轮后产生删除候选。
- 台账与策略：投影无 dev 题号；记分板；剧本只含带 ref 的教训；导出导入往返。
- 端到端（`fake_upstream` 的 `rsi` 域，脚本化 R）：一轮产出一个 tool 候选过 V0～V2a、V3 记条件修复；一个 hook 候选过 V2b；有可机械化簇而无代码候选时台账记 `no_code_candidate`。

## 14 开发顺序

`runtime/trace.py`（搬）→ `evidence.py` → `ledger.py`、`archive.py` → `diagnose.py` → `allocate.py` → `verify/static.py`、`unit.py` → `session.py` → `verify/toolcheck.py`、`replay.py`、`reach.py` → `screen.py` → `credit.py` → `policy.py` → `round.py` → activity 的 `round` 阶段 → 端到端测试。
