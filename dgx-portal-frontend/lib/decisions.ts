/**
 * The « Décisions » page's request building, kept out of the component so the
 * format is unit-tested (the backend validates the same rules again — this is
 * the screen's own guard, not the authority).
 *
 * The System One API takes a `state` (text or JSON) and a map of typed
 * questions: `noul` (yes/no), `choice` (one of a list) and `score` (an ordered
 * scale). See docs/api.md.
 */

export type QuestionType = "noul" | "choice" | "score";

export type QuestionDraft = {
  /** The key the answer comes back under. */
  id: string;
  type: QuestionType;
  instructions: string;
  /** `choice` → the options, `score` → the levels (lowest first). One per
   * line or comma-separated; ignored for `noul`. */
  options: string;
};

export const MAX_QUESTIONS = 32;
export const MAX_STATE_BYTES = 64 * 1024;
export const MAX_CHOICES = 255;
export const MAX_LEVELS = 10;

export type BuiltRequest =
  | { ok: true; body: { state: unknown; questions: Record<string, unknown> } }
  | { ok: false; error: string };

/** The state as the API wants it: a JSON value, or the text as it was typed. */
export function parseState(text: string, isJson: boolean): { ok: true; value: unknown } | { ok: false; error: string } {
  const trimmed = text.trim();
  if (!trimmed) return { ok: false, error: "Le champ « state » est vide." };
  if (!isJson) return { ok: true, value: text };
  try {
    return { ok: true, value: JSON.parse(trimmed) };
  } catch {
    return { ok: false, error: "Le JSON du « state » est invalide." };
  }
}

/** One option per line, or comma-separated; blanks dropped, order kept. */
export function splitOptions(raw: string): string[] {
  return raw
    .split(/[\n,]/)
    .map((o) => o.trim())
    .filter((o) => o.length > 0);
}

/**
 * The body `POST /api/decisions/run` takes. Every refusal names the question
 * concerned: a generic « requête invalide » teaches nothing.
 */
export function buildDecisionRequest(stateText: string, isJson: boolean, drafts: QuestionDraft[]): BuiltRequest {
  const state = parseState(stateText, isJson);
  if (!state.ok) return state;
  if (new TextEncoder().encode(JSON.stringify(state.value)).length > MAX_STATE_BYTES) {
    return { ok: false, error: `Le « state » dépasse ${MAX_STATE_BYTES} octets.` };
  }
  const filled = drafts.filter((d) => d.id.trim() !== "" || d.instructions.trim() !== "" || d.options.trim() !== "");
  if (filled.length === 0) return { ok: false, error: "Ajoute au moins une question." };
  if (filled.length > MAX_QUESTIONS) return { ok: false, error: `${MAX_QUESTIONS} questions au maximum.` };
  const questions: Record<string, unknown> = {};
  for (const draft of filled) {
    const id = draft.id.trim();
    if (!id) return { ok: false, error: "Chaque question a besoin d'un identifiant." };
    if (id.length > 64) return { ok: false, error: `Identifiant trop long : « ${id} » (64 caractères au maximum).` };
    if (id in questions) return { ok: false, error: `Identifiant « ${id} » en double.` };
    const instructions = draft.instructions.trim();
    if (!instructions) return { ok: false, error: `La question « ${id} » n'a pas d'énoncé.` };
    if (draft.type === "noul") {
      questions[id] = { type: "noul", instructions };
      continue;
    }
    const options = splitOptions(draft.options);
    if (draft.type === "choice") {
      if (options.length < 2 || options.length > MAX_CHOICES) {
        return { ok: false, error: `La question « ${id} » doit offrir 2 à ${MAX_CHOICES} choix.` };
      }
      questions[id] = { type: "choice", instructions, criteria: Object.fromEntries(options.map((o) => [o, null])) };
      continue;
    }
    if (options.length < 2 || options.length > MAX_LEVELS) {
      return { ok: false, error: `La question « ${id} » doit avoir 2 à ${MAX_LEVELS} niveaux.` };
    }
    questions[id] = { type: "score", instructions, criteria: options };
  }
  return { ok: true, body: { state: state.value, questions } };
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
