# Announcement draft (2026-10-26) — not published

The public note for the open-source release of reharness, written on 2026-10-26. The user decides whether and when it goes out. Every number is on record (docs/RESULTS.md, docs/JOURNAL.md). The Chinese version follows the English one. The architecture figures are the README's two (`docs/assets/architecture.svg`, `docs/assets/one-improvement.svg`); attach them wherever the note is posted.

---

## reharness: A self-improving agent rewrites its own harness, not its model, and proves each change live

Most real work repeats. A finance team runs the same reconciliation, the same invoice batch, the same tax estimate again and again with other numbers. An agent that fails such a request once should not fail it the same way tomorrow — and whatever fixes it should be checked by evidence, not chosen by a person.

reharness is an open-source loop that does this for a business agent on Zapier's AutomationBench (47 simulated SaaS tools, scored on the world state the agent leaves behind). The agent's harness — its tools, hooks, skills and prompts — is a versioned package. When a request comes back sent back, an improver (the same model, Claude Opus 5.5) reads the agent's own run of it, builds a change to the package, tries it on the same request for real, and only a try that comes back accepted is adopted — for that request. Everything else is frozen: the scorer, the splits, the budget, and what the improver may see: at most "accepted or sent back" per attempt, and which message or row was rejected, never the expected value.

### What is different

