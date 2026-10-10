# improver：证据、诊断、分层、编码会话、验收阶梯、筛选、档案、记功、台账、策略

> 阶段 3 的设计文档（接口随阶段 1 一起定；2026-10-09 开发前按阶段 1、2 的代码定稿）。包 `ouroloop/improver/`。对应 [DESIGN](../DESIGN.md) §6。来源：tag `archive-2026-10-09` 上的 `research/{session,drafts,experiment,diagnosis}.py`（拆分重写）、`evolve/experience.py`；`runtime/trace.py`（阶段 2 已搬入）。

## 1 职责与不做

**做：** 一轮改进：量当前 W 的 train；从 train 证据诊断出原因簇；按估值给簇指派组件类型；R 在自己的编码会话里实现候选；逐档验收；V3 筛选；V4 交 evaluate 的 gate；装入与存档；条件记功；维护估值与剧本。固定代码的候选（memory 快照、memory 升级出的技能、删除候选）也走同一条阶梯。

**不做：** 不写 gate 的统计（evaluate）；不写 memory（只读它的快照事件与升级输入）；不读 dev/test 的内容（dev 只有失败断言类型的直方图）；R 不跑 shell、不上网、不碰宿主文件。

## 2 Settings（K 的 `improver` 节）

```python
@dataclass(frozen=True)
class Settings:
    k: int = 3                                   # 每轮 R 的候选上限（固定代码的候选另算，≤ fixed_per_round）
    fixed_per_round: int = 2
    session_max_turns: int = 40; session_max_edits: int = 32
    session_max_read_bytes: int = 65536; session_max_read_total: int = 196608   # 一次读与整段会话的读（read_trajectory 与 read_file 合计）
    session_compact_tokens: int = 120000          # 上一回合的输入过此数，较早的工具输出与写入文本换成一行说明
    diagnose_max_cases: int = 60; diagnose_successes: int = 6
    causes: tuple = CAUSES; mechanizable: tuple = MECHANIZABLE
    priors: dict = {"tool": (0.3, 0.3), "hook": (0.3, 0.3), "skill": (0.08, 0.1), "prompt": (0.08, 0.1),
                    "agent": (0.1, 0.3), "config": (0.1, 0.2), "vendor": (0.05, 0.3), "model": (0.0, 0.5)}   # (均值, 不确定度)
    explore_slots: int = 1; l5_slots: int = 1; code_guarantee: bool = True; stall_rounds: int = 3
    toolcheck_min_pass: int = 2; replay_min_triggers: int = 2; reach_min_tasks: int = 2; reach_min_score: float = 0.2
    screen_passing_sample: int = 6; screen_min_net_flips: int = 0; screen_min_fixes: int = 1
    screen_max_sample_regressions: int = 1; thin_screen_tasks: int = 3
    removal_zero_credit_rounds: int = 3
    holdout_share: float = 0.4                  # 留出的 train 题：R 看不到，V3 只在这里筛
    round_share: float = 0.25                    # 每轮研究份额占根的比例上限（一轮的账户就是这么大）
```

先验按 DESIGN §6.4：文字层按三次 final 的读数（均值低、不确定度小），代码层按外部读数给高不确定度，所以探索名额先落在代码层。合并规则：数量上限 cap，筛选与通过门槛 floor（`holdout_share` 也是 floor：只能留出更多），`causes`、`mechanizable` superset（只能多），`priors` fixed。

## 3 一轮（`round.py`）

```python
@dataclass(frozen=True)
class RoundResult: round_id: str; status: str; causes: int; assignments: int; candidates: int; ladder: dict; screened: str | None; gate: dict | None; installed: str | None; stop: str | None; spend: dict
def run_round(activity, index: int, *, out) -> RoundResult
```

`round_id = f"{scope_id}/r{index}"`。状态与产物写在 `rounds/r{index}/round.json`：每步一个键，值是该步的输出（多为对象 ref），已有就复用，不重做（这一轮不用 `Execution`：它的状态机是 gate 的实验状态）。步骤：

`measure → evidence → diagnosed → allocated → implemented → verified → screened → gated → installed | rejected → credited`

