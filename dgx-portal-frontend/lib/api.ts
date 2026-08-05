import type { PlaygroundData, Settings } from "./types";

function redirectToLogin(): never {
  if (typeof window !== "undefined") {
    // Rechargement complet volontaire (pas next/link) : la session vient
    // d'expirer côté serveur, tout l'état client en mémoire est périmé.
    window.location.href = "/login";
  }
  throw new Error("Non authentifié");
}

/**
 * Thrown for a 403: the user IS authenticated but lacks permission (e.g. a
 * non-admin hitting an admin-only endpoint). Distinct from a plain fetch
 * failure so callers can show "accès refusé" instead of an empty/broken page
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
 * Soumet un formulaire vers une route Flask existante (POST classique,
 * form-encodée) en réutilisant sa logique déjà testée — pas de nouvel
 * endpoint de mutation côté backend. Flask répond par une redirection
 * suivie automatiquement par fetch ; on ignore ce corps HTML.
 */
export async function postForm(url: string, csrf: string, data: Record<string, string>): Promise<void> {
  await authFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrf },
    body: new URLSearchParams(data).toString(),
  });
}

/**
 * Comme postForm, mais pour les routes qui renvoient un vrai corps JSON
 * {ok, error?} plutôt qu'un 204 — utilisé quand l'action peut échouer pour
 * une raison que l'utilisateur doit connaître (ex: serveur MCP injoignable),
 * contrairement aux actions "quasi toujours réussies" (créer une clé) qui se
 * contentent d'un toast optimiste après postForm().
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
 * Comme postFormJSON, mais pour les routes qui reçoivent un fichier (upload
 * image) — multipart/form-data, pas d'en-tête Content-Type manuel (le
 * navigateur doit fixer la boundary lui-même).
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
  choices?: { delta?: { content?: string; reasoning_content?: string } }[];
  tool_call?: ToolCallEvent;
};

/** Lit un flux SSE (paquets `data: {...}\n\n`) et invoque onEvent par paquet reçu. */
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
        // paquet SSE incomplet/invalide — ignoré, le flux continue
      }
    }
  }
}

/** Lit le flux SSE de /playground/chat et invoque onDelta pour chaque paquet reçu. */
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
    const delta = json.choices?.[0]?.delta;
    if (delta?.reasoning_content) onDelta({ reasoningChunk: delta.reasoning_content });
    if (delta?.content) onDelta({ contentChunk: delta.content });
  });
}

/** Lit le flux SSE de /api/ocr/extract (upload multipart, réponse streamée comme le playground). */
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

/** Lit le flux SSE de /support/chat : texte et invocations d'outils (ChatToolCalls). */
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
