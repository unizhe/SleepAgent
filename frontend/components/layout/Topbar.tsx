"use client";

import { Download, Play, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { RunStatus } from "@/lib/types";

export function Topbar({
  status,
  onStart,
  onReset,
}: {
  status: RunStatus;
  onStart: () => void;
  onReset: () => void;
}) {
  const label = status === "completed" ? "任务完成" : status === "running" ? "执行中" : "等待任务";
  return (
    <header className="sticky top-0 z-30 flex h-16 items-center justify-between border-b border-line bg-background/95 px-6 backdrop-blur">
      <div className="min-w-0">
        <h1 className="truncate text-lg font-medium">SleepAgent 任务驱动健康助手</h1>
        <p className="truncate text-sm text-muted">下达任务、查看 Agent 计划、生成报告并跟进关怀计划</p>
      </div>
      <div className="flex items-center gap-2">
        <Badge variant={status === "completed" ? "default" : status === "running" ? "warning" : "neutral"}>
          {label}
        </Badge>
        <Badge variant="default">数据质量良好</Badge>
        <Badge variant={status === "completed" ? "info" : "neutral"}>报告已生成</Badge>
        <Button variant="outline" onClick={onReset}>
          <RefreshCw className="h-4 w-4" />
          重置
        </Button>
        <Button onClick={onStart} disabled={status === "running"}>
          <Play className="h-4 w-4" />
          开始任务
        </Button>
        <Button variant="secondary" disabled={status !== "completed"}>
          <Download className="h-4 w-4" />
          导出
        </Button>
      </div>
    </header>
  );
}
