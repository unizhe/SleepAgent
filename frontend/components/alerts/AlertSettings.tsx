"use client";

import { useState } from "react";
import { BellRing, CalendarDays, HeartPulse, UserRoundCheck } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { CarePlan, SleepAgentTask } from "@/lib/types";

export function AlertSettings({
  carePlan,
  task,
  onConfirmCarePlan,
}: {
  carePlan: CarePlan;
  task: SleepAgentTask;
  onConfirmCarePlan: () => Promise<void> | void;
}) {
  const [feedback, setFeedback] = useState("当前为产品原型：不会发送真实站内信、家属消息或医生提醒。");
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const careArtifact = task.artifacts.find((artifact) => artifact.type === "care_plan");

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-medium">关怀计划</h2>
        <p className="mt-1 text-sm text-muted">把单晚风险结论延伸为未来几天的观察和照护闭环。</p>
      </div>

      <Card className="border-brand-100">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <CardTitle className="flex items-center gap-2 font-medium">
                <CalendarDays className="h-5 w-5 text-brand-700" />
                观察周期
              </CardTitle>
              <CardDescription>{carePlan.period}，重点观察呼吸稳定性和白天症状。</CardDescription>
            </div>
            <Badge variant="warning">中等风险随访</Badge>
          </div>
        </CardHeader>
        <CardContent className="grid gap-4 lg:grid-cols-3">
          <div className="rounded-md border border-amber-100 bg-amber-50 p-4">
            <HeartPulse className="h-5 w-5 text-amber-700" />
            <div className="mt-3 text-sm font-medium">关注指标</div>
            <div className="mt-3 space-y-2">
              {carePlan.indicators.map((item) => (
                <div key={item} className="text-sm leading-6 text-muted">- {item}</div>
              ))}
            </div>
          </div>
          <div className="rounded-md border border-sky-100 bg-sky-50 p-4">
            <UserRoundCheck className="h-5 w-5 text-sky-700" />
            <div className="mt-3 text-sm font-medium">提醒对象</div>
            <div className="mt-3 flex flex-wrap gap-2">
              {carePlan.recipients.map((item) => (
                <Badge key={item} variant="info">{item}</Badge>
              ))}
            </div>
          </div>
          <div className="rounded-md border border-line bg-background p-4">
            <BellRing className="h-5 w-5 text-brand-700" />
            <div className="mt-3 text-sm font-medium">提醒方式</div>
            <div className="mt-3 flex flex-wrap gap-2">
              {carePlan.channels.map((item) => (
                <Badge key={item} variant="neutral">{item}</Badge>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="font-medium">计划操作</CardTitle>
          <CardDescription>Ask Before Act：开启观察、发送家属摘要等关键动作必须先确认。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {careArtifact && (
            <div className="rounded-md border border-line bg-background p-3">
              <div className="flex items-center justify-between gap-3">
                <div className="text-sm font-medium">{careArtifact.title}</div>
                <Badge variant={careArtifact.status === "ready" ? "default" : "neutral"}>{careArtifact.status}</Badge>
              </div>
              <p className="mt-2 text-sm leading-6 text-muted">{careArtifact.content}</p>
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            {carePlan.actions.map((action) => (
              <Button
                key={action}
                variant={action.includes("开启") ? "default" : "outline"}
                onClick={() => {
                  setPendingAction(action);
                  setFeedback(`请确认是否执行“${action}”。`);
                }}
              >
                {action}
              </Button>
            ))}
          </div>
          {pendingAction && (
            <div className="rounded-md border border-amber-100 bg-amber-50 p-3">
              <div className="text-sm font-medium">确认操作</div>
              <p className="mt-2 text-sm leading-6 text-muted">关键动作“{pendingAction}”需要确认后才会进入执行状态。</p>
              <div className="mt-3 flex flex-wrap gap-2">
                <Button
                  size="sm"
                  onClick={async () => {
                    try {
                      if (pendingAction.includes("开启")) await onConfirmCarePlan();
                      setFeedback(`已确认“${pendingAction}”。当前原型不会发送真实通知。`);
                      setPendingAction(null);
                    } catch (error) {
                      setFeedback(`确认失败：${error instanceof Error ? error.message : "未知错误"}`);
                    }
                  }}
                >
                  确认执行
                </Button>
                <Button size="sm" variant="outline" onClick={() => setPendingAction(null)}>
                  暂不执行
                </Button>
              </div>
            </div>
          )}
          <div className="rounded-md border border-line bg-background p-3 text-sm text-muted">{feedback}</div>
        </CardContent>
      </Card>
    </div>
  );
}
