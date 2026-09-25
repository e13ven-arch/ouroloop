# 贪吃蛇：ouroloop 的框架 demo

连续跑几代完整的 RSI 循环：每代用当前版本玩一批对局，环境给每一步打标签，冻结门记录，训练、校准、过门，晋升或不晋升；下一代用新版本玩。决策点只有一个：`move`，在合法方向（最多 3 个）里选一个。模型只在阈值允许的地方执行，其余步由规则蛇走（`rules.greedy_safe`：朝食物走，能不死就不往死路走）。第 2 代埋一个用打乱标签训练的坏候选，走同一个门；第 5 代起棋盘从 8×8 换成 10×10：已部署的模型在新棋盘上如果退化，执行后监控会撤销它，下一代用新棋盘的数据重训。会不会退化要看模型，GPU 上还没跑过。

## 在 GPU 主机上跑

整个循环都在 GPU 主机上：TDE 的 venv（已装 torch+CUDA 和 tde）里装上 ouroloop，推理在进程内，训练是子进程。

```bash
cd ~/ouroloop && pip install -e .     # 装进 TDE 的 venv
ouroloop demo snake --generations 2   # 在 ouroloop 的 checkout 根目录运行；先试两代
ouroloop demo snake                   # 默认 8 代，每代 80 局
```

`demo.toml` 里的 `tde_repo`、`[backends.general] run` 和 `[recipe] init_from` 按本机 TDE checkout 和通用 checkpoint（tde-general-v0.1）的位置改；也可以复制成 `demo.local.toml`（git 忽略），用 `--config examples/snake/demo.local.toml`。同一个工作区再跑会接着上一代往下跑；`--dir` 换一个工作区。

没有 GPU 时用笔记本版：`ouroloop demo snake --config examples/snake/offline.toml`，用一个按局部棋面计数的小模型（签名模型）代替 TDE，几分钟跑完，不需要任何 key。

笔记本版演示的是机制，不是更会玩的蛇。小模型只看朝向、蛇头四周的四个格子和食物在哪个方向，这正是规则蛇看的东西，所以它最多学会规则蛇本来就走对的那些步：自动决策的比例会上升、实际错误率在预算内，但它接管的步和规则蛇走的一样（offline.toml 的一次运行里它接管了一万四千多步，没有一步和规则蛇不同），每一代的指标和括号里的基线完全相同；换成 10×10 它也不会退化，撤销和恢复只有单元测试覆盖。蛇比规则蛇玩得好（指标高于括号里的基线），要看 GPU 上能看到整个棋盘的 TDE。

## 输出的每一列

| 列 | 含义 |
|---|---|
| `gen` | 第几代 |
| `data from` | 这一代的对局由哪个版本下（session 事件里也记着）；`base` 表示还没有晋升过模型；`(revoked)` 表示这个版本在这一代开始前已被撤销，它不再执行，对局由规则蛇下 |
| `auto` | 这一代的 move 决策里由模型执行的比例，其余由规则蛇走 |
| `realized error <=CP95 vs budget` | 模型执行的步里和环境标签不一致的比例。每局只抽一步计数（`sampled`）：`错/局数=点估计`，`<=` 后是 Clopper-Pearson 95% 上界，`vs` 后是风险预算（门模板的 risk，5%）。模型没执行过就是 `-` |
| `board` `food` `steps` `died` | 棋盘；每局平均吃到的食物；每局平均步数；撞死的局数/总局数（其余是走满步数、吃满棋盘或太久没吃到）。括号里是同一批对局（同样的 session、seed 和棋盘）只由规则蛇下、不问任何模型的结果（`baseline/`）：棋盘变大、换 seed 带来的变化两边都有，两边的差别才是模型带来的 |
| `round` | 这一轮的候选和门的结论；`-> champion` 表示晋升。`planted` 是埋的坏候选，`monitor revoked` 是执行后监控撤销了某个版本（它的实际错误率的 95% 下界超过预算） |

最后一行把工作区里的各代（接着跑的也算）合并，给出总的实际错误率和上界，并列出落盘位置：`ledger/`（每个 session、决策、结果，每局的指标（`metrics` 事件）和每一代打印的那一行（`generation` 事件），门和监控事件）、`gates/`（门模板和每一轮的门记录与结论）、`models/`（每个候选的卡片、数据和模型，卡片上有父版本；`champion.json`）、`harness/`（校准和 `revoked.json`）、`baseline/`（规则蛇下的同一批对局的账本）。屏幕上的每个数都能从这些文件重新算出来。

## 标签从哪来

`labeler.py`，只由环境代码计算，和谁在下无关：在不会立刻撞死的方向里，选沿最短路吃到食物、吃完还能追到自己尾巴的那一步（吃到最后一个食物、填满棋盘就赢了，也算）；暂时没有这样的路，就在走完还能追到尾巴的方向里（都追不到就在所有不撞死的方向里）选留给蛇头空间最大的那一步。平局时保持当前方向，否则按 up、down、left、right 的顺序取第一个。每个方向都会立刻撞死时不打标签（标签为 None，训练和校准都不读）。

## 文件

| 文件 | 作用 |
|---|---|
| `game.py` | 规则、合法方向、棋盘文本（和 jev 仓库 `integrations/snake` 的格式一样） |
| `labeler.py` | 环境的标签 |
| `rules.py` | 规则蛇（默认回答者），和签名模型的键 |
| `scenario.py` | `move` 决策点和对局（一局一个 session） |
| `demo.toml`、`offline.toml` | GPU 版和笔记本版的配置 |
