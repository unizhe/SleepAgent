import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SleepAgent 雷达睡眠照护工作台",
  description: "SleepAgent 家庭睡眠观察与照护协同工作台。",
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
