import { EN } from "./dictionary";

/* Server sentences arrive PRE-FORMATTED with their runtime value
 * (« Trop de tentatives. Réessaie dans 2 min. »): the exact lookup can only
 * miss. The dictionary therefore keeps the TEMPLATE as msgid (« … dans
 * {wait} min. »), and we match the sentence against those templates to
 * re-inject the captured value into the translation with .replace() — the
 * same trick as the quota notice (lib/notices.ts). Exact keys win first;
 * anything unmatched is displayed as-is (a missing translation must never
 * mangle a message). */
type ServerModel = { re: RegExp; msgid: string };
let serverModels: ServerModel[] | null = null;

function serverModelsTable(): ServerModel[] {
  if (!serverModels) {
    // Longest templates first: a short one must not swallow a longer sibling.
    serverModels = Object.keys(EN)
      .filter((k) => /\{[^}]+\}/.test(k))
      .sort((a, b) => b.length - a.length)
      .map((msgid) => {
        const pattern = msgid
          .split(/(\{[^}]+\})/)
          .filter(Boolean)
          .map((m) => (/^\{[^}]+\}$/.test(m) ? "([\\s\\S]*?)" : m.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")))
          .join("");
        return { re: new RegExp(`^${pattern}$`), msgid };
      });
  }
  return serverModels;
}

/** Translates a SERVER-PROVIDED sentence at display time (cf. block comment).
 *  `t` is passed as a parameter (module function): like `noticeText`, a hook
 *  can only be called from a component. */
export function tServer(message: string, t: (fr: string) => string): string {
  const direct = t(message);
  if (direct !== message) return direct;          // exact key, done
  for (const { re, msgid } of serverModelsTable()) {
    const m = message.match(re);
    if (!m) continue;
    let i = 0;
    return t(msgid).replace(/\{[^}]+\}/g, () => m[++i] ?? "");
  }
  return direct;
}
