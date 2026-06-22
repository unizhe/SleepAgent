# SleepAgent

SleepAgent 是一个面向老年用户、家属和医生的智能睡眠健康分析系统。项目以 SHHS 多导睡眠 PSG 数据为主要实验数据，结合睡眠分期、呼吸暂停风险检测、医学知识检索增强报告生成和多轮对话交互，输出可解释、可追踪、可持续扩展的睡眠健康分析结果。

本项目主目录为 `sleepagent/`。仓库根目录下的 `yasa/` 是第三方源码目录，仅作为参考或依赖，不在其中放置 SleepAgent 项目文件。

## 目标用户

- 老年用户：获得通俗、温和、可执行的睡眠健康反馈。
- 家属：理解长期睡眠风险、异常趋势和就医建议。
- 医生或研究人员：查看相对专业的指标、模型结果和风险依据。

## 核心目标

- 基于 SHHS PSG 数据构建睡眠健康分析实验闭环。
- 使用 YASA 完成 Wake、REM、NREM 三分类睡眠分期。
- 使用自建 1D-CNN + BiLSTM 完成 normal breathing、hypopnea、suspected apnea 三分类呼吸事件检测。
- 睡眠分期评估 Accuracy、Cohen's Kappa、macro F1、weighted F1 和各类别 F1。
- 呼吸暂停检测以 Recall 为核心，辅以 AUC 和 F1，目标为 AUC > 0.85、Recall > 0.80。
- 通过 Agent 架构整合模型推理、医学知识检索、报告生成和对话交互。

## 计划技术栈

- 后端：FastAPI
- 模型：PyTorch
- Agent 编排：LangGraph
- 数据库：PostgreSQL
- 向量库：Chroma
- 前端：Next.js / React
- 部署：Docker
- 睡眠分期依赖：YASA

## 核心输出

- 睡眠总览
- AHI 指数
- 疑似呼吸暂停统计
- 呼吸频率趋势
- 老人易懂版报告
- 子女/医生专业版报告
- 睡眠风险等级
- 可视化趋势图
- 进一步就医建议

## Agent 设计

SleepAgent 包含三个核心 Agent：

- 睡眠分析 Agent：调用模型推理、整合信号统计、识别异常事件。
- 报告生成 Agent：基于 RAG 检索医学知识库，生成老人易懂版和子女/医生专业版报告。
- 对话交互 Agent：支持多轮问答、个性化建议和主动关怀问候。

系统还包含三个独立服务：

- 报警推送服务：负责高危事件预警。
- 数据管理服务：负责用户数据、分析结果和长期记忆压缩。
- 外部数据工具服务：融合天气、温度、饮食等生活方式因素。

## MVP 策略

项目采用增量开发方式。第一阶段只完成最小可运行闭环，不追求一次性实现完整医疗系统。

MVP 优先完成：

- 项目结构和文档规范。
- SHHS 数据字段和访问方式说明。
- YASA 睡眠分期三分类流程设计。
- 呼吸暂停检测模型的数据接口和评估方案。
- 报告生成的数据结构和模板骨架。
- 最小 FastAPI 接口、Next.js 任务工作台和 legacy Streamlit 调试入口。
- 每次任务结束后更新 `TASK_LOG.md`。

## 本地启动

默认开发部署为：

- FastAPI backend: `http://127.0.0.1:18000`
- Next.js frontend: `http://127.0.0.1:18510`

终端 1：

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 18000
```

终端 2：

```bash
cd frontend && npm run dev
```

前端默认使用真实任务 API 和 SSE 事件流；只有显式设置
`NEXT_PUBLIC_SLEEPAGENT_MOCK_MODE=true` 时才进入无后端 mock 模式。
`frontend/app.py` 保留为 legacy/debug 工具，不作为主 demo 路径。

## 医学安全声明

SleepAgent 是医学辅助分析和科研原型系统，不替代医生诊断、治疗建议或急救判断。任何呼吸暂停、高危睡眠事件或严重症状提示，都应由专业医生结合临床检查、完整 PSG 报告和患者病史综合判断。

SHHS 数据需通过合法授权渠道获取和使用，开发过程中应遵守数据使用协议、隐私保护要求和伦理规范。
