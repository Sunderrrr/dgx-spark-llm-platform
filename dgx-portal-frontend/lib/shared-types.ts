/* Data contracts shared by several pages.
 *
 * Each of these types used to be declared in place, once per page, and the
 * copies drifted: a field added to one copy only, a `kind` union widened on
 * one page and not the next. The canonical definition now lives here and the
 * pages import it, so the copies cannot diverge any more.
 */

/** One clarifying question of a model's ```ask block, plus its proposed
 *  answers. Parsed by `parseAsk` (lib/playground-parsers.ts, which re-exports
 *  this type) and rendered by the playground's question card. */
export type AskQ = { question: string; options: string[] };

/** Lifecycle of an on-demand generation job (image, video) as the sidecar
 *  reports it. `idle` is client-only: no job has been started yet. */
export type JobStatus = "idle" | "pending" | "running" | "done" | "error" | "cancelled";

/** Fields every entry of `/api/running-models` carries, whatever its `kind`. */
export type RunningModelBase = { name: string; exposed: boolean };

/** Every `kind` the endpoint can report: the chat model, the on-demand media
 *  engines, and the decision models (portal-rs `sidecars::running_and_metrics`). */
export type RunningModelKind = "chat" | "image" | "video" | "ocr" | "voice" | "decisions";

/** A running model, with `kind` covering the whole endpoint vocabulary.
 *  A page that only reacts to SOME kinds narrows it locally
 *  (`RunningModelBase & { kind: "video" }`) so that comparing with a kind
 *  that page cannot meet stays a type error. */
export type RunningModel = RunningModelBase & { kind: RunningModelKind };
