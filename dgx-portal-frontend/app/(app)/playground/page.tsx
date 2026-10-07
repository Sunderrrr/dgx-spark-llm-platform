"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "@astryxdesign/core/Icon";
import { Layout, LayoutHeader, LayoutContent } from "@astryxdesign/core/Layout";
import { useToast } from "@astryxdesign/core/Toast";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { useResizable } from "@astryxdesign/core/Resizable";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import {
  ChatLayout,
  ChatMessageList,
} from "@astryxdesign/core/Chat";
import {
  SparklesIcon,
} from "@heroicons/react/24/outline";
import { useT, tServeur } from "@/lib/i18n";
import { useWhoami } from "@/lib/whoami";
import { useCsrf } from "@/lib/useCsrf";
import { useSettingsDialog } from "@/lib/settings-dialog";
import { useDictation } from "@/lib/useDictation";
import { useIsNarrow } from "@/lib/useIsNarrow";
import { useStickToBottom } from "@/lib/useStickToBottom";
import { useThemeMode } from "../../theme-provider";

import type { Attachment, ChatMsg, Conversation, Settings } from "@/lib/types";
import { type EtapeWeb, fetchPlaygroundData, sendJSON, streamChat } from "@/lib/api";
// System notices (cronos_notice) are shared with the Support assistant,
// which also runs on the user's key: see lib/notices.ts.
import { texteNotice } from "@/lib/notices";
import {
  convAsJson, convAsMarkdown, convTitleFallback, downloadText, type ExportConversation,
} from "@/lib/export";
// Pure model-output parsers, extracted for unit tests (tests/playground-parsers.test.ts).
import { parseAsk, contenuCloture, openCodeFence } from "@/lib/playground-parsers";
// Thread/artifact helpers shared with the extracted child components
// (message bubbles, document panel): moved out verbatim, behaviour unchanged.
import {
  type Artifact,
  type QueuedMsg,
  type Snippet,
  type Tab,
  MAX_REPRISES_AUTO,
  PROMPT_INTEGRAL,
  PROMPT_REPRISE_COMPLET,
  REPRISE_INSTRUCTION,
  abandonDeclare,
  docTitleFromContent,
  estReprise,
  fichierLaisseOuvert,
  fichiersJusqua,
  isDocTask,
  mimePourLangage,
  nomTelechargeable,
  parseArtifacts,
  slugify,
  titleFromContext,
  tourAvorte,
  trimAfterAsk,
} from "@/lib/playground-thread";

import {
  fetchConversation,
  fetchConversations,
  persistConversation,
  removeConversation,
  migrateLegacyConversations,
} from "@/lib/conversations";
import { PlaygroundActions } from "./_components/PlaygroundActions";
import { PlaygroundComposer } from "./_components/PlaygroundComposer";
import { ContextDialog } from "./_components/ContextDialog";
import { DocumentPanel, type DocumentPanelProps } from "./_components/DocumentPanel";
import { HistoryDialog } from "./_components/HistoryDialog";
import { SettingsOverlay } from "./_components/SettingsOverlay";
import { SnippetsDialog } from "./_components/SnippetsDialog";
import { SummaryDialog } from "./_components/SummaryDialog";
import { DocumentPanelFullscreen } from "./_components/DocumentPanelFullscreen";
import { ThreadMessage } from "./_components/ThreadMessage";
import { ThreadTabs } from "./_components/ThreadTabs";
import { SkillCreator } from "./_components/SkillCreator";
import { BASE_SKILLS, type Skill, loadCustomSkills, saveCustomSkills, skillMatches } from "@/lib/skills";

const DEFAULT_SETTINGS: Settings = {
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

const ATTACH_ACCEPT =
  ".md,.markdown,.txt,.text,.log,.logs,.err,.error,.out,.json,.jsonl,.csv,.tsv,.yaml,.yml,.toml,.ini,.conf,.cfg,.env,.py,.js,.ts,.jsx,.tsx,.java,.c,.cpp,.h,.go,.rs,.rb,.php,.sh,.bash,.sql,.html,.css,.xml,.diff,.patch";

const MAX_ATTACHMENT_BYTES = 96 * 1024;

/** Extensions that the `accept` attribute only SUGGESTS: a file picker
 *  always lets « Tous les fichiers » through, and drag-and-drop
 *  ignores it entirely. Without this check, an image was read as TEXT
 *  (`readAsText`) and its binary content went into the prompt as
 *  mojibake, with no warning at all. */
const ATTACH_EXTENSIONS = ATTACH_ACCEPT.split(",").map((e) => e.trim().toLowerCase());

/** Images: accepted ONLY if the selected model reads them (`model_vision`).
 *  Downscaled in the browser before sending — a phone photo (4 000 px,
 *  5 Mo) would otherwise cost thousands of preload tokens and exceed
 *  the conversation save limit. 1 568 px is enough to read text
 *  or a screenshot. See `preparerImage`: an already reasonable image
 *  goes as is, without going through a canvas. */
const IMAGE_ACCEPT = ".png,.jpg,.jpeg,.webp,.gif";
const IMAGE_EXTENSIONS = IMAGE_ACCEPT.split(",");
const IMAGE_COTE_MAX = 1568;
const IMAGE_SOURCE_MAX_BYTES = 25 * 1024 * 1024;
const IMAGES_PAR_MESSAGE = 4;
/** Images replayed to the model per request: the most recent ones (same bound as the server). */
const IMAGES_MAX_REQUETE = 8;
/** Image budget of a SAVED conversation (characters): the server bounds
 *  the conversation to 2 M and the form field to 4 Mo. Beyond that, the
 *  oldest images are no longer kept (the text, however, is). */
const IMAGES_BUDGET_SAUVEGARDE = 1_500_000;
/** Estimated weight of an image in the context window, in characters (~1 500 tokens). */
const IMAGE_POIDS_CHARS = 1500 * 4;

/** Size beyond which an image is downscaled (the server rejects > ~2,2 Mo). */
const IMAGE_ORIGINALE_MAX_BYTES = 2 * 1024 * 1024;

function lireDataUrl(file: Blob): Promise<string> {
  return new Promise((ok, ko) => {
    const r = new FileReader();
    r.onload = () => ok(String(r.result));
    r.onerror = () => ko(r.error);
    r.readAsDataURL(file);
  });
}

/** Does the canvas really render what is drawn on it?
 *  Measured on 2026-10-01: on some browsers (canvas anti-tracking
 *  protections), reading back yields an EMPTY canvas — the image went out
 *  all black, and the model confidently described an invented scene. */
function canevasFiable(): boolean {
  try {
    const c = document.createElement("canvas");
    c.width = c.height = 4;
    const ctx = c.getContext("2d");
    if (!ctx) return false;
    ctx.fillStyle = "rgb(255,0,0)";
    ctx.fillRect(0, 0, 4, 4);
    const [r, g, b, a] = ctx.getImageData(1, 1, 1, 1).data;
    return r > 200 && g < 60 && b < 60 && a > 200;
  } catch {
    return false;
  }
}

/** The image to send, as a `data:` URL.
 *  An already reasonable image goes AS IS (no canvas: nothing to
 *  damage). Only a large image is downscaled to IMAGE_COTE_MAX in JPEG, and
 *  only if the canvas is reliable; otherwise we refuse rather than send
 *  a black image. */
async function preparerImage(file: File): Promise<string> {
  const bmp = await createImageBitmap(file);
  const { width, height } = bmp;
  const typeOk = /^image\/(png|jpeg|webp|gif)$/.test(file.type);
  if (typeOk && file.size <= IMAGE_ORIGINALE_MAX_BYTES && Math.max(width, height) <= IMAGE_COTE_MAX * 2) {
    bmp.close();
    return lireDataUrl(file);
  }
  if (!canevasFiable()) {
    bmp.close();
    throw new Error("canevas");
  }
  const echelle = Math.min(1, IMAGE_COTE_MAX / Math.max(width, height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(width * echelle));
  canvas.height = Math.max(1, Math.round(height * echelle));
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("canvas");
  ctx.fillStyle = "white";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);
  bmp.close();
  return canvas.toDataURL("image/jpeg", 0.85);
}

/** Keeps images only on the most recent messages, within `max`
 *  images or `budget` characters. Text is never touched. */
function imagesRecentes<M extends { images?: string[] }>(msgs: M[], max: number, budget = Infinity): M[] {
  let reste = max;
  let poids = 0;
  const out = [...msgs];
  for (let i = out.length - 1; i >= 0; i--) {
    const imgs = out[i].images;
    if (!imgs?.length) continue;
    const garde: string[] = [];
    for (let k = imgs.length - 1; k >= 0 && reste > 0; k--) {
      if (poids + imgs[k].length > budget) { reste = 0; break; }
      garde.unshift(imgs[k]);
      poids += imgs[k].length;
      reste--;
    }
    out[i] = { ...out[i], images: garde.length ? garde : undefined };
  }
  return out;
}

/** Title and summary only read the text: no need to ship megabytes of images through them. */
function sansImages(msgs: ChatMsg[]) {
  return msgs.map((m) => ({ role: m.role, content: m.content, hidden: m.hidden }));
}

/** Messages as they are saved (flags + images within the budget). */
function pourSauvegarde(msgs: ChatMsg[]) {
  return imagesRecentes(
    msgs.map((m) => ({
      role: m.role, content: m.content, hidden: m.hidden,
      truncated: m.truncated, isError: m.isError, images: m.images,
    })),
    Infinity, IMAGES_BUDGET_SAUVEGARDE);
}


// Appended to the system prompt so the model can ask the user one or several
// multiple-choice clarifying questions (rendered as selectable answers, submitted
// together) instead of guessing — the same idea as Claude's "ask the user" tool.
const ASK_INSTRUCTION = `When you need the user to clarify things before you can answer well, ask your questions as a single fenced block. Output it exactly like this:
\`\`\`ask
{"questions": [{"question": "<question 1>", "options": ["<option>", "<option>"]}, {"question": "<question 2>", "options": ["<option>", "<option>", "<option>"]}]}
\`\`\`
Strict rules:
- Before the block you MAY write ONE short introductory sentence (e.g. "Bien sûr ! Quelques précisions pour bien t'aider :"). Do NOT write the questions or their options as normal text anywhere — they go INSIDE the block ONLY.
- Ask as many questions as are genuinely useful — two if two are enough, more if the request really needs it. Do not pad to reach a number, and do not drop a question that matters. Each question gets 2 to 6 short options in the user's language. Ask everything you need in this one block (the user answers them all at once).
- Do NOT add an "Other" option (the interface adds one).
- The user can pick SEVERAL options for the same question, so write options that can be combined rather than mutually exclusive ones whenever that makes sense. Their answer may come back as "A + B".
- Ask AT MOST ONCE. As soon as the user has answered, you MUST give your real, complete answer using their choices — NEVER reply with another ask block once they have answered.
- The fence language MUST be \`ask\` — never \`json\` or anything else, or the user will not see clickable questions.
- Only ask when it genuinely helps; otherwise just answer normally.`;

// Instruction added to the system prompt: fix an already produced file without
// rewriting it entirely. Rewriting 400 lines to change three costs time,
// tokens, and reintroduces errors elsewhere in the file.
// The model names its own files: it knows what the user asked for
// and which project they fit into. Without this, the UI has to guess and
// falls back to a generic name.
const NAME_INSTRUCTION = `Name every file you output: put its path in backticks on the line just before the code block (\`index.html\`, \`roles/web/tasks/main.yml\`), chosen from what the user asked. Reuse the exact same name for a file you already produced.`;

// The partial-edit protocol was removed: on generated code containing
// errors, a spot fix left a half-right file, and
// a mis-copied anchor did not apply at all. We ask for the file
// ENTIRE — it is longer to generate, but what comes out is usable as is.
const REWRITE_INSTRUCTION = `When the user asks you to fix or change a file you already produced, output that file COMPLETE, from its first line to its last, under the exact same name. Never output a partial file, an excerpt, a diff, or a "rest unchanged" placeholder.`;

// The model GIVES UP on its own on a large file: measured in prod on 22/08,
// it stopped at 14 187 tokens out of 131 072 available, with
// finish_reason=stop (so nothing distinguished it from a successful answer),
// writing « Le fichier est trop long pour etre affiche en entier ici » followed
// by a truncated file presented as complete. No technical limit was
// reached. This instruction goes out on EVERY turn, not only when a
// file already exists, and goes LAST so as not to dilute ASK_INSTRUCTION
// (whose effectiveness depends on its position at the top, see measurement below).
const INTEGRALITE_INSTRUCTION = `Never abridge a file you were asked to produce. Never write that a file is "too long to show here", never say you are giving a "shortened", "simplified" or "essential" version, and never replace any part of a file with an ellipsis, a placeholder, or a comment such as "rest of the code unchanged". There is no display limit: write the file in full, from its first line to its last. If you run out of room before the end, stop mid-file rather than closing it early — you will be asked to continue, and you will resume at the exact character where you stopped. A truncated file presented as complete is the worst possible answer.`;

// Input placeholder: we rotate a few texts (including the « / » trick
// to call a skill). Each entry is an i18n key (FR-as-msgid).
const PLACEHOLDER_TEXTS = [
  "Comment puis-je vous aider aujourd'hui ?",
  "Tapez / pour appeler une compétence",
  "Résumez un document, générez une image, écrivez du code…",
];

// « / » commands that open the skill creator.
const SLASH_CREATE_COMMANDS = ["skill-creator", "create", "new", "creer", "competence", "compétence"];

// Pinned (starred) conversations: a personal preference stored in the browser —
// no backend migration, shared on this machine.
const PINNED_KEY = "cronos.pinned.conversations";
function loadPinnedIds(): string[] {
  try {
    return (JSON.parse(localStorage.getItem(PINNED_KEY) || "[]") as string[]);
  } catch {
    return [];
  }
}
function savePinnedIds(ids: string[]) {
  try {
    localStorage.setItem(PINNED_KEY, JSON.stringify(ids));
  } catch {
    /* storage unavailable (private mode): we ignore it */
  }
}

// Math/LaTeX: rendering now lives in `lib/maths.tsx`, called by
// `MarkdownSur` (see `lib/markdown.tsx`). It could not stay here: an
// `inlinePlugins` applies PER text node, and the parser splits text at
// every backslash — so the formula was never recognized. Protecting
// LaTeX now happens BEFORE the parser.
const SNIPPET_KEY = "cronos.snippets";
function loadSnippets(): Snippet[] {
  try {
    return (JSON.parse(localStorage.getItem(SNIPPET_KEY) || "[]") as Snippet[]);
  } catch {
    return [];
  }
}
function saveSnippets(list: Snippet[]) {
  try {
    localStorage.setItem(SNIPPET_KEY, JSON.stringify(list));
  } catch {
    /* storage unavailable (private mode): we ignore it */
  }
}

// Tabs: several conversations open in parallel. The generation logic
// stays intact (the live state `messages`/`model`/… belongs to the ACTIVE
// tab); we only save/restore each tab's snapshot on
// switch. Switching is disabled during a stream so as not to cut a
// answer in progress.
let tabSeq = 0;
function newTabId() {
  tabSeq += 1;
  return `tab-${Date.now()}-${tabSeq}`;
}

// Renaming generated files (artifacts). Key = `convId::kind::titre`
// (file names are made unique by the model) and persisted in
// localStorage to survive a reload; `convId` prevents the same name
// (`fichier-1.yml`) in two conversations from renaming each other.
const ARTIFACT_RENAME_KEY = "cronos.artifact.renames";
function loadArtifactRenames(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(ARTIFACT_RENAME_KEY) || "{}") as Record<string, string>;
  } catch {
    return {};
  }
}
function saveArtifactRenames(m: Record<string, string>) {
  try {
    localStorage.setItem(ARTIFACT_RENAME_KEY, JSON.stringify(m));
  } catch {
    /* storage unavailable (private mode): we ignore it */
  }
}
function artifactRenameKey(convId: string | null, a: Artifact): string {
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
const SETTINGS_KEY = "cronos.playground.settings";
type Provenance = "persona" | "skill" | "manual";
type ReglagesStockes = { settings: Settings; provenance: Provenance };
function chargerReglages(): ReglagesStockes {
  const base: ReglagesStockes = { settings: DEFAULT_SETTINGS, provenance: "manual" };
  try {
    const brut = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "null") as
      (Partial<Settings> & { provenance?: unknown }) | null;
    if (!brut || typeof brut !== "object") return base;
    // Field by field over the defaults: a value stored by an OLDER version (or
    // hand-edited) must not break the panel — unknown or wrong-typed, it is
    // ignored and the default stands.
    return {
      settings: {
        ...DEFAULT_SETTINGS,
        system: typeof brut.system === "string" ? brut.system : DEFAULT_SETTINGS.system,
        temperature: typeof brut.temperature === "number" ? brut.temperature : DEFAULT_SETTINGS.temperature,
        maxTokens: typeof brut.maxTokens === "number" ? brut.maxTokens : DEFAULT_SETTINGS.maxTokens,
        topP: typeof brut.topP === "number" ? brut.topP : DEFAULT_SETTINGS.topP,
        reasoning:
          brut.reasoning === true || brut.reasoning === false || brut.reasoning === "auto"
            ? brut.reasoning
            : DEFAULT_SETTINGS.reasoning,
        reasoningEffort:
          typeof brut.reasoningEffort === "string" ? brut.reasoningEffort : DEFAULT_SETTINGS.reasoningEffort,
      },
      provenance: brut.provenance === "persona" ? "persona" : "manual",
    };
  } catch {
    return base; // stockage illisible : on repart des réglages par défaut
  }
}
function enregistrerReglages(settings: Settings, provenance: Provenance) {
  try {
    // The system prompt a SKILL injects belongs to that skill, not to the
    // user's own defaults: selecting a skill must not become the persona of
    // every conversation to come. We keep the stored one instead.
    const ancien = chargerReglages();
    const persona =
      provenance === "skill"
        ? { system: ancien.settings.system, provenance: ancien.provenance }
        : { system: settings.system, provenance };
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({ ...settings, ...persona }));
  } catch {
    /* storage unavailable (private mode): we ignore it */
  }
}



