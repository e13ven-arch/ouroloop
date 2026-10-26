# 训练主机与训练器插件

> 本文说明现有决策 trainer、作业与 serving，保持兼容。新 actor 生成训练归第二版，以 Tinker 托管服务为参考实现（自托管训练写插件模块路径，由 `JobTrainer` 作为作业运行，不走 `module:Class`；评测推理走 RunPod 上自托管的 vLLM；2026-10-08，V2 §7 第 6 项），加独立生成 artifact/provider 契约，不要求本地/SSH host。首版能力包演化不要求训练账号。见 技术选型（tag archive-2026-10-09）与 M2（tag archive-2026-10-09）。
>
> 2026-10-08（瘦身第一批，[PLAN](PLAN.md) P1）：本文的代码移进可选子包，路径与命令随之改：插件、模型库与 serving 在 `reharness/train/`（extra `[train]`），作业、主机与云在 `reharness/train/jobs/`（extra `[train-jobs]`）；`reharness host` 改为 `python -m reharness.train host`；作业进程是 `python -m reharness.train.jobs.worker`；训练主机镜像是 `reharness/train/jobs/Dockerfile`。冠军指针一族（门、注册表、模型卡、校准、部署、监控撤销）、`reharness serve` 与 `[serving] url` 已删，tag `archive-2026-10-08` 上可读：训练出的模型由 `train_artifact`（`reharness/train/train.py`）做成一个产物，加载探针过了才算 ready；serving 只在控制面进程内按引用加载，所以别的机器上训练的模型发布到 `hf` 库。下文按现状改写，删掉的配置写法不再列出。


本文介绍现有的决策模型训练路径，不限于哪一种决策模型。一种模型就是一个训练器插件：TDE 是其中一种（`tde`），签名小模型是另一种（`signature`，只用标准库，离线测试走的就是它）。框架负责其余的一切，对所有插件都一样：建数据、在主机上跑作业、把 run 发布到模型库、加载探针、按模型引用服务。校准、过门与部署随冠军指针一族删除，v2 按 Releases 与 task_gate 重写（[V2](design/V2_TRAINING_PIPELINE.md) §3.4–§3.6）。

## 1. 训练器插件

插件是 `reharness/train/trainers/` 下的一个模块（包外的插件用模块路径命名，例如 `mypackage.mytrainer`）。契约写在 `reharness/train/trainers/__init__.py`：

| 名字 | 在哪一侧 | 做什么 |
|---|---|---|
| `NAME` | | 插件名，即 `[trainer] type` |
| `REQUIREMENTS` | 主机 | 除 reharness 之外要装的 pip 依赖。插件自己的包可以由选项 `source` 给出（见 §3） |
| `SUPPORTS_WEIGHTS` | | 可选，默认 False：按 `meta.weight` 加权；不加权时框架只给它权重为 1.0 的样本 |
| `NEEDS_PARENT` | | 可选，默认 False：必须从父模型开始训练；没有父模型的作业在启动之前就被拒绝。父模型是调用方交给 `train_artifact` 的 `parent_model_ref`（例如上一个作业的模型），否则是 `init_from` |
| `RECIPE` | | 可选：自己训练器的配方键和默认值，叠加在框架的键（`min_weight`、`focus`、`seed`、`init_from`）之上。研究候选只能改这些键，别的键一律拒绝 |
| `PATH_KEYS` | | 可选：`RECIPE` 里指向主机上文件的键（tde：`replay`），只能由人在 `[recipe]` 里设 |
| `validate(spec, job_dir) -> dict` | 主机 | dry run 检查的内容，不训练 |
| `train(spec, job_dir, deadline) -> run 目录` | 主机 | 读 `train.jsonl`、`val.jsonl`（样本格式见 `reharness/train/dataset.py` 开头），把 `spec["recipe"]` 映射成自己的训练配置，父模型用 `worker.parent_run(spec, job_dir)` 取；起进程一律用 `reharness.train.jobs.worker.run`，到 `deadline` 自动停。run 目录必须是 `job_dir/run`，worker 把它发布到模型库 |
| `version()` | 主机 | 可选：训练用的是哪个版本，记进 `result.json` 和 `host.json` |
| `check(options) -> dict` | 主机 | 可选：host init 装完后用插件选项调用，例如 tde 检查 `torch.cuda`；返回 `"ok": false`（附 `why`）时主机不算就绪 |
| `backend(run, ref, options) -> Backend` | 服务 | 在进程内加载 `run`（模型库按引用取到本机的目录），回答时报告 `ref`；`options` 是产物的后端描述里的插件选项加上 `[serving] device`。选项只能决定怎么加载；模型本身是什么（签名插件的键函数等）由 `train()` 写进 run 里 |
| `release()` | 服务 | 可选：训练前释放已加载的模型占用的显存（只有服务和作业在同一台机器上时才会被调用） |

