"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AppShell } from "@/components/layout/AppShell";
import { AgentRunPage } from "@/components/agent/AgentRunPage";
import { AlertSettings } from "@/components/alerts/AlertSettings";
import { ChatAgentPage } from "@/components/chat/ChatAgentPage";
import { DataManagement } from "@/components/data/DataManagement";
import { TodayAnalysis } from "@/components/dashboard/TodayAnalysis";
import { ReportCenter } from "@/components/reports/ReportCenter";
import { TrendFollowup } from "@/components/trends/TrendFollowup";
import { Card, CardContent } from "@/components/ui/card";
import { Tabs } from "@/components/ui/tabs";
import {
  cancelSleepTask,
  confirmSleepArtifact,
  confirmSleepTask,
  createSleepTask,
  getSleepTask,
  isBackendTaskApiEnabled,
  reviseSleepArtifact,
  runMockAnalysis,
  subscribeTaskEvents,
} from "@/lib/api";
import { sleepAnalysis } from "@/lib/mock-data";
import type { Role, RunStatus, SleepAgentTask, TaskStatus, ViewKey } from "@/lib/types";

const roleLabels: Record<Role, string> = {
  elder: "老人模式",
  family: "家属模式",
  doctor: "医生模式",
};

const roleByLabel: Record<string, Role> = {
  老人模式: "elder",
  家属模式: "family",
  医生模式: "doctor",
};

const ACTIVE_TASK_STORAGE_KEY = "sleepagent.activeTaskId";

