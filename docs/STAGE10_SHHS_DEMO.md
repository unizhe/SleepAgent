# Stage 10 SHHS Demo

本页是最终演示的最小手动流程。它假设你已经获得 SHHS 数据授权，并且只在本机使用 EDF/XML 文件。不要把 SHHS 原始文件、NPZ、checkpoint 或本地输出提交到 Git。

## 1. 准备环境

### 本地开发环境

本地 Codex 用于修改、审查和运行不依赖真实 SHHS 数据的测试。建议使用独立
Python 3.11 虚拟环境，不需要下载数据或模型权重：

```bash
cd /path/to/SleepAgent
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
cd frontend && npm ci
```

需要在本地检查 PostgreSQL、LangGraph 或 Chroma 接口时，可以按需安装已有
extra：

```bash
python -m pip install -e ".[postgres,agent,rag]"
```

本地开发环境不要求存在 SHHS EDF/XML、派生 NPZ 或 checkpoint；这些文件和
`data/`、`outputs/`、`results/`、`checkpoints/`、根目录 `models/` 均由 Git
忽略。

### 服务器实验环境

服务器真实实验统一使用 Conda 环境 `sleepagent-exp`。不要在 `(base)`、`yasa`
和 `stress` 之间混跑命令或复用进程；`stress` 只作为包含现有 PyTorch/CUDA
栈的克隆源，真实 SHHS 预处理、训练、评估和推理都必须在
`sleepagent-exp` 中完成。

首次创建环境：

```bash
conda create --name sleepagent-exp --clone stress
conda activate sleepagent-exp
cd /mnt/data4/wz/SleepAgent/sleepagent
```

在克隆环境中安装 SleepAgent 服务依赖，再补充真实 SHHS/YASA 读取依赖。不要
给下面的命令增加 `--upgrade`，以免替换 `stress` 中已有的 Torch/CUDA 和科学
计算二进制包：

```bash
python -m pip install -e ".[postgres]"
python -m pip install "numpy>=1.26,<2"
python -m pip install mne yasa pyedflib
```

`experiments` extra 用于声明兼容边界和 Docker 构建：NumPy 必须为 1.26.x
或至少 `<2`，并对 pandas、SciPy、scikit-learn、MNE、YASA、PyTorch 和
pyEDFlib 设置保守上限。服务器原生环境按上面的分步命令安装，避免 pip 为了
追逐新版本而破坏克隆自 `stress` 的现有 Torch 环境。

安装后运行依赖自检。它同时验证模型源码可导入、实验依赖齐全，并打印关键
二进制依赖版本：

```bash
python - <<'PY'
import importlib

modules = ["sleepagent.models", "torch", "mne", "yasa", "pyedflib"]
for name in modules:
    try:
        importlib.import_module(name)
        print(f"IMPORT OK: {name}")
    except Exception as exc:
        raise SystemExit(f"IMPORT FAILED: {name}: {exc}") from exc

import numpy
import sklearn
import torch
import yasa

versions = {
    "numpy": numpy.__version__,
    "torch": torch.__version__,
    "sklearn": sklearn.__version__,
    "yasa": yasa.__version__,
}
for name, version in versions.items():
    print(f"{name}=={version}")

if int(numpy.__version__.split(".", maxsplit=1)[0]) >= 2:
    raise SystemExit("NumPy must be <2 in sleepagent-exp; recreate the environment.")
PY
```

每次开始真实 SHHS 实验前都必须先激活并确认环境：

```bash
conda activate sleepagent-exp
test "$CONDA_DEFAULT_ENV" = "sleepagent-exp"
```

如果自检发现 NumPy 2.x、缺少依赖或 Torch/CUDA 版本被替换，停止实验并从
`stress` 重新克隆 `sleepagent-exp`，不要回到 `base`、`yasa` 或 `stress`
继续拼接运行。

### Docker 实验镜像

默认 Docker backend 只安装 `postgres` extra。使用 SHHS override 构建时会
自动改为安装 `postgres,experiments`：

```bash
SLEEPAGENT_SHHS_ROOT_HOST=/mnt/data4/wz/SleepAgent/data/raw/shhs_sample \
docker compose -f compose.yaml -f compose.shhs-demo.yaml build backend
```

也可以直接构建实验镜像：

```bash
docker build \
  --build-arg SLEEPAGENT_INSTALL_EXTRAS=postgres,experiments \
  -f docker/Dockerfile \
  -t sleepagent:experiments .
```

原始数据不会复制进镜像；SHHS override 只把服务器数据目录只读挂载到
`/data/shhs_sample`。

## 2. 准备本地 SHHS 样本

推荐使用已经约定的本地样本目录：

```bash
export SLEEPAGENT_SHHS_ROOT=/mnt/data4/wz/SleepAgent/data/raw/shhs_sample
```

