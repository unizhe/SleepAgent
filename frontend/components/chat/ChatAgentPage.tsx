"use client";

import { useEffect, useState } from "react";
import { Bot, FileText, Search, Send, ShieldCheck } from "lucide-react";
import { askMockAgent } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import type { ChatMessage, Role, SleepAgentTask, SleepAnalysisMock, ViewKey } from "@/lib/types";

export function ChatAgentPage({
  data,
  task,
  role,
  onViewChange,
  onUpdateArtifact,
  onConfirmCarePlan,
}: {
  data: SleepAnalysisMock;
  task: SleepAgentTask;
  role: Role;
  onViewChange: (view: ViewKey) => void;
  onUpdateArtifact: (artifactId: string, content: string, revisionInstruction: string) => Promise<void> | void;
  onConfirmCarePlan: () => Promise<void> | void;
}) {
  const welcome = data.chatMessages[0];
  const suggestions = data.roleContent[role].chatSuggestions;
  const [messages, setMessages] = useState<ChatMessage[]>([welcome]);
  const [question, setQuestion] = useState(suggestions[0] ?? "");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setQuestion(suggestions[0] ?? "");
    setMessages([welcome]);
  }, [role, suggestions, welcome]);

  async function submit(nextQuestion = question) {
    if (!nextQuestion.trim()) return;
    setLoading(true);
    setMessages((current) => [...current, { role: "user", content: nextQuestion }]);
    const operationAnswer = await handleCommand(nextQuestion);
    if (operationAnswer) {
      setMessages((current) => [...current, operationAnswer]);
    } else {
      const answer = await askMockAgent(nextQuestion, role);
      setMessages((current) => [...current, answer]);
    }
    setQuestion("");
    setLoading(false);
  }

  async function handleCommand(command: string): Promise<ChatMessage | null> {
    if (command.includes("生成医生版") || command.includes("医生摘要") || command.includes("医生报告")) {
      onViewChange("reports");
      return {
        role: "assistant",
        content: "我已打开医生专业版 Artifact。你可以继续让我压缩摘要、复制给门诊沟通，或查看事件流和证据链。",
      };
    }

    if (command.includes("简单") || command.includes("写得简单")) {
      const elder = task.artifacts.find((artifact) => artifact.type === "elder_report");
      if (elder) {
        await onUpdateArtifact(
          elder.id,
          "这份记录提示睡眠呼吸有些不稳定，但血氧没有明显偏低。建议先连续观察几晚，记录打鼾、憋醒和白天困倦。如持续异常，请家人陪同咨询医生。",
          "把报告写得简单点",
        );
      }
      onViewChange("reports");
      return {
        role: "assistant",
        content: "我已把老人易懂版报告改得更简单，并跳转到 Artifact 工作区供你继续确认或导出。",
      };
    }

    if (command.includes("开启 7 天观察") || command.includes("7 天观察")) {
      await onConfirmCarePlan();
      onViewChange("alerts");
      return {
        role: "assistant",
        content: "已进入 7 天观察计划确认流程。关键提醒动作仍需要你确认提醒对象和方式。",
      };
    }

    if (command.includes("证据链")) {
      onViewChange("trends");
      return {
        role: "assistant",
        content: "我已打开趋势与证据页面，你可以查看医学依据、异常事件标记和模型解释。",
      };
    }

    return null;
  }

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-medium">问答 Agent</h2>
        <p className="mt-1 text-sm text-muted">围绕当前睡眠记录进行解释、报告解读和行动建议。</p>
      </div>
      <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
        <Card>
          <CardHeader>
            <CardTitle className="font-medium">当前记录上下文</CardTitle>
            <CardDescription>回答会优先引用本次记录。</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {[
              ["用户", data.patient.name],
              ["日期", data.patient.recordDate],
              ["AHI", "5.94"],
              ["风险", "中等风险"],
              ["关键事件", "疑似暂停 15 次，低通气 27 次"],
            ].map(([label, value]) => (
              <div key={label} className="rounded-md border border-line bg-background p-3">
                <div className="text-xs text-muted">{label}</div>
                <div className="mt-1 text-sm font-medium">{value}</div>
              </div>
            ))}
            <div className="rounded-md border border-sky-100 bg-sky-50 p-3 text-sm leading-6 text-muted">
              {data.roleContent[role].headline}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <CardTitle className="font-medium">SleepAgent Chat</CardTitle>
                <CardDescription>默认已加载本次 PSG 记录摘要和安全边界。</CardDescription>
              </div>
              <Badge variant="info">{role === "elder" ? "老人视角" : role === "family" ? "家属视角" : "医生视角"}</Badge>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap gap-2">
              {suggestions.map((item) => (
                <Button key={item} variant="outline" onClick={() => submit(item)} disabled={loading}>
                  {item}
                </Button>
              ))}
            </div>
            <div className="max-h-[420px] space-y-3 overflow-y-auto rounded-md border border-line bg-background p-3">
              {messages.map((message, index) => (
                <div
                  key={`${message.role}-${index}`}
                  className={message.role === "assistant" ? "flex justify-start" : "flex justify-end"}
                >
                  <div
                    className={
                      message.role === "assistant"
                        ? "max-w-[82%] rounded-md border border-brand-100 bg-brand-50 p-3"
                        : "max-w-[82%] rounded-md border border-line bg-white p-3 text-right"
                    }
                  >
                    {message.role === "assistant" && (
                      <div className="mb-2 flex items-center gap-2 text-xs font-medium text-brand-700">
                        <Bot className="h-4 w-4" />
                        SleepAgent
                      </div>
                    )}
                    <p className="text-sm leading-6">{message.content}</p>
                  </div>
                </div>
              ))}
            </div>
            {messages.length > 1 && (
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" onClick={() => onViewChange("today")}>
                  <Search className="h-4 w-4" />
                  查看证据链
                </Button>
                <Button variant="outline" onClick={() => onViewChange("reports")}>
                  <FileText className="h-4 w-4" />
                  生成医生报告
                </Button>
                <Button variant="secondary" onClick={() => onViewChange("alerts")}>
                  <ShieldCheck className="h-4 w-4" />
                  设置观察提醒
                </Button>
              </div>
            )}
            <div className="rounded-md border border-sky-100 bg-sky-50 p-3">
              <div className="text-sm font-medium">可触发操作</div>
              <div className="mt-2 flex flex-wrap gap-2">
                {["帮我生成医生版", "把报告写得简单点", "开启 7 天观察", "查看证据链"].map((command) => (
                  <Button key={command} size="sm" variant="outline" onClick={() => submit(command)} disabled={loading}>
                    {command}
                  </Button>
                ))}
              </div>
            </div>
            <div className="flex gap-2">
              <Input value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="继续追问本次报告" />
              <Button onClick={() => submit()} disabled={loading}>
                <Send className="h-4 w-4" />
                发送
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
