import type { PlaygroundData, Settings } from "./types";
import type { CronosNotice } from "./notices";

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
  if (!res.ok) {
    // The server says WHY (Hugging Face unreachable, unknown task…).
    // Throwing this message away forced the UI to display « Échec du chargement »,
    // which teaches nothing and makes an upstream outage look like a portal bug.
    const corps = (await res.json().catch(() => null)) as { error?: string; code?: string } | null;
    const err = new Error(corps?.error || `Échec du chargement (${url}).`) as Error & { code?: string };
    // `code` travels with the error: it is what lets a screen translate
    // a refusal or a stable upstream outage (the portal answers in French, cf.
    // CLAUDE.md § i18n) instead of displaying a French sentence in English mode.
    if (corps?.code) err.code = corps.code;
    throw err;
  }
  return res.json();
}

/**
 * POST/DELETE JSON to a JSON route of the backend. The memory routes
 * exchange structured JSON (a fact has a subject, a relation, an object),
 * where `postForm` serializes flat.
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
 * mutation endpoint on the backend.
 *
 * NOTE — the original comment said « we ignore that HTML body », and that was
 * the trap: `postForm` said NOTHING about the result, neither status nor body.
 * Three production bugs came from it (conversation history truncated at
 * 413, « Clé créée ! » on a failed creation, avatar refused), so the
 * `Response` is now RETURNED. For an action whose result matters,
 * read the status or go through `postFormVerifie`; if you do not need to
 * know, the user does not either — but then do not display « c'est fait ».
 */
export async function postForm(url: string, csrf: string, data: Record<string, string>): Promise<Response> {
  return authFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrf },
    body: new URLSearchParams(data).toString(),
  });
}

/**
 * Verdict of a form-encoded action.
 *
 * `ok: false` covers the three ways of failing, including the one that misled
 * the most: a NON-JSON response (HTML redirect, error page) is a failure, not
 * a success by default. The `code` of a stable refusal is surfaced as-is,
 * so that the UI can translate it (cf. CLAUDE.md § i18n).
 */
