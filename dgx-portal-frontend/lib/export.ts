/* Export of a conversation as a file — shared by the playground and the Support.
 *
 * Extracted from `app/(app)/playground/page.tsx` (a MOVE, not a rewrite) so the
 * Support can export its thread too: an operator who reports a problem keeps
 * the whole exchange, thinking included. Everything here is pure except
 * `downloadText` (the browser's own download). */

// A conversation to export: same fields as the API (same shape as ApiConversation
// on the lib/conversations side). `reasoning` is what the model thought before
// answering — exported too: the Support diagnoses, and the reasoning IS part of
// the diagnosis.
export type ExportConversation = {
  title: string;
  model: string;
  messages: {
    role: "user" | "assistant";
    content: string;
    hidden?: boolean;
    reasoning?: string;
    reasoningMs?: number;
  }[];
};

/** First user line, capped: the fallback title of a conversation. */
export function convTitleFallback(
  msgs: ExportConversation["messages"],
  fallback: string,
): string {
  const first = msgs.find((m) => m.role === "user")?.content ?? "";
  return first.slice(0, 80).trim() || fallback;
}

/** Conversation → readable Markdown, exported as .md.
 *  `t` is passed as a parameter: this is a module function, so no hook is
 *  called here. The reasoning, when there is one, goes into a folded block: it
 *  is part of the exchange, but it must not bury the answer. */
export function convAsMarkdown(conv: ExportConversation, t: (s: string) => string): string {
  const lines = [`# ${conv.title}`, "", `_${t("Modèle :")} ${conv.model || "—"}_`, "", "---", ""];
  for (const m of conv.messages) {
    if (m.hidden) continue;
    lines.push(m.role === "user" ? `**${t("Vous :")}**` : `**${t("Assistant :")}**`);
    lines.push("");
    if (m.reasoning) {
      const duree = m.reasoningMs ? ` (${Math.round(m.reasoningMs / 1000)} s)` : "";
      lines.push(`<details><summary>${t("Réflexion")}${duree}</summary>`, "", m.reasoning, "", "</details>", "");
    }
    lines.push(m.content, "");
  }
  return lines.join("\n");
}

/** Conversation → full JSON (we keep everything, including hidden), as .json. */
export function convAsJson(conv: ExportConversation): string {
  return JSON.stringify(
    { title: conv.title, model: conv.model, exported_at: new Date().toISOString(), messages: conv.messages },
    null,
    2,
  );
}

/** Client-side download of some text as a file. */
export function downloadText(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
