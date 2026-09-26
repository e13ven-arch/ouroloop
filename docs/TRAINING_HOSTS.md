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

`[trainer] host` 有三种取值：

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

`docker/worker/Dockerfile` 在构建时跑同一个 `bootstrap.sh`；启动时（`start.sh`）把 `$PUBLIC_KEY` 加进 root 的 `authorized_keys`，把 `$HF_TOKEN` 写给 ssh 会话，生成这个容器自己的 ssh host key，重新记录 `host.json`（这时才看得到 GPU），然后一直跑 sshd。镜像里没有任何密钥。

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
