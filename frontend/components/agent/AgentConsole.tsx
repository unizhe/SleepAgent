import { TerminalSquare } from "lucide-react";
import type { AgentStep } from "@/lib/types";

export function AgentConsole({
  steps,
  completedCount,
}: {
  steps: AgentStep[];
  completedCount: number;
}) {
  return (
    <div className="p-5">
      <div className="mb-3 flex items-center gap-2 text-sm font-medium">
        <TerminalSquare className="h-5 w-5 text-brand-700" />
        Agent Run Console
      </div>
      <div className="space-y-2 font-mono text-xs leading-6">
        {steps.slice(0, Math.max(completedCount, 1)).map((step, index) => (
          <div key={step.id} className="rounded-md bg-[#171a1f] px-3 py-2 text-[#e8efe9]">
            <span className="text-[#8fd3c5]">[{String(index + 1).padStart(2, "0")}]</span>{" "}
            <span>{step.agent}</span>{" "}
            <span className="text-[#f5c26b]">{index < completedCount ? "completed" : "running"}</span>
            <div className="text-[#b7c2bd]">-&gt; {step.outputFinding}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
