# activity：配置与 preset → K、工作区、阶段编排、预算根、启动检查、报告、CLI

> 阶段 1 设计文档。包 `ouroloop/activity/`。对应 [DESIGN](../DESIGN.md) §8、§10。来源：`bench.py`（5,360 行）、`cli.py`、`report.py`、`config.py`、`policies.py`（删）。2026-10-09 按阶段 1 的实现对齐（`activity/` 合计约 1,300 行）。

## 1 职责与不做

**做：** 读配置、按 preset 补全并冻结 K；开工作区；按阶段编排（smoke、freeze、baseline、library、round、evaluate、report、export）；开预算根与份额；启动检查（只查钱、泄漏、正确性三类）；报告；CLI。

**不做：** 不实现任何机制（尝试、gate、诊断、记功都在别的包）；不保留 v3/v5 preset、研究位、多领域合并、采集批、段延长、方法库。

## 2 文件

| 文件 | 内容 |
|---|---|
| `activity/config.py` | TOML 读取、schema 校验、与 preset 合并（只能收紧）、`Core` 冻结 |
| `activity/presets.py` | preset 表：`automationbench_v1`（种子 stirrup、六站点、Limits、worker 512m/128/256 MiB tmpfs、memory 开、improver 与 gate 的默认，数值来自 v6 的证据）；`RULES`：每个字段的合并规则 |
| `activity/workspace.py` | `Activity`：工作区的全部句柄（suite、splits、objects、events、releases、budgets、results、transcripts、trace、memory、worker、egress） |
| `activity/phases.py` | 八个阶段函数与 `cycle --until` |
| `activity/checks.py` | 启动检查 |
| `activity/report.py` | `build_report`、`render_markdown`、`write_report` |
| `activity/cli.py` | `ouroloop bench / freeze / export` |

## 3 接口

