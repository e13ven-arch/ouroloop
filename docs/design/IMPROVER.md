# improver：证据、诊断、聚焦、编码会话、验收阶梯、留出筛选、档案、记功、台账、策略

> 阶段 3 的设计文档（接口随阶段 1 一起定；2026-10-09 开发前按阶段 1、2 的代码定稿）。包 `reharness/improver/`。对应 [DESIGN](../DESIGN.md) §6。来源：tag `archive-2026-10-09` 上的 `research/{session,drafts,experiment,diagnosis}.py`（拆分重写）、`evolve/experience.py`；`runtime/trace.py`（阶段 2 已搬入）。

## 1 职责与不做

**做：** 一轮改进（v1.2）：量工作线在研究部分的 train；从 train 证据诊断出原因簇；给 R 一份聚焦清单；R 在自己的编码会话里实现一个候选包；逐档验收（V0/V1 对整包，V2 按组件）；V3 在全部留出题上筛选并按接受规则装入工作线；存档；条件记功；维护估值与剧本。固定代码的候选（memory 快照、memory 升级出的技能、删除候选）也走同一条阶梯。活动末的发布 gate 是 evaluate 的（`activity/phases.py` 的 `gate` 阶段）。

**不做：** 不写 gate 的统计（evaluate）；不写 memory（只读它的快照事件与升级输入）；不读 dev/test 的内容（dev 只有失败断言类型的直方图）；R 不跑 shell、不上网、不碰宿主文件。

## 2 Settings（K 的 `improver` 节，v1.2）

```python
@dataclass(frozen=True)
class Settings:
    packages_per_round: int = 3                  # R 每轮交的候选包数（v1.3：各自筛选，接受的叠装，§9.4）
    audit_hook_share: float = 0.3                # 只剩文字载体的簇占失败案例的份额到此，清单建议一个调 aux 的审核 hook（§6）
    stream_screen_max: int = 30                  # 流式一轮的留出筛选最多跑最近多少道留出题（§3.1；v1.4 从 20 提到 30）
    failures_max_cases: int = 30                 # inputs/failures.json 最多几条并排（v1.5，§7）
    failures_case_chars: int = 4000              # 一条并排的正文上限（写入摘录按比例截）
    audit_additive_only: bool = True             # v1.6：调 aux 的 hook 只能答 missing（fixed）
    audit_max_false_alarm: float = 0.25          # v1.6：V2d 预演里在通过题样本上报警的比例上限（cap）
    fixed_per_round: int = 2                     # 固定代码的候选（快照、升级技能、删除）每轮上限
    session_max_turns: int = 60; session_max_edits: int = 80            # v1.11：40 / 48 → 60 / 80
    session_max_read_bytes: int = 65536; session_max_read_total: int = 196608
    session_compact_tokens: int = 120000
    diagnose_max_cases: int = 60; diagnose_successes: int = 6
    causes: tuple = CAUSES; mechanizable: tuple = MECHANIZABLE
    priors: dict = {"tool": (0.3, 0.3), "hook": (0.3, 0.3), "skill": (0.08, 0.1), "prompt": (0.08, 0.1),
                    "agent": (0.1, 0.3), "config": (0.1, 0.2), "vendor": (0.05, 0.3), "model": (0.0, 0.5)}
    require_code_component: bool = True          # 有可机械化簇时包里至少一个 tool/hook，否则台账 no_code_candidate
    l5_per_round: int = 3                        # 一轮至多几处 agent/config/vendor 改动（v1.12：1 → 3）
    stall_rounds: int = 3
    hook_aux_calls: int = 2                      # on_finish / after_turn 的 hook 每次尝试最多调 aux 模型的次数
    toolcheck_min_pass: int = 2; replay_min_triggers: int = 1; reach_min_tasks: int = 2; reach_min_score: float = 0.2   # v1.11：replay_min_triggers 2 → 1
    replay_max_false_trigger: float = 0.25       # v1.7：V2b 回放里 hook 在通过题样本上的触发率上限（cap；DESIGN §6.17）
    holdout_share: float = 0.4                   # 留出的 train 题：R 看不到；V3 在全部留出题上筛
    accept_min_net_flips: int = 1                # 留出筛选的接受：可复现修复 − 可复现退步 ≥ 此（v1.7：改在可复现的计数上）
    accept_max_regressions: int = 3              # 留出里可复现的退步 ≤ 此（v1.7 从 1 降到 0；v1.12 放到 3，DESIGN §6.22）
    accept_min_whole_fixes: int = 2              # v1.7：足迹是全部题的包（只改 prompt / config / agent / vendor）要这么多可复现修复（floor）
    confirm_readings: int = 2                    # v1.7：确认时候选与 W 各要有的读数（候选 1 + 1；W 不足的补读；cap）
    accept_floor_tolerance: int = 3              # 留出通过数不低于活动内历史最好减此数（v1.12：1 → 3）
    removal_zero_credit_rounds: int = 3
    round_share: float = 0.15                    # 一轮的账户占根的比例上限
    regression_evidence: str = "research"        # v1.8：退步证据回灌 R 的档：counts | abstract | research（§9.5）
    regression_research_sample: int = 6          # v1.8：research 档在研究部分抽几道稳定通过题跑候选（cap）
    frontier_max: int = 4                        # v1.8：前沿最多几个部分包（cap）
    merge_per_round: int = 1                     # v1.8：一轮最多几个合并包（cap）
    frontier_min_fixes: int = 1                  # v1.8：进前沿要的可复现修复数（floor）
    frontier_max_attempts: int = 2               # v1.8：一个前沿条目最多合并几次（cap）
    text_additive_only: bool = True              # v1.8：prompt 与已有 skill 只能插入；删改另作 rewrite 包（fixed）
    heuristic_terms_min: int = 3                 # v1.8：触发条件里的字面词表到这么多词就记软信号（floor）
    trial_max_tasks: int = 4                     # v1.9：样本内试跑每包每次至多几道研究失败题（cap；0 关掉试跑，§9.6）
    trial_max_rounds: int = 2                    # v1.9：试跑之后 R 最多修订几次再试（cap）
    trial_max_attempts: int = 40                 # v1.9：一轮全部试跑尝试的上限（cap）；v1.11 从 12 提到 40（R 的 try 也计入）
    try_per_task: int = 2                        # v1.11：R 对同一道研究题最多 try 几次（cap；一次 rollout 是弱证据，DESIGN §6.21）
    trial_min_fixes: int = 1                     # v1.9：进留出筛选要的样本内修复数（free；0 只反馈不设门）
    regression_by_footprint: bool = True         # v1.9：只含 tool / hook / skill 的包，退步只在组件出手的题上算（fixed）
    merge_min_acts: int = 1                      # v1.9：合并版回放时须在条目修复题的尝试上出手的题数（floor）
    replay_sample: int = 12                      # v1.9：V2b / V2d 的通过题样本（floor；此前常量 6）
    scoped_packages: bool = True                 # v1.10：packages_per_round ≥ 2 时第一个包是通用的、第二个是领域的（fixed；§7）
```

合并规则：数量上限 cap（`packages_per_round`、`fixed_per_round`、会话上限、`hook_aux_calls`、`l5_per_round`、`round_share`、`stream_screen_max`）；`audit_hook_share` floor；门槛 floor（`accept_min_net_flips`、`toolcheck_min_pass`、`replay_min_triggers`、`reach_*`、`holdout_share`）；退步与容差 cap；`require_code_component`、`priors`、`removal_zero_credit_rounds` fixed；`causes`、`mechanizable` superset。v1.1 的 `k`、`explore_slots`、`l5_slots`、`code_guarantee`、`screen_*`、`thin_screen_tasks`、`gate_every` 删除。


v1.8（阶段 4i，DESIGN §6.18）：`regression_evidence` 三档逐级包含，规则 free（三档之一都可以配；research 档的花费由 `regression_research_sample` 的 cap 封顶，三档都只读研究题）；`replay_max_false_trigger` 默认改为 0.2。

v1.9（阶段 4j，DESIGN §6.19）：试跑的三个数量 cap、`trial_min_fixes` free（它只决定花不花筛选的钱，不是接受；0 时试跑只作反馈）；`regression_by_footprint` fixed；`merge_min_acts`、`replay_sample` floor。`REPLAY_SAMPLE` 常量删除。

v1.10（阶段 4l，DESIGN §6.20）：`scoped_packages` fixed。回放保真、分歧优先与按组件裁决没有超参。

## 3 一轮（`round.py`，v1.2）

```python
@dataclass(frozen=True)
class RoundResult: round_id: str; status: str; causes: int; components: int; ladder: dict; screen: dict | None; accepted: bool; stop: str | None; spend: dict
def run_round(activity, index: int, *, out) -> RoundResult
```

`round_id = f"{scope_id}/r{index}"`。状态与产物写在 `rounds/r{index}/round.json`：每步一个键，已有就复用，不重做。步骤：

`measure → evidence → diagnosed → focused → implemented → verified → screened → accepted | rejected → credited`