- **研究部分与留出部分**：train 题按 `holdout_share` 分两部分，整个活动固定（按冻结的 train 顺序与 K 的 `gate.order_seed` 定）。诊断覆盖所有失败题；R 只看研究部分：它的案例、成功轨迹、可读的 session、`predicted_affected`、toolcheck 与回放都在研究部分；V3 只在留出部分筛选。这样筛选量的是候选能不能修好它没见过的同类题（finance-v2 的两轮里，筛选在 R 研究过的题上净 +1～+3，dev 上增益都是 0）。`holdout_share` 为 0 时不留出，筛选回到声称的题（测试用）。
- **measure**：当前 W（活跃 release 的包）在每道 train 题上要有一次有效读数；第 0 轮用基线的读数，装入之后缺的题现量（run 的 role `measure`，候选名 `train:<release 短 ref>:r<i>`：每轮一个 run，一轮被预算拒掉的尝试下一轮重读，不会被当成记录交回；读数按包汇总，记功与下一轮共用）。
- **stop**：没有失败的 train 题 → `nothing_to_fix`；研究份额不够付下一档 → `budget`；会话没有候选、没有固定候选 → `no_candidate`。停下也写台账。停下的一轮不再续跑（它的份额已用完），`cycle` 再跑时从下一轮接着。
- **续跑**：会话一结束，它的声明与文件就记进 `round.json`（`session_result`），之后断了只重验、不重付会话；这一轮自己的装入移动了活跃 release（`install:<round_id>` 的收据是 installed）而 gated 一步还没保存时照常续跑（gate 从记录重放、装入重放是空操作）；台账行在一轮里按内容去重，续跑写同一行拿到记录里那一行。
- 一轮的花费记在 `<scope>:round-<i>` 账户（上限 `round_share × [budget] usd`），下分 `measure`、`diagnose`、`session`、`screen`、`gate`、`policy` 子账户，报告按它们单列。

`cycle --until round-K` 依次跑第 0～K 轮；连续 `schedule.patience` 轮没有装入就停。

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
def dev_histogram(activity, release) -> dict                          # 失败断言类型 → 计数；不带题号、参数
```

`footprint`：tool 在 gateway 的回复里被调用过；hook 在 `hook` 行里触发过（action ≠ none）；skill 在 `skill_read` 行里读到过；memory 条目在 `library_retrieval` 行里给过；prompt、agent、config、vendor 是全部题。

## 5 诊断（`diagnose.py`）

```python
CAUSES = ("excluded_mentioned", "missed_action", "wrong_target", "missing_recipient", "unread_value", "arith_mismatch",
          "real_date", "wrong_location", "premature_stop", "tool_error_loop", "context_overflow", "finish_protocol", "other")
MECHANIZABLE = ("excluded_mentioned", "arith_mismatch", "real_date", "wrong_location", "tool_error_loop")
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

## 6 分层（`allocate.py`）

```python
@dataclass
class Estimate: line; cause; kind; fixes: float; cost: float; n: int; mean: float; unc: float
class Estimates:                    # ledger/estimates.json；跨活动可导入
    def get(self, line, cause, kind) -> Estimate     # 没有就用 priors
    def update(self, line, cause, kind, *, fixes, cost) -> None
    def stalled(self, line, kinds, rounds) -> bool
@dataclass(frozen=True)
class Assignment(Record): cause_id; kind; slots: int; exploration: bool; reason: str
def allocate(causes, estimates, *, settings, line, kinds_allowed, history: Ledger, train_available=False) -> list[Assignment]
```

0. 有留出时，只分配研究部分至少 max(`toolcheck_min_pass`, `replay_min_triggers`) 道题、留出部分至少 1 道题的 cause：R 要有 V2a、V2b 需要的案例，V3 要有留出题可筛（finance 每族一道题，0.4 留出时 5 道题的簇可能一道留出题都没有，2 道题的簇研究部分只剩 1 道）。
1. 簇可用的 kind：可机械化 → tool、hook、skill、prompt；否则 → skill、prompt；cause ∈ `LOOP_CAUSES` 再加 agent、config、vendor（L5）。都要在 K 的 `harness.kinds` 里。memory 不派给 R：R 不能写 memory，库的快照是固定候选（§10）。
2. 分数 = `mean + unc`（上界）；簇按题数从大到小，前 `k` 个簇各给分数最高的 kind（同分取靠前的，代码类在前）。
3. 探索：`explore_slots` 个名额换成该 cause 上 `n == 0` 的 kind，代码类优先。
4. 没用上的名额：会话结束时，指派里有 (cause, kind) 一个候选都没声明的，台账记 `unused`，估值按这个名额的观测记一次（修复 0），所以探索不会一直落在 R 不去做的那一处。
5. 代码保证：有可机械化簇而指派里没有 tool/hook → 最后一个名额换成最大可机械化簇的代码类；上一轮台账记了这个 cause 的 `no_code_candidate` → 这一轮它的代码名额 `slots = 2`。
6. L5 每轮 ≤ `l5_slots`，只给 `LOOP_CAUSES` 的簇。
7. L6（model）：`stalled(line, 代码与文字类, stall_rounds)` 且 `train_available`；阶段 3 训练链没接上，`train_available` 恒为假，台账记 `l6_unavailable`。

