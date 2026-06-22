import { Check } from "lucide-react";
import { cn } from "@/lib/utils";
import type { AgentStep } from "@/lib/types";

export function AgentTimeline({
  steps,
  completedCount,
}: {
  steps: AgentStep[];
  completedCount: number;
}) {
  return (
    <div className="grid gap-2 lg:grid-cols-6">
      {steps.map((step, index) => {
        const done = index < completedCount;
        const active = index === completedCount;
        return (
          <div
            key={step.id}
            className={cn(
              "rounded-md border p-3",
              done ? "border-brand-100 bg-brand-50" : active ? "border-amber-100 bg-amber-50" : "border-line bg-white",
            )}
          >
            <div className="flex items-center gap-2">
              <div className={cn("flex h-6 w-6 items-center justify-center rounded text-xs font-semibold", done ? "bg-brand-600 text-white" : "bg-white text-muted")}>
                {done ? <Check className="h-4 w-4" /> : step.id}
              </div>
              <div className="truncate text-sm font-medium">{step.agent.replace(" Agent", "")}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