新增一个插件：照 `reharness/train/trainers/signature.py` 写一个模块，实现上表的函数，配置里写 `[trainer] type = "<name>"`。数据契约、作业、主机、截止时间、取消、模型库、模型引用、加载探针，都不用管。

一个作业在主机上是 `<jobs_root>/<job id>/` 目录，只传三个文件：`spec.json`、`train.jsonl`、`val.jsonl`。主机上装好的 worker（`python -m reharness.train.jobs.worker spec.json <job id>`）读 `spec.json` 里的插件名，加载插件，先 validate 再 train，把 run 发布到 `spec.json` 里的模型库，写 `result.json`：模型引用、reharness 版本、插件版本、主机清单。`spec.json` 记着控制面的 reharness 版本（代码哈希和 git HEAD）；主机上装的不是这个版本时，worker 拒绝运行（退出码 3，提示 `python -m reharness.train host init`）。

## 2. 准备主机

主机的准备只靠两个脚本，都在 `reharness/train/jobs/hosts/` 里，对所有插件都一样：`bootstrap.sh` 装环境（host init 和镜像构建时跑），`start.sh` 是主机的启动脚本（镜像的启动命令，云实例开机时也跑它，见 §4）。

`[trainer] host` 有三种取值（或者用 `[trainer.cloud]`，每个作业临时租一台云 GPU，见 §4）：

| host | 作业在哪里、用哪个 Python | 准备 |
|---|---|---|
| 不写 | 本机，控制面自己的 Python（导入控制面能导入的一切） | 不需要；插件的依赖要装在这个环境里 |
| `"local"` | 本机，`~/.reharness/venv` | `python -m reharness.train host init local`，或者让训练器自己做 |
| `"user@box"` | 那台主机，`~/.reharness/venv` | 配好 ssh 密钥即可，训练器自己做 host init |

```bash
python -m reharness.train host init user@gpu --config train.toml    # 准备一次（可以重复跑）；插件和选项取自 [trainer]、[trainers.*]，或 --trainer
python -m reharness.train host check user@gpu --config train.toml   # 打印 host.json；能跑这份 reharness 的作业时退出码为 0
```

`host init` 做的事：把这份 checkout 打成 sdist（包里的模块和声明过的数据文件，也就是代码哈希覆盖的那些），连同本地的插件源码（git 跟踪的文件）、`requirements.txt`、`plan.json` 和 `bootstrap.sh` 打成一个 tar 包经 ssh 发过去（主机上还不需要 rsync），在那边跑 `bootstrap.sh`：

