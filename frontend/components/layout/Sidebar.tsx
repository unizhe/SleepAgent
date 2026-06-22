"use client";

import { Activity, Bell, Bot, Database, FileText, LineChart, Moon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { NavigationItem, ViewKey } from "@/lib/types";

const items: NavigationItem[] = [
  { key: "today", label: "今日分析", description: "下达任务和决策", icon: Activity },
  { key: "agent", label: "Agent Run", description: "Plan / Act / Result", icon: Bot },
  { key: "reports", label: "报告生成器", description: "多角色报告", icon: FileText },
  { key: "trends", label: "趋势随访", description: "7 日变化", icon: LineChart },
  { key: "chat", label: "问答 Agent", description: "追问解释", icon: Moon },
  { key: "alerts", label: "关怀计划", description: "7 天观察闭环", icon: Bell },
  { key: "data", label: "睡眠记录", description: "记录与质量", icon: Database },
];

export function Sidebar({
  active,
  onChange,
}: {
  active: ViewKey;
  onChange: (view: ViewKey) => void;
}) {
  return (
    <aside className="flex h-screen w-72 shrink-0 flex-col border-r border-line bg-white">
      <div className="border-b border-line px-5 py-5">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-md bg-brand-600 text-white">
            <Moon className="h-5 w-5" />
          </div>
          <div>
            <div className="text-base font-semibold">SleepAgent</div>
            <div className="text-xs text-muted">睡眠健康工作台</div>
          </div>
        </div>
      </div>
      <div className="border-b border-line px-4 py-4">
        <div className="rounded-md bg-background p-3">
          <div className="text-xs text-muted">当前用户</div>
          <div className="mt-1 text-sm font-medium">张阿姨 · 68 岁</div>
          <div className="mt-3 grid grid-cols-2 gap-2 text-xs leading-5 text-muted">
            <div>
              <div>记录日期</div>
              <div className="font-medium text-foreground">2026-01-02</div>
            </div>
            <div>
              <div>数据来源</div>
              <div className="font-medium text-foreground">SHHS / PSG</div>
            </div>
            <div>
              <div>记录时长</div>
              <div className="font-medium text-foreground">480 分钟</div>
            </div>
            <div>
              <div>分析状态</div>
              <div className="font-medium text-foreground">已完成</div>
            </div>
          </div>
        </div>
      </div>
      <nav className="flex-1 space-y-1 px-3 py-4">
        {items.map((item) => {
          const Icon = item.icon;
          const isActive = active === item.key;
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => onChange(item.key)}
              className={cn(
                "flex w-full items-center gap-3 rounded-md px-3 py-3 text-left transition",
                isActive ? "bg-brand-50 text-brand-700" : "text-foreground hover:bg-black/[0.04]",
              )}
            >
              <Icon className="h-5 w-5 shrink-0" />
              <span className="min-w-0">
                <span className="block text-sm font-medium">{item.label}</span>
                <span className="block truncate text-xs text-muted">{item.description}</span>
              </span>
            </button>
          );
        })}
      </nav>
      <div className="border-t border-line p-4">
        <div className="mb-3 grid grid-cols-2 gap-2">
          {[
            { label: "历史报告", view: "reports" as ViewKey },
            { label: "咨询 Agent", view: "chat" as ViewKey },
            { label: "关怀计划", view: "alerts" as ViewKey },
            { label: "原始数据", view: "trends" as ViewKey },
          ].map((item) => (
            <button
              key={item.label}
              type="button"
              onClick={() => onChange(item.view)}
              className="rounded-md border border-line bg-white px-2 py-2 text-xs font-medium transition hover:bg-brand-50"
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="rounded-md bg-background p-3 text-xs leading-5 text-muted">
          当前为 React 产品原型；后续可在 lib/api.ts 接入 FastAPI。
        </div>
      </div>
    </aside>
  );
}
