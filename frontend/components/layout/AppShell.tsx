"use client";

import type { ReactNode } from "react";
import { Sidebar } from "@/components/layout/Sidebar";
import { Topbar } from "@/components/layout/Topbar";
import type { RunStatus, ViewKey } from "@/lib/types";

export function AppShell({
  activeView,
  onViewChange,
  status,
  onStart,
  onReset,
  children,
}: {
  activeView: ViewKey;
  onViewChange: (view: ViewKey) => void;
  status: RunStatus;
  onStart: () => void;
  onReset: () => void;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-screen">
      <Sidebar active={activeView} onChange={onViewChange} />
      <div className="min-w-0 flex-1">
        <Topbar status={status} onStart={onStart} onReset={onReset} />
        <main className="mx-auto w-full max-w-[1480px] px-6 py-6">{children}</main>
      </div>
    </div>
  );
}
