# 训练主机与训练器插件

ouroloop 训练的是决策模型，不限于哪一种。一种模型就是一个训练器插件：TDE 是 demo 用的那一种（`tde`），签名小模型是另一种（`signature`，只用标准库，离线测试走的就是它）。框架负责其余的一切，对所有插件都一样：建数据、在主机上跑作业、校准、过门、按模型引用加载和服务。

## 1. 训练器插件

插件是 `ouroloop/trainers/` 下的一个模块（包外的插件用模块路径命名，例如 `mypackage.mytrainer`）。契约写在 `ouroloop/trainers/__init__.py`：

| 名字 | 在哪一侧 | 做什么 |
|---|---|---|
| `NAME` | | 插件名，即 `[trainer] type` |
| `REQUIREMENTS` | 主机 | 除 ouroloop 之外要装的 pip 依赖。插件自己的包可以由选项 `source` 给出（见 §3） |
| `SUPPORTS_WEIGHTS` | | 可选，默认 False：按 `meta.weight` 加权；不加权时框架只给它权重为 1.0 的样本 |
| `NEEDS_PARENT` | | 可选，默认 False：必须从父模型开始训练；没有父模型的作业在启动之前就被拒绝 |
| `validate(spec, job_dir) -> dict` | 主机 | dry run 检查的内容，不训练 |
| `train(spec, job_dir, deadline) -> run 目录` | 主机 | 读 `train.jsonl`、`val.jsonl`（`evolve/build.py` 写的样本），把 `spec["recipe"]` 映射成自己的训练配置；起进程一律用 `ouroloop.worker.run`，到 `deadline` 自动停 |
| `version()` | 主机 | 可选：训练用的是哪个版本，记进 `result.json` 和 `host.json` |
| `check()` | 主机 | 可选：host init 装完后调用，例如 tde 检查 `torch.cuda` |
| `backend(ref, options) -> Backend` | 控制面 | 按模型引用在进程内加载模型；`options` 是 `[trainers.<name>]` 加上 `[models]`（`jobs_root`、`hf_prefix`、`device`）。模型服务器（`ouroloop serve`）也通过它加载 |
| `release()` | 控制面 | 可选：训练前释放已加载的模型占用的显存 |

新增一个插件：照 `ouroloop/trainers/signature.py` 写一个模块，实现上表的函数，配置里写 `[trainer] type = "<name>"`。数据契约、作业、主机、截止时间、取消、HF 发布、模型引用、门，都不用管。

一个作业在主机上是 `<jobs_root>/<job id>/` 目录，只传三个文件：`spec.json`、`train.jsonl`、`val.jsonl`。主机上装好的 worker（`python -m ouroloop.worker spec.json <job id>`）读 `spec.json` 里的插件名，加载插件，先 validate 再 train，写 `result.json`：模型引用、ouroloop 版本、插件版本、主机清单。`spec.json` 记着控制面的 ouroloop 版本（代码哈希和 git HEAD）；主机上装的不是这个版本时，worker 拒绝运行（退出码 3，提示 `ouroloop host init`）。

## 2. 准备主机

主机的准备只靠两个脚本，都在 `ouroloop/hosts/` 里，对所有插件都一样：`bootstrap.sh` 装环境（host init 和镜像构建时跑），`start.sh` 是主机的启动脚本（镜像的启动命令，云实例开机时也跑它，见 §4）。

`[trainer] host` 有三种取值（或者用 `[trainer.cloud]`，每个作业临时租一台云 GPU，见 §4）：

| host | 作业在哪里、用哪个 Python | 准备 |
|---|---|---|
| 不写 | 本机，控制面自己的 Python（导入控制面能导入的一切） | 不需要；插件的依赖要装在这个环境里 |
| `"local"` | 本机，`~/.ouroloop/venv` | `ouroloop host init local`，或者让训练器自己做 |
| `"user@box"` | 那台主机，`~/.ouroloop/venv` | 配好 ssh 密钥即可，训练器自己做 host init |

```bash
ouroloop host init user@gpu --config ouroloop.toml    # 准备一次（可以重复跑）；插件和选项取自 [trainer]、[trainers.*]
ouroloop host check user@gpu --config ouroloop.toml   # 打印 host.json；能跑这份 ouroloop 的作业时退出码为 0
```

`host init` 做的事：把这份 checkout 打成 sdist，连同本地的插件源码（git 跟踪的文件）、`requirements.txt`、`plan.json` 和 `bootstrap.sh` 用 rsync 发过去，在那边跑 `bootstrap.sh`：