- **研究部分与留出部分**：train 题按 `holdout_share` 分两部分，整个活动固定（整族留出，family 按 `gate.order_seed` 与 family 名的哈希排序）。诊断覆盖研究部分的失败题；R 只看研究部分；留出部分只在 V3 跑，它的题号从不进 R 的输入、台账投影与剧本（换成计数）。
- **measure**：工作线的包在研究部分每道题要有一次有效读数；第 0 轮用基线（或导入的基线，ACTIVITY §4）；接受装入之后缺的题现量（run 的 role `measure`，每轮一个 run）。留出部分的读数来自上一轮的筛选（接受的包）或基线。
- **focused**：§6 的建议清单与硬要求。
- **implemented**：§7 的会话，产出 ≤ `packages_per_round` 个候选包；v1.9：每个过了免费阶梯的包随即在它声明的研究失败题上试跑（§9.6），结果回会话修订，至多 `trial_max_rounds` 次。
- **verified**：§8，按组件；不过的退回修订一次。v1.9：试跑过而样本内修复 < `trial_min_fixes` 的包不进筛选（`verified` 行 outcome `no_trial_fix`）。
- **screened**：§9，每个过 V2 的包在全部留出题上跑一次；`packages_per_round` > 1 时按净修复、再成本、再声明顺序挑最优。
- **accepted**：装入工作线（CAS，reason `screen`，`install:<round_id>` 幂等）；**rejected**：工作线不动，台账记读数。
- **credited**：§10，按组件在留出题上的足迹记功，估值更新；零功组件 N 轮后生成删除候选（下一轮作固定候选）。
- **stop**：研究部分没有失败题 → `nothing_to_fix`；份额不够付下一档 → `budget`；会话没有候选且没有固定候选 → `no_candidate`。停下的一轮不再续跑，`cycle` 从下一轮接着。
- **续跑**：会话一结束，声明与文件就记进 `round.json`；之后断了只重验、不重付会话；筛选的尝试按 attempt id 续跑；装入与记功幂等。
- 钱：一轮记在 `<scope>:round-<i>` 账户（上限 `round_share × [budget] usd`），下分 `measure`、`diagnose`、`session`、`screen`、`policy`；付诊断之前先核一个下限：研究部分补测 + 一次留出筛选（留出题数 × 当前 W 的 train 每次尝试成本 × 1.15）+ `trial_max_attempts` 次试跑（v1.9）+ 将要付的研究调用，放不进余额就停在 `budget`。子账户多一个 `trial`。

`cycle --until round-K` 依次跑第 0～K 轮；连续 `schedule.patience` 轮没有接受就停（`rounds` 上限 12，`patience` 3）。没有 carry 轮、没有活动内的 gate。活动末的发布 gate 是 `--phase gate`（EVALUATE §5.2、ACTIVITY §4）。

v1.3：`accepted` 之后多一步 `stacked`（§9.4）：第一个接受的包装入后，其余接受的包叠到工作线上、过 V0/V1、装入或记 `not_stacked`。

### 3.1 流式的一轮（v1.3，DESIGN §6.13）

`--phase stream` 每 `schedule.stream_batch` 道题调一次 `run_round(activity, index, pool=已计分的题)`：
- 不现量：证据池是 `pool` 里每道题的最新一次有效尝试（`Evidence.latest(pool, prefer=工作线的包)`：同一道题有工作线自己的尝试时取它，否则取任何包的最新一次——role `stream`、`measure`、`screen` 都算，`library` 不算），所以被拒候选的筛选尝试不会顶掉工作线自己的读数；装入之前在旧线上记下的失败留在池里当证据，直到这道题再被读到（流里一道题只首遇一次，所以同一个失败可能被再诊断；重复的候选由档案的去重挡住，不再付筛选）；
- 划分：`pool` 按同一个族哈希规则分研究 / 留出两部分（`holdout_share`），一道题的归属在整个流里不变；
- 诊断只看研究部分的失败，优先最近批次的；
- 筛选只跑留出部分里最近 `stream_screen_max` 道（按流的顺序），v1.9 另加本轮合并条目（`Focus.merge`）的修复集与退步集（按流的顺序并入，同一轮每个包同一个窗口），`reading` 是这些题的最新尝试；接受规则不变；`best_so_far` 是工作线历史上在这组留出题上的最高通过数；
- 划分的归属与批式同一个：先在整个 train 上按族哈希算出留出部分，再取池内的部分（研究 / 留出各按流的顺序）；
- 其余步骤同批式。轮的目录 `rounds/s<i>/`，round_id `{scope}/s{i}`，账户 `{scope}:round-s<i>`（`round.json` 的 `account` 与 `pool`）。


## 4 证据（`runtime/trace.py` + `improver/evidence.py`）

```python
@dataclass(frozen=True)
class AttemptEvidence: attempt_id; task_id; task_hash; session; result: TaskResult; failed_checks: tuple; tool_errors: tuple; hooks: tuple; tools_called: tuple; skills_read: tuple; entries_shown: tuple; writes: int
class Evidence:
    def __init__(self, activity, trace: TraceStore)
    def reading(self, release) -> dict[str, TaskResult]               # 该 release 的包在每道 train 题上最新的有效尝试
    def failed(self, release) -> list[AttemptEvidence]
    def passed(self, release) -> list[AttemptEvidence]
    def best_successes(self, release, n) -> list[AttemptEvidence]     # 步数少、成本低者优先
    def of(self, row: TaskResult) -> AttemptEvidence                   # 只对 train；dev/test 抛 TraceAccessError
    def features(self, ev) -> dict                                     # 确定性：失败断言类型、工具错误、终止、写入数、hook、工具、技能、条目
    def footprint(self, row, component: dict) -> bool                 # 该尝试里这个组件有没有出现（V3 条件记功与记功共用）
    def digest(self, ev, *, chars) -> dict                             # v1.5：一条并排——请求、失败断言（类型与参数）、写入（工具、方法、URL、正文摘录）、最后的话、读 / 搜计数
    def persistence(self, task_id) -> int                              # v1.5：这道题在工作线几个不同的包下失败过
    def readings_of(self, task_id) -> dict                             # v1.10：工作线历史包在该题的有效 train 读数 {passed, failed, passing_session, failing_session}
def dev_histogram(activity, release) -> dict                          # 失败断言类型 → 计数；不带题号、参数
```

`digest`（DESIGN §6.15）：写入 = `tool` 行里 `api_fetch` 非 GET / HEAD 的调用与包工具的写调用，取方法、URL（去协议）与正文前若干字符；GET 与 `api_search` 只计数；最后的话取 `end` 行的 final text。只对 train；留出题由调用方不传。

`readings_of`（v1.10，DESIGN §6.20）：工作线历史（`releases.history()`）里每个包在这道题上的有效 train 读数（role 不在 `NOT_READINGS`；被拒候选的 trial 不算），过与败各计数，并给最新的一次通过与失败的 session；`divergent = passed ≥ 1 and failed ≥ 1`。只对 train。

`footprint`：tool 在 gateway 的回复里被调用过；hook 在 `hook` 行里触发过（action ≠ none）；skill 在 `skill_read` 行里读到过；memory 条目在 `library_retrieval` 行里给过；prompt、agent、config、vendor 是全部题。

## 5 诊断（`diagnose.py`）

```python
CAUSES = ("excluded_mentioned", "missed_action", "wrong_target", "missing_recipient", "unread_value", "arith_mismatch",
          "real_date", "wrong_location", "premature_stop", "tool_error_loop", "context_overflow", "finish_protocol",
          "rule_not_applied", "vocabulary_mismatch", "omitted_report", "other")      # 后三个 v1.5（DESIGN §6.15）
MECHANIZABLE = ("excluded_mentioned", "arith_mismatch", "real_date", "wrong_location", "tool_error_loop", "vocabulary_mismatch")
LOOP_CAUSES = ("context_overflow", "finish_protocol", "tool_error_loop")
@dataclass(frozen=True)
class Case(Record):  attempt_id; task_id; cause; mechanizable: bool; evidence_refs: tuple; assertion_kinds: tuple; tool_errors: tuple; termination; note; proposed: str
@dataclass(frozen=True)
class Cause(Record):  cause_id; name; cases: tuple; tasks: tuple; memory_entries: tuple; mechanizable: bool
class NoteCache:  # rounds/notes.jsonl：(attempt_id, 请求摘要, 模型) → Case ref
def diagnose(evidence, failed, *, researcher, settings, vocabulary, cache, memory_entries, round_id) -> tuple[list[Cause], list[Case]]
def vocabulary_extension(cases, vocabulary) -> list[str]
```

