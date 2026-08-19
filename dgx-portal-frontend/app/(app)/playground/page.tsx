"use client";

import { useEffect, useRef, useState } from "react";
import { Icon } from "@astryxdesign/core/Icon";
import { Layout, LayoutHeader, LayoutContent } from "@astryxdesign/core/Layout";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Card } from "@astryxdesign/core/Card";
import { Toolbar } from "@astryxdesign/core/Toolbar";
import { useResizable, ResizeHandle } from "@astryxdesign/core/Resizable";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { Selector } from "@astryxdesign/core/Selector";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Markdown } from "@astryxdesign/core/Markdown";
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { Timestamp } from "@astryxdesign/core/Timestamp";
import { Token } from "@astryxdesign/core/Token";
import { DropdownMenu } from "@astryxdesign/core/DropdownMenu";
import type { DropdownMenuOption } from "@astryxdesign/core/DropdownMenu";
import { ClickableCard } from "@astryxdesign/core/ClickableCard";
import { Grid } from "@astryxdesign/core/Grid";
import {
  ChatLayout,
  ChatMessageList,
  ChatMessage,
  ChatMessageBubble,
  ChatMessageMetadata,
  ChatComposer,
  ChatComposerDrawer,
  ChatComposerInput,
} from "@astryxdesign/core/Chat";
import {
  PaperClipIcon,
  Cog6ToothIcon,
  ArrowDownTrayIcon,
  ArrowDownIcon,
  ClipboardDocumentIcon,
  ArrowPathIcon,
  PencilIcon,
  PlusIcon,
  ClockIcon,
  TrashIcon,
  SparklesIcon,
  CodeBracketIcon,
  LightBulbIcon,
  DocumentMagnifyingGlassIcon,
  DocumentTextIcon,
  XMarkIcon,
  PaperAirplaneIcon,
} from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { useDictation } from "@/lib/useDictation";
import { useIsNarrow } from "@/lib/useIsNarrow";
import { useStickToBottom } from "@/lib/useStickToBottom";
import { DictateButton } from "../_components/DictateButton";

import type { Attachment, ChatMsg, Conversation, Settings } from "@/lib/types";
import { fetchCsrfToken, fetchPlaygroundData, streamChat } from "@/lib/api";
import {
  fetchConversations,
  persistConversation,
  removeConversation,
  migrateLegacyConversations,
} from "@/lib/conversations";
import { AskQuestion } from "./_components/AskQuestion";
import { ContextMeter } from "./_components/ContextMeter";
import { SettingsPanel } from "./_components/SettingsPanel";
import { ThinkingIndicator } from "../_components/ThinkingIndicator";

const DEFAULT_SETTINGS: Settings = {
  system: "",
  temperature: 0.7,
  maxTokens: 4096,
  topP: 1,
  reasoning: false,
};

const ATTACH_ACCEPT =
  ".md,.markdown,.txt,.text,.log,.logs,.err,.error,.out,.json,.jsonl,.csv,.tsv,.yaml,.yml,.toml,.ini,.conf,.cfg,.env,.py,.js,.ts,.jsx,.tsx,.java,.c,.cpp,.h,.go,.rs,.rb,.php,.sh,.bash,.sql,.html,.css,.xml,.diff,.patch";

const PRESETS = [
  {
    heading: "Code Python",
    body: "Génère une fonction, un script ou un test",
    prompt: "Écris une fonction Python qui vérifie si un nombre est premier.",
    icon: CodeBracketIcon,
  },
  {
    heading: "Expliquer",
    body: "Décompose un sujet technique simplement",
    prompt: "Explique la mémoire unifiée du DGX Spark en termes simples.",
    icon: LightBulbIcon,
  },
  {
    heading: "Analyser des logs",
    body: "Trouve la cause d'une erreur dans un extrait de logs",
    prompt: "Analyse ces logs et trouve la cause de l'erreur : ",
    icon: DocumentMagnifyingGlassIcon,
  },
  {
    heading: "Résumer",
    body: "Condense un texte en points clés",
    prompt: "Résume ce texte en 3 points : ",
    icon: DocumentTextIcon,
  },
];
const MAX_ATTACHMENT_BYTES = 96 * 1024;

// Something the assistant produced worth showing in the side panel (canvas/
// artifact style) and copying in one click: a code "file" or a long "document"
// (e.g. a rewritten/reformatted text).
type Artifact =
  | { kind: "code"; title: string; lang: string; content: string }
  | { kind: "doc"; title: string; content: string };

// Below this length a document-task answer is probably a clarifying question →
// keep it inline rather than filing it as a document.
const DOC_MIN_CHARS = 400;

// Appended to the system prompt so the model can ask the user one or several
// multiple-choice clarifying questions (rendered as selectable answers, submitted
// together) instead of guessing — the same idea as Claude's "ask the user" tool.
const ASK_INSTRUCTION = `When you need the user to clarify things before you can answer well, ask your questions as a single fenced block. Output it exactly like this:
\`\`\`ask
{"questions": [{"question": "<question 1>", "options": ["<option>", "<option>"]}, {"question": "<question 2>", "options": ["<option>", "<option>", "<option>"]}]}
\`\`\`
Strict rules:
- Before the block you MAY write ONE short introductory sentence (e.g. "Bien sûr ! Quelques précisions pour bien t'aider :"). Do NOT write the questions or their options as normal text anywhere — they go INSIDE the block ONLY.
- 1 to 4 questions, each with 2 to 4 short options in the user's language. Ask everything you need in this one block (the user answers them all at once).
- Do NOT add an "Other" option (the interface adds one).
- Ask AT MOST ONCE. As soon as the user has answered, you MUST give your real, complete answer using their choices — NEVER reply with another ask block once they have answered.
- Only ask when it genuinely helps; otherwise just answer normally.`;