## 7 编码会话（`session.py`）

```python
@dataclass(frozen=True)
class Declaration(Record): dir; cause_id; kind; site; predicted_affected: tuple; trigger; differs_from: tuple; scripts: dict   # scripts: task_id -> toolcheck 脚本
class WorkTree:                      # worker 的 work op：init / list / read / write / edit / delete / export
class CodingSession:
    def __init__(self, *, researcher, work: WorkTree, trace: TraceStore, check: Callable[[str, dict], str], tests: Callable[[str], str], settings, k, system, inputs: dict, readable: set[str])
    def start(self) -> SessionResult
    def revise(self, text: str) -> SessionResult        # 验收退回的修订，续写同一会话，同一套上限
    def transcript(self) -> dict;  record(self) -> dict
@dataclass
class SessionResult: declarations: list[Declaration]; ended: str
TOOLS = ("list_files", "read_file", "write_file", "edit_file", "delete_file", "read_trajectory", "search_trace", "run_tests", "run_check", "finish")
```

- 模型循环在宿主（研究模型经计量 provider，每回合先预留后结算），文件树在 worker（`/work`：`package/` 只读、`inputs/` 只读、`candidates/<dir>/` 可写；第一次写某个 dir 时它从当前包复制）。
- `inputs/`：`assignments.json`（簇、kind、名额）、`causes.json`（只有研究部分的案例：题号、cause、说明、证据 ref、失败断言类型；留出部分只给题数）、`successes.json`（成功轨迹的 session）、`ledger.json`（台账的 train 投影，留出题的题号换成计数）、`scoreboard.json`（每类候选在留出部分的修复、退步与净值）、`playbook.md`、`histogram.json`（dev 失败类型与计数）、`base_tools.json`、`package_api.md`（包侧契约：ctx、hook 站点的事件与返回、工具与 agent 入口、toolcheck 脚本、manifest 的形状）。
- `read_trajectory(session, offset?, limit?)`：重建的轨迹（JSON），只限 `readable`（本轮的 train session）；按 `session_max_read_bytes` / `session_max_read_total` 截断；`read_file` 同样计入这两个上限（会话的转写每回合整段重发）。下一次调用要发的上下文（按文本估算，3 字一 token）超过 `session_compact_tokens` 时，除最近 4 条（不够就 2 条、1 条）之外的工具输出与写入文本换成“已省略，重读可见”的一行，直到降到阈值的 0.6；被省略的读取把字节还给读取总量，重读不会被拒；两次压缩之间每次调用都是上一次的延伸（缓存）。回复被输出上限截断时告诉 R 写小一点；连续两次截断结束会话。`search_trace(query)`：TraceStore 的 FTS，只限 train。
- `run_check(dir, declaration)`：对该目录当前文件跑 V0～V2，返回逐档结果的文本；`run_tests(dir)`：只跑包测试。都不付费。
- `finish(candidates)`：每个是一份声明。声明不全或 dir 没写过 → 工具错误，R 可以在上限内再 finish。`finish([])` 直接结束会话（R 认为没有可做的）。超出 `session_max_turns` / `session_max_edits` 即结束，已声明的候选照常验收；没有声明则本轮没有 R 的候选。
- 验收：每个候选跑一次阶梯；没过的把 Verdict 文本退回会话修订一次（`revise`），修订只改文件、不改声明（声明新目录是工具错误）；仍不过 → invalid。
- 提供方失败（订阅限额、超时用尽）或 Ctrl-C：会话的消息、计数与候选目录的文件存进 `round.json` 的 `session_checkpoint`，异常照常抛出；同一轮再跑时把候选目录原样恢复（先删包里有而它没有的文件，再按增量从小到大写；任何一步被拒就报错、检查点留着），从那一回合接着，已付的回合不再付。worker 崩溃或进程被杀时没有检查点，会话重付。会话一结束，候选就记进 `session_result`。
- 诊断与剧本的回复被拒、被截断或读不出 JSON：不算答案，不进缓存（诊断记 other 并注明，下一轮再问）；剧本保留原有的教训。
- 提示文本在 `improver/prompts/{diagnose,propose,playbook}.md`，哈希进 `ImproverPolicy`。

