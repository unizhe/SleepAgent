import { CheckCircle2, Clock3, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { ToolCallCard } from "@/components/agent/ToolCallCard";
import type { AgentStep } from "@/lib/types";

export function AgentStepCard({ step, active }: { step: AgentStep; active?: boolean }) {
  const Icon = step.status === "done" ? CheckCircle2 : step.status === "running" ? Loader2 : Clock3;
  return (
    <Card className={active ? "border-brand-500" : undefined}>
      <CardContent>
        <div className="flex items-start gap-4">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-brand-50 text-brand-700">
            <Icon className={step.status === "running" ? "h-5 w-5 animate-spin" : "h-5 w-5"} />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="text-sm text-muted">Step {step.id}</div>
                <div className="font-semibold">{step.agent}</div>
              </div>
              <div className="flex items-center gap-2">
                <Badge variant={step.status === "done" ? "default" : step.status === "running" ? "warning" : "neutral"}>
                  {step.status}
                </Badge>
                <Badge variant="neutral">{step.elapsedMs} ms</Badge>
              </div>
            </div>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <div>
                <div className="text-xs font-medium text-muted">输入摘要</div>
                <p className="mt-1 text-sm leading-6">{step.inputSummary}</p>
              </div>
              <div>
                <div className="text-xs font-medium text-muted">输出发现</div>
                <p className="mt-1 text-sm leading-6">{step.outputFinding}</p>
              </div>
            </div>
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              {step.tools.map((tool) => (
                <ToolCallCard key={tool.name} tool={tool} />
              ))}
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
