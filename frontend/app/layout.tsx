import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SleepAgent 工作台",
  description: "Agent-driven sleep health analysis workbench.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
