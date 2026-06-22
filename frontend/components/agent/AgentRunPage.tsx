"use client";

import { CheckCircle2, Clock3, Loader2, Radio, Wrench } from "lucide-react";
import { motion } from "framer-motion";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";
import type { AgentEvent, SleepAgentTask, TaskStatus } from "@/lib/types";

const eventLabels: Record<AgentEvent["type"], string> = {
  task_created: "任务",
  plan_created: "计划",
  step_started: "步骤",
  tool_called: "工具",
  finding_created: "发现",
  artifact_created: "产物",
  step_completed: "完成",
  task_completed: "完成",
  error: "错误",
};

export function AgentRunPage({
  task,
  events,
  taskStatus,
  completedCount,
}: {
  task: SleepAgentTask;
  events: AgentEvent[];
  taskStatus: TaskStatus;
  completedCount: number;
}) {
  const progress =
    taskStatus === "completed" ? 100 : taskStatus === "running" ? Math.round((completedCount / task.plan.length) * 100) : 0;

  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="max-w-3xl">
          <h2 className="text-xl font-medium">任务线程</h2>
          <p className="mt-2 text-sm leading-6 text-muted">{task.userGoal}</p>
        </div>
        <div className="w-full max-w-xs">
          <Progress value={progress} />
          <div className="mt-2 flex items-center justify-between text-xs text-muted">
            <span>{taskStatus}</span>
            <span>{progress}%</span>
          </div>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-[0.9fr_1.25fr_0.95fr]">
        <Card>
          <CardHeader>
            <CardTitle className="font-medium">Agent Plan</CardTitle>
            <CardDescription>未来后端可直接返回同构计划节点。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {task.plan.map((step, index) => {
              const Icon = step.status === "completed" ? CheckCircle2 : step.status === "running" ? Loader2 : Clock3;
              return (
                <details
                  key={step.id}
                  open={step.status === "running"}
                  className={cn(
                    "rounded-md border p-3",
                    step.status === "completed"
                      ? "border-brand-100 bg-brand-50"
                      : step.status === "running"
                        ? "border-amber-100 bg-amber-50"
                        : "border-line bg-white",
                  )}
                >
                  <summary className="cursor-pointer list-none">
                    <div className="flex gap-3">
                      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-white">
                        <Icon className={cn("h-4 w-4", step.status === "running" && "animate-spin text-amber-700", step.status === "completed" && "text-brand-700")} />
                      </div>
                      <div className="min-w-0">
                        <div className="text-sm font-medium">{index + 1}. {step.title}</div>
                        <div className="mt-1 text-xs text-muted">{step.agent} · {step.durationMs} ms</div>
                      </div>
                    </div>
                  </summary>
                  <p className="mt-3 text-xs leading-5 text-muted">{step.description}</p>
                  <div className="mt-3 space-y-2">
                    {step.toolCalls.map((tool) => (
                      <div key={tool.id} className="rounded-md border border-line bg-white p-3">
                        <div className="flex items-center justify-between gap-2">
                          <div className="flex min-w-0 items-center gap-2 text-sm font-medium">
                            <Wrench className="h-4 w-4 text-brand-700" />
                            <span className="truncate">{tool.toolName}</span>
                          </div>
                          <Badge variant={tool.status === "success" ? "default" : tool.status === "running" ? "warning" : "danger"}>
                            {tool.status}
                          </Badge>
                        </div>
                        <p className="mt-2 text-xs leading-5 text-muted">输入：{tool.inputSummary}</p>
                        <p className="mt-1 text-xs leading-5 text-muted">输出：{tool.outputSummary}</p>
                      </div>
                    ))}
                  </div>
                </details>
              );
            })}
          </CardContent>
        </Card>

        <Card className="border-sky-100">
          <CardHeader>
            <div className="flex items-center justify-between gap-3">
              <CardTitle className="flex items-center gap-2 font-medium">
                <Radio className="h-5 w-5 text-sky-700" />
                事件流
              </CardTitle>
              <Badge variant="info">SSE/WebSocket ready shape</Badge>
            </div>
            <CardDescription>事件来自后端任务 API；mock mode 仍可用于无后端开发。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {events.length > 0 ? (
              events.map((event) => (
                <div key={event.id} className="rounded-md border border-line bg-white p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="font-medium">{event.title}</div>
                    <div className="flex items-center gap-2">
                      <Badge variant={event.type === "error" ? "danger" : event.type === "tool_called" ? "warning" : "neutral"}>
                        {eventLabels[event.type]}
                      </Badge>
                      <span className="text-xs text-muted">{event.timestamp}</span>
                    </div>
                  </div>
                  <p className="mt-2 text-sm leading-6 text-muted">{event.message}</p>
                  {event.payload ? (
                    <details className="mt-2">
                      <summary className="cursor-pointer text-xs font-medium text-muted">查看 payload</summary>
                      <pre className="mt-2 overflow-x-auto rounded-md bg-slate-50 p-2 text-xs">
                        {JSON.stringify(event.payload, null, 2)}
                      </pre>
                    </details>
                  ) : null}
                </div>
              ))
            ) : (
              <div className="rounded-md border border-line bg-background p-5 text-sm text-muted">
                任务确认后，这里会按事件流展示工具调用、发现和 Artifact 创建。
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="font-medium">Artifacts</CardTitle>
            <CardDescription>任务执行产生的可迭代产物。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {task.artifacts.map((artifact) => (
              <div key={artifact.id} className="rounded-md border border-line bg-white p-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="font-medium">{artifact.title}</div>
                  <Badge variant={artifact.status === "draft" ? "neutral" : artifact.status === "revised" ? "warning" : "default"}>
                    {artifact.status}
                  </Badge>
                </div>
                <p className="mt-2 line-clamp-3 text-sm leading-6 text-muted">{artifact.content}</p>
                <div className="mt-2 text-xs text-muted">createdBy: {artifact.createdByStepId}</div>
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
    </motion.div>
  );
}
