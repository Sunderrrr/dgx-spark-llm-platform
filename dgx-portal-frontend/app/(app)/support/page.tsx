"use client";

import { useEffect, useRef, useState } from "react";
import { Layout, LayoutHeader, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Markdown } from "@astryxdesign/core/Markdown";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { ClickableCard } from "@astryxdesign/core/ClickableCard";
import { Card } from "@astryxdesign/core/Card";
import { Grid } from "@astryxdesign/core/Grid";
import { Icon } from "@astryxdesign/core/Icon";
import { Button } from "@astryxdesign/core/Button";
import { TextInput } from "@astryxdesign/core/TextInput";
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
import { useT } from "@/lib/i18n";
import { texteNotice } from "@/lib/notices";
import type { CronosNotice } from "@/lib/notices";

type ChatMsg = {
  role: "user" | "assistant";
  content: string;
  ts?: number;
  isError?: boolean;
  /** Réponse coupée par « Arrêter » : le texte déjà reçu est gardé, mais on le
   * marque pour qu'il ne passe pas pour une réponse complète. */
  interrupted?: boolean;
  toolCalls?: ChatToolCallItem[];
  /** Action sensible proposée par le modèle (cronos_confirm) : rien n'est
   * exécuté tant que l'utilisateur n'a pas cliqué Confirmer/Annuler. */
  pendingAction?: SupportConfirmRequest;
};

/** État du pouce haut/bas d'UN message (index → état). */
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