- **Per-task adoption by an accepted real try.** No judge model, no held-out screen between the improver and the agent: the improver's candidate runs the request for real, and the bit that comes back decides. Accepted tasks are never touched, so learning never breaks a task that was passing.
- **A typed, bounded learning signal.** What the improver is told is a field of the sealed plan: one bit per attempt, or the bit plus the rejected checks' kinds and objects (which message, which row — never a value), plus an inventory of the world's data sources as counts (which the agent read, which only the improver's own hook read, which were never opened). The grading rows never leave the host.
- **The improver learns habits, not answers.** Given that signal, it built the same two mechanisms in independent sessions with none of the words in its prompt: a hold that stops the first write until every unread source is opened, and a tool that derives a figure from the inputs read. Every calculation tool in an adopted package was written by the improver; the seed has none.
- **A frozen core with three kinds of guard and nothing else.** Money: every model call reserves its worst case before it is made and settles after; a breach stops the scope. Leakage: splits by task family, plans sealed and committed before the first test attempt. Correctness: a pinned worker image and code hashes, failures typed so only infrastructure is retried. Package code runs in an isolated Docker worker with no network.

### How it works

<p align="center"><img src="../assets/architecture.svg" alt="Components and architecture: the Frozen Core K over the Harness Package W, the Isolated Worker and the Revision Session R" width="100%"></p>

<p align="center"><img src="../assets/one-improvement.svg" alt="How one improvement happens: every run through the Host-Side Scorer, the first pass accepted or returned, one Revision Session, an admitted candidate under Live Probe, Per-Task Adoption by the host, later encounters on W_t" width="100%"></p>

A sealed plan names the tasks and how many times each is met (three). Encounter 1 runs the base harness; an accepted request is finished at $0. A sent-back request gives the improver one coding session bound to that task: it reads the run's digest (calls, hooks, writes, the final text — no score), the inventory of the world's sources, the rejected items the plan allows, and the library of earlier adoptions; it edits a copy of the package; the host runs the free static and unit checks and then `try_task` executes the same request once for real. The answer is the new run beside the first, plus the bit. At most four tries; the last candidate whose try was accepted is adopted and runs on encounters 2 and 3. The report reads the pass rate at encounter 1 against encounter 3, paired by task family with a bootstrap interval.

### What it achieves

- On the 50 finance test tasks, with one bit of feedback per attempt and the plan sealed before the first test attempt: first pass 64%, steady state 68%. 18 tasks were sent back, 3 were corrected and stayed corrected, and no passing task was broken by learning ($184). The paired difference, +4.0 points, is inside its interval.
- With the rejected items as the signal, two train tasks whose correct figure hung on a correction posted in a chat channel went 0/2 → 2/2 ($11): the improver built the unread-sources hold on its own, the agent read the channel on its later encounters and derived 13,162.5 and 105,792.
- At effort max, one session built the whole mechanism and a task that never held at medium held on both later encounters, at five to seven times the cost ($61 for three tasks).
- What is left is documented, not hidden: ambiguities in the task's own documents that a thin signal cannot resolve, a world clock the agent is never told, and a benchmark noise floor of 10–17% of tasks flipping between two runs of the same agent.

**Why it is built this way.** Before this loop we spent eight activities and about $1,150 searching for packages that generalise across different workflows under a held-out screen with confirmed flips. Nothing beat the noise floor, and the reason was structural: the benchmark's 150 training tasks are 150 distinct workflows. The loop now follows how work actually repeats, and it states exactly what the improver was told.

**What you get.** The platform (typed harness packages, an isolated worker, a metered gateway, sealed plans and budget guards), the per-task loop and its prompts, the records of every measured run, and the design documents each run forced to change. MIT licensed. Reproduction starts from one config and the public benchmark: `reharness bench --config examples/automationbench/quickstart-repeat.toml --phase smoke --checks-only`.

---

## reharness：自我改进的 agent：改写自己的 harness 而不是模型，每个改动都真跑一次来证明

真实工作大多在重复。财务团队反复跑同一个对账、同一批发票、同一份税额估算，只是数字换了。一个 agent 这次把请求做砸了，明天不该再以同样的方式砸一次——而修它的改动应当由证据裁定，不是由人挑。

reharness 是一个开源回路，在 Zapier 的 AutomationBench（47 个模拟 SaaS 工具，按 agent 留下的世界状态评分）上为业务 agent 做这件事。agent 的 harness——工具、hook、技能、提示词——是一个版本化的包。一个请求被退回时，改进器（同一个模型，Claude Opus 5.5）读 agent 自己那次运行，给包写一个改动，在同一个请求上真跑一次，只有被接受的那次 try 才会被采用——只对这个请求。其他一切冻结：评分器、划分、预算，以及改进器能看到什么：至多每次尝试一个"被接受 / 被退回"的比特，和哪封邮件、哪一行被退回，从不带期望值。

### 不一样在哪

- **按题采用，凭真跑一次的接受比特。** 改进器和 agent 之间没有裁判模型、没有留出筛选：候选真跑这个请求，回来的那个比特决定。被接受的题从不动，所以学习从不弄坏本来能过的题。
- **类型化、有界的学习信号。** 改进器被告知什么是封存计划的一个字段：每次尝试一个比特，或者比特加上被退回检查的种类与对象（哪封邮件、哪一行——从不带值），再加一份世界数据源的清单（只有条数：agent 读过的、只有改进器自己的 hook 读过的、没打开过的）。评分行从不离开宿主。
- **改进器学的是习惯，不是答案。** 在这个信号下，它在互不相通的会话里建出了同样的两个机制，提示词里没有这些词：第一次写入前打开每个没读的源的 hold，以及按读到的输入算数字的工具。采用的包里每一个计算工具都是改进器写的，种子里一个都没有。
- **只有三类保护的冻结核心。** 钱：每次模型调用先按最坏情况预留、事后结算，超限即停。泄漏：按任务族划分，计划在第一次 test 尝试前封存并提交。正确性：pin 住的 worker 镜像与代码哈希，失败分类，只有基础设施重试。包代码跑在无网络的隔离 Docker worker 里。

### 怎么工作

（架构图同上。）一份封存的计划点名题目和每道题遇到的次数（三次）。第 1 次用 base harness 跑；被接受的请求到此结束，$0。被退回的请求给改进器一个绑定这道题的编码会话：它读运行的 digest（调用、hook、写入、最后的话——没有评分）、世界数据源的清单、计划允许的退回项、此前采用记录的库；改包的一个副本；宿主先跑免费的静态与单元检查，再由 `try_task` 在同一请求上真跑一次。答复是新运行和第一次的并排，加上那个比特。至多四次 try；最后一个被接受的候选被采用，在第 2、3 次运行。报告读第 1 次对第 3 次的通过率，按任务族配对、bootstrap 给区间。

### 做到了什么

- finance 的 50 道 test 题上，每次尝试一个比特、计划在第一次 test 尝试前封存：首次 64%，稳态 68%。18 道被退回，3 道修好并站住，没有一道通过题被学习弄坏（$184）。配对差 +4.0 分，在区间内。
- 用退回项作信号，两道正确数字取决于聊天频道里一条更正的 train 题从 0/2 到 2/2（$11）：改进器自己建出了"没读的源"的 hold，agent 在再遇时读了频道，算出 13,162.5 和 105,792。
- effort = max 下一个会话把整个机制建全，一道 medium 下从没站住的题在两次再遇里都对，代价五到七倍（三道题 $61）。
- 剩下的都写明了，没有藏：薄信号分不出的任务文档歧义、从不告诉 agent 的世界时钟、以及基准自身 10～17% 的噪声底。

**为什么这样设计。** 在这条回路之前，我们花了八个活动、约 $1,150 去找跨不同工作流泛化的包，用带确认翻转的留出筛选把关。没有东西高于噪声底，原因是结构性的：基准的 150 道训练题是 150 个不同的工作流。现在的回路按工作真正重复的方式来，并且把改进器被告知了什么写得清清楚楚。

**你能拿到什么。** 平台（类型化的 harness 包、隔离 worker、计量网关、封存计划与预算保护）、逐题回路与它的提示词、每次实测运行的记录，以及每次运行逼着改的设计文档。MIT 许可。复现从一个配置和公开基准开始：`reharness bench --config examples/automationbench/quickstart-repeat.toml --phase smoke --checks-only`。
