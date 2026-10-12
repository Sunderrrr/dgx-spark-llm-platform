"use client";

// One chat bubble of the playground thread: question, reasoning + answer,
// tool steps, artifact cards, clarifying-question card. Extracted VERBATIM
// from the message list of app/(app)/playground/page.tsx (plain props, no
// behaviour change): the derived parsing (artifacts, edits, resumes) runs here
// exactly as it did inline in the map.

import { Icon } from "@astryxdesign/core/Icon";
import { useToast } from "@astryxdesign/core/Toast";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { AspectRatio } from "@astryxdesign/core/AspectRatio";
import { Banner } from "@astryxdesign/core/Banner";
import { ClientTimestamp } from "@/lib/client-timestamp";
import { Thumbnail } from "@astryxdesign/core/Thumbnail";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { ClickableCard } from "@astryxdesign/core/ClickableCard";
import {
  ChatMessage,
  ChatMessageBubble,
  ChatMessageMetadata,
} from "@astryxdesign/core/Chat";
import {
  ClipboardDocumentIcon,
  ArrowPathIcon,
  PencilIcon,
  DocumentTextIcon,
} from "@heroicons/react/24/outline";
// Wrapper that closes over the dependency's URL filter (see lib/markdown.tsx).
import { SafeMarkdown as Markdown } from "@/lib/markdown";
import { useT } from "@/lib/i18n";
import { copyText } from "@/lib/clipboard";
import { ReasoningBlock } from "../../_components/ReasoningBlock";
import { ThinkingIndicator } from "../../_components/ThinkingIndicator";
import { GenerationPlaceholder } from "../../_components/GenerationPlaceholder";
import type { ChatMsg } from "@/lib/types";
import type { WebStep } from "@/lib/api";
import { parseAsk, closedContent } from "@/lib/playground-parsers";
import {
  MAX_AUTO_RESUMES,
  applyEdits,
  docTitleFromContent,
  isResume,
  isDocTask,
  messageFragments,
  messageMerge,
  webStepLabel,
  incompleteMessage,
  parseArtifacts,
  brokenScript,
  type Artifact,
} from "@/lib/playground-thread";
import { AskQuestion } from "./AskQuestion";

/** File being written live, as shown in the side panel (null = none). */
export type LiveCode = { lang: string; body: string; start: number };

export type ThreadMessageProps = {
  m: ChatMsg;
  i: number;
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
  onAnswer: (text: string) => void;
  onRequestFullFile: (name: string) => void;
  onRedoFile: (name: string) => void;
  onContinue: () => void;
  onRegenerate: () => void;
  onEditMessage: (i: number) => void;
  onOpenImage: (v: { srcs: string[]; index: number }) => void;
  onOpenArtifact: (a: Artifact) => void;
  onOpenLiveDoc: () => void;
};

