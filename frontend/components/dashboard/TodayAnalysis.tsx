"use client";

import { useMemo, useState } from "react";
import { motion } from "framer-motion";
import { FileText, HeartPulse, MessageSquare } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { MetricCard } from "@/components/dashboard/MetricCard";
import { RiskSummaryCard } from "@/components/dashboard/RiskSummaryCard";
import { EvidenceChain } from "@/components/dashboard/EvidenceChain";
import { TaskComposer } from "@/components/dashboard/TaskComposer";
import { DetailDrawer } from "@/components/ui/detail-drawer";
import type { Role, RunStatus, SleepAgentTask, TaskStatus, SleepAnalysisMock, ViewKey } from "@/lib/types";

export function TodayAnalysis({
  data,
  status,
  taskStatus,
  taskThread,
  role,
  task,
  onTaskChange,
  onStartTask,
  onConfirmTask,
  onCancelTask,
  onViewChange,
}: {
  data: SleepAnalysisMock;
  status: RunStatus;
  taskStatus: TaskStatus;
  taskThread: SleepAgentTask;
  role: Role;
  task: string;
  onTaskChange: (task: string) => void;
  onStartTask: () => void;
  onConfirmTask: () => void;
  onCancelTask: () => void;
  onViewChange: (view: ViewKey) => void;
}) {
  const content = data.roleContent[role];
  const [metricDrawerKey, setMetricDrawerKey] = useState<string | null>(null);
  const activeMetricExplanation = useMemo(
    () => data.metricExplanations.find((item) => item.metricKey === metricDrawerKey),
    [data.metricExplanations, metricDrawerKey],
  );
  const visibleMetricKeys =
    role === "doctor" ? data.metrics.map((metric) => metric.key) : role === "family" ? ["ahi", "apnea", "hypopnea", "spo2"] : ["ahi", "efficiency", "spo2"];
  const visibleMetrics = data.metrics.filter((metric) => visibleMetricKeys.includes(metric.key));

  if (status === "idle") {
    return (
      <div className="space-y-5">
        <TaskComposer
          templates={data.taskTemplates}
          value={task}
          status={status}
          onChange={onTaskChange}
          onStart={onStartTask}
        />
        {taskStatus === "awaiting_confirmation" && (
          <Card className="border-amber-100">
            <CardHeader>
              <CardTitle className="font-medium">SleepAgent 已生成任务计划</CardTitle>
              <CardDescription>v5 采用 Ask Before Act：确认计划后才会执行工具调用。</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-3 lg:grid-cols-3">
                {taskThread.plan.map((step, index) => (
                  <div key={step.id} className="rounded-md border border-line bg-white p-3">
                    <div className="text-xs text-muted">Step {index + 1}</div>
                    <div className="mt-1 text-sm font-medium">{step.title}</div>
                    <p className="mt-2 text-xs leading-5 text-muted">{step.description}</p>
                  </div>
                ))}
              </div>
              <div className="flex flex-wrap gap-2">
                <Button onClick={onConfirmTask}>确认执行</Button>
                <Button variant="outline" onClick={() => onTaskChange(`${task} 请把报告语气写得更适合家属沟通。`)}>
                  修改任务目标
                </Button>
                <Button variant="ghost" onClick={onCancelTask}>取消任务</Button>
              </div>
            </CardContent>
          </Card>
        )}
        <Card>
          <CardHeader>
            <CardTitle className="font-medium">等待下达任务</CardTitle>
            <CardDescription>开始任务后，SleepAgent 会先制定计划，再执行分析并逐步产生发现。</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid gap-3 md:grid-cols-3">
              {["风险结论", "主要原因", "下一步行动"].map((item) => (
                <div key={item} className="rounded-md border border-line bg-background p-4 text-sm font-medium">
                  {item}
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} className="space-y-5">
      <TaskComposer
        templates={data.taskTemplates}
        value={task}
        status={status}
        onChange={onTaskChange}
        onStart={onStartTask}
      />
      {taskStatus === "awaiting_confirmation" && (
        <Card className="border-amber-100">
          <CardHeader>
            <CardTitle className="font-medium">等待用户确认执行</CardTitle>
            <CardDescription>计划已生成，但还没有调用任何分析工具。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2">
            <Button onClick={onConfirmTask}>确认执行</Button>
            <Button variant="outline" onClick={() => onViewChange("agent")}>查看计划详情</Button>
            <Button variant="ghost" onClick={onCancelTask}>取消任务</Button>
          </CardContent>
        </Card>
      )}
      <RiskSummaryCard data={data} role={role} />
      <Card>
        <CardHeader>
          <CardTitle className="font-medium">推荐下一步</CardTitle>
          <CardDescription>根据当前角色优先展示最可能需要的操作。</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => onViewChange("chat")}>
            <MessageSquare className="h-4 w-4" />
            咨询 SleepAgent
          </Button>
          <Button variant="secondary" onClick={() => onViewChange("alerts")}>
            <HeartPulse className="h-4 w-4" />
            {content.recommendedActions[0]}
          </Button>
          <Button onClick={() => onViewChange("reports")}>
            <FileText className="h-4 w-4" />
            查看{data.reports.find((report) => report.id === content.defaultReportId)?.title}
          </Button>
        </CardContent>
      </Card>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {visibleMetrics.map((metric) => (
          <MetricCard key={metric.key} metric={metric} role={role} onExplain={setMetricDrawerKey} />
        ))}
      </div>
      <EvidenceChain evidence={data.evidence} />
      <DetailDrawer
        open={Boolean(activeMetricExplanation)}
        title={activeMetricExplanation?.title ?? "指标解释"}
        description="SleepAgent 判断依据"
        onClose={() => setMetricDrawerKey(null)}
      >
        {activeMetricExplanation && (
          <div className="space-y-4">
            {[
              ["指标含义", activeMetricExplanation.meaning],
              ["当前值", activeMetricExplanation.currentValue],
              ["参考范围", activeMetricExplanation.referenceRange],
              ["风险意义", activeMetricExplanation.riskMeaning],
              ["判断依据", activeMetricExplanation.agentRationale],
            ].map(([label, value]) => (
              <div key={label} className="rounded-md border border-line bg-background p-3">
                <div className="text-xs font-medium text-muted">{label}</div>
                <p className="mt-2 text-sm leading-6">{value}</p>
              </div>
            ))}
          </div>
        )}
      </DetailDrawer>
    </motion.div>
  );
}
