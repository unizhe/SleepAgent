import { Wrench } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { AgentToolCall } from "@/lib/types";

export function ToolCallCard({ tool }: { tool: AgentToolCall }) {
  return (
    <div className="rounded-md border border-line bg-background p-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <Wrench className="h-4 w-4 shrink-0 text-brand-700" />
          <div className="truncate text-sm font-medium">{tool.name}</div>
        </div>
        <Badge variant="neutral">{tool.latencyMs} ms</Badge>
      </div>
      <p className="mt-2 text-xs leading-5 text-muted">{tool.detail}</p>
    </div>
  );
}