// Suggestions d'accueil DYNAMIQUES : elles dépendent de l'état que la page
// observe réellement (modèle à l'arrêt, aucune clé API) et passent DEVANT les
// cartes génériques — inutile de proposer « Modèles disponibles » à quelqu'un
// dont le modèle est down, ou « Erreur 401 » à qui n'a encore aucune clé.
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
  // Le compte a-t-il au moins une clé API ? (GET /api/keys, le même que la
  // page Mes clés API) — sert uniquement à l'accueil dynamique.
  const [hasApiKey, setHasApiKey] = useState(true);
  // Contrôleur du flux en cours : le bouton « Arrêter » l'interrompt (même
  // idiome que le playground). Null dès que le flux est terminé.
  const abortRef = useRef<AbortController | null>(null);
  // Jeton dont la confirmation/annulation est en cours : les deux boutons de
  // la carte passent en isDisabled (le jeton est à usage unique côté serveur).
  const [confirmingToken, setConfirmingToken] = useState<string | null>(null);
  // Pouce haut/bas, par index de message.
  const [feedback, setFeedback] = useState<Record<number, FeedbackUi>>({});

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
    // Fil conservé côté serveur : un rechargement ne perd plus la conversation.
    // On n'écrase jamais l'état local si l'utilisateur a déjà écrit (course
    // entre cette réponse et un premier envoi de sa part).
    getJSON<{ messages?: { role: string; content: string }[] }>("/api/support/thread")
      .then((d) => {
        const restored = (d.messages ?? [])
          .filter((m) => m.role === "user" || m.role === "assistant")
          .map((m) => ({ role: m.role as ChatMsg["role"], content: m.content }));
        if (restored.length) setMessages((prev) => (prev.length ? prev : restored));
      })
      .catch(() => {});
    // Accueil dynamique : sans clé API, on met en avant sa création.
    getJSON<{ user_keys?: unknown[] }>("/api/keys")
      .then((d) => setHasApiKey((d.user_keys ?? []).length > 0))
      .catch(() => {});
  }, []);

  async function runStream(nextMessages: ChatMsg[]) {
    if (!csrf) return;
    // eslint-disable-next-line react-hooks/purity -- runStream only runs from event handlers
    const startTs = Date.now();
    // La réponse remplacée repart à zéro : son vote aussi (régénération).
    setFeedback((prev) => ({
      ...prev,
      [nextMessages.length]: { commentOpen: false, comment: "", busy: false, sent: false },
    }));
    setMessages([...nextMessages, { role: "assistant", content: "", ts: startTs }]);
    setIsSending(true);
    const controller = new AbortController();
    abortRef.current = controller;
    let acc = "";
    const toolCalls: ChatToolCallItem[] = [];
    const updateLast = (patch: Partial<ChatMsg> & { content: string }) => {
      setMessages((prev) => {
        const copy = [...prev];
        const last = copy[copy.length - 1];
        // Fil vidé entre-temps (« Nouvelle conversation ») : rien à mettre à jour.
        if (!last) return copy;
        copy[copy.length - 1] = {
          ...last,
          toolCalls: toolCalls.length ? [...toolCalls] : undefined,
          ...patch,
        };
        return copy;
      });
    };
    // Refus système du serveur (aucune clé API créée, quota épuisé) : le Support
    // tourne sur la clé de l'utilisateur, donc sur son budget, comme le
    // playground. On affiche la raison telle quelle — sinon il ne verrait
    // qu'une réponse vide, sans savoir quoi corriger.
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
    // Demande de confirmation d'une action sensible : accrochée au message
    // courant, la carte rendue DANS la bulle attend le clic — côté serveur,
    // rien n'est exécuté tant que l'utilisateur n'a pas choisi.
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
          acc += chunk;
          updateLast({ content: acc });
        },
        onToolCall,
        onNotice,
        onConfirm,
      );
      if (!acc && !notice) updateLast({ content: t("Pas de réponse."), isError: true });
    } catch (e) {
      if ((e as Error)?.name === "AbortError") {
        // Arrêt volontaire (« Arrêter ») : pas une erreur. On garde le texte
        // déjà arrivé mais on le marque incomplet ; sans aucun texte, on
        // retire le message vide plutôt que de laisser une réponse fantôme.
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
    const message = result.message || result.error || t("Erreur réseau — réessaie.");
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
      // 200 (ok true ou false) comme 404/409 : on affiche le message du
      // serveur et on retire la carte — le jeton est à usage unique, un
      // second clic ne pourrait que retomber sur « déjà traitée ».
      appendConfirmResult(result, token, false);
    } catch {
      // Le serveur n'a pas répondu : le jeton est peut-être encore valide, on
      // garde la carte (boutons réactivés) pour permettre une nouvelle tentative.
      appendConfirmResult({ ok: false, message: t("Erreur réseau — réessaie.") }, token, true);
    } finally {
      setConfirmingToken(null);
    }
  }

  async function nouvelleConversation() {
    if (!csrf) return;
    // Un flux en cours réécrirait dans le fil juste après le reset : on le coupe.
    abortRef.current?.abort();
    setMessages([]);
    setFeedback({});
    setConfirmingToken(null);
    try {
      await sendJSON("/support/thread/clear", csrf, {});
    } catch {
      // Le fil local est déjà vidé : une erreur réseau ne doit rien bloquer.
    }
  }

  function setFeedbackUi(i: number, patch: Partial<FeedbackUi>) {
    setFeedback((prev) => {
      const base: FeedbackUi = prev[i] ?? { commentOpen: false, comment: "", busy: false, sent: false };
      return { ...prev, [i]: { ...base, ...patch } };
    });
  }

  async function sendFeedback(i: number, vote: 1 | -1, comment?: string) {
    if (!csrf) return;
    const msg = messages[i];
    if (msg?.role !== "assistant") return;
    // question = le dernier message utilisateur précédant cette réponse.
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
      // Vote perdu : on ne casse jamais le chat, les boutons restent utilisables.
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

  // Cartes d'accueil : les suggestions liées à l'état observé d'abord.
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
                // La carte de confirmation peut arriver AVANT le premier token
                // de texte : dès qu'elle est là, plus d'indicateur de réflexion,
                // sinon elle resterait cachée sous le ThinkingIndicator.
                const isThinking = isSending && isLast && m.role === "assistant" && !m.content && !m.toolCalls?.length && !m.pendingAction;
                // isStreaming: without it, Markdown reparses all the text on
                // every token and only re-renders complete blocks — hence
                // a reply that appears in chunks instead of flowing token by
                // token like in the Playground. Streaming mode does
                // incremental parsing with per-fragment fade-in.
                const isStreamingThis = isSending && isLast && m.role === "assistant";
                const canRegenerateThis = m.role === "assistant" && isLast && !isSending && i > 0;
                const action = m.pendingAction;
                // Une confirmation en cours désactive les boutons de TOUTES les
                // cartes : le jeton est à usage unique, un double clic ne peut
                // de toute façon aboutir, autant ne pas l'exposer.
                const isConfirming = confirmingToken !== null;
                // Vote : seulement sur une réponse finie (jamais pendant le
                // flux, jamais sur le message d'accueil) et une seule fois.
                const feedbackUi = feedback[i];
                const canVote =
                  m.role === "assistant" && m.content && messages.length > 0 && !isThinking && !isStreamingThis && !feedbackUi?.sent;
                return (
                <ChatMessage key={i} sender={m.role}>
                  <ChatMessageBubble
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
                                    onClick={() => navigator.clipboard?.writeText(m.content)}
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
                        {m.toolCalls?.length ? <ChatToolCalls calls={m.toolCalls} /> : null}
                        {m.content && <Markdown isStreaming={isStreamingThis}>{m.content}</Markdown>}
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
