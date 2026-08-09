"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  Loader2,
  MoonStar,
  ShieldCheck,
  Trash2,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  confirmHabitChangeSet,
  loadHabitProfile,
  pruneHabitChangeSet,
  requestHabitForget,
  startHabitProfileUpdate,
  startOptionalHabitIntake,
  submitHabitAnswers,
} from "@/lib/habit-profile-api";
import type {
  HabitDisposition,
  HabitPendingChangeSet,
  HabitProfile,
  HabitQuestionCandidate,
  HabitSelection,
} from "@/lib/habit-profile-types";
import { cn } from "@/lib/utils";

type DraftAnswer = {
  disposition: HabitDisposition;
  value?: string | number;
};

const conceptLabels: Record<string, string> = {
  "habit.primary_goal": "当前希望改善的事",
  "habit.schedule_constraint": "作息约束",
  "habit.nap_pattern": "午睡模式",
  "habit.nap_duration_minutes": "午睡时长",
  "habit.pre_sleep_behavior": "睡前活动",
  "habit.environment_preference": "睡眠环境偏好",
  "habit.stimulant_timing": "咖啡或浓茶时间",
  "habit.sleep_satisfaction_recent": "近期睡眠感受",
  "habit.observed_snoring": "近期可观察夜间行为",
};

