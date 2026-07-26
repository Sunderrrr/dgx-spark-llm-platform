import type { NextRequest } from "next/server";
import { proxySSE } from "@/lib/sseProxy";

// See app/playground/chat/route.ts for why this bypasses rewrites/proxy.ts.
export async function POST(request: NextRequest) {
  return proxySSE(request, "/support/chat");
}