- 每个失败尝试一次 aux 调用（≤ `diagnose_max_cases`，按上一次被诊断的轮次从旧到新，再按冻结顺序）：输入是确定性特征、轨迹的最后若干条（按字节截断）、train 评分行里失败的断言（类型与参数）、词表与定义；输出 JSON `{cause, note, evidence_refs, proposed?}`。`cause` 不在词表里一律 `other`；`proposed` 是模型提的新名。
- 词表增项：同一个新名在 ≥ 2 条案例里出现才进词表（台账 `vocabulary_added`，下一轮生效）；增项默认不可机械化。
- 缓存：请求摘要 + 模型名相同就复用，不调模型。
- memory 的升级（MEMORY §8）：`promotable(entries, settings, mechanizable)` 的 `mechanizable(entry)` 就是对条目原文做同一种 aux 分类，cause 落在 `mechanizable` 里 → `cause_input` 并进那个簇（`memory_entries`）；否则 → 技能候选（固定代码，§10）。
- 簇 = 同 cause 的案例；`cause_id = f"{round_id}:{name}"`；按题数、再按名字排序。
- v1.5 的三个 cause（DESIGN §6.15；定义进 `DEFINITIONS` 与诊断提示）：`rule_not_applied`——读过的政策 / 流程 / 更新邮件里的一条要求（一步、一个通知、一个值、一个阈值）没执行到；`vocabulary_mismatch`——记录、政策或请求命名了状态、类别、标签、列格式或单位，agent 用了自己的词、格式或单位；`omitted_report`——请求或政策要求出现在消息或记录里的通知、标记、条目没写（被挡住的动作没报告、被拒 / 被标记的项没列、汇总没发）。`missed_action` 与 `unread_value` 相应收窄为请求正文直接要求的。

## 6 聚焦（`allocate.py`，v1.2）

```python
@dataclass
class Estimate: line; cause; kind; fixes: float; cost: float; n: int; mean: float; unc: float
class Estimates:                    # ledger/estimates.json；跨活动可导入
    def get(self, line, cause, kind) -> Estimate     # 没有就用 priors
    def update(self, line, cause, kind, *, fixes, cost) -> None
    def stalled(self, line, kinds, rounds) -> bool
@dataclass(frozen=True)
class Focus(Record):
    ranked: tuple[dict, ...]        # [{cause_id, name, tasks, mechanizable, kinds: [{kind, score, n, tried: [archive refs]}]}]
    require_code: bool              # 有可机械化簇
    l5_suggested: bool              # 有 LOOP_CAUSES 的簇
    l6_suggested: bool              # stalled 且训练链可用
def focus(causes, estimates, archive, *, settings, line, kinds_allowed, train_available=False) -> Focus
```

1. 簇按题数从大到小；每簇列可用的 kind（可机械化 → tool、hook、skill、prompt；否则 hook、skill、prompt（v1.11：hold 对文档要求类失败也是载体）；`LOOP_CAUSES` 再加 agent、config、vendor），按分数 `mean + unc` 排序，附该 (cause, kind) 在档案里试过的候选与结果。
2. 清单是建议：R 自己决定包里放什么，可以对多个簇、一个簇多个载体。名额、探索、代码保证的翻倍都删除。
3. 硬要求在 V0 的声明检查里：`require_code` 而声明的组件里没有 tool / hook → 台账 `no_code_candidate`，候选照常验收；`l5_per_round` 之外的 agent / config / vendor 改动 → V0 拒绝。
4. L6：`l6_suggested` 时清单里写明"训练链可用"；阶段 5 之前恒为假，台账记 `l6_unavailable`。
5. 估值只由记功更新（§10）：一个组件在它声明的每个 cause 的留出题上的条件净修复 / 本轮花费的份额。
6. v1.3：`Focus` 多 `audit_suggested: bool` 与 `text_only_share: float`——只剩文字载体（不可机械化、不指向循环）的簇的案例数占全部案例的份额；份额 ≥ `audit_hook_share` 时 `audit_suggested` 为真，notes 里写明"一个调 aux 模型的审核 hook（on_finish / after_turn，`uses_model`）可以一个组件覆盖这些簇"，并列出它们。


## 7 编码会话（`session.py`，v1.2）

```python
@dataclass(frozen=True)
class Component(Record): kind: str; site: str; cause_ids: tuple[str, ...]; predicted_affected: tuple[str, ...]; trigger: str; scripts: dict; probes: dict; uses_model: bool
@dataclass(frozen=True)
class Declaration(Record): dir: str; mechanism: str; components: tuple[Component, ...]; differs_from: tuple[dict, ...]
class WorkTree:                      # worker 的 `work` op：init(package, inputs) / list / read / write / edit / delete / export(dir)
class CodingSession:
    def __init__(self, *, researcher, work, trace, checker, settings, prompts, inputs)
    def start(self) -> SessionResult
    def revise(self, verdict_text: str) -> SessionResult          # 验收退回的修订，续写同一会话，一次
@dataclass
class SessionResult: packages: list[tuple[str, dict[str, str], Declaration]]; turns: int; edits: int; ended: str
TOOLS = ("list_files", "read_file", "write_file", "edit_file", "delete_file", "read_trajectory", "search_trace", "run_tests", "run_check", "try_task", "finish")
```

- 输入 `inputs/`（v1.8 另加 `regressions.json`：上一轮可复现退步的证据，按 `regression_evidence` 的档；`frontier.json`：前沿条目的 diff、修复与退步计数、退步证据、尝试次数；§9.5）：`focus.json`（§6 的清单）、`causes.json`（研究部分全部簇与案例、证据 ref）、`successes.json`、`histogram.json`（dev 失败类型，只有枚举与计数）、`ledger.json`（投影，留出题号换成计数）、`scoreboard.json`、`playbook.md`、`package_api.md`、`base_tools.json`；v1.5 加 `failures.json`（DESIGN §6.15：研究部分每个失败案例的并排——请求、失败断言的类型与参数、写入、最后的话、cause、`persistent`，按 cause 分组，最多 `failures_max_cases` 条、每条 `failures_case_chars` 字；留出题不进）。
- 候选目录固定为 `candidates/package/`（`packages_per_round` > 1 时 `package-2/` …）；第一次写就把当前包复制进去。
- `run_check()` 对候选包跑 V0～V2，按组件返回；`run_tests()` 跑包测试；都不付费。
- `finish` 带包级声明：`mechanism` 一句话；每个改动的组件一条 `Component`；tool 的 `scripts` 或 `probes`、hook 的 `predicted_affected`、skill 的 `site` 是它的名字；`uses_model` 标出调 aux 的 hook。声明与 diff 不一致（声明了不存在的组件、改了没声明的组件）或某组件不过 V2 → 退回修订一次；仍不过 → 本轮无候选，台账记原因。
- 提示词（`improver/prompts/propose.md`）相应改：一轮交一个包；对清单里的几个簇一起下手；一个机制可以是工具 + 强制它的 hook + 说明何时用的技能；提交前的审核 hook 可以调 aux 模型；不把一道题的值写进包。
- v1.3：`packages_per_round` > 1 时提示词要求交几个包就是几种不同的机制、对不同的簇，第一个包对最大的簇；`audit_suggested` 时明说第二个包应当是审核 hook；每个组件的 `cause_ids` 尽量列出它覆盖的所有簇；叠装规则告诉 R：不同的包不要改同一个文件（manifest 除外）。
- v1.6（DESIGN §6.16）：审核 hook 用 `{"missing": [...]}` 答（§8 的 V2d 预演它；自由文本是候选缺陷）；提示词说明"只补缺、不改已写"是构造出来的，审核要做的是把读过的文档里的每条要求与 agent 的写入对照、列出没有对应写入的；`inputs/audits.json`（这条线上试过的审核：机制、预演与筛选的数、研究部分的误报原文）是下一个审核的起点。清单有几个簇就交几个包，少交要在 finish 里说明（台账 `single_package`）。
- v1.9（DESIGN §6.19）：**试跑的反馈**——会话 finish 之后，每个过了免费阶梯的包先在研究失败题上试跑（§9.6），宿主把每道题的结果作为一条 user 消息交回会话（`revise`）：题号、通过与否、出手的组件、失败断言的类别与参数（train 的评分行，R 本来看得到）、候选下的 session（加进 `readable`，R 用 `read_trajectory` 读），剩几次修订、剩几次试跑尝试；R 改文件后再 finish（修订不改声明），宿主重验阶梯、再试。**编辑额度提醒**：编辑数到 `session_max_edits` 的 75% 时那次工具结果末尾附一句（用了几次、剩下的留给 finish），到 90% 再附一次（`EDIT_REMINDERS`）；会话因 turns / edits 额度结束时台账记 `session_ended`（原因、turns、edits、已声明的包数）。
- v1.11（DESIGN §6.21）：**逐题 debug**——`try_task(task, dir?)`：宿主取回候选目录、build、V1，在这一道研究失败题上真跑一次（role `trial`，候选名 `{tag}:{dir}`，`trial` 子账户，按包 ref 免付），交回 `trial.contrast` 的并排文字（W 的读数与这次执行：通过、部分得分、步数、失败断言、出手的 hook 与它说的话、写入按路由、最后的话；R 的组件哪个出手；剩几次尝试），session 进 `readable`；留出题 `AccessDenied`；一轮 R 的 try 与宿主试跑合计 ≤ `trial_max_attempts`；每次 try 前核价；台账 `tried` 行。提示词改成 Harness² 的 probe 形状（读 W 的轨迹找最早分叉 → 一处追溯到时刻的改动 → try 一次 → 看对比：出手与否、说了什么、写入变没变是确定的依据，通过与否是一次 rollout、标明未确认 → 同一道题至多 `try_per_task` 次 → 下一道）；hold 是文档要求类失败的首选载体；审核不再是默认建议；声明字段清单加 `scope`。
- v1.10（DESIGN §6.20）：**分歧优先**——`failures.json` 每条并排加 `w_readings`（`{passed, failed}`）、`divergent` 与 `passing_session`（W 通过的一次 session，加进 `readable`）；排序：分歧题在先，其次 `persistent`，再其余（同档按 `persistence` 降序）。提示词加一段证据顺序：先改分歧（两条轨迹并读、找最早分叉的时刻），再改重复的具体失败，最后才是没兑现的风险；每处改动追溯到一个轨迹时刻，没有的不交；每个失败配最便宜的有效组件，常驻 prompt 最贵，按需的 skill / hook 优先。**两种 scope**——`scoped_packages` 且 `packages_per_round` ≥ 2 时提示词要求 `candidates/package/` 是通用的（`"scope": "invariant"`：agent 怎么做事——抽要求、核对、完成协议、工具使用、上下文），`candidates/package-2/` 是领域的（`"scope": "domain"`：从业者知道什么——交付物的形状、谁收、以哪份文档为准、记录自己的词与格式）；声明的 `scope` 可选，取值只能是这两个；台账 `declared` / `screened` 行记它。
- v1.5：提示词说明顽固失败的三种形态（文档里的一条要求没执行到、没沿用记录的词与格式、该报告的没报告），要求候选改变 agent 从文档到动作的方式——把读过的文档里的每条要求与原词抽成待办，做完前逐条对照，被挡住或被拒的项按请求报告——而不是再加一个"再看一眼"的提醒（scope hook 这类试过，带来了漏报）；`failures.json` 的 `persistent` 条目优先。


