import type { NextRequest } from "next/server";
import { proxySSEGet } from "@/lib/sseProxy";

// Same reasoning as app/playground/chat/route.ts: next.config.ts's `fallback`
// rewrite buffers SSE responses entirely before forwarding them, so the admin
// log tail (EventSource) would otherwise arrive in one lump at stream end
// instead of live. A Route Handler streams it correctly.
export async function GET(request: NextRequest) {
  return proxySSEGet(request, "/admin/runner/stream");
}
