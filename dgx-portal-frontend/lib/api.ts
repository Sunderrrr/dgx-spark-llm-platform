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
    // Le serveur dit POURQUOI (Hugging Face injoignable, tâche inconnue…).
    // Jeter ce message obligeait l'interface à afficher « Échec du chargement »,
    // qui n'apprend rien et fait passer une panne amont pour un bug du portail.
    const corps = (await res.json().catch(() => null)) as { error?: string; code?: string } | null;
    const err = new Error(corps?.error || `Échec du chargement (${url}).`) as Error & { code?: string };
    // `code` voyage avec l'erreur : c'est lui qui permet à un écran de traduire
    // un refus ou une panne amont stable (le portail répond en français, cf.
    // CLAUDE.md § i18n) au lieu d'afficher une phrase française en mode anglais.
    if (corps?.code) err.code = corps.code;
    throw err;
  }
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
 * mutation endpoint on the backend.
 *
 * ATTENTION — le commentaire d'origine disait « we ignore that HTML body », et
 * c'était le piège : `postForm` ne disait RIEN du résultat, ni statut ni corps.
 * Trois bugs de production en sont venus (historique de conversation tronqué en
 * 413, « Clé créée ! » sur une création échouée, avatar refusé), donc le
 * `Response` est désormais RENDU. Pour une action dont le résultat compte,
 * lis le statut ou passe par `postFormVerifie` ; si tu n'as pas besoin de
 * savoir, l'utilisateur non plus — mais alors ne lui affiche pas « c'est fait ».
 */
export async function postForm(url: string, csrf: string, data: Record<string, string>): Promise<Response> {
  return authFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrf },
    body: new URLSearchParams(data).toString(),
  });
}

/**
 * Verdict d'une action form-encodée.
 *
 * `ok: false` couvre les trois façons d'échouer, y compris celle qui trompait le
 * plus : une réponse NON-JSON (redirection HTML, page d'erreur) est un échec, et
 * non un succès par défaut. Le `code` d'un refus stable est remonté tel quel,
 * pour que l'interface le traduise (cf. CLAUDE.md § i18n).
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

/** Une étape de recherche web ou de génération d'image, telle que le backend
 * l'annonce au fil de l'eau. */
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
  /** Adresses des images produites (génération réussie). */
  images?: string[];
};

export type StreamDelta = {
  reasoningChunk?: string;
  contentChunk?: string;
  usage?: { total_tokens?: number; completion_tokens?: number; prompt_tokens?: number };
  /** Le modèle a été coupé net par le plafond de tokens (finish_reason="length"). */
  truncated?: boolean;
  /** Étape de recherche web : ce que le modèle est en train de chercher ou lire. */
  webStep?: EtapeWeb;
  /** Notice système STRUCTURÉE (quota dépassé, erreur modèle…) : traduite au
     rendu dans la langue de l'interface — le serveur n'envoie jamais de phrase. */
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

/** Action sensible proposée par l'assistant Support (trame `cronos_confirm`
 * du flux /support/chat : révocation de clé, lancement/arrêt du modèle).
 * RIEN n'est exécuté tant que l'utilisateur n'a pas cliqué : le jeton opaque,
 * à usage unique et lié au compte, ne part qu'au POST /support/confirm — il
 * n'entre jamais dans le contexte du modèle, donc une injection indirecte ne
 * peut pas le rejouer (contrairement à une consigne de prompt). */
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
  // Un corps non-SSE (413 « trop gros », page HTML d'erreur du proxy, JSON de
  // refus) ne contient aucune ligne `data:` : la fonction rendait donc la main
  // SANS RIEN DIRE, et l'écran affichait un résultat vide. Cas mesuré : une
  // image de 17 Mo passe le proxy (20 Mo) mais dépasse le plafond de Flask
  // (16 Mo) → « aucun texte détecté » au lieu d'une erreur de taille.
  // Les trois appelants ont déjà un `catch` qui affiche le message.
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
      // '' (défaut du template) part en undefined : le backend ne transmet
      // reasoning_effort au modèle que s'il est renseigné.
      reasoning_effort: settings.reasoningEffort || undefined,
    }),
    signal,
  });
  await readSSE(res, (json) => {
    if (json.usage) onDelta({ usage: json.usage });
    // Le backend relaie les lignes SSE telles quelles : finish_reason arrive ici.
    // "length" = réponse tronquée par max_tokens, à signaler au lecteur.
    if (json.choices?.[0]?.finish_reason === "length") onDelta({ truncated: true });
    // Recherche web : événement à part, jamais mêlé au texte de la réponse.
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
 * notices système (cronos_notice) et demandes de confirmation d'action sensible
 * (cronos_confirm).
 *
 * `onNotice` reçoit les notices système du serveur (cronos_notice) : le Support
 * tourne sur la clé API de l'utilisateur, donc sur son budget, et peut donc
 * répondre « pas de clé » ou « quota dépassé » — exactement comme le playground.
 * Sans ce rappel, ces refus se traduisaient par une réponse vide.
 *
 * `onConfirm` reçoit les demandes de confirmation (cronos_confirm) : le modèle
 * a demandé une action SENSIBLE (révocation de clé, lancement/arrêt du modèle),
 * rien n'est exécuté — l'interface affiche Confirmer/Annuler et c'est le clic
 * (confirmSupportAction) qui tranche. */
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

/** Confirme (`cancel: false`) ou annule (`cancel: true`) une action sensible
 * proposée par l'assistant Support (POST /support/confirm, jeton à usage
 * unique). 200 → {ok, message} ; 404/409/400 → {error} (demande expirée, déjà
 * traitée ou invalide) : sendJSON ne lève pas sur ces statuts, l'appelant lit
 * ok/error/message. Seule exception : une erreur réseau, qui propage. */
export function confirmSupportAction(
  csrf: string,
  token: string,
  cancel: boolean,
): Promise<{ ok?: boolean; message?: string; error?: string }> {
  return sendJSON("/support/confirm", csrf, { token, cancel });
}

/** Pouce haut/bas sur une réponse du Support (commentaire facultatif pour le
 * 👎). question/réponse/model accompagnent le vote pour repérer les réponses
 * qui échouent ; l'échec du POST ne doit jamais casser le chat. */
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
