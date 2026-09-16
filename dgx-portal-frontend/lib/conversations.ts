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
  // Les drapeaux voyagent avec le message : `hidden` (réponses aux questions,
  // demandes de reprise), et `isError`/`truncated` sans lesquels une réponse
  // coupée repassait pour finie après un rechargement.
  messages: {
    role: "user" | "assistant";
    content: string;
    hidden?: boolean;
    isError?: boolean;
    truncated?: boolean;
  }[];
  /** Vrai quand le serveur a laissé les messages de côté (budget de la liste). */
  messages_omis?: boolean;
};

export async function fetchConversations(): Promise<Conversation[]> {
  try {
    const data = await getJSON<{ conversations: ApiConversation[] }>("/api/conversations");
    return data.conversations.map((c) => ({
      // L'identifiant vient du serveur (`client_id`) et n'est PAS un nombre :
      // le convertir donnait NaN, donc un horodatage fabriqué sans rapport — et
      // toute suppression visait un identifiant inexistant, sans erreur visible.
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

/** Une conversation ENTIÈRE, à la demande.
 *
 * `GET /api/conversations` borne ce qu'il transporte (30 conversations pleines
 * faisaient ~60 Mo, chargés à chaque ouverture du playground) : au-delà, il ne
 * renvoie que les métadonnées avec `messages_omis`. C'est ce chemin qui rend le
 * contenu quand on ouvre vraiment la conversation.
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

/** Enregistre la conversation ; rend `false` si l'enregistrement a échoué.
 *
 * L'échec ne doit pas interrompre la conversation — mais il ne doit pas non plus
 * être INVISIBLE : `postForm` ne lève pas sur un 413, et une sauvegarde refusée
 * (le cas de toute conversation dépassant la limite de champ de formulaire de
 * Werkzeug, corrigé côté serveur) faisait disparaître l'historique au
 * rechargement sans le moindre indice. L'appelant prévient l'utilisateur.
 */
export async function persistConversation(csrf: string, conv: Conversation): Promise<boolean> {
  try {
    // `postForm` ne rend ni le statut ni le corps : un 413 (champ de formulaire
    // trop gros) ou un `{ok:false}` passaient donc totalement inaperçus.
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

/** Supprime la conversation ; rend `false` si le serveur a refusé.
 *
 * Elle était avalée (`postForm` dans un `try {} catch {}` vide) : la
 * conversation disparaissait de la liste, puis RÉAPPARAISSAIT au rechargement
 * sans un mot d'explication. Une suppression qui n'a pas eu lieu doit se dire.
 */
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

/** `t` est passé en paramètre : fonction de module, un hook ne s'y appelle pas. */
export function relativeTime(ts: number, t: (s: string) => string): string {
  const s = (Date.now() - ts) / 1000;
  if (s < 60) return t("à l'instant");
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return `${Math.floor(s / 86400)} ${t("j")}`;
}