### 7.1 组合（v1.2：默认）

v1.1 一个候选一种组件，10-10 改为允许多载体，finance-v4 的九个候选仍全是单组件：规则是允许而不是要求，提示词也按"一个候选一个机制"写。v1.2 一轮一个包，组合是默认：阶梯按出现的组件类型取并集（有 tool 跑 V2a、有 hook 跑 V2b、有 skill 跑 V2c），每个组件的档都要过；记功按组件各自的足迹；估值按组件声明的 cause 更新。


## 8 验收阶梯（`verify/`，v1.2）

```python
@dataclass(frozen=True)
class Verdict(Record): dir: str; steps: dict[str, dict]; components: dict[str, dict]    # steps: {"V0", "V1"}; components: {site: {"V2a"|"V2b"|"V2c": {passed, detail}}}
    def passed(self) -> bool
def ladder_for(component: Component) -> tuple[str, ...]   # tool: V2a；hook: V2b（uses_model 时无）；skill: V2c；prompt/config/agent/vendor: 无
def run_ladder(files, decl, *, parent, seed, core, worker, evidence, suite, objects) -> Verdict
```

- V0（`verify/static.py`）：形状、K 允许的类型与站点、上限、相对种子改动行的字面量、`memory/` 未改、manifest、声明与 diff 一致（两个方向）、禁止 import；`require_code` 的检查在这里写台账；`l5_per_round` 在这里拒绝。v1.9：`middlewares/*.py` 里定义了两个位置参数 `(ctx, event)` 的函数而 manifest 的 hooks 没有哪条 entrypoint 指向它的，记 V0 结果的 `unregistered_hooks`（按文件）、verdict 与 `verified` 行，提示 R 挂上或删掉；不拒。
- V1（`verify/unit.py`）：包测试；agent / vendor / config 改动跑 smoke。
- V2a（`verify/toolcheck.py`）：每个 tool 组件，脚本或探针（§8.1）；`toolcheck_min_pass`。
- V2b（`verify/replay.py`）：每个 `uses_model = false` 的 hook 组件，在研究部分的轨迹上回放；声明的失败题上触发 ≥ `replay_min_triggers`，通过题样本（`replay_sample`，12）上的触发率 ≤ `replay_max_false_trigger`（v1.7；此前只报告，v7 装上了一个在 85% 的尝试上说话的 hook）；`verified` 行记 `false_trigger_rate`。
- V2b 回放保真（v1.10，DESIGN §6.20）：`attempt_events(trace, session)` 把一条轨迹重建成**一条**事件流——六个站点按记录顺序交错（before_run；每回合 before_model，然后按调用顺序 before_tool / after_tool 与 on_finish，回合末 after_turn），每个事件带 `view_events`（该时刻之前 broker 的调用，最近 `VIEW_EVENTS` 条，`call_id` / `name` / `args` / `result_excerpt` / `error` / `truncated` / `step`，与真跑的 `ctx.view["events"]` 同形；回合内按基础工具调用的名字顺序对齐 `tool` 行，包工具的调用吞掉两次对齐之间的行）与 `turn`；`site_events` 是它按站点的投影（V2d 与合并回放照用）。回放一道题：worker 里重新 `load` 包（模块状态清零），view 的 `events` 与 `turn` 按事件换，包里全部不调模型的 hook 按 manifest 顺序跑在每个事件上；first-wins 站点上先答 block / pushback 的 hook 结束该事件的派发（调模型的父 hook 按记录的 `acted`）；只有候选在声明站点改过的 hook 答非 none 才记触发。门槛与 `verified` 行的字段不变。
- V2c（`verify/reach.py`）：每个 skill 组件。
- V2d（`verify/audit.py`，v1.6，DESIGN §6.16）：每个 `uses_model = true` 的 hook 组件（after_turn / on_finish）。用 V2b 的回放机器把声明的失败题与通过题样本（`replay_sample`，12）的站点事件喂给它，`ctx.tool` 只读对着新开的世界，`ctx.model` 接研究模型（role aux，计在轮的 `verify` 子账户，`hook_aux_calls` 封顶）。报警 = 答了非空的 `missing`。门槛：声明的失败题上报警 ≥ `replay_min_triggers`，通过题样本上的报警比例 ≤ `audit_max_false_alarm`；另报告命中条目是否点到没过的断言（断言参数的字符串值或收件人出现在条目里）。不过门槛时退回文本列出每个误报（任务请求的前 300 字、审核的 `missing` 原文），会话修订一次再预演。预演是免费档的最后一级；过了进 V3。
- 合并包的回放（v1.9，`verify/merge.py`，verdict 的 `merge` 步）：声明 `merge_of` 的包，其不调模型的 hook 在条目修复题上候选自己的留出尝试（条目筛选时的 session，宿主的 `VerifyContext.merge_sessions`）上回放，须在 ≥ `merge_min_acts` 道上出手；不过时回给 R 的只有计数（"在条目修好的 2 道题的尝试上一道都不出手"），会话修订一次再回放；仍不过 → verdict 不过，不筛。没有可回放的 hook（只有 tool、或 hook 调模型）→ 步记 `not_replayable`，通过。
- 调 aux 模型的 hook（`uses_model = true`）：hook 的 ctx 带 `model(role="aux")`，每次尝试 `hook_aux_calls` 次封顶，超出即 `CandidateDefect`；`audit_additive_only` 下它只能答 `missing`，自由文本的 `pushback` / `message` 是 `CandidateDefect`（HARNESS §7）；V1 的 smoke 用脚本化模型答一次，V2d 预演，然后 V3。


### 8.1 V2a 的两种证明：脚本与探针（2026-10-10 修订）

finance-v2 里 R 两轮都没为 arith_mismatch 写工具：V2a 要求脚本把整道题做完才算过，对计算、比对一类的工具门槛过高。改为两种证明，任一种够 `toolcheck_min_pass` 道题即过：

- **脚本**（原样）：`scripts[task_id]` 做完整道题，世界按真实断言评分，须调用了声明的工具。这是强证明。
- **探针**（新）：`probes[task_id] = [{"args": {...}, "expect": "<子串或 null>"}, ...]`。宿主在该题的新世界里装载候选，对每个探针调用一次声明的工具（经可写 broker，世界用完即弃），要求没有 `is_error`、输出含 `expect`（给了的话）。这证明工具在真实世界的接口上能跑、输出对得上 R 从轨迹里抽出的输入；它不证明用它能做完任务，那是 V3 的事。
- 一道题两种都给时以脚本为准；回传给 R 的仍只有通过与否、失败断言的类别和探针的错误文本（截断）。
- `Declaration` 多一个 `probes` 字段；V0 校验形状（args 是对象，expect 是字符串或 null）。

## 9 留出筛选 V3 = 活动内接受（`screen.py`，v1.2）

```python
@dataclass(frozen=True)
class ScreenResult(Record): dir: str; package_ref: str; holdout: tuple; failing: tuple; passing: tuple; fixed: tuple; regressed: tuple   # v1.7：fixed / regressed 是可复现的；flipped_fixed、flipped_regressed、noisy、confirm_attempts
                            footprints: dict[str, tuple]; net_flips: int; holdout_score: int; best_so_far: int; cost: float | None; outcome: str; reasons: tuple
def screen(package_ref, decl, *, activity, base_release, holdout, readings, evidence, account) -> ScreenResult
def pick_best(results, settings) -> ScreenResult | None
def accept(activity, result, *, round_id) -> InstallReceipt     # 装入工作线
```

