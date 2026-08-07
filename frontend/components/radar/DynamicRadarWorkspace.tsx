"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  ArrowRight,
  Bot,
  CheckCircle2,
  ChevronRight,
  Clock3,
  FileClock,
  History,
  Loader2,
  Radio,
  RefreshCw,
  Send,
  ShieldCheck,
  TriangleAlert,
  Wifi,
} from "lucide-react";

import {
  answerRadarUserInput,
  createRadarGoalTask,
  declineRadarUserInput,
  getRadarDashboard,
  getRadarDecisionTrace,
  getRadarRealtime,
  getRadarTask,
  listRadarDevices,
  listRadarTasks,
  runRadarTask,
  resolveRadarConfirmation,
  subscribeRadarTask,
} from "@/lib/radar-api";
import type {
  RadarDecisionTrace,
  RadarDecisionTraceEntry,
  RadarGoalType,
  RadarPublicDashboardSummary,
  RadarPublicDevice,
  RadarRealtimeState,
  RadarTaskDetail,
  RadarTaskStreamEvent,
  RadarUserInputRequest,
} from "@/lib/radar-types";
import { cn } from "@/lib/utils";


const goals: Array<{ type: RadarGoalType; label: string; description: string }> = [
  { type: "night_review", label: "昨夜观察", description: "整理指定夜晚的记录与质量" },
  { type: "trend_comparison", label: "趋势对比", description: "比较一个明确日期范围" },
  { type: "change_explanation", label: "变化说明", description: "说明观察到的变化与不确定性" },
  { type: "data_quality_diagnosis", label: "数据质量", description: "排查设备与记录覆盖问题" },
  { type: "doctor_material", label: "医生材料", description: "生成需确认的结构化材料" },
  { type: "grounded_question", label: "指定材料问答", description: "仅根据明确报告与日期回答" },
];

const activeStatuses = new Set([
  "created",
  "running",
  "waiting_for_user_input",
  "waiting_for_confirmation",
]);