// One clarifying question + its proposed answers.
type AskQ = { question: string; options: string[] };
// A model's clarifying block: one or more questions, plus any prose around it.
type AskBlock = { questions: AskQ[]; prose: string };

// Detect a ```ask block. Accepts {questions:[…]} and the legacy {question,options}.
function parseAsk(content: string): AskBlock | null {
  const m = content.match(/```ask\s*\n([\s\S]*?)```/);
  if (!m) return null;
  try {
    const obj = JSON.parse(m[1].trim());
    const raw: unknown[] = Array.isArray(obj.questions) ? obj.questions : (obj.question ? [obj] : []);
    const questions: AskQ[] = raw
      .map((q) => {
        const qq = q as { question?: unknown; options?: unknown };
        const question = typeof qq.question === "string" ? qq.question.trim() : "";
        const options = Array.isArray(qq.options)
          ? qq.options.filter((o: unknown) => typeof o === "string" && o.trim()).map((o: string) => o.trim()).slice(0, 4)
          : [];
        return { question, options };
      })
      .filter((q) => q.question && q.options.length >= 1)
      .slice(0, 4);
    if (!questions.length) return null;
    return { questions, prose: content.replace(m[0], "").trim() };
  } catch {
    return null;
  }
}

// Whether the user's request is a "document" task (correct / rewrite / reformat /
// draft / "make a document/report/note…"). Only then is the answer treated as a
// document. Accent- and language-insensitive (FR + EN). Plain "explain"/"summarize"
// stays inline.
function isDocTask(prompt: string): boolean {
  const p = prompt.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  // (a) Correction / rewrite / reformat tasks.
  if (/(corrig|reformul|reecri|reecrire|redig|remet(s)? en forme|met(s)? en forme|mise en forme|orthograph|\brelis\b|relire|proofread|rewrite|re-?format|rephrase|\bcorrect\b|clean ?up|\bedit this\b|\bfix the\b)/.test(p)) return true;
  // (b) "Produce a document / report / note / letter / …": a create verb near a doc noun.
  if (/(fais|fait|faire|cree|creer|genere|generer|ecri|redig|prepar|make|create|write|draft|produce|compose|generate|prepare)[\s\S]{0,30}(document|rapport|report|\bnote\b|compte[- ]?rendu|fiche|guide|essai|essay|lettre|letter|courriel|e-?mail|\bmail\b|article|synthese|memo|dossier|cahier|resume)/.test(p)) return true;
  return false;
}

