# Announcement draft (2026-10-19) — not published

A draft of the public note for the first open-source release, written on 2026-10-19 after finance-v9 r0. It is a draft:
the user decides whether and when it goes out, and under the project's own standard (a sealed held-out gain that is
not noise, docs/PLAN.md §0.1) the results are not yet the headline it would need. Every number below is on record
(docs/RESULTS.md, docs/JOURNAL.md). The Chinese version follows the English one.

---

## reharness: a bounded self-improvement loop for business agents, measured honestly

reharness is an open-source prototype of bounded recursive self-improvement for agent applications. An improver reads
an agent's failures on training tasks, writes changes to the agent's harness package (tools, hooks, prompts, skills,
memory), and the platform decides whether to install them — by evidence, never by a person. Everything the improver
cannot touch is frozen: the scorer, the data splits, the gates, the budget. The claim for any version is a sealed final
on held-out test tasks, under a plan committed before the first test attempt.

We built it on Zapier's AutomationBench (47 simulated SaaS tools, scored on the world state the agent leaves behind) with
Claude Opus 5.5 as both the agent and the improver, at one fixed effort level, on a subscription budget.

**What we measured.**
- The loop finds changes from training failures alone. On Marketing, the first version improved the held-out pass rate
  from 41.3% to 54.3% (+13.0 points, 95% interval [+2.2, +26.1]). On finance, two sealed finals read +8.0 and +4.0
  points, both within the interval that 25–50 test tasks can resolve.
- Then we measured the benchmark's noise floor: between two runs of the same agent, 10–25% of tasks flip. Most of
  what an unguarded loop installs is that noise. We rebuilt acceptance on it: a flip counts only when it repeats under
  the candidate and the baseline agrees with itself; a package of tools and hooks is credited or blamed only on the
  attempts where one of its components acted; a candidate runs on the failures it claims before it is screened.
- With those rules the measurements are clean — the latest round installed nothing and blamed nothing falsely — and
  no package has yet beaten the floor on the three-domain stream. A hand-written package built from a reading of all
  63 failing tasks did not either (+5 fixes, −3 regressions on 60 held-out tasks, twice). That is the current result,
  and the repository shows the whole path to it.

**What is in the repository.** The typed harness package and its verification ladder; the isolated worker and the
runner shell; the sealed-final machinery with its budget book; the improver's rounds with their ledger; the memory
component on ReMe; every config, plan and journal entry of the nine measured activities. Run records stay local; the
scripts that produced each number are tracked.

**What we are doing next.** Fixing the hook replay so a hook sees in replay what it sees live (the defect that pushed
the improver from the right mechanism to a weaker one in the last round), then moving the evidence infrastructure
into the seed so the improver writes rules instead of plumbing.

Repository: https://github.com/e13ven-arch/reharness (MIT).

---

## reharness：一个诚实测量的业务 agent 有界自改进回路

reharness 是一个面向 agent 应用的有界递归自改进开源原型。改进器读 agent 在训练任务上的失败，改 agent 的 harness 包
（工具、hook、提示词、技能、memory），由平台按证据决定装不装——不由人挑。改进器碰不到的东西都冻结：评分器、数据划分、
gate、预算。任何版本的主张只来自留出 test 题上的封存 final，计划在第一次 test 尝试之前提交。

它建在 Zapier 的 AutomationBench 上（47 个模拟 SaaS 工具，按 agent 留下的世界状态评分），agent 与改进器都是 Claude
Opus 5.5、同一档 effort、订阅预算。

**量到了什么。**
- 回路只从训练失败里找改动。Marketing 上第一个版本把留出通过率从 41.3% 提到 54.3%（+13.0 分，95% 区间 [+2.2, +26.1]）。
  finance 上两次封存 final 读到 +8.0 与 +4.0 分，都在 25～50 道 test 题分辨不出的区间内。
- 然后量出了基准的噪声底：同一个 agent 跑两次，10～25% 的题翻转。一个没有防护的回路装进去的多半就是这种噪声。接受规则
  因此重建：翻转只在候选下重复、且基线与自己一致时才算；只含工具与 hook 的包只在组件出手过的尝试上记功或记过；候选先在它
  认领的失败题上真跑再进筛选。
- 在这些规则下测量是干净的——最近一轮没有装入任何东西，也没有误判任何退步——而三域流上还没有一个包压过噪声底。人读完全部
  63 道失败题手写的包也没有（60 道留出修 5 退 3，两次）。这就是当前的结果，仓库里是走到这里的全部路径。

**仓库里有什么。** 有类型的 harness 包与它的验收阶梯；隔离 worker 与运行壳；带预算簿的封存 final 机制；带台账的改进器轮次；
基于 ReMe 的 memory 组件；九个实测活动的全部配置、计划与日志。运行记录留在本机，产出每个数字的脚本都在仓库里。

**接下来做什么。** 修 hook 回放，让 hook 在回放里看到和真跑一样的东西（上一轮把改进器从对的机制逼到弱机制的缺陷）；再把
证据基础设施搬进种子，让改进器写规则而不是写管道。

仓库：https://github.com/e13ven-arch/reharness（MIT）。
