"use client";

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Download, Info } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { MedicalEvidence, ModelInsight, RawDataRow, RespirationPoint, SleepStagePoint, TrendPoint } from "@/lib/types";

const stageTicks: Record<number, string> = {
  0: "NREM",
  1: "REM",
  2: "Wake",
};

export function TrendFollowup({
  trend,
  respirationTrend,
  sleepStages,
  rawDataRows,
  medicalEvidence,
  modelInsights,
  developerDetails,
}: {
  trend: TrendPoint[];
  respirationTrend: RespirationPoint[];
  sleepStages: SleepStagePoint[];
  rawDataRows: RawDataRow[];
  medicalEvidence: MedicalEvidence[];
  modelInsights: ModelInsight[];
  developerDetails: Array<{ label: string; value: string }>;
}) {
  const eventPoints = respirationTrend.filter((point) => point.event);

  function downloadCsv() {
    const header = "minute,clockTime,breathingRate,spo2,sleepStage,event";
    const rows = rawDataRows.map((row) =>
      [row.minute, row.clockTime, row.breathingRate, row.spo2, row.sleepStage, row.event].join(","),
    );
    downloadText("sleepagent_raw_trend.csv", [header, ...rows].join("\n"));
  }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-medium">趋势随访</h2>
        <p className="mt-1 text-sm text-muted">用历史趋势、睡眠阶段和异常事件标记解释单晚风险。</p>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="font-medium">7 日风险趋势</CardTitle>
            <CardDescription>AHI、最低血氧和睡眠效率共同判断是否持续异常。</CardDescription>
          </CardHeader>
          <CardContent className="h-80">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={trend}>
                <CartesianGrid stroke="#dde2dc" strokeDasharray="3 3" />
                <XAxis dataKey="night" />
                <YAxis yAxisId="left" />
                <YAxis yAxisId="right" orientation="right" domain={[90, 100]} />
                <Tooltip />
                <Legend />
                <ReferenceLine yAxisId="left" y={5} stroke="#c3831f" strokeDasharray="4 4" label="AHI 5" />
                <Bar yAxisId="left" dataKey="apneaCount" fill="#f8d88a" name="疑似暂停" />
                <Line yAxisId="left" type="monotone" dataKey="ahi" stroke="#2c8f7b" strokeWidth={2} name="AHI" />
                <Line yAxisId="right" type="monotone" dataKey="minSpo2" stroke="#2563eb" strokeWidth={2} name="最低 SpO2" />
                <Line yAxisId="left" type="monotone" dataKey="sleepEfficiency" stroke="#855817" strokeWidth={2} name="睡眠效率" />
              </ComposedChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="font-medium">呼吸事件与血氧趋势</CardTitle>
            <CardDescription>异常事件标记点用于定位风险时间段，背景显示 REM 片段。</CardDescription>
          </CardHeader>
          <CardContent className="h-80">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={respirationTrend}>
                <CartesianGrid stroke="#dde2dc" strokeDasharray="3 3" />
                <XAxis dataKey="clockTime" />
                <YAxis domain={[90, 100]} />
                <Tooltip />
                <Legend />
                <ReferenceArea x1="00:30" x2="00:45" strokeOpacity={0} fill="#dbeafe" fillOpacity={0.35} />
                <ReferenceArea x1="02:00" x2="02:15" strokeOpacity={0} fill="#dbeafe" fillOpacity={0.35} />
                <ReferenceArea x1="04:30" x2="04:30" strokeOpacity={0} fill="#dbeafe" fillOpacity={0.35} />
                <ReferenceLine y={94} stroke="#d1495b" strokeDasharray="4 4" label="SpO2 94%" />
                <Area type="monotone" dataKey="spo2" stroke="#2c8f7b" fill="#d6eee7" name="SpO2" />
                <Scatter data={eventPoints} dataKey="spo2" fill="#d1495b" name="异常事件" />
              </AreaChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
        <Card>
          <CardHeader>
            <CardTitle className="font-medium">睡眠分期图</CardTitle>
            <CardDescription>Wake / REM / NREM 随夜间时间变化，帮助理解异常事件发生背景。</CardDescription>
          </CardHeader>
          <CardContent className="h-72">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={sleepStages}>
                <CartesianGrid stroke="#dde2dc" strokeDasharray="3 3" />
                <XAxis dataKey="clockTime" />
                <YAxis type="number" domain={[0, 2]} ticks={[0, 1, 2]} tickFormatter={(value) => stageTicks[value] ?? String(value)} />
                <Tooltip formatter={(value) => stageTicks[Number(value)] ?? value} />
                <Line type="stepAfter" dataKey="stageValue" stroke="#237462" strokeWidth={3} dot={{ r: 3 }} name="睡眠阶段" />
              </LineChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="font-medium">呼吸异常事件构成</CardTitle>
            <CardDescription>疑似暂停与低通气数量决定本次 AHI 解释。</CardDescription>
          </CardHeader>
          <CardContent className="h-72">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={[
                { name: "疑似暂停", value: 15, fill: "#d1495b" },
                { name: "低通气", value: 27, fill: "#c3831f" },
                { name: "正常呼吸窗口", value: 438, fill: "#2c8f7b" },
              ]}>
                <CartesianGrid stroke="#dde2dc" strokeDasharray="3 3" />
                <XAxis dataKey="name" />
                <YAxis />
                <Tooltip />
                <Bar dataKey="value" name="次数">
                  {["#d1495b", "#c3831f", "#2c8f7b"].map((color) => (
                    <Cell key={color} fill={color} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 font-medium">
            <Info className="h-5 w-5 text-sky-700" />
            医学依据与模型解释
          </CardTitle>
          <CardDescription>P2 能力入口：展示检索依据、模型置信度和可解释性摘要。</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 lg:grid-cols-2">
          <div className="space-y-3">
            {medicalEvidence.map((item) => (
              <div key={item.title} className="rounded-md border border-sky-100 bg-sky-50 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="font-medium">{item.title}</div>
                  <Badge variant="info">{item.source}</Badge>
                </div>
                <p className="mt-2 text-sm leading-6 text-muted">{item.excerpt}</p>
                <p className="mt-2 text-xs leading-5 text-muted">{item.relevance}</p>
              </div>
            ))}
          </div>
          <div className="space-y-3">
            {modelInsights.map((item) => (
              <div key={item.label} className="rounded-md border border-line bg-background p-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="font-medium">{item.label}</div>
                  <Badge variant="neutral">{item.value}</Badge>
                </div>
                <p className="mt-2 text-sm leading-6 text-muted">{item.explanation}</p>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      <details className="rounded-lg border border-line bg-white shadow-soft">
        <summary className="cursor-pointer px-5 py-4 text-sm font-medium">查看原始数据</summary>
        <div className="border-t border-line p-5">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-muted">minute、呼吸频率、SpO2、睡眠阶段和事件标签默认折叠。</p>
            <Button variant="outline" onClick={downloadCsv}>
              <Download className="h-4 w-4" />
              导出 CSV
            </Button>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] border-collapse text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-muted">
                  <th className="py-2 pr-3">Minute</th>
                  <th className="py-2 pr-3">时间</th>
                  <th className="py-2 pr-3">呼吸频率</th>
                  <th className="py-2 pr-3">SpO2</th>
                  <th className="py-2 pr-3">睡眠阶段</th>
                  <th className="py-2 pr-3">事件</th>
                </tr>
              </thead>
              <tbody>
                {rawDataRows.map((row) => (
                  <tr key={`${row.minute}-${row.event}`} className="border-b border-line/70">
                    <td className="py-2 pr-3">{row.minute}</td>
                    <td className="py-2 pr-3">{row.clockTime}</td>
                    <td className="py-2 pr-3">{row.breathingRate}</td>
                    <td className="py-2 pr-3">{row.spo2}%</td>
                    <td className="py-2 pr-3">{row.sleepStage}</td>
                    <td className="py-2 pr-3">{row.event}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </details>

      <details className="rounded-lg border border-line bg-white shadow-soft">
        <summary className="cursor-pointer px-5 py-4 text-sm font-medium">开发者模式</summary>
        <div className="grid gap-3 border-t border-line p-5 md:grid-cols-2">
          {developerDetails.map((item) => (
            <div key={item.label} className="rounded-md border border-line bg-slate-50 p-3">
              <div className="text-xs font-medium text-muted">{item.label}</div>
              <div className="mt-1 font-mono text-xs leading-5">{item.value}</div>
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}

function downloadText(filename: string, content: string) {
  const blob = new Blob([content], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}
