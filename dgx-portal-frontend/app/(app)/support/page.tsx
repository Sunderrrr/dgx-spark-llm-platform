"use client";

import { useEffect, useRef, useState } from "react";
import { Layout, LayoutHeader, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { MarkdownSur as Markdown } from "@/lib/markdown";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { ClickableCard } from "@astryxdesign/core/ClickableCard";
import { Card } from "@astryxdesign/core/Card";
import { Grid } from "@astryxdesign/core/Grid";
import { Icon } from "@astryxdesign/core/Icon";
import { Button } from "@astryxdesign/core/Button";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Banner } from "@astryxdesign/core/Banner";
import { Timestamp } from "@astryxdesign/core/Timestamp";
import {
  ClipboardDocumentIcon,
  ArrowPathIcon,
  HandThumbUpIcon,
  HandThumbDownIcon,
  ShieldExclamationIcon,
  ServerStackIcon,
  PlusIcon,
  StopIcon,
} from "@heroicons/react/24/outline";
import {
  KeyIcon,
  BanknotesIcon,
  Square3Stack3DIcon,
  ExclamationTriangleIcon,
} from "@heroicons/react/24/outline";
import {
  ChatLayout,
  ChatMessageList,
  ChatMessage,
  ChatMessageBubble,
  ChatMessageMetadata,
  ChatComposer,
  ChatComposerInput,
  ChatToolCalls,
} from "@astryxdesign/core/Chat";
import type { ChatToolCallItem } from "@astryxdesign/core/Chat";
import { useCsrf } from "@/lib/useCsrf";
import {
  confirmSupportAction,
  getJSON,
  sendJSON,
  sendSupportFeedback,
  streamSupportChat,
} from "@/lib/api";
import type { SupportConfirmRequest, ToolCallEvent } from "@/lib/api";
import { ThinkingIndicator } from "../_components/ThinkingIndicator";
import { ReasoningBlock } from "../_components/ReasoningBlock";
import { useT, tServeur } from "@/lib/i18n";
import { copierTexte } from "@/lib/copier";
import { texteNotice } from "@/lib/notices";
import type { CronosNotice } from "@/lib/notices";

type ChatMsg = {
  role: "user" | "assistant";
  content: string;
  ts?: number;
  isError?: boolean;
  /** Response cut short by « Arrêter »: the text already received is kept, but
   * it is marked so it does not pass for a complete answer. */
  interrupted?: boolean;
  toolCalls?: ChatToolCallItem[];
  /** The model's reasoning, streamed as `reasoning_content` (same contract as
   *  the playground): shown in the collapsible block, never in the answer. */
  reasoning?: string;
  /** Thinking duration in ms (first reasoning chunk → first content chunk). */
  reasoningMs?: number;
  /** Sensitive action proposed by the model (cronos_confirm): nothing is
   * executed until the user has clicked Confirmer/Annuler. */
  pendingAction?: SupportConfirmRequest;
};

/** Thumbs up/down state of ONE message (index → state). */
type FeedbackUi = {
  commentOpen: boolean;
  comment: string;
  busy: boolean;
  sent: boolean;
};

const SUGGESTIONS = [
  {
    heading: "Créer une clé",
    body: "Génère une nouvelle clé API pour tes intégrations",
    prompt: "Crée-moi une clé API pour mon laptop",
    icon: KeyIcon,
  },
  {
    heading: "Demander du budget",
    body: "Augmente ton quota de tokens mensuel",
    prompt: "Demande plus de budget pour mon compte",
    icon: BanknotesIcon,
  },
  {
    heading: "Modèles disponibles",
    body: "Liste les modèles actifs et leur fenêtre de contexte",
    prompt: "Quels modèles je peux utiliser et quel est leur contexte ?",
    icon: Square3Stack3DIcon,
  },
  {
    heading: "Erreur 401",
    body: "Diagnostique un problème d'authentification",
    prompt: "Ma clé API renvoie une erreur 401, pourquoi ?",
    icon: ExclamationTriangleIcon,
  },
];

