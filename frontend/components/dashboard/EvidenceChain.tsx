import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { EvidenceItem } from "@/lib/types";

export function EvidenceChain({ evidence }: { evidence: EvidenceItem[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>证据链解释</CardTitle>
        <CardDescription>系统如何从指标、事件和安全边界得到当前风险结论。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {evidence.map((item, index) => (
          <div key={item.title} className="flex gap-3 rounded-md border border-line bg-white p-4">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-brand-50 text-sm font-semibold text-brand-700">
              {index + 1}
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center justify-between gap-3">
                <div className="font-medium">{item.title}</div>
                <Badge variant={item.severity === "danger" ? "danger" : item.severity === "warning" ? "warning" : "neutral"}>
                  {item.severity === "warning" ? "关键证据" : "参考证据"}
                </Badge>
              </div>
              <p className="mt-2 text-sm leading-6 text-muted">{item.body}</p>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
