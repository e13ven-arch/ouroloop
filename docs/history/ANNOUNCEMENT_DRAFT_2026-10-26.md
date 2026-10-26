# Announcement draft (2026-10-26) — not published

A draft of the public note for the open-source release, written on 2026-10-26 after finance-v25. The user decides whether and when it goes out. Every number below is on record (docs/RESULTS.md, docs/JOURNAL.md). The Chinese version follows the English one. It supersedes the draft of 2026-10-19.

---

## reharness: an agent that gets the same request right the second time

Most real work repeats. A finance team runs the same reconciliation, the same invoice batch, the same tax estimate again and again with other numbers. An agent that fails such a request once should not fail it the same way tomorrow — and whatever fixes it should be checked by evidence, not chosen by a person.

reharness is an open-source loop that does this for a business agent on Zapier's AutomationBench (47 simulated SaaS tools, scored on the world state the agent leaves behind). The agent's harness — its tools, hooks, skills and prompts — is a versioned package. When a request comes back rejected, an improver (the same model, Claude Opus 5.5) reads the agent's own run of it, builds a change to the package, tries it on the same request for real, and only a try that comes back accepted is adopted for that request. Everything else is frozen: the scorer, the splits, the budget, and what the improver may see — at most "accepted or sent back" per attempt, and which message or row was rejected, never the expected value.

**What we measured.**
- On 50 finance test tasks with only the accepted-or-sent-back bit, the steady-state pass rate went from 64% to 68%, with zero regressions: 18 tasks were sent back, 3 were corrected and stayed corrected, and no task that passed was broken by learning ($184 of nominal API cost).
- Told which check was rejected and which data sources the world holds, the improver learned generic working habits on its own — open every source the run never read before the first write; derive a figure by code from the inputs read — in four independent sessions, with none of those words in its prompt. One task went 0.5 → 1.0 → 1.0. At effort max, one session built the whole mechanism and a task that had never held before held at the right figure twice, at five to seven times the cost.
- What is left is documented, not hidden: ambiguities in the task's own documents that a thin signal cannot resolve, a world clock the agent is never told, and a benchmark noise floor of 10–17% of tasks flipping between two runs of the same agent.

**Why it is built this way.** Before this loop we spent eight activities and about $1,150 searching for packages that generalise across different workflows, under a held-out screen with confirmed flips. Nothing beat the noise floor, and the reason was structural: the benchmark's 150 training tasks are 150 distinct workflows. The loop now follows how work actually repeats, and it says exactly what the improver was told.

**What you get.** The platform (typed harness packages, an isolated worker, a metered gateway, sealed plans and budget guards), the improver's loop and prompts, the records of every measured run, and the design documents that each run forced to change. MIT licensed. Reproduction starts from one config and the public benchmark.

---

## reharness：同一个请求，第二次就做对的 agent

真实工作大多在重复。财务团队反复跑同一个对账、同一批发票、同一份税额估算，只是数字换了。一个 agent 这次把请求做砸了，明天不该再以同样的方式砸一次——而修它的改动应当由证据裁定，不是由人挑。

reharness 是一个开源回路，在 Zapier 的 AutomationBench（47 个模拟 SaaS 工具，按 agent 留下的世界状态评分）上为业务 agent 做这件事。agent 的 harness——工具、hook、技能、提示词——是一个版本化的包。一个请求被退回时，改进器（同一个模型，Claude Opus 5.5）读 agent 自己那次运行，给包写一个改动，在同一个请求上真跑一次，只有被接受的那次 try 才会被这个请求采用。其他一切冻结：评分器、划分、预算，以及改进器能看到什么——至多每次尝试一个"被接受 / 被退回"的比特，和哪封邮件、哪一行被退回，从不带期望值。

**量到了什么。**
- finance 的 50 道 test 题上，只给"接受 / 退回"一个比特，稳态通过率从 64% 到 68%，零退步：18 道被退回，3 道修好并站住，没有一道通过题被学习弄坏（名义 API 费用 $184）。
- 告诉它哪条检查被退回、世界里有哪些数据源之后，改进器自己学出了通用的工作习惯——第一次写入前打开运行里没读过的每个源；派生数字按读到的输入由代码算——四个互不相通的会话都学出来了，提示词里没有这些词。一道题 0.5 → 1.0 → 1.0。effort = max 下一个会话把整个机制建全，一道从没站住过的题两次都算对，代价五到七倍。
- 剩下的都写明了，没有藏：薄信号分不出的任务文档歧义、从不告诉 agent 的世界时钟、以及基准自身 10～17% 的噪声底。

**为什么这样设计。** 在这条回路之前，我们花了八个活动、约 $1,150 去找跨不同工作流泛化的包，用带确认翻转的留出筛选把关。没有东西高于噪声底，原因是结构性的：基准的 150 道训练题是 150 个不同的工作流。现在的回路按工作真正重复的方式来，并且把改进器被告知了什么写得清清楚楚。

**你能拿到什么。** 平台（类型化的 harness 包、隔离 worker、计量网关、封存计划与预算保护）、改进器的回路与提示词、每次实测运行的记录，以及每次运行逼着改的设计文档。MIT 许可。复现从一个配置和公开基准开始。
