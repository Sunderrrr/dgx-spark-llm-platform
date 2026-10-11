// Server-side reads of the Flask API, for server rendering.
//
// The pages used to render empty, boot their JavaScript, THEN ask Flask for
// their data: one more browser round-trip (through Cloudflare) after the page
// arrived, and a visible « fill in » on every load. The server components now
// ask Flask directly on the compose network and hand the answer to the client
// component as its initial state (see lib/seed.ts).
//
// Server-only: it reads the incoming request's headers (next/headers refuses
// to load in a client component).
import { headers } from "next/headers";
import { redirect } from "next/navigation";
import type { Seed } from "./seed";

const BACKEND_URL = process.env.BACKEND_URL || "http://dgx-portal:5000";

// The page waits for these reads before anything is sent: past this budget the
// page renders without them and the client fetches them as before. Flask
// answers the usual reads in 5-50 ms on the compose network.
const TIMEOUT_MS = 1500;

// What Flask needs to see the real visitor rather than this container: the
// session, Cloudflare's client IP (auth._client_ip, rate limits, the session
// registry) and the browser identity.
const FORWARDED = ["cookie", "cf-connecting-ip", "x-forwarded-for", "user-agent", "accept-language"];

export type ServerRead<T> =
  | { status: "ok"; data: T }
  | { status: "unauthenticated" }
  | { status: "failed" };

/** GET a Flask JSON endpoint as the visitor. Never throws. */
export async function serverRead<T>(path: string): Promise<ServerRead<T>> {
  try {
    const incoming = await headers();
    const h: Record<string, string> = { accept: "application/json" };
    for (const name of FORWARDED) {
      const v = incoming.get(name);
      if (v) h[name] = v;
    }
    const r = await fetch(new URL(path, BACKEND_URL), {
      headers: h,
      redirect: "manual",
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (r.status === 401) return { status: "unauthenticated" };
    if (!r.ok) return { status: "failed" };
    return { status: "ok", data: (await r.json()) as T };
  } catch {
    return { status: "failed" };
  }
}

/** Several reads for one page, in parallel, as one Seed: each key holds its
 *  endpoint's answer, or null when that read failed (the client page then
 *  fetches that key itself). A 401 on any of them sends to /login. */
export async function pageSeed<T extends Record<string, unknown>>(
  paths: { [K in keyof T]: string },
): Promise<Seed<{ [K in keyof T]: T[K] | null }>> {
  const keys = Object.keys(paths) as (keyof T)[];
  const reads = await Promise.all(keys.map((k) => serverRead<T[typeof k]>(paths[k])));
  if (reads.some((r) => r.status === "unauthenticated")) redirect("/login");
  const data = {} as { [K in keyof T]: T[K] | null };
  keys.forEach((k, i) => {
    const r = reads[i];
    data[k] = r.status === "ok" ? (r.data as T[typeof k]) : null;
  });
  return { id: crypto.randomUUID(), data };
}