export async function postFormVerifie(
  url: string,
  csrf: string,
  data: Record<string, string>,
): Promise<{ ok: boolean; error?: string; code?: string }> {
  try {
    const res = await postForm(url, csrf, data);
    const corps = (await res.json().catch(() => null)) as
      { ok?: boolean; error?: string; code?: string } | null;
    if (!res.ok) return { ok: false, error: corps?.error, code: corps?.code };
    if (corps && corps.ok === false) return { ok: false, error: corps.error, code: corps.code };
    return { ok: true };
  } catch {
    return { ok: false, error: "Le serveur n'a pas répondu — réessaie." };
  }
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

/** A web search or image generation step, as the backend
 * announces it on the fly. */
export type EtapeWeb = {
  etape: "recherche" | "recherche_finie" | "lecture" | "lecture_finie"
    | "generation" | "generation_finie" | "inconnue";
  outil: string;
  question?: string;
  urls?: string[];
  nombre?: number;
  lues?: number;
  erreur?: string | null;
  sources?: { titre: string; url: string }[];
  echecs?: { url: string; raison: string }[];
  /** Addresses of the produced images (successful generation). */
  images?: string[];
};

export type StreamDelta = {
  reasoningChunk?: string;
  contentChunk?: string;
  usage?: { total_tokens?: number; completion_tokens?: number; prompt_tokens?: number };
  /** The model was cut short by the token ceiling (finish_reason="length"). */
  truncated?: boolean;
  /** Web search step: what the model is currently searching or reading. */
  webStep?: EtapeWeb;
  /** STRUCTURED system notice (quota exceeded, model error…): translated at
     render time into the UI language — the server never sends a sentence. */
  notice?: { id: string; reset?: string; status?: number };
};

export type ToolCallEvent = {
  id: string;
  name: string;
  status: "running" | "complete" | "error";
  target?: string;
  duration_ms?: number;
  error?: string;
};

/** Sensitive action proposed by the Support assistant (`cronos_confirm`
 * frame of the /support/chat stream: key revocation, model launch/stop).
 * NOTHING is executed until the user has clicked: the opaque, single-use,
 * account-bound token only goes out with the POST /support/confirm — it
 * never enters the model context, so an indirect injection cannot replay it
 * (unlike a prompt instruction). */
export type SupportConfirmRequest = {
  token: string;
  tool: string;
  label: string;
  target?: string | null;
};

type SSEPayload = {
  usage?: { total_tokens?: number; completion_tokens?: number; prompt_tokens?: number };
  choices?: { delta?: { content?: string; reasoning_content?: string }; finish_reason?: string | null }[];
  cronos_web?: EtapeWeb;
  cronos_notice?: { id: string; reset?: string; status?: number };
  cronos_confirm?: SupportConfirmRequest;
  tool_call?: ToolCallEvent;
};

/** Reads an SSE stream (packets `data: {...}\n\n`) and invokes onEvent per received packet. */
async function readSSE(res: Response, onEvent: (payload: SSEPayload) => void): Promise<void> {
  // A non-SSE body (413 « trop gros », proxy HTML error page, refusal JSON)
  // contains no `data:` line: the function therefore returned
  // WITHOUT SAYING ANYTHING, and the screen showed an empty result. Measured
  // case: a 17 MB image passes the proxy (20 MB) but exceeds Flask's ceiling
  // (16 MB) → « aucun texte détecté » instead of a size error.
  // The three callers already have a `catch` that displays the message.
  if (!res.ok) throw new Error(`Erreur ${res.status}`);
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
  messages: { role: string; content: string; images?: string[] }[],
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
      reasoning: settings.reasoning === true ? true : settings.reasoning === false ? false : "auto",
      // '' (template default) goes out as undefined: the backend only passes
      // reasoning_effort to the model when it is set.
      reasoning_effort: settings.reasoningEffort || undefined,
    }),
    signal,
  });
  await readSSE(res, (json) => {
    if (json.usage) onDelta({ usage: json.usage });
    // The backend relays the SSE lines as-is: finish_reason arrives here.
    // "length" = response truncated by max_tokens, to be flagged to the reader.
    if (json.choices?.[0]?.finish_reason === "length") onDelta({ truncated: true });
    // Web search: separate event, never mixed into the response text.
    if (json.cronos_web) onDelta({ webStep: json.cronos_web });
    if (json.cronos_notice) onDelta({ notice: json.cronos_notice });
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

/** Reads the SSE stream from /support/chat: text, tool invocations (ChatToolCalls),
 * system notices (cronos_notice) and sensitive-action confirmation requests
 * (cronos_confirm).
 *
 * `onNotice` receives the server's system notices (cronos_notice): Support
 * runs on the user's API key, hence on their budget, and can therefore
 * answer « pas de clé » or « quota dépassé » — exactly like the playground.
 * Without this reminder, these refusals showed up as an empty answer.
 *
 * `onConfirm` receives the confirmation requests (cronos_confirm): the model
 * asked for a SENSITIVE action (key revocation, model launch/stop),
 * nothing is executed — the UI displays Confirmer/Annuler and the click
 * (confirmSupportAction) decides. */
export async function streamSupportChat(
  csrf: string,
  messages: { role: string; content: string }[],
  signal: AbortSignal,
  onChunk: (content: string) => void,
  onToolCall?: (event: ToolCallEvent) => void,
  onNotice?: (notice: CronosNotice) => void,
  onConfirm?: (request: SupportConfirmRequest) => void,
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
    if (json.cronos_notice) onNotice?.(json.cronos_notice);
    if (json.cronos_confirm) onConfirm?.(json.cronos_confirm);
  });
}

/** Confirms (`cancel: false`) or cancels (`cancel: true`) a sensitive action
 * proposed by the Support assistant (POST /support/confirm, single-use
 * token). 200 → {ok, message} ; 404/409/400 → {error} (request expired,
 * already handled or invalid): sendJSON does not throw on these statuses, the
 * caller reads ok/error/message. Only exception: a network error, which propagates. */
export function confirmSupportAction(
  csrf: string,
  token: string,
  cancel: boolean,
): Promise<{ ok?: boolean; message?: string; error?: string }> {
  return sendJSON("/support/confirm", csrf, { token, cancel });
}

/** Thumbs up/down on a Support answer (optional comment for the
 * 👎). question/answer/model accompany the vote to spot the answers
 * that fail; the POST failure must never break the chat. */
export function sendSupportFeedback(
  csrf: string,
  payload: {
    vote: 1 | -1;
    comment?: string;
    question?: string;
    answer?: string;
    model?: string;
  },
): Promise<{ ok?: boolean; error?: string }> {
  return sendJSON("/support/feedback", csrf, payload);
}