## 8 验收阶梯（`verify/`）

```python
@dataclass(frozen=True)
class Verdict(Record): candidate_ref: str | None; dir; steps: dict     # {"V0": {"passed", "detail"}, ...}
    def passed(self, step=None) -> bool                                 # None：全部
def ladder_for(kind) -> tuple    # tool: V0,V1,V2a；hook: V0,V1,V2b；skill: V0,V2c；prompt: V0；agent/vendor/config: V0,V1；memory/removal: V0
def run_ladder(files, decl, *, ctx: VerifyContext) -> Verdict
# VerifyContext: parent、seed、core、limits、objects、worker、suite、evidence、forbidden 字面量、train 题文本、embed
```

- **V0 静态**（`verify/static.py`）：`check_shape`（含上限、manifest、vendor 保护文件）；diff 的 kind ⊆ K 的 `harness.kinds`；hook 站点 ⊆ K 的 `sites`；`memory/` 未改；相对种子改动行的字面量；改动的 `.py` 不新增 import `socket`、`subprocess`、`ctypes`、`pickle`、`multiprocessing` 等，不新增调用 `os.system`/`os.popen`/`os.exec*`/`os.spawn*`/`eval`/`exec`（按名字匹配，`import os as o`、`from os import system` 解析成原名）；声明与 diff 一致（声明的 kind 在 diff 的 kinds 里；tool 的 site 是包里新增或改动的工具名，hook 的 site 是改动的 hook 所在站点，skill 的 site 是技能名）；`predicted_affected` 都是研究部分的 train 题。
- **V1 单元**（`verify/unit.py`）：`runtime.worker.check_package`（probe、smoke、包测试），夹具是一道 train 题的新 episode。
- **V2a toolcheck**（`verify/toolcheck.py`）：声明里每道声称的、当前 W 失败的研究部分 train 题的脚本（别的题的脚本不算通过）：该题新 episode → worker 装候选 → `check` op（世界经可写 broker）→ `episode.score()`；要求脚本调用了 site 指的工具，通过 ≥ `toolcheck_min_pass` 道。只回传 `{task_id: {passed, failed_kinds}}`，不回传断言参数。
- **V2b 回放**（`verify/replay.py`）：从 transcript 重建站点事件，形状与 stirrup 种子交给 hook 的一致——`before_run`（任务、system）、`before_model`（每次 act 调用的 messages 与工具）、`before_tool` / `after_tool`（每个非 finish 的工具调用与它的结果；被阻止或跳过的没有 after_tool）、`after_turn`（一个回合的结果，`call_id`、输出截到 4000 字）、`on_finish`（reason 为 finish 或 implicit，final_text 是 finish 的理由，pushbacks 是此前的退回次数；连续纯文字回复变成的隐式 finish 也在）——按 manifest 顺序，同一站点上排在候选前面、在记录里阻止了这一次调用（before_tool 的 block）或退回了这一次 finish（on_finish 的 pushback）的 hook 先答，候选不会被问到，这个事件不给它；逐个经 `hook` op 调候选在该站点的 hook；返回非 none 的 action 算触发；声称的失败题（研究部分）上触发 ≥ `replay_min_triggers` 道；报告研究部分通过样本上的误触发率。
- **V2c 可达**（`verify/reach.py`）：技能的名字与描述对 train 题文本的相似度 ≥ `reach_min_score` 的题，除去 `predicted_affected`，≥ `reach_min_tasks` 道。向量：确定的词袋向量（`lexical_embed`），`reach_min_score` 按它校准；不用 sidecar 的 bge-small（它给无关文本 0.5 以上，0.2 会放过一切描述）。

## 9 筛选 V3（`screen.py`）与 gate V4

```python
@dataclass(frozen=True)
class ScreenResult(Record): candidate: str; package_ref; tasks: tuple; failing: tuple; sample: tuple; fixed: tuple; regressed: tuple; conditioned: dict; net_flips: int; cost: float | None; thin: bool; outcome: str
def passing_sample(reading, *, order, size, seed, round_index) -> list[str]
def screen(candidate, *, activity, base_release, reading, tasks, evidence, account) -> ScreenResult
def pick_best(results, settings) -> ScreenResult | None
def gate(activity, candidate_package_ref, *, round_id, account) -> dict           # V4：evaluate_task + 装入
```