// DYNAMIC welcome suggestions: they depend on the state the page
// actually observes (model stopped, no API key) and come BEFORE the
// generic cards — no point offering « Modèles disponibles » to someone
// whose model is down, or « Erreur 401 » to someone who has no key yet.
const SUGGESTION_MODELE_ARRETE = {
  heading: "Pourquoi le modèle est-il arrêté ?",
  body: "Diagnostique l'arrêt et propose de le relancer",
  prompt: "Pourquoi le modèle est-il arrêté ? Peux-tu le relancer ?",
  icon: ServerStackIcon,
};
const SUGGESTION_CLE_API = {
  heading: "Comment créer ma clé API ?",
  body: "L'assistant te la crée et t'explique comment l'utiliser",
  prompt: "Comment créer ma clé API ? Peux-tu le faire pour moi ?",
  icon: KeyIcon,
};

const WELCOME_MESSAGE_FR =
  "Bonjour 👋 Je suis **Cronos**, l'assistant de la plateforme. Je peux te dépanner (clé, quota, modèle, intégration OpenCode/Hermes…) mais aussi **agir pour toi** : créer une clé, demander du budget, demander un modèle. Dis-moi ce qu'il te faut.";

export default function SupportPage() {
  const t = useT();
  const csrf = useCsrf();
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [runningModel, setRunningModel] = useState<string | null>(null);
  // Does the account have at least one API key? (GET /api/keys, the same as the
  // Mes clés API page) — only used for the dynamic welcome.
  const [hasApiKey, setHasApiKey] = useState(true);
  // Controller of the current stream: the « Arrêter » button interrupts it (same
  // idiom as the playground). Null as soon as the stream is done.
  const abortRef = useRef<AbortController | null>(null);
  // Token whose confirmation/cancellation is in progress: the card's two
  // buttons go isDisabled (the token is single-use server-side).
  const [confirmingToken, setConfirmingToken] = useState<string | null>(null);
  // Thumbs up/down, by message index.
  const [feedback, setFeedback] = useState<Record<number, FeedbackUi>>({});
  // This page has no toast system: a failure that has no place in the
  // thread (missing CSRF token, copy impossible) is displayed here. Without
  // this, these buttons were SILENT no-ops.
  const [erreurUi, setErreurUi] = useState<string | null>(null);

  // The welcome message depends on the language, known only after the first
  // render (read from localStorage then /api/whoami) — impossible to freeze
  // in the initial state. So we compute it on every render as long as no
  // message has been sent, rather than storing it: it thus stays always
  // up to date if the language changes before the first send, without ever
  // overwriting an ongoing conversation.
  const displayMessages = messages.length ? messages : [{ role: "assistant" as const, content: t(WELCOME_MESSAGE_FR) }];

  useEffect(() => {
    getJSON<{ running_models: string[] }>("/api/playground/data").then((d) =>
      setRunningModel(d.running_models[0] || null),
    );
    // Thread kept server-side: a reload no longer loses the conversation.
    // We never overwrite the local state if the user has already written (race
    // between this answer and a first send from them).
    getJSON<{ messages?: { role: string; content: string }[] }>("/api/support/thread")
      .then((d) => {
        const restored = (d.messages ?? [])
          .filter((m) => m.role === "user" || m.role === "assistant")
          .map((m) => ({ role: m.role as ChatMsg["role"], content: m.content }));
        if (restored.length) setMessages((prev) => (prev.length ? prev : restored));
      })
      .catch(() => {});
    // Dynamic welcome: without an API key, we put its creation front and center.
    getJSON<{ user_keys?: unknown[] }>("/api/keys")
      .then((d) => setHasApiKey((d.user_keys ?? []).length > 0))
      .catch(() => {});
  }, []);

  async function runStream(nextMessages: ChatMsg[]) {
    if (!csrf) {
      setErreurUi(t("Session incomplète — recharge la page."));
      return;
    }
    // eslint-disable-next-line react-hooks/purity -- runStream only runs from event handlers
    const startTs = Date.now();
    // The replaced answer starts over: its vote too (regeneration).
    setFeedback((prev) => ({
      ...prev,
      [nextMessages.length]: { commentOpen: false, comment: "", busy: false, sent: false },
    }));
    setMessages([...nextMessages, { role: "assistant", content: "", ts: startTs }]);
    setIsSending(true);
    const controller = new AbortController();
    abortRef.current = controller;
    let acc = "";
    // The model's thinking, like the playground: accumulated apart (it never
    // goes into the answer), with its real duration (first reasoning chunk →
    // first content chunk).
    let raison = "";
    let raisonDebut: number | null = null;
    let finPensee: number | null = null;
    const toolCalls: ChatToolCallItem[] = [];
    const updateLast = (patch: Partial<ChatMsg> & { content: string }) => {
      setMessages((prev) => {
        const copy = [...prev];
        const last = copy[copy.length - 1];
        // Thread emptied in the meantime (« Nouvelle conversation »): nothing to update.
        if (!last) return copy;
        copy[copy.length - 1] = {
          ...last,
          toolCalls: toolCalls.length ? [...toolCalls] : undefined,
          ...patch,
        };
        return copy;
      });
    };
    // Server system refusal (no API key created, quota exhausted): Support
    // runs on the user's key, hence on their budget, like the
    // playground. We display the reason as-is — otherwise they would only see
    // an empty answer, without knowing what to fix.
    let notice = "";
    const onNotice = (n: CronosNotice) => {
      notice = texteNotice(n, t);
      updateLast({ content: notice, isError: true });
    };
    const onToolCall = (event: ToolCallEvent) => {
      const item: ChatToolCallItem = {
        key: event.id,
        name: t(event.name),
        target: event.target,
        status: event.status,
        duration: event.duration_ms != null ? `${(event.duration_ms / 1000).toFixed(1)}s` : undefined,
        errorMessage: event.error ? t(event.error) : event.error,
      };
      const i = toolCalls.findIndex((c) => c.key === event.id);
      if (i >= 0) toolCalls[i] = item;
      else toolCalls.push(item);
      updateLast({ content: acc });
    };
    // Sensitive-action confirmation request: attached to the current
    // message, the card rendered INSIDE the bubble waits for the click —
    // server-side, nothing is executed until the user has chosen.
    const onConfirm = (request: SupportConfirmRequest) => {
      setMessages((prev) => {
        const copy = [...prev];
        const last = copy[copy.length - 1];
        if (!last || last.role !== "assistant") return copy;
        copy[copy.length - 1] = { ...last, pendingAction: request };
        return copy;
      });
    };
    try {
      await streamSupportChat(
        csrf,
        nextMessages,
        controller.signal,
        (chunk) => {
          if (finPensee === null) finPensee = performance.now();
          acc += chunk;
          updateLast({ content: acc });
        },
        onToolCall,
        onNotice,
        onConfirm,
        (chunk) => {
          if (raisonDebut === null) raisonDebut = performance.now();
          raison += chunk;
          updateLast({ content: acc, reasoning: raison });
        },
      );
      // The thinking duration is only known here, at the end of the stream.
      if (raison) {
        updateLast({
          content: acc,
          reasoning: raison,
          // eslint-disable-next-line react-hooks/purity -- send() only runs from event handlers
          reasoningMs: raisonDebut ? Math.round((finPensee ?? performance.now()) - raisonDebut) : undefined,
        });
      }
      if (!acc && !notice) updateLast({ content: t("Pas de réponse."), isError: true });
    } catch (e) {
      if ((e as Error)?.name === "AbortError") {
        // Deliberate stop (« Arrêter »): not an error. We keep the text
        // that already arrived but mark it incomplete; with no text at all, we
        // remove the empty message rather than leave a ghost answer.
        if (acc) updateLast({ content: acc, interrupted: true });
        else setMessages((prev) => prev.slice(0, -1));
      } else {
        updateLast({ content: t("Erreur réseau — réessaie."), isError: true });
      }
    } finally {
      setIsSending(false);
      abortRef.current = null;
    }
  }

  function stop() {
    abortRef.current?.abort();
  }

  function appendConfirmResult(
    result: { ok?: boolean; message?: string; error?: string },
    token: string,
    keepCard: boolean,
  ) {
    // The server sentence (French) is translated at display time; the model's
    // own prose (`message` of a tool result) has no key and falls back to itself.
    const message = result.message
      ? tServeur(result.message, t)
      : result.error
        ? tServeur(result.error, t)
        : t("Erreur réseau — réessaie.");
    // eslint-disable-next-line react-hooks/purity -- handleConfirm only runs from event handlers
    const now = Date.now();
    setMessages((prev) => {
      const copy = prev.map((m) =>
        !keepCard && m.pendingAction?.token === token ? { ...m, pendingAction: undefined } : m,
      );
      copy.push({ role: "assistant", content: message, ts: now, isError: result.ok !== true });
      return copy;
    });
  }

  async function handleConfirm(token: string, cancel: boolean) {
    if (!csrf || confirmingToken) return;
    setConfirmingToken(token);
    try {
      const result = await confirmSupportAction(csrf, token, cancel);
      // 200 (ok true or false) as with 404/409: we display the server
      // message and remove the card — the token is single-use, a
      // second click could only fall back on « déjà traitée ».
      appendConfirmResult(result, token, false);
    } catch {
      // The server did not answer: the token may still be valid, we
      // keep the card (buttons re-enabled) to allow a new attempt.
      appendConfirmResult({ ok: false, message: t("Erreur réseau — réessaie.") }, token, true);
    } finally {
      setConfirmingToken(null);
    }
  }

  async function nouvelleConversation() {
    if (!csrf) {
      setErreurUi(t("Session incomplète — recharge la page."));
      return;
    }
    // A stream in progress would write into the thread right after the reset: we cut it.
    abortRef.current?.abort();
    setMessages([]);
    setFeedback({});
    setConfirmingToken(null);
    try {
      await sendJSON("/support/thread/clear", csrf, {});
    } catch {
      // The local thread is already emptied: a network error must not block anything.
    }
  }

  function setFeedbackUi(i: number, patch: Partial<FeedbackUi>) {
    setFeedback((prev) => {
      const base: FeedbackUi = prev[i] ?? { commentOpen: false, comment: "", busy: false, sent: false };
      return { ...prev, [i]: { ...base, ...patch } };
    });
  }

  async function sendFeedback(i: number, vote: 1 | -1, comment?: string) {
    if (!csrf) {
      setErreurUi(t("Session incomplète — recharge la page."));
      return;
    }
    const msg = messages[i];
    if (msg?.role !== "assistant") return;
    // question = the last user message preceding this answer.
    const question =
      [...messages.slice(0, i)].reverse().find((m) => m.role === "user")?.content ?? "";
    setFeedbackUi(i, { busy: true });
    try {
      await sendSupportFeedback(csrf, {
        vote,
        comment: comment?.trim() || undefined,
        question,
        answer: msg.content,
        model: runningModel ?? "",
      });
      setFeedbackUi(i, { sent: true, busy: false, commentOpen: false });
    } catch {
      // Vote lost: we never break the chat, the buttons stay usable.
      setFeedbackUi(i, { busy: false });
    }
  }

  function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || isSending) return;
    // eslint-disable-next-line react-hooks/purity -- send only runs from event handlers
    const nextMessages: ChatMsg[] = [...displayMessages, { role: "user", content: trimmed, ts: Date.now() }];
    setInput("");
    void runStream(nextMessages);
  }

  function regenerate() {
    if (isSending || !messages.length) return;
    const last = messages[messages.length - 1];
    const base = last.role === "assistant" ? messages.slice(0, -1) : messages;
    if (base.length && base[base.length - 1].role === "user") void runStream(base);
  }

  // Welcome cards: the suggestions tied to the observed state first.
  const suggestions = [
    ...(!runningModel ? [SUGGESTION_MODELE_ARRETE] : []),
    ...(!hasApiKey ? [SUGGESTION_CLE_API] : []),
    ...SUGGESTIONS,
  ];

  return (
    <Layout
      height="fill"
      padding={6}
      header={
        <LayoutHeader hasDivider>
          <HStack hAlign="between" vAlign="center" wrap="wrap" gap={3}>
            <VStack gap={0}>
              <Heading level={2}>{t("Support")}</Heading>
              <Text type="supporting" color="secondary">{t("Un assistant IA connecté à la plateforme : il voit tes clés (masquées), ton budget et l'état du serveur pour t'aider en cas de pépin.")}</Text>
            </VStack>
            <HStack gap={2} vAlign="center">
              {messages.length > 0 && (
                <Button
                  label={t("Nouvelle conversation")}
                  variant="ghost"
                  size="sm"
                  icon={<Icon icon={PlusIcon} size="sm" />}
                  onClick={() => void nouvelleConversation()}
                />
              )}
              <StatusDot variant={runningModel ? "success" : "error"} label={runningModel || t("aucun modèle actif")} />
            </HStack>
          </HStack>
        </LayoutHeader>
      }
      content={
        <LayoutContent padding={0} isScrollable={false}>
          {/* No scrollRef → ChatLayout is self-scrolling: its own root is the
              overflow:auto container and its dock uses position:sticky, which
              stays correctly pinned to the bottom during scroll (fixed-via-
              transform, tried earlier, drifted upward instead — see playground
              page.tsx for the full explanation). Its root is full width (no
              contentWidth on the outer Layout), so its native scrollbar lands at
              the true right edge; density="spacious" narrows just the message
              column and composer to a shared reading width. */}
          <VStack height="100%">
          <ChatLayout
            density="spacious"
            composer={
              <VStack gap={2} padding={4}>
                {messages.length === 0 && (
                  <Grid columns={{ minWidth: 220, max: 2 }} gap={3} width="100%">
                    {suggestions.map((s) => (
                      <ClickableCard key={s.heading} label={t(s.heading)} variant="muted" onClick={() => send(t(s.prompt))}>
                        <VStack gap={1}>
                          <HStack gap={2} vAlign="center">
                            <Icon icon={s.icon} size="sm" color="secondary" />
                            <Text weight="semibold">{t(s.heading)}</Text>
                          </HStack>
                          <Text type="supporting" color="secondary">
                            {t(s.body)}
                          </Text>
                        </VStack>
                      </ClickableCard>
                    ))}
                  </Grid>
                )}
                {erreurUi ? (
                  <Banner status="error" title={erreurUi} isDismissable onDismiss={() => setErreurUi(null)} />
                ) : null}
                <ChatComposer
                  value={input}
                  onChange={setInput}
                  onSubmit={send}
                  isDisabled={isSending}
                  placeholder={t("Écris ton message…  (Entrée pour envoyer, Maj+Entrée pour un saut de ligne)")}
                  input={<ChatComposerInput value={input} onChange={setInput} onSubmit={send} isDisabled={isSending} />}
                  sendButton={
                    isSending ? (
                      <Button
                        label={t("Arrêter")}
                        variant="primary"
                        isIconOnly
                        size="md"
                        icon={<Icon icon={StopIcon} size="sm" />}
                        onClick={stop}
                      />
                    ) : undefined
                  }
                />
                <Text type="supporting" color="secondary">{t("L'assistant ne voit que tes données (clés masquées). Ne colle jamais une clé complète ici.")}</Text>
              </VStack>
            }>
            <ChatMessageList>
              {displayMessages.map((m, i) => {
                const isLast = i === displayMessages.length - 1;
                // The confirmation card can arrive BEFORE the first text
                // token: as soon as it is there, no more thinking indicator,
                // otherwise it would stay hidden under the ThinkingIndicator.
                const isThinking = isSending && isLast && m.role === "assistant" && !m.content && !m.toolCalls?.length && !m.pendingAction;
                // isStreaming: without it, Markdown reparses all the text on
                // every token and only re-renders complete blocks — hence
                // a reply that appears in chunks instead of flowing token by
                // token like in the Playground. Streaming mode does
                // incremental parsing with per-fragment fade-in.
                const isStreamingThis = isSending && isLast && m.role === "assistant";
                const canRegenerateThis = m.role === "assistant" && isLast && !isSending && i > 0;
                const action = m.pendingAction;
                // A confirmation in progress disables the buttons of ALL the
                // cards: the token is single-use, a double click cannot
                // succeed anyway, better not expose it.
                const isConfirming = confirmingToken !== null;
                // Vote: only on a finished answer (never during the
                // stream, never on the welcome message) and only once.
                const feedbackUi = feedback[i];
                const canVote =
                  m.role === "assistant" && m.content && messages.length > 0 && !isThinking && !isStreamingThis && !feedbackUi?.sent;
                return (
                <ChatMessage key={i} sender={m.role}>
                  <ChatMessageBubble
                    variant={m.role === "user" ? "filled" : "ghost"}
                    className={m.role === "user" ? "bulle-question" : undefined}
                    /* Same registers as the playground: the question in its
                       bubble, the answer as plain text, and WHO is answering
                       named above (the model, as in the reference shots). */
                    name={
                      m.role === "assistant" && runningModel ? (
                        <Text type="supporting" color="secondary">{runningModel}</Text>
                      ) : undefined
                    }
                    metadata={
                      !isThinking && m.ts ? (
                        <ChatMessageMetadata
                          timestamp={<Timestamp value={m.ts} format="time" />}
                          status={m.isError ? "error" : undefined}
                          footer={
                            m.role === "assistant" && (m.content || canRegenerateThis) ? (
                              <HStack gap={1} vAlign="center">
                                {m.content && (
                                  <Button
                                    label={t("Copier")}
                                    variant="ghost"
                                    size="sm"
                                    isIconOnly
                                    icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                                    onClick={() => void copierTexte(m.content, () =>
                                      setErreurUi(t("Copie impossible depuis ce navigateur.")))}
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
                      <ThinkingIndicator />
                    ) : (
                      <VStack gap={2}>
                        {/* The thinking, in the same collapsible block as the
                            playground: open while it runs, folded on the first
                            answer word with its measured duration. */}
                        <ReasoningBlock
                          reasoning={m.reasoning || ""}
                          ms={m.reasoningMs}
                          streaming={isStreamingThis && !m.content.trim()}
                        />
                        {m.toolCalls?.length ? <ChatToolCalls calls={m.toolCalls} /> : null}
                        {m.content && (
                          <Markdown
                            className={m.reasoning && m.content.trim() ? "apparition-ecriture" : undefined}
                            isStreaming={isStreamingThis}>
                            {m.content}
                          </Markdown>
                        )}
                        {m.interrupted && (
                          <Text type="supporting" color="secondary">{t("Réponse interrompue.")}</Text>
                        )}
                        {action && (
                          <Card variant="muted" padding={3}>
                            <VStack gap={2}>
                              <HStack gap={2} vAlign="center">
                                <Icon icon={ShieldExclamationIcon} size="sm" color="warning" />
                                <Text weight="semibold">{t(action.label)}</Text>
                              </HStack>
                              {action.target ? (
                                <Text type="supporting" color="secondary">{action.target}</Text>
                              ) : null}
                              <Text type="supporting" color="secondary">
                                {t("Cette action sensible n'a pas encore été exécutée : elle attend ta confirmation.")}
                              </Text>
                              <HStack gap={2}>
                                <Button
                                  label={t("Confirmer")}
                                  variant="primary"
                                  size="sm"
                                  isDisabled={isConfirming}
                                  onClick={() => void handleConfirm(action.token, false)}
                                />
                                <Button
                                  label={t("Annuler")}
                                  variant="ghost"
                                  size="sm"
                                  isDisabled={isConfirming}
                                  onClick={() => void handleConfirm(action.token, true)}
                                />
                              </HStack>
                            </VStack>
                          </Card>
                        )}
                      </VStack>
                    )}
                  </ChatMessageBubble>
                  {canVote && (
                    <VStack gap={2}>
                      <HStack gap={1} vAlign="center">
                        <Button
                          label={t("Réponse utile")}
                          variant="ghost"
                          size="sm"
                          isIconOnly
                          isDisabled={feedbackUi?.busy}
                          icon={<Icon icon={HandThumbUpIcon} size="sm" />}
                          onClick={() => void sendFeedback(i, 1)}
                        />
                        <Button
                          label={t("Réponse pas utile")}
                          variant="ghost"
                          size="sm"
                          isIconOnly
                          isDisabled={feedbackUi?.busy}
                          icon={<Icon icon={HandThumbDownIcon} size="sm" />}
                          onClick={() => setFeedbackUi(i, { commentOpen: true })}
                        />
                      </HStack>
                      {feedbackUi?.commentOpen && (
                        <HStack gap={2} vAlign="center" width="100%">
                          <StackItem size="fill">
                            <TextInput
                              label={t("Un commentaire ? (facultatif)")}
                              isLabelHidden
                              size="sm"
                              value={feedbackUi.comment}
                              onChange={(v) => setFeedbackUi(i, { comment: v })}
                              placeholder={t("Un commentaire ? (facultatif)")}
                            />
                          </StackItem>
                          <Button
                            label={t("Envoyer")}
                            variant="primary"
                            size="sm"
                            isDisabled={feedbackUi.busy}
                            onClick={() => void sendFeedback(i, -1, feedbackUi.comment)}
                          />
                        </HStack>
                      )}
                    </VStack>
                  )}
                  {feedbackUi?.sent && (
                    <Text type="supporting" color="secondary">{t("Merci, c'est noté.")}</Text>
                  )}
                </ChatMessage>
                );
              })}
            </ChatMessageList>
          </ChatLayout>
          </VStack>
        </LayoutContent>
      }
    />
  );
}