// Playground settings panel: target width, and safety margin to the
// window edge (= --spacing-2). See toggleSettings for the clamping.
const LARGEUR_PANNEAU_REGLAGES = 480;
const MARGE_PANNEAU_REGLAGES = 8;

export default function PlaygroundPage() {
  const t = useT();
  // The composer's two effects (border beam, voice halo) paint
  // according to the background: we give them the APPLICATION mode, not `auto`,
  // because Astryx only sets `data-theme` for dark (see ThinkingIndicator).
  const { mode } = useThemeMode();
  const { open: openSettings } = useSettingsDialog();
  const csrf = useCsrf();
  const [runningModels, setRunningModels] = useState<string[]>([]);
  const [modelLimits, setModelLimits] = useState<Record<string, number>>({});
  const [modelVision, setModelVision] = useState<Record<string, boolean>>({});
  // Image opened large from a bubble (null = closed).
  const [imageVue, setImageVue] = useState<{ srcs: string[]; index: number } | null>(null);
  // null = not known yet. We only show the warning once the answer is received,
  // so as not to flash a warning on load.
  const [hasKey, setHasKey] = useState<boolean | null>(null);
  const [model, setModel] = useState("");
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  // Index of the user message being edited (inline edit). The
  // conversation stays displayed; on resend, we rebranch from that index.
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  // Playground settings, restored from the browser (see `chargerReglages`):
  // « je dois le sélectionner à chaque fois » — nothing was kept, so every
  // reload threw the panel back to the defaults. Both states read the SAME
  // stored object (parsed once), and every change writes it back.
  const [reglagesDepart] = useState(chargerReglages);
  const [settings, setSettings] = useState<Settings>(reglagesDepart.settings);
  // Origin of the system prompt: a persona, a skill, or manual input.
  // Allows flagging that a skill overwrote a persona's system prompt.
  const [systemProvenance, setSystemProvenance] = useState<Provenance>(reglagesDepart.provenance);
  useEffect(() => {
    enregistrerReglages(settings, systemProvenance);
  }, [settings, systemProvenance]);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  // Position of the settings panel: captured on the button click (button rect →
  // bottom-left corner of the panel under the gear). FIXED overlay:
  // it never touches the flow, so the page does not move a pixel.
  const [settingsPos, setSettingsPos] = useState<{ top: number; right: number; maxH: number; largeur: number } | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  // Closing the settings panel: outside click or Escape. NO focus move
  // on open — the Popover component's focus trap used to
  // scroll the page (sr-only close button revealed under the panel),
  // hence a chat "yanked back down" on every click of the gear.
  const toggleSettings = (el: HTMLElement | null) => {
    if (!isSettingsOpen && el) {
      const r = el.getBoundingClientRect();
      // The panel is anchored to the gear, but its width cannot exceed the
      // window: under 768 px the sidebar is folded, the gear moves down to
      // ~250 px from the left edge, and a 480 px panel anchored there started 201 px
      // OFF SCREEN — with no way out, a `position: fixed` does not scroll. So we clamp
      // the width to the window, then the right offset so the left
      // edge stays visible. Beyond ~1024 px the gear is far enough right:
      // both bounds are inactive and the panel keeps its nominal size.
      const largeur = Math.min(LARGEUR_PANNEAU_REGLAGES, window.innerWidth - 2 * MARGE_PANNEAU_REGLAGES);
      setSettingsPos({
        top: r.bottom + 8,
        right: Math.max(MARGE_PANNEAU_REGLAGES,
          Math.min(window.innerWidth - r.right, window.innerWidth - largeur - MARGE_PANNEAU_REGLAGES)),
        maxH: window.innerHeight - r.bottom - 48,
        largeur,
      });
    }
    setIsSettingsOpen((v) => !v);
  };
  useEffect(() => {
    if (!isSettingsOpen) return;
    const onDown = (e: PointerEvent) => {
      const cible = e.target as HTMLElement | null;
      // Clicking the gear itself goes through the button's toggle: do not
      // close here, otherwise pointerdown closes then click reopens (net zero).
      if (cible?.closest(".playground-settings-panel")) return;
      setIsSettingsOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setIsSettingsOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [isSettingsOpen]);
  // Search in history + pinned (starred) conversations.
  const [histQuery, setHistQuery] = useState("");
  const [pinnedIds, setPinnedIds] = useState<string[]>(loadPinnedIds);
  // Renaming a conversation from the history.
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [streaming, setStreaming] = useState(false);

  // Tabs: several conversations open. The live state above is
  // the active tab; each `tabs` entry keeps a snapshot of it.
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [activeTabId, setActiveTabId] = useState<string>("");
  const tabsRef = useRef<Tab[]>([]);
  useEffect(() => { tabsRef.current = tabs; }, [tabs]);
  const tabsInitRef = useRef(false);
  useEffect(() => {
    if (tabsInitRef.current) return;
    tabsInitRef.current = true;
    const id = newTabId();
    setActiveTabId(id);
    setTabs([{ id, title: "", currentId: null, model, settings, attachments: [], messages: [] }]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Queue: messages submitted while an answer is generating. Instead
  // of being lost (the old « if (streaming) return »), they pile up in a
  // panel above the composer and go out ALL BY THEMSELVES as soon as the current
  // answer ends. Each row's buttons only short-circuit
  // this wait: « Envoyer » interrupts the current answer (its already
  // written part is kept, as with Stop) to move to this message right away.
  // Chained resumes without intervention: beyond that, we hand back rather
  // than let a looping model consume the account's budget.
  const reprisesRef = useRef(0);
  const [reprise, setReprise] = useState(0);
  const [queued, setQueued] = useState<QueuedMsg[]>([]);
  const queuedRef = useRef<QueuedMsg[]>([]);
  // runStream closes the conversation from its own closure: to start again from
  // the up-to-date list (partial answer kept, etc.) we read via ref.
  const messagesRef = useRef(messages);
  useEffect(() => { messagesRef.current = messages; });

  // ── Thread auto-scroll during generation ───────────────────────────────────
  // Follow the bottom of the stream while it pushes — and STOP as soon as the
  // reader scrolls up. The old mechanism attached its listener at page MOUNT
  // (deps []) on `.astryx-chat-layout`: but ChatLayout does not exist yet at
  // that point — it only shows up at the first conversation (the
  // `isFirstEmpty` ternary) — so the listener was NEVER attached,
  // `suitLeBas` stayed stuck at true and pinning pulled back to the bottom
  // on EVERY render: « je ne peux plus monter quand il réfléchit ». The
  // home-made hook (useStickToBottom, already in service for the live panel
  // and the admin logs) attaches by REF: it lands when the element appears,
  // and re-arms on the real distance to the bottom (48 px), not on a scroll
  // delta.
  const dernier = messages[messages.length - 1];
  const fluxDep = `${dernier?.content?.length ?? 0}:${dernier?.reasoning?.length ?? 0}`;
  const {
    setRef: suitLeBasSetRef,
    showButton: montrerDescendre,
    scrollToBottom: descendreToutEnBas,
  } = useStickToBottom(fluxDep, streaming);
  // The scroll container is the ROOT of ChatLayout, and it alone.
  // Root cause of « la scrollbar ne marche pas » (2026-10-04): we passed
  // `scrollRef` to ChatLayout with a ref NEVER attached to an element —
  // ChatLayout then stops being self-scrolling (`isSelfScrolling = !scrollRef`)
  // and renders its root as `overflow: visible`: NO scroll container existed
  // in the chat column, neither the wheel nor the bar did anything (measured:
  // `scrollHeight` 1278 for `clientHeight` 817 and `scrollTop` always 0).
  // Here `suitLeBasSetRef` IS the root's callback ref: it attaches when the
  // conversation appears (ChatLayout does not exist on the empty page), and
  // it serves the bottom-following as well as the « Descendre » button.

  const updateQueue = (q: QueuedMsg[]) => {
    queuedRef.current = q;
    setQueued(q);
  };
  // The queue panel grows/shrinks the dock, which is sticky at the BOTTOM of the
  // scroller: its height adds to the content, and if we were following the bottom,
  // the last line ends up hidden underneath. Astryx only observes the message
  // content, not the dock — hence this explicit re-stick. We only do it if
  // we were already stuck to the bottom, so as not to yank someone re-reading
  // further up. The scroller is ChatLayout itself (no scrollRef provided),
  // and the page contains only one.
  useEffect(() => {
    const el = document.querySelector<HTMLElement>(".astryx-chat-layout");
    if (!el) return;
    // Wide margin: the gap just GREW by the panel's height.
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 200) {
      el.scrollTo({ top: el.scrollHeight });
    }
  }, [queued.length]);
  // Live rate: `usage.completion_tokens` only arrives at the END of the stream. We
  // therefore estimate the token count from the received CHARACTERS — counting
  // SSE deltas would be wrong (measured: with MTP speculative decoding, vLLM sends
  // several tokens per delta, hence ~2,7x underestimation). The
  // characters/token ratio is auto-calibrated at the end of each generation on the
  // exact `usage`, so it adapts to the model and the language (measured: ~4,5 in
  // French, ~5,0 in English). Refs: updated on every delta without re-render.
  const liveCharsRef = useRef(0);
  const charsPerTokenRef = useRef(4.8);
  const liveStartRef = useRef<number | null>(null);
  const [liveStats, setLiveStats] = useState<{ tokens: number; tps: number } | null>(null);

  // Refreshes the displayed counter 4x/s during the stream — smooth enough to the eye,
  // without adding a re-render per token (updateLast already does one).
  useEffect(() => {
    if (!streaming) return;
    const id = setInterval(() => {
      const start = liveStartRef.current;
      const chars = liveCharsRef.current;
      if (!start || !chars) return;
      const secs = (performance.now() - start) / 1000;
      const tokens = Math.round(chars / charsPerTokenRef.current);
      if (secs > 0 && tokens > 0) setLiveStats({ tokens, tps: Number((tokens / secs).toFixed(1)) });
    }, 250);
    return () => clearInterval(id);
  }, [streaming]);
  // Web-search steps of the IN-PROGRESS generation. Without this display,
  // the wait is silent: several tens of seconds during which the
  // model searches and reads, with nothing indicating it.
  const [etapesWeb, setEtapesWeb] = useState<EtapeWeb[]>([]);
  // First runtime error reported by the preview (empty = the page runs).
  const [erreurApercu, setErreurApercu] = useState("");
  const [currentId, setCurrentId] = useState<string | null>(null);
  // Mirror of `currentId`: the end of a stream runs in its own closure and
  // must know whether the user is STILL looking at the same conversation (see
  // below, "did the thread change during the generation").
  const currentIdRef = useRef<string | null>(null);
  useEffect(() => { currentIdRef.current = currentId; }, [currentId]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [ctxUsed, setCtxUsed] = useState(0);
  // Input/output split of the LAST generation (exact measurement reported
  // by LiteLLM via `usage`): prompt_tokens = what fills the window,
  // completion_tokens = the produced answer. null until a generation happens.
  const [ioTokens, setIoTokens] = useState<{ prompt: number; completion: number } | null>(null);
  // Cumulated tokens of the current conversation (real cost: each request
  // bills prompt + completion, so the sum of `total_tokens` = what is
  // deducted from the budget). Per message, `m.tokens` is the already existing display.
  const [convTokens, setConvTokens] = useState(0);
  useWhoami();
  // Artifact/canvas side-panel: when the assistant writes a file (a substantial
  // code block), it opens on the side automatically instead of being dumped
  // inline — inspired by the Astryx ai-chat template.
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const artifactResize = useResizable({ defaultSize: 560, minSizePx: 420, maxSizePx: 900, autoSaveId: "playground-artifact" });
  // On phones the resizable side panel would crush the chat, so the artifact
  // opens as a fullscreen dialog instead.
  const isNarrow = useIsNarrow();
  // "Watch the document being written live": set when the user clicks the
  // in-progress document card during a document stream. A ref mirrors it so the
  // stream-completion closure can read the current value.
  const [liveDocOpen, setLiveDocOpen] = useState(false);
  // Rendered preview of a generated HTML page, rather than its source code.
  const [htmlPreview, setHtmlPreview] = useState(true);
  // URL of the preview served by the backend. A `srcdoc` iframe inherits the
  // portal's CSP and its inline scripts are blocked: the page displays but
  // nothing responds in it. So we go through a response that carries its own sandbox.
  const [previewUrl, setPreviewUrl] = useState("");
  // Full screen for the panel: essential to watch a generated HTML page,
  // unreadable in a 400 px column.
  const [plein, setPlein] = useState(false);
  const liveDocOpenRef = useRef(false);
  /** Panel close: leaving full screen returns to the side panel, we only
   *  close the file if we were not in it. */
  const fermerPanneau = () => {
    if (plein) { setPlein(false); return; }
    setArtifact(null);
    setLiveDocOpen(false);
    // A closed panel stays closed: an in-progress draft must not reopen it
    // behind the user (the ref serves the end-of-stream summary).
    liveDocOpenRef.current = false;
  };
  const openLiveDoc = () => { setLiveDocOpen(true); liveDocOpenRef.current = true; };

  const abortRef = useRef<AbortController | null>(null);

  // Dictation: same hook as the Voice and Video pages.
  const dictation = useDictation({ value: input, onChange: setInput, csrf });
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Radius of the composer card, in pixels, for the two effects that
  // wrap it (voice halo, border beam).
  //
  // Both libraries can detect it on their own — but on their
  // FIRST CHILD, and `border-beam`'s first child is its OWN
  // `<style>` tag (0 px): measured on 2026-10-01, the beam was therefore drawn with
  // the 16 px fallback in a 28 card, corners visibly squarer than
  // the composer. We read the token rather than hardcoding « 28 » here: it is the
  // same source of truth as the card (`--radius-chat`, `:root` in
  // astryx.css), so a theme that changes radii follows.
  const [rayonComposeur, setRayonComposeur] = useState<number | undefined>(undefined);
  useEffect(() => {
    const px = parseFloat(getComputedStyle(document.documentElement)
      .getPropertyValue("--radius-chat"));
    // DOM read on mount: `getComputedStyle` does not exist at server
    // render, so the value cannot be derived during render. A single
    // setState, once, no cascade — this is the "sync
    // from an external system" case the rule aims to distinguish from render
    // loops. (The directive must IMMEDIATELY precede the code: placed before the
    // comments, it only disabled the first comment line.)
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (Number.isFinite(px) && px > 0) setRayonComposeur(px);
  }, []);

  useEffect(() => {
    // A WAITING guard, not a dead button: the effect replays as soon as the token
    // arrives (`csrf` dependency), so nothing to report to the user.
    if (!csrf) return;
    let annule = false;
    (async () => {
      await migrateLegacyConversations(csrf);
      const convs = await fetchConversations();
      if (!annule) setConversations(convs);
    })();
    return () => {
      annule = true;
    };
  }, [csrf]);

  useEffect(() => {
    fetchPlaygroundData()
      .then((data) => {
        setRunningModels(data.running_models);
        setModelLimits(data.model_limits);
        setModelVision(data.model_vision ?? {});
        setHasKey(data.has_key);
        if (data.running_models.length) setModel(data.running_models[0]);
      })
      .catch(() => {});
  }, []);

  // Targeted open from the home page: ?conv=<id> opens a specific
  // conversation, ?model=<name> preselects the current model. Applied once,
  // once the conversations AND the current models are loaded (the two
  // fetches are in separate effects, the order is not guaranteed).
  const initFromUrlRef = useRef(false);
  /* eslint-disable react-hooks/set-state-in-effect, react-hooks/exhaustive-deps -- one-shot init from the URL */
  useEffect(() => {
    if (initFromUrlRef.current) return;
    const params = new URLSearchParams(window.location.search);
    const convId = params.get("conv");
    const modelName = params.get("model");
    if (!convId && !modelName) return;
    if (runningModels.length === 0) return; // waiting for the models
    if (convId && conversations.length === 0) return; // waiting for the conversations
    initFromUrlRef.current = true;
    if (modelName && runningModels.includes(modelName)) setModel(modelName);
    const conv = convId ? conversations.find((c) => c.id === convId) : undefined;
    if (conv) {
      setMessages(conv.messages.map((m) => ({ role: m.role, content: m.content, hidden: m.hidden })));
      setCurrentId(conv.id);
      setTabs((prev) => prev.map((t) => (t.id === activeTabId ? { ...t, title: conv.title, currentId: conv.id } : t)));
      if (conv.model && runningModels.includes(conv.model)) setModel(conv.model);
      setCtxUsed(0);
      setConvTokens(0);
      setIoTokens(null);
    }
  }, [conversations, runningModels]);
  /* eslint-enable react-hooks/set-state-in-effect, react-hooks/exhaustive-deps */

  // persist()/runStream() only ever run from event handlers (send/regenerate/edit),
  // never during render, so Date.now()/performance.now() here are safe despite the
  // purity lint rule's conservative render-reachability analysis.
  function persist(msgs: ChatMsg[], convId: string | null, activeModel: string) {
    if (!msgs.length) return convId;
    const title = (msgs.find((m) => m.role === "user")?.content || t("Conversation")).slice(0, 80);
    const item: Conversation = {
       
      id: convId ?? String(Date.now()),
      title,
       
      ts: Date.now(),
      model: activeModel,
      // `hidden` is part of the message: without it, an answer to questions
      // became an ordinary message again on reload, shifting indexes and
      // changing the rendering of the conversation.
      // `truncated` and `isError` travel too, and the server keeps them:
      // without them, an answer cut mid-file passed for COMPLETE
      // after a reload — no more banner, no more « Continuer » button,
      // while the file remained unfinished on screen.
      messages: pourSauvegarde(msgs),
    };
    // Optimistic on the UI side, then server save in the background: the
    // list must not wait for the network round-trip to update.
    setConversations((prev) => {
      const rest = prev.filter((c) => c.id !== item.id);
      return [item, ...rest];
    });
    // A refused save (413, database unavailable) must not turn into
    // a silently emptied history: we SAY it.
    if (csrf) {
      void persistConversation(csrf, item).then((ok) => {
        if (!ok) showToast({ body: t("Conversation non enregistrée — elle disparaîtra au rechargement."), type: "error" });
      });
    }
    return item.id;
  }

  // Close any open document/file panel — it belongs to the conversation we're
  // leaving, not the one we're opening.
  function closeArtifact() {
    setArtifact(null);
    setLiveDocOpen(false);
  }

  function newConversation() {
    // No re-save here: each generation already saves at its end.
    // Re-saving used to move the conversation to the top of the list on the mere
    // fact of changing it, while the order must reflect the last ACTIVITY.
    // With tabs, « nouvelle conversation » opens a clean tab.
    newTab();
  }

  // Switch to a tab: we freeze the active tab's snapshot then
  // restore the target's. Disabled during a stream (the live state then
  // belongs to the ongoing generation).
  function switchTab(id: string) {
    if (streaming || id === activeTabId || !id) return;
    setTabs((prev) => prev.map((t) =>
      t.id === activeTabId ? { ...t, messages, currentId, model, settings, attachments } : t));
    const target = tabsRef.current.find((t) => t.id === id);
    if (!target) return;
    setMessages(target.messages);
    setCurrentId(target.currentId);
    if (target.model && runningModels.includes(target.model)) setModel(target.model);
    setSettings(target.settings);
    setAttachments(target.attachments);
    setCtxUsed(0);
    setConvTokens(0);
    setEditingIdx(null);
    setIoTokens(null);
    updateQueue([]);
    closeArtifact();
    setActiveTabId(id);
  }

  function newTab() {
    if (streaming) return;
    const id = newTabId();
    setTabs((prev) => [
      ...prev.map((t) => (t.id === activeTabId ? { ...t, messages, currentId, model, settings, attachments } : t)),
      { id, title: "", currentId: null, model, settings, attachments: [], messages: [] },
    ]);
    setActiveTabId(id);
    setMessages([]);
    setCurrentId(null);
    setAttachments([]);
    setEditingIdx(null);
    setCtxUsed(0);
    setConvTokens(0);
    setIoTokens(null);
    updateQueue([]);
    closeArtifact();
  }

  function closeTab(id: string) {
    if (streaming) return;
    const idx = tabsRef.current.findIndex((t) => t.id === id);
    if (idx < 0) return;
    // Title generated on close: we analyze the WHOLE conversation we
    // close (not just its first exchange) to set a reliable title
    // in the history. Silent if the conversation has neither messages nor id.
    const closingTab = tabsRef.current[idx];
    if (closingTab && closingTab.messages.length > 0 && closingTab.currentId) {
      void autoTitle(closingTab.currentId, closingTab.messages, id, closingTab.model);
    }
    const next = tabsRef.current.filter((t) => t.id !== id);
    setTabs(next);
    if (activeTabId !== id) return;
    const fallback = next[Math.min(idx, next.length - 1)];
    if (fallback) {
      setMessages(fallback.messages);
      setCurrentId(fallback.currentId);
      if (fallback.model && runningModels.includes(fallback.model)) setModel(fallback.model);
      setSettings(fallback.settings);
      setAttachments(fallback.attachments);
      setCtxUsed(0);
      setConvTokens(0);
      setEditingIdx(null);
      setIoTokens(null);
      updateQueue([]);
      closeArtifact();
      setActiveTabId(fallback.id);
    } else {
      const nid = newTabId();
      setTabs([{ id: nid, title: "", currentId: null, model, settings, attachments: [], messages: [] }]);
      setActiveTabId(nid);
      setMessages([]);
      setCurrentId(null);
      setAttachments([]);
      setEditingIdx(null);
      setCtxUsed(0);
      setConvTokens(0);
      setIoTokens(null);
      updateQueue([]);
      closeArtifact();
    }
  }

  function selectConversation(conv: Conversation) {
    // The history list is BOUNDED: beyond a byte budget, the
    // server only carried the metadata (`messages_omis`). We fetch
    // the content when we really open it — without this detour, the
    // conversation would open on an empty thread, which is worse than the latency
    // we save.
    if (conv.messagesOmis) {
      void fetchConversation(conv.id).then((complete) => {
        if (complete) {
          setConversations((prev) => prev.map((c) => (c.id === complete.id ? complete : c)));
          appliquerConversation(complete);
        } else {
          showToast({ body: t("Cette conversation n'a pas pu être chargée."), type: "error" });
        }
      });
      return;
    }
    appliquerConversation(conv);
  }

  /** Applies a conversation to the displayed thread (open from the history). */
  function appliquerConversation(conv: Conversation) {
    // Same: opening a conversation does not modify it, so it must not
    // move it up nor rewrite the one we leave.
    setMessages(conv.messages.map((m) => ({ role: m.role, content: m.content, hidden: m.hidden })));
    setCurrentId(conv.id);
    setEditingIdx(null);
    // The active tab carries this conversation (title in the tab bar).
    setTabs((prev) => prev.map((t) => (t.id === activeTabId ? { ...t, title: conv.title, currentId: conv.id } : t)));
    if (conv.model && runningModels.includes(conv.model)) setModel(conv.model);
    setCtxUsed(0);
    setConvTokens(0);
    setIoTokens(null);
    updateQueue([]);
    closeArtifact();
  }

  function deleteConversation(id: string) {
    setConversations((prev) => prev.filter((c) => c.id !== id));
    if (csrf) {
      // Deletion is optimistic on screen, but a server refusal is
      // SAID: otherwise the conversation came back on reload, as if the click
      // had done nothing.
      void removeConversation(csrf, id).then((ok) => {
        if (!ok) {
          showToast({
            body: t("Suppression impossible — la conversation est conservée."),
            type: "error",
          });
        }
      });
    }
    if (id === currentId) setCurrentId(null);
  }

  // Star/pin a conversation (browser preference, sorted first).
  function togglePinned(id: string) {
    setPinnedIds((prev) => {
      const next = prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id];
      savePinnedIds(next);
      return next;
    });
  }

  // Snippets: saves the current prompt, inserts, deletes.
  function saveSnippet() {
    const content = input.trim();
    if (!content) return;
     
    const snip: Snippet = { id: String(Date.now()), label: content.slice(0, 60), content };
    setSnippets((prev) => { const next = [...prev, snip]; saveSnippets(next); return next; });
  }
  function deleteSnippet(id: string) {
    setSnippets((prev) => { const next = prev.filter((s) => s.id !== id); saveSnippets(next); return next; });
  }
  function insertSnippet(content: string) {
    setInput(content);
    setSnippetsOpen(false);
  }

  // Auto-title: asks the model for a short title, then renames the conversation.
  async function genTitle() {
    if (busyTitle || !messages.length || !currentId) return;
    setBusyTitle(true);
    try {
      const res = await sendJSON<{ title?: string; error?: string }>("/api/playground/title", csrf, { model, messages: sansImages(messages) });
      const title = res?.title;
      const conv = conversations.find((c) => c.id === currentId);
      if (title && conv) {
        setConversations((prev) => prev.map((c) => (c.id === currentId ? { ...c, title } : c)));
        setTabs((prev) => prev.map((t) => (t.currentId === currentId || t.id === activeTabId ? { ...t, title } : t)));
        void persistConversation(csrf, { ...conv, title });
      }
    } finally {
      setBusyTitle(false);
    }
  }

  // Summary: condenses the conversation, displayed in a copyable dialog.
  async function genSummary() {
    if (!messages.length) return;
    setSummary(t("Génération en cours…"));
    setSummaryOpen(true);
    try {
      const res = await sendJSON<{ summary?: string; error?: string }>("/api/playground/summarize", csrf, { model, messages: sansImages(messages) });
      if (res.summary) setSummary(res.summary);
      else setSummary(res.error ? tServeur(res.error, t) : t("Impossible de générer le résumé."));
    } catch {
      setSummary(t("Impossible de générer le résumé."));
    }
  }

  // Auto-title triggered on the first answer: short summary generated by the
  // model, propagated live to the history + the tab, without a reload.
  async function autoTitle(convId: string, msgs: ChatMsg[], tabId: string, modelForTitle?: string) {
    if (!csrf) {
      showToast({ body: t("Session incomplète — recharge la page."), type: "error" });
      return;
    }
    const titleModel = modelForTitle || model;
    try {
      const res = await sendJSON<{ title?: string; error?: string }>("/api/playground/title", csrf, { model: titleModel, messages: sansImages(msgs) });
      const title = res?.title;
      if (!title) return;
      setConversations((prev) => prev.map((c) => (c.id === convId ? { ...c, title } : c)));
      setTabs((prev) => prev.map((t) => (t.id === tabId || t.currentId === convId ? { ...t, title, currentId: convId } : t)));
      const item: Conversation = {
        id: convId, title,
         
        ts: Date.now(), model: titleModel,
        messages: pourSauvegarde(msgs),
      };
      void persistConversation(csrf, item);
    } catch {
      // silent: we keep the provisional title (start of the prompt)
    }
  }

  // Renaming a generated file: applies the new name (persisted per
  // conversation) and closes the edit. The title updates in the chat card,
  // the panel and the download name.
  function renamedTitle(a: Artifact): string {
    return artifactRenames[artifactRenameKey(currentId, a)] ?? a.title;
  }
  function commitArtifactRename(a: Artifact, title: string) {
    const clean = title.trim();
    if (clean) {
      const key = artifactRenameKey(currentId, a);
      setArtifactRenames((prev) => {
        const next = { ...prev, [key]: clean };
        saveArtifactRenames(next);
        return next;
      });
    }
    setRenamingArtifact(false);
  }

  const [shared, setShared] = useState(false);
  // Snippets / reusable prompts (browser library).
  const [snippetsOpen, setSnippetsOpen] = useState(false);
  const [snippets, setSnippets] = useState<Snippet[]>(loadSnippets);
  // Skills: base skills + those created by the user.
  const [skills, setSkills] = useState<Skill[]>(() => [...BASE_SKILLS, ...loadCustomSkills()]);
  const [skillCreatorOpen, setSkillCreatorOpen] = useState(false);
  const [editingSkill, setEditingSkill] = useState<Skill | null>(null);
  const customSkills = skills.filter((s) => !s.builtin);

  // Opens the creator (edit if an existing skill is provided) and
  // closes the « / » menu so as not to stack modal + menu.
  function openSkillCreator(skill?: Skill) {
    setEditingSkill(skill ?? null);
    setSkillCreatorOpen(true);
    setInput("");
  }

  function addCustomSkill(s: Skill) {
    const custom = [...customSkills, s];
    saveCustomSkills(custom);
    setSkills([...BASE_SKILLS, ...custom]);
  }

  // Edit: we replace the skill with the same id (the others stay).
  function updateCustomSkill(s: Skill) {
    const custom = customSkills.map((c) => (c.id === s.id ? s : c));
    saveCustomSkills(custom);
    setSkills([...BASE_SKILLS, ...custom]);
  }

  function deleteCustomSkill(id: string) {
    const custom = customSkills.filter((s) => s.id !== id);
    saveCustomSkills(custom);
    setSkills([...BASE_SKILLS, ...custom]);
  }

  // Selecting a skill: we put its (localized) prompt into the input
  // and, if it has one, apply its system prompt to the model (flagging
  // that the provenance becomes « compétence »).
  function selectSkill(s: Skill) {
    setInput(t(s.prompt));
    const sp = s.systemPrompt;
    if (sp) {
      setSettings((prev) => ({ ...prev, system: sp }));
      setSystemProvenance("skill");
    }
  }

  // Auto-title + summary (generated by the model, billed to the budget).
  const [summaryOpen, setSummaryOpen] = useState(false);
  const [summary, setSummary] = useState("");
  const [busyTitle, setBusyTitle] = useState(false);
  // « contexte » panel: what the model sees (system, files, tokens).
  const [ctxOpen, setCtxOpen] = useState(false);
  // Renaming generated files: persisted map (convId::kind::titre → name).
  const [artifactRenames, setArtifactRenames] = useState<Record<string, string>>(loadArtifactRenames);
  const [renamingArtifact, setRenamingArtifact] = useState(false);
  const [renameArtifactValue, setRenameArtifactValue] = useState("");

  // Export (Markdown/JSON) of the loaded conversation.
  function exportConversation(conv: ExportConversation, fmt: "md" | "json") {
    const name = slugify(convTitleFallback(conv.messages, t("Conversation")));
    if (fmt === "json") downloadText(`${name}.json`, convAsJson(conv), "application/json");
    else downloadText(`${name}.md`, convAsMarkdown(conv, t), "text/markdown");
  }

  // Read-only share link: we create the snapshot then copy the URL.
  // Renaming a conversation (title derived from the 1st message by default).
  function startRename(id: string, title: string) {
    setRenamingId(id);
    setRenameValue(title || t("Conversation"));
  }
  function commitRename(id: string) {
    const title = (renameValue.trim().slice(0, 120) || t("Conversation"));
    setConversations((prev) => prev.map((c) => (c.id === id ? { ...c, title } : c)));
    const conv = conversations.find((c) => c.id === id);
    if (csrf && conv) void persistConversation(csrf, { ...conv, title });
    setRenamingId(null);
  }

  const showToast = useToast();

  async function shareConversation(id: string) {
    if (!csrf) {
      showToast({ body: t("Session incomplète — recharge la page."), type: "error" });
      return;
    }
    try {
      const res = await sendJSON<{ ok: boolean; token: string; error?: string }>("/conversations/share", csrf, { client_id: id });
      if (!res?.ok || !res.token) {
        showToast({ body: t("Partage impossible :") + " " + tServeur(res?.error || "erreur", t), type: "error" });
        return;
      }
      const url = `${window.location.origin}/c/${res.token}`;
      // navigator.clipboard requires a secure context (HTTPS): on the LAN over
      // HTTP it does not exist — we fall back to execCommand, and as a last
      // resort we DISPLAY the link rather than fail silently.
      try {
        await navigator.clipboard.writeText(url);
      } catch {
        const ta = document.createElement("textarea");
        ta.value = url;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand("copy");
        document.body.removeChild(ta);
        if (!ok) {
          window.prompt(t("Copiez le lien de partage :"), url);
          setShared(true);
          setTimeout(() => setShared(false), 2500);
          return;
        }
      }
      setShared(true);
      showToast({ body: t("Lien de partage copié (lecture seule, réservé aux connectés).") });
      setTimeout(() => setShared(false), 2500);
    } catch {
      showToast({ body: t("Partage impossible : le serveur n'a pas répondu."), type: "error" });
    }
  }

  /** Reads a TEXT file as an attachment (content inlined into the message).
   *
   *  Three explicit refusals, because all three were silent:
   *  a binary (image, PDF, archive) went to the model as mojibake; a
   *  too large file was announced by a blocking native `alert()`; and a
   *  read failure (`FileReader.onerror`, vanished file, disk) said
   *  nothing at all. The refusal is a message in the UI, not a console.
   */
  function handleFiles(files: FileList | null) {
    if (!files) return;
    let nbImages = attachments.filter((a) => a.image).length;
    for (const file of Array.from(files)) {
      const ext = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
      if (IMAGE_EXTENSIONS.includes(ext) || file.type.startsWith("image/")) {
        if (!modelVision[model]) {
          showToast({ body: t("« {name} » : ce modèle ne lit pas les images.").replace("{name}", file.name), type: "error" });
          continue;
        }
        if (nbImages >= IMAGES_PAR_MESSAGE) {
          showToast({ body: t("4 images au plus par message."), type: "error" });
          continue;
        }
        if (file.size > IMAGE_SOURCE_MAX_BYTES) {
          showToast({ body: t("« {name} » dépasse 25 Mo.").replace("{name}", file.name), type: "error" });
          continue;
        }
        nbImages++;
        preparerImage(file)
          .then((url) => setAttachments((prev) => [...prev, { name: file.name, content: "", image: url }]))
          .catch((e: unknown) => showToast({
            body: (e instanceof Error && e.message === "canevas"
              ? t("« {name} » est trop grande (plus de 2 Mo) et ce navigateur bloque sa réduction : réduis-la avant de la joindre.")
              : t("« {name} » n'a pas pu être lu.")).replace("{name}", file.name),
            type: "error",
          }));
        continue;
      }
      if (!ATTACH_EXTENSIONS.includes(ext)) {
        showToast({ body: t("« {name} » : seuls les fichiers texte sont acceptés.").replace("{name}", file.name), type: "error" });
        continue;
      }
      if (file.size > MAX_ATTACHMENT_BYTES) {
        showToast({ body: t("« {name} » dépasse 96 Ko — trop gros pour le contexte.").replace("{name}", file.name), type: "error" });
        continue;
      }
      const reader = new FileReader();
      reader.onload = () => {
        setAttachments((prev) => [...prev, { name: file.name, content: String(reader.result) }]);
      };
      reader.onerror = () => {
        showToast({ body: t("« {name} » n'a pas pu être lu.").replace("{name}", file.name), type: "error" });
      };
      reader.readAsText(file);
    }
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  // Drag-and-drop on the PAGE: without a handler, the browser navigates to the
  // dropped file and the thread disappears. We accept the drop
  // anywhere in the conversation. The ref avoids re-subscribing on every render.
  const handleFilesRef = useRef<(f: FileList | null) => void>(() => {});
  useEffect(() => { handleFilesRef.current = handleFiles; });
  useEffect(() => {
    function surDepot(e: DragEvent) {
      if (!e.dataTransfer?.files?.length) return;
      e.preventDefault();
      handleFilesRef.current(e.dataTransfer.files);
    }
    function surSurvol(e: DragEvent) {
      if (e.dataTransfer?.types?.includes("Files")) e.preventDefault();
    }
    window.addEventListener("dragover", surSurvol);
    window.addEventListener("drop", surDepot);
    return () => {
      window.removeEventListener("dragover", surSurvol);
      window.removeEventListener("drop", surDepot);
    };
  }, []);

  async function runStream(nextMessages: ChatMsg[]) {
    if (!model) {
      setMessages([...nextMessages, { role: "assistant", content: t("Aucun modèle actif.") }]);
      return;
    }
    setStreaming(true);
    setEtapesWeb([]);
    // New send: the reader wants to see the answer arrive, we rearm the tracking.
    descendreToutEnBas();   // on re-suit le bas quand on envoie
    // « quand un texte est lancé je veux garder la barre latérale, là il la fait
    // disparaitre »: a DOCUMENT draft shows up in the panel from the very first
    // word, the way writing a code file already does — before, `liveDocOpen`
    // only became true on a click on the chat card, and for the whole draft the
    // panel stayed absent (or stuck on the previous file). The state remains
    // that of the user's WILL: « Fermer » during the draft sets it back to false
    // and nothing reopens it, the « Ouvrir le document en cours de rédaction »
    // card sets it back to true. On phones there is no panel: the text then
    // scrolls in the chat.
    const redactionDoc =
      isDocTask(nextMessages[nextMessages.length - 1]?.content ?? "") && !isNarrow;
    setLiveDocOpen(redactionDoc);
    liveDocOpenRef.current = redactionDoc;
    const controller = new AbortController();
    abortRef.current = controller;
     
    const startTs = Date.now();
    const withPlaceholder = [...nextMessages, { role: "assistant", content: "", ts: startTs } as ChatMsg];
    setMessages(withPlaceholder);

     
    const t0 = performance.now();
    liveCharsRef.current = 0;
    liveStartRef.current = null;
    setLiveStats(null);
    let tf: number | null = null;
    let acc = "";
    let reason = "";
    let raisonDebut: number | null = null;   // 1er fragment de pensée
    let finPensee: number | null = null;     // 1er fragment de réponse
    let usage: { total_tokens?: number; completion_tokens?: number; prompt_tokens?: number } | undefined;

    const updateLast = () => {
      setMessages((prev) => {
        const copy = [...prev];
        copy[copy.length - 1] = { role: "assistant", content: acc, reasoning: reason, ts: copy[copy.length - 1]?.ts , reasoningMs: raisonDebut ? Math.round((finPensee ?? performance.now()) - raisonDebut) : undefined };
        return copy;
      });
    };

    let isError = false;
    let tronque = false;
    let wasAborted = false;
    try {
      // We remove the "asking questions" capability ONLY on the turn that
      // immediately follows answers — where the model would be tempted to chain
      // question upon question instead of answering.
      // Before, this test swept the WHOLE conversation (`some`): as soon as we had
      // answered once, the model could never ask for details again,
      // even much later on an unrelated request. Hence the impression
      // that it "charged ahead" while it was still asking questions at the start of
      // the conversation (measured: 16 vague requests out of 18 lead to a
      // question, and 0 out of 9 precise requests).
      const dernier = nextMessages[nextMessages.length - 1];
    // Is this turn a resume (« Continuer » button or automatic resume)?
    const enReprise = estReprise(dernier);
      const alreadyAsked = !!dernier?.hidden;
      const plafondModele = modelLimits[model];
      const askSettings = {
        ...settings,
        // The output ceiling cannot exceed the loaded model's window.
        // The backend already lowers it to what remains; here we avoid sending a
        // value that makes sense for no current model.
        maxTokens: plafondModele ? Math.min(settings.maxTokens, plafondModele) : settings.maxTokens,
        // Order and VOLUME matter: measured on 12 vague requests, the model
        // asks a question 11 times out of 12 with the questions instruction alone,
        // 7 times out of 12 when adding naming and editing, and 3 times out of 12 if
        // the questions instruction goes last. So we keep the questions
        // AT THE TOP, a naming reduced to one sentence, and the edit instruction
        // only when a file already exists — before that, it is useless.
        // On a RESUME turn, neither questions nor edit protocol: the model
        // had precisely answered with a truncated ```edit block instead of finishing
        // the file, leaving the user with nothing.
        system: (enReprise
          ? [settings.system.trim(), REPRISE_INSTRUCTION]
          : [
              settings.system.trim(),
              alreadyAsked ? "" : ASK_INSTRUCTION,
              NAME_INSTRUCTION,
              fichiersJusqua(nextMessages, nextMessages.length - 1, t).size ? REWRITE_INSTRUCTION : "",
              INTEGRALITE_INSTRUCTION,
            ]
        ).filter(Boolean).join("\n\n"),
      };
      await streamChat(
        csrf,
        model,
        imagesRecentes(nextMessages, IMAGES_MAX_REQUETE).map((m) => ({ role: m.role, content: m.content, images: m.images })),
        askSettings,
        controller.signal,
        (delta) => {
          if (delta.usage) usage = delta.usage;
          if (delta.truncated) tronque = true;
          if (delta.notice) {
            // System notice (quota exceeded…): translated HERE according to the
            // interface language, not server-side.
            acc += (acc ? "\n\n" : "") + texteNotice(delta.notice, t);
            updateLast();
          }
          if (delta.webStep) {
            const e = delta.webStep;
            // A "finished" step replaces its announcement, it is not added.
            // The portail can send an EMPTY frame ({}) when a tool refused
            // before doing anything: no step, therefore nothing to replace —
            // without this guard `etape.endsWith` threw and the answer was
            // lost as a network error.
            setEtapesWeb((prec) => {
              const base = (e.etape ?? "").endsWith("_finie") ? prec.slice(0, -1) : prec;
              return [...base, e].slice(-6);
            });
          }
          if (delta.reasoningChunk) {
            if (tf === null) tf = performance.now();
            if (raisonDebut === null) raisonDebut = performance.now();
            if (liveStartRef.current === null) liveStartRef.current = tf;
            liveCharsRef.current += delta.reasoningChunk.length;
            reason += delta.reasoningChunk;
            updateLast();
          }
          if (delta.contentChunk) {
            if (tf === null) tf = performance.now();
            if (finPensee === null) finPensee = performance.now();
            if (liveStartRef.current === null) liveStartRef.current = tf;
            liveCharsRef.current += delta.contentChunk.length;
            acc += delta.contentChunk;
            updateLast();
          }
        },
      );
    } catch (e) {
      if ((e as Error)?.name === "AbortError") {
        wasAborted = true; // Deliberate stop (Stop button): not an error.
      } else {
        isError = true;
        if (!acc) acc = t("Erreur réseau.");
      }
    }
    if (!isError && !wasAborted && !acc && !reason) {
      isError = true;
      acc = t("Le modèle n'a renvoyé aucune réponse.");
    }

     
    const te = performance.now();
    const finalMessages = [...nextMessages];
    if (acc || reason) {
      const gen = tf ? (te - tf) / 1000 : 0;
      const tokens = usage?.completion_tokens;
      // Auto-calibration: the true token count is known here, we derive the
      // real characters/token ratio of this model/language so the estimate
      // of the NEXT generation is right. Bounded so that a degenerate
      // answer (1 token, 500 characters) does not skew the display for good.
      const producedChars = acc.length + reason.length;
      if (tokens && producedChars > 0) {
        charsPerTokenRef.current = Math.min(12, Math.max(1.5, producedChars / tokens));
      }
      finalMessages.push({
        role: "assistant",
        content: trimAfterAsk(acc),
        truncated: tronque,
        reasoning: reason,
        reasoningMs: raisonDebut ? Math.round((finPensee ?? te) - raisonDebut) : undefined,
        tokens,
        tokensPerSec: tokens && gen > 0 ? Number((tokens / gen).toFixed(1)) : undefined,
        ttft: tf ? Number(((tf - t0) / 1000).toFixed(2)) : undefined,
        ts: startTs,
        isError,
      });
    }
    // Did the THREAD change during the generation? Loading another conversation
    // from the history (or switching tab) is not blocked — it is
    // legitimate — but the end of the stream then rewrote `messages` AND `currentId`
    // with the values captured at START: we brutally jumped back to the old
    // conversation, the partial answer added on top, and the active tab
    // kept the other one's title. We always save the answer in the right
    // place (no loss), we only repaint the screen if we are still there.
    const memeFil = currentIdRef.current === currentId;
    if (memeFil) setMessages(finalMessages);
    // If the assistant wrote a file, or wrote a document in reply to a document
    // task, surface the last one in the side panel automatically.
    const lastUser = [...nextMessages].reverse().find((mm) => mm.role === "user");
    // A clarifying question is never a document/file artifact — leave the panel closed.
    const produced = parseAsk(acc)
      ? []
      : parseArtifacts(contenuCloture(acc), isDocTask(lastUser?.content ?? ""), t).artifacts;
    if (produced.length && memeFil) {
      const lastArt = produced[produced.length - 1];
      // A code file opens on its own; a document opens automatically only if the
      // user was already watching it being written live (otherwise it stays a
      // card in the chat that they can click open).
      if (lastArt.kind === "code" || liveDocOpenRef.current) setArtifact(lastArt);
    }
    liveDocOpenRef.current = false;
    const total = usage?.total_tokens;
    if (total && memeFil) {
      setCtxUsed(total);
      setConvTokens((p) => p + total);
    }
    if (memeFil && (typeof usage?.prompt_tokens === "number" || typeof usage?.completion_tokens === "number")) {
      setIoTokens({ prompt: usage?.prompt_tokens ?? 0, completion: usage?.completion_tokens ?? 0 });
    }
    const savedId = persist(finalMessages, currentId, model);
    if (memeFil) {
      setCurrentId(savedId ?? null);
      // The active tab immediately carries this conversation (even before
      // the auto-title answers), so that closing/switching stays consistent.
      setTabs((prev) => prev.map((t) => (t.id === activeTabId ? { ...t, currentId: savedId ?? null } : t)));
    }
    // (Auto-title on close only — we analyze the full conversation
    // for a reliable history title; see closeTab.)
    setStreaming(false);
    setLiveStats(null);
    abortRef.current = null;
    // The model dropped mid-file: we chain on our own. Seen in
    // production — a large HTML file stopped mid-expression without
    // anything indicating it, and everything had to be asked again. The token
    // ceiling had nothing to do with it: the model hands back too early.
    // `isError` does NOT rule out a resume: the most frequent cut is precisely
    // an error — the stream breaks along the way on a generation of several
    // minutes, and the text already received stops mid-word. This is exactly the
    // case where resuming on our own is most valuable. Only a REQUESTED stop (Stop
    // button) forbids the resume.
    // Aborted turn (announcement without the follow-up): we REDO the turn instead of
    // resuming it — there is nothing to extend, the answer never started.
    if (!wasAborted && tourAvorte(finalMessages[finalMessages.length - 1])) {
      if (reprisesRef.current < MAX_REPRISES_AUTO) {
        reprisesRef.current += 1;
        setReprise(reprisesRef.current);
        void runStream(nextMessages);
        return;
      }
    }
    // `messageIncomplet` below already evaluates `fichierLaisseOuvert`: we keep
    // its result instead of recomputing it two lines below (each call
    // re-reads the whole message, resumes included).
    const dernierIdx = finalMessages.length - 1;
    const laisseOuvert = !wasAborted
      && fichierLaisseOuvert(finalMessages, dernierIdx, t);
    if (!wasAborted
        && (laisseOuvert || abandonDeclare(finalMessages[dernierIdx]?.content ?? ""))) {
      if (reprisesRef.current < MAX_REPRISES_AUTO) {
        reprisesRef.current += 1;
        setReprise(reprisesRef.current);
        // File left OPEN: we extend it to the next character. File
        // CLOSED but abbreviated by the model's own admission: extending it would
        // produce content after the last line of an already closed file — what is
        // needed is a full rewrite request.
        const suite = laisseOuvert ? PROMPT_REPRISE_COMPLET : PROMPT_INTEGRAL;
        void runStream([...finalMessages, {
          role: "user", content: suite,
           
          ts: Date.now(), hidden: true,
        }]);
        return;
      }
      // Too many resumes in a row: the banner and the button take over.
    }
    reprisesRef.current = 0;
    setReprise(0);
    // Messages were queued during this generation: they go out
    // now, with no manual validation. Explicit base (finalMessages):
    // messagesRef is not yet resynchronized in this same tick.
    if (queuedRef.current.length) dispatchQueued(finalMessages);
  }

  function send(value: string) {
    const text = value.trim();
    if (!text && !attachments.length) return;
    // The key may meanwhile have been created from the settings box: we
    // re-check here rather than leave the banner (and the failure) lingering.
    // One extra request, and only in the broken state.
    if (hasKey === false) {
      void fetchPlaygroundData().then((d) => setHasKey(d.has_key)).catch(() => {});
    }
    // Sending ends dictation: the message goes out with what has been
    // transcribed so far, and no still-in-flight pass will rewrite the
    // field once it's been cleared.
    dictation.cancel();
    const fichiers = attachments.filter((f) => !f.image);
    let images: string[] | undefined = attachments.flatMap((f) => (f.image ? [f.image] : []));
    // The model may have changed since the attachment: we do not send images
    // it would not read (the server would drop them anyway).
    if (images.length && !modelVision[model]) {
      showToast({ body: t("Ce modèle ne lit pas les images : elles n'ont pas été envoyées."), type: "error" });
      images = [];
    }
    if (!images.length) images = undefined;
    if (!text && !fichiers.length && !images) return;
    let full = text;
    if (fichiers.length) {
      full =
        (text ? text + "\n\n" : "") +
        fichiers.map((f) => "```" + f.name + "\n" + f.content + "\n```").join("\n\n");
    }
    const attachmentCount = fichiers.length || undefined;
    // Mid-generation, the message goes to the queue (validated by the
    // « Envoyer » button of its bubble) instead of being silently lost.
    if (streaming) {
       
      updateQueue([...queuedRef.current, { content: full, text, attachmentCount, images, ts: Date.now() }]);
      setInput("");
      setAttachments([]);
      return;
    }
    const nextMessages: ChatMsg[] = [
      // In inline edit: we rebranch from the edited message — we keep
      // everything that precedes it, we replace this message + the rest with the
      // new version.
      ...(editingIdx !== null ? messages.slice(0, editingIdx) : messages),
       
      { role: "user", content: full, ts: Date.now(), attachmentCount, images },
    ];
    setMessages(nextMessages);
    setInput("");
    setAttachments([]);
    setEditingIdx(null);
    void runStream(nextMessages);
  }

  // « Envoyer » on a row: do not wait for the current answer to end.
  // We move this message to the head of the queue then interrupt the generation —
  // its start is kept, exactly as with Stop. The send itself is done by
  // the end of runStream (the abort is asynchronous: reading state here would
  // lose the partial answer).
  function sendQueuedNow(idx: number) {
    const q = queuedRef.current;
    if (!q[idx]) return;
    updateQueue([q[idx], ...q.filter((_, i) => i !== idx)]);
    if (streaming) {
      abortRef.current?.abort();
      return;
    }
    dispatchQueued();
  }

  // « Modifier »: the message comes out of the queue and goes back into the composer.
  // We put back `content` (not the raw text) so as not to silently lose the
  // attached files that were inlined into it.
  function editQueued(idx: number) {
    const m = queuedRef.current[idx];
    if (!m) return;
    updateQueue(queuedRef.current.filter((_, i) => i !== idx));
    setInput(m.content);
  }

  function dispatchQueued(base?: ChatMsg[]) {
    const q = queuedRef.current;
    if (!q.length) return;
    updateQueue([]);
    const msgs: ChatMsg[] = q.map((m) => ({ role: "user", content: m.content, ts: m.ts, attachmentCount: m.attachmentCount, images: m.images }));
    const nextMessages = [...(base ?? messagesRef.current), ...msgs];
    setMessages(nextMessages);
    void runStream(nextMessages);
  }

  function discardQueued(idx: number) {
    updateQueue(queuedRef.current.filter((_, i) => i !== idx));
  }

  // Answer a clarifying question the model asked (clicking an option, or the
  // free-text "Other"): send the chosen answer as a user message and continue.
  function answer(text: string) {
    if (streaming) return;
    const t2 = text.trim();
    if (!t2) return;
    // `hidden`: the answers go to the model but are not shown in the chat — the
    // user's choices already live in the (now locked) question card.
     
    const nextMessages: ChatMsg[] = [...messages, { role: "user", content: t2, ts: Date.now(), hidden: true }];
    setMessages(nextMessages);
    void runStream(nextMessages);
  }

  /** Asks again for the WHOLE file when the model only returned a snippet. */
  function demanderFichierComplet(nom: string) {
    if (streaming) return;
    const nextMessages: ChatMsg[] = [
      ...messages,
      { role: "user",
        content: `Renvoie le fichier ${nom} EN ENTIER, du début à la fin, `
          + "avec la correction intégrée. Un seul bloc de code, aucun extrait, "
          + "aucune ligne omise, pas de « ... » ni de commentaire du type "
          + "« reste inchangé ».",
         
        ts: Date.now(), hidden: true },
    ];
    setMessages(nextMessages);
    void runStream(nextMessages);
  }

  /** Resumes an answer cut by the token ceiling, without redoing it. */
  function continuer() {
    if (streaming || !messages.length) return;
    const nextMessages: ChatMsg[] = [
      ...messages,
      { role: "user",
        content: PROMPT_REPRISE_COMPLET,
         
        ts: Date.now(), hidden: true },
    ];
    setMessages(nextMessages);
    void runStream(nextMessages);
  }

  /** The delivered file does not run: we ask again for a complete version.
   *
   * Definitely NOT a resume: the cut is not at the end, it is in the middle
   * (a never-closed block). Adding text after it would change nothing.
   */
  function refaireFichier(nom: string) {
    if (streaming || !messages.length) return;
    const nextMessages: ChatMsg[] = [
      ...messages,
      { role: "user",
        content: `Le fichier \`${nom}\` ne fonctionne pas : son JavaScript ne compile pas `
          + "(« Unexpected end of input » — un bloc n'est jamais refermé), donc la page reste "
          + "vide. Renvoie le fichier COMPLET et corrigé, en entier, du début à la fin, "
          + "sous le même nom. Vérifie que chaque accolade et chaque parenthèse est refermée.",
         
        ts: Date.now(), hidden: true },
    ];
    setMessages(nextMessages);
    void runStream(nextMessages);
  }

  /** The preview raised an error: we ask again for the fixed file, in full. */
  function corrigerErreur(nom: string, message: string) {
    if (streaming || !messages.length) return;
    const nextMessages: ChatMsg[] = [
      ...messages,
      { role: "user",
        content: `Le fichier \`${nom}\` s'ouvre mais ne fonctionne pas. Le navigateur `
          + `signale : « ${message} ». Corrige la cause exacte de cette erreur `
          + "(vérifie notamment que les fonctions appelées existent bien dans la version "
          + "de bibliothèque que tu utilises) et renvoie le fichier COMPLET corrigé, "
          + "en entier, sous le même nom.",
         
        ts: Date.now(), hidden: true },
    ];
    setMessages(nextMessages);
    void runStream(nextMessages);
  }

  function stop() {
    abortRef.current?.abort();
  }

  function regenerate() {
    if (streaming || !messages.length) return;
    const last = messages[messages.length - 1];
    const base = last.role === "assistant" ? messages.slice(0, -1) : messages;
    if (base.length && base[base.length - 1].role === "user") void runStream(base);
  }

  // Editing a PAST message, in place: the conversation stays displayed, the
  // content goes back into the input, and sending will rebranch from that point
  // (the edited message + the rest are replaced, not truncated on display).
  function editMessage(i: number) {
    if (streaming) return;
    const m = messages[i];
    if (!m || m.role !== "user") return;
    setEditingIdx(i);
    setInput(m.content);
    setAttachments((m.images ?? []).map((u, k) => ({ name: `image-${k + 1}.jpg`, content: "", image: u })));
  }


  const max = modelLimits[model] || 32768;
  // A single character sum and a single estimate per render:
  // `estimateTokens` was called TWICE with the same arguments, and the
  // breakdown below redid the same sum a third time.
  let contentChars = input.length;
  for (const m of messages) contentChars += m.content.length;
  for (const a of attachments) contentChars += a.image ? IMAGE_POIDS_CHARS : a.content.length;
  for (const m of messages) contentChars += (m.images?.length ?? 0) * IMAGE_POIDS_CHARS;
  const estTokens = Math.round((settings.system.length + contentChars) / 4);
  // Input / output: the window fills with INPUT tokens (prompt: system
  // + history + message + files) and OUTPUT tokens (the generated
  // answer). Exact measurement of the last generation (LiteLLM usage) when it
  // exists; otherwise a chars/4 estimate of the current input.
  const used = Math.max(ctxUsed, estTokens);
  const inTokens = ioTokens?.prompt ?? estTokens;
  const outTokens = ioTokens?.completion ?? 0;
  // Content breakdown for « Fenêtre de contexte »: system prompt vs
  // messages/files (same chars/4 estimate). The backend does not provide the
  // per-segment count, so we estimate the relative share — faithful enough to
  // visualize what occupies the window.
  const systemTokens = Math.round(settings.system.length / 4);
  const contentTokens = Math.round(contentChars / 4);
  const totalEstimate = Math.max(1, systemTokens + contentTokens);
  const sysPct = Math.round((systemTokens / totalEstimate) * 100);
  const msgPct = 100 - sysPct;
  const ctxLevel: "accent" | "warning" | "error" =
    used / max >= 0.95 ? "error" : used / max >= 0.8 ? "warning" : "accent";

  // History list: search by title + pinned first, then recent.
  const q = histQuery.trim().toLowerCase();
  // `filter().sort()` over all conversations was redone on EVERY render,
  // hence on every token received, while the list is only read in the
  // « Historique » box: we only compute it when it is open.
  const visibleConvs = useMemo(
    () => (historyOpen
      ? conversations
          .filter((c) => !q || (c.title || "").toLowerCase().includes(q))
          .sort((a, b) => {
            const pa = pinnedIds.includes(a.id) ? 0 : 1;
            const pb = pinnedIds.includes(b.id) ? 0 : 1;
            return pa - pb || ((b.ts ?? 0) - (a.ts ?? 0));
          })
      : []),
    [historyOpen, conversations, q, pinnedIds],
  );
  const lastMsg = messages[messages.length - 1];
  // While a document is being streamed, the chat shows a card (not the raw text)
  // and the side panel can show a live view of the document being written.
  const streamingDocActive =
    streaming && lastMsg?.role === "assistant" && isDocTask(messages[messages.length - 2]?.content ?? "");
  const liveContent = streamingDocActive ? (lastMsg?.content ?? "") : "";
  // Code file being written: it displays DIRECTLY in the panel,
  // not in the chat. On a narrow screen there is no panel — we then let it
  // scroll in the chat, otherwise the user would see nothing being written.
  const liveCode =
    streaming && !isNarrow && lastMsg?.role === "assistant" && !streamingDocActive
      ? openCodeFence(lastMsg.content ?? "")
      : null;
  const liveCodeTitle = liveCode
    ? titleFromContext((lastMsg?.content ?? "").slice(0, liveCode.start)) || `${liveCode.lang}`
    : "";
  const showLiveDoc = liveDocOpen && streamingDocActive;
  // Between two files (previous block closed, next not started), the panel
  // keeps the last finished file instead of emptying. Derived from content, no
  // state: nothing to synchronize, hence nothing to desynchronize.
  const dernierFini =
    streaming && !isNarrow && lastMsg?.role === "assistant" && !streamingDocActive
      ? (parseArtifacts(lastMsg.content ?? "", false, t).artifacts.slice(-1)[0] ?? null)
      : null;
  const epingle = liveCode ? null : (dernierFini ?? artifact);
  const showLive = showLiveDoc || !!liveCode;
  // Unified panel values: file being written, last finished file, live document,
  // else the pinned artifact.
  const panelIsCode = !!liveCode || epingle?.kind === "code";
  const panelTitle = liveCode
    ? liveCodeTitle
    : showLiveDoc
      ? docTitleFromContent(liveContent)
      : (epingle ? renamedTitle(epingle) : "Document");
  const canRenamePanel = !!epingle && !liveCode && !showLiveDoc;
  const panelContent = liveCode
    ? liveCode.body
    : showLiveDoc
      ? liveContent
      : (epingle?.content ?? "");
  const panelSubtitle = liveCode
    ? t("Écriture en cours…")
    : showLiveDoc
      ? t("Rédaction en cours…")
      : (panelIsCode && epingle?.kind === "code" ? epingle.lang : "");
  const panelLang = liveCode ? liveCode.lang : (epingle?.kind === "code" ? epingle.lang : "text");
  // A finished HTML page can be WATCHED, not only read as code. We do not
  // offer it while it is being written: a half-written render flickers.
  const panelEstHtml = !liveCode && panelIsCode && /^html?$/i.test(panelLang);
  const panelDownloadName = panelIsCode
    ? nomTelechargeable(panelTitle, panelLang)
    : `${slugify(panelTitle)}.md`;
  const panelDownloadMime = panelIsCode ? mimePourLangage(panelLang) : "text/markdown";
  // Publishes the page to preview as soon as its content changes. External write
  // (network) followed by a state update AFTER the await: no cascading
  // render. Nothing is published while no preview is being watched.
  useEffect(() => {
    let annule = false;
    if (!panelEstHtml || !htmlPreview || !panelContent) {
      // Deferred reset: updating state in the BODY of the effect
      // would trigger a cascading render.
      void Promise.resolve().then(() => { if (!annule) setPreviewUrl(""); });
      return () => { annule = true; };
    }
    void sendJSON<{ ok: boolean; id?: string }>("/playground/preview", csrf, { html: panelContent })
      .then((r) => {
        if (annule || !r.ok || !r.id) return;
        setErreurApercu("");            // new page: we start from a clean state
        setPreviewUrl(`/playground/preview/${r.id}`);
      })
      .catch(() => {});
    return () => { annule = true; };
  }, [panelEstHtml, htmlPreview, panelContent, csrf]);

  // The preview reports its runtime errors. A page can be perfectly
  // well-formed and do nothing — wrong library version, non-existent
  // method — and no text analysis can see it. Only execution
  // says so, and the preview is what executes.
  // Legitimate preview windows. The iframe has an OPAQUE origin (sandbox without
  // allow-same-origin), so `e.origin` is « null » and proves nothing: without
  // checking `e.source`, any window holding a reference to
  // this page (frame, `window.opener`) could inject a fake error
  // message — displayed in a Banner, then sent back to the model via « Corriger ».
  // A WeakSet covers BOTH preview iframes (they are not necessarily
  // mounted exclusively depending on the layout) and retains nothing.
  const apercuWindows = useRef(new WeakSet<Window>());
  const refApercu = useCallback((el: HTMLIFrameElement | null) => {
    const w = el?.contentWindow;
    if (w) apercuWindows.current.add(w);
  }, []);

  useEffect(() => {
    function surMessage(e: MessageEvent) {
      if (!e.source || !apercuWindows.current.has(e.source as Window)) return;
      const d = e.data as { cronosPreviewError?: unknown } | null;
      const msg = d && typeof d.cronosPreviewError === "string" ? d.cronosPreviewError : null;
      if (!msg) return;
      // Content produced by the generated page: never interpreted, only displayed.
      setErreurApercu((prec) => prec || msg.slice(0, 300));
    }
    window.addEventListener("message", surMessage);
    return () => window.removeEventListener("message", surMessage);
  }, []);
  // Auto-follow the document while it streams into the panel; show a "jump to
  // bottom" button when the reader scrolls up and leaves the live tail.
  const {
    setRef: panelScrollRef,
    showButton: showPanelJump,
    onScroll: onPanelScroll,
    scrollToBottom: panelJumpDown,
  } = useStickToBottom(panelContent, showLive);
  // Time-of-day greeting for the first message (Claude-style). « soir » from 18h.
  const playHour = new Date().getHours();
  const greeting =
    playHour >= 18 || playHour < 5
      ? t("Bonsoir, comment allez-vous ?")
      : t("Bonjour, comment allez-vous ?");
  // First message: we center the composer with the greeting above.
  const isFirstEmpty = messages.length === 0 && !isSettingsOpen && !streaming;

  // Rotating placeholder: a few texts that rotate (including the « / » trick
  // to call a skill). The timer is independent of rendering: it simply
  // advances the index in PLACEHOLDER_TEXTS.
  const [placeholderIdx, setPlaceholderIdx] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setPlaceholderIdx((i) => (i + 1) % PLACEHOLDER_TEXTS.length), 5000);
    return () => clearInterval(id);
  }, []);
  const placeholderText = t(PLACEHOLDER_TEXTS[placeholderIdx] ?? PLACEHOLDER_TEXTS[0]);

  // « /compétence » mode: as soon as the input starts with a « / », we show
  // the skills menu. The text after « / » acts as a filter.
  const slashQuery = input.startsWith("/") ? input.slice(1).trim().toLowerCase() : null;

  // Special « / » commands: /skill-creator (or /create, /new) opens the
  // skill creator and clears the input. Detected on typing (onChange),
  // not in an effect, to respect the react-hooks/set-state-in-effect rule.
  function handleInput(v: string) {
    const cmd = v.startsWith("/") ? v.slice(1).trim().toLowerCase() : null;
    if (cmd && SLASH_CREATE_COMMANDS.includes(cmd)) {
      openSkillCreator();
      return;
    }
    setInput(v);
  }

  // Filtered skills (for keyboard navigation + the menu): base then created.
  const baseHits = useMemo(
    () => (slashQuery !== null ? skills.filter((s) => s.builtin && skillMatches(s, slashQuery)) : []),
    [skills, slashQuery],
  );
  const customHits = useMemo(
    () => (slashQuery !== null ? skills.filter((s) => !s.builtin && skillMatches(s, slashQuery)) : []),
    [skills, slashQuery],
  );
  // Row 0 = « créer » card, rows 1..n = skills (base then created).
  const skillHits = useMemo(() => [...baseHits, ...customHits], [baseHits, customHits]);
  const menuRows = 1 + skillHits.length;

  // Highlighted row in the menu (0 = « Créer un skill », 1..n = skills).
  const [skillSel, setSkillSel] = useState(1);
  useEffect(() => {
    // We move the highlight back to the first skill on every filter
    // change (sync from the input state, legitimate case allowed by the lint).
    if (slashQuery !== null) {
      /* eslint-disable react-hooks/set-state-in-effect */
      setSkillSel(1);
      /* eslint-enable react-hooks/set-state-in-effect */
    }
  }, [slashQuery]);

  // Keyboard navigation handler, kept up to date on every render via a ref:
  // the listener effect only depends on the menu being open, with no unstable
  // dependencies (recreated functions / arrays), hence 0 exhaustive-deps warnings.
  const onMenuKeyRef = useRef<(e: KeyboardEvent) => void>(() => {});
  useEffect(() => {
    onMenuKeyRef.current = (e: KeyboardEvent) => {
      // No skill matches the filter: this is not a command,
      // it is a message that STARTS with « / » (a file path, a
      // command from another tool…). The menu is then just a « créer un
      // skill » row, and Enter opened the creator WHILE CLEARING THE INPUT: the
      // written message was destroyed without a word. We no longer touch the
      // keyboard in this case — Enter sends, as for any message.
      if (!skillHits.length) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        e.stopPropagation();
        setSkillSel((s) => Math.min(s + 1, menuRows - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        e.stopPropagation();
        setSkillSel((s) => Math.max(s - 1, 0));
      } else if (e.key === "Escape") {
        // Escape NEVER clears the input: it used to "close" the menu, but
        // the menu has no state of its own (it follows the input text), so the
        // key erased everything that was typed. We leave it to the composer.
        e.stopPropagation();
      } else if (e.key === "Enter") {
        const sel = Math.min(skillSel, menuRows - 1);
        if (sel === 0) {
          // The « créer un skill » row is only reachable by keyboard with ↑/↓
          // when skills exist: we no longer open it by accident.
          e.preventDefault();
          e.stopPropagation();
          openSkillCreator();
        } else {
          const s = skillHits[sel - 1];
          if (!s) return;
          e.preventDefault();
          e.stopPropagation();
          selectSkill(s);
        }
      }
    };
  });

  useEffect(() => {
    if (slashQuery === null) return;
    function onMenuKey(e: KeyboardEvent) {
      onMenuKeyRef.current(e);
    }
    // CAPTURE-PHASE listener on the window to beat the input (otherwise
    // ↑/↓ re-reads the history and Enter sends the message).
    window.addEventListener("keydown", onMenuKey, true);
    return () => window.removeEventListener("keydown", onMenuKey, true);
  }, [slashQuery]);
  const effectiveSel = Math.min(skillSel, menuRows - 1);

  // « composer » node reused both in the bottom-anchored layout (ongoing
  // conversation) and centered on the first message. Bottom bar: Attacher (left),
  // model selector + mic/send button (right).
  const composerNode = (
    <PlaygroundComposer
      input={input}
      handleInput={handleInput}
      send={send}
      stop={stop}
      streaming={streaming}
      editingIdx={editingIdx}
      setEditingIdx={setEditingIdx}
      setInput={setInput}
      hasKey={hasKey}
      openSettings={openSettings}
      queued={queued}
      editQueued={editQueued}
      sendQueuedNow={sendQueuedNow}
      discardQueued={discardQueued}
      mode={mode}
      dictation={dictation}
      rayonComposeur={rayonComposeur}
      placeholderText={placeholderText}
      attachments={attachments}
      setAttachments={setAttachments}
      handleFiles={handleFiles}
      fileInputRef={fileInputRef}
      fileAccept={modelVision[model] ? `${ATTACH_ACCEPT},${IMAGE_ACCEPT}` : ATTACH_ACCEPT}
      modelVision={modelVision}
      model={model}
      setModel={setModel}
      runningModels={runningModels}
      slashQuery={slashQuery}
      baseHits={baseHits}
      customHits={customHits}
      effectiveSel={effectiveSel}
      selectSkill={selectSkill}
      openSkillCreator={openSkillCreator}
      deleteCustomSkill={deleteCustomSkill}
      imageVue={imageVue}
      setImageVue={setImageVue}
      isNarrow={isNarrow}
      setSnippetsOpen={setSnippetsOpen}
      setSkillCreatorOpen={setSkillCreatorOpen}
      setCtxOpen={setCtxOpen}
      used={used}
      max={max}
      hasMessages={messages.length > 0}
    />
  );

  // Everything the two document-panel layouts need, gathered in one plain
  // object (same values as before the extraction — see the derivations above).
  const panelProps: DocumentPanelProps = {
    panelIsCode,
    panelTitle,
    panelSubtitle,
    panelContent,
    panelLang,
    panelEstHtml,
    panelDownloadName,
    panelDownloadMime,
    showLive,
    canRenamePanel,
    renamingArtifact,
    renameArtifactValue,
    setRenamingArtifact,
    setRenameArtifactValue,
    epingle,
    commitArtifactRename,
    htmlPreview,
    setHtmlPreview,
    previewUrl,
    erreurApercu,
    streaming,
    corrigerErreur,
    refApercu,
    panelScrollRef,
    onPanelScroll,
    showPanelJump,
    panelJumpDown,
    onClose: () => {
      setArtifact(null);
      setLiveDocOpen(false);
      liveDocOpenRef.current = false;
    },
    artifactResize,
    setPlein,
    fermerPanneau,
  };

  return (
    <Layout
      height="fill"
      padding={6}
      header={
        <LayoutHeader hasDivider>
          <HStack hAlign="between" vAlign="center" wrap="wrap" gap={3}>
            <VStack gap={0}>
              <Heading level={2}>{t("Playground")}</Heading>
              <Text type="supporting" color="secondary">{t("Discute en direct avec un modèle actif — réglages avancés, fichiers joints, réponses en streaming, sur ton budget de compte.")}</Text>
            </VStack>
            <PlaygroundActions
              streaming={streaming}
              newConversation={newConversation}
              setHistoryOpen={setHistoryOpen}
              exportConversation={exportConversation}
              messages={messages}
              model={model}
              shared={shared}
              currentId={currentId}
              shareConversation={shareConversation}
              toggleSettings={toggleSettings}
              busyTitle={busyTitle}
              genTitle={genTitle}
              genSummary={genSummary}
              setCtxOpen={setCtxOpen}
            />
          </HStack>
        </LayoutHeader>
      }
      content={
        <LayoutContent padding={0} isScrollable={false}>
          {/* VStack (flex column) gives ChatLayout the flex parent its own flex:1
              needs to fill the remaining height — LayoutContent renders display:block,
              so without this wrapper ChatLayout's flex:1 is a no-op.
              No scrollRef → ChatLayout is self-scrolling: its OWN root becomes the
              overflow:auto container and its dock uses position:sticky. Sticky is
              what actually keeps the composer pinned to the bottom during scroll —
              fixed-via-transform (tried earlier) turns out to behave like absolute
              positioning for descendants inside the same scrolling flow, so it drifts
              upward as the container scrolls (confirmed by instrumenting the DOM
              mid-stream). Since ChatLayout's own root is now full width (no
              `contentWidth` on the outer Layout — that's what narrowed it before and
              pushed the scrollbar to the middle of the page), its native scrollbar
              lands at the true right edge; density="spacious" narrows just the
              message column and composer to a shared 800px reading width. */}
          <HStack height="100%">
          <StackItem size="fill">
          <VStack height="100%">
          <ThreadTabs
            tabs={tabs}
            activeTabId={activeTabId}
            streaming={streaming}
            switchTab={switchTab}
            closeTab={closeTab}
            newTab={newTab}
          />
          {isFirstEmpty ? (
            <StackItem size="fill">
              <VStack width="100%" height="100%" vAlign="center" hAlign="center" gap={4} padding={4}>
                <HStack gap={2} vAlign="center">
                  <Icon icon={SparklesIcon} size="lg" color="accent" />
                  <Text type="display-2" as="h1">{greeting}</Text>
                </HStack>
                <HStack gap={4} width="100%" hAlign="center">
                  <VStack maxWidth={720} width="100%">{composerNode}</VStack>
                </HStack>
              </VStack>
            </StackItem>
          ) : (
          <ChatLayout
            density="spacious"
            composer={composerNode}
            ref={suitLeBasSetRef}
            scrollButton={
              montrerDescendre ? (
                <Button
                  label={t("Descendre")}
                  variant="secondary"
                  size="sm"
                  onClick={descendreToutEnBas}
                />
              ) : null
            }>
            <ChatMessageList
              emptyState={
                <VStack gap={2} hAlign="center">
                  <Text type="display-2" as="h1">{greeting}</Text>
                  <Text type="supporting" color="secondary">{t("Écris ton message ci-dessous pour commencer.")}</Text>
                </VStack>
              }>
                {messages.map((m, i) => (
                  <ThreadMessage
                    key={i}
                    m={m}
                    i={i}
                    messages={messages}
                    streaming={streaming}
                    liveCode={liveCode}
                    isNarrow={isNarrow}
                    etapesWeb={etapesWeb}
                    editingIdx={editingIdx}
                    liveStats={liveStats}
                    reprise={reprise}
                    model={model}
                    renamedTitle={renamedTitle}
                    onAnswer={answer}
                    onDemanderFichierComplet={demanderFichierComplet}
                    onRefaireFichier={refaireFichier}
                    onContinuer={continuer}
                    onRegenerate={regenerate}
                    onEditMessage={editMessage}
                    onOpenImage={setImageVue}
                    onOpenArtifact={(a) => {
                      setArtifact(a);
                      setRenamingArtifact(false);
                    }}
                    onOpenLiveDoc={openLiveDoc}
                  />
                ))}
            </ChatMessageList>
          </ChatLayout>
          )}
          </VStack>
          </StackItem>
          {(artifact || showLive || dernierFini) && !isNarrow && (
            <DocumentPanel {...panelProps} />
          )}
          {/* Historique : UNE ligne par conversation, corbeille au bout. Cliquer la
              ligne ouvre la conversation (et referme le panneau) ; cliquer la
              corbeille supprime SANS refermer, pour pouvoir faire le ménage
              d'affilée. La corbeille est un bouton FRÈRE de la ligne, pas un
              bouton imbriqué : aucun risque qu'un clic déclenche les deux. */}
          {/* Réglages du playground : overlay FIXE ancré sous la roue crantée.
              Pas de focus trap, pas d'élément caché à révéler : ouvrir ne
              déclenche AUCUN défilement — le chat reste exactement où il est. */}
          {isSettingsOpen && settingsPos && (
            <SettingsOverlay
              settingsPos={settingsPos}
              setIsSettingsOpen={setIsSettingsOpen}
              settings={settings}
              setSettings={setSettings}
              contexte={modelLimits[model]}
              systemProvenance={systemProvenance}
              setSystemProvenance={setSystemProvenance}
            />
          )}
          <HistoryDialog
            historyOpen={historyOpen}
            setHistoryOpen={setHistoryOpen}
            count={conversations.length}
            histQuery={histQuery}
            setHistQuery={setHistQuery}
            q={q}
            visibleConvs={visibleConvs}
            currentId={currentId}
            renamingId={renamingId}
            setRenamingId={setRenamingId}
            renameValue={renameValue}
            setRenameValue={setRenameValue}
            commitRename={commitRename}
            pinnedIds={pinnedIds}
            togglePinned={togglePinned}
            startRename={startRename}
            selectConversation={selectConversation}
            exportConversation={exportConversation}
            shareConversation={shareConversation}
            deleteConversation={deleteConversation}
          />
          <SnippetsDialog
            snippetsOpen={snippetsOpen}
            setSnippetsOpen={setSnippetsOpen}
            input={input}
            saveSnippet={saveSnippet}
            snippets={snippets}
            insertSnippet={insertSnippet}
            deleteSnippet={deleteSnippet}
          />
          <SkillCreator
            open={skillCreatorOpen}
            onOpenChange={(o) => {
              setSkillCreatorOpen(o);
              if (!o) setEditingSkill(null);
            }}
            initial={editingSkill}
            customSkills={customSkills}
            onSave={(s) => (editingSkill ? updateCustomSkill(s) : addCustomSkill(s))}
            onDelete={deleteCustomSkill}
          />
          <SummaryDialog summaryOpen={summaryOpen} setSummaryOpen={setSummaryOpen} summary={summary} />
          <ContextDialog
            ctxOpen={ctxOpen}
            setCtxOpen={setCtxOpen}
            used={used}
            max={max}
            inTokens={inTokens}
            outTokens={outTokens}
            hasIo={!!ioTokens}
            sysPct={sysPct}
            msgPct={msgPct}
            ctxLevel={ctxLevel}
            model={model}
            convTokens={convTokens}
            systemPrompt={settings.system}
            attachments={attachments}
          />
          {(artifact || showLive) && (isNarrow || plein) && (
            <DocumentPanelFullscreen {...panelProps} />
          )}
          </HStack>
        </LayoutContent>
      }
    />
  );
}