- 任务集 = 全部留出 train 题（`holdout_share` × train，finance 为 20 道）；候选包每道一次尝试（run 的 role `screen`，候选名 `r{i}:package`）。失败集 = 留出里当前工作线失败的题，通过集 = 其余。
- 条件记功：修复只在该题至少一个改动组件留下足迹时算（tool 调用过、hook 触发过、skill 读过；prompt / config / agent 的足迹是全部题）；退步在通过集上算——v1.9（`regression_by_footprint`）：只含 tool / hook / skill 组件的包，通过题上的翻转只在该次尝试有足迹时才是退步候选，没有足迹的记 `unconditioned_regressed`（不确认、不计、不进证据）；全足迹包照旧每道都算。`footprints[site]` 记每个组件留下足迹的题；`acted` 与 `footprint_share`（一次读数里有足迹的题数 / 有效读数；全足迹包 1.0）记候选在留出上的实际出手份额。
- 翻转确认（v1.7，DESIGN §6.17）：一次读数之后，候选与池里读数不同的每道题——条件修复与退步（无足迹的通过 `unconditioned` 本来不计，不再读）——在候选下再读一次（role `confirm`，候选名同筛选，账户同筛选）；对照面是当前 W 对这道题已有的有效 train 读数（首遇、现量、以前的确认；筛选与确认的候选读数不算），不足 `confirm_readings`（2）次的补读到两次（role `confirm`，候选名 `confirm:<W>`）。可复现修复 = 候选两次都过且有足迹、W 的读数全败；可复现退步 = 候选两次都败（v1.9：按足迹的包，每次都有足迹）、W 的读数全过；W 的读数有过有败 → `noisy`，不计。`fixed` / `regressed` 是可复现的集合，`flipped_fixed` / `flipped_regressed` 是一次读数的，`confirm_attempts` 记花了几次。
- 接受规则：`net_flips = 可复现修复 − 可复现退步 ≥ accept_min_net_flips`（1）、`可复现退步 ≤ accept_max_regressions`（0）、足迹是全部题的包（改动的组件全是 WHOLE_KINDS）`可复现修复 ≥ accept_min_whole_fixes`（2）、`holdout_score ≥ best_so_far − accept_floor_tolerance`（一次读数的通过数；`best_so_far` 是活动内工作线在留出上的最高通过数，基线算第一次）。三条都成立 → `accepted`，装入工作线；否则 `rejected`，原因写进台账与报告。
- 付费之前核价：留出题数（加确认的余量）× 当前工作线的 train 每次尝试成本 × 1.15；放不进本轮余额 → 不筛，停在 `budget`。v1.9：研究抽样不计入每个包的事前核价（它挤掉过第三个包）；筛选后只对有可复现退步的包按实际题数再核一次，放不下就不抽，证据对象的 `research.outcome` 记 `budget`，轮次不停。
- 候选只有一个时没有挑选；`packages_per_round` > 1 或有固定候选时，按净修复、再成本、再声明顺序挑最优，其余记 `not_selected`。固定候选（快照、升级技能、删除）与 R 的包分开筛，各自可接受，先 R 的包、再固定候选；同一轮两者都接受时先装 R 的包，固定候选在它之上重验 V0 后装入。

### 9.4 几个包：各自筛选，接受的叠装（v1.3）

- 每个过 V2 的包各自在全部留出题上筛一遍（钱按包核价、按声明顺序放，放不下的 `not_run`），接受规则各判。
- 接受的包按净修复、再成本、再声明顺序排；第一个照常 `accept()` 装入；其余逐个叠装（`stack()`）：取它相对本轮基线的改动集 `D = changed_paths(base, files)`，与当前工作线相对本轮基线的改动集求交；`manifest.json` 按节合并（tools / hooks / skills 按名字并集，同名两边都改即冲突；prompt / config / agent / vendor / memory 的同一键两边都改即冲突），其他文件两边都改即冲突；冲突 → 台账 `not_stacked`（reason `conflict: <paths>`）。无冲突 → 叠出的包过 V0、V1（`run_ladder(rungs=("V0","V1"))`，parent 是当前工作线），过 → 以它自己的筛选读数为依据装入（`ScreenResult` 复制一份换 `package_ref`、`outcome="stacked"`），台账 `accepted` 带 `stacked_on`；不过 → `not_stacked`（reason 是阶梯文字）。
- 叠装不重筛：假设是效果可加。下一轮的现量读的是叠装后的线，容差规则挡住退步。

### 9.1 留出部分的适应

R 看不到留出题，但工作线连续多轮按同一组留出题接受，会向它们适应（RRSI 把这叫 adaptive data analysis）。设计上的两道防线：容差规则（`accept_floor_tolerance`）挡住噪声里的来回；发布只看 dev，dev 只在活动末读一次（EVALUATE §5.2）。报告并列工作线的留出曲线与 dev 的一次读数，两者的差就是适应的量。

### 9.2 活动末的发布 gate

`--phase gate`（ACTIVITY §4）：工作线 W_work 对 W0 在全部 dev 上，W_work 读 `gate.repeats` 次（2），W0 用基线的池化读数（`control_readings` 2）；接受规则是 EVALUATE §5 的严格规则或 §5.1 的部分得分规则，第二次读数单独也要满足所选的那条（它就是确认读数）。通过 → `Releases.promote(W_work, reason="gate")` 成为发布线 W\*，之后 `freeze` 封存 final；不通过 → 没有发布，活动结束，报告写明工作线的留出曲线与 dev 读数。v1.1 的 carry 与 `gate_every` 删除。


### 9.3 部分得分进 gate（2026-10-10 修订；v1.2 只用于活动末的 gate）

严格通过率是任务级 0/1，25 题的分辨率是 4 个点；部分得分（通过的断言比例）是连续的，同一次读数的功效高一个量级。gate 同时报两者，接受规则二选一（EVALUATE §5.1）：严格增益 ≥ `min_gain`（原规则），或部分得分的配对增益 ≥ `partial_min_gain` 且其区间不含 0、严格增益 ≥ 0、退步率与成本项照旧；确认读数单独也要满足所选的那条。对外的 C1 仍按严格口径，部分得分并列披露。

### 9.5 退步证据回灌与前沿合并（v1.8，阶段 4i）

```python
# screen.py
@dataclass(frozen=True)
class RegressionEvidence(Record):   # 一道可复现退步的留出题，去题化
    domain: str; failed_kinds: tuple[str, ...]; passed_kinds_before: tuple[str, ...]
    footprints: tuple[dict, ...]       # [{site, kind, action, said}]：候选下在该题出手的组件与它说的话（R 自己写的文字）
    writes: dict                       # {"before": {tool/route: n}, "after": {tool/route: m}}，不含参数值
    termination: str
def regression_evidence(activity, result: ScreenResult, *, level: str, evidence) -> list[RegressionEvidence]
def research_regressions(activity, *, run_arm, package, base_release, sample: list[str], reading) -> dict   # research 档：研究部分抽样题在候选下的读数，退步题附两条轨迹的 session id
ScreenResult 多：partial: bool（可复现修复 ≥ frontier_min_fixes 且只因可复现退步被拒）、regression_ref（证据对象的 ref）、research_sample: tuple、research_regressed: tuple

# frontier.py
@dataclass
class FrontierEntry: id; round_index; tag; dir; line; files_ref; declaration_ref; base_package_ref; fixed: tuple; regressed: tuple; regression_ref; screen_ref; mechanism; kind; heuristic_terms: dict; attempts: int; status: str; history: tuple   # status: active | merged | subsumed | abandoned | dropped（超 frontier_max 被丢）
class Frontier:                     # rounds/frontier.json
    def add(self, entry) -> (entry, dropped_ids)          # 同一候选再加是原条目（续跑）；按可复现修复排序，超 frontier_max 的弱者 dropped
    def prune(self, line, line_passes) -> list[str]        # 修复集全被工作线通过的条目 → subsumed
    def picks(self, line, n) -> list[FrontierEntry]        # 本轮要合并的条目：attempts 最少、修复最多，至多 n 个
    def merged(self, entry_id, *, round_id, dir_name)      # 合并包装入 → merged
    def failed(self, entry_id, *, round_id, dir_name, replacement=None) -> (entry, "retried" | "replaced" | "abandoned" | "on_record" | "not_active")
    def for_prompt(self, entry, *, base_files, files, declaration, regressions, hide, scrub) -> dict   # diff、计数、证据；留出题号换成计数
```

