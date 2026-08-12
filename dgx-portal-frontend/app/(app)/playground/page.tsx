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
} from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { useDictation } from "@/lib/useDictation";
import { useIsNarrow } from "@/lib/useIsNarrow";
import { DictateButton } from "../_components/DictateButton";

import type { Attachment, ChatMsg, Conversation, Settings } from "@/lib/types";
import { fetchCsrfToken, fetchPlaygroundData, streamChat } from "@/lib/api";
import {
  fetchConversations,
  persistConversation,
  removeConversation,
  migrateLegacyConversations,
} from "@/lib/conversations";
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

  function newConversation() {
    persist(messages, currentId, model);
    setMessages([]);
    setCurrentId(null);
    setCtxUsed(0);
  }

  function selectConversation(conv: Conversation) {
    persist(messages, currentId, model);
    setMessages(conv.messages.map((m) => ({ role: m.role, content: m.content })));
    setCurrentId(conv.id);
    if (conv.model && runningModels.includes(conv.model)) setModel(conv.model);
    setCtxUsed(0);
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
      await streamChat(
        csrf,
        model,
        nextMessages.map((m) => ({ role: m.role, content: m.content })),
        settings,
        controller.signal,
        (delta) => {
          if (delta.usage) usage = delta.usage;
          if (delta.reasoningChunk) {
            if (tf === null) tf = performance.now();
            reason += delta.reasoningChunk;
            updateLast();
          }
          if (delta.contentChunk) {
            if (tf === null) tf = performance.now();
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
    const produced = parseArtifacts(acc, isDocTask(lastUser?.content ?? "")).artifacts;
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
    abortRef.current = null;
  }

  function send(value: string) {
    if (streaming) return;
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
    const nextMessages: ChatMsg[] = [
      ...messages,
      // eslint-disable-next-line react-hooks/purity -- send() only runs from a handler
      { role: "user", content: full, ts: Date.now(), attachmentCount: attachments.length || undefined },
    ];
    setMessages(nextMessages);
    setInput("");
    setAttachments([]);
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
                  const isLast = i === messages.length - 1;
                  const streamingThis = streaming && isLast;
                  const isThinking = streamingThis && m.role === "assistant" && !m.content && !m.reasoning;
                  const prevAttachments = messages[i - 1]?.attachmentCount;
                  const canRegenerateThis = m.role === "assistant" && isLast && !streaming;
                  // Once a reply finishes, detect artifacts (code files / long
                  // documents) so they get a card in the bubble + the copyable panel.
                  const arts = m.role === "assistant" && !streamingThis ? parseArtifacts(m.content, isDocTask(messages[i - 1]?.content ?? "")) : null;
                  const items = arts?.artifacts ?? [];
                  // When the model puts everything in the artifact and writes nothing
                  // outside, still show a short line in the chat (not an empty bubble).
                  const emptyMsg = items.some((a) => a.kind === "doc")
                    ? t("Voici le document — ouvre-le pour le lire ou le copier.")
                    : t("Voici le fichier — ouvre-le pour le copier.");
                  const bodyText = items.length ? (arts!.prose.trim() || emptyMsg) : m.content;
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
                              m.role === "assistant" && (m.tokens || m.tokensPerSec || canRegenerateThis) ? (
                                <HStack gap={1} vAlign="center">
                                  <Text type="supporting" color="secondary">
                                    {[
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
                style={{ width: artifactResize.size, flexShrink: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
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
                <VStack padding={4} isScrollable style={{ flex: 1, minHeight: 0 }}>
                  {panelIsCode && artifact?.kind === "code"
                    ? <CodeBlock title={artifact.title} language={artifact.lang} code={artifact.content} width="100%" />
                    : <Markdown isStreaming={showLive}>{panelContent || " "}</Markdown>}
                </VStack>
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
                  <LayoutContent padding={4} isScrollable>
                    {panelIsCode && artifact?.kind === "code"
                      ? <CodeBlock title={artifact.title} language={artifact.lang} code={artifact.content} width="100%" />
                      : <Markdown isStreaming={showLive}>{panelContent || " "}</Markdown>}
                  </LayoutContent>
                }
              />
            </Dialog>
          )}
          </HStack>
        </LayoutContent>
      }
    />
  );
}