- 失败集 = 留出部分里当前 W 仍失败、诊断归到候选所属 cause 的题（固定代码的候选：留出部分里所有失败题）；样本 = `screen_passing_sample` 道留出部分里当前通过的题（按 `order_seed` 与轮次抽，重跑同一轮抽到同样的）。失败集为空 → 不筛（not_run）；少于 `thin_screen_tasks` 道记 `thin`。memory 的库从全部 train 题学，它的快照在留出部分上的筛选不是迁移检验，gate 才是。`holdout_share` 为 0 时：失败集 = (声称的题 ∪ 簇的题) ∩ 当前 W 仍失败的 train 题，样本取自所有通过的 train 题。
- 运行：`run_arm(release=W_base, package=候选, split="train", candidate=f"r{i}:{dir}", role="screen")`。
- 条件记功：修复只在 `footprint` 为真的题上算（§4）；退步在样本上算，不论足迹。净翻转 = 条件修复 − 退步。资格：净翻转 ≥ `screen_min_net_flips`、条件修复 ≥ `screen_min_fixes`、退步 ≤ `screen_max_sample_regressions`；最优者（净翻转大，再成本低，再声明顺序）进 V4。
- 付诸任何研究调用之前（诊断之前）先核一个下限：gate、一次小筛选（通过样本加 `thin_screen_tasks` 道失败题）与将要付的研究（每个案例一次诊断、每回合一次会话，按活动里研究调用的均价；还没有记录时不算）的价，加下面的余量，放不进本轮的余额 → 停在 `budget`，不付诊断与会话；没有成本读数 → 停在 `unpriced`。
- 付费之前逐个核价：筛选按 R 的候选（声明顺序）、固定代码的候选、库快照（它声称所有失败题）最后的顺序，一个个加进来，只要已加的筛选、它自己与 gate 放得进余额就收下；放不下的记台账 `screened`（outcome not_run，原因 budget），一个都放不下才停在 `budget`。价＝还没记录的筛选尝试数（续跑时已付的不再算）× W_base 的 train 每次尝试成本，加 gate 的界（dev 题数 ×（1 + 确认次数）+ 对照池的缺口，`evaluate.gate.shortfall`）× W_base 的 dev 每次尝试成本，乘以 1.15 再加两次调用的上界，要放得进本轮账户的余额（每次调用都按上界预留，所以一轮永远花不到最后一个上界；两个上界让最后一臂不至于串行）；没有成本读数就无法核价，不开始。gate 开跑之前按胜者的筛选成本再核一次 gate 的价，放不下就停在 `budget`，不付一个跑不完的 gate；也再读一次 claude CLI 的版本，与活动记下的不同就停在 `cli`。记功一步对新 W 的 train 重读不在筛选的价里（只在装入后才付）：预算拒掉它时装入照常，这次装入不记功（台账 credited 写明 not credited），下一轮照常量新 W。
- V4：冻结的 gate 模板（`gate_template` 事件）填上候选包、对照包、父 release，经 `evaluate.gate.evaluate_task`；对照池是 W_base 在 dev 上同条件的有效读数（role `base` 或 `control`；基线的读数在里面；装入之后新 W 在 dev 上的读数是它那次 gate 的被测臂，不进池，所以它的第一次 gate 要补满对照），补缺口的 run 是这一次 gate 的（候选名 `control:<W 短 ref>:<round_id>`，一次被拒的补读下次 gate 重读）；被测臂 `run_arm(package=候选, split="dev", role="subject")`。收据登记后若 accepted：`Releases.candidate` → `install(..., operation_id=f"install:{round_id}")`（与 Execution 的操作 id 不同名，避免读到别人的记录）。

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
STAGES = ("measured", "diagnosed", "allocated", "declared", "verified", "screened", "gated", "installed", "rejected",
          "credited", "removal_proposed", "no_code_candidate", "unused", "vocabulary_added", "l6_unavailable", "stopped", "playbook")
class Ledger:    # ledger/experience.jsonl
    def row(self, stage, **fields) -> dict
    def rows(self, *, stage=None, round_id=None) -> list[dict]
    def projection(self, *, line, hide=()) -> list[dict]   # 给 R：只有 train 题号（hide 里的留出题换成计数）；dev 只有计数
    def scoreboard(self, line) -> dict              # 每类候选在筛选上的修复、退步、净值（计数）

