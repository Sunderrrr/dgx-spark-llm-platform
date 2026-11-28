"use client";

// The thread area of the playground: the self-scrolling chat column, the
// message bubbles, the « Descendre » button and the empty thread state.
// Extracted VERBATIM from the message list of
// app/(app)/playground/PlaygroundPage.tsx (plain props, no behaviour change):
// the page keeps the state, the streaming logic and every handler.
//
// The scroll container is ChatLayout's OWN root, attached here through
// `followBottomSetRef` (the callback ref from useStickToBottom, owned by the
// page). No scrollRef is passed: without it ChatLayout stays self-scrolling
// and its dock (the composer) is sticky — see the long note in the page.

import type { ReactNode } from "react";
import { Button } from "@astryxdesign/core/Button";
import { VStack } from "@astryxdesign/core/Stack";
import { ChatLayout, ChatMessageList } from "@astryxdesign/core/Chat";
import { Text } from "@astryxdesign/core/Text";
import { useT } from "@/lib/i18n";
import type { ChatMsg } from "@/lib/types";
import type { WebStep } from "@/lib/api";
import type { Artifact, FileVersion } from "@/lib/playground-thread";
import { ThreadMessage, type LiveCode } from "./ThreadMessage";

export type PlaygroundChatProps = {
  /** The composer node, built by the page and kept in the dock. */
  composer: ReactNode;
  /** Callback ref of the scroll container (useStickToBottom). */
  followBottomSetRef: (node: HTMLElement | null) => void;
  showScrollDown: boolean;
  scrollToBottom: () => void;
  greeting: string;
  messages: ChatMsg[];
  streaming: boolean;
  liveCode: LiveCode | null;
  isNarrow: boolean;
  webSteps: WebStep[];
  editingIdx: number | null;
  liveStats: { tokens: number; tps: number } | null;
  resume: number;
  model: string;
  renamedTitle: (a: Artifact) => string;
  versions: Map<string, FileVersion[]>;
  waitLabel?: string;
  onAnswerNow: () => void;
  onAnswer: (text: string) => void;
  onRequestFullFile: (name: string) => void;
  onRedoFile: (name: string) => void;
  onContinue: () => void;
  onRegenerate: () => void;
  onEditMessage: (i: number) => void;
  onOpenImage: (v: { srcs: string[]; index: number }) => void;
  setArtifact: (a: Artifact) => void;
  setRenamingArtifact: (v: boolean) => void;
  onOpenLiveDoc: () => void;
};

export function PlaygroundChat({
  composer,
  followBottomSetRef,
  showScrollDown,
  scrollToBottom,
  greeting,
  messages,
  streaming,
  liveCode,
  isNarrow,
  webSteps,
  editingIdx,
  liveStats,
  resume,
  model,
  renamedTitle,
  versions,
  waitLabel,
  onAnswerNow,
  onAnswer,
  onRequestFullFile,
  onRedoFile,
  onContinue,
  onRegenerate,
  onEditMessage,
  onOpenImage,
  setArtifact,
  setRenamingArtifact,
  onOpenLiveDoc,
}: PlaygroundChatProps) {
  const t = useT();
  return (
    <ChatLayout
      density="spacious"
      composer={composer}
      ref={followBottomSetRef}
      scrollButton={
        showScrollDown ? (
          <Button
            label={t("Descendre")}
            variant="secondary"
            size="sm"
            onClick={scrollToBottom}
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
              webSteps={webSteps}
              editingIdx={editingIdx}
              liveStats={liveStats}
              resume={resume}
              model={model}
              renamedTitle={renamedTitle}
              versions={versions}
              waitLabel={waitLabel}
              onAnswerNow={onAnswerNow}
              onAnswer={onAnswer}
              onRequestFullFile={onRequestFullFile}
              onRedoFile={onRedoFile}
              onContinue={onContinue}
              onRegenerate={onRegenerate}
              onEditMessage={onEditMessage}
              onOpenImage={onOpenImage}
              onOpenArtifact={(a) => {
                setArtifact(a);
                setRenamingArtifact(false);
              }}
              onOpenLiveDoc={onOpenLiveDoc}
            />
          ))}
      </ChatMessageList>
    </ChatLayout>
  );
}