1. 缺什么系统工具就装什么：rsync、git、ps、Python ≥ 3.11（带 venv；发行版默认的 python3 太旧时，apt 上再试 python3.12、python3.11）。是 root 就直接装，否则只在有免密 sudo 时用 sudo；都不行就报错，说清楚缺什么。
2. 建 `~/.reharness/venv`，装上发过去的 reharness（主机跑的就是控制面这份代码）、插件和它的依赖。
3. 检查 GPU（有 `nvidia-smi` 就跑一下），插件自己的 `check()`（tde：`torch.cuda`；看不到 CUDA 设备时主机不算就绪，除非 `[trainers.tde] device = "cpu"`）。
4. 写 `~/.reharness/host.json`：reharness 版本、Python、GPU、每个插件的版本和来源、每种模型库在那台机器上的情况（`stores`，例如 `hf`：`huggingface_hub` 装了没有、有没有 token）。

`bootstrap.sh` 是幂等的。每个作业之前，训练器读一次 `host.json`，并在同一次 ssh 里问 venv 现在实际跑的 reharness 版本：不存在、reharness 版本不同（或 venv 坏了）、缺插件、插件来源变了（本地源码的指纹，或 pip 需求串）、插件的 `check()` 没通过、`[store]` 在那台机器上发布不了（库模块的 `ready()`，例如 `hf` 没有 `huggingface_hub` 或 token），它就先跑一次 host init，再启动作业；init 失败算 `TrainError`：`train_artifact` 返回 `failed` 并写明原因，作业还没提交，什么都没用掉。

Hugging Face token 不经过 reharness：在主机上设 `HF_TOKEN`，或者在那边 `hf auth login`。

环境变量：`REHARNESS_PYTHON` 指定建 venv 用的 Python。`REHARNESS_TEST_PIP` 只给测试用：用一个替身代替 venv 里的 pip，venv 建成不带 pip 的，什么也不下载。

### 镜像

`reharness/train/jobs/Dockerfile` 在构建时跑同一个 `bootstrap.sh`；启动时（`reharness/train/jobs/hosts/start.sh`）缺 sshd 或 rsync 就装上，把 `$PUBLIC_KEY` 加进 root 的 `authorized_keys`，把 `$HF_TOKEN` 写给 ssh 会话，生成这个容器自己的 ssh host key，重新记录 `host.json`（这时才看得到 GPU），然后一直跑 sshd。镜像里没有任何密钥。

```bash
docker build -f reharness/train/jobs/Dockerfile -t reharness-worker .          # 只有 reharness
docker build -f reharness/train/jobs/Dockerfile --build-arg TRAINERS="tde" \
    --build-context sources=../plugin-sources -t reharness-worker .     # 连同插件：../plugin-sources/tde 是 TDE 的 clone
docker run -d --gpus all -p 2222:22 -e PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)" reharness-worker
```

构建参数 `TRAINERS`（默认为空：主机缺插件时，第一个作业之前 host init 会装上）是要装的插件。插件的本地源码放在 `sources` 这个 build context 里，每个插件一个子目录 `<name>/`（不能是符号链接）；它只在安装那一步挂载进来，不会成为镜像的任何一层，装进去的只有 git 跟踪的文件。也可以用 `SOURCES="name=<pip 需求串>"`。任何 docker 主机、RunPod、Vast.ai 都能跑它。用别的镜像也可以：开机后训练器的检查发现没准备好，就走 host init。

## 3. 配置：四个角色

不管有几台机器，都是四个角色，各自一张表：

| 角色 | 配置 | 是什么 |
|---|---|---|
| 控制面 | 运行 `reharness` 的那台机器 | 账本、建集、`train_artifact`（预算预留、加载探针）、serving、研究循环 |
| 训练 | `[trainer]`、`[trainers.<name>]` | 本机、一台主机（`host`）或每个作业一台云实例（`[trainer.cloud]`）；只训练，把 run 发布到模型库 |
| 模型库 | `[store]` | `type = "dir"`：一个目录（`root`，默认 `~/.reharness/jobs`，与 `jobs_root` 相同时 run 就留在作业目录里，否则拷一次过去）；有训练主机时这个目录在那台主机上。引用是作业 id。`type = "hf"`：私有仓库 `<prefix><job id>`，每个 run 提交一次，引用是 `<repo>@<commit>`，哪里都能读 |
| 服务 | `[serving]` | 进程内：从模型库取引用，由训练它的插件加载（只能设 `device`）。原来的 `url` 模式（一台跑 `reharness serve` 的模型服务器）已删，写了 `url`、`token_env` 或 `timeout` 会按名字报错 |

