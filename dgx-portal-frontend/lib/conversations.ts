import type { Conversation } from "./types";
import { authFetch, getJSON, postFormVerifie } from "./api";

// The history now lives server-side (`conversations` table): it follows
// the user from one machine or browser to another. localStorage now
// only serves to recover once the history left by the old version.
const LEGACY_KEY = "pg_convs";

type ApiConversation = {
  id: string;
  title: string;
  model: string;
  ts: string;
  // The flags travel with the message: `hidden` (answers to questions,
  // resume requests), and `isError`/`truncated` without which a truncated
  // answer looked finished again after a reload.
  messages: {
    role: "user" | "assistant";
    content: string;
    hidden?: boolean;
    isError?: boolean;
    truncated?: boolean;
    images?: string[];
  }[];
  /** True when the server set the messages aside (list budget). */
  messages_omis?: boolean;
};

export async function fetchConversations(): Promise<Conversation[]> {
  try {
    const data = await getJSON<{ conversations: ApiConversation[] }>("/api/conversations");
    return data.conversations.map((c) => ({
      // The identifier comes from the server (`client_id`) and is NOT a number:
      // converting it gave NaN, hence an unrelated fabricated timestamp — and
      // every deletion targeted a nonexistent identifier, with no visible error.
      id: c.id,
      title: c.title,
      ts: Date.parse(c.ts) || Date.now(),
      model: c.model,
      messages: c.messages,
      messagesOmis: c.messages_omis === true,
    }));
  } catch {
    return [];
  }
}

/** A WHOLE conversation, on demand.
 *
 * `GET /api/conversations` bounds what it carries (30 full conversations
 * were ~60 MB, loaded at every playground opening): beyond that, it only
 * returns the metadata with `messages_omis`. This is the path that returns
 * the content when the conversation is actually opened.
 */
export async function fetchConversation(id: string): Promise<Conversation | null> {
  try {
    const data = await getJSON<{ ok: boolean; conversation: ApiConversation }>(
      `/api/conversations/${encodeURIComponent(id)}`);
    const c = data?.conversation;
    if (!c) return null;
    return {
      id: c.id,
      title: c.title,
      ts: Date.parse(c.ts) || Date.now(),
      model: c.model,
      messages: c.messages,
    };
  } catch {
    return null;
  }
}

/** Saves the conversation; returns `false` if the save failed.
 *
 * The failure must not interrupt the conversation — but it must not be
 * INVISIBLE either: `postForm` does not throw on a 413, and a refused save
 * (the case of every conversation exceeding Werkzeug's form field size
 * limit, fixed on the server side) made the history disappear on reload
 * without the slightest clue. The caller warns the user.
 */
export async function persistConversation(csrf: string, conv: Conversation): Promise<boolean> {
  try {
    // `postForm` returns neither status nor body: a 413 (form field too
    // large) or an `{ok:false}` therefore went totally unnoticed.
    const res = await authFetch("/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrf },
      body: new URLSearchParams({
        action: "save",
        id: String(conv.id),
        title: conv.title,
        model: conv.model || "",
        messages: JSON.stringify(conv.messages),
      }).toString(),
    });
    if (!res.ok) return false;
    const corps = (await res.json().catch(() => ({ ok: true }))) as { ok?: boolean };
    return corps?.ok !== false;
  } catch {
    return false;
  }
}

/** Deletes the conversation; returns `false` if the server refused.
 *
 * It was swallowed (`postForm` in an empty `try {} catch {}`): the
 * conversation vanished from the list, then REAPPEARED on reload
 * without a word of explanation. A deletion that did not happen must
 * be told. */
export async function removeConversation(csrf: string, id: string): Promise<boolean> {
  const res = await postFormVerifie("/conversations", csrf, { action: "delete", id: String(id) });
  return res.ok;
}

/** Migrates the old version's history (localStorage) to the server once,
 *  then clears the local key. */
export async function migrateLegacyConversations(csrf: string): Promise<boolean> {
  if (typeof window === "undefined") return false;
  let legacy: Conversation[] = [];
  try {
    legacy = JSON.parse(window.localStorage.getItem(LEGACY_KEY) || "[]");
  } catch {
    legacy = [];
  }
  if (!Array.isArray(legacy) || legacy.length === 0) return false;
  for (const conv of legacy) {
    if (conv && Array.isArray(conv.messages) && conv.messages.length) {
      await persistConversation(csrf, conv);
    }
  }
  window.localStorage.removeItem(LEGACY_KEY);
  return true;
}

/** `t` is passed as a parameter: module function, a hook cannot be called there. */
export function relativeTime(ts: number, t: (s: string) => string): string {
  const s = (Date.now() - ts) / 1000;
  if (s < 60) return t("à l'instant");
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return `${Math.floor(s / 86400)} ${t("j")}`;
}
