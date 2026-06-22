"use client";

import { cn } from "@/lib/utils";

export function Tabs({
  tabs,
  active,
  onChange,
}: {
  tabs: string[];
  active: string;
  onChange: (tab: string) => void;
}) {
  return (
    <div className="inline-flex rounded-md border border-line bg-white p-1">
      {tabs.map((tab) => (
        <button
          key={tab}
          type="button"
          onClick={() => onChange(tab)}
          className={cn(
            "h-8 rounded px-3 text-sm font-medium transition",
            active === tab
              ? "bg-brand-600 text-white"
              : "text-muted hover:bg-black/[0.04] hover:text-foreground",
          )}
        >
          {tab}
        </button>
      ))}
    </div>
  );
}