```python
# config.py
@dataclass(frozen=True)
class Core(Record):                      # kind "core"：preset、sections（CONTRACTS §4 的节）、split_hash、code_version；json() 是 core.json
def load_config(path) -> dict            # 带 _base（配置所在目录），env_file 像 .env 一样加载
def check_tables(config) -> None         # 旧表名报错并指出新名；未知表、[rsi] 未知键、占位符 ref 报错
def merge(config: dict, preset: dict) -> dict          # 按 RULES 只能收紧；违反即 ConfigError 并指出字段
def resolve(config) -> dict              # preset 补全 + 合并 + 派生字段（model.provider/identity/max_effort、harness.api_version）
def freeze_core(config: dict, *, split_hash: str, code_version: str) -> Core
def core_problems(core: Core) -> list[str]
def workspace_root(config) -> Path;  upstream_path(config) -> str | None

# workspace.py
class Activity:
    config; root: Path; core: Core; core_ref; suite; splits: SplitManifest; drift; versions; objects; events
    releases: Releases; budgets: Budgets; results: Results; worker_factory; provider_factory
    k -> dict;  domain;  scope_id;  transcripts;  concurrency;  attempt_limits() -> Budget
    def active_release(self) -> Release;  seed_release();  package(release=None) -> Package
    def worker(self) -> Worker                           # docker_worker(WorkerResources.from_core(K))，或测试给的
    def egress(self) -> LazyPool | None                  # K 的网络是代理时
    def pricing(self) -> Pricing;  root_account() -> BudgetAccount;  account(phase) -> BudgetAccount
    def actor_provider(self, account, *, allocation=None) -> MeteredProvider   # K.model
    def research_provider(self, step: str) -> MeteredProvider                 # K.research_model，在 research:<step> 账户
    def memory(self) -> MemoryService                    # 惰性起 sidecar（MEMORY §4）；进程结束时停
    def memory_for(self, package, *, live=None) -> Callable | None   # 包的 manifest 开了 memory 才有：task -> TaskMemory
    def run_manifest(self, *, candidate, role, split, release, package=None, notes="", repeats=1) -> RunManifest
    # trace() 随阶段 3 加入（runtime/trace.py）
def freeze_split(suite, bench, path, versions) -> (SplitManifest, drift)
def open_activity(config: dict, *, out, freeze_k=True, worker_factory=None, provider_factory=None) -> Activity
    # 冻结或读出划分、由配置算 K；[rsi] core_ref 或 core.json 与之不同即拒绝（新 K 是新活动）；
    # freeze_k 时第一次写 core.json 与种子 release；--checks-only 的 smoke 不写

# phases.py
PHASES = ("smoke", "freeze", "baseline", "library", "round", "evaluate", "report", "export")
def run_arm(activity, *, release, split, candidate, role, pairs, account, plan_ref="", arm="subject", retry=0, supersedes=None, notes="", allocation=None, package=None, memory=None) -> list[TaskResult]
    # 一个运行身份下的一组尝试；已在记录里的不再付费
def smoke(activity, *, tasks=None, limit=None, checks_only=False, out) -> dict      # 划分哈希、评分契约、输入审计、种子全量字面量扫描；一次 dev 尝试
def freeze(activity, *, out) -> dict                                    # 冻结 gate 计划、日程、预算、final 计划；打印 [rsi] 行
def baseline(activity, *, out) -> dict                                  # W0 在 train 与 dev（control_readings 次）
def library(activity, *, out) -> dict                                   # memory 的流与快照（MEMORY §7）：首遇、重试、写入、读数、快照候选（按 K 的 max_memory_bytes 装，MEMORY §7.1）
def round_(activity, index: int, *, out) -> RoundResult                 # improver.run_round（IMPROVER §3）；每轮后停
def evaluate(activity, *, out) -> dict                                  # final（EVALUATE §6）
def report(activity, *, out) -> dict
def export(activity, release_ref, to: Path, *, out) -> AppManifest
def cycle(activity, *, until: str | None, out) -> dict                  # baseline → library → round 0..N；`--until baseline | library | round-K`；连续 patience 轮没有装入就停

# checks.py
def startup_checks(activity, *, phase: str, needs_model: bool = True, needs_worker: bool | None = None) -> list[str]
    # 钱：预算根与份额存在、价目有界、provider 是订阅登录、effort 钉在 K；
    # 泄漏：划分哈希相符、test 未读（final 之前）、R 与 memory 只读 train 的配置；
    # 正确性：worker 镜像 id 与 runner sha256、egress 镜像、memory sidecar 的 venv 等于锁文件、模型各文件的 sha256、K 的 backend 钉表等于本检出的锁文件与 flows（library 阶段、开了 memory 的 round，及跑开了 memory 的包的阶段）、种子 ref 与 K 一致、code_version 相符（含 R 的提示词 `improver/prompts/*.md`）、评分契约、round 前 gate 计划模板已冻结、claude CLI 的版本读得出且与活动第一次付费阶段记下的相同（`cli_version` 事件）

# report.py
def build_report(activity) -> dict      # 基线、每轮（簇、指派、候选与各档、筛选、gate 判定与增益、装入、停因、本轮花费）、memory 读数、final（各臂与 W* − W0 的家族重抽样 95% 区间）、花费（按步单列）、本活动 final 读 test 的次数
def render_markdown(report) -> str
def write_report(activity, report) -> dict   # 两次相同（确定性）

# cli.py
# ouroloop bench --config C --phase P [--until U] [--task T] [--limit N] [--checks-only] [--out J] [--concurrency N]
# ouroloop freeze --config C
# ouroloop export --config C [--release R] --to DIR
# 付费阶段与 freeze 持有 <root>/rsi/activity.lock：同一个活动同时只有一个进程（两个进程会为同样缺的尝试各付一次）；cycle 结束时打印停在哪、为什么、活动已花多少；花费一节列根的直接子账户（各带子树），加起来等于根
```

## 4 阶段与停下点

| 阶段 | 做什么 | 停下来分析 | 花费 |
|---|---|---|---|
| smoke | `--checks-only`：上游版本、划分哈希、评分契约、输入审计、train 与 dev 之间的近重复（不读 test 的断言）；不带：一次 dev 尝试走完整运行壳 | 划分哈希与预期一致？ | $0 / 约 $0.5 |
| freeze | 冻结 gate 计划模板（`TaskEvalPlan`，任务 = 全部 dev，阈值来自 K 的 `gate`）、打开预算根（final 份额事先留出）、final 计划（配置有 `[final]` 且已有装入时封存；还没有装入时注明“未封存”，照常打印 `[rsi]`；封存的 W* 是当时的活跃 release，所以在轮次结束后再封存；还有未结束的轮次时不封存并注明）；打印 `[rsi]` 的 `scope_id`、`core_ref`、`split_ref`、`final_plan_ref`，用户填回配置并提交；日程在 K 里，不另冻结 | — | $0 |
| baseline | W0 跑 train 全部 + dev × `control_readings` | 余量与噪声 | 约 $58 |
| library | memory 流（MEMORY §7）：对 train 逐题首遇与重试，写入、记功；快照（精简行，按记功装进 K 的 `max_memory_bytes`，没装的记在 `library_snapshot` 事件的 `fit`：MEMORY §7.1）作候选从 V3 起走阶梯 | D、学习曲线、快照装了几条、快照 gate | 约 $50～100 |
| round k | improver 的一轮（IMPROVER §3）；每轮后停 | 有没有代码候选到 V2；V3/V4 读数 | 约 $40 |
| evaluate | final | — | 约 $72 |
| report | 报告 | — | $0 |

`cycle --until` 保留：`baseline`、`library`、`round-K`；中间崩溃按 `Execution` 续跑。

**gate 的节奏（2026-10-10 修订，IMPROVER §9.2）：** `cycle` 的耐心只数付过 gate 而没装入的轮；carry 轮（最优候选带到下一轮）不计。`schedule.rounds` 的上限 8；`improver.gate_every` 默认 2。报告的每轮一行多 `carried` 与 `in_sample` 的标注。

## 5 预算根与份额

- 一个活动一个根（`budget.usd`）；final 份额在根打开时预留（`final_usd`）；每轮的研究份额按 K 的 `improver.round_share` 分配；超出的付费步骤不开始。
- 每次付费步骤先 `affordable(bound)`，再预留、再调用、再结算（EVALUATE §3 的 Budgets）。
- 报告按步单列：基线、memory、每轮的 R 会话 / aux 诊断 / V3 / V4、final。

## 6 删掉的（`bench.py` 的这些不搬）

研究位（`research_slot`、`cycle_pairs`、`halve_dev`、`slot_headroom`、campaign 相关的 `_run_campaign_arm`、`_campaign_common`、`_evaluate_outer`）；多领域合并（`pooled_name`、`pool_suites`、`per_domain_counts`）；采集批（`_collection_batches`）；段延长（`_extension_of`、`_record_extension`）；方法库与 `learn`（`learn`、`_refit_round`）；`_v5_preset`、`PRESET_ONLY_TABLES`、`METHOD_COMPONENTS`；v3/v5 的 `harness_settings` 兼容分支；`improvement_settings` 的 `[improvement]` 表；`policies.py` 全部；`startup_checks` 里与三类无关的项。

目标：`activity/` 合计约 1,200 行（`phases.py` ≤ 400）。

## 7 不变量

- K 冻结后，活动期间任何 `.py` 改动都不合入该检出（规则 13，由 `code_version` 守住：启动检查比对）。
- `freeze` 之后 `[rsi]` 的 ref 不变；改了即启动错误。
- test 只在 `evaluate` 读；report 披露读取次数；smoke 的近重复提示只看 train 与 dev。
- 启动检查只查三类；不加别的。

## 8 与现有代码的对应

| 现有 | 处理 |
|---|---|
| `bench.py` | `BenchConfig`/`RsiConfig` → `config.py`；`Activity`/`open_activity` → `workspace.py`；`smoke`/`freeze`/`baseline`/`library_track`/`improve`/`evaluate`/`report`/`cycle` → `phases.py`（内容大改：round 交给 improver，library 交给 memory）；`startup_checks` 等 → `checks.py`；其余删（§6） |
| `cli.py` | → `activity/cli.py`；去掉 `--rounds`/`--reason`/`--plan`/`--replace-splits` 之外的旧项保留 |
| `report.py` | → `activity/report.py`；去掉 `v05_evidence`、`m_tool_writes`、研究位与多领域的段；加各档通过数与条件记功 |
| `config.py` | `load`、`load_dotenv`、`build_provider` → `activity/config.py` 与 `providers/` |
| `examples/automationbench/*.toml` | 旧配置删（`claude.toml`、`claude-full.toml`、`claude-v3.toml`、`improvement.toml`、`claude-v4-*`、`claude-v5-*`：工作区留在本机，配置在 tag 上）；新写 `quickstart.toml`（新表名）与 `finance-v1.toml` |

## 9 测试

- config：preset 合并只能收紧（放宽即 ConfigError）；旧表名报错并指出新名；`freeze_core` 可重复。
- phases：在 `fake_upstream` 的 `rsi` 域上，`smoke --checks-only`、`freeze`、`baseline`、`library`（memory 用脚本化 provider）、`round 0`（improver 用脚本化 R）、`report` 各一例端到端；`cycle --until` 的三个停点；崩溃后续跑不重复付费。
- checks：三类各一例失败；无关项不存在（不测“没有”，只测有的）。
- report：两次相同；各步花费之和等于预算簿；test 读取次数正确。
- `test_hot_path_boundary`：smoke/baseline 路径不加载 improver、memory.sidecar、train。

## 10 开发顺序

`presets.py`、`config.py` → `workspace.py` → `phases.py`（smoke、freeze、baseline 先；library、round 等 memory、improver 包到位后接）→ `checks.py` → `report.py` → `cli.py` → 新配置与 README。
