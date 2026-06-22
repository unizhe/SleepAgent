"use client";

import { Play, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import type { RunStatus, TaskTemplate } from "@/lib/types";

export function TaskComposer({
  templates,
  value,
  status,
  onChange,
  onStart,
}: {
  templates: TaskTemplate[];
  value: string;
  status: RunStatus;
  onChange: (value: string) => void;
  onStart: () => void;
}) {
  return (
    <Card className="border-brand-100">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-lg font-medium">
              <Sparkles className="h-5 w-5 text-brand-700" />
              今天想让 SleepAgent 帮你做什么？
            </CardTitle>
            <p className="mt-2 text-sm leading-6 text-muted">
              选择一个任务模板，或直接描述你想让 Agent 完成的睡眠健康任务。
            </p>
          </div>
          <Badge variant={status === "completed" ? "default" : status === "running" ? "warning" : "neutral"}>
            {status === "completed" ? "已完成一次任务" : status === "running" ? "任务执行中" : "等待任务"}
          </Badge>
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid gap-3 lg:grid-cols-5">
          {templates.map((template) => (
            <button
              key={template.id}
              type="button"
              onClick={() => onChange(template.prompt)}
              className="rounded-md border border-line bg-white p-3 text-left transition hover:border-brand-100 hover:bg-brand-50"
            >
              <div className="text-sm font-medium">{template.title}</div>
              <p className="mt-2 text-xs leading-5 text-muted">{template.description}</p>
            </button>
          ))}
        </div>
        <div className="mt-4 flex flex-col gap-3 lg:flex-row">
          <Textarea value={value} onChange={(event) => onChange(event.target.value)} className="min-h-20 flex-1" />
          <Button className="lg:self-end" size="lg" onClick={onStart} disabled={status === "running" || !value.trim()}>
            <Play className="h-4 w-4" />
            开始任务
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
