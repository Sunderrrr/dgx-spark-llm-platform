import type { NextRequest } from "next/server";
import { proxySSE } from "@/lib/sseProxy";

// Same reasoning as app/playground/chat/route.ts: next.config.ts's `fallback`
// rewrite buffers the ENTIRE Flask response before forwarding it, so without
// this Route Handler the OCR text only ever appeared all at once instead of
// streaming live. proxySSE() reads the incoming body as an ArrayBuffer (not
// text) so the multipart/form-data image upload passes through intact.
export async function POST(request: NextRequest) {
  return proxySSE(request, "/api/ocr/extract");
}
