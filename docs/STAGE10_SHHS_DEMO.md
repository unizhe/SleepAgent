# Stage 10 SHHS Demo

本页是最终演示的最小手动流程。它假设你已经获得 SHHS 数据授权，并且只在本机使用 EDF/XML 文件。不要把 SHHS 原始文件、NPZ、checkpoint 或本地输出提交到 Git。

## 1. 准备环境

从项目代码根目录进入：

```bash
cd /mnt/data4/wz/SleepAgent/sleepagent
python -m pip install -e .
```

如果要运行 YASA 真实 EDF 演示，还需要当前环境已经可导入 YASA/MNE。若只演示后端、前端、Agent、报告和 Stage 9 本地存储，可先不安装可选依赖。

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