目录应保留 SHHS 的相对结构，例如：

```text
polysomnography/edfs/shhs1/shhs1-200001.edf
polysomnography/annotations-events-nsrr/shhs1/shhs1-200001-nsrr.xml
polysomnography/annotations-events-profusion/shhs1/shhs1-200001-profusion.xml
```

如果还没有样本，可先参考 `docs/SHHS_LOCAL_DATA.md` 从授权压缩包中只抽取 1 条记录用于 smoke demo。

## 3. 生成演示命令

```bash
python scripts/run_stage10_shhs_demo.py \
  --shhs-root "$SLEEPAGENT_SHHS_ROOT" \
  --record-id shhs1-200001
```

脚本会打印：

- 本地 EDF/XML 是否存在。
- 后端启动命令。
- Next.js 前端启动命令。
- 后端 API smoke 命令。
- SHHS XML 摘要命令。
- YASA 睡眠分期和评估命令。

默认通道名是 `EEG`、`EOG(L)`、`EMG`。如果 EDF header 不一致，先运行脚本打印出的 YASA 命令，观察通道列表，再用 `--eeg`、`--eog`、`--emg` 调整。

## 4. 启动服务

终端 1 启动后端：

```bash
SLEEPAGENT_DATA_STORE_DIR=/tmp/sleepagent_stage10_demo \
uvicorn backend.main:app --host 127.0.0.1 --port 18000
```

终端 2 启动前端：

```bash
cd frontend && npm run dev
```

浏览器打开：

```text
http://127.0.0.1:18510
```

当前 Next.js 前端默认连接 FastAPI 任务 API：点击“开始任务”会调用
`POST /tasks` 创建后端任务，确认运行后调用
`POST /tasks/{task_id}/confirm`，Agent Run Console 通过
`GET /tasks/{task_id}/events/stream` 显示后端事件。只有显式设置
`NEXT_PUBLIC_SLEEPAGENT_MOCK_MODE=true` 时才使用本地 mock 数据。

## 5. 跑后端闭环 smoke

终端 3：

```bash
python scripts/run_stage10_shhs_demo.py \
  --api-smoke \
  --api-base-url http://127.0.0.1:18000 \
  --record-id shhs1-200001
```

该命令会访问：

- `GET /health`
- `POST /tasks`
- `GET /mock-analysis`
- `GET /mock-report`
- `POST /agent/orchestrate`
- `POST /stage9/mock-context`

它会创建一个后端任务用于验证任务 API，但不会自动 confirm，因此不会
读取 SHHS EDF。其余 legacy smoke 端点仍使用 mock 分析结果和本地存储。

## 6. 跑 SHHS 数据演示

先跑 XML 摘要：

```bash
python scripts/summarize_shhs_sample.py \
  --root "$SLEEPAGENT_SHHS_ROOT" \
  --record-id shhs1-200001
```

再跑 YASA 睡眠分期：

```bash
python scripts/run_yasa_sleep_staging_sample.py \
  --edf "$SLEEPAGENT_SHHS_ROOT/polysomnography/edfs/shhs1/shhs1-200001.edf" \
  --eeg EEG \
  --eog 'EOG(L)' \
  --emg EMG \
  --out ../data/processed/sleepagent/stage10_demo/shhs1-200001_yasa_summary.json
```

最后评估 Wake/REM/NREM：

```bash
python scripts/evaluate_yasa_staging_against_shhs_xml.py \
  --yasa-summary ../data/processed/sleepagent/stage10_demo/shhs1-200001_yasa_summary.json \
  --shhs-xml "$SLEEPAGENT_SHHS_ROOT/polysomnography/annotations-events-nsrr/shhs1/shhs1-200001-nsrr.xml" \
  --out ../data/processed/sleepagent/stage10_demo/shhs1-200001_yasa_vs_shhs_eval.json
```

## 演示边界

- Next.js 主前端默认使用真实任务 API 和 SSE 事件流；无后端开发时可通过
  `NEXT_PUBLIC_SLEEPAGENT_MOCK_MODE=true` 切回本地 mock。
- `/mock-analysis`、`/mock-report` 和 legacy Agent smoke 仍使用 mock 分析结果。
- `frontend/app.py` / Streamlit 仅保留为 legacy/debug 工具，不作为主 demo
  或默认 Docker Compose 服务。
- DeepSeek 默认关闭；只有显式传 `use_deepseek=true` 或对应脚本参数才会尝试调用。
- Chroma 和 LangGraph 是可选依赖，未安装时默认走内存检索和线性 Agent 编排。
- Stage 9 告警只写本地 JSONL，不发送短信、邮件或 App 推送。
- 当前呼吸事件模型仍是 MVP 演示级结果，不能作为排除呼吸异常或临床诊断依据。