模型库发布出的引用不可变，产物与绑定只记它。问训练模型的一律走服务：加载探针、绑定的 model 策略；训练器不建后端。`train_artifact` 在作业成功后让服务加载模型、问一次探针（`_probe_artifact`）：模型报告的 id 必须等于作业的模型引用，答案要落在探针问题的标签上，否则产物是 `failed`。门、晋升、部署与回滚不在这里：一个训练出的模型作为 W 的组件进同一个 `task_gate`，经 Releases 安装（v2，V2 §3.6）。

下面的布局差别只在配置。

**布局一：一台机器**（控制面、训练、模型库、服务都在本机）

```toml
[trainer]
type = "signature"            # 或 "tde"，在本机 GPU 上训练

[store]
type = "dir"                  # root 默认 ~/.reharness/jobs：run 留在作业目录里

# [serving] 不写：进程内加载
```

**布局二：笔记本上的控制面 + 一台训练用的 GPU 主机**

```toml
[trainer]
type = "tde"
host = "user@gpu"             # 不写：本机、当前 Python；"local"：本机 ~/.reharness/venv
# jobs_root = "~/.reharness/jobs"
# ssh_opts = "-i ~/.ssh/key -o BatchMode=yes"
# deadline_s = 28800
# poll_s = 30

[trainers.tde]                # 插件的选项
source = "~/tde"              # 本机的 TDE checkout（host init 发过去），或 pip 需求串，如 "git+ssh://git@github.com/org/tde.git@v0.1.1"
init_from = "~/tde-runs/base" # 第一个模型的父模型，主机上的路径（绝对路径或 ~ 开头）

[store]
type = "hf"                   # 模型从 Hub 按 <repo>@<commit> 加载；GPU 主机上要有 HF_TOKEN
prefix = "your-org/reharness-"

# [serving] 不写：控制面进程内加载

[recipe]                      # 框架的键（min_weight、focus、seed、init_from）加上插件的 RECIPE 键（tde：epochs、lr_head、replay ...）
replay = "~/tde-data/general/train.jsonl"
```

训练在另一台主机上时，模型库要用 `hf`：`dir` 库在训练主机上，控制面读不到那些 run，构建训练器时就被拒绝（原来可以配那台主机上的模型服务器，`[serving] url`，已删）。

**布局三：云上训练 + `hf` 模型库**：见 §4。

旧配置照读：`[models]` 就是 `[serving]`（其中的 `url` 一类键按上面的规则报错）；没有 `[store]` 时，`[trainer] transport = "direct"` 读作 `dir` 库，`root` 取 `[models] jobs_root`，没有就取 `[trainer] jobs_root`；`transport = "hf"` 读作 `hf` 库，`prefix` 取 `hf_prefix`。没写 `transport` 时按 `"direct"`，除非 `[models]` 里有 `hf_prefix`：那是旧的模型服务器配置，按 `hf` 库读（一台服务器现在只读一个库；它还要读 job id 的话，写 `[store]`）。`transport`、`hf_prefix` 和 `[models]` 已经弃用，新配置写 `[store]` 和 `[serving]`；`[store]` 和这些旧键同时出现会报错，只留一种。

父模型由调用方给（`train_artifact` 的 `parent_model_ref`），框架不再替你挑：原来按冠军自动选（只选同一插件、同一个库、同一台训练机器上的冠军），随冠军指针一族删除。给的父模型照样要满足这三条，否则不能当父模型。没有父模型时，第一个模型从 `[recipe]` 或 `[trainers.<name>]` 的 `init_from` 开始。