export function DynamicRadarWorkspace() {
  const [device, setDevice] = useState<RadarPublicDevice | null>(null);
  const [dashboard, setDashboard] = useState<RadarPublicDashboardSummary | null>(null);
  const [realtime, setRealtime] = useState<RadarRealtimeState | null>(null);
  const [task, setTask] = useState<RadarTaskDetail | null>(null);
  const [trace, setTrace] = useState<RadarDecisionTrace | null>(null);
  const [activeTasks, setActiveTasks] = useState<RadarTaskDetail[]>([]);
  const [history, setHistory] = useState<RadarTaskDetail[] | null>(null);
  const [goalType, setGoalType] = useState<RadarGoalType>("night_review");
  const [targetDate, setTargetDate] = useState(previousDate());
  const [rangeStart, setRangeStart] = useState(daysBefore(7));
  const [rangeEnd, setRangeEnd] = useState(previousDate());
  const [focus, setFocus] = useState("");
  const [question, setQuestion] = useState("");
  const [sourceArtifactId, setSourceArtifactId] = useState("");
  const [sourceDate, setSourceDate] = useState(previousDate());
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const closeStream = useRef<null | (() => void)>(null);

  const pendingQuestion = useMemo(
    () =>
      task?.user_input_requests.find(
        (item) =>
          item.status === "pending" &&
          item.request_id === task.task.pending_user_input_request_id,
      ) ?? null,
    [task],
  );
  const report = useMemo(() => latestTaskResult(task), [task]);
  const urgentDeviceAlert = dashboard?.recent_alerts.find(
    (item) => item.severity === "critical",
  );

  useEffect(() => {
    let active = true;
    async function loadCleanHome() {
      try {
        const devices = await listRadarDevices();
        const first = devices[0] ?? null;
        if (!active) return;
        setDevice(first);
        if (first) {
          const [nextDashboard, nextRealtime] = await Promise.all([
            getRadarDashboard(first.radar_device_id),
            getRadarRealtime(first.radar_device_id),
          ]);
          if (!active) return;
          setDashboard(nextDashboard);
          setRealtime(nextRealtime);
        }
        const activeTasks = await listRadarTasks("active");
        if (!active) return;
        const resumable = activeTasks.filter(
          (item) => item.task.runtime_kind === "product_episode",
        );
        setActiveTasks(resumable);
      } catch (cause) {
        if (active) setError(messageOf(cause));
      }
    }
    void loadCleanHome();
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    const taskId = task?.task.task_id;
    if (!taskId || !activeStatuses.has(task.task.status)) return;
    let active = true;
    let poll: ReturnType<typeof setInterval> | null = null;
    closeStream.current?.();

    async function refresh() {
      try {
        const [nextTask, nextTrace] = await Promise.all([
          getRadarTask(taskId!),
          getRadarDecisionTrace(taskId!),
        ]);
        if (!active) return;
        setTask(nextTask);
        setTrace(nextTrace);
        if (!activeStatuses.has(nextTask.task.status) && poll) {
          clearInterval(poll);
          poll = null;
          closeStream.current?.();
        }
      } catch (cause) {
        if (active) setError(messageOf(cause));
      }
    }

    closeStream.current = subscribeRadarTask(taskId, {
      onEvent: (_event: RadarTaskStreamEvent) => void refresh(),
      onError: () => undefined,
    });
    poll = setInterval(() => void refresh(), 900);
    void refresh();
    return () => {
      active = false;
      if (poll) clearInterval(poll);
      closeStream.current?.();
    };
  }, [task?.task.task_id]);

  async function startGoal() {
    setBusy(true);
    setError(null);
    setHistory(null);
    setTrace(null);
    try {
      const created = await createRadarGoalTask({
        goalType,
        targetDate: usesTargetDate(goalType) ? targetDate : undefined,
        rangeStart: usesRange(goalType) ? rangeStart : undefined,
        rangeEnd: usesRange(goalType) ? rangeEnd : undefined,
        focus,
        question: goalType === "grounded_question" ? question : undefined,
        sourceArtifactId:
          goalType === "grounded_question" ? sourceArtifactId : undefined,
        sourceDate: goalType === "grounded_question" ? sourceDate : undefined,
        idempotencyKey: `radar-goal:${goalType}:${crypto.randomUUID()}`,
      });
      setTask(created);
      await runRadarTask(created.task.task_id);
      setTask(await getRadarTask(created.task.task_id));
    } catch (cause) {
      setError(messageOf(cause));
    } finally {
      setBusy(false);
    }
  }

  async function submitAnswer(request: RadarUserInputRequest) {
    if (!task || !answer.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await answerRadarUserInput({
        taskId: task.task.task_id,
        requestId: request.request_id,
        answer: answer.trim(),
      });
      setAnswer("");
      setTask(await getRadarTask(task.task.task_id));
    } catch (cause) {
      setError(messageOf(cause));
    } finally {
      setBusy(false);
    }
  }

  async function declineInput(request: RadarUserInputRequest) {
    if (!task) return;
    setBusy(true);
    setError(null);
    try {
      await declineRadarUserInput({
        taskId: task.task.task_id,
        requestId: request.request_id,
      });
      setAnswer("");
      setTask(await getRadarTask(task.task.task_id));
    } catch (cause) {
      setError(messageOf(cause));
    } finally {
      setBusy(false);
    }
  }

  async function resolveConfirmation(confirmationId: string, approved: boolean) {
    if (!task) return;
    setBusy(true);
    setError(null);
    try {
      await resolveRadarConfirmation({
        taskId: task.task.task_id,
        confirmationId,
        approved,
      });
      setTask(await getRadarTask(task.task.task_id));
    } catch (cause) {
      setError(messageOf(cause));
    } finally {
      setBusy(false);
    }
  }

  async function runCreatedTask() {
    if (!task || task.task.status !== "created") return;
    setBusy(true);
    setError(null);
    try {
      await runRadarTask(task.task.task_id);
      setTask(await getRadarTask(task.task.task_id));
    } catch (cause) {
      setError(messageOf(cause));
    } finally {
      setBusy(false);
    }
  }

  async function openHistory() {
    setBusy(true);
    setError(null);
    try {
      setHistory(await listRadarTasks("history"));
    } catch (cause) {
      setError(messageOf(cause));
    } finally {
      setBusy(false);
    }
  }

  async function refreshDevice() {
    if (!device) return;
    setBusy(true);
    try {
      const [nextDashboard, nextRealtime] = await Promise.all([
        getRadarDashboard(device.radar_device_id),
        getRadarRealtime(device.radar_device_id),
      ]);
      setDashboard(nextDashboard);
      setRealtime(nextRealtime);
    } catch (cause) {
      setError(messageOf(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="min-h-screen bg-[#f3f0e8] text-[#16211f]">
      <header className="border-b border-[#d8d2c5] bg-[#f8f6f0]">
        <div className="mx-auto flex max-w-[1280px] items-center justify-between px-5 py-5 md:px-10">
          <div className="flex items-center gap-3">
            <Radio className="h-5 w-5 text-[#1f5b51]" />
            <div>
              <div className="font-semibold tracking-tight">SleepAgent 雷达</div>
              <div className="mt-0.5 text-xs text-[#64716e]">家庭睡眠观察与照护协同</div>
            </div>
          </div>
          <div className="flex items-center gap-5">
            <Link
              href="/habit-profile"
              className="text-sm font-medium text-[#1f5b51] hover:text-[#173f38]"
            >
              我的睡眠习惯
            </Link>
            <button
              type="button"
              onClick={() => void openHistory()}
              className="flex items-center gap-2 text-sm text-[#4e5e5a] hover:text-[#173f38]"
            >
              <History className="h-4 w-4" />
              历史任务
            </button>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-[1280px] px-5 py-8 md:px-10 md:py-12">
        {urgentDeviceAlert && (
          <div role="alert" className="mb-8 flex items-start gap-3 border-y border-[#ba3d3d] py-4 text-[#8c2525]">
            <TriangleAlert className="mt-0.5 h-5 w-5 shrink-0" />
            <div>
              <div className="font-semibold">设备上报了需要优先处理的提醒</div>
              <div className="mt-1 text-sm">{urgentDeviceAlert.message ?? urgentDeviceAlert.title}</div>
            </div>
          </div>
        )}

        <section className="grid gap-10 border-b border-[#d8d2c5] pb-10 lg:grid-cols-[1.35fr_.65fr]">
          <div>
            <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.18em] text-[#55726b]">
              <Wifi className="h-3.5 w-3.5" />
              实时设备
            </div>
            <h1 className="mt-5 max-w-3xl text-4xl font-medium leading-[1.15] tracking-[-0.035em] md:text-6xl">
              设备在记录。分析由你决定何时开始。
            </h1>
            <p className="mt-5 max-w-2xl text-base leading-7 text-[#5e6966]">
              刷新页面不会自动生成报告，也不会把上一份结果当作昨夜结果展示。请先确认日期和目标。
            </p>
          </div>

          <div className="border-l-0 border-[#d8d2c5] lg:border-l lg:pl-8">
            <div className="flex items-center justify-between">
              <span className="text-sm text-[#64716e]">{device?.display_name ?? "设备未连接"}</span>
              <button type="button" onClick={() => void refreshDevice()} aria-label="刷新设备数据">
                <RefreshCw className={cn("h-4 w-4", busy && "animate-spin")} />
              </button>
            </div>
            <div className="mt-8 grid grid-cols-2 gap-x-8 gap-y-7">
              <LiveDatum label="设备" value={deviceStatus(device)} />
              <LiveDatum label="床上状态" value={bedPresence(realtime)} />
              <LiveDatum label="心率" value={vital(realtime?.latest_snapshot?.heart_rate_bpm, "次/分")} />
              <LiveDatum label="呼吸" value={vital(realtime?.latest_snapshot?.breath_rate_bpm, "次/分")} />
            </div>
            <div className="mt-8 flex items-center gap-2 text-xs text-[#78827f]">
              <Clock3 className="h-3.5 w-3.5" />
              数据时间 {formatTime(realtime?.generated_at ?? dashboard?.generated_at)}
            </div>
          </div>
        </section>

        {error && (
          <div className="mt-6 border-l-2 border-[#a74343] py-2 pl-4 text-sm text-[#8c2525]">
            {error}
          </div>
        )}

        {history !== null ? (
          <HistoryView
            history={history}
            onClose={() => setHistory(null)}
            onOpen={(selected) => {
              setTask(selected);
              setHistory(null);
            }}
          />
        ) : task ? (
          <ActiveTaskView
            detail={task}
            trace={trace}
            report={report}
            pendingQuestion={pendingQuestion}
            answer={answer}
            setAnswer={setAnswer}
            busy={busy}
            onAnswer={submitAnswer}
            onDecline={declineInput}
            onConfirmation={resolveConfirmation}
            onRun={runCreatedTask}
            onNew={() => {
              closeStream.current?.();
              setTask(null);
              setTrace(null);
            }}
          />
        ) : (
          <>
            <ResumeTasks tasks={activeTasks} onOpen={setTask} />
            <GoalLauncher
              goalType={goalType}
              setGoalType={setGoalType}
              targetDate={targetDate}
              setTargetDate={setTargetDate}
              rangeStart={rangeStart}
              setRangeStart={setRangeStart}
              rangeEnd={rangeEnd}
              setRangeEnd={setRangeEnd}
              focus={focus}
              setFocus={setFocus}
              question={question}
              setQuestion={setQuestion}
              sourceArtifactId={sourceArtifactId}
              setSourceArtifactId={setSourceArtifactId}
              sourceDate={sourceDate}
              setSourceDate={setSourceDate}
              busy={busy}
              onStart={startGoal}
            />
          </>
        )}

        <footer className="mt-14 flex flex-wrap items-center justify-between gap-3 border-t border-[#d8d2c5] pt-5 text-xs leading-5 text-[#78827f]">
          <span>AI 辅助整理，仅用于睡眠健康观察，不构成诊断或医疗建议。</span>
          <span className="flex items-center gap-1.5"><ShieldCheck className="h-3.5 w-3.5" />证据门控 · 动作确认 · 可追溯</span>
        </footer>
      </div>
    </main>
  );
}

function ResumeTasks({
  tasks,
  onOpen,
}: {
  tasks: RadarTaskDetail[];
  onOpen: (detail: RadarTaskDetail) => void;
}) {
  if (!tasks.length) return null;
  return (
    <section className="border-b border-[#d8d2c5] py-7">
      <div className="text-xs font-medium uppercase tracking-[0.16em] text-[#61716d]">尚未结束的任务</div>
      <div className="mt-3 divide-y divide-[#d8d2c5] border-y border-[#d8d2c5]">
        {tasks.map((item) => (
          <button
            key={item.task.task_id}
            type="button"
            onClick={() => onOpen(item)}
            className="flex w-full items-center justify-between gap-5 py-4 text-left"
          >
            <span>
              <span className="block font-medium">继续分析 {goalDate(item)} · {goalLabel(item)}</span>
              <span className="mt-1 block text-xs text-[#78827f]">{taskStatus(item)} · {shortId(item.task.task_id)}</span>
            </span>
            <ArrowRight className="h-4 w-4 shrink-0 text-[#1f5b51]" />
          </button>
        ))}
      </div>
    </section>
  );
}

function GoalLauncher(props: {
  goalType: RadarGoalType;
  setGoalType: (value: RadarGoalType) => void;
  targetDate: string;
  setTargetDate: (value: string) => void;
  rangeStart: string;
  setRangeStart: (value: string) => void;
  rangeEnd: string;
  setRangeEnd: (value: string) => void;
  focus: string;
  setFocus: (value: string) => void;
  question: string;
  setQuestion: (value: string) => void;
  sourceArtifactId: string;
  setSourceArtifactId: (value: string) => void;
  sourceDate: string;
  setSourceDate: (value: string) => void;
  busy: boolean;
  onStart: () => Promise<void>;
}) {
  return (
    <section className="py-10 md:py-14">
      <div className="flex items-center gap-2 text-sm font-medium text-[#1f5b51]">
        <Bot className="h-4 w-4" />
        启动一次 Agent 任务
      </div>
      <div className="mt-7 grid gap-x-10 gap-y-7 lg:grid-cols-[1fr_1fr]">
        <div className="divide-y divide-[#d8d2c5] border-y border-[#d8d2c5]">
          {goals.map((goal) => (
            <button
              type="button"
              key={goal.type}
              onClick={() => props.setGoalType(goal.type)}
              className="flex w-full items-center justify-between gap-5 py-4 text-left"
            >
              <span>
                <span className={cn("block text-base", props.goalType === goal.type ? "font-semibold text-[#173f38]" : "text-[#42504d]")}>{goal.label}</span>
                <span className="mt-1 block text-xs text-[#78827f]">{goal.description}</span>
              </span>
              <ChevronRight className={cn("h-4 w-4", props.goalType === goal.type ? "text-[#1f5b51]" : "text-[#a2aaa7]")} />
            </button>
          ))}
        </div>

        <div className="lg:pt-1">
          {usesTargetDate(props.goalType) && (
            <Field label="要分析哪一晚">
              <input type="date" value={props.targetDate} onChange={(event) => props.setTargetDate(event.target.value)} className={inputClass} />
            </Field>
          )}
          {usesRange(props.goalType) && (
            <div className="grid grid-cols-2 gap-4">
              <Field label="开始日期"><input type="date" value={props.rangeStart} onChange={(event) => props.setRangeStart(event.target.value)} className={inputClass} /></Field>
              <Field label="结束日期"><input type="date" value={props.rangeEnd} onChange={(event) => props.setRangeEnd(event.target.value)} className={inputClass} /></Field>
            </div>
          )}
          {props.goalType === "grounded_question" && (
            <div className="grid gap-5">
              <Field label="指定报告标识"><input value={props.sourceArtifactId} onChange={(event) => props.setSourceArtifactId(event.target.value)} placeholder="必须明确指定，不使用‘最近一份’" className={inputClass} /></Field>
              <Field label="报告对应日期"><input type="date" value={props.sourceDate} onChange={(event) => props.setSourceDate(event.target.value)} className={inputClass} /></Field>
              <Field label="你的问题"><textarea value={props.question} onChange={(event) => props.setQuestion(event.target.value)} rows={3} className={inputClass} /></Field>
            </div>
          )}
          <Field label="特别想关注什么（可选）">
            <input value={props.focus} onChange={(event) => props.setFocus(event.target.value)} placeholder="例如：昨晚离床是否比平时多" className={inputClass} />
          </Field>
          <button
            type="button"
            disabled={props.busy || !launcherReady(props)}
            onClick={() => void props.onStart()}
            className="mt-7 flex w-full items-center justify-between border-y border-[#1f5b51] py-4 font-semibold text-[#173f38] disabled:cursor-not-allowed disabled:opacity-40"
          >
            <span>{props.busy ? "正在创建任务" : launcherActionLabel(props)}</span>
            {props.busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <ArrowRight className="h-4 w-4" />}
          </button>
        </div>
      </div>
    </section>
  );
}

function ActiveTaskView({
  detail,
  trace,
  report,
  pendingQuestion,
  answer,
  setAnswer,
  busy,
  onAnswer,
  onDecline,
  onConfirmation,
  onRun,
  onNew,
}: {
  detail: RadarTaskDetail;
  trace: RadarDecisionTrace | null;
  report: RadarTaskResultView | null;
  pendingQuestion: RadarUserInputRequest | null;
  answer: string;
  setAnswer: (value: string) => void;
  busy: boolean;
  onAnswer: (request: RadarUserInputRequest) => Promise<void>;
  onDecline: (request: RadarUserInputRequest) => Promise<void>;
  onConfirmation: (confirmationId: string, approved: boolean) => Promise<void>;
  onRun: () => Promise<void>;
  onNew: () => void;
}) {
  const isWorking = detail.task.status === "running";
  const pendingConfirmation = detail.confirmations.find((item) => item.status === "pending") ?? null;
  const pendingDecision = pendingConfirmation
    ? (detail.decisions ?? []).find(
        (item) =>
          ["pending", "partially_approved"].includes(item.status) &&
          item.proposal.target_id === pendingConfirmation.evidence_refs[0] &&
          item.proposal.target_hash === pendingConfirmation.evidence_refs[1],
      ) ?? null
    : null;
  const canConfirm = Boolean(
    pendingConfirmation &&
      pendingConfirmation.allowed_roles.includes(detail.task.role) &&
      pendingDecision?.requirements.some(
        (item) =>
          item.role === detail.task.role &&
          !pendingDecision.decisions.some(
            (decision) => decision.actor_role === item.role,
          ),
      ),
  );
  return (
    <section className="py-10 md:py-14">
      <div className="flex flex-wrap items-start justify-between gap-5">
        <div>
          <div className="flex items-center gap-2 text-xs uppercase tracking-[0.16em] text-[#61716d]">
            {isWorking ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : detail.task.status === "created" ? (
              <Clock3 className="h-3.5 w-3.5" />
            ) : (
              <CheckCircle2 className="h-3.5 w-3.5" />
            )}
            {taskStatus(detail)}
          </div>
          <h2 className="mt-4 text-3xl font-medium tracking-[-0.025em] md:text-4xl">
            {goalLabel(detail)}
          </h2>
          <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-sm text-[#687571]">
            <span>目标日期 {goalDate(detail)}</span>
            <ModeIndicator detail={detail} />
            <span>任务 {shortId(detail.task.task_id)}</span>
          </div>
        </div>
        {!isWorking && detail.task.status !== "waiting_for_user_input" && (
          <button type="button" onClick={onNew} className="text-sm text-[#1f5b51]">开始新的任务</button>
        )}
      </div>

      {detail.task.status === "created" && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void onRun()}
          className="mt-8 flex items-center gap-2 border-y border-[#1f5b51] py-3 text-sm font-semibold text-[#173f38] disabled:opacity-40"
        >
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <ArrowRight className="h-4 w-4" />}
          继续并启动这项分析
        </button>
      )}

      {pendingQuestion && (
        <div className="mt-9 max-w-2xl border-y border-[#a9925d] py-6">
          <div className="text-xs uppercase tracking-[0.16em] text-[#806b3d]">Agent 需要一个可观察事实</div>
          <div className="mt-3 text-xl font-medium">{pendingQuestion.question_text}</div>
          <p className="mt-2 text-sm leading-6 text-[#6b716e]">{pendingQuestion.why_needed}</p>
          {pendingQuestion.answer_options.length > 0 && (
            <div className="mt-4 flex flex-wrap gap-2">
              {pendingQuestion.answer_options.map((option) => (
                <button key={option} type="button" onClick={() => setAnswer(option)} className={cn("border px-3 py-2 text-sm", answer === option ? "border-[#1f5b51] bg-[#e4ece8]" : "border-[#d8d2c5]")}>{option}</button>
              ))}
            </div>
          )}
          <div className="mt-4 flex gap-3">
            <div className={cn(inputClass, "flex flex-1 items-center text-[#56635f]")}>{answer || "请选择一个经过审核的实际观察选项"}</div>
            <button type="button" onClick={() => void onAnswer(pendingQuestion)} disabled={busy || !answer.trim()} className="flex items-center gap-2 bg-[#1f5b51] px-4 text-sm text-white disabled:opacity-40"><Send className="h-4 w-4" />继续</button>
          </div>
          <button
            type="button"
            disabled={busy}
            onClick={() => void onDecline(pendingQuestion)}
            className="mt-4 text-sm text-[#6f746f] underline decoration-[#bbb3a2] underline-offset-4 disabled:opacity-40"
          >
            暂不回答并停止本次分析
          </button>
        </div>
      )}

      {pendingConfirmation && (
        <div className="mt-9 max-w-2xl border-y border-[#a9925d] py-6">
          <div className="flex flex-wrap items-center gap-3 text-xs uppercase tracking-[0.16em] text-[#806b3d]">
            <span>需要人工确认</span>
            {pendingDecision && <span className="border border-[#bba978] px-2 py-1">{pendingDecision.risk_level} · {pendingDecision.route}</span>}
          </div>
          <div className="mt-3 text-xl font-medium">
            {pendingDecision?.proposal.explanation.what_will_change ?? pendingConfirmation.reason}
          </div>
          {pendingDecision ? (
            <dl className="mt-4 grid gap-3 text-sm leading-6 text-[#5f6966] md:grid-cols-[100px_1fr]">
              <dt className="text-[#806b3d]">为什么现在</dt><dd>{pendingDecision.proposal.explanation.why_now}</dd>
              <dt className="text-[#806b3d]">影响谁</dt><dd>{pendingDecision.proposal.explanation.who_will_receive_or_be_affected}</dd>
              <dt className="text-[#806b3d]">持续多久</dt><dd>{pendingDecision.proposal.explanation.duration_or_frequency}</dd>
              <dt className="text-[#806b3d]">如何撤回</dt><dd>{pendingDecision.proposal.explanation.how_to_revoke}</dd>
              <dt className="text-[#806b3d]">精确变更</dt>
              <dd>
                <ul className="space-y-1">
                  {pendingDecision.proposal.explanation.exact_changes.map((item) => <li key={item}>· {item}</li>)}
                </ul>
              </dd>
            </dl>
          ) : (
            <p className="mt-2 text-sm leading-6 text-[#6b716e]">{pendingConfirmation.reason}</p>
          )}
          {!canConfirm && (
            <p className="mt-4 border-l-2 border-[#a9925d] pl-3 text-sm text-[#6b624d]">
              当前身份不能作出这个决定。需要：
              {pendingConfirmation.allowed_roles.join("、")}。
            </p>
          )}
          <div className="mt-4 flex gap-3">
            <button type="button" disabled={busy || !canConfirm} onClick={() => void onConfirmation(pendingConfirmation.confirmation_id, true)} className="bg-[#1f5b51] px-4 py-2 text-sm text-white disabled:opacity-40">批准这个精确版本</button>
            <button type="button" disabled={busy || !canConfirm} onClick={() => void onConfirmation(pendingConfirmation.confirmation_id, false)} className="border border-[#d8d2c5] px-4 py-2 text-sm disabled:opacity-40">拒绝</button>
          </div>
        </div>
      )}

      {report && (
        <article className="mt-10 max-w-4xl border-l border-[#1f5b51] pl-6 md:pl-9">
          <div className="text-xs uppercase tracking-[0.16em] text-[#61716d]">本次任务产物</div>
          <h3 className="mt-3 text-2xl font-medium">{report.title}</h3>
          <p className="mt-5 whitespace-pre-line text-lg leading-8 text-[#34423f]">{report.content}</p>
          {completionCaveats(detail).map((item) => (
            <p key={item} className="mt-3 text-sm leading-6 text-[#77817e]">{item}</p>
          ))}
        </article>
      )}

      {detail.completion_receipt &&
        "safe_next_step" in detail.completion_receipt &&
        detail.completion_receipt.safe_next_step && (
        <aside
          aria-label="安全下一步"
          className="mt-9 max-w-4xl border-y border-[#a9925d] py-5"
        >
          <div className="text-xs uppercase tracking-[0.16em] text-[#806b3d]">
            安全下一步
          </div>
          <p className="mt-2 text-base leading-7 text-[#403d34]">
            {detail.completion_receipt.safe_next_step}
          </p>
        </aside>
      )}

      <DecisionJournal trace={trace} working={isWorking} />
    </section>
  );
}

function DecisionJournal({ trace, working }: { trace: RadarDecisionTrace | null; working: boolean }) {
  const entries = trace?.entries ?? [];
  return (
    <details className="mt-10 border-t border-[#d8d2c5] pt-5" open={working}>
      <summary className="flex cursor-pointer list-none items-center justify-between text-sm font-medium">
        <span className="flex items-center gap-2"><FileClock className="h-4 w-4 text-[#1f5b51]" />决策日志</span>
        <span className="text-xs font-normal text-[#78827f]">{entries.length} 条真实事件</span>
      </summary>
      <ol className="mt-5 divide-y divide-[#ddd8cd]">
        {entries.map((entry) => (
          <li key={`${entry.sequence}-${entry.event_type}`} className="grid gap-2 py-3 text-sm md:grid-cols-[150px_1fr_auto]">
            <span className="font-mono text-xs text-[#687571]">{eventLabel(entry.event_type)}</span>
            <span className="text-[#34423f]">
              <span className="block">{entry.summary}</span>
              {decisionDetail(entry) && (
                <span className="mt-1 block font-mono text-[11px] leading-5 text-[#74807c]">
                  {decisionDetail(entry)}
                </span>
              )}
            </span>
            <time className="text-xs text-[#8a9390]">{formatTime(entry.created_at)}</time>
          </li>
        ))}
        {!entries.length && <li className="py-4 text-sm text-[#78827f]">等待任务产生第一条可审计事件。</li>}
      </ol>
    </details>
  );
}

function HistoryView({
  history,
  onClose,
  onOpen,
}: {
  history: RadarTaskDetail[];
  onClose: () => void;
  onOpen: (detail: RadarTaskDetail) => void;
}) {
  return (
    <section className="py-10 md:py-14">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-medium">历史任务</h2>
        <button type="button" onClick={onClose} className="text-sm text-[#1f5b51]">返回启动页</button>
      </div>
      <div className="mt-7 divide-y divide-[#d8d2c5] border-y border-[#d8d2c5]">
        {history.map((item) => (
          <button key={item.task.task_id} type="button" onClick={() => onOpen(item)} className="grid w-full gap-2 py-5 text-left md:grid-cols-[1fr_auto_auto] md:items-center md:gap-8">
            <div>
              <div className="font-medium">{goalLabel(item)}</div>
              <div className="mt-1 text-xs text-[#78827f]">{goalDate(item)} · {shortId(item.task.task_id)}</div>
            </div>
            <span className="text-sm text-[#56635f]">{executionMode(item)}</span>
            <span className="text-sm text-[#56635f]">{item.task.completion_status ?? item.task.status}</span>
          </button>
        ))}
        {!history.length && <div className="py-8 text-sm text-[#78827f]">暂无已完成任务。</div>}
      </div>
    </section>
  );
}

function LiveDatum({ label, value }: { label: string; value: string }) {
  return <div><div className="text-xs text-[#78827f]">{label}</div><div className="mt-1 text-lg font-medium tabular-nums">{value}</div></div>;
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="mb-5 block"><span className="mb-2 block text-xs text-[#687571]">{label}</span>{children}</label>;
}

const inputClass = "w-full border-0 border-b border-[#bdb8ad] bg-transparent px-0 py-3 text-sm outline-none placeholder:text-[#9aa19f] focus:border-[#1f5b51]";

function usesTargetDate(type: RadarGoalType) {
  return ["night_review", "data_quality_diagnosis", "doctor_material"].includes(type);
}

function usesRange(type: RadarGoalType) {
  return ["trend_comparison", "change_explanation"].includes(type);
}

function launcherReady(props: {
  goalType: RadarGoalType;
  targetDate: string;
  rangeStart: string;
  rangeEnd: string;
  question: string;
  sourceArtifactId: string;
  sourceDate: string;
}) {
  if (usesTargetDate(props.goalType)) return Boolean(props.targetDate);
  if (usesRange(props.goalType)) return Boolean(props.rangeStart && props.rangeEnd && props.rangeStart <= props.rangeEnd);
  return Boolean(props.question.trim() && props.sourceArtifactId.trim() && props.sourceDate);
}

type RadarTaskResultView = {
  title: string;
  content: string;
};

function latestTaskResult(detail: RadarTaskDetail | null): RadarTaskResultView | null {
  if (!detail) return null;
  const reports = detail.artifacts.filter((item) => item.report).sort((a, b) => b.version - a.version);
  const legacy = reports[0]?.report;
  if (legacy) return { title: legacy.title, content: legacy.content };
  const productArtifacts = detail.artifacts
    .filter((item) => item.artifact_type === "product_episode_result")
    .sort((a, b) => b.version - a.version);
  const publication = productArtifacts[0]?.payload.publication;
  if (!isRecord(publication) || typeof publication.text !== "string") return null;
  return {
    title: goalLabel(detail),
    content: publication.text,
  };
}

function completionCaveats(detail: RadarTaskDetail): string[] {
  const receipt = detail.completion_receipt;
  if (!receipt) return [];
  if ("caveats" in receipt) return receipt.caveats;
  return receipt.failure_codes.map((code) => `本次执行边界：${code}`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function taskStatus(detail: RadarTaskDetail) {
  const labels: Record<string, string> = {
    created: "任务已创建",
    running: "Agent 正在执行",
    waiting_for_user_input: "等待你的补充",
    waiting_for_confirmation: "材料已生成，等待动作确认",
    completed: "本次任务已完成",
    failed: "本次任务未完成",
  };
  return labels[detail.task.status] ?? detail.task.status;
}

function executionMode(detail: RadarTaskDetail) {
  if (detail.task.execution_mode === "intelligent") return "智能执行";
  if (detail.task.execution_mode === "safe_degraded") return "安全降级执行";
  if (detail.task.execution_mode === "deterministic_only") return "确定性安全执行";
  if (detail.task.execution_mode === "legacy_fixed") return "旧版固定流程";
  return "尚未确定执行模式";
}

function goalLabel(detail: RadarTaskDetail) {
  const type = detail.task.goal_payload?.goal_type;
  return goals.find((item) => item.type === type)?.label ?? "睡眠观察任务";
}

function goalDate(detail: RadarTaskDetail) {
  const goal = detail.task.goal_payload;
  if (!goal) return "未指定";
  if (typeof goal.target_date === "string") return goal.target_date;
  if (typeof goal.range_start === "string" && typeof goal.range_end === "string") return `${goal.range_start} 至 ${goal.range_end}`;
  if (typeof goal.source_date === "string") return goal.source_date;
  return "未指定";
}

function launcherActionLabel(props: {
  goalType: RadarGoalType;
  targetDate: string;
  rangeStart: string;
  rangeEnd: string;
  sourceDate: string;
}) {
  const goal = goals.find((item) => item.type === props.goalType)?.label ?? "睡眠观察";
  if (usesTargetDate(props.goalType)) return `启动 ${props.targetDate || "所选日期"} · ${goal}`;
  if (usesRange(props.goalType)) return `启动 ${props.rangeStart || "开始日期"} 至 ${props.rangeEnd || "结束日期"} · ${goal}`;
  return `启动 ${props.sourceDate || "所选报告日期"} · ${goal}`;
}

function ModeIndicator({ detail }: { detail: RadarTaskDetail }) {
  const intelligent = detail.task.execution_mode === "intelligent";
  const degraded = detail.task.execution_mode === "safe_degraded";
  return (
    <span
      data-execution-mode={detail.task.execution_mode ?? "pending"}
      className={cn(
        "inline-flex items-center border px-2 py-0.5 text-xs font-medium",
        intelligent && "border-[#1f5b51] bg-[#e0ebe7] text-[#173f38]",
        degraded && "border-[#a9925d] bg-[#f3ecdc] text-[#765f2e]",
        !intelligent && !degraded && "border-[#c8c5bc] text-[#687571]",
      )}
    >
      {executionMode(detail)}
    </span>
  );
}

function eventLabel(type: string) {
  const labels: Record<string, string> = {
    "goal.accepted": "目标确认",
    "plan.created": "计划生成",
    "plan.revised": "计划调整",
    "plan.evaluated": "完成评估",
    "plan.rejected": "计划被策略拒绝",
    "plan.step_reused": "复用已验证结果",
    "plan.step_skipped": "跳过不需要的能力",
    "agent.started": "Agent 启动",
    "agent.completed": "Agent 完成",
    "agent.invocation_started": "Agent 启动",
    "agent.invocation_completed": "Agent 完成",
    "agent.invocation_failed": "Agent 调用失败",
    "tool.started": "工具启动",
    "tool.completed": "工具完成",
    "tool.invocation_started": "工具启动",
    "tool.invocation_completed": "工具完成",
    "tool.invocation_failed": "工具调用失败",
    "a2a.requested": "发起协作请求",
    "a2a.accepted": "协作接受",
    "a2a.handled": "协作完成",
    "a2a.rejected": "协作请求未执行",
    "a2a.resolved": "协作完成",
    "execution.degraded": "执行降级",
    "execution.preflight_completed": "证据边界检查完成",
    "execution.postflight_completed": "发布安全门完成",
    "execution.interrupted": "安全边界中断",
    "execution.budget_exhausted": "执行预算已耗尽",
    "task.partial": "任务部分完成",
    "task.blocked": "任务已阻断",
    "confirmation.action_completed": "已执行确认动作",
    "confirmation.resolved": "人工确认已解决",
    "user_input.requested": "请求补充",
    "user_input.received": "补充已接收",
    "user_input.decline_submitted": "已选择不补充",
    "user_input.declined": "补充已拒绝",
  };
  return labels[type] ?? type;
}

function decisionDetail(entry: RadarDecisionTraceEntry) {
  const details = entry.details;
  if (Array.isArray(details.capabilities)) return `capabilities: ${details.capabilities.join(" → ")}`;
  if (typeof details.agent === "string") return `agent: ${details.agent}${typeof details.capability === "string" ? ` · ${details.capability}` : ""}`;
  if (typeof details.tool_name === "string") return `tool: ${details.tool_name}${typeof details.capability === "string" ? ` · ${details.capability}` : ""}`;
  if (typeof details.sender === "string" && typeof details.receiver === "string") {
    const changed = Array.isArray(details.changed_fields) ? ` · changed: ${details.changed_fields.join(", ") || "none"}` : "";
    return `${details.sender} → ${details.receiver}${changed}`;
  }
  if (typeof details.decision === "string") return `decision: ${details.decision}${typeof details.checkpoint_kind === "string" ? ` · checkpoint: ${details.checkpoint_kind}` : ""}`;
  if (typeof details.skip_reason === "string") return `skip: ${details.skip_reason}`;
  if (typeof details.reason_code === "string") return `reason: ${details.reason_code}`;
  if (typeof details.action_type === "string") return `confirmed action: ${details.action_type}`;
  return "";
}

function deviceStatus(device: RadarPublicDevice | null) {
  if (!device) return "未连接";
  return device.status === "online" ? "在线" : device.status === "offline" ? "离线" : "未知";
}

function bedPresence(realtime: RadarRealtimeState | null) {
  const value = realtime?.latest_snapshot?.bed_presence;
  return value === "in_bed" ? "在床" : value === "out_of_bed" ? "离床" : "未确认";
}

function vital(value: number | null | undefined, unit: string) {
  return value == null ? "暂无" : `${value} ${unit}`;
}

function formatTime(value: string | null | undefined) {
  if (!value) return "暂无";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
}

function shortId(value: string) {
  return `…${value.slice(-8)}`;
}

function previousDate() {
  return daysBefore(1);
}

function daysBefore(days: number) {
  const value = new Date();
  value.setDate(value.getDate() - days);
  return value.toISOString().slice(0, 10);
}

function messageOf(cause: unknown) {
  return cause instanceof Error ? cause.message : "请求未完成，请稍后重试。";
}
