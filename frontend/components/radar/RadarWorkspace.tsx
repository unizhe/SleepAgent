"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import {
  AlertTriangle,
  ArrowDownRight,
  BedDouble,
  Check,
  ChevronDown,
  CircleDot,
  ClipboardCheck,
  Download,
  FileHeart,
  HeartHandshake,
  Info,
  Loader2,
  MessageCircle,
  Radio,
  RefreshCw,
  Send,
  ShieldCheck,
  Sparkles,
  Stethoscope,
  PhoneCall,
  Users,
  X,
} from "lucide-react";
import {
  askRadarTask,
  buildRadarTaskIdempotencyKey,
  createRadarTask,
  getRadarTask,
  getRadarTaskEvents,
  resolveRadarConfirmation,
  runRadarTask,
  subscribeRadarTask,
} from "@/lib/radar-api";
import {
  defaultRadarReplayScenarioId,
  getRadarReplayScenario,
  localizedReplayScenarioTitle,
  localizedReplayTrendSummary,
  radarReplayScenarios,
} from "@/lib/radar-replay-scenarios";
import type {
  RadarAgentRole,
  RadarEvidenceLedger,
  RadarHumanConfirmation,
  RadarReplayRiskLevel,
  RadarRoleReport,
  RadarTaskChatResponse,
  RadarTaskDetail,
  RadarTaskStreamEvent,
} from "@/lib/radar-types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";
import { DynamicRadarWorkspace } from "@/components/radar/DynamicRadarWorkspace";

const roleMeta: Record<
  RadarAgentRole,
  { label: string; short: string; icon: typeof HeartHandshake }
> = {
  elder: { label: "老人视图", short: "今天怎么做", icon: HeartHandshake },
  family: { label: "家属视图", short: "趋势与照护", icon: Users },
  doctor: { label: "医生材料", short: "证据与导出", icon: Stethoscope },
};

const terminalEvents = new Set(["task.completed", "task.failed"]);

export function RadarWorkspace() {
  return <DynamicRadarWorkspace />;
}