// Turn an assistant answer into prose (chat) + artifacts (side panel).
// Deterministic — no reliance on the model's own formatting:
//  - Document task with a substantial answer → the WHOLE answer is ONE document
//    artifact; the chat shows only a short line + a card (no duplication, no
//    stray code-block cards for tables/ascii inside the document).
//  - Otherwise → substantial fenced code blocks become file artifacts, the rest
//    of the prose stays inline.
// Name a generated document from its own content: the first Markdown heading,
// else the first non-empty line, stripped of Markdown decoration. Returns
// "Document" while a stream hasn't produced a usable title line yet.
function docTitleFromContent(content: string): string {
  const text = content.trim();
  if (!text) return "Document";
  const heading = text.match(/^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$/m);
  let raw = heading ? heading[1] : (text.split("\n").find((l) => l.trim().length > 0) ?? "");
  raw = raw
    .replace(/[*_`~#>]/g, "")          // md emphasis / fences / quotes / hashes
    .replace(/^\s*[-•]+\s+/, "")       // bullet markers
    .replace(/^\s*\d+[.)]\s+/, "")     // numbered markers
    .replace(/\s+/g, " ")
    .trim();
  if (!raw) return "Document";
  return raw.length > 60 ? raw.slice(0, 57).trimEnd() + "…" : raw;
}

// A filesystem-safe slug for the download filename.
function slugify(s: string): string {
  const base = s
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "") // strip accents
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  return base || "document";
}

// Client-side download of some text as a file (used for the Markdown export).
function downloadText(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function parseArtifacts(content: string, allowDoc: boolean): { prose: string; artifacts: Artifact[] } {
  const text = content.trim();
  if (allowDoc && text.length >= DOC_MIN_CHARS) {
    return { prose: "", artifacts: [{ kind: "doc", title: docTitleFromContent(text), content: text }] };
  }
  const fence = /```([^\n`]*)\n([\s\S]*?)```/g;
  const artifacts: Artifact[] = [];
  let prose = "";
  let lastIndex = 0;
  let m: RegExpExecArray | null;
  let n = 0;
  while ((m = fence.exec(content)) !== null) {
    const body = m[2].replace(/\n$/, "");
    const substantial = body.length >= 200 || body.split("\n").length >= 6;
    if (!substantial) continue; // leave small snippets inline
    const info = m[1].trim();
    const first = info.split(/\s+/)[0] || "";
    if (first === "ask") continue; // handled by parseAsk, never a file artifact
    let lang = first;
    let title = "";
    if (first.includes(".")) { title = first; lang = first.split(".").pop() || ""; }
    const named = info.match(/(?:title|file|filename)=(\S+)/i);
    if (named) title = named[1];
    n += 1;
    if (!title) title = lang ? `${lang} · ${n}` : `file ${n}`;
    artifacts.push({ kind: "code", title, lang: lang || "text", content: body });
    prose += content.slice(lastIndex, m.index);
    lastIndex = fence.lastIndex;
  }
  prose += content.slice(lastIndex);
  return { prose: prose.trim(), artifacts };
}

function estimateTokens(
  settings: Settings,
  input: string,
  messages: ChatMsg[],
  attachments: Attachment[],
): number {
  let chars = settings.system.length + input.length;
  for (const m of messages) chars += m.content.length;
  for (const a of attachments) chars += a.content.length;
  return Math.round(chars / 4);
}

type QueuedMsg = { content: string; attachmentCount?: number; ts: number };

export default function PlaygroundPage() {
  const t = useT();
  const [csrf, setCsrf] = useState("");
  const [runningModels, setRunningModels] = useState<string[]>([]);
  const [modelLimits, setModelLimits] = useState<Record<string, number>>({});
  const [model, setModel] = useState("");
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [streaming, setStreaming] = useState(false);

  // File d'attente : messages soumis pendant qu'une réponse se génère. Au lieu
  // d'être perdus (l'ancien « if (streaming) return »), ils s'affichent dans la
  // conversation avec un badge « En attente » et ne partent au modèle qu'une
  // fois validés. Valider interrompt la réponse en cours (sa partie déjà écrite
  // est conservée, comme avec Stop) : le modèle répond à CE message à la place.
  const [queued, setQueued] = useState<QueuedMsg[]>([]);
  const queuedRef = useRef<QueuedMsg[]>([]);
  const flushAfterStreamRef = useRef(false);
  // runStream clôt la conversation depuis sa propre closure : pour repartir de
  // la liste à jour (réponse partielle conservée, etc.) on lit par ref.
  const messagesRef = useRef(messages);
  useEffect(() => { messagesRef.current = messages; });

  const updateQueue = (q: QueuedMsg[]) => {
    queuedRef.current = q;
    setQueued(q);
  };
  // Débit en direct : `usage.completion_tokens` n'arrive qu'à la FIN du flux. On
  // estime donc le nombre de tokens à partir des CARACTÈRES reçus — compter les
  // deltas SSE serait faux (mesuré : avec le décodage spéculatif MTP, vLLM envoie
  // plusieurs tokens par delta, d'où ~2,7x de sous-estimation). Le ratio
  // caractères/token est auto-calibré à la fin de chaque génération sur le
  // `usage` exact, donc il s'adapte au modèle et à la langue (mesuré : ~4,5 en
  // français, ~5,0 en anglais). Refs : mises à jour à chaque delta sans re-render.
  const liveCharsRef = useRef(0);
  const charsPerTokenRef = useRef(4.8);
  const liveStartRef = useRef<number | null>(null);
  const [liveStats, setLiveStats] = useState<{ tokens: number; tps: number } | null>(null);

  // Rafraîchit le compteur affiché 4x/s pendant le flux — assez fluide à l'œil,
  // sans ajouter un re-render par token (updateLast en fait déjà un).
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
  const [currentId, setCurrentId] = useState<number | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [ctxUsed, setCtxUsed] = useState(0);
  const [firstName, setFirstName] = useState("");
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
  const liveDocOpenRef = useRef(false);
  const openLiveDoc = () => { setLiveDocOpen(true); liveDocOpenRef.current = true; };

  const abortRef = useRef<AbortController | null>(null);

  // Dictation: same hook as the Voice and Video pages.
  const dictation = useDictation({ value: input, onChange: setInput, csrf });
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
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
    fetchCsrfToken().then(setCsrf).catch(() => {});
    fetchPlaygroundData()
      .then((data) => {
        setRunningModels(data.running_models);
        setModelLimits(data.model_limits);
        if (data.running_models.length) setModel(data.running_models[0]);
      })
      .catch(() => {});
    fetch("/api/whoami", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setFirstName(d?.fullname?.split(" ")[0] || ""))
      .catch(() => {});
  }, []);

  // persist()/runStream() only ever run from event handlers (send/regenerate/edit),
  // never during render, so Date.now()/performance.now() here are safe despite the
  // purity lint rule's conservative render-reachability analysis.
  function persist(msgs: ChatMsg[], convId: number | null, activeModel: string) {
    if (!msgs.length) return convId;
    const title = (msgs.find((m) => m.role === "user")?.content || t("Conversation")).slice(0, 80);
    const item: Conversation = {
      // eslint-disable-next-line react-hooks/purity
      id: convId ?? Date.now(),
      title,
      // eslint-disable-next-line react-hooks/purity
      ts: Date.now(),
      model: activeModel,
      messages: msgs.map((m) => ({ role: m.role, content: m.content })),
    };
    // Optimistic on the UI side, then server save in the background: the
    // list must not wait for the network round-trip to update.
    setConversations((prev) => {
      const rest = prev.filter((c) => c.id !== item.id);
      return [item, ...rest];
    });
    if (csrf) void persistConversation(csrf, item);
    return item.id;
  }

  // Close any open document/file panel — it belongs to the conversation we're
  // leaving, not the one we're opening.
  function closeArtifact() {
    setArtifact(null);
    setLiveDocOpen(false);
  }

  function newConversation() {
    persist(messages, currentId, model);
    setMessages([]);
    setCurrentId(null);
    setCtxUsed(0);
    updateQueue([]);
    flushAfterStreamRef.current = false;
    closeArtifact();
  }

  function selectConversation(conv: Conversation) {
    persist(messages, currentId, model);
    setMessages(conv.messages.map((m) => ({ role: m.role, content: m.content })));
    setCurrentId(conv.id);
    if (conv.model && runningModels.includes(conv.model)) setModel(conv.model);
    setCtxUsed(0);
    updateQueue([]);
    flushAfterStreamRef.current = false;
    closeArtifact();
  }

  function deleteConversation(id: number) {
    setConversations((prev) => prev.filter((c) => c.id !== id));
    if (csrf) void removeConversation(csrf, id);
    if (id === currentId) setCurrentId(null);
  }

  function handleFiles(files: FileList | null) {
    if (!files) return;
    for (const file of Array.from(files)) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        window.alert(`« ${file.name} » dépasse 96 Ko — trop gros pour le contexte.`);
        continue;
      }
      const reader = new FileReader();
      reader.onload = () => {
        setAttachments((prev) => [...prev, { name: file.name, content: String(reader.result) }]);
      };
      reader.readAsText(file);
    }
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  async function runStream(nextMessages: ChatMsg[]) {
    if (!model) {
      setMessages([...nextMessages, { role: "assistant", content: t("Aucun modèle actif.") }]);
      return;
    }
    setStreaming(true);
    setLiveDocOpen(false);
    liveDocOpenRef.current = false;
    const controller = new AbortController();
    abortRef.current = controller;
    // eslint-disable-next-line react-hooks/purity -- runStream only runs from event handlers
    const startTs = Date.now();
    const withPlaceholder = [...nextMessages, { role: "assistant", content: "", ts: startTs } as ChatMsg];
    setMessages(withPlaceholder);

    // eslint-disable-next-line react-hooks/purity -- runStream only runs from event handlers
    const t0 = performance.now();
    liveCharsRef.current = 0;
    liveStartRef.current = null;
    setLiveStats(null);
    let tf: number | null = null;
    let acc = "";
    let reason = "";
    let usage: { total_tokens?: number; completion_tokens?: number } | undefined;

    const updateLast = () => {
      setMessages((prev) => {
        const copy = [...prev];
        copy[copy.length - 1] = { role: "assistant", content: acc, reasoning: reason, ts: copy[copy.length - 1]?.ts };
        return copy;
      });
    };

    let isError = false;
    let wasAborted = false;
    try {
      // Offer the "ask the user" capability only until the user has answered a
      // question once (a hidden answer message exists). This stops the model from
      // looping on more and more questions instead of actually answering.
      const alreadyAsked = nextMessages.some((mm) => mm.hidden);
      const askSettings = {
        ...settings,
        system: alreadyAsked
          ? settings.system
          : [settings.system.trim(), ASK_INSTRUCTION].filter(Boolean).join("\n\n"),
      };
      await streamChat(
        csrf,
        model,
        nextMessages.map((m) => ({ role: m.role, content: m.content })),
        askSettings,
        controller.signal,
        (delta) => {
          if (delta.usage) usage = delta.usage;
          if (delta.reasoningChunk) {
            if (tf === null) tf = performance.now();
            if (liveStartRef.current === null) liveStartRef.current = tf;
            liveCharsRef.current += delta.reasoningChunk.length;
            reason += delta.reasoningChunk;
            updateLast();
          }
          if (delta.contentChunk) {
            if (tf === null) tf = performance.now();
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

    // eslint-disable-next-line react-hooks/purity -- runStream only runs from event handlers
    const te = performance.now();
    const finalMessages = [...nextMessages];
    if (acc || reason) {
      const gen = tf ? (te - tf) / 1000 : 0;
      const tokens = usage?.completion_tokens;
      // Auto-calibrage : le vrai nombre de tokens est connu ici, on en déduit le
      // ratio caractères/token réel de ce modèle/cette langue pour que l'estimation
      // en direct de la PROCHAINE génération soit juste. Borné pour qu'une réponse
      // dégénérée (1 token, 500 caractères) ne fausse pas durablement l'affichage.
      const producedChars = acc.length + reason.length;
      if (tokens && producedChars > 0) {
        charsPerTokenRef.current = Math.min(12, Math.max(1.5, producedChars / tokens));
      }
      finalMessages.push({
        role: "assistant",
        content: acc,
        reasoning: reason,
        tokens,
        tokensPerSec: tokens && gen > 0 ? Number((tokens / gen).toFixed(1)) : undefined,
        ttft: tf ? Number(((tf - t0) / 1000).toFixed(2)) : undefined,
        ts: startTs,
        isError,
      });
    }
    setMessages(finalMessages);
    // If the assistant wrote a file, or wrote a document in reply to a document
    // task, surface the last one in the side panel automatically.
    const lastUser = [...nextMessages].reverse().find((mm) => mm.role === "user");
    // A clarifying question is never a document/file artifact — leave the panel closed.
    const produced = parseAsk(acc) ? [] : parseArtifacts(acc, isDocTask(lastUser?.content ?? "")).artifacts;
    if (produced.length) {
      const lastArt = produced[produced.length - 1];
      // A code file opens on its own; a document opens automatically only if the
      // user was already watching it being written live (otherwise it stays a
      // card in the chat that they can click open).
      if (lastArt.kind === "code" || liveDocOpenRef.current) setArtifact(lastArt);
    }
    liveDocOpenRef.current = false;
    if (usage?.total_tokens) setCtxUsed(usage.total_tokens);
    const savedId = persist(finalMessages, currentId, model);
    setCurrentId(savedId ?? null);
    setStreaming(false);
    setLiveStats(null);
    abortRef.current = null;
    // File validée pendant cette génération : on enchaîne maintenant que la
    // conversation est refermée. Base explicite (finalMessages) : messagesRef
    // n'est pas encore resynchronisé dans cette même tick.
    if (flushAfterStreamRef.current) {
      flushAfterStreamRef.current = false;
      dispatchQueued(finalMessages);
    }
  }

  function send(value: string) {
    const text = value.trim();
    if (!text && !attachments.length) return;
    // Sending ends dictation: the message goes out with what has been
    // transcribed so far, and no still-in-flight pass will rewrite the
    // field once it's been cleared.
    dictation.cancel();
    let full = text;
    if (attachments.length) {
      full =
        (text ? text + "\n\n" : "") +
        attachments.map((f) => "```" + f.name + "\n" + f.content + "\n```").join("\n\n");
    }
    const attachmentCount = attachments.length || undefined;
    // En pleine génération, le message passe en file d'attente (validé par le
    // bouton Envoyer de sa bulle) au lieu d'être silencieusement perdu.
    if (streaming) {
      // eslint-disable-next-line react-hooks/purity -- send() only runs from a handler
      updateQueue([...queuedRef.current, { content: full, attachmentCount, ts: Date.now() }]);
      setInput("");
      setAttachments([]);
      return;
    }
    const nextMessages: ChatMsg[] = [
      ...messages,
      // eslint-disable-next-line react-hooks/purity -- send() only runs from a handler
      { role: "user", content: full, ts: Date.now(), attachmentCount },
    ];
    setMessages(nextMessages);
    setInput("");
    setAttachments([]);
    void runStream(nextMessages);
  }

  // Envoie la file d'attente. Si une réponse est en cours, on l'interrompt
  // d'abord — son début est conservé dans la conversation — puis la file part
  // à la fin du runStream en cours (d'où le flag, l'abort étant asynchrone).
  function flushQueue() {
    if (!queuedRef.current.length) return;
    if (streaming) {
      flushAfterStreamRef.current = true;
      abortRef.current?.abort();
      return;
    }
    dispatchQueued();
  }

  function dispatchQueued(base?: ChatMsg[]) {
    const q = queuedRef.current;
    if (!q.length) return;
    updateQueue([]);
    const msgs: ChatMsg[] = q.map((m) => ({ role: "user", content: m.content, ts: m.ts, attachmentCount: m.attachmentCount }));
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
    // eslint-disable-next-line react-hooks/purity -- answer() only runs from a handler
    const nextMessages: ChatMsg[] = [...messages, { role: "user", content: t2, ts: Date.now(), hidden: true }];
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

  function editLast() {
    if (streaming || !messages.length) return;
    let base = messages;
    if (base[base.length - 1]?.role === "assistant") base = base.slice(0, -1);
    const last = base[base.length - 1];
    if (last?.role === "user") {
      setInput(last.content);
      setMessages(base.slice(0, -1));
    }
  }

  function exportMarkdown() {
    if (!messages.length) {
      window.alert(t("Rien à exporter."));
      return;
    }
    let out = "# Conversation Cronos\n\n";
    for (const m of messages) {
      out += (m.role === "user" ? `## ${t("Vous")}` : `## ${model || t("Modèle")}`) + "\n\n" + m.content + "\n\n";
    }
    const blob = new Blob([out], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "cronos-conversation.md";
    a.click();
    URL.revokeObjectURL(a.href);
  }

  const max = modelLimits[model] || 32768;
  const used = Math.max(ctxUsed, estimateTokens(settings, input, messages, attachments));
  const lastMsg = messages[messages.length - 1];
  // While a document is being streamed, the chat shows a card (not the raw text)
  // and the side panel can show a live view of the document being written.
  const streamingDocActive =
    streaming && lastMsg?.role === "assistant" && isDocTask(messages[messages.length - 2]?.content ?? "");
  const liveContent = streamingDocActive ? (lastMsg?.content ?? "") : "";
  const showLive = liveDocOpen && streamingDocActive;
  // Unified panel values: live document while streaming, else the pinned artifact.
  const panelIsCode = !showLive && artifact?.kind === "code";
  const panelTitle = showLive ? docTitleFromContent(liveContent) : (artifact?.title ?? "Document");
  const panelContent = showLive ? liveContent : (artifact?.content ?? "");
  const panelSubtitle = showLive
    ? t("Rédaction en cours…")
    : (panelIsCode && artifact?.kind === "code" ? artifact.lang : "");
  const panelDownloadName = panelIsCode ? panelTitle : `${slugify(panelTitle)}.md`;
  const panelDownloadMime = panelIsCode ? "text/plain" : "text/markdown";
  // Auto-follow the document while it streams into the panel; show a "jump to
  // bottom" button when the reader scrolls up and leaves the live tail.
  const {
    setRef: panelScrollRef,
    showButton: showPanelJump,
    onScroll: onPanelScroll,
    scrollToBottom: panelJumpDown,
  } = useStickToBottom(panelContent, showLive);
  const canRegenerate = !streaming && lastMsg && (lastMsg.role === "assistant" || lastMsg.role === "user");
  const canEdit = !streaming && messages.some((m) => m.role === "user");

  const historyItems: DropdownMenuOption[] =
    conversations.length === 0
      ? [{ label: t("Aucune conversation"), isDisabled: true }]
      : conversations.map((conv) => ({
          label: conv.title || t("Conversation"),
          onClick: () => selectConversation(conv),
        }));

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
            <HStack gap={2}>
              <Button
                label={t("Nouvelle conversation")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={PlusIcon} size="sm" />}
                isIconOnly
                onClick={newConversation}
              />
              <DropdownMenu
                button={{ label: t("Historique"), variant: "secondary", size: "sm", icon: <Icon icon={ClockIcon} size="sm" /> }}
                items={historyItems}
                menuWidth={260}
              />
              {currentId != null && (
                <Button
                  label={t("Supprimer cette conversation")}
                  variant="secondary"
                  size="sm"
                  icon={<Icon icon={TrashIcon} size="sm" />}
                  isIconOnly
                  onClick={() => deleteConversation(currentId)}
                />
              )}
              <Button
                label={t("Exporter en Markdown")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                isIconOnly
                onClick={exportMarkdown}
              />
              <Button
                label={t("Réglages")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={Cog6ToothIcon} size="sm" />}
                isIconOnly
                onClick={() => setIsSettingsOpen((v) => !v)}
              />
            </HStack>
          </HStack>
        </LayoutHeader>
      }
      content={
        <LayoutContent padding={0} isScrollable={false}>
          {isSettingsOpen && (
            <VStack padding={4}>
              <SettingsPanel settings={settings} onChange={setSettings} />
            </VStack>
          )}
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
          <ChatLayout
            density="spacious"
            composer={
              <VStack gap={2} padding={4}>
                <ChatComposer
                  value={input}
                  onChange={setInput}
                  onSubmit={send}
                  isStopShown={streaming}
                  onStop={stop}
                  placeholder={t("Écris ton message… (Entrée pour envoyer, Maj+Entrée = saut de ligne)")}
                  input={<ChatComposerInput value={input} onChange={setInput} onSubmit={send} />}
                  headerActions={
                    <>
                      <Button
                        label={t("Joindre un fichier")}
                        variant="ghost"
                        size="sm"
                        isIconOnly
                        icon={<Icon icon={PaperClipIcon} size="sm" />}
                        onClick={() => fileInputRef.current?.click()}
                      />
                      <DictateButton dictation={dictation} isDisabled={streaming} />
                    </>
                  }
                  headerContext={<ContextMeter used={used} max={max} />}
                  drawer={
                    attachments.length ? (
                      <ChatComposerDrawer count={attachments.length} label={t("Fichiers joints")}>
                        {attachments.map((f, i) => (
                          <Token
                            key={f.name + i}
                            label={`${f.name} (${Math.ceil(f.content.length / 1024)} Ko)`}
                            onRemove={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                          />
                        ))}
                      </ChatComposerDrawer>
                    ) : undefined
                  }
                  footerActions={
                    <Selector
                      label={t("Modèle")}
                      isLabelHidden
                      size="sm"
                      placeholder={t("Aucun modèle actif")}
                      options={runningModels}
                      value={model}
                      onChange={(v) => setModel(v ?? "")}
                    />
                  }
                />
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  accept={ATTACH_ACCEPT}
                  style={{ display: "none" }}
                  onChange={(e) => handleFiles(e.target.files)}
                />
                <HStack hAlign="between" gap={2}>
                  <Text type="supporting" color="secondary">{t("Fichiers texte uniquement. Les tokens comptent sur ton budget.")}</Text>
                  <HStack gap={2}>
                    {canEdit && (
                      <Button
                        label={t("Éditer")}
                        variant="ghost"
                        size="sm"
                        icon={<Icon icon={PencilIcon} size="sm" />}
                        onClick={editLast}
                      />
                    )}
                    {canRegenerate && (
                      <Button
                        label={t("Régénérer")}
                        variant="ghost"
                        size="sm"
                        icon={<Icon icon={ArrowPathIcon} size="sm" />}
                        onClick={regenerate}
                      />
                    )}
                  </HStack>
                </HStack>
              </VStack>
            }>
            <ChatMessageList
              emptyState={
                <VStack gap={6} hAlign="center">
                  <VStack gap={1} hAlign="center">
                    <HStack gap={2} vAlign="center">
                      <Icon icon={SparklesIcon} size="md" color="accent" />
                      <Text type="large" as="h2">
                        {firstName ? `${t("Bonjour")}, ${firstName}` : t("Bonjour")}
                      </Text>
                    </HStack>
                    <Text type="display-2" as="h1">{t("Sur quoi veux-tu travailler ?")}</Text>
                  </VStack>
                  <Grid columns={{ minWidth: 220, max: 2 }} gap={3} width="100%">
                    {PRESETS.map((preset) => (
                      <ClickableCard
                        key={preset.heading}
                        label={t(preset.heading)}
                        variant="muted"
                        onClick={() => setInput(t(preset.prompt))}>
                        <VStack gap={1}>
                          <HStack gap={2} vAlign="center">
                            <Icon icon={preset.icon} size="sm" color="secondary" />
                            <Text weight="semibold">{t(preset.heading)}</Text>
                          </HStack>
                          <Text type="supporting" color="secondary">
                            {t(preset.body)}
                          </Text>
                        </VStack>
                      </ClickableCard>
                    ))}
                  </Grid>
                </VStack>
              }>
                {messages.map((m, i) => {
                  // Hidden messages (e.g. answers submitted from a question card)
                  // are sent to the model but never shown in the chat.
                  if (m.hidden) return null;
                  const isLast = i === messages.length - 1;
                  const streamingThis = streaming && isLast;
                  const isThinking = streamingThis && m.role === "assistant" && !m.content && !m.reasoning;
                  const prevAttachments = messages[i - 1]?.attachmentCount;
                  const canRegenerateThis = m.role === "assistant" && isLast && !streaming;
                  // Once a reply finishes, detect artifacts (code files / long
                  // documents) so they get a card in the bubble + the copyable panel.
                  // A clarifying question the model asked (rendered as clickable
                  // answers). Takes precedence over document/code artifact detection.
                  const ask = m.role === "assistant" && !streamingThis ? parseAsk(m.content) : null;
                  const arts = m.role === "assistant" && !streamingThis && !ask ? parseArtifacts(m.content, isDocTask(messages[i - 1]?.content ?? "")) : null;
                  const items = arts?.artifacts ?? [];
                  // When the model puts everything in the artifact and writes nothing
                  // outside, still show a short line in the chat (not an empty bubble).
                  const emptyMsg = items.some((a) => a.kind === "doc")
                    ? t("Voici le document — ouvre-le pour le lire ou le copier.")
                    : t("Voici le fichier — ouvre-le pour le copier.");
                  // While streaming, hide a half-written ```ask block (raw JSON) —
                  // the question UI appears once the block is complete.
                  const streamingBody = streamingThis && m.content.includes("```ask")
                    ? m.content.split("```ask")[0]
                    : m.content;
                  // With a question card, show only the model's short intro
                  // sentence (its first line) — never the questions/options text,
                  // which live in the interactive card.
                  const askIntro = ask ? (ask.prose.split("\n").map((s) => s.trim()).find(Boolean) ?? "").slice(0, 280) : "";
                  const bodyText = ask ? askIntro : (items.length ? (arts!.prose.trim() || emptyMsg) : streamingBody);
                  // A document being streamed shows only a live-updating card in
                  // the chat (its raw text streams into the side panel instead).
                  const streamingDoc =
                    streamingThis && m.role === "assistant" && m.content.length > 0 &&
                    isDocTask(messages[i - 1]?.content ?? "");
                  return (
                  <ChatMessage key={i} sender={m.role}>
                    <ChatMessageBubble
                      metadata={
                        !isThinking && m.ts ? (
                          <ChatMessageMetadata
                            timestamp={<Timestamp value={m.ts} format="time" />}
                            status={m.isError ? "error" : undefined}
                            footer={
                              m.role === "assistant" && (m.tokens || m.tokensPerSec || canRegenerateThis || (streamingThis && liveStats)) ? (
                                <HStack gap={1} vAlign="center">
                                  <Text type="supporting" color="secondary">
                                    {streamingThis && liveStats
                                      ? // Pendant le flux : compté sur les deltas SSE, donc « ~ ».
                                        `~${liveStats.tokens} tokens · ${liveStats.tps} tok/s`
                                      : [
                                          m.tokens ? `${m.tokens} tokens` : null,
                                          m.tokensPerSec ? `${m.tokensPerSec} tok/s` : null,
                                          m.ttft ? `TTFT ${m.ttft}s` : null,
                                        ]
                                          .filter(Boolean)
                                          .join(" · ")}
                                  </Text>
                                  <Button
                                    label={t("Copier")}
                                    variant="ghost"
                                    size="sm"
                                    isIconOnly
                                    icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                                    onClick={() => navigator.clipboard?.writeText(m.content)}
                                  />
                                  {canRegenerateThis && (
                                    <Button
                                      label={t("Régénérer")}
                                      variant="ghost"
                                      size="sm"
                                      isIconOnly
                                      icon={<Icon icon={ArrowPathIcon} size="sm" />}
                                      onClick={regenerate}
                                    />
                                  )}
                                </HStack>
                              ) : m.role === "user" ? (
                                <Button
                                  label={t("Copier")}
                                  variant="ghost"
                                  size="sm"
                                  isIconOnly
                                  icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                                  onClick={() => navigator.clipboard?.writeText(m.content)}
                                />
                              ) : undefined
                            }
                          />
                        ) : undefined
                      }>
                      {isThinking ? (
                        <ThinkingIndicator fixedLabel={prevAttachments ? t("Lecture du fichier…") : undefined} />
                      ) : streamingDoc ? (
                        <ClickableCard
                          label={t("Ouvrir le document en cours de rédaction")}
                          variant="muted"
                          onClick={openLiveDoc}>
                          <HStack gap={2} vAlign="center">
                            <Icon icon={DocumentTextIcon} size="sm" color="secondary" />
                            <VStack gap={0}>
                              <Text weight="semibold">{docTitleFromContent(m.content)}</Text>
                              <Text type="supporting" color="secondary">{t("Rédaction en cours…")}</Text>
                            </VStack>
                          </HStack>
                        </ClickableCard>
                      ) : ask ? (
                        <VStack gap={2}>
                          {m.reasoning ? (
                            <Collapsible trigger={t("Raisonnement")} defaultIsOpen={false}>
                              <Markdown>{m.reasoning}</Markdown>
                            </Collapsible>
                          ) : null}
                          {bodyText.trim() ? <Markdown>{bodyText}</Markdown> : null}
                          <AskQuestion
                            questions={ask.questions}
                            answered={!isLast}
                            onSubmit={(ans) =>
                              answer(
                                ask.questions.length === 1
                                  ? ans[0]
                                  : ask.questions.map((q, k) => `${q.question}\n→ ${ans[k]}`).join("\n\n"),
                              )
                            }
                          />
                        </VStack>
                      ) : (
                        <VStack gap={2}>
                          {m.reasoning ? (
                            <Collapsible trigger={t("Raisonnement")} defaultIsOpen={false}>
                              <Markdown isStreaming={streamingThis}>{m.reasoning}</Markdown>
                            </Collapsible>
                          ) : null}
                          <Markdown isStreaming={streamingThis}>{bodyText || " "}</Markdown>
                          {items.map((a, ai) => (
                            <ClickableCard
                              key={ai}
                              label={a.kind === "code" ? `${t("Ouvrir le fichier")} ${a.title}` : t("Ouvrir le document")}
                              variant="muted"
                              onClick={() => setArtifact(a)}>
                              <HStack gap={2} vAlign="center">
                                <Icon icon={DocumentTextIcon} size="sm" color="secondary" />
                                <VStack gap={0}>
                                  <Text weight="semibold">{a.title}</Text>
                                  <Text type="supporting" color="secondary">
                                    {a.kind === "code" ? a.lang : t("Ouvrir et copier dans le volet")}
                                  </Text>
                                </VStack>
                              </HStack>
                            </ClickableCard>
                          ))}
                        </VStack>
                      )}
                    </ChatMessageBubble>
                  </ChatMessage>
                  );
                })}
                {queued.map((q, i) => (
                  <ChatMessage key={`queued-${q.ts}`} sender="user">
                    <ChatMessageBubble
                      metadata={
                        <ChatMessageMetadata
                          timestamp={<Timestamp value={q.ts} format="time" />}
                          footer={
                            <HStack gap={1} vAlign="center" wrap="wrap">
                              <Badge label={t("En attente")} variant="warning" />
                              {i === queued.length - 1 && streaming && (
                                <Text type="supporting" color="secondary">
                                  {t("La réponse en cours sera interrompue.")}
                                </Text>
                              )}
                              {/* Un seul bouton Envoyer, sur le dernier message en
                                  attente : valider envoie TOUTE la file d'un coup. */}
                              {i === queued.length - 1 && (
                                <Button
                                  label={t("Envoyer")}
                                  variant="primary"
                                  size="sm"
                                  icon={<Icon icon={PaperAirplaneIcon} size="sm" />}
                                  onClick={flushQueue}
                                />
                              )}
                              <Button
                                label={t("Retirer")}
                                variant="ghost"
                                size="sm"
                                isIconOnly
                                icon={<Icon icon={XMarkIcon} size="sm" />}
                                onClick={() => discardQueued(i)}
                              />
                            </HStack>
                          }
                        />
                      }
                    >
                      <Markdown>{q.content}</Markdown>
                    </ChatMessageBubble>
                  </ChatMessage>
                ))}
            </ChatMessageList>
          </ChatLayout>
          </VStack>
          </StackItem>
          {(artifact || showLive) && !isNarrow && (
            <>
              <ResizeHandle
                direction="horizontal"
                resizable={artifactResize.props}
                isReversed
                pillPlacement="start"
                hasDivider
                label={t("Redimensionner le panneau")}
              />
              <Card
                variant="transparent"
                height="100%"
                style={{ width: artifactResize.size, flexShrink: 0, display: "flex", flexDirection: "column", overflow: "hidden", position: "relative" }}>
                <Toolbar
                  label={panelIsCode ? t("Fichier") : t("Document")}
                  dividers={["bottom"]}
                  startContent={
                    <HStack gap={2} vAlign="center">
                      <Icon icon={DocumentTextIcon} size="sm" color="secondary" />
                      <VStack gap={0}>
                        <Text weight="semibold">{panelTitle}</Text>
                        {panelSubtitle ? <Text type="supporting" color="secondary">{panelSubtitle}</Text> : null}
                      </VStack>
                    </HStack>
                  }
                  endContent={
                    <>
                      <Button label={t("Télécharger")} variant="ghost" size="sm" isIconOnly
                        icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                        onClick={() => downloadText(panelDownloadName, panelContent, panelDownloadMime)} />
                      <Button label={t("Copier")} variant="ghost" size="sm" isIconOnly
                        icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                        onClick={() => navigator.clipboard?.writeText(panelContent)} />
                      <Button label={t("Fermer")} variant="ghost" size="sm" isIconOnly
                        icon={<Icon icon={XMarkIcon} size="sm" />}
                        onClick={() => { setArtifact(null); setLiveDocOpen(false); }} />
                    </>
                  }
                />
                <VStack ref={panelScrollRef} onScroll={onPanelScroll} padding={4} isScrollable style={{ flex: 1, minHeight: 0 }}>
                  {panelIsCode && artifact?.kind === "code"
                    ? <CodeBlock title={artifact.title} language={artifact.lang} code={artifact.content} width="100%" />
                    : <Markdown isStreaming={showLive}>{panelContent || " "}</Markdown>}
                </VStack>
                {showPanelJump && (
                  <HStack style={{ position: "absolute", bottom: "var(--spacing-4)", left: "50%", transform: "translateX(-50%)", zIndex: 2 }}>
                    <Button label={t("Descendre")} variant="primary" size="sm"
                      icon={<Icon icon={ArrowDownIcon} size="sm" />}
                      onClick={panelJumpDown} />
                  </HStack>
                )}
              </Card>
            </>
          )}
          {(artifact || showLive) && isNarrow && (
            <Dialog isOpen onOpenChange={(o) => { if (!o) { setArtifact(null); setLiveDocOpen(false); } }} variant="fullscreen">
              <Layout
                header={
                  <DialogHeader
                    title={panelTitle}
                    subtitle={panelSubtitle || undefined}
                    hasDivider
                    onOpenChange={(o) => { if (!o) { setArtifact(null); setLiveDocOpen(false); } }}
                    endContent={
                      <HStack gap={1} vAlign="center">
                        <Button label={t("Télécharger")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                          onClick={() => downloadText(panelDownloadName, panelContent, panelDownloadMime)} />
                        <Button label={t("Copier")} variant="ghost" size="sm" isIconOnly
                          icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                          onClick={() => navigator.clipboard?.writeText(panelContent)} />
                      </HStack>
                    }
                  />
                }
                content={
                  <LayoutContent ref={panelScrollRef} onScroll={onPanelScroll} padding={4} isScrollable>
                    {panelIsCode && artifact?.kind === "code"
                      ? <CodeBlock title={artifact.title} language={artifact.lang} code={artifact.content} width="100%" />
                      : <Markdown isStreaming={showLive}>{panelContent || " "}</Markdown>}
                  </LayoutContent>
                }
              />
              {showPanelJump && (
                <HStack style={{ position: "fixed", bottom: "var(--spacing-6)", left: "50%", transform: "translateX(-50%)", zIndex: 10 }}>
                  <Button label={t("Descendre")} variant="primary" size="sm"
                    icon={<Icon icon={ArrowDownIcon} size="sm" />}
                    onClick={panelJumpDown} />
                </HStack>
              )}
            </Dialog>
          )}
          </HStack>
        </LayoutContent>
      }
    />
  );
}