export default function HomePage() {
  const [activeView, setActiveView] = useState<ViewKey>("today");
  const [role, setRole] = useState<Role>("elder");
  const [taskStatus, setTaskStatus] = useState<TaskStatus>("idle");
  const [completedCount, setCompletedCount] = useState(0);
  const [currentTask, setCurrentTask] = useState(sleepAnalysis.defaultTask);
  const eventUnsubscribeRef = useRef<(() => void) | null>(null);
  const backendTaskMode = isBackendTaskApiEnabled();
  const [taskThread, setTaskThread] = useState<SleepAgentTask>({
    ...sleepAnalysis.taskThread,
    status: "idle",
    events: [],
    artifacts: sleepAnalysis.taskThread.artifacts.map((artifact) => ({ ...artifact, status: "draft" })),
    plan: sleepAnalysis.taskThread.plan.map((step) => ({ ...step, status: "pending" })),
  });

  const status: RunStatus = taskStatus === "completed" ? "completed" : taskStatus === "running" ? "running" : "idle";

  useEffect(() => {
    if (!backendTaskMode) return;
    const activeTaskId = window.localStorage.getItem(ACTIVE_TASK_STORAGE_KEY);
    if (!activeTaskId) return;

    let cancelled = false;
    getSleepTask(activeTaskId)
      .then((task) => {
        if (cancelled) return;
        applyTaskSnapshot(task);
        setCurrentTask(task.userGoal);
        if (task.status === "running") {
          attachEventStream(task.id);
        }
      })
      .catch(() => {
        window.localStorage.removeItem(ACTIVE_TASK_STORAGE_KEY);
      });

    return () => {
      cancelled = true;
      detachEventStream();
    };
  }, [backendTaskMode]);

  const visibleEvents = useMemo(() => {
    if (backendTaskMode) return taskThread.events;
    if (taskStatus === "completed") return taskThread.events;
    if (taskStatus === "running") return taskThread.events.slice(0, Math.max(completedCount, 1));
    if (taskStatus === "awaiting_confirmation") return sleepAnalysis.taskThread.events.slice(0, 2);
    return taskThread.events;
  }, [backendTaskMode, completedCount, taskStatus, taskThread.events]);

  async function startAnalysis() {
    if (taskStatus === "running") return;
    if (backendTaskMode) {
      setTaskStatus("planning");
      setCompletedCount(0);
      try {
        const task = await createSleepTask(currentTask);
        window.localStorage.setItem(ACTIVE_TASK_STORAGE_KEY, task.id);
        applyTaskSnapshot(task);
      } catch (error) {
        showTaskError(error instanceof Error ? error.message : "任务创建失败。");
      }
      return;
    }

    const now = new Date().toLocaleString("zh-CN", { hour12: false });
    setTaskStatus("awaiting_confirmation");
    setCompletedCount(0);
    setTaskThread({
      ...sleepAnalysis.taskThread,
      id: `task-${Date.now()}`,
      userGoal: currentTask,
      title: currentTask.slice(0, 28),
      status: "awaiting_confirmation",
      createdAt: now,
      events: sleepAnalysis.taskThread.events.slice(0, 2),
      plan: sleepAnalysis.taskThread.plan.map((step) => ({ ...step, status: "pending" })),
      artifacts: sleepAnalysis.taskThread.artifacts.map((artifact) => ({ ...artifact, status: "draft" })),
    });
  }

  async function confirmTaskRun() {
    if (taskStatus === "running") return;
    setActiveView("agent");
    if (backendTaskMode) {
      setTaskStatus("running");
      setCompletedCount(countCompletedPlanSteps(taskThread));
      attachEventStream(taskThread.id);
      try {
        const task = await confirmSleepTask(taskThread.id);
        applyTaskSnapshot(task);
      } catch (error) {
        showTaskError(error instanceof Error ? error.message : "任务执行失败。");
      }
      return;
    }

    setTaskStatus("running");
    setCompletedCount(0);
    setTaskThread((current) => ({
      ...current,
      status: "running",
      events: sleepAnalysis.taskThread.events,
    }));
    await runMockAnalysis();
    for (let index = 1; index <= sleepAnalysis.taskThread.plan.length; index += 1) {
      await wait(360);
      setCompletedCount(index);
      setTaskThread((current) => ({
        ...current,
        plan: current.plan.map((step, stepIndex) => ({
          ...step,
          status: stepIndex < index ? "completed" : stepIndex === index ? "running" : "pending",
        })),
      }));
    }
    setTaskStatus("completed");
    setTaskThread((current) => ({
      ...current,
      status: "completed",
      plan: current.plan.map((step) => ({ ...step, status: "completed" })),
      artifacts: sleepAnalysis.taskThread.artifacts.map((artifact) => ({ ...artifact, status: "ready" })),
    }));
  }

  function reset() {
    detachEventStream();
    if (backendTaskMode) {
      window.localStorage.removeItem(ACTIVE_TASK_STORAGE_KEY);
    }
    setTaskStatus("idle");
    setCompletedCount(0);
    setCurrentTask(sleepAnalysis.defaultTask);
    setTaskThread({
      ...sleepAnalysis.taskThread,
      status: "idle",
      events: [],
      artifacts: sleepAnalysis.taskThread.artifacts.map((artifact) => ({ ...artifact, status: "draft" })),
      plan: sleepAnalysis.taskThread.plan.map((step) => ({ ...step, status: "pending" })),
    });
    setActiveView("today");
  }

  async function cancelTask() {
    if (backendTaskMode && taskThread.id) {
      try {
        const task = await cancelSleepTask(taskThread.id);
        applyTaskSnapshot(task);
      } catch {
        reset();
      }
      return;
    }
    reset();
  }

  async function updateArtifact(artifactId: string, content: string, revisionInstruction = "用户通过前端修改 Artifact。") {
    if (backendTaskMode) {
      const revised = await reviseSleepArtifact({
        artifactId,
        content,
        revisionInstruction,
      });
      setTaskThread((current) => ({
        ...current,
        artifacts: current.artifacts.map((artifact) => (artifact.id === artifactId ? revised : artifact)),
      }));
      try {
        const refreshed = await getSleepTask(taskThread.id);
        applyTaskSnapshot(refreshed);
      } catch {
        // Keep the revised artifact in local state if the refresh fails.
      }
      return;
    }

    const updatedAt = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    setTaskThread((current) => ({
      ...current,
      artifacts: current.artifacts.map((artifact) =>
        artifact.id === artifactId ? { ...artifact, content, status: "revised", updatedAt } : artifact,
      ),
      events: [
        ...current.events,
        {
          id: `evt-revise-${Date.now()}`,
          type: "artifact_created",
          title: "Artifact 已修改",
          message: "用户通过修改指令更新了报告产物。",
          timestamp: updatedAt,
          payload: { artifactId },
        },
      ],
    }));
  }

  async function confirmArtifactAction(artifactId: string) {
    if (backendTaskMode) {
      const confirmed = await confirmSleepArtifact(artifactId);
      setTaskThread((current) => ({
        ...current,
        artifacts: current.artifacts.map((artifact) => (artifact.id === confirmed.id ? confirmed : artifact)),
      }));
      try {
        const refreshed = await getSleepTask(taskThread.id);
        applyTaskSnapshot(refreshed);
      } catch {
        // Keep the confirmed artifact in local state if the refresh fails.
      }
      return;
    }

    const updatedAt = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    setTaskThread((current) => ({
      ...current,
      artifacts: current.artifacts.map((artifact) =>
        artifact.id === artifactId ? { ...artifact, status: "ready", updatedAt } : artifact,
      ),
      events: [
        ...current.events,
        {
          id: `evt-confirm-${Date.now()}`,
          type: "artifact_created",
          title: "Artifact 已确认",
          message: "用户确认该 Artifact 可用于后续关键操作。",
          timestamp: updatedAt,
          payload: { artifactId },
        },
      ],
    }));
  }

  async function completeCarePlanAction() {
    const careArtifact = taskThread.artifacts.find((artifact) => artifact.type === "care_plan");
    if (!careArtifact) return;
    await confirmArtifactAction(careArtifact.id);
  }

  function applyTaskSnapshot(task: SleepAgentTask) {
    setTaskThread(task);
    setTaskStatus(task.status);
    setCompletedCount(countCompletedPlanSteps(task));
    if (task.status === "completed" || task.status === "failed") {
      detachEventStream();
    }
  }

  function attachEventStream(taskId: string) {
    detachEventStream();
    eventUnsubscribeRef.current = subscribeTaskEvents({
      taskId,
      onEvent: (event) => {
        setTaskThread((current) => {
          if (current.events.some((item) => item.id === event.id)) {
            return current;
          }
          return { ...current, events: [...current.events, event] };
        });
        getSleepTask(taskId)
          .then(applyTaskSnapshot)
          .catch(() => undefined);
      },
      onConnectionError: (message) => {
        getSleepTask(taskId)
          .then(applyTaskSnapshot)
          .catch(() => undefined);
        setTaskThread((current) => {
          const event = buildLocalErrorEvent(message);
          return current.events.some((item) => item.id === event.id)
            ? current
            : { ...current, events: [...current.events, event] };
        });
      },
      onTerminalEvent: () => {
        getSleepTask(taskId)
          .then(applyTaskSnapshot)
          .catch(() => undefined);
      },
    });
  }

  function detachEventStream() {
    eventUnsubscribeRef.current?.();
    eventUnsubscribeRef.current = null;
  }

  function showTaskError(message: string) {
    setTaskStatus("failed");
    setTaskThread((current) => ({
      ...current,
      status: "failed",
      events: [...current.events, buildLocalErrorEvent(message)],
    }));
  }

  const roleTab = roleLabels[role];

  return (
    <AppShell
      activeView={activeView}
      onViewChange={setActiveView}
      status={status}
      onStart={startAnalysis}
      onReset={reset}
    >
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <Card className="min-w-72">
          <CardContent className="flex items-center gap-4 py-4">
            <div>
              <div className="text-xs text-muted">当前用户</div>
              <div className="mt-1 font-semibold">
                {sleepAnalysis.patient.name} · {sleepAnalysis.patient.age} 岁
              </div>
            </div>
            <div className="h-8 w-px bg-line" />
            <div>
              <div className="text-xs text-muted">本次记录</div>
              <div className="mt-1 font-semibold">
                {sleepAnalysis.patient.recordDate} · {sleepAnalysis.patient.durationMinutes} 分钟
              </div>
            </div>
          </CardContent>
        </Card>
        <Tabs
          tabs={Object.values(roleLabels)}
          active={roleTab}
          onChange={(tab) => setRole(roleByLabel[tab])}
        />
      </div>

      <AnimatePresence mode="wait">
        <motion.div
          key={activeView}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={{ duration: 0.18 }}
        >
          {activeView === "today" && (
            <TodayAnalysis
              data={sleepAnalysis}
              status={status}
              taskStatus={taskStatus}
              taskThread={taskThread}
              role={role}
              task={currentTask}
              onTaskChange={setCurrentTask}
              onStartTask={startAnalysis}
              onConfirmTask={confirmTaskRun}
              onCancelTask={cancelTask}
              onViewChange={setActiveView}
            />
          )}
          {activeView === "agent" && (
            <AgentRunPage
              task={taskThread}
              events={visibleEvents}
              taskStatus={taskStatus}
              completedCount={completedCount}
            />
          )}
          {activeView === "reports" && (
            <ReportCenter
              data={sleepAnalysis}
              role={role}
              task={taskThread}
              onUpdateArtifact={updateArtifact}
              onConfirmArtifact={confirmArtifactAction}
              onViewChange={setActiveView}
            />
          )}
          {activeView === "trends" && (
            <TrendFollowup
              trend={sleepAnalysis.trend}
              respirationTrend={sleepAnalysis.respirationTrend}
              sleepStages={sleepAnalysis.sleepStages}
              rawDataRows={sleepAnalysis.rawDataRows}
              medicalEvidence={sleepAnalysis.medicalEvidence}
              modelInsights={sleepAnalysis.modelInsights}
              developerDetails={sleepAnalysis.developerDetails}
            />
          )}
          {activeView === "chat" && (
            <ChatAgentPage
              data={sleepAnalysis}
              task={taskThread}
              role={role}
              onViewChange={setActiveView}
              onUpdateArtifact={updateArtifact}
              onConfirmCarePlan={completeCarePlanAction}
            />
          )}
          {activeView === "alerts" && (
            <AlertSettings
              carePlan={sleepAnalysis.carePlan}
              task={taskThread}
              onConfirmCarePlan={completeCarePlanAction}
            />
          )}
          {activeView === "data" && <DataManagement data={sleepAnalysis} task={taskThread} />}
        </motion.div>
      </AnimatePresence>
    </AppShell>
  );
}

function wait(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function countCompletedPlanSteps(task: SleepAgentTask): number {
  return task.plan.filter((step) => step.status === "completed").length;
}

function buildLocalErrorEvent(message: string) {
  const timestamp = new Date().toISOString();
  return {
    id: `frontend-error-${Date.now()}`,
    type: "error" as const,
    title: "后端任务连接失败",
    message,
    timestamp,
    payload: { source: "next_frontend" },
  };
}
