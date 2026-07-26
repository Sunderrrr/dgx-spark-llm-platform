import type { NextRequest } from "next/server";
import { proxySSE } from "@/lib/sseProxy";

// Deliberately NOT proxied via next.config.ts rewrites or proxy.ts's
// NextResponse.rewrite — both were confirmed (by timing the actual SSE chunk
// arrivals) to buffer the ENTIRE Flask response before forwarding it to the
// browser, even though Flask itself streams token-by-token correctly. A Route
// Handler that manually pipes `upstream.body` through a `Response` is the one
// pattern Next.js genuinely streams without buffering.
export async function POST(request: NextRequest) {
  return proxySSE(request, "/playground/chat");
}