- 筛选之后：`partial` 的包进前沿（`Frontier.add`，台账 `partial`）；工作线每次装入后 `prune`（台账 `frontier_pruned`）。
- 聚焦：前沿非空时 `Focus.merge = Frontier.picks(line, merge_per_round)`（每项：entry、round、dir、mechanism、kind、修复与退步计数、attempts、`cause_ids`——条目所列 cause 在本轮的 id，本轮没诊断到的沿用条目自己的、`candidate_dir`），提示词的合并段打开：交一个条件化版本，目录 `candidates/merge/`（第二个 `merge-2/`），声明 `merge_of: <entry id>`，组件同原包（触发条件收窄）。`read_declaration` 只接受 focus.json 列出的 entry id；V0 的 cause 检查也接受条目自己的 cause id。
- `partial` 的判定（`screen.partial_of`）：outcome 为 screened、未接受、有可复现退步、可复现修复 ≥ `frontier_min_fixes`，且把退步计数置零后接受规则的各条都成立（全足迹的包仍要 `accept_min_whole_fixes` 个修复）。
- 验收与筛选与普通包相同；接受并装入 → 条目 `merged`（台账 `merged`）；拒绝 → `attempts += 1`，到 `frontier_max_attempts` 则 `abandoned`（台账 `abandoned`）；合并包自己也是 `partial` 时替换原条目的文件、修复集、退步集与证据，attempts 继承（台账 `partial`，带 entry）。
- `research` 档的抽样：只对有可复现退步的包做（证据是给退步用的）；从研究部分里 W 的读数通过的题里取，V2b 回放里候选 hook 误触发的题优先，其余按候选名播种的固定顺序补到 `regression_research_sample`；候选跑一遍（role `research`，账户同筛选，v1.9：筛选后按实际题数核价，不计入事前上限）；退步的题记 `research_regressed`，`regressions.json` 里给两条轨迹的 session id（R 用 `read_trajectory` 读；只会是研究题）。证据对象（`regression_evidence_set`：level、计数、records、research、heuristic_terms）由 `screened` 行的 `regression_ref` 与前沿条目引用，同时写 `rounds/<tag>/regressions.json`；下一轮的 `inputs/regressions.json` 取本线最近 8 个带证据的包，按当前档只给对应内容。
- 受保护集：留出里首次有效读数通过的题；每轮 `measured` 行记 `protected` 与 `protected_passed`，报告画逐轮曲线。
- 文字只增（V0）：`verify/static.py` 的 `text_problems` 对 `prompt/*.md` 与父包已有的 `skills/*.md` 做行级 diff，出现删除或改写即 V0 不过，除非该包只含这一个组件且声明 `rewrite: true`（筛选时按全足迹条件）；声明了 rewrite 却还有别的组件也不过。固定代码的候选不查。
- 启发式软信号（V0）：`heuristic_terms(code, minimum) -> list[str]`：只数作为条件用的词表——`in` / `not in` 右边的字面量列表（或模块常量）、for / 推导式遍历的词表、交给 `re.*` 的正则的候选项；schema 的 `required` 这类数据不算。≥ `heuristic_terms_min` 个自然语言词时写进 V0 结果的 `heuristic_terms`（按文件）、verdict、`verified` 行、前沿条目与证据对象；不拒绝。

- v1.9（DESIGN §6.19）**修复证据**：每道可复现修复一条去题化记录 `FixEvidence`（域、W 下没过而候选下过的断言类别 `fixed_kinds`、出手的组件与它说的话、前后写入按路由、终止；无题号、请求、世界里的值），`fix_evidence(activity, result, ...)` 生成，进证据对象的 `fixes_evidence`（abstract 档及以上给 R）、`regressions.json`、前沿条目（`fix_ref`）与 `frontier.json`（`fixes_evidence`）。合并版的回放（§8 的 `merge` 步）用条目修复题上候选自己的留出 session（`FrontierEntry.fixed_sessions`：题号 → session，只在宿主）；流式窗口另加本轮合并条目的修复集与退步集（§3.1）；合并包的 `screened` 行与 ScreenResult 记 `kept_fixes`。

台账新增 stage：`partial`、`frontier_pruned`、`merged`、`abandoned`；v1.9 再加 `trialed`、`session_ended`。报告新增：受保护集通过数的逐轮曲线、前沿的进出、合并的接受率、research 档抽样的命中率；v1.9：每个筛选行的出手份额、无足迹的退步候选数、合并保留的修复数，每轮的试跑（题数、修复、尝试、进筛选的包）。

### 9.6 样本内试跑（v1.9，阶段 4j，`trial.py`）

```python
@dataclass(frozen=True)
class TrialResult(Record): candidate: str; package_ref: str; iteration: int; tasks: tuple; fixed: tuple; failed: tuple; unconditioned: tuple; attempts: int; outcome: str   # ok | budget | invalid | defect | not_run
def trial_tasks(decl, *, causes, study_failing, n) -> list[str]   # 声明的 predicted_affected（空则取组件 cause 的题）∩ 研究部分当前失败的题，冻结顺序，至多 n
def trial(activity, *, run_arm, name, package, base_release, tasks, reading, evidence, conditions, account, retries, iteration) -> TrialResult
def feedback(result, *, evidence, grading, revisions_left, attempts_left) -> tuple[str, set]   # 给 R 的文字与可读的 session
```

- 时机：会话 finish → 阶梯 → 过了的包试跑 → 结果 `revise` 回会话 → R 改文件再 finish → 重验阶梯 → 再试，至多 `trial_max_rounds` 次修订；会话已因额度结束的不修订。固定代码的候选不试。
- 题：`trial_tasks`，每包每次 ≤ `trial_max_tasks`；一轮全部包全部迭代的尝试 ≤ `trial_max_attempts`，用完就不再试、不再修订。没有可试的题 → 不试、不设门，`trialed` 行记 `not_run`。
- 跑：run 的 role `trial`，候选名同筛选（`{tag}:{dir}`），钱记本轮 `trial` 子账户；run 的身份含包 ref，修订后的包是新 run；续跑按记录免付。每次试跑前 `_afford`（题数 × W 的 train 每次尝试成本），放不下 → 这次不试（outcome `budget`），包照常按核价进筛选。
- 判：样本内修复 = 通过且至少一个改动组件有足迹（与 V3 的条件记功同一个 `condition` 与 `footprint`）；通过而无足迹记 `unconditioned`，不算。
- 门：`verified` 步只放行试跑过且修复 ≥ `trial_min_fixes` 的包（以及没试的包）；不过的 `verified` 行 outcome `no_trial_fix`，台账 `trialed` 行每次迭代一行（round_id、dir、iteration、tasks、fixed、failed、unconditioned、attempts、passed）。`round.json` 的 implemented 步记每包的 `trial`（迭代列表与总尝试）。
- 不是接受：不记功、不进估值与记分板的修复计数；留出题从不在试跑里。R 看到的 session 都是研究题的。
- 读数：试跑的尝试是候选包在那道题上的有效 train 读数——包装入后它就是工作线对这道题的读数（下一轮的 measure 与 release 的补读不再付它）；被拒候选的试跑不是流式证据池的读数（`Evidence.latest` 跳过非工作线包的 `trial` 尝试），旧线上的失败仍留作证据。

## 10 档案、记功、台账、策略、固定候选

```python
# archive.py
class Archive:   # rounds/r<i>/candidates/<dir>/{files.json, declaration.json, verdict.json, screen.json}
    def put(self, round_id, dir, files, decl, verdict, screen=None, receipt_ref=None) -> str
    def rows(self, *, line=None) -> list[dict]
    def for_prompt(self, line, cause_id, n=10) -> list[dict]

# credit.py
@dataclass(frozen=True)
class Credit(Record): release_ref; component: str; kind; task_id; conditioned: bool; delta: float
def components(parent: Package, child: Package) -> list[dict]          # child 相对 parent 改动的组件 {kind, name, site?}
def credit(release, parent_reading, child_reading, *, components, evidence) -> list[Credit]
def removal_candidates(credits_by_round, package, *, zero_rounds) -> list[tuple[dict, dict[str, str]]]   # (组件, 删掉它的文件集)

# ledger.py
STAGES = ("measured", "diagnosed", "focused", "declared", "verified", "screened", "accepted", "gated", "installed",
          "rejected", "credited", "removal_proposed", "no_code_candidate", "vocabulary_added", "l6_unavailable", "stopped", "playbook",
          "partial", "frontier_pruned", "merged", "abandoned",           # v1.8
          "trialed", "session_ended")                                   # v1.9
class Ledger:    # ledger/experience.jsonl
    def row(self, stage, **fields) -> dict
    def rows(self, *, stage=None, round_id=None) -> list[dict]
    def projection(self, *, line, hide=()) -> list[dict]   # 给 R：只有 train 题号（hide 里的留出题换成计数）；dev 只有计数
    def scoreboard(self, line) -> dict              # 每类候选在筛选上的修复、退步、净值（计数）
    def audits(self, line) -> list[dict]             # v1.6：这条线上试过的审核——机制、V2d 的命中与误报（研究部分的误报带原文）、筛选的数（`audited` 与 `screened` 行）

# policy.py
@dataclass(frozen=True)
class ImproverPolicy(Record): prompts_ref; estimates_ref; playbook_ref; settings_ref
def update_playbook(ledger, playbook, *, researcher, line) -> str      # 轮末一次 aux 调用；每条教训要引用台账行 id，引不上的丢掉
def export_policy(activity) -> str;  def import_policy(activity, policy_ref) -> None
```