1. 缺什么系统工具就装什么：rsync、git、ps、Python ≥ 3.11（带 venv）。是 root 就直接装，否则只在有免密 sudo 时用 sudo；都不行就报错，说清楚缺什么。
2. 建 `~/.ouroloop/venv`，装上发过去的 ouroloop（主机跑的就是控制面这份代码）、插件和它的依赖。
3. 检查 GPU（有 `nvidia-smi` 就跑一下），插件自己的 `check()`（tde：`torch.cuda`）。
4. 写 `~/.ouroloop/host.json`：ouroloop 版本、Python、GPU、每个插件的版本和来源、Hugging Face 是否可用。

`bootstrap.sh` 是幂等的。每个作业之前，训练器读一次 `host.json`：不存在、ouroloop 版本不同、缺插件、插件来源变了（本地源码的指纹，或 pip 需求串）、`transport = "hf"` 却没有 `huggingface_hub` 或 token，它就先跑一次 host init，再启动作业；init 失败算 `TrainError`，这一轮是 inconclusive，门记录不会被用掉。

Hugging Face token 不经过 ouroloop：在主机上设 `HF_TOKEN`，或者在那边 `hf auth login`。

环境变量：`OUROLOOP_PYTHON` 指定建 venv 用的 Python。`OUROLOOP_TEST_PIP` 只给测试用：用一个替身代替 venv 里的 pip，venv 建成不带 pip 的，什么也不下载。

### 镜像

`docker/worker/Dockerfile` 在构建时跑同一个 `bootstrap.sh`；启动时（`ouroloop/hosts/start.sh`）缺 sshd 或 rsync 就装上，把 `$PUBLIC_KEY` 加进 root 的 `authorized_keys`，把 `$HF_TOKEN` 写给 ssh 会话，生成这个容器自己的 ssh host key，重新记录 `host.json`（这时才看得到 GPU），然后一直跑 sshd。镜像里没有任何密钥。

```bash
docker build -f docker/worker/Dockerfile --build-context tde=../tde -t ouroloop-worker .
docker run -d --gpus all -p 2222:22 -e PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)" ouroloop-worker
```

构建参数 `TRAINERS`（默认 `tde`）是要装的插件；插件的本地源码用同名的 build context 给（用干净的 clone：装的是 git 跟踪的文件），或者用 `SOURCES="name=<pip 需求串>"`。任何 docker 主机、RunPod、Vast.ai 都能跑它。用别的镜像也可以：开机后训练器的检查发现没准备好，就走 host init。

## 3. 配置

```toml
[trainer]
type = "tde"                  # 插件名
host = "user@gpu"             # 不写：本机、当前 Python；"local"：本机 ~/.ouroloop/venv
# jobs_root = "~/.ouroloop/jobs"
# ssh_opts = "-i ~/.ssh/key -o BatchMode=yes"
# transport = "hf"            # 每个 run 提交一次到私有仓库 <hf_prefix><job id>
# hf_prefix = "your-org/ouroloop-"
# deadline_s = 28800
# poll_s = 30

[trainers.tde]                # 插件的选项
source = "~/tde"              # 本机的 TDE checkout（host init 发过去），或 pip 需求串，如 "git+ssh://git@github.com/org/tde.git@v0.1.1"
init_from = "~/tde-runs/base" # 第一个模型的父模型，主机上的路径（绝对路径或 ~ 开头）

[models]                      # 训练出的模型怎么服务：主机上的 ouroloop serve，或本机的 jobs_root
url = "http://gpu:8765"

[recipe]                      # 配方还是配方，由插件映射到它的训练器
replay = "~/tde-data/general/train.jsonl"
```

`source` 以 `/`、`~`、`./`、`../` 开头时是本地目录，否则是 pip 需求串；它会记在 `host.json` 和模型卡上，不要把凭据写进去（私有仓库用主机上的 ssh 密钥或 git 的 credential helper）。主机上的路径（`init_from`、`replay`）没有 checkout 可以相对，只能是绝对路径或 `~` 开头。卡片上的 `backend` 记着训练它的插件和选项（`{"type": "model", "model": <引用>, "trainer": "tde", "options": {...}}`），`[models]` 按它找回插件加载；没有 `trainer` 的旧卡按 `tde` 处理。模型服务器按请求里的 `trainer` 加载，只接受本包的插件和它配置里写到的插件。

签名插件（离线、无 GPU）：

```toml
[trainer]
type = "signature"
jobs_root = "~/.ouroloop/jobs"
poll_s = 0.2

[trainers.signature]
signature = "examples.snake.rules:signature"   # 视图 -> 键；主机上也要能导入

[models]
jobs_root = "~/.ouroloop/jobs"
```

`[trainer] type` 写成 `"module:Class"` 时，是在当前进程里训练的 Trainer 对象（测试和 demo 用），不走作业。

## 4. 云 GPU：RunPod 和 Vast.ai

`[trainer.cloud]` 让每个作业在一台新租的云实例上跑，跑完就销毁，不需要常驻主机。每个作业：