export function ThreadMessage({
  m,
  i,
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
  onAnswer,
  onRequestFullFile,
  onRedoFile,
  onContinue,
  onRegenerate,
  onEditMessage,
  onOpenImage,
  onOpenArtifact,
  onOpenLiveDoc,
}: ThreadMessageProps) {
  const t = useT();
  const showToast = useToast();
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
                  // FINISHED files become cards right away, without
                  // waiting for the answer to end: parseArtifacts only extracts
                  // blocks whose closing fence has arrived, the one in progress stays
                  // aside (panel) and not in the chat. `allowDoc` stays reserved
                  // for the end: flipping the whole message into a document mid-stream
                  // would make the already read text disappear.
                  // The file being written goes into the panel: we
                  // remove it once and for all from the analyzed content, so it
                  // comes out neither as a card nor in the chat prose.
                  const shownContent =
                    streamingThis && isLast && liveCode ? m.content.slice(0, liveCode.start) : m.content;
                  const arts = m.role === "assistant" && !ask
                    ? parseArtifacts(
                        // Never-closed fence: we close it, otherwise the
                        // cut file stays as raw code in the middle of the bubble.
                        streamingThis ? shownContent : closedContent(shownContent),
                        !streamingThis && isDocTask(messages[i - 1]?.content ?? ""), t)
                    : null;
                  // Resume message: it only carries the end of the file.
                  const continuation = m.role === "assistant" && !streamingThis
                    ? messageMerge(messages, i, t)
                    : [];
                  // A message may contain only EDITS: the file
                  // to show is then the result, not what the message contains.
                  const changes = m.role === "assistant" && !streamingThis
                    ? applyEdits(messages, i, t)
                    : { files: [], failures: [] };
                  // The model returned « la partie corrigée » instead of the file.
                  const fragments = m.role === "assistant" && !streamingThis
                    ? messageFragments(messages, i, t)
                    : [];
                  // The stream broke AFTER delivering text: this is not a
                  // heuristic, it is an observed error. Without this flag, the
                  // answer stopped mid-word with nothing to explain it.
                  const networkCut = !!m.isError && m.role === "assistant"
                    && m.content.length > 200 && !streamingThis;
                  // A RESUME message only shows the rebuilt file. If
                  // there was nothing to complete, it shows no file: its
                  // content is the continuation of a text, not a deliverable. Without this test,
                  // the « fichier-2.txt » card (half a script) came back into the thread.
                  const isContinuation = m.role === "assistant" && !streamingThis
                    && isResume(messages[i - 1]);
                  const items = isContinuation
                    ? [...continuation, ...changes.files]
                    : [...(arts?.artifacts ?? []), ...changes.files];
                  // A file ending with </html> but whose script does not compile
                  // is unusable: nothing reported it, the page stayed blank.
                  const brokenFile = !streamingThis
                    ? items.find((a) => a.kind === "code" && brokenScript(a.content))
                    : undefined;
                  // When the model puts everything in the artifact and writes nothing
                  // outside, still show a short line in the chat (not an empty bubble).
                  const emptyMsg = items.some((a) => a.kind === "doc")
                    ? t("Voici le document — ouvre-le pour le lire ou le copier.")
                    : t("Voici le fichier — ouvre-le pour le copier.");
                  // While streaming, hide a half-written ```ask block (raw JSON) —
                  // the question UI appears once the block is complete.
                  // What is left to display in the chat during the stream: neither the
                  // half-written ```ask block (raw JSON), nor the file being
                  // written — the latter is written in the panel.
                  const streamingBody =
                    streamingThis && m.content.includes("```ask")
                      ? m.content.split("```ask")[0]
                      : streamingThis && m.content.includes("```edit")
                        ? m.content.split("```edit")[0]
                        : shownContent;
                  // With a question card, show only the model's short intro
                  // sentence (its first line) — never the questions/options text,
                  // which live in the interactive card.
                  const askIntro = ask ? (ask.prose.split("\n").map((s) => s.trim()).find(Boolean) ?? "").slice(0, 280) : "";
                  // The ```edit block itself has no business in the chat: we
                  // keep the explanation sentence, the result goes to a card.
                  // The ```edit block is ALWAYS removed from the chat, even when it
                  // could not be applied: it is protocol, not an answer. In
                  // case of failure, the banner below explains it.
                  const containsEdit = m.content.includes("```edit");
                  // A resume has no prose to show: all its content is
                  // the end of the file, already glued into the card.
                  const proseOutsideEdit = isContinuation && continuation.length
                    ? ""
                    : (arts?.prose ?? m.content)
                        .replace(/```edit[\s\S]*?(?:```|$)/g, "")
                        .trim();
                  const bodyText = ask
                    ? askIntro
                    : items.length
                      ? (proseOutsideEdit || emptyMsg)
                      : containsEdit && !streamingThis
                        ? (proseOutsideEdit || t("Modification proposée."))
                        : streamingBody;
                  // Block present but nothing applied and nothing reported: the parser
                  // could not read it at all. Must be said, otherwise the answer looks empty.
                  const unreadableEdit =
                    containsEdit && !streamingThis && !changes.files.length && !changes.failures.length;
                  // A document being streamed shows only a live-updating card in
                  // the chat (its raw text streams into the side panel instead).
                  // On a narrow screen there is no panel: the card would hide
                  // the very text being written, so we let it flow in the chat —
                  // exactly like a code file.
                  const streamingDoc =
                    streamingThis && m.role === "assistant" && m.content.length > 0 && !isNarrow &&
                    isDocTask(messages[i - 1]?.content ?? "");
                  return (
                  <ChatMessage key={i} sender={m.role}>
                    <ChatMessageBubble
                      variant={m.role === "user" ? "filled" : "ghost"}
                      className={m.role === "user" ? "question-bubble" : undefined}
                      /* The model name above the content, like LM Studio
                         (« google/gemma-4-e4b » under the question): it says WHO
                         is speaking, and it marks the start of the answer. */
                      name={
                        m.role === "assistant" ? (
                          <Text type="supporting" color="secondary">{model}</Text>
                        ) : undefined
                      }
                      metadata={
                        !isThinking && m.ts ? (
                          <ChatMessageMetadata
                            timestamp={<ClientTimestamp value={m.ts} format="time" />}
                            status={m.isError ? "error" : undefined}
                            footer={
                              m.role === "assistant" && (m.tokens || m.tokensPerSec || canRegenerateThis || (streamingThis && liveStats)) ? (
                                <HStack gap={1} vAlign="center">
                                  <Text type="supporting" color="secondary">
                                    {streamingThis && liveStats
                                      ? // During the stream: counted on the SSE deltas, hence « ~ ».
                                        // An automatic resume is announced: otherwise the answer
                                        // seems to come out of nowhere.
                                        (resume
                                          ? `${t("Reprise automatique")} ${resume}/${MAX_AUTO_RESUMES} · `
                                          : "") + `~${liveStats.tokens} tokens · ${liveStats.tps} tok/s`
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
                                    onClick={() => void copyText(m.content, () =>
                                      showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }))}
                                  />
                                  {canRegenerateThis && (
                                    <Button
                                      label={t("Régénérer")}
                                      variant="ghost"
                                      size="sm"
                                      isIconOnly
                                      icon={<Icon icon={ArrowPathIcon} size="sm" />}
                                      onClick={onRegenerate}
                                    />
                                  )}
                                </HStack>
                              ) : m.role === "user" ? (
                                <HStack gap={1} vAlign="center">
                                  {editingIdx === i && (
                                    <StatusDot variant="accent" isPulsing label={t("En modification")} />
                                  )}
                                  <Button
                                    label={t("Éditer")}
                                    variant="ghost"
                                    size="sm"
                                    isIconOnly
                                    icon={<Icon icon={PencilIcon} size="sm" />}
                                    onClick={() => onEditMessage(i)}
                                  />
                                  <Button
                                    label={t("Copier")}
                                    variant="ghost"
                                    size="sm"
                                    isIconOnly
                                    icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                                    onClick={() => void copyText(m.content, () =>
                                      showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }))}
                                  />
                                </HStack>
                              ) : undefined
                            }
                          />
                        ) : undefined
                      }>
                      {m.role === "user" && m.images?.length ? (
                        <HStack gap={1} wrap="wrap">
                          {m.images.map((src, k) => (
                            <Thumbnail
                              key={k}
                              src={src}
                              alt={t("Image jointe {n}").replace("{n}", String(k + 1))}
                              label={t("Image jointe {n}").replace("{n}", String(k + 1))}
                              onClick={() => onOpenImage({ srcs: m.images ?? [], index: k })}
                            />
                          ))}
                        </HStack>
                      ) : null}
                      {/* Tools at work: we say what is being searched, read or
                          generated, as it happens. Without it the wait is
                          silent for tens of seconds. */}
                      {streamingThis && webSteps.length > 0 && (
                        <VStack gap={1} padding={2}>
                          {webSteps.map((e, k) => {
                            const l = webStepLabel(e, t);
                            const media = e.step === "generation" ? "image"
                              : e.step === "generation_video" ? "video" : null;
                            return (
                              <VStack key={`web-${k}`} gap={2}>
                                <HStack gap={2} vAlign="center">
                                  <StatusDot variant={l.finished ? "success" : "accent"}
                                    isPulsing={!l.finished} label={l.text} />
                                  <Text type="supporting" color="secondary">{l.text}</Text>
                                </HStack>
                                {/* Generation in progress: the SAME tile as the
                                    Image page (square, photo) or Video page
                                    (16/9, film), not a spinner — it is what
                                    will take the result's place. ONE TILE PER
                                    announced IMAGE (the step's `count` field):
                                    four grey squares while four images bake is
                                    what the Image page shows, and a single
                                    square read as « nothing is happening ». */}
                                {!l.finished && media && (
                                  <HStack gap={2} wrap="wrap">
                                    {Array.from({ length: Math.max(1, Math.min(4, Number((e as { count?: number }).count) || 1)) })
                                      .map((_, p) => (
                                        <VStack key={`gen-${p}`} maxWidth={200} width="100%">
                                          <AspectRatio ratio={media === "image" ? 1 : 16 / 9} fit="contain">
                                            <GenerationPlaceholder media={media} />
                                          </AspectRatio>
                                        </VStack>
                                      ))}
                                  </HStack>
                                )}
                              </VStack>
                            );
                          })}
                        </VStack>
                      )}
                      {isThinking && webSteps.length === 0 ? (
                        <ThinkingIndicator fixedLabel={prevAttachments ? t("Lecture du fichier…") : undefined} />
                      ) : streamingDoc ? (
                        <ClickableCard
                          label={t("Ouvrir le document en cours de rédaction")}
                          variant="muted"
                          onClick={onOpenLiveDoc}>
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
                          <ReasoningBlock reasoning={m.reasoning || ""} ms={m.reasoningMs} />
                          {bodyText.trim() ? <Markdown>{bodyText}</Markdown> : null}
                          <AskQuestion
                            questions={ask.questions}
                            answered={!isLast}
                            onSubmit={(ans) =>
                              onAnswer(
                                ask.questions.length === 1
                                  ? ans[0]
                                  : ask.questions.map((q, k) => `${q.question}\n→ ${ans[k]}`).join("\n\n"),
                              )
                            }
                          />
                        </VStack>
                      ) : (
                        <VStack gap={2}>
                          <ReasoningBlock reasoning={m.reasoning || ""} ms={m.reasoningMs} streaming={streamingThis && !bodyText.trim()} />
                          {/* The short fade of the answer's FIRST word, when
                              there was reasoning: it is the exact moment the
                              reasoning block folds — the reasoning → writing
                              boundary, visible at last. */}
                          <Markdown
                            className={m.reasoning && bodyText.trim() ? "typing-appear" : undefined}
                            isStreaming={streamingThis}>
                            {bodyText || " "}
                          </Markdown>
                          {/* An edit whose anchor does not exist in the file is
                              NOT applied. We say so, rather than letting the
                              user believe the fix is done. */}
                          {fragments.length > 0 && (
                            <Banner
                              status="warning"
                              title={t("Seul un extrait a été renvoyé")}
                              description={t(
                                "Le modèle a donné la partie corrigée, pas le fichier complet. La version précédente reste ouverte dans le volet.",
                              )}
                              endContent={
                                isLast ? (
                                  <Button
                                    label={t("Demander le fichier complet")}
                                    variant="primary"
                                    size="sm"
                                    isDisabled={streaming}
                                    onClick={() => onRequestFullFile(fragments[0])}
                                  />
                                ) : undefined
                              }
                            />
                          )}
                          {(changes.failures.length > 0 || unreadableEdit) && (
                            <Banner
                              status="warning"
                              title={t("Modification non appliquée")}
                              description={
                                unreadableEdit
                                  ? t("La modification proposée n'a pas pu être lue. Redemande la correction, ou demande le fichier complet.")
                                  : t("Le texte à remplacer n'a pas été retrouvé dans le fichier. Demande la correction en précisant l'endroit, ou demande le fichier complet.")
                              }
                            />
                          )}
                          {brokenFile && (
                            <Banner
                              status="warning"
                              title={t("Fichier inutilisable")}
                              description={t("Le fichier se termine bien, mais son JavaScript ne compile pas — un bloc n'est jamais refermé, donc la page reste vide.")}
                              endContent={
                                isLast ? (
                                  <Button
                                    label={t("Refaire le fichier")}
                                    variant="primary"
                                    size="sm"
                                    isDisabled={streaming}
                                    onClick={() => onRedoFile(brokenFile.title)}
                                  />
                                ) : undefined
                              }
                            />
                          )}
                          {/* Cut by the token ceiling: without this message the
                              answer stops mid-word and nothing explains it. */}
                          {(m.truncated || networkCut || (!streamingThis && incompleteMessage(messages, i, t))) && !streamingThis && (
                            <Banner
                              status="warning"
                              title={t("Réponse coupée")}
                              description={
                                m.truncated
                                  ? t("Le plafond de tokens a été atteint. Reprends la suite, ou augmente « Max tokens » dans les réglages.")
                                  : networkCut
                                  ? t("La connexion s'est interrompue pendant la génération. Reprends la suite — ce qui est déjà écrit est conservé.")
                                  : t("Le fichier s'arrête avant sa fin et les reprises automatiques n'ont pas suffi. Relance la suite, ou demande-lui de l'écrire en plusieurs fichiers.")
                              }
                              endContent={
                                isLast ? (
                                  <Button
                                    label={t("Continuer")}
                                    variant="primary"
                                    size="sm"
                                    isDisabled={streaming}
                                    onClick={onContinue}
                                  />
                                ) : undefined
                              }
                            />
                          )}
                          {items.map((a, ai) => (
                            <ClickableCard
                              key={ai}
                              label={a.kind === "code" ? `${t("Ouvrir le fichier")} ${renamedTitle(a)}` : t("Ouvrir le document")}
                              variant="muted"
                              onClick={() => onOpenArtifact(a)}>
                              <HStack gap={2} vAlign="center">
                                <Icon icon={DocumentTextIcon} size="sm" color="secondary" />
                                <VStack gap={0}>
                                  <Text weight="semibold">{renamedTitle(a)}</Text>
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
}