- v1.10（DESIGN §6.20）**按组件裁决**：筛选后 `component_verdicts(result)` 对包改动的每个组件给 `{verdict, acted, fixed, regressed}`——`hurt`（它出手的题里有可复现退步）、`helped`（出手的题里有可复现修复、无退步）、`no_effect`（出手了、出手的题零可复现翻转）、`unobserved`（留出上没出手）；包级 `confounded` = 无足迹的翻转数（`unconditioned` + `unconditioned_regressed`）。进 `screened` 行的 `verdicts`（只有计数，题号不进）与 R 的投影；`scoreboard` 在 kind 之外按 `scope` 另列（`scope:invariant`、`scope:domain`）；剧本提示词：每条教训点名组件与它的裁决，`no_effect` 的组件不得写成起效。
- 台账的 `screened` 行（v1.7）：`fixed` / `regressed` / `net_flips` 是可复现的，另记 `flipped_fixed` / `flipped_regressed` / `noisy` / `confirm_attempts`；`verified` 行的 V2b 多 `false_trigger_rate`。v1.9：`screened` 行多 `unconditioned_regressed`、`acted`、`footprint_share`、`kept_fixes`（合并包）；`verified` 行多 `unregistered_hooks`、outcome `no_trial_fix`；`trialed` 行与 `session_ended` 行如 §9.6、§7。
- 记功（v1.2，接受后）：直接用留出筛选的读数——对改动的每个组件，在它留下足迹的留出题上记 `delta = r_新 − r_旧`；研究部分在下一轮 measure 补测后再记一次。对改动的每个组件，在 `footprint` 为真的题上记 `delta = r_新 − r_旧`；估值 `(line, cause, kind)` 用该 cause 的题上、声明的 kind 的组件有足迹处的净修复与本轮花费更新。估值的每个观测按 `round:dir` 记：筛选时记一次（条件净翻转 / 失败集），装入后用记功的读数替换它，续跑写同一键不重复计。
- 反膨胀：一个组件连续 `removal_zero_credit_rounds` 个已记功的轮次条件记功为零 → 删除候选（固定代码生成），下一轮作为固定候选进 V3。
- R 的策略（估值与剧本）在活动内随台账更新；release 里的 `improver_ref` 在一个活动内保持 R0，活动结束时 `export_policy` 产出下一个活动的 R1（DESIGN §6.9）。
- 升级的技能装入后，记功一步调 `MemoryService.supersede`：流的台账里条目 `superseded`，live store 退役它，live 库文件里没有它（MEMORY §8）。memory 条目的分类按（条目、请求、模型）缓存在 `rounds/notes.jsonl`。
- 固定候选的目录名（`memory-snapshot`、`promoted-`、`remove-` 开头）R 不能用。v1.2：library 默认关（MEMORY §9），没有 library 时固定候选只有删除候选。
- 固定候选（每轮 ≤ `fixed_per_round`，先快照、再升级技能、再删除）：library 阶段的快照候选（`library_snapshot` 事件，outcome candidate，还没进过阶梯的；快照文本是 library 阶段按 K 的 `max_memory_bytes` 装好的，MEMORY §7.1，原样放到当前 W 上，同一个 K 下照样放得下）走 V0 → V3；memory 升级出的技能候选走 V0、V2c → V3；删除候选走 V0 → V3。它们和 R 的包一起筛选；一轮只装一个（先 R 的包），被接受而未选中的固定候选记 `not_selected`，下一轮再作固定候选。

## 11 不变量

- v1.2：留出题从不进 R 的输入、台账投影、剧本与档案的文字；dev 只在活动末的 gate 读一次；活动内的装入只凭留出筛选；发布只凭 dev gate。

- R 读到的一切都来自 train：trace 的 split 守卫、grading 只对 train、台账投影不带 dev 题号、dev 只有失败类型直方图。
- R 写的任何字节都在 worker 的工作树里；候选经 `export` 回宿主后才入对象库。
- V0～V2 不调付费模型；V3 起每次尝试先预留后结算，记在本轮的账户；付费前核价。
- 一轮的每一步幂等；`round.json` 里有的步不重做；中断后重跑同一轮只付缺的。
- 有可机械化簇而无代码候选到达 V2 → 台账 `no_code_candidate`。
- 装入的 `operation_id` 是 `install:<round_id>`，不与 Execution 的操作 id 冲突。

## 12 与现有代码的对应

`research/` 与 `evolve/` 已在阶段 1 删除，下表的“现有”从 tag `archive-2026-10-09` 读。

| 现有 | 处理 |
|---|---|
| `research/session.py` | `CodingSession`、`WorkTree` 的形状保留，改输入与工具（加 `search_trace`、`run_tests`、`run_check`）；`TrajectoryReader` 改由 TraceStore 重建轨迹；`ScreenSettings` → Settings；`passing_sample`、`screen_counts`、`pick_best` 的规则保留；方法库、读取下限、`FINISH_V5` 删 |
| `research/drafts.py` | 草稿检查链 → `verify/static.py` + `verify/unit.py`；修订循环 → `CodingSession.revise` |
| `research/experiment.py` | 声明检查 → `verify/static.py`；`duplicate_experiment` → `Archive` 的重复检查；其余删 |
| `research/diagnosis.py` | `failure_summary`、`NoteCache` → `diagnose.py`（输出改 Case，轨迹改由 TraceStore 给） |
| `research/context.py`、`inspection.py` | 删（投影由 `Ledger.projection` 与 `Evidence` 给） |
| `evolve/experience.py` | → `ledger.py`（行的形状改 STAGES） |
| `research/methods.py` 的 `reach_check`、`replay_events` | 思路进 `verify/reach.py`、`verify/replay.py`，代码不搬 |

## 13 测试

- v1.11：脚本化 R 的模式 `debug`——写弱 hook，`try_task` 票据题：对比文字说 FAILED、hook 出手、W 的失败断言；改成能用的 hook 再 try：PASSED；finish 后宿主的试跑按记录免付（attempts 0），包筛选装入；台账 `tried` 两行；留出题 `AccessDenied`；没有候选目录时拒绝；尝试上限用完拒绝；同一道题超过 `try_per_task` 拒绝；`try` 计入 `_trial_used`；对比文字把通过标为未确认。V2b 把 R 的 try 里 hook 出手的认领题计为触发（`tried_acted`）；回放把 modify 后的事件传给下一个 hook；R 写了没交的目录进台账 `unfinished`，下一轮的 `_unfinished_input` 把改过的文件放到 `inputs/unfinished/<dir>/`。

- v1.10：回放保真——假上游一条 train 轨迹的 `attempt_events` 按记录顺序交错六个站点，每个 before_tool / after_tool 事件的 `view_events` 只含它之前的 broker 调用；一个两 hook 的包（before_run 记状态、before_tool 只在状态与 `view["events"]` 都成立时 hold）在 V2b 里于声称题出手（旧回放里永远不出手）；每道题重载后前一道题的状态不泄漏；V2d 与合并回放照旧通过。分歧优先——W 有过有败的题在 `failures.json` 里 `divergent`、带 `passing_session`、排在顽固题之前，`readable` 含该 session。scope——声明的 `scope` 只收两个值；`packages_per_round` ≥ 2 时提示词含两段 scope 的要求、1 时不含；`declared` / `screened` 行与记分板带 scope。裁决——`component_verdicts` 对出手零翻转的组件给 `no_effect`、对有退步的给 `hurt`、没出手的 `unobserved`，`screened` 行与投影里只有计数。

- v1.9：脚本化 R 的模式 `trial`（第一版的 hook 在试跑里不出手、不过；收到试跑反馈后修订成出手的版本，第二次试跑过，进筛选并装入）与 `trial_bad`（修订后仍不过：`no_trial_fix`，不筛，台账两行 `trialed`）；试跑的尝试记在 `trial` 子账户、按候选名与包 ref 续跑免付、`trial_max_attempts` 用完不再试；`trial_max_tasks = 0` 时不试照常筛；退步按足迹：hook 没出手的通过题翻转记 `unconditioned_regressed`、不确认、不进证据，全足迹包照旧；`footprint_share` 与 `acted`；修复证据的记录与 R 的输入（无留出题号）；合并版回放不出手 → 修订一次 → 仍不出手不筛；流式窗口含条目的修复集与退步集；导入 stream 读数（同一 AttemptIdentity）后 `--phase stream` 不再付首遇；V0 的 `unregistered_hooks`；`heuristic_terms` 对 HTTP 方法元组、工具名列表、驼峰与短词不报；编辑额度 75% / 90% 的提醒与 `session_ended`；研究抽样不计入事前核价、筛选后放不下就不抽。

