"use client";

import { useEffect, useRef, useState } from "react";
import { Icon } from "@astryxdesign/core/Icon";
import { Layout, LayoutHeader, LayoutContent } from "@astryxdesign/core/Layout";
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
  // Document/artifact side-panel: pop a long assistant answer into a wide,
  // resizable reading pane (Markdown + copy), inspired by the Astryx ai-chat template.
  const [artifact, setArtifact] = useState<{ title: string; content: string } | null>(null);
  const artifactResize = useResizable({ defaultSize: 560, minSizePx: 420, maxSizePx: 900, autoSaveId: "playground-artifact" });

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
                  const isThinking = streaming && isLast && m.role === "assistant" && !m.content && !m.reasoning;
                  const prevAttachments = messages[i - 1]?.attachmentCount;
                  const canRegenerateThis = m.role === "assistant" && isLast && !streaming;
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
                                  {m.content.length > 400 && (
                                    <Button
                                      label={t("Ouvrir en document")}
                                      variant="ghost"
                                      size="sm"
                                      isIconOnly
                                      icon={<Icon icon={DocumentTextIcon} size="sm" />}
                                      onClick={() => setArtifact({ title: t("Document"), content: m.content })}
                                    />
                                  )}
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
                              ) : undefined
                            }
                          />
                        ) : undefined
                      }>
                      {isThinking ? (
                        <ThinkingIndicator fixedLabel={prevAttachments ? t("Lecture du fichier…") : undefined} />
                      ) : m.reasoning ? (
                        <VStack gap={2}>
                          <Collapsible trigger={t("Raisonnement")} defaultIsOpen={false}>
                            <Markdown isStreaming={streaming && isLast}>
                              {m.reasoning}
                            </Markdown>
                          </Collapsible>
                          <Markdown isStreaming={streaming && isLast}>
                            {m.content || " "}
                          </Markdown>
                        </VStack>
                      ) : (
                        <Markdown isStreaming={streaming && isLast}>
                          {m.content || " "}
                        </Markdown>
                      )}
                    </ChatMessageBubble>
                  </ChatMessage>
                  );
                })}
            </ChatMessageList>
          </ChatLayout>
          </VStack>
          </StackItem>
          {artifact && (
            <>
              <ResizeHandle
                direction="horizontal"
                resizable={artifactResize.props}
                isReversed
                pillPlacement="start"
                hasDivider
                label={t("Redimensionner le document")}
              />
              <Card
                variant="transparent"
                height="100%"
                style={{ width: artifactResize.size, flexShrink: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
                <Toolbar
                  label={t("Document")}
                  dividers={["bottom"]}
                  startContent={
                    <HStack gap={2} vAlign="center">
                      <Icon icon={DocumentTextIcon} size="sm" color="secondary" />
                      <Text weight="semibold">{artifact.title}</Text>
                    </HStack>
                  }
                  endContent={
                    <>
                      <Button label={t("Copier")} variant="ghost" size="sm" isIconOnly
                        icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                        onClick={() => navigator.clipboard?.writeText(artifact.content)} />
                      <Button label={t("Fermer")} variant="ghost" size="sm" isIconOnly
                        icon={<Icon icon={XMarkIcon} size="sm" />}
                        onClick={() => setArtifact(null)} />
                    </>
                  }
                />
                <VStack padding={4} isScrollable style={{ flex: 1, minHeight: 0 }}>
                  <Markdown>{artifact.content}</Markdown>
                </VStack>
              </Card>
            </>
          )}
          </HStack>
        </LayoutContent>
      }
    />
  );
}
