import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type { Metric, Role } from "@/lib/types";

export function MetricCard({
  metric,
  role,
  onExplain,
}: {
  metric: Metric;
  role: Role;
  onExplain?: (metricKey: string) => void;
}) {
  return (
    <Card className="transition hover:border-brand-100">
      <CardContent>
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-sm text-muted">{metric.label}</div>
            <div className="mt-2 text-2xl font-semibold">{metric.value}</div>
          </div>
          <Badge variant={metric.status.includes("关注") || metric.status.includes("异常") ? "warning" : "default"}>
            {metric.status}
          </Badge>
        </div>
        <p className="mt-3 text-sm leading-6 text-muted">{metric.roleExplanations[role]}</p>
        <p className="mt-2 text-xs leading-5 text-muted">{metric.reference}</p>
        {onExplain && (
          <button
            type="button"
            onClick={() => onExplain(metric.key)}
            className="mt-3 text-xs font-medium text-brand-700 hover:underline"
          >
            为什么？
          </button>
        )}
      </CardContent>
    </Card>
  );
}