function LegacyFixedRadarWorkspace() {
  const prefersReducedMotion = useReducedMotion();
  const [scenarioId, setScenarioId] = useState(defaultRadarReplayScenarioId);
  const [role, setRole] = useState<RadarAgentRole>("family");
  const [detail, setDetail] = useState<RadarTaskDetail | null>(null);
  const [events, setEvents] = useState<RadarTaskStreamEvent[]>([]);
  const [runAttempt, setRunAttempt] = useState(0);
  const [phase, setPhase] = useState<"creating" | "running" | "ready" | "error">(
    "creating",
  );
  const [error, setError] = useState<string | null>(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [safetyOpen, setSafetyOpen] = useState(false);
  const [chatResponse, setChatResponse] = useState<RadarTaskChatResponse | null>(null);
  const [busyConfirmation, setBusyConfirmation] = useState<string | null>(null);
  const closeStream = useRef<null | (() => void)>(null);

  const scenario = useMemo(() => getRadarReplayScenario(scenarioId), [scenarioId]);
  const taskScenario = detail?.replay_scenario ?? scenario;
  const ledger = useMemo(() => latestLedger(detail), [detail]);
  const report = useMemo(() => reportForRole(detail, role), [detail, role]);
  const qualityReport = useMemo(
    () => reportForRole(detail, "family") ?? report,
    [detail, report],
  );

  useEffect(() => {
    let active = true;
    closeStream.current?.();
    setDetail(null);
    setEvents([]);
    setChatResponse(null);
    setError(null);
    setPhase("creating");

    async function startTask() {
      try {
        const created = await createRadarTask({
          scenarioId,
          idempotencyKey: buildRadarTaskIdempotencyKey(
            scenarioId,
            `dashboard-${runAttempt}`,
          ),
        });
        if (!active) return;
        setDetail(created);
        setPhase("running");
        closeStream.current = subscribeRadarTask(created.task.task_id, {
          onEvent: (event) => {
            if (!active) return;
            setEvents((current) => mergeEvents(current, [event]));
            if (terminalEvents.has(event.event_type)) {
              setPhase(event.event_type === "task.completed" ? "ready" : "error");
            }
          },
        });
        const completed = await runRadarTask(created.task.task_id);
        const history = await getRadarTaskEvents(created.task.task_id);
        if (!active) return;
        setDetail(completed);
        setEvents((current) => mergeEvents(current, history));
        setPhase(completed.task.status === "completed" ? "ready" : "error");
        if (completed.task.status === "completed") closeStream.current?.();
      } catch (cause) {
        if (!active) return;
        setError(toMessage(cause));
        setPhase("error");
        closeStream.current?.();
      }
    }

    void startTask();
    return () => {
      active = false;
      closeStream.current?.();
    };
  }, [scenarioId, runAttempt]);

  async function handleConfirmation(item: RadarHumanConfirmation, approved: boolean) {
    if (!detail) return false;
    setBusyConfirmation(item.confirmation_id);
    try {
      await resolveRadarConfirmation({
        taskId: detail.task.task_id,
        confirmationId: item.confirmation_id,
        approved,
      });
      setDetail(await getRadarTask(detail.task.task_id));
      return true;
    } catch (cause) {
      setError(toMessage(cause));
      return false;
    } finally {
      setBusyConfirmation(null);
    }
  }

  async function handleDoctorExport() {
    if (!detail) return;
    const confirmation = detail.confirmations.find(
      (item) => item.action_type === "export_doctor_material",
    );
    if (!confirmation || !["pending", "approved"].includes(confirmation.status)) {
      setError("医生材料导出需要有效的家属确认请求。");
      return;
    }
    if (confirmation?.status === "pending") {
      const approved = await handleConfirmation(confirmation, true);
      if (!approved) return;
    }
    const doctorReport = reportForRole(detail, "doctor");
    if (doctorReport) downloadDoctorMaterial(doctorReport, ledger);
  }

  return (
    <main className="min-h-screen bg-[#f4f1e9] text-slate-900">
      <header className="border-b border-[#ded8ca] bg-[#fbfaf6]">
        <div className="mx-auto flex max-w-[1440px] flex-wrap items-center justify-between gap-4 px-4 py-4 md:px-8">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-md bg-[#285d54] text-white shadow-sm">
              <Radio className="h-5 w-5" />
            </div>
            <div>
              <div className="text-base font-semibold tracking-tight">SleepAgent 雷达</div>
              <div className="text-xs text-slate-500">家庭睡眠观察与照护协同</div>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <label className="sr-only" htmlFor="radar-scenario">
              回放场景
            </label>
            <select
              id="radar-scenario"
              value={scenarioId}
              onChange={(event) => setScenarioId(event.target.value)}
              className="h-10 max-w-[240px] rounded-md border border-[#d8d2c4] bg-white px-3 text-sm outline-none focus:border-[#386f65]"
            >
              {radarReplayScenarios.map((item) => (
                <option key={item.scenario_id} value={item.scenario_id}>
                  {item.title}
                </option>
              ))}
            </select>
            <Button
              variant="outline"
              onClick={() => setRunAttempt((value) => value + 1)}
              disabled={phase === "creating" || phase === "running"}
            >
              <RefreshCw className="h-4 w-4" />
              重新分析
            </Button>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-[1440px] px-4 py-5 md:px-8 md:py-7">
        <TaskHeader detail={detail} phase={phase} scenarioTitle={localizedReplayScenarioTitle(taskScenario.scenario_id)} />
        <QualitySummaryBar quality={qualityReport?.data_quality} />

        <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-b border-[#d8d2c4] pb-3">
          <div className="flex gap-1" role="tablist" aria-label="报告角色">
            {(Object.keys(roleMeta) as RadarAgentRole[]).map((item) => {
              const Icon = roleMeta[item].icon;
              return (
                <button
                  key={item}
                  type="button"
                  role="tab"
                  aria-selected={role === item}
                  onClick={() => setRole(item)}
                  className={cn(
                    "flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition",
                    role === item
                      ? "bg-[#234f48] text-white shadow-sm"
                      : "text-slate-600 hover:bg-[#ebe7dc] hover:text-slate-900",
                  )}
                >
                  <Icon className="h-4 w-4" />
                  <span>{roleMeta[item].label}</span>
                  <span className={cn("hidden text-xs md:inline", role === item ? "text-white/70" : "text-slate-400")}>
                    {roleMeta[item].short}
                  </span>
                </button>
              );
            })}
          </div>
          <Button
            variant="outline"
            onClick={() => setChatOpen(true)}
            disabled={!detail || detail.task.status !== "completed"}
          >
            <MessageCircle className="h-4 w-4" />
            问问报告
          </Button>
        </div>

        {error && <ErrorNotice message={error} />}

        {detail?.risk_level === "urgent_boundary" && <UrgentBoundaryBanner />}

        <motion.div
          key={`${role}-${detail?.task.task_id ?? "loading"}`}
          initial={prefersReducedMotion ? false : { opacity: 0, y: 8 }}
          animate={prefersReducedMotion ? undefined : { opacity: 1, y: 0 }}
          transition={{ duration: 0.25 }}
          className="mt-5"
        >
          {!detail || phase === "creating" || phase === "running" ? (
            <LoadingDashboard detail={detail} />
          ) : role === "elder" ? (
            <ElderView detail={detail} report={report} />
          ) : role === "family" ? (
            <FamilyView
              detail={detail}
              report={report}
              scenario={taskScenario}
              busyConfirmation={busyConfirmation}
              onConfirmation={handleConfirmation}
            />
          ) : (
            <DoctorView
              detail={detail}
              report={report}
              ledger={ledger}
              scenario={taskScenario}
              busyConfirmation={busyConfirmation}
              onExport={handleDoctorExport}
            />
          )}
        </motion.div>

        <AgentProgress detail={detail} events={events} />
        <SafetyFootnote onOpen={() => setSafetyOpen(true)} />
      </div>

      <ChatPanel
        open={chatOpen}
        onClose={() => setChatOpen(false)}
        detail={detail}
        role={role}
        response={chatResponse}
        onResponse={setChatResponse}
      />
      <SafetyDisclosure open={safetyOpen} onClose={() => setSafetyOpen(false)} />
    </main>
  );
}

function TaskHeader({
  detail,
  phase,
  scenarioTitle,
}: {
  detail: RadarTaskDetail | null;
  phase: "creating" | "running" | "ready" | "error";
  scenarioTitle: string;
}) {
  const risk = detail?.risk_level ?? "info";
  return (
    <section className="flex flex-wrap items-start justify-between gap-4">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={riskVariant(risk)}>{riskLabel(risk)}</Badge>
          <Badge variant="neutral">{scenarioTitle}</Badge>
          <Badge variant={phase === "error" ? "danger" : phase === "ready" ? "default" : "info"}>
            {phaseLabel(phase)}
          </Badge>
        </div>
        <h1 className="mt-3 text-2xl font-semibold tracking-tight md:text-3xl">
          昨夜睡眠观察
        </h1>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-600">
          基于当前任务的证据台账与角色报告，持续整理昨夜观察和照护动作。
        </p>
      </div>
      {detail && (
        <div className="text-right text-xs leading-5 text-slate-500">
          <div>任务标识 {displayIdentifier(detail.task.task_id)}</div>
          <div>追踪标识 {displayIdentifier(detail.task.trace_id)}</div>
        </div>
      )}
    </section>
  );
}

function ElderView({ detail, report }: { detail: RadarTaskDetail; report: RadarRoleReport | null }) {
  const risk = detail.risk_level ?? "info";
  const primaryObservation = localizedGeneratedText(report?.structured_summary.primary_observation, elderObservation(risk));
  const primarySuggestion = localizedGeneratedText(report?.structured_summary.primary_suggestion, elderSuggestion(risk));
  return (
    <section className="grid gap-5 lg:grid-cols-[minmax(0,1.35fr)_minmax(280px,.65fr)]">
      <Card className="border-[#d7d1c3] bg-[#fffdf8] p-6 md:p-8">
        <div className="flex items-center gap-2 text-sm font-medium text-[#285d54]">
          <Sparkles className="h-4 w-4" />
          今天的状态
        </div>
        <h2 className="mt-5 max-w-3xl text-3xl font-semibold leading-tight tracking-tight md:text-4xl">
          {elderHeadline(risk)}
        </h2>
        <p className="mt-5 max-w-3xl text-lg leading-8 text-slate-700">{primaryObservation}</p>
        {risk === "urgent_boundary" && (
          <div className="mt-6 rounded-md border border-rose-200 bg-rose-50 p-4 text-base font-medium leading-7 text-rose-800">
            当前描述涉及急症边界，请及时寻求线下医疗或急救评估。
          </div>
        )}
      </Card>
      <Card className="border-[#cbd9d4] bg-[#eaf2ee] p-6 md:p-7">
        <div className="flex h-11 w-11 items-center justify-center rounded-md bg-[#285d54] text-white">
          <HeartHandshake className="h-5 w-5" />
        </div>
        <div className="mt-5 text-xs font-semibold uppercase tracking-[0.16em] text-[#47746b]">
          一个主要建议
        </div>
        <p className="mt-3 text-xl font-semibold leading-8 text-[#173d37]">{primarySuggestion}</p>
        <p className="mt-5 text-sm leading-6 text-[#48665f]">
          如不适明显，请让家人协助联系医生。
        </p>
      </Card>
    </section>
  );
}

function FamilyView({
  detail,
  report,
  scenario,
  busyConfirmation,
  onConfirmation,
}: {
  detail: RadarTaskDetail;
  report: RadarRoleReport | null;
  scenario: ReturnType<typeof getRadarReplayScenario>;
  busyConfirmation: string | null;
  onConfirmation: (item: RadarHumanConfirmation, approved: boolean) => Promise<boolean>;
}) {
  const quality = report?.data_quality;
  const pending = detail.confirmations.filter((item) => item.status === "pending");
  return (
    <section className="grid gap-5 xl:grid-cols-[minmax(0,1.25fr)_minmax(320px,.75fr)]">
      <div className="grid gap-5">
        <div className="grid gap-4 sm:grid-cols-3">
          <FamilyRiskCard risk={detail.risk_level} report={report} />
          <MetricCard label="数据覆盖" value={percent(quality?.coverage_ratio)} detail={qualityLabel(quality?.status)} tone={quality?.status === "unusable" ? "danger" : quality?.status === "partial" ? "warn" : "good"} />
          <MetricCard label="待确认动作" value={`${pending.length} 项`} detail="外发与长期写入需确认" tone={pending.length ? "warn" : "good"} />
        </div>

        <Card className="border-[#d7d1c3] bg-[#fffdf8] p-5">
          <SectionTitle icon={ArrowDownRight} title="近 7 天变化" subtitle="来自当前回放任务绑定的标准化趋势摘要" />
          <TrendChart points={scenario.deterministic_input.trend_summary} />
          <div className="mt-4 grid gap-2 md:grid-cols-2">
            {(report?.trend_highlights ?? []).slice(0, 4).map((item, index) => (
              <div key={item} className="rounded-md bg-[#f1eee5] px-3 py-2 text-sm leading-6 text-slate-700">
                {localizedGeneratedText(item, localizedReplayTrendSummary(scenario.scenario_id, index))}
              </div>
            ))}
          </div>
        </Card>

        <Card className="border-[#d7d1c3] bg-[#fffdf8] p-5">
          <SectionTitle icon={ClipboardCheck} title="补充问卷" subtitle="题目来自版本化策略，Agent 不临场编造医学问题" />
          <div className="mt-4 grid gap-3 md:grid-cols-2">
            {detail.questionnaire_candidates.length ? (
              detail.questionnaire_candidates.slice(0, 3).map((item, index) => (
                <div key={item.question_id} className="flex gap-3 rounded-md border border-[#e2ddd1] bg-white p-3">
                  <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-[#e6efeb] text-xs font-semibold text-[#285d54]">
                    {index + 1}
                  </span>
                  <div>
                    <div className="text-sm font-medium">{localizedGeneratedText(item.prompt_text, questionnairePrompt(item.question_id))}</div>
                    <div className="mt-1 text-xs text-slate-500">
                      {questionSourceLabel(item.question_source)} · 版本 {displayVersion(item.source_version)} · 等待家属补充
                    </div>
                  </div>
                </div>
              ))
            ) : (
              <EmptyLine text="当前任务无需追加问卷。" />
            )}
          </div>
        </Card>
      </div>

      <div className="grid content-start gap-5">
        <Card className="border-[#d7d1c3] bg-[#fffdf8] p-5">
          <SectionTitle icon={ShieldCheck} title="数据质量" subtitle="质量不足时系统会少说，并停止过度解释" />
          <QualityRows quality={quality} />
        </Card>

        <Card className="border-[#d7d1c3] bg-[#fffdf8] p-5">
          <SectionTitle icon={Check} title="需要确认" subtitle="确认只授权动作，不会改变证据台账" />
          <div className="mt-4 grid gap-3">
            {pending.length ? (
              pending.map((item) => (
                <div key={item.confirmation_id} className="rounded-md border border-[#e2ddd1] bg-white p-3">
                  <div className="text-sm font-medium">{actionLabel(item.action_type)}</div>
                  <p className="mt-1 text-xs leading-5 text-slate-500">该动作执行前需要获得对应角色确认。</p>
                  <div className="mt-3 flex gap-2">
                    <Button size="sm" onClick={() => void onConfirmation(item, true)} disabled={busyConfirmation === item.confirmation_id}>
                      {busyConfirmation === item.confirmation_id && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                      确认
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => void onConfirmation(item, false)} disabled={busyConfirmation === item.confirmation_id}>
                      暂不执行
                    </Button>
                  </div>
                </div>
              ))
            ) : (
              <EmptyLine text="没有待确认动作。" />
            )}
          </div>
        </Card>
      </div>
    </section>
  );
}

function DoctorView({
  detail,
  report,
  ledger,
  scenario,
  busyConfirmation,
  onExport,
}: {
  detail: RadarTaskDetail;
  report: RadarRoleReport | null;
  ledger: RadarEvidenceLedger | null;
  scenario: ReturnType<typeof getRadarReplayScenario>;
  busyConfirmation: string | null;
  onExport: () => Promise<void>;
}) {
  const exportConfirmation = detail.confirmations.find(
    (item) => item.action_type === "export_doctor_material",
  );
  return (
    <section className="grid gap-5 xl:grid-cols-[minmax(0,1.3fr)_minmax(320px,.7fr)]">
      <Card className="border-[#d7d1c3] bg-[#fffdf8] p-5 md:p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <SectionTitle icon={FileHeart} title="结构化证据链" subtitle={ledger ? "当前任务证据台账已发布" : "证据台账尚未发布"} />
          <Button
            onClick={() => void onExport()}
            disabled={
              !report ||
              !exportConfirmation ||
              !["pending", "approved"].includes(exportConfirmation.status) ||
              busyConfirmation === exportConfirmation.confirmation_id
            }
          >
            {busyConfirmation === exportConfirmation?.confirmation_id ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Download className="h-4 w-4" />
            )}
            {exportConfirmation?.status === "approved"
              ? "导出材料"
              : exportConfirmation?.status === "pending"
                ? "确认并导出"
                : "需家属确认"}
          </Button>
        </div>
        <div className="mt-5 grid gap-3">
          {(ledger?.claims ?? []).map((claim, index) => (
            <article key={claim.claim_id} className="grid gap-3 rounded-md border border-[#e2ddd1] bg-white p-4 md:grid-cols-[32px_1fr_auto]">
              <span className="flex h-8 w-8 items-center justify-center rounded-md bg-[#e7efec] text-xs font-semibold text-[#285d54]">
                {index + 1}
              </span>
              <div>
                <div className="text-sm font-medium leading-6">{localizedClaimText(claim, index)}</div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {claim.evidence_refs.slice(0, 4).map((ref, refIndex) => (
                    <code key={ref} className="rounded bg-[#f1eee5] px-2 py-1 text-[11px] text-slate-600">
                      证据引用 {refIndex + 1}
                    </code>
                  ))}
                </div>
                {claim.caveats[0] && <p className="mt-2 text-xs leading-5 text-slate-500">{localizedGeneratedText(claim.caveats[0], localizedClaimCaveat(claim))}</p>}
              </div>
              <div className="text-right text-xs text-slate-500">
                <div>可信度 {Math.round(claim.confidence * 100)}%</div>
                <div className="mt-1">{reviewStatusLabel(claim.review_status)}</div>
              </div>
            </article>
          ))}
        </div>
      </Card>

      <div className="grid content-start gap-5">
        <Card className="border-[#d7d1c3] bg-[#fffdf8] p-5">
          <SectionTitle icon={ArrowDownRight} title="趋势摘要" subtitle="7 天趋势及当前夜间质量" />
          <div className="mt-4 grid gap-2">
            {(report?.trend_highlights ?? []).slice(0, 4).map((item) => (
              <div key={item} className="border-l-2 border-[#47746b] pl-3 text-sm leading-6 text-slate-700">{item}</div>
            ))}
            {!report?.trend_highlights.length && <EmptyLine text={`${scenario.deterministic_input.trend_summary.length} 个趋势点，暂无可审阅变化。`} />}
          </div>
        </Card>
        <Card className="border-[#d7d1c3] bg-[#fffdf8] p-5">
          <SectionTitle icon={ShieldCheck} title="质量与问卷" subtitle="医生材料保留不确定性和输入缺口" />
          <QualityRows quality={report?.data_quality} />
          <div className="mt-4 border-t border-[#e2ddd1] pt-4 text-sm text-slate-600">
            问卷记录：{report?.questionnaire_entries.length ?? 0} 条；候选：{detail.questionnaire_candidates.length} 项
          </div>
        </Card>
        <Card className="border-[#d7d1c3] bg-[#fffdf8] p-5">
          <SectionTitle icon={Info} title="完整注意事项" subtitle="随医生材料保留，不因页面简化而省略" />
          <ul className="mt-4 max-h-64 space-y-2 overflow-y-auto pr-2 text-xs leading-5 text-slate-600">
            {(report?.caveats ?? []).map((item, index) => (
              <li key={`caveat-${item}`} className="border-l-2 border-[#c8b98f] pl-3">{localizedGeneratedText(item, doctorCaveat(index, report))}</li>
            ))}
            {(report?.safety_notices ?? []).map((item, index) => (
              <li key={`notice-${item}`} className="border-l-2 border-[#7b9b93] pl-3">{localizedGeneratedText(item, doctorSafetyNotice(index))}</li>
            ))}
          </ul>
        </Card>
        <div className="rounded-md border border-[#dfd5be] bg-[#f5efe1] p-4 text-xs leading-5 text-[#6d5a34]">
          医生材料仅作异步审阅参考，保留证据来源、数据质量、注意事项与非诊断边界。
        </div>
      </div>
    </section>
  );
}

function AgentProgress({ detail, events }: { detail: RadarTaskDetail | null; events: RadarTaskStreamEvent[] }) {
  const statuses = Object.values(detail?.task.node_status ?? {});
  const completed = statuses.filter((status) => status === "succeeded" || status === "skipped").length;
  const progress = statuses.length ? (completed / statuses.length) * 100 : 0;
  return (
    <details className="mt-5 rounded-md border border-[#d8d2c4] bg-[#faf8f2] px-4 py-3 text-sm text-slate-600">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-4">
        <span className="flex items-center gap-2 font-medium text-slate-700">
          <CircleDot className="h-4 w-4 text-[#47746b]" />
          分析协作已完成 {completed}/{statuses.length || 14}
        </span>
        <span className="flex items-center gap-2 text-xs text-slate-500">
          多 Agent 分析过程
          <ChevronDown className="h-4 w-4" />
        </span>
      </summary>
      <Progress value={progress} className="mt-3" />
      <div className="mt-3 grid gap-1.5 md:grid-cols-2 xl:grid-cols-3">
        {events.slice(-9).map((event) => (
          <div key={event.event_id} className="truncate rounded bg-white px-2.5 py-2 text-xs">
            {eventDisplayMessage(event)}
          </div>
        ))}
      </div>
    </details>
  );
}

function ChatPanel({
  open,
  onClose,
  detail,
  role,
  response,
  onResponse,
}: {
  open: boolean;
  onClose: () => void;
  detail: RadarTaskDetail | null;
  role: RadarAgentRole;
  response: RadarTaskChatResponse | null;
  onResponse: (response: RadarTaskChatResponse | null) => void;
}) {
  const [message, setMessage] = useState("请用当前证据解释昨晚最需要关注的变化。");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!open) return null;

  async function ask() {
    if (!detail || !message.trim()) return;
    setBusy(true);
    setError(null);
    try {
      onResponse(await askRadarTask({ taskId: detail.task.task_id, role, message: message.trim() }));
    } catch (cause) {
      setError(toMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50">
      <button type="button" aria-label="关闭对话" onClick={onClose} className="absolute inset-0 bg-black/20" />
      <aside className="absolute bottom-0 right-0 top-0 flex w-full max-w-lg flex-col border-l border-[#d8d2c4] bg-[#fbfaf6] shadow-2xl">
        <div className="flex items-start justify-between border-b border-[#ded8ca] p-5">
          <div>
            <div className="flex items-center gap-2 font-semibold"><MessageCircle className="h-4 w-4 text-[#285d54]" />问问当前报告</div>
            <p className="mt-1 text-xs leading-5 text-slate-500">辅助解释入口；只读当前证据台账与已审阅知识检索结果。</p>
          </div>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="关闭"><X className="h-4 w-4" /></Button>
        </div>
        <div className="flex-1 overflow-y-auto p-5">
          {response ? (
            <div className="rounded-md border border-[#d4dfda] bg-[#edf4f1] p-4 text-sm leading-7 text-slate-800">
              {localizedGeneratedText(response.answer, localizedChatAnswer(detail))}
              <div className="mt-4 border-t border-[#cddbd5] pt-3 text-xs leading-5 text-slate-500">
                证据引用 {response.evidence_refs.length} 条 · 知识引用 {response.rag_citation_refs.length} 条 · 事实未被修改
              </div>
            </div>
          ) : (
            <div className="rounded-md border border-dashed border-[#d8d2c4] p-4 text-sm leading-6 text-slate-500">对话不会重跑任务或改写风险等级。</div>
          )}
          {error && <ErrorNotice message={error} />}
        </div>
        <div className="border-t border-[#ded8ca] p-4">
          <textarea value={message} onChange={(event) => setMessage(event.target.value)} className="min-h-24 w-full resize-none rounded-md border border-[#d8d2c4] bg-white p-3 text-sm leading-6 outline-none focus:border-[#47746b]" />
          <Button className="mt-3 w-full" onClick={() => void ask()} disabled={busy || !detail}>
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
            基于当前证据回答
          </Button>
        </div>
      </aside>
    </div>
  );
}

function LoadingDashboard({ detail }: { detail: RadarTaskDetail | null }) {
  const statuses = Object.values(detail?.task.node_status ?? {});
  const completed = statuses.filter((status) => status === "succeeded").length;
  return (
    <Card className="border-[#d7d1c3] bg-[#fffdf8] p-8 md:p-12">
      <div className="mx-auto max-w-xl text-center">
        <Loader2 className="mx-auto h-8 w-8 animate-spin text-[#386f65]" />
        <h2 className="mt-5 text-xl font-semibold">正在整理昨夜证据</h2>
        <p className="mt-2 text-sm leading-6 text-slate-500">质量门控、趋势、风险和三角色报告会依次完成。</p>
        <Progress value={statuses.length ? (completed / statuses.length) * 100 : 8} className="mt-6" />
      </div>
    </Card>
  );
}

function QualitySummaryBar({
  quality,
}: {
  quality: RadarRoleReport["data_quality"] | undefined;
}) {
  const intervals = quality?.out_of_bed_intervals ?? [];
  const notInBed = quality?.not_in_bed_intervals ?? [];
  const items = [
    { label: "设备状态", value: deviceStatusLabel(quality?.device_status) },
    { label: "覆盖率", value: percent(quality?.coverage_ratio) },
    { label: "无效读数", value: countValue(quality?.invalid_reading_count) },
    { label: "离床时段", value: intervalSummary(intervals) },
    { label: "不在床时段", value: intervalSummary(notInBed) },
  ];
  return (
    <section
      aria-label="雷达数据质量摘要"
      className="mt-5 grid gap-px overflow-hidden rounded-md border border-[#d8d2c4] bg-[#d8d2c4] sm:grid-cols-2 xl:grid-cols-5"
    >
      {items.map((item) => (
        <div key={item.label} className="min-w-0 bg-[#fbfaf6] px-3 py-2.5">
          <div className="text-[11px] text-slate-500">{item.label}</div>
          <div className="mt-1 truncate text-sm font-medium text-slate-800" title={item.value}>
            {item.value}
          </div>
        </div>
      ))}
    </section>
  );
}

function FamilyRiskCard({
  risk,
  report,
}: {
  risk: RadarReplayRiskLevel | null;
  report: RadarRoleReport | null;
}) {
  const highlighted = risk === "watch" || risk === "escalate";
  const source = report?.source_refs[0] ?? report?.evidence_refs[0];
  const caveat = report?.caveats[0] ?? "风险线索用于连续观察，不构成临床诊断。";
  return (
    <Card
      className={cn(
        "p-4",
        highlighted
          ? risk === "escalate"
            ? "border-rose-200 bg-rose-50"
            : "border-amber-200 bg-amber-50"
          : "border-[#d7d1c3] bg-[#fffdf8]",
      )}
    >
      <div className="text-xs font-medium text-slate-500">风险线索</div>
      <div className={cn("mt-2 text-2xl font-semibold", riskTone(risk) === "danger" ? "text-rose-700" : riskTone(risk) === "warn" ? "text-amber-700" : "text-[#285d54]")}>{riskLabel(risk)}</div>
      {highlighted ? (
        <div className="mt-2 space-y-1 text-[11px] leading-5 text-slate-600">
          <p>提示：{localizedGeneratedText(caveat, "风险线索仅用于连续观察，不构成临床诊断。")}</p>
          <p>来源：{source ? "当前任务证据台账" : "规则与标准化数据"}</p>
        </div>
      ) : (
        <div className="mt-1 text-xs leading-5 text-slate-500">规则与证据共同裁决</div>
      )}
    </Card>
  );
}

function UrgentBoundaryBanner() {
  return (
    <section
      role="alert"
      className="mt-5 flex flex-col gap-4 rounded-md border-2 border-rose-400 bg-rose-50 p-5 text-rose-950 md:flex-row md:items-center md:justify-between"
    >
      <div className="flex gap-3">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-rose-700 text-white">
          <AlertTriangle className="h-5 w-5" />
        </span>
        <div>
          <h2 className="text-lg font-semibold">先处理急症线索，不要等待睡眠解释</h2>
          <p className="mt-1 text-sm leading-6 text-rose-800">
            如有胸痛、严重呼吸困难、意识异常或跌倒，请立即寻求线下医疗或急救评估，并让家人知情。
          </p>
        </div>
      </div>
      <a
        href="tel:120"
        className="inline-flex h-11 shrink-0 items-center justify-center gap-2 rounded-md bg-rose-700 px-5 text-sm font-semibold text-white hover:bg-rose-800 focus:outline-none focus:ring-2 focus:ring-rose-300"
      >
        <PhoneCall className="h-4 w-4" />
        拨打 120
      </a>
    </section>
  );
}

function SafetyFootnote({ onOpen }: { onOpen: () => void }) {
  return (
    <footer className="mt-4 flex flex-wrap items-center justify-between gap-2 px-1 text-[11px] leading-5 text-slate-500">
      <span>人工智能辅助整理 · 仅作睡眠健康观察参考 · 不构成诊断或医疗建议</span>
      <button type="button" onClick={onOpen} className="underline decoration-slate-300 underline-offset-2 hover:text-slate-700">
        关于人工智能分析 / 数据说明
      </button>
    </footer>
  );
}

function SafetyDisclosure({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[60]">
      <button type="button" aria-label="关闭数据说明" onClick={onClose} className="absolute inset-0 bg-black/20" />
      <aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="safety-disclosure-title"
        tabIndex={-1}
        autoFocus
        onKeyDown={(event) => {
          if (event.key === "Escape") onClose();
        }}
        className="absolute bottom-0 right-0 top-0 flex w-full max-w-md flex-col border-l border-[#d8d2c4] bg-[#fbfaf6] shadow-2xl"
      >
        <div className="flex items-start justify-between border-b border-[#ded8ca] p-5">
          <div>
            <h2 id="safety-disclosure-title" className="font-semibold">关于人工智能分析 / 数据说明</h2>
            <p className="mt-1 text-xs text-slate-500">详细边界集中放置，不占据日常总览。</p>
          </div>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="关闭"><X className="h-4 w-4" /></Button>
        </div>
        <div className="space-y-5 overflow-y-auto p-5 text-sm leading-7 text-slate-700">
          <DisclosureSection title="人工智能辅助边界" items={["报告由人工智能基于结构化证据台账辅助整理。", "表达 Agent 只能改变措辞，不能改变风险等级或事实源。", "对话只读当前任务的证据台账与已审阅知识检索结果。"]} />
          <DisclosureSection title="雷达数据边界" items={["毫米波雷达不能替代多导睡眠监测、病史、查体或临床判断。", "数据质量不足时，系统会停止风险解释并提示检查设备。", "云端模型不接收原始雷达流，只接收必要摘要和引用。"]} />
          <DisclosureSection title="产品与合规说明" items={["本产品仅用于睡眠健康观察与照护协同。", "不提供确诊、精确呼吸暂停低通气指数、处方或药物建议。", "本项目不宣称符合美国健康信息隐私法规、美国食品药品管理要求、医疗器械或临床诊断合规要求。"]} />
        </div>
      </aside>
    </div>
  );
}

function DisclosureSection({ title, items }: { title: string; items: string[] }) {
  return (
    <section>
      <h3 className="font-medium text-slate-900">{title}</h3>
      <ul className="mt-2 list-disc space-y-1 pl-5 text-slate-600">
        {items.map((item) => <li key={item}>{item}</li>)}
      </ul>
    </section>
  );
}

function MetricCard({ label, value, detail, tone }: { label: string; value: string; detail: string; tone: "good" | "warn" | "danger" }) {
  return (
    <Card className="border-[#d7d1c3] bg-[#fffdf8] p-4">
      <div className="text-xs font-medium text-slate-500">{label}</div>
      <div className={cn("mt-2 text-2xl font-semibold", tone === "danger" ? "text-rose-700" : tone === "warn" ? "text-amber-700" : "text-[#285d54]")}>{value}</div>
      <div className="mt-1 text-xs leading-5 text-slate-500">{detail}</div>
    </Card>
  );
}

function TrendChart({ points }: { points: Array<{ date: string; sleep_score: number; getup_count: number }> }) {
  const maxGetups = Math.max(...points.map((item) => item.getup_count), 1);
  return (
    <div className="mt-5 grid grid-cols-6 gap-2" aria-label="七天趋势图">
      {points.slice(-6).map((point) => (
        <div key={point.date} className="text-center">
          <div className="flex h-28 items-end justify-center gap-1 rounded-md bg-[#f1eee5] px-2 pb-2">
            <div className="w-3 rounded-sm bg-[#6f958d]" style={{ height: `${Math.max(12, point.sleep_score)}%` }} title={`睡眠评分 ${point.sleep_score}`} />
            <div className="w-3 rounded-sm bg-[#b88a42]" style={{ height: `${Math.max(8, (point.getup_count / maxGetups) * 100)}%` }} title={`离床 ${point.getup_count} 次`} />
          </div>
          <div className="mt-2 text-[11px] text-slate-500">{point.date}</div>
        </div>
      ))}
      <div className="col-span-6 mt-1 flex justify-end gap-4 text-xs text-slate-500"><span>■ 睡眠评分</span><span className="text-[#8d682f]">■ 离床次数</span></div>
    </div>
  );
}

function QualityRows({ quality }: { quality: RadarRoleReport["data_quality"] | undefined }) {
  const rows = [
    ["状态", qualityLabel(quality?.status)],
    ["设备状态", deviceStatusLabel(quality?.device_status)],
    ["覆盖率", percent(quality?.coverage_ratio)],
    ["无效读数", countValue(quality?.invalid_reading_count)],
    ["异常读数", countValue(quality?.abnormal_reading_count)],
    ["离床时段", intervalSummary(quality?.out_of_bed_intervals ?? [])],
    ["不在床时段", intervalSummary(quality?.not_in_bed_intervals ?? [])],
  ];
  return (
    <dl className="mt-4 divide-y divide-[#e5e0d5]">
      {rows.map(([label, value]) => (
        <div key={label} className="flex items-center justify-between py-2.5 text-sm"><dt className="text-slate-500">{label}</dt><dd className="font-medium text-slate-800">{value}</dd></div>
      ))}
    </dl>
  );
}

function SectionTitle({ icon: Icon, title, subtitle }: { icon: typeof ArrowDownRight; title: string; subtitle: string }) {
  return <div className="flex gap-3"><span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-[#e7efec] text-[#285d54]"><Icon className="h-4 w-4" /></span><div><h2 className="font-semibold">{title}</h2><p className="mt-0.5 text-xs leading-5 text-slate-500">{subtitle}</p></div></div>;
}

function ErrorNotice({ message }: { message: string }) {
  return <div className="mt-5 flex gap-3 rounded-md border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800"><AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /><span>{message}</span></div>;
}

function EmptyLine({ text }: { text: string }) {
  return <div className="rounded-md border border-dashed border-[#d8d2c4] p-3 text-sm text-slate-500">{text}</div>;
}

function latestLedger(detail: RadarTaskDetail | null): RadarEvidenceLedger | null {
  return [...(detail?.artifacts ?? [])].reverse().find((item) => item.evidence_ledger)?.evidence_ledger ?? null;
}

function reportForRole(detail: RadarTaskDetail | null, role: RadarAgentRole): RadarRoleReport | null {
  return [...(detail?.artifacts ?? [])].reverse().find((item) => item.report?.role === role)?.report ?? null;
}

function mergeEvents(current: RadarTaskStreamEvent[], incoming: RadarTaskStreamEvent[]) {
  return [...new Map([...current, ...incoming].map((item) => [item.event_id, item])).values()].sort((a, b) => a.sequence - b.sequence);
}

function riskLabel(risk: RadarReplayRiskLevel | null | undefined) {
  return { info: "日常观察", uncertain: "暂无法判断", watch: "需要关注", escalate: "建议进一步评估", urgent_boundary: "及时线下评估" }[risk ?? "info"];
}

function riskVariant(risk: RadarReplayRiskLevel | null | undefined): "default" | "warning" | "danger" | "neutral" {
  if (risk === "urgent_boundary" || risk === "escalate") return "danger";
  if (risk === "watch") return "warning";
  if (risk === "uncertain") return "neutral";
  return "default";
}

function riskTone(risk: RadarReplayRiskLevel | null | undefined): "good" | "warn" | "danger" {
  return risk === "urgent_boundary" || risk === "escalate" ? "danger" : risk === "watch" || risk === "uncertain" ? "warn" : "good";
}

function elderHeadline(risk: RadarReplayRiskLevel) {
  return { info: "昨晚整体平稳，可以安心开始今天。", uncertain: "昨晚记录不够完整，先检查设备。", watch: "有些变化值得家人一起留意。", escalate: "建议请家人协助，整理材料进一步评估。", urgent_boundary: "请先处理身体不适，不要等待睡眠分析。" }[risk];
}

function elderObservation(risk: RadarReplayRiskLevel) {
  return risk === "info" ? "设备记录显示昨夜状态大体稳定。" : risk === "uncertain" ? "数据质量不足，系统没有把缺失记录解释成健康结论。" : "系统发现了需要继续观察的变化，但这不是诊断。";
}

function elderSuggestion(risk: RadarReplayRiskLevel) {
  if (risk === "urgent_boundary") return "现在就请家人协助联系线下医疗或急救评估。";
  if (risk === "escalate") return "今天请家人协助整理医生材料，并考虑预约评估。";
  if (risk === "uncertain") return "今晚睡前请检查雷达供电与摆放位置。";
  if (risk === "watch") return "今天告诉家人自己的睡眠感受，并继续观察一晚。";
  return "保持平常作息，今晚继续正常监测。";
}

function phaseLabel(phase: "creating" | "running" | "ready" | "error") {
  return { creating: "正在创建任务", running: "正在分析", ready: "分析完成", error: "任务异常" }[phase];
}

function qualityLabel(status?: string) {
  return { good: "质量良好", partial: "部分可信", unusable: "不可解释", urgent_text_override: "急症文本优先" }[status ?? ""] ?? "等待质量结论";
}

function percent(value?: number) {
  return typeof value === "number" ? `${Math.round(value * 100)}%` : "—";
}

function countValue(value?: number) {
  return typeof value === "number" ? `${value} 条` : "—";
}

function deviceStatusLabel(value?: string) {
  return { online: "在线", offline: "离线", unknown: "未知" }[value ?? ""] ?? "等待设备状态";
}

function intervalSummary(intervals: string[]) {
  if (!intervals.length) return "无记录";
  const first = formatInterval(intervals[0]);
  return intervals.length === 1 ? first : `${first} 等 ${intervals.length} 段`;
}

function formatInterval(value: string) {
  const [start, end] = value.split("/");
  const startTime = formatClock(start);
  const endTime = formatClock(end);
  return startTime === endTime ? startTime : `${startTime}–${endTime}`;
}

function formatClock(value?: string) {
  if (!value) return "未知";
  const match = value.match(/T(\d{2}:\d{2})/);
  return match?.[1] ?? value;
}

function questionnaireLabel(value: string) {
  const labels: Record<string, string> = { daytime_sleepiness: "白天是否明显困倦？", snoring_or_gasping: "近期是否有打鼾或憋醒？", device_position_check: "设备位置和供电是否正常？", out_of_bed_reason: "昨夜离床的主要原因是什么？", recent_discomfort: "近期是否有明显身体不适？" };
  return labels[value] ?? value.replaceAll("_", " ");
}

function questionnairePrompt(questionId: string) {
  const prompts: Record<string, string> = {
    "q-daytime-sleepiness": "白天是否出现明显困倦？",
    "q-snoring-or-gasping": "近期是否出现打鼾、憋醒或呼吸不畅？",
    "q-device-placement": "雷达设备的供电和摆放位置是否正常？",
    "q-slept-away-from-bed": "昨晚是否在监测床位之外休息？",
    "q-night-bathroom": "昨夜多次离床是否与夜间如厕有关？",
    "q-sleep-schedule-change": "近期作息时间是否有明显变化？",
  };
  return prompts[questionId] ?? "请补充一项与昨夜睡眠相关的信息。";
}

function actionLabel(value: string) {
  const labels: Record<string, string> = {
    export_doctor_material: "导出医生材料",
    send_doctor_material: "发送医生材料",
    create_medical_evaluation_card: "生成评估建议卡片",
    write_long_term_memory: "写入长期趋势记忆",
    notify_family: "通知家属",
    notify_family_delivery_record: "记录家属通知送达状态",
    enable_persistent_family_reminder: "启用家属持续提醒",
    push_supplemental_questionnaire: "推送补充问卷",
    enable_care_plan: "启用照护计划",
  };
  return labels[value] ?? "待确认照护动作";
}

function localizedGeneratedText(value: unknown, fallback: string) {
  if (typeof value !== "string" || !value.trim()) return fallback;
  const inspected = value.replaceAll("SleepAgent", "").replaceAll("Agent", "").replaceAll("agent", "");
  return /[A-Za-z]/.test(inspected) ? fallback : value;
}

function localizedClaimText(claim: RadarEvidenceLedger["claims"][number], index: number) {
  const claimId = claim.claim_id;
  const fallback = claimId.includes("out-of-bed")
    ? "近七天夜间离床次数较个人基线上升。"
    : claimId.includes("sleep_minutes")
      ? "近七天睡眠时长较个人基线下降。"
      : claimId.includes("urgent-boundary")
        ? "当前自述触发急症安全边界，应优先接受线下医疗或急救评估。"
        : claimId.startsWith("risk:")
          ? `当前风险线索为“${riskLabel(claim.risk_level)}”，建议按对应照护边界处理。`
          : index === 0
            ? "雷达数据已完成标准化与数据质量门控。"
            : "已根据当前标准化证据生成一条可审阅观察。";
  return localizedGeneratedText(claim.text, fallback);
}

function localizedClaimCaveat(claim: RadarEvidenceLedger["claims"][number]) {
  return claim.risk_level === "urgent_boundary"
    ? "急症提示优先于睡眠趋势解释，外部通知仍需记录确认状态。"
    : "该结论仅用于睡眠健康连续观察，不构成医学诊断。";
}

function doctorCaveat(index: number, report: RadarRoleReport | null) {
  const items = [
    `当前数据质量为“${qualityLabel(report?.data_quality.status)}”，覆盖率为 ${percent(report?.data_quality.coverage_ratio)}。`,
    "毫米波雷达不能替代多导睡眠监测、病史、查体或医生判断。",
    "风险线索用于照护协同与连续观察，不构成确诊结论。",
    "数据缺失、离床和设备状态可能影响趋势解释。",
  ];
  return items[index % items.length];
}

function doctorSafetyNotice(index: number) {
  return [
    "本材料由人工智能基于结构化证据辅助整理。",
    "本材料仅供异步审阅参考，不替代医生诊断或治疗建议。",
    "任何外发、长期写入或升级动作均受确认和审计约束。",
  ][index % 3];
}

function reviewStatusLabel(value: string) {
  return { reviewed: "已审阅", needs_human_review: "需要人工审阅", rejected: "已驳回", pending: "待审阅" }[value] ?? "待审阅";
}

function questionSourceLabel(value: string) {
  return { bank: "版本化题库", skill: "Agent 能力库" }[value] ?? "已审阅题库";
}

function displayVersion(value: string) {
  const match = value.match(/\d+(?:\.\d+)*/);
  return match?.[0] ?? "1";
}

function displayIdentifier(value: string) {
  const suffix = value.split("-").at(-1) ?? value;
  return suffix.length > 10 ? `…${suffix.slice(-10)}` : suffix;
}

function eventDisplayMessage(event: RadarTaskStreamEvent) {
  const labels: Record<string, string> = {
    "task.created": "任务已创建",
    "task.running": "任务正在执行",
    "task.completed": "任务分析完成",
    "task.failed": "任务执行失败",
    "task.waiting_for_confirmation": "任务正在等待确认",
    "node.running": "分析节点正在执行",
    "node.succeeded": "分析节点执行完成",
    "node.failed": "分析节点执行失败",
    "agent.completed": "Agent 已完成当前分析",
    "a2a.message": "Agent 协作消息已处理",
    "claim.created": "已生成一条证据结论",
    "artifact.version_created": "已生成一个新版分析产物",
    "confirmation.requested": "已创建一项人工确认请求",
    "confirmation.approved": "确认请求已通过",
    "confirmation.rejected": "确认请求已拒绝",
    "action.auto_published": "安全范围内的结果已自动发布",
    "llm.fallback": "语言模型不可用，已使用模板兜底",
    "llm.completed": "语言模型表达整理完成",
    "conflict.resolved": "Agent 结论冲突已完成裁决",
    "questionnaire.candidate": "已选择一项补充问卷",
    "chat.answered": "报告问题已完成回答",
  };
  return labels[event.event_type] ?? "分析过程已记录一项新进展";
}

function localizedChatAnswer(detail: RadarTaskDetail | null) {
  const risk = detail?.risk_level ?? "info";
  return `当前证据显示风险线索为“${riskLabel(risk)}”。回答仅基于本任务的证据台账和已审阅知识，不会改变原始事实或风险等级。`;
}

function toMessage(cause: unknown) {
  return cause instanceof Error
    ? localizedGeneratedText(cause.message, "雷达任务请求失败，请检查后端服务与接口配置。")
    : "雷达任务请求失败，请检查后端服务与接口配置。";
}

function downloadDoctorMaterial(report: RadarRoleReport, ledger: RadarEvidenceLedger | null) {
  const evidence = (ledger?.claims ?? []).map((claim, index) => `- ${localizedClaimText(claim, index)}\n  - 可信度：${Math.round(claim.confidence * 100)}%\n  - 证据引用：${claim.evidence_refs.length} 条`).join("\n");
  const reportContent = localizedGeneratedText(report.content, "本材料依据当前证据台账整理，建议结合数据质量、连续趋势和补充问卷进行人工审阅。");
  const caveats = report.caveats.map((item, index) => `- ${localizedGeneratedText(item, doctorCaveat(index, report))}`);
  const content = ["# SleepAgent 医生材料", "", `风险线索：${riskLabel(report.risk_level)}`, `数据质量：${qualityLabel(report.data_quality.status)} / ${percent(report.data_quality.coverage_ratio)}`, "", "## 证据链", evidence, "", "## 报告", reportContent, "", "## 注意事项", ...caveats, "", "仅作睡眠健康观察参考，不构成诊断。"].join("\n");
  const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${displayIdentifier(report.task_id)}-医生材料.md`;
  anchor.click();
  URL.revokeObjectURL(url);
}
