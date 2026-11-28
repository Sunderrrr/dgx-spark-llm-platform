// Browser-stored playground preferences: the panel settings (+ where their
// system prompt came from), the pinned conversations, the reusable snippets
// and the renamed generated files. Extracted VERBATIM from PlaygroundPage.tsx
// (module-scope helpers, no behaviour change) — the page keeps the React state
// and the effects that read/write them.
//
// Why the browser and not the account: see the notes kept on each block below.
// A stored value written by an OLDER version is ignored field by field, so an
// old entry never breaks the panel.

import type { Settings } from "@/lib/types";
import type { Artifact, Snippet } from "@/lib/playground-thread";

export const DEFAULT_SETTINGS: Settings = {
  system: "",
  temperature: 0.7,
  // The maximum. Only tokens ACTUALLY produced are billed, so a
  // high ceiling costs nothing on a short answer — and 4096 cleanly cut off any
  // slightly long answer (full HTML page, large configuration file).
  // The backend lowers this value to what remains in the context window
  // once the prompt is counted: a long conversation therefore does not fail,
  // it simply gets a shorter answer.
  maxTokens: 131072,
  topP: 1,
  reasoning: "auto" as const,
  // Reasoning effort: '' = leave the model's template at its default
  // value (recognized values depend on the model — e.g. Qwen3.8: xhigh).
  reasoningEffort: "",
};

// Pinned (starred) conversations: a personal preference stored in the browser —
// no backend migration, shared on this machine.
const PINNED_KEY = "dgx.pinned.conversations";
export function loadPinnedIds(): string[] {
  try {
    return (JSON.parse(localStorage.getItem(PINNED_KEY) || "[]") as string[]);
  } catch {
    return [];
  }
}
export function savePinnedIds(ids: string[]) {
  try {
    localStorage.setItem(PINNED_KEY, JSON.stringify(ids));
  } catch {
    /* storage unavailable (private mode): we ignore it */
  }
}

const SNIPPET_KEY = "dgx.snippets";
export function loadSnippets(): Snippet[] {
  try {
    return (JSON.parse(localStorage.getItem(SNIPPET_KEY) || "[]") as Snippet[]);
  } catch {
    return [];
  }
}
export function saveSnippets(list: Snippet[]) {
  try {
    localStorage.setItem(SNIPPET_KEY, JSON.stringify(list));
  } catch {
    /* storage unavailable (private mode): we ignore it */
  }
}

// Renaming generated files (artifacts). Key = `convId::kind::titre`
// (file names are made unique by the model) and persisted in
// localStorage to survive a reload; `convId` prevents the same name
// (`file-1.yml`) in two conversations from renaming each other.
const ARTIFACT_RENAME_KEY = "dgx.artifact.renames";
export function loadArtifactRenames(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(ARTIFACT_RENAME_KEY) || "{}") as Record<string, string>;
  } catch {
    return {};
  }
}
export function saveArtifactRenames(m: Record<string, string>) {
  try {
    localStorage.setItem(ARTIFACT_RENAME_KEY, JSON.stringify(m));
  } catch {
    /* storage unavailable (private mode): we ignore it */
  }
}
export function artifactRenameKey(convId: string | null, a: Artifact): string {
  return `${convId ?? "anon"}::${a.kind}::${a.title}`;
}

// Playground settings (persona, generation, reasoning): a personal preference
// kept in the browser, like the pinned conversations and the snippets.
// « je dois le sélectionner à chaque fois » : NOTHING was saved, so every page
// load — and every round trip through another page — reset the panel to
// DEFAULT_SETTINGS, the reasoning choice (Auto / Toujours / Jamais) first. Now
// loaded ONCE at mount, saved on every change. Per browser, like every other
// playground preference: a server-side copy would follow the account, this one
// needs no migration and restores without a single loading frame.
const SETTINGS_KEY = "dgx.playground.settings";
export type Provenance = "persona" | "skill" | "manual";
export type StoredSettings = { settings: Settings; provenance: Provenance };
export function loadSettings(): StoredSettings {
  const base: StoredSettings = { settings: DEFAULT_SETTINGS, provenance: "manual" };
  try {
    const raw = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "null") as
      (Partial<Settings> & { provenance?: unknown }) | null;
    if (!raw || typeof raw !== "object") return base;
    // Field by field over the defaults: a value stored by an OLDER version (or
    // hand-edited) must not break the panel — unknown or wrong-typed, it is
    // ignored and the default stands.
    return {
      settings: {
        ...DEFAULT_SETTINGS,
        system: typeof raw.system === "string" ? raw.system : DEFAULT_SETTINGS.system,
        temperature: typeof raw.temperature === "number" ? raw.temperature : DEFAULT_SETTINGS.temperature,
        maxTokens: typeof raw.maxTokens === "number" ? raw.maxTokens : DEFAULT_SETTINGS.maxTokens,
        topP: typeof raw.topP === "number" ? raw.topP : DEFAULT_SETTINGS.topP,
        reasoning:
          raw.reasoning === true || raw.reasoning === false || raw.reasoning === "auto"
            ? raw.reasoning
            : DEFAULT_SETTINGS.reasoning,
        reasoningEffort:
          typeof raw.reasoningEffort === "string" ? raw.reasoningEffort : DEFAULT_SETTINGS.reasoningEffort,
      },
      provenance: raw.provenance === "persona" ? "persona" : "manual",
    };
  } catch {
    return base; // unreadable storage: back to the default settings
  }
}
export function saveSettings(settings: Settings, provenance: Provenance) {
  try {
    // The system prompt a SKILL injects belongs to that skill, not to the
    // user's own defaults: selecting a skill must not become the persona of
    // every conversation to come. We keep the stored one instead.
    const legacy = loadSettings();
    const persona =
      provenance === "skill"
        ? { system: legacy.settings.system, provenance: legacy.provenance }
        : { system: settings.system, provenance };
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({ ...settings, ...persona }));
  } catch {
    /* storage unavailable (private mode): we ignore it */
  }
}
