"use client";

import { useEffect, useState } from "react";
import { Layout, LayoutHeader, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Markdown } from "@astryxdesign/core/Markdown";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { ClickableCard } from "@astryxdesign/core/ClickableCard";
import { Grid } from "@astryxdesign/core/Grid";
import { Icon } from "@astryxdesign/core/Icon";
import { Button } from "@astryxdesign/core/Button";
import { Timestamp } from "@astryxdesign/core/Timestamp";
import { ClipboardDocumentIcon } from "@heroicons/react/24/outline";
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
} from "@astryxdesign/core/Chat";
import { useCsrf } from "@/lib/useCsrf";
import { getJSON, streamSupportChat } from "@/lib/api";
import { ThinkingIndicator } from "../_components/ThinkingIndicator";

type ChatMsg = { role: "user" | "assistant"; content: string; ts?: number };

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

const WELCOME_MESSAGE: ChatMsg = {
  role: "assistant",
  content:
    "Bonjour 👋 Je suis **Cronos**, l'assistant de la plateforme. Je peux te dépanner (clé, quota, modèle, intégration OpenCode/Hermes…) mais aussi **agir pour toi** : créer une clé, demander du budget, demander un modèle. Dis-moi ce qu'il te faut.",
};

export default function SupportPage() {
  const csrf = useCsrf();
  const [messages, setMessages] = useState<ChatMsg[]>([WELCOME_MESSAGE]);
  const [input, setInput] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [runningModel, setRunningModel] = useState<string | null>(null);

  useEffect(() => {
    getJSON<{ running_models: string[] }>("/api/playground/data").then((d) =>
      setRunningModel(d.running_models[0] || null),
    );
  }, []);

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || !csrf) return;
    // eslint-disable-next-line react-hooks/purity -- send only runs from event handlers
    const now = Date.now();
    const nextMessages: ChatMsg[] = [...messages, { role: "user", content: trimmed, ts: now }];
    setMessages([...nextMessages, { role: "assistant", content: "", ts: now }]);
    setInput("");
    setIsSending(true);
    let acc = "";
    const updateLast = (content: string) => {
      setMessages((prev) => {
        const copy = [...prev];
        copy[copy.length - 1] = { role: "assistant", content, ts: copy[copy.length - 1]?.ts };
        return copy;
      });
    };
    try {
      await streamSupportChat(csrf, nextMessages, new AbortController().signal, (chunk) => {
        acc += chunk;
        updateLast(acc);
      });
      if (!acc) updateLast("Pas de réponse.");
    } catch {
      updateLast("Erreur réseau — réessaie.");
    } finally {
      setIsSending(false);
    }
  }

  return (
    <Layout
      height="fill"
      padding={6}
      header={
        <LayoutHeader hasDivider>
          <HStack hAlign="between" vAlign="center" wrap="wrap" gap={3}>
            <VStack gap={0}>
              <Heading level={2}>Support</Heading>
              <Text type="supporting" color="secondary">
                Un assistant IA connecté à la plateforme : il voit tes clés (masquées), ton budget et l&apos;état du
                serveur pour t&apos;aider en cas de pépin.
              </Text>
            </VStack>
            <StatusDot variant={runningModel ? "success" : "error"} label={runningModel || "aucun modèle actif"} />
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
                {messages.length === 1 && (
                  <Grid columns={{ minWidth: 220, max: 2 }} gap={3} width="100%">
                    {SUGGESTIONS.map((s) => (
                      <ClickableCard key={s.heading} label={s.heading} variant="muted" onClick={() => send(s.prompt)}>
                        <VStack gap={1}>
                          <HStack gap={2} vAlign="center">
                            <Icon icon={s.icon} size="sm" color="secondary" />
                            <Text weight="semibold">{s.heading}</Text>
                          </HStack>
                          <Text type="supporting" color="secondary">
                            {s.body}
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
                  placeholder="Écris ton message…  (Entrée pour envoyer, Maj+Entrée pour un saut de ligne)"
                  input={<ChatComposerInput value={input} onChange={setInput} onSubmit={send} isDisabled={isSending} />}
                />
                <Text type="supporting" color="secondary">
                  L&apos;assistant ne voit que tes données (clés masquées). Ne colle jamais une clé complète ici.
                </Text>
              </VStack>
            }>
            <ChatMessageList>
              {messages.map((m, i) => {
                const isLast = i === messages.length - 1;
                const isThinking = isSending && isLast && m.role === "assistant" && !m.content;
                return (
                <ChatMessage key={i} sender={m.role}>
                  <ChatMessageBubble
                    metadata={
                      !isThinking && m.ts ? (
                        <ChatMessageMetadata
                          timestamp={<Timestamp value={m.ts} format="time" />}
                          footer={
                            m.role === "assistant" && m.content ? (
                              <Button
                                label="Copier"
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
                    {isThinking ? <ThinkingIndicator /> : <Markdown>{m.content}</Markdown>}
                  </ChatMessageBubble>
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