`source` 以 `/`、`~`、`./`、`../` 开头时是本地目录，否则是 pip 需求串；它会记在 `host.json` 和作业记录里，不要把凭据写进去（私有仓库用主机上的 ssh 密钥或 git 的 credential helper）。主机上的路径（`init_from`、`replay`）没有 checkout 可以相对，只能是绝对路径或 `~` 开头。产物的后端描述（`backend_spec`）记着训练它的插件和选项（`{"type": "model", "model": <引用>, "trainer": "tde", "options": {...}}`），服务按它找回插件加载；没有 `trainer` 的旧描述按 `tde` 处理。服务只接受本包的插件和配置里写到的插件。

签名插件（离线、无 GPU）：

```toml
[trainer]
type = "signature"
jobs_root = "~/.reharness/jobs"
poll_s = 0.2

[trainers.signature]
signature = "mypackage.rules:signature"       # 视图 -> 键；主机上也要能导入

[store]
type = "dir"
root = "~/.reharness/jobs"
```

`[trainer] type` 写成 `"module:Class"` 时，是在当前进程里训练的 Trainer 对象（只给测试用），不走作业；它的模型也只在进程内：后端由产物的后端描述在控制面直接建，不经过模型库。

## 4. 云 GPU：RunPod 和 Vast.ai

`[trainer.cloud]` 让每个作业在一台新租的云实例上跑，跑完就销毁，不需要常驻主机。每个作业：

1. 开一台实例，名字（RunPod 的 `name`、Vast.ai 的 `label`）是 `<工作区标签>-<作业 id>`；工作区标签来自一个随机 id，第一次用时生成并存在 `<工作区>/models/.cloud_id`，不含本机路径，两个共用账号的控制面即使路径相同也不会撞。创建请求不会盲目重发：应答丢了（断线、5xx），先按名字找，找到就接管，找不到才再建。
2. 等它就绪，最长 `boot_timeout_s`：先看 API 说就绪（RunPod：`RUNNING` 且有 `ssh.direct`；Vast.ai：`actual_status` 为 `running`），再经 ssh 确认 `start.sh` 已经跑完（它最后写 `~/.reharness/started`；Vast.ai 上 sshd 比 `start.sh` 先起来）。
3. 像任何主机一样读 `host.json`：用的是 worker 镜像且版本一致就直接跑；别的镜像（或版本不同）先跑 host init。
4. 通过 ssh 跑作业（主机和端口来自 API）。每次轮询也问一次 API：实例没了（Vast.ai 主机下线、RunPod pod 退出），作业立刻算失败，不等到截止时间。
5. 作业结束、失败、超时、或等待被中断（包括 Ctrl-C），都会销毁实例（RunPod `DELETE /v2/pods/{id}`，Vast.ai `DELETE /api/v0/instances/{id}/`：销毁后不再计费；「停止」会继续收存储费，所以不用）。

一个进程的第一个作业会先销毁这个工作区上次被杀掉的进程留下的实例，只销毁名字带本工作区标签的，别的工作区和别人的实例不碰。销毁失败会在 stderr 大声说出来，记进作业记录的 `cloud.stop_failed`，由下一个作业再试。

控制面停不掉实例时，它的寿命也有上限：作业进行中收到 SIGTERM、SIGHUP 会像异常一样走到销毁；实例拿到 `REHARNESS_TTL_S`（开机超时 + host init + 作业截止时间 + 余量），`start.sh` 到时结束容器。

实例开机时跑 `start.sh`，任何镜像都可以：RunPod 上它是容器的启动命令（自己起 sshd），Vast.ai 上它是 `onstart`（ssh_direct 运行方式由 Vast 自己起 sshd，这时 `REHARNESS_SSHD=0`）。它把作业的公钥（`ssh_key` 旁边的 `.pub`）加进 `authorized_keys`，把 `HF_TOKEN` 写成 Hugging Face 的 token 文件（ssh 会话看不到容器的环境变量）。用 worker 镜像开机最快；用别的镜像，开机后训练器会跑 host init（装 torch 这类依赖要几分钟）。

