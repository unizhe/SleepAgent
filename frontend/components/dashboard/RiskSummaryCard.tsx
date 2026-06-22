import { AlertTriangle, CheckCircle2, ClipboardList } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { RiskLevel, Role, SleepAnalysisMock } from "@/lib/types";

const riskText: Record<RiskLevel, string> = {
  low: "低风险",
  moderate: "中等风险",
  high: "较高风险",
};

export function RiskSummaryCard({ data, role }: { data: SleepAnalysisMock; role: Role }) {
  const content = data.roleContent[role];

  return (
    <Card className="border-brand-100">
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="text-lg font-medium">今日风险结论</CardTitle>
          <Badge variant={data.riskLevel === "high" ? "danger" : data.riskLevel === "moderate" ? "warning" : "default"}>
            {riskText[data.riskLevel]}
          </Badge>
        </div>
        <p className="mt-3 max-w-4xl text-base leading-7 text-foreground">{content.headline}</p>
      </CardHeader>
      <CardContent>
        <div className="grid gap-4 md:grid-cols-3">
          <div className="rounded-md border border-line bg-brand-50 p-4">
            <CheckCircle2 className="h-5 w-5 text-brand-700" />
            <div className="mt-3 text-sm font-semibold">风险结论</div>
            <p className="mt-2 text-sm leading-6 text-muted">{content.riskConclusion}</p>
          </div>
          <div className="rounded-md border border-line bg-amber-50 p-4">
            <AlertTriangle className="h-5 w-5 text-amber-700" />
            <div className="mt-3 text-sm font-semibold">主要原因</div>
            <p className="mt-2 text-sm leading-6 text-muted">{content.primaryReason}</p>
          </div>
          <div className="rounded-md border border-line bg-sky-50 p-4">
            <ClipboardList className="h-5 w-5 text-sky-700" />
            <div className="mt-3 text-sm font-semibold">下一步行动</div>
            <p className="mt-2 text-sm leading-6 text-muted">{content.nextAction}</p>
          </div>
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          {content.recommendedActions.map((action) => (
            <Badge key={action} variant="info">
              {action}
            </Badge>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