export function HabitProfileWorkspace() {
  const [profile, setProfile] = useState<HabitProfile | null>(null);
  const [selection, setSelection] = useState<HabitSelection | null>(null);
  const [notice, setNotice] = useState("");
  const [answers, setAnswers] = useState<Record<string, DraftAnswer>>({});
  const [replacement, setReplacement] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<HabitPendingChangeSet | null>(null);
  const [busy, setBusy] = useState(false);
  const [safetyMessage, setSafetyMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const complete = useMemo(
    () =>
      !!selection &&
      selection.candidates.length > 0 &&
      selection.candidates.every((item) => {
        const answer = answers[item.concept_id];
        return (
          !!answer &&
          (answer.disposition !== "answered" ||
            (answer.value !== undefined && answer.value !== ""))
        );
      }),
    [answers, selection],
  );

  useEffect(() => {
    void refreshProfile();
  }, []);

  async function refreshProfile() {
    try {
      setError(null);
      setProfile(await loadHabitProfile());
    } catch (cause) {
      setError(toMessage(cause));
    }
  }

  async function beginOptionalIntake() {
    setBusy(true);
    setError(null);
    setSafetyMessage(null);
    try {
      const result = await startOptionalHabitIntake({
        episodeId: `habit-intake-${crypto.randomUUID()}`,
        maxQuestions: 3,
      });
      setNotice(result.purpose_notice);
      setSelection(result.selection);
      setAnswers({});
      setReplacement({});
      setPending(null);
    } catch (cause) {
      setError(toMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  async function beginUpdate(conceptId: string, factRef: string) {
    setBusy(true);
    setError(null);
    try {
      const result = await startHabitProfileUpdate({
        episodeId: `habit-update-${crypto.randomUUID()}`,
        conceptIds: [conceptId],
      });
      setNotice(result.purpose_notice);
      setSelection(result.selection);
      setAnswers({});
      setReplacement({ [conceptId]: factRef });
      setPending(null);
    } catch (cause) {
      setError(toMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (!selection || !complete) return;
    setBusy(true);
    setError(null);
    try {
      const result = await submitHabitAnswers({
        selection,
        answers: selection.candidates.map((item) => ({
          concept_id: item.concept_id,
          concept_version: item.concept_version,
          disposition: answers[item.concept_id].disposition,
          ...(answers[item.concept_id].value !== undefined
            ? { value: answers[item.concept_id].value }
            : {}),
        })),
        replaceFactIdByConcept: replacement,
      });
      setSelection(null);
      setAnswers({});
      if (result.next_status === "safety_preempted") {
        setSafetyMessage(
          "这条回答需要优先按安全流程处理，本轮习惯问题已经停止，内容不会写入习惯画像。",
        );
        setPending(null);
      } else {
        setPending(result.pending_change_set ?? null);
      }
      await refreshProfile();
    } catch (cause) {
      setError(toMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  async function confirmPending() {
    if (!pending) return;
    setBusy(true);
    setError(null);
    try {
      await confirmHabitChangeSet({
        decisionId: pending.decision_id,
        idempotencyKey: `habit-confirm:${pending.decision_id}`,
      });
      setPending(null);
      await refreshProfile();
    } catch (cause) {
      setError(toMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  async function removeCandidate(candidateId: string) {
    if (!pending || pending.change_set.candidates.length <= 1) return;
    setBusy(true);
    setError(null);
    try {
      setPending(
        await pruneHabitChangeSet({
          pending,
          candidateIdsToRemove: [candidateId],
        }),
      );
    } catch (cause) {
      setError(toMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  async function beginForget(factRef: string) {
    setBusy(true);
    setError(null);
    try {
      setPending(
        await requestHabitForget({
          episodeId: `habit-forget-${crypto.randomUUID()}`,
          factId: factRef,
        }),
      );
      setSelection(null);
    } catch (cause) {
      setError(toMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="min-h-screen bg-[#f4f1e9] text-slate-900">
      <header className="border-b border-[#ded8ca] bg-[#fbfaf6]">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-4 px-5 py-5">
          <div className="flex items-center gap-3">
            <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-[#285d54] text-white">
              <MoonStar className="h-6 w-6" aria-hidden="true" />
            </div>
            <div>
              <h1 className="text-xl font-semibold">我的睡眠习惯</h1>
              <p className="text-sm text-slate-600">只记录对解释和低负担照护有用的内容</p>
            </div>
          </div>
          <Link
            href="/"
            className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-[#d4cdbd] bg-white px-4 text-base font-medium"
          >
            <ArrowLeft className="h-4 w-4" />
            返回
          </Link>
        </div>
      </header>

      <div className="mx-auto max-w-5xl space-y-5 px-5 py-7">
        <Card className="border-[#cfded8] bg-[#eef7f3] p-5">
          <div className="flex gap-3">
            <ShieldCheck className="mt-0.5 h-6 w-6 shrink-0 text-[#285d54]" />
            <div className="space-y-1 text-base leading-7">
              <p className="font-semibold">您可以随时跳过，不影响晨间解释和一般问答。</p>
              <p>回答可用于当前说明；长期保存前会再次逐项显示并请您整体确认。</p>
              <p>这里不生成睡眠健康分、固定类型或诊断。</p>
            </div>
          </div>
        </Card>

        {error ? (
          <div role="alert" className="rounded-xl border border-red-200 bg-red-50 p-4 text-base text-red-800">
            {error}
          </div>
        ) : null}
        {safetyMessage ? (
          <div role="alert" className="rounded-xl border border-amber-300 bg-amber-50 p-5 text-lg leading-8 text-amber-950">
            {safetyMessage}
          </div>
        ) : null}

        {selection ? (
          <QuestionPanel
            notice={notice}
            selection={selection}
            answers={answers}
            busy={busy}
            onAnswer={(conceptId, answer) =>
              setAnswers((current) => ({ ...current, [conceptId]: answer }))
            }
            onCancel={() => {
              setSelection(null);
              setAnswers({});
            }}
            onSubmit={submit}
            complete={complete}
          />
        ) : pending ? (
          <ConfirmationPanel
            pending={pending}
            busy={busy}
            onCancel={() => setPending(null)}
            onConfirm={confirmPending}
            onRemove={removeCandidate}
          />
        ) : (
          <>
            <ProfilePanel
              profile={profile}
              busy={busy}
              onUpdate={beginUpdate}
              onForget={beginForget}
            />
            <div className="flex justify-center">
              <Button
                className="min-h-14 rounded-xl bg-[#285d54] px-7 text-lg hover:bg-[#204b44]"
                onClick={beginOptionalIntake}
                disabled={busy}
              >
                {busy ? <Loader2 className="h-5 w-5 animate-spin" /> : null}
                可选：回答 2–3 个问题
              </Button>
            </div>
          </>
        )}
      </div>
    </main>
  );
}

function QuestionPanel({
  notice,
  selection,
  answers,
  busy,
  complete,
  onAnswer,
  onCancel,
  onSubmit,
}: {
  notice: string;
  selection: HabitSelection;
  answers: Record<string, DraftAnswer>;
  busy: boolean;
  complete: boolean;
  onAnswer: (conceptId: string, answer: DraftAnswer) => void;
  onCancel: () => void;
  onSubmit: () => void;
}) {
  return (
    <Card className="space-y-6 border-[#d8d2c4] bg-white p-6">
      <div>
        <h2 className="text-2xl font-semibold">只问这几项</h2>
        <p className="mt-2 text-base leading-7 text-slate-600">{notice}</p>
      </div>
      {selection.candidates.map((candidate, index) => (
        <Question
          key={`${candidate.concept_id}:${candidate.concept_version}`}
          index={index + 1}
          candidate={candidate}
          answer={answers[candidate.concept_id]}
          onAnswer={(answer) => onAnswer(candidate.concept_id, answer)}
        />
      ))}
      <div className="flex flex-wrap justify-end gap-3 border-t border-slate-200 pt-5">
        <Button variant="outline" className="min-h-12 px-5 text-base" onClick={onCancel}>
          中途退出
        </Button>
        <Button
          className="min-h-12 bg-[#285d54] px-6 text-base"
          disabled={!complete || busy}
          onClick={onSubmit}
        >
          {busy ? <Loader2 className="h-5 w-5 animate-spin" /> : null}
          提交这些回答
        </Button>
      </div>
    </Card>
  );
}

function Question({
  index,
  candidate,
  answer,
  onAnswer,
}: {
  index: number;
  candidate: HabitQuestionCandidate;
  answer?: DraftAnswer;
  onAnswer: (answer: DraftAnswer) => void;
}) {
  return (
    <fieldset className="space-y-4 rounded-xl border border-slate-200 p-5">
      <legend className="px-2 text-lg font-semibold">
        {index}. {candidate.prompt_text}
      </legend>
      {candidate.options.length ? (
        <div className="grid gap-3 sm:grid-cols-2">
          {candidate.options.map((option) => (
            <button
              key={option}
              type="button"
              aria-pressed={answer?.disposition === "answered" && answer.value === option}
              onClick={() => onAnswer({ disposition: "answered", value: option })}
              className={cn(
                "min-h-12 rounded-xl border px-4 text-left text-base",
                answer?.disposition === "answered" && answer.value === option
                  ? "border-[#285d54] bg-[#e5f1ed] font-semibold text-[#204b44]"
                  : "border-slate-300 bg-white hover:border-[#6d9188]",
              )}
            >
              {option}
            </button>
          ))}
        </div>
      ) : (
        <input
          type={candidate.answer_type === "bounded_number" ? "number" : "text"}
          min={candidate.minimum ?? undefined}
          max={candidate.maximum ?? undefined}
          aria-label={candidate.prompt_text}
          value={answer?.value ?? ""}
          onChange={(event) =>
            onAnswer({
              disposition: "answered",
              value:
                candidate.answer_type === "bounded_number" &&
                event.target.value !== ""
                  ? Number(event.target.value)
                  : event.target.value,
            })
          }
          className="min-h-12 w-full rounded-xl border border-slate-300 px-4 text-lg outline-none focus:border-[#285d54]"
        />
      )}
      <div className="flex flex-wrap gap-2">
        {[
          ["unknown", "不清楚"],
          ["prefer_not_to_answer", "不愿回答"],
          ["skipped", "跳过"],
        ].map(([disposition, label]) => (
          <button
            key={disposition}
            type="button"
            onClick={() =>
              onAnswer({ disposition: disposition as HabitDisposition })
            }
            className={cn(
              "min-h-11 rounded-lg border px-4 text-base",
              answer?.disposition === disposition
                ? "border-slate-600 bg-slate-100 font-semibold"
                : "border-slate-300 bg-white",
            )}
          >
            {label}
          </button>
        ))}
      </div>
    </fieldset>
  );
}

function ConfirmationPanel({
  pending,
  busy,
  onCancel,
  onConfirm,
  onRemove,
}: {
  pending: HabitPendingChangeSet;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  onRemove: (candidateId: string) => void;
}) {
  return (
    <Card className="space-y-5 border-[#d8d2c4] bg-white p-6">
      <div>
        <h2 className="text-2xl font-semibold">请确认是否长期保存</h2>
        <p className="mt-2 text-base leading-7 text-slate-600">
          下面是完整清单。确认只授权保存，不会把家属观察变成您的自述。
        </p>
      </div>
      <div className="space-y-3">
        {pending.confirmation_summary.map((item) => (
          <div key={item.candidate_id} className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 p-4">
            <div>
              <p className="font-semibold">{labelFor(item.concept_id)}</p>
              <p className="mt-1 text-lg">
                {item.operation === "forget"
                  ? "停止用于后续个性化"
                  : formatValue(item.value)}
              </p>
              {item.origin_semantic ? (
                <p className="mt-1 text-sm text-slate-500">
                  来源：
                  {item.origin_semantic === "family_observation"
                    ? "家属观察"
                    : "本人回答"}
                </p>
              ) : null}
            </div>
            {pending.confirmation_summary.length > 1 ? (
              <Button variant="outline" className="min-h-11" disabled={busy} onClick={() => onRemove(item.candidate_id)}>
                不保存这一项
              </Button>
            ) : null}
          </div>
        ))}
      </div>
      <div className="flex flex-wrap justify-end gap-3 border-t border-slate-200 pt-5">
        <Button variant="outline" className="min-h-12 px-5 text-base" onClick={onCancel}>
          暂不保存
        </Button>
        <Button className="min-h-12 bg-[#285d54] px-6 text-base" disabled={busy} onClick={onConfirm}>
          {busy ? <Loader2 className="h-5 w-5 animate-spin" /> : <CheckCircle2 className="h-5 w-5" />}
          确认执行以上 {pending.confirmation_summary.length} 项
        </Button>
      </div>
    </Card>
  );
}

function ProfilePanel({
  profile,
  busy,
  onUpdate,
  onForget,
}: {
  profile: HabitProfile | null;
  busy: boolean;
  onUpdate: (conceptId: string, factRef: string) => void;
  onForget: (factRef: string) => void;
}) {
  if (!profile) {
    return <Card className="p-7 text-center text-lg text-slate-600">正在读取已保存内容…</Card>;
  }
  return (
    <Card className="space-y-5 border-[#d8d2c4] bg-white p-6">
      <div>
        <h2 className="text-2xl font-semibold">已保存的内容</h2>
        <p className="mt-2 text-base text-slate-600">未知或未回答的项目不会显示，也没有“完整度”要求。</p>
      </div>
      {!profile.facts.length ? (
        <div className="rounded-xl bg-slate-50 p-6 text-center text-lg text-slate-600">
          目前没有长期保存的睡眠习惯。您仍可正常使用所有核心功能。
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {profile.facts.map((fact) => (
            <div key={fact.fact_ref} className="rounded-xl border border-slate-200 p-5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="font-semibold">{labelFor(fact.concept_id)}</p>
                  <p className="mt-2 text-xl">{formatValue(fact.value, fact.unit)}</p>
                </div>
                <span className="rounded-full bg-[#e5f1ed] px-3 py-1 text-sm text-[#285d54]">
                  {fact.effective_status === "disputed"
                    ? "待核对"
                    : fact.effective_status === "stale"
                      ? "已过期"
                      : "有效"}
                </span>
              </div>
              <p className="mt-3 text-sm leading-6 text-slate-500">
                来源：{fact.origin_semantic === "family_observation" ? "家属观察（经本人确认保存）" : "本人回答"}
                <br />
                最近确认：{new Date(fact.confirmed_at).toLocaleDateString("zh-CN")}
              </p>
              <div className="mt-4 flex gap-2">
                <Button variant="outline" className="min-h-11 flex-1" disabled={busy} onClick={() => onUpdate(fact.concept_id, fact.fact_ref)}>
                  更正
                </Button>
                <Button variant="outline" className="min-h-11 text-red-700" disabled={busy} onClick={() => onForget(fact.fact_ref)}>
                  <Trash2 className="h-4 w-4" />
                  遗忘
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
      <p className="border-t border-slate-200 pt-4 text-sm leading-6 text-slate-500">
        遗忘后，该内容不再用于个性化；为安全和审计依法需要保留的最小事件记录可能仍会保留。
      </p>
    </Card>
  );
}

function labelFor(conceptId: string) {
  return conceptLabels[conceptId] ?? conceptId;
}

function formatValue(value: unknown, unit?: string | null) {
  const rendered =
    typeof value === "object" ? JSON.stringify(value) : String(value ?? "不适用");
  return unit ? `${rendered} ${unit === "minute" ? "分钟" : unit}` : rendered;
}

function toMessage(cause: unknown) {
  return cause instanceof Error ? cause.message : "操作失败，请稍后重试。";
}