- v1.8：部分包进前沿（修复 ≥ 1、只因退步被拒）；前沿排序与 `frontier_max`、`subsumed`、`abandoned`；合并包占名额、声明 `merge_of`、接受后条目 `merged`；`regressions.json` 三档的内容（abstract 不含题 id / 请求 / 参数值；research 带两条轨迹的 session id 且只有研究题）；research 抽样优先回放里出手的题；文字删改被 V0 拒，`rewrite: true` 的单组件包可筛；`heuristic_terms` 的检出与软信号；`replay_max_false_trigger` 0.2 下 v1.7 的噪声 hook 仍被拒。脚本化 R 的模式 `partial`（修 1 退 1 的全局 hook）与 `merge`（条件化后零退步）在假上游上端到端。

- v1.2 新增：一轮一个包（声明的两向一致、某组件不过 V2 退回一次、仍不过则无候选）；聚焦清单不含名额且 `require_code` 只写台账不拒绝；留出筛选在全部留出题上跑、三条接受规则各一例失败、`best_so_far` 的容差；接受装入工作线（reason `screen`）、拒绝不动；活动末 gate 通过才有发布线；留出题号不出现在 R 的任何输入；调 aux 的 hook 计量、封顶、跳过 V2b；脚本化 R 在假上游上跑 6 轮：接受累积、耐心停、基线导入后第 0 轮不付测量。

- 证据：非 train 的 session 抛 `TraceAccessError`；特征与足迹确定；dev 直方图不带题号。
- 诊断：脚本化 aux 给出案例；词表外归 other；增项需 ≥ 2 案例；缓存命中不调模型；memory 条目分类。
- 聚焦（v1.2）：簇按题数、kind 按分数排序；`require_code` 只在有可机械化簇且 K 允许代码时为真；L5 只对三类簇建议；L6 不可用时记台账；`stalled` 按连续无接受的 focused 轮数。
- 会话：上限各一例；`run_check` 返回与 `run_ladder` 一致；声明不全被拒后再 finish；修订只改文件。
- 阶梯：V0 每条规则一例；V1 抓确定性崩溃；V2a 在 fake_upstream 上一个脚本通过、一个没调用工具被拒；V2b 触发计数与误触发率；V2c 可达。
- 筛选：条件记功（变异：把条件去掉，结果变）；thin；pick_best 的并列规则；核价不过不付费。
- 记功与反膨胀：零功组件 N 轮后产生删除候选。
- 台账与策略：投影无 dev 题号；记分板；剧本只含引得上的教训；导出导入往返。
- 付费安全（EVALUATE §9 等到本阶段的几条）：研究步计量（会话每回合、诊断每次调用、剧本）、无法核价的候选不开始、中断的一轮续跑不重付、验证界核价。
- 装入的 operation_id 与 Execution 的操作 id 不冲突。
- 端到端（`fake_upstream` 的 `rsi` 域，脚本化 R；第六道 train 题 `rsi.notify_ticket` 让留出一对与研究部分各有一道失败题）：一轮产出一个 tool 包过 V0～V2a、V3 在留出题上记条件修复并接受装入工作线、记功；一个 hook 包过 V2b；tool + hook 的两组件包各过各的档；有可机械化簇而无代码候选时台账记 `no_code_candidate`；memory 快照作为固定候选进 V3。

## 15 逐题采用（v2.0，`repeat.py`，DESIGN §6.23）

```python
def digest(trace, session) -> dict             # 无标签：steps、calls（工具、方法、路由、错误标记与摘录）、errors、hooks（站点、名字、动作、说的话）、writes 按路由、skills_read、termination、final_text
def footprint_of(trace, session) -> dict       # {hooks, tools, skills}：出手的 hook、模型调过的工具、读过的技能
def acted_components(conditions, foot) -> list[str]
def contrast_text(reference, probe, *, task, dir_name, acted, probes_left) -> str   # try_task 在这里的答复：两次执行并排、无评分
class Library:                                 # <root>/library/：entries.json、INDEX.md、<id>/EXPERIENCE.md + 改动的文件
    def add(self, *, task, dir_name, mechanism, files_ref, changed, experience, why, scope) -> dict
    def inputs(self) -> dict                   # inputs/library/INDEX.md 与最新 LIBRARY_ENTRIES_SHOWN 条的文件
def verify_context(activity, settings, *, package) -> VerifyContext   # 只给 V0 / V1：study_tasks=()、cause_ids=()
def adopt(activity, *, task_id, reference, base_release, run_arm, account, plan, library, out, outcome=None, feedback="attempts") -> dict   # outcome "not accepted"：task.json 多一行；feedback attempts：try 的答复末尾加"这次被接受 / 没被接受"，被接受的候选采用、不问 composer
    # -> {choice: "base" | dir, why, package_ref, files_ref, probes, candidates, entry, session, usd, verdicts}
```

- 会话：`prompts/repeat.md`；输入 `task.json`（请求 + 参考执行的 digest）、`package_api.md`、`library/…`；`readable` 只有本题的参考会话；`require_causes=False`（声明的 `cause_ids` / `predicted_affected` 可空）；`try_task` 绑定本题：build、V1，`run_arm(split=plan.split, role="probe", candidate="probe:<dir>", package=候选)` 一次，答 `contrast_text`；每个目录一次，总数 ≤ `plan.max_probes`。
- finish 后：每个声明过 V0+V1（`rungs=("V0","V1")`），R 没 try 过的由宿主 probe 一次；composer（`prompts/compose.md`，一次 aux 调用，输入请求、参考 digest、每个候选的机制 / 组件 / 出手 / digest）答 `{"choice", "why"}`；采用则 reflector（`prompts/reflect.md`）答逐组件裁决与至多一条教训（cue / lesson / evidence / confidence 齐全才入库）。
- 不做：接受规则、留出筛选、两次确认、记功、估值——这条回路的正确性只由第 2～E 次的计分在报告里量。
- v2.1（阶段 4p，DESIGN §6.24）：**退回项与工具**。`feedback="items"`：`adopt(..., rejected=[...])` 把第 1 次的退回项写进 `task.json` 的 `rejected_items`，每次 probe 没被接受时 try 的答复附上那次的退回项（宿主读该 probe 的 grading，经适配器 `rejected_items` 去掉期望值）；提示词 `FEEDBACK_ITEMS` 说"退回项告诉你哪一项、不告诉你该是什么"。`repeat.md` 加"该成工具的步骤"一段（算出来、匹配出来、汇总出来、按规则判出来的值 → 工具；输入是读到的原值、输出带来源与公式；配技能或 hold 让 agent 调用；probe 的对比里看工具调没调、输入输出）；try 的答复在 `acted` 里列 `tool:<name>` 并附该工具每次调用的参数与输出摘录。try 的规则：同一目录文件改过可再 try（`probes[dir]["files_hash"]` 不同），没改过的拒绝；finish 时文件在最后一次 try 之后改过的目录，宿主再 probe 一次（有 try 剩余时），没剩的按最后一次 try 的结果算。`[repeat] tasks` 点名题目。第二刀（DESIGN §6.24）：`adopt(..., sources=…)`——`task.json` 多一项 `sources`（宿主经适配器 `world_sources(initial_state)` 给出服务名与条数，`service_of_route` 把运行的调用按路由归到服务：`{service: {holds, read, written}}`），try 的答复附同一行；`CodingSession(tool_notes={name: description})` 覆盖工具说明（重复流里 try_task 说"本题、{max_probes} 次、改过可再 try"，read_trajectory 说参考运行，finish 说至多两个包）；`repeat.md` 重写成"你知道什么 / 往哪看 / 建什么 / 怎么做 / 限制"五段；第三刀："怎么做"加第 5 步——被接受的派生值补工具（package-2 = 被接受的包 + 工具 + 送 agent 去用它的 hold，再 try 一次；声明时放最后，采用取最后一个被接受的候选，代码不变）。第六刀：被接受却没交的候选（会话被截断 / 无 finish）由宿主从 `components(package, tried)` 合成声明（hook 的 site 取自 manifest；mechanism "undeclared: …"；`read_declaration(require_causes=False)`），对 probe 过的文件跑 V0 + V1，过了进候选。脚本化 R 的模式 `repeat_cut`（try 被接受后两次回复 `stop = "max_tokens"` → 会话结束 → 宿主声明并采用）、`repeat_harden`（hook 被接受后在 package-2 写 hook + 工具、try、两个都声明 → 采用 package-2、文件里两者都有、再遇足迹有工具）、`repeat_tool`：读 task.json 与库索引，写工具（`post_channel_message`）与 manifest，try 一次（actor 看到工具就用它）、finish。测试：工具被采用、第 2 次足迹里有 `tool:post_channel_message`、库条目带 `tools/` 文件；items 模式下 task.json 有退回项且不含期望文本、probe 的答复附退回项；点名计划只跑点名的题；改过的目录再 try 不被拒、finish 时宿主再 probe。

## 14 开发顺序

`settings.py` → `evidence.py` → `ledger.py`、`archive.py` → `diagnose.py` → `allocate.py` → `verify/static.py`、`unit.py` → `session.py` → `verify/toolcheck.py`、`replay.py`、`reach.py` → `screen.py` → `credit.py` → `policy.py` → `round.py` → activity 的 `round` 阶段、cycle 与活动末的 `gate` → 测试。阶段 1 留下的三项（种子传 effort、回合用完记 budget、速率限制落事件）先做。
