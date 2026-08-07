import { NextResponse } from "next/server";

const BACKEND_API_BASE_URL =
  process.env.SLEEPAGENT_API_BASE_URL ??
  process.env.NEXT_PUBLIC_SLEEPAGENT_API_BASE_URL ??
  "http://127.0.0.1:18000";
const PRODUCT_RADAR_API_KEY = process.env.SLEEPAGENT_PRODUCT_RADAR_API_KEY;
const RADAR_AGENT_API_KEY =
  process.env.SLEEPAGENT_RADAR_AGENT_API_KEY ?? PRODUCT_RADAR_API_KEY;
const RADAR_AGENT_ACTOR_ID = process.env.SLEEPAGENT_RADAR_AGENT_ACTOR_ID;
const RADAR_AGENT_ACTOR_ROLE = process.env.SLEEPAGENT_RADAR_AGENT_ACTOR_ROLE;
const RADAR_AGENT_SUBJECT_ID =
  process.env.SLEEPAGENT_RADAR_AGENT_SUBJECT_ID;
const RADAR_AGENT_AUTHORIZATION_ID =
  process.env.SLEEPAGENT_RADAR_AGENT_AUTHORIZATION_ID;
const RADAR_AGENT_ROLE_BINDING_IDS =
  process.env.SLEEPAGENT_RADAR_AGENT_ROLE_BINDING_IDS;
const HABIT_PROFILE_ACTOR_ID = process.env.SLEEPAGENT_HABIT_PROFILE_ACTOR_ID;
const HABIT_PROFILE_ACTOR_ROLE =
  process.env.SLEEPAGENT_HABIT_PROFILE_ACTOR_ROLE;
const HABIT_PROFILE_SUBJECT_ID =
  process.env.SLEEPAGENT_HABIT_PROFILE_SUBJECT_ID;
const HABIT_PROFILE_AUTHORIZATION_SCOPES =
  process.env.SLEEPAGENT_HABIT_PROFILE_AUTHORIZATION_SCOPES ?? "";

type RouteContext = {
  params: Promise<{
    path?: string[];
  }>;
};

export async function GET(request: Request, context: RouteContext) {
  return proxyRadarRequest(request, context);
}

export async function POST(request: Request, context: RouteContext) {
  return proxyRadarRequest(request, context);
}

async function proxyRadarRequest(request: Request, context: RouteContext) {
  const { path = [] } = await context.params;
  const upstreamPath = `/${path.join("/")}`;
  const isTaskApi =
    upstreamPath === "/radar-agent/chat" ||
    upstreamPath.startsWith("/radar-agent/tasks");
  const isHabitProfileApi =
    upstreamPath === "/product/habit-profile" ||
    upstreamPath.startsWith("/product/habit-profile/");
  const apiKey = isTaskApi ? RADAR_AGENT_API_KEY : PRODUCT_RADAR_API_KEY;

  if (!apiKey) {
    return NextResponse.json(
      { detail: "雷达接口密钥尚未在服务端配置。" },
      { status: 503 },
    );
  }
  if (
    isTaskApi &&
    (!RADAR_AGENT_ACTOR_ID ||
      !RADAR_AGENT_ACTOR_ROLE ||
      !RADAR_AGENT_SUBJECT_ID ||
      !RADAR_AGENT_AUTHORIZATION_ID ||
      !RADAR_AGENT_ROLE_BINDING_IDS)
  ) {
    return NextResponse.json(
      { detail: "雷达任务身份尚未在服务端配置。" },
      { status: 503 },
    );
  }

  if (
    !isTaskApi &&
    !isHabitProfileApi &&
    !upstreamPath.startsWith("/product/radar/")
  ) {
    return NextResponse.json(
      { detail: "雷达代理仅允许访问已授权的雷达接口路径。" },
      { status: 404 },
    );
  }
  if (
    isHabitProfileApi &&
    (!HABIT_PROFILE_ACTOR_ID ||
      !HABIT_PROFILE_ACTOR_ROLE ||
      !HABIT_PROFILE_SUBJECT_ID)
  ) {
    return NextResponse.json(
      { detail: "睡眠习惯画像身份尚未在服务端配置。" },
      { status: 503 },
    );
  }

  const incomingUrl = new URL(request.url);
  const upstreamUrl = new URL(upstreamPath, BACKEND_API_BASE_URL);
  upstreamUrl.search = incomingUrl.search;
  const body = request.method === "GET" ? undefined : await request.text();
  const idempotencyKey = request.headers.get("idempotency-key");
  const lastEventId = request.headers.get("last-event-id");

  const upstreamResponse = await fetch(upstreamUrl, {
    method: request.method,
    headers: {
      "Content-Type": request.headers.get("content-type") ?? "application/json",
      "X-API-Key": apiKey,
      ...(isTaskApi
        ? {
            "X-Actor-Id": RADAR_AGENT_ACTOR_ID,
            "X-Actor-Role": RADAR_AGENT_ACTOR_ROLE,
            "X-Subject-Id": RADAR_AGENT_SUBJECT_ID,
            "X-Authorization-Id": RADAR_AGENT_AUTHORIZATION_ID,
            "X-Role-Binding-Ids": RADAR_AGENT_ROLE_BINDING_IDS,
          }
        : {}),
      ...(isHabitProfileApi
        ? {
            "X-Actor-Id": HABIT_PROFILE_ACTOR_ID!,
            "X-Actor-Role": HABIT_PROFILE_ACTOR_ROLE!,
            "X-Subject-Id": HABIT_PROFILE_SUBJECT_ID!,
            "X-Authorization-Scopes": HABIT_PROFILE_AUTHORIZATION_SCOPES,
          }
        : {}),
      ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
      ...(lastEventId ? { "Last-Event-ID": lastEventId } : {}),
    },
    body: body || undefined,
    cache: "no-store",
  });
  const contentType =
    upstreamResponse.headers.get("content-type") ?? "application/json";

  return new NextResponse(upstreamResponse.body, {
    status: upstreamResponse.status,
    headers: {
      "Content-Type": contentType,
      "Cache-Control":
        upstreamResponse.headers.get("cache-control") ?? "no-store",
      ...(contentType.includes("text/event-stream")
        ? { "X-Accel-Buffering": "no" }
        : {}),
    },
  });
}