规则：

- 云训练器要用比实例活得久的模型库（`[store] type = "hf"`）：实例用完就没了，run 要在那之前提交到 Hugging Face。`dir` 库在构建训练器时就被拒绝。
- API key 只从环境变量读：`RUNPOD_API_KEY`、`VAST_API_KEY`。不写进配置（`[trainer.cloud]` 里出现别的键会报错）、`spec.json`、日志、作业记录或镜像，只出现在请求的 `Authorization` 头里。
- 模型库发布要用的环境变量（库模块的 `ENV`；`hf`：`HF_TOKEN`）从本机环境读，只作为实例的环境变量交给实例。没设就在开实例之前报错。
- 厂商或开机的失败（API 报错、价格内没有机器、开机超时）都是 `TrainError`：`train_artifact` 返回 `failed` 并写明原因，在开实例之前就停下，没有作业被提交。
- 作业记录（`TrainResult.job`，`train_artifact` 写进产物对象）里有 `cloud`：`provider`、`instance`、`gpu`、`price_per_hour`、`uptime_s`（销毁失败时还有 `stop_failed`），没有任何密钥。
- ssh 用 `ssh_key` 指定的私钥和实例的端口；新实例的 host key 首次见到时接受，只记在这个作业自己的临时 known_hosts 里。

```toml
[trainer]
type = "tde"
jobs_root = "~/.reharness/jobs"

[trainer.cloud]
provider = "runpod"
image = "ghcr.io/your-org/reharness-worker:latest"   # worker 镜像最快；任何镜像都行
gpu = "NVIDIA GeForce RTX 4090"                     # RunPod 的 GPU 类型 id（/v2/catalog/gpus 的 id）
gpu_count = 1
disk_gb = 50                                        # 容器盘；不挂持久卷，销毁后不留任何计费
max_price = 1.0                                     # 可选：美元/小时（按目录价 x 数量，开机后再对照 pod 的 cost）
cloud_type = "SECURE"                               # SECURE（默认，总有公网 IP）或 COMMUNITY
# data_centers = ["EU-RO-1"]
ssh_key = "~/.ssh/id_ed25519"                       # 私钥；它的 .pub 交给实例
boot_timeout_s = 900
boot_poll_s = 10

[store]
type = "hf"
prefix = "your-org/reharness-"                       # 模型从 Hub 按 <repo>@<commit> 加载

# [serving] 不写：控制面进程内加载
```

```toml
[trainer.cloud]
provider = "vast"
image = "ghcr.io/your-org/reharness-worker:latest"
gpu = "RTX 4090"                                    # Vast.ai 的 gpu_name（空格，不是下划线）
gpu_count = 1
disk_gb = 50
max_price = 0.6                                     # 必填：美元/小时，含 disk_gb 的存储费（dph_total）
min_reliability = 0.98
offers = 3                                          # 最便宜的机器被抢走时，最多再试几个报价
ssh_key = "~/.ssh/id_ed25519"
```

```bash
export RUNPOD_API_KEY=...   # 或 VAST_API_KEY
export HF_TOKEN=...
```

RunPod 用 REST v2（`https://api.runpod.io/v2`，v1 在 2026-11-15 下线）。v2 没有价格上限、标签和按名字过滤，所以价格在开机前按 GPU 目录检查，实例按名字在本地过滤。Vast.ai 选价格内最便宜、可直连 ssh、已验证的报价（`/api/v0/bundles/`），用 `ssh_direct` 运行方式创建，作业的公钥也单独挂到实例上。两家文档不清楚或自相矛盾的地方，`reharness/train/jobs/cloud.py` 里就地注释了。