1. 开一台实例，名字（RunPod 的 `name`、Vast.ai 的 `label`）是 `<工作区标签>-<作业 id>`；工作区标签是工作区路径的哈希，不含本机路径。
2. 等它的 ssh 能连上，最长 `boot_timeout_s`：先看 API 说就绪（RunPod：`RUNNING` 且有 `ssh.direct`；Vast.ai：`actual_status` 为 `running`），再真连一次 ssh。
3. 像任何主机一样读 `host.json`：用的是 worker 镜像且版本一致就直接跑；别的镜像（或版本不同）先跑 host init。
4. 通过 ssh 跑作业（主机和端口来自 API）。
5. 作业结束、失败、超时、或等待被中断（包括 Ctrl-C），都会销毁实例（RunPod `DELETE /v2/pods/{id}`，Vast.ai `DELETE /api/v0/instances/{id}/`：销毁后不再计费；「停止」会继续收存储费，所以不用）。

一个进程的第一个作业会先销毁这个工作区上次被杀掉的进程留下的实例，只销毁名字带本工作区标签的，别的工作区和别人的实例不碰。销毁失败会在 stderr 大声说出来，由下一个作业再试。

实例开机时跑 `start.sh`，任何镜像都可以：RunPod 上它是容器的启动命令（自己起 sshd），Vast.ai 上它是 `onstart`（ssh_direct 运行方式由 Vast 自己起 sshd，这时 `OUROLOOP_SSHD=0`）。它把作业的公钥（`ssh_key` 旁边的 `.pub`）加进 `authorized_keys`，把 `HF_TOKEN` 写成 Hugging Face 的 token 文件（ssh 会话看不到容器的环境变量）。用 worker 镜像开机最快；用别的镜像，开机后训练器会跑 host init（装 torch 这类依赖要几分钟）。

规则：

- 云训练器必须用 `transport = "hf"`：实例用完就没了，run 要在那之前提交到 Hugging Face。`"direct"` 在构建训练器时就被拒绝。
- API key 只从环境变量读：`RUNPOD_API_KEY`、`VAST_API_KEY`。不写进配置（`[trainer.cloud]` 里出现别的键会报错）、`spec.json`、日志、模型卡或镜像，只出现在请求的 `Authorization` 头里。
- `HF_TOKEN` 从本机环境读，只作为实例的环境变量交给实例。没设就在开实例之前报错。
- 厂商或开机的失败（API 报错、价格内没有机器、开机超时）都是 `TrainError`：这一轮是 inconclusive，门记录不会被用掉。
- 模型卡的作业记录里有 `cloud`：`provider`、`instance`、`gpu`、`price_per_hour`、`uptime_s`，没有任何密钥。
- ssh 用 `ssh_key` 指定的私钥和实例的端口；新实例的 host key 首次见到时接受，只记在这个作业自己的临时 known_hosts 里。

```toml
[trainer]
type = "tde"
transport = "hf"
hf_prefix = "your-org/ouroloop-"
jobs_root = "~/.ouroloop/jobs"

[trainer.cloud]
provider = "runpod"
image = "ghcr.io/your-org/ouroloop-worker:latest"   # worker 镜像最快；任何镜像都行
gpu = "NVIDIA GeForce RTX 4090"                     # RunPod 的 GPU 类型 id（/v2/catalog/gpus 的 id）
gpu_count = 1
disk_gb = 50                                        # 容器盘；不挂持久卷，销毁后不留任何计费
max_price = 1.0                                     # 可选：美元/小时（按目录价 x 数量，开机后再对照 pod 的 cost）
cloud_type = "SECURE"                               # SECURE（默认，总有公网 IP）或 COMMUNITY
# data_centers = ["EU-RO-1"]
ssh_key = "~/.ssh/id_ed25519"                       # 私钥；它的 .pub 交给实例
boot_timeout_s = 900
boot_poll_s = 10

[models]
jobs_root = "~/.ouroloop/jobs"
hf_prefix = "your-org/ouroloop-"                    # 模型从 Hub 按 <repo>@<commit> 加载
```

```toml
[trainer.cloud]
provider = "vast"
image = "ghcr.io/your-org/ouroloop-worker:latest"
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

RunPod 用 REST v2（`https://api.runpod.io/v2`，v1 在 2026-11-15 下线）。v2 没有价格上限、标签和按名字过滤，所以价格在开机前按 GPU 目录检查，实例按名字在本地过滤。Vast.ai 选价格内最便宜、可直连 ssh、已验证的报价（`/api/v0/bundles/`），用 `ssh_direct` 运行方式创建，作业的公钥也单独挂到实例上。两家文档不清楚或自相矛盾的地方，`ouroloop/evolve/cloud.py` 里就地注释了。
