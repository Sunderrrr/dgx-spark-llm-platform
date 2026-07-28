import type { Conversation } from "./types";
import { getJSON, postForm } from "./api";

// L'historique vit maintenant côté serveur (table `conversations`) : il suit
// l'utilisateur d'une machine ou d'un navigateur à l'autre. Le localStorage ne
// sert plus qu'à récupérer une fois l'historique laissé par l'ancienne version.
const LEGACY_KEY = "pg_convs";

type ApiConversation = {
  id: string;
  title: string;
  model: string;
  ts: string;
  messages: { role: "user" | "assistant"; content: string }[];
};

export async function fetchConversations(): Promise<Conversation[]> {
  try {
    const data = await getJSON<{ conversations: ApiConversation[] }>("/api/conversations");
    return data.conversations.map((c) => ({
      id: Number(c.id) || Date.parse(c.ts) || Date.now(),
      title: c.title,
      ts: Date.parse(c.ts) || Date.now(),
      model: c.model,
      messages: c.messages,
    }));
  } catch {
    return [];
  }
}

export async function persistConversation(csrf: string, conv: Conversation): Promise<void> {
  try {
    await postForm("/conversations", csrf, {
      action: "save",
      id: String(conv.id),
      title: conv.title,
      model: conv.model || "",
      messages: JSON.stringify(conv.messages),
    });
  } catch {
    // L'échec d'enregistrement ne doit jamais interrompre la conversation.
  }
}

export async function removeConversation(csrf: string, id: number): Promise<void> {
  try {
    await postForm("/conversations", csrf, { action: "delete", id: String(id) });
  } catch {
    // idem
  }
}

/** Remonte une seule fois l'historique de l'ancienne version (localStorage)
 *  vers le serveur, puis efface la clé locale. */
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

export function relativeTime(ts: number): string {
  const s = (Date.now() - ts) / 1000;
  if (s < 60) return "à l'instant";
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return `${Math.floor(s / 86400)} j`;
}