# policy.py
@dataclass(frozen=True)
class ImproverPolicy(Record): prompts_ref; estimates_ref; playbook_ref; settings_ref
def update_playbook(ledger, playbook, *, researcher, line) -> str      # 轮末一次 aux 调用；每条教训要引用台账行 id，引不上的丢掉
def export_policy(activity) -> str;  def import_policy(activity, policy_ref) -> None
```

- 记功（装入后）：新 W 在 train 上量一遍（与下一轮的 measure 同一次）；对改动的每个组件，在 `footprint` 为真的题上记 `delta = r_新 − r_旧`；估值 `(line, cause, kind)` 用该 cause 的题上、声明的 kind 的组件有足迹处的净修复与本轮花费更新。估值的每个观测按 `round:dir` 记：筛选时记一次（条件净翻转 / 失败集），装入后用记功的读数替换它，续跑写同一键不重复计。
- 反膨胀：一个组件连续 `removal_zero_credit_rounds` 个已记功的轮次条件记功为零 → 删除候选（固定代码生成），下一轮作为固定候选进 V3。
- R 的策略（估值与剧本）在活动内随台账更新；release 里的 `improver_ref` 在一个活动内保持 R0，活动结束时 `export_policy` 产出下一个活动的 R1（DESIGN §6.9）。
- 升级的技能装入后，记功一步调 `MemoryService.supersede`：流的台账里条目 `superseded`，live store 退役它，live 库文件里没有它（MEMORY §8）。memory 条目的分类按（条目、请求、模型）缓存在 `rounds/notes.jsonl`。
- 固定候选的目录名（`memory-snapshot`、`promoted-`、`remove-` 开头）R 不能用。
- 固定候选（每轮 ≤ `fixed_per_round`，先快照、再升级技能、再删除）：library 阶段的快照候选（`library_snapshot` 事件，outcome candidate，还没进过阶梯的）走 V0 → V3；memory 升级出的技能候选走 V0、V2c → V3；删除候选走 V0 → V3。它们和 R 的候选一起筛选，最优者进 V4。

## 11 不变量

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

- 证据：非 train 的 session 抛 `TraceAccessError`；特征与足迹确定；dev 直方图不带题号。
- 诊断：脚本化 aux 给出案例；词表外归 other；增项需 ≥ 2 案例；缓存命中不调模型；memory 条目分类。
- 分层：上界排序；探索名额落在 n=0 的代码类；代码保证触发；`no_code_candidate` 后名额加倍；L5 只在三类簇；L6 不可用时记台账。
- 会话：上限各一例；`run_check` 返回与 `run_ladder` 一致；声明不全被拒后再 finish；修订只改文件。
- 阶梯：V0 每条规则一例；V1 抓确定性崩溃；V2a 在 fake_upstream 上一个脚本通过、一个没调用工具被拒；V2b 触发计数与误触发率；V2c 可达。
- 筛选：条件记功（变异：把条件去掉，结果变）；thin；pick_best 的并列规则；核价不过不付费。
- 记功与反膨胀：零功组件 N 轮后产生删除候选。
- 台账与策略：投影无 dev 题号；记分板；剧本只含引得上的教训；导出导入往返。
- 付费安全（EVALUATE §9 等到本阶段的几条）：研究步计量（会话每回合、诊断每次调用、剧本）、无法核价的候选不开始、中断的一轮续跑不重付、验证界核价。
- 装入的 operation_id 与 Execution 的操作 id 不冲突。
- 端到端（`fake_upstream` 的 `rsi` 域，脚本化 R）：一轮产出一个 tool 候选过 V0～V2a、V3 记条件修复、V4 装入、记功；一个 hook 候选过 V2b；有可机械化簇而无代码候选时台账记 `no_code_candidate`；memory 快照作为固定候选进 V3。

## 14 开发顺序

`settings.py` → `evidence.py` → `ledger.py`、`archive.py` → `diagnose.py` → `allocate.py` → `verify/static.py`、`unit.py` → `session.py` → `verify/toolcheck.py`、`replay.py`、`reach.py` → `screen.py`（含 V4）→ `credit.py` → `policy.py` → `round.py` → activity 的 `round` 阶段与 cycle → 测试。阶段 1 留下的三项（种子传 effort、回合用完记 budget、速率限制落事件）先做。
