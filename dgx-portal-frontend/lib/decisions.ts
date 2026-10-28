/**
 * The « Décisions » workbench: the request format and the answer rendering.
 *
 * Two JSON editors (state, questions) like the System One playground, and a
 * result list. Kept out of the component so the rules are unit-tested — the
 * backend validates the same shape again, and a drift between the two would
 * only surface as a 400 far from the editor.
 */

export type QuestionType = "noul" | "choice" | "score";

export const MAX_QUESTIONS = 32;
export const MAX_STATE_BYTES = 64 * 1024;
export const MAX_CHOICES = 255;
export const MAX_LEVELS = 10;

export type Validated =
  | { ok: true; body: { state: unknown; questions: Record<string, unknown>; files?: string[] } }
  | { ok: false; error: string };

/** The state as the API wants it: a JSON value, or the text as it was typed. */
export function parseState(text: string): { ok: true; value: unknown } | { ok: false; error: string } {
  const trimmed = text.trim();
  if (!trimmed) return { ok: false, error: "Le champ « state » est vide." };
  try {
    return { ok: true, value: JSON.parse(trimmed) };
  } catch {
    // A plainly-textual state is the common case, and the quotes are what
    // people forget: the raw text is sent rather than refused.
    return { ok: true, value: text };
  }
}

/** `JSON.stringify` for the « Formater » button; null when it does not parse. */
export function prettyJson(text: string): string | null {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return null;
  }
}

function oneQuestion(id: string, raw: unknown): { ok: true; question: Record<string, unknown> } | { ok: false; error: string } {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return { ok: false, error: `La question « ${id} » doit être un objet.` };
  }
  const q = raw as Record<string, unknown>;
  const type = typeof q.type === "string" ? q.type : "";
  if (!["noul", "choice", "score"].includes(type)) {
    return { ok: false, error: `La question « ${id} » : type « ${type || "?"} » inconnu (noul, choice, score).` };
  }
  if (typeof q.instructions !== "string" || q.instructions.trim() === "") {
    return { ok: false, error: `La question « ${id} » n'a pas d'énoncé.` };
  }
  const question: Record<string, unknown> = { type, instructions: q.instructions };
  if (type === "noul") return { ok: true, question };
  if (type === "choice") {
    const criteria = q.criteria;
    if (typeof criteria !== "object" || criteria === null || Array.isArray(criteria)) {
      return { ok: false, error: `La question « ${id} » (choice) exige « criteria » : {"valeur": description|null}.` };
    }
    const n = Object.keys(criteria).length;
    if (n < 2 || n > MAX_CHOICES) return { ok: false, error: `La question « ${id} » doit offrir 2 à ${MAX_CHOICES} choix.` };
    question.criteria = criteria;
    return { ok: true, question };
  }
  const criteria = q.criteria;
  if (!Array.isArray(criteria) || criteria.length < 2 || criteria.length > MAX_LEVELS) {
    return { ok: false, error: `La question « ${id} » doit avoir 2 à ${MAX_LEVELS} niveaux.` };
  }
  if (!criteria.every((c) => typeof c === "string")) {
    return { ok: false, error: `La question « ${id} » : chaque niveau doit être un texte.` };
  }
  question.criteria = criteria;
  return { ok: true, question };
}

/**
 * The body `POST /api/decisions/run` takes, from the two editors' values.
 * Every refusal names the question concerned.
 */
export function validatePayload(stateText: string, questionsText: string, files: string[] = []): Validated {
  const state = parseState(stateText);
  if (!state.ok) return state;
  const size = new TextEncoder().encode(JSON.stringify(state.value)).length;
  if (size > MAX_STATE_BYTES) return { ok: false, error: `Le « state » dépasse ${MAX_STATE_BYTES} octets.` };
  let parsed: unknown;
  try {
    parsed = JSON.parse(questionsText.trim() || "{}");
  } catch {
    return { ok: false, error: "Le JSON des questions est invalide." };
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { ok: false, error: "Les questions doivent être un objet (id → question)." };
  }
  const entries = Object.entries(parsed as Record<string, unknown>);
  if (entries.length === 0) return { ok: false, error: "Ajoute au moins une question." };
  if (entries.length > MAX_QUESTIONS) return { ok: false, error: `${MAX_QUESTIONS} questions au maximum.` };
  const questions: Record<string, unknown> = {};
  for (const [id, raw] of entries) {
    if (id === "" || id.length > 64) return { ok: false, error: "Identifiant de question invalide (1 à 64 caractères)." };
    const built = oneQuestion(id, raw);
    if (!built.ok) return built;
    questions[id] = built.question;
  }
  const body: { state: unknown; questions: Record<string, unknown>; files?: string[] } = { state: state.value, questions };
  // Images ride alongside the state, as data URLs: llama.cpp reads them, the
  // portal relays them to the engine (LiteLLM refuses image parts on System One).
  if (files.length > 0) body.files = files;
  return { ok: true, body };
}

/** 0.7342 → « 73 % ». The API's probabilities are 0..1. */
export function formatProbability(value: number | undefined): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "—";
  return `${Math.round(value * 100)} %`;
}

export type DecisionAnswer = {
  type?: string;
  noul?: number;
  choice?: string | boolean | number;
  score?: number;
  legend?: Record<string, string>;
  confidence?: number;
  probabilities?: Record<string, number> | { value: string | boolean | number; probability: number }[];
};

/** The answer's headline: what the model decided, not how sure it is. */
export function answerHeadline(answer: DecisionAnswer): string {
  if (typeof answer.noul === "number") return formatProbability(answer.noul);
  if (answer.choice !== undefined) return String(answer.choice);
  if (typeof answer.score === "number") {
    const legend = answer.legend ?? {};
    const nearest = legend[String(Math.round(answer.score))];
    return nearest ? `${answer.score.toFixed(2)} — ${nearest}` : answer.score.toFixed(2);
  }
  return "—";
}

/** Every option with its probability, highest first, as `[label, value]`. */
export function answerOptions(answer: DecisionAnswer): [string, number][] {
  const probs = answer.probabilities;
  if (!probs) return [];
  const rows: [string, number][] = Array.isArray(probs)
    ? probs.map((p) => [String(p.value), p.probability])
    : Object.entries(probs).map(([k, v]) => [answer.legend?.[k] ?? k, v]);
  return rows.sort((a, b) => b[1] - a[1]);
}

/** The type, spelled as the result list shows it. */
export function answerTypeLabel(answer: DecisionAnswer): string {
  switch (answer.type) {
    case "noul":
      return "Noul";
    case "choice":
      return "Choice";
    case "score":
      return "Score";
    default:
      return answer.type ?? "—";
  }
}

/** « 2 levels · 0~1 » — the extra line the right column shows for a score. */
export function answerScale(answer: DecisionAnswer): string | null {
  if (answer.type !== "score") return null;
  const levels = answerOptions(answer).length;
  return levels > 0 ? `${levels} ${levels > 1 ? "niveaux" : "niveau"} · 0~${levels - 1}` : null;
}
