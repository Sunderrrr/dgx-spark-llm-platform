import type { PlaygroundData, Settings } from "./types";

function redirectToLogin(): never {
  if (typeof window !== "undefined") {
    // Deliberate full reload (not next/link): the session just expired
    // server-side, all in-memory client state is stale.
    window.location.href = "/login";
  }
  throw new Error("Non authentifié");
}

/**
 * Thrown for a 403: the user IS authenticated but lacks permission (e.g. a
 * non-admin hitting an admin-only endpoint). Distinct from a plain fetch
 * failure so callers can show "access denied" instead of an empty/broken page
 * — redirecting to /login here would be wrong (and confusing) since the user
 * is already logged in.
 */
export class ForbiddenError extends Error {
  constructor(url: string) {
    super(`Accès refusé (${url}).`);
    this.name = "ForbiddenError";
  }
}

export async function authFetch(input: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(input, { ...init, credentials: "include" });
  // Only a real 401 (not authenticated) means the session is gone — a 403
  // means the session is valid but the account doesn't have permission,
  // which redirecting to /login can't fix and would only hide.
  if (res.status === 401) redirectToLogin();
  return res;
}

export async function getJSON<T>(url: string): Promise<T> {
  const res = await authFetch(url);
  if (res.status === 403) throw new ForbiddenError(url);
  if (!res.ok) throw new Error(`Échec du chargement (${url}).`);
  return res.json();
}

/**
 * POST/DELETE JSON vers une route JSON du backend. Les routes de mémoire
 * échangent du JSON structuré (un fait a un sujet, une relation, un objet),
 * là où `postForm` sérialise à plat.
 */
export async function sendJSON<T = { ok: boolean; error?: string }>(
  url: string,
  csrf: string,
  body?: unknown,
  method: "POST" | "PATCH" | "DELETE" = "POST",
): Promise<T> {
  const res = await authFetch(url, {
    method,
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 403) throw new ForbiddenError(url);
  return res.json().catch(() => ({}) as T);
}

/**
 * Submits a form to an existing Flask route (classic POST,
 * form-encoded) by reusing its already-tested logic — no new
 * mutation endpoint on the backend. Flask responds with a redirect
 * automatically followed by fetch; we ignore that HTML body.
 */
export async function postForm(url: string, csrf: string, data: Record<string, string>): Promise<void> {
  await authFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrf },
    body: new URLSearchParams(data).toString(),
  });
}

/**
 * Like postForm, but for routes that return a real JSON body
 * {ok, error?} rather than a 204 — used when the action can fail for
 * a reason the user needs to know (e.g. unreachable MCP server),
 * unlike the "almost always successful" actions (creating a key) that
 * settle for an optimistic toast after postForm().
 */
export async function postFormJSON<T = { ok: boolean; error?: string }>(
  url: string,
  csrf: string,
  data: Record<string, string>,
): Promise<T> {
  const res = await authFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrf },
    body: new URLSearchParams(data).toString(),
  });
  return res.json();
}

/**
 * Like postFormJSON, but for routes that receive a file (image
 * upload) — multipart/form-data, no manual Content-Type header (the
 * browser must set the boundary itself).
 */
export async function postFormData<T = { error?: string }>(
  url: string,
  csrf: string,
  data: Record<string, string | File>,
): Promise<T> {
  const body = new FormData();
  for (const [k, v] of Object.entries(data)) body.append(k, v);
  const res = await authFetch(url, {
    method: "POST",
    headers: { "X-CSRFToken": csrf },
    body,
  });
  return res.json();
}

export async function fetchCsrfToken(): Promise<string> {
  const res = await authFetch("/api/csrf");
  if (!res.ok) throw new Error("Impossible de récupérer le jeton CSRF.");
  const data = await res.json();
  return data.token as string;
}

export async function fetchPlaygroundData(): Promise<PlaygroundData> {
  const res = await authFetch("/api/playground/data");
  if (!res.ok) throw new Error("Impossible de charger les modèles disponibles.");
  return res.json();
}

export type StreamDelta = {
  reasoningChunk?: string;
  contentChunk?: string;
  usage?: { total_tokens?: number; completion_tokens?: number };
  /** Le modèle a été coupé net par le plafond de tokens (finish_reason="length"). */
  truncated?: boolean;
};

export type ToolCallEvent = {
  id: string;
  name: string;
  status: "running" | "complete" | "error";
  target?: string;
  duration_ms?: number;
  error?: string;
};

type SSEPayload = {
  usage?: { total_tokens?: number; completion_tokens?: number };
  choices?: { delta?: { content?: string; reasoning_content?: string }; finish_reason?: string | null }[];
  tool_call?: ToolCallEvent;
};

/** Reads an SSE stream (packets `data: {...}\n\n`) and invokes onEvent per received packet. */
async function readSSE(res: Response, onEvent: (payload: SSEPayload) => void): Promise<void> {
  if (!res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      const line = part.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      const payload = line.slice(5).trim();
      if (payload === "[DONE]") continue;
      try {
        onEvent(JSON.parse(payload));
      } catch {
        // incomplete/invalid SSE packet — ignored, the stream continues
      }
    }
  }
}

/** Reads the SSE stream from /playground/chat and invokes onDelta for each received packet. */
export async function streamChat(
  csrf: string,
  model: string,
  messages: { role: string; content: string }[],
  settings: Settings,
  signal: AbortSignal,
  onDelta: (delta: StreamDelta) => void,
): Promise<void> {
  const res = await authFetch("/playground/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
    body: JSON.stringify({
      model,
      messages,
      system: settings.system,
      temperature: settings.temperature,
      max_tokens: settings.maxTokens,
      top_p: settings.topP,
      reasoning: settings.reasoning,
    }),
    signal,
  });
  await readSSE(res, (json) => {
    if (json.usage) onDelta({ usage: json.usage });
    // Le backend relaie les lignes SSE telles quelles : finish_reason arrive ici.
    // "length" = réponse tronquée par max_tokens, à signaler au lecteur.
    if (json.choices?.[0]?.finish_reason === "length") onDelta({ truncated: true });
    const delta = json.choices?.[0]?.delta;
    if (delta?.reasoning_content) onDelta({ reasoningChunk: delta.reasoning_content });
    if (delta?.content) onDelta({ contentChunk: delta.content });
  });
}

/** Reads the SSE stream from /api/ocr/extract (multipart upload, response streamed like the playground). */
export async function streamOcr(
  csrf: string,
  data: Record<string, string | File>,
  signal: AbortSignal,
  onChunk: (content: string) => void,
): Promise<void> {
  const form = new FormData();
  for (const [k, v] of Object.entries(data)) form.append(k, v);
  const res = await authFetch("/api/ocr/extract", {
    method: "POST",
    headers: { "X-CSRFToken": csrf },
    body: form,
    signal,
  });
  await readSSE(res, (json) => {
    const content = json.choices?.[0]?.delta?.content;
    if (content) onChunk(content);
  });
}

/** Reads the SSE stream from /support/chat: text and tool invocations (ChatToolCalls). */
export async function streamSupportChat(
  csrf: string,
  messages: { role: string; content: string }[],
  signal: AbortSignal,
  onChunk: (content: string) => void,
  onToolCall?: (event: ToolCallEvent) => void,
): Promise<void> {
  const res = await authFetch("/support/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
    body: JSON.stringify({ messages }),
    signal,
  });
  await readSSE(res, (json) => {
    const content = json.choices?.[0]?.delta?.content;
    if (content) onChunk(content);
    if (json.tool_call) onToolCall?.(json.tool_call);
  });
}
