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
import { Timestamp } from "@astryxdesign/core/Timestamp";
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
import { MarkdownSur as Markdown } from "@/lib/markdown";
import { useT } from "@/lib/i18n";
import { copierTexte } from "@/lib/copier";
import { ReasoningBlock } from "../../_components/ReasoningBlock";
import { ThinkingIndicator } from "../../_components/ThinkingIndicator";
import { GenerationPlaceholder } from "../../_components/GenerationPlaceholder";
import type { ChatMsg } from "@/lib/types";
import type { EtapeWeb } from "@/lib/api";
import { parseAsk, contenuCloture } from "@/lib/playground-parsers";
import {
  MAX_REPRISES_AUTO,
  appliquerEdits,
  docTitleFromContent,
  estReprise,
  isDocTask,
  fragmentsDuMessage,
  fusionDuMessage,
  libelleEtapeWeb,
  messageIncomplet,
  parseArtifacts,
  scriptCasse,
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
  etapesWeb: EtapeWeb[];
  editingIdx: number | null;
  liveStats: { tokens: number; tps: number } | null;
  reprise: number;
  model: string;
  renamedTitle: (a: Artifact) => string;
  onAnswer: (text: string) => void;
  onDemanderFichierComplet: (nom: string) => void;
  onRefaireFichier: (nom: string) => void;
  onContinuer: () => void;
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
  etapesWeb,
  editingIdx,
  liveStats,
  reprise,
  model,
  renamedTitle,
  onAnswer,
  onDemanderFichierComplet,
  onRefaireFichier,
  onContinuer,
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
                  const contenuAffiche =
                    streamingThis && isLast && liveCode ? m.content.slice(0, liveCode.start) : m.content;
                  const arts = m.role === "assistant" && !ask
                    ? parseArtifacts(
                        // Never-closed fence: we close it, otherwise the
                        // cut file stays as raw code in the middle of the bubble.
                        streamingThis ? contenuAffiche : contenuCloture(contenuAffiche),
                        !streamingThis && isDocTask(messages[i - 1]?.content ?? ""), t)
                    : null;
                  // Resume message: it only carries the end of the file.
                  const suite = m.role === "assistant" && !streamingThis
                    ? fusionDuMessage(messages, i, t)
                    : [];
                  // A message may contain only EDITS: the file
                  // to show is then the result, not what the message contains.
                  const modifs = m.role === "assistant" && !streamingThis
                    ? appliquerEdits(messages, i, t)
                    : { fichiers: [], echecs: [] };
                  // The model returned « la partie corrigée » instead of the file.
                  const fragments = m.role === "assistant" && !streamingThis
                    ? fragmentsDuMessage(messages, i, t)
                    : [];
                  // The stream broke AFTER delivering text: this is not a
                  // heuristic, it is an observed error. Without this flag, the
                  // answer stopped mid-word with nothing to explain it.
                  const coupeReseau = !!m.isError && m.role === "assistant"
                    && m.content.length > 200 && !streamingThis;
                  // A RESUME message only shows the rebuilt file. If
                  // there was nothing to complete, it shows no file: its
                  // content is the continuation of a text, not a deliverable. Without this test,
                  // the « fichier-2.txt » card (half a script) came back into the thread.
                  const estSuite = m.role === "assistant" && !streamingThis
                    && estReprise(messages[i - 1]);
                  const items = estSuite
                    ? [...suite, ...modifs.fichiers]
                    : [...(arts?.artifacts ?? []), ...modifs.fichiers];
                  // A file ending with </html> but whose script does not compile
                  // is unusable: nothing reported it, the page stayed blank.
                  const fichierCasse = !streamingThis
                    ? items.find((a) => a.kind === "code" && scriptCasse(a.content))
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
                        : contenuAffiche;
                  // With a question card, show only the model's short intro
                  // sentence (its first line) — never the questions/options text,
                  // which live in the interactive card.
                  const askIntro = ask ? (ask.prose.split("\n").map((s) => s.trim()).find(Boolean) ?? "").slice(0, 280) : "";
                  // The ```edit block itself has no business in the chat: we
                  // keep the explanation sentence, the result goes to a card.
                  // The ```edit block is ALWAYS removed from the chat, even when it
                  // could not be applied: it is protocol, not an answer. In
                  // case of failure, the banner below explains it.
                  const contientEdit = m.content.includes("```edit");
                  // A resume has no prose to show: all its content is
                  // the end of the file, already glued into the card.
                  const proseHorsEdit = estSuite && suite.length
                    ? ""
                    : (arts?.prose ?? m.content)
                        .replace(/```edit[\s\S]*?(?:```|$)/g, "")
                        .trim();
                  const bodyText = ask
                    ? askIntro
                    : items.length
                      ? (proseHorsEdit || emptyMsg)
                      : contientEdit && !streamingThis
                        ? (proseHorsEdit || t("Modification proposée."))
                        : streamingBody;
                  // Block present but nothing applied and nothing reported: the parser
                  // could not read it at all. Must be said, otherwise the answer looks empty.
                  const editIllisible =
                    contientEdit && !streamingThis && !modifs.fichiers.length && !modifs.echecs.length;
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
                      className={m.role === "user" ? "bulle-question" : undefined}
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
                            timestamp={<Timestamp value={m.ts} format="time" />}
                            status={m.isError ? "error" : undefined}
                            footer={
                              m.role === "assistant" && (m.tokens || m.tokensPerSec || canRegenerateThis || (streamingThis && liveStats)) ? (
                                <HStack gap={1} vAlign="center">
                                  <Text type="supporting" color="secondary">
                                    {streamingThis && liveStats
                                      ? // During the stream: counted on the SSE deltas, hence « ~ ».
                                        // An automatic resume is announced: otherwise the answer
                                        // seems to come out of nowhere.
                                        (reprise
                                          ? `${t("Reprise automatique")} ${reprise}/${MAX_REPRISES_AUTO} · `
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
                                    onClick={() => void copierTexte(m.content, () =>
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
                                    onClick={() => void copierTexte(m.content, () =>
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
                      {/* Outils en cours : on dit ce qui est cherché, lu ou
                          généré, au fil de l'eau. Sans ça l'attente est muette
                          pendant des dizaines de secondes. */}
                      {streamingThis && etapesWeb.length > 0 && (
                        <VStack gap={1} padding={2}>
                          {etapesWeb.map((e, k) => {
                            const l = libelleEtapeWeb(e, t);
                            const media = e.etape === "generation" ? "image"
                              : e.etape === "generation_video" ? "video" : null;
                            return (
                              <VStack key={`web-${k}`} gap={2}>
                                <HStack gap={2} vAlign="center">
                                  <StatusDot variant={l.fini ? "success" : "accent"}
                                    isPulsing={!l.fini} label={l.texte} />
                                  <Text type="supporting" color="secondary">{l.texte}</Text>
                                </HStack>
                                {/* Génération en cours : le MÊME pavé que la page
                                    Image (carré, photo) ou Vidéo (16/9, pellicule),
                                    pas un spinner — c'est lui qui occupera la place
                                    du résultat. UN PAVÉ PAR IMAGE annoncée (le champ
                                    `nombre` de l'étape) : quatre carrés gris pendant
                                    quatre images en cuisson, c'est ce que la page
                                    Image montre, et un seul carré se lisait comme
                                    « rien ne se passe ». */}
                                {!l.fini && media && (
                                  <HStack gap={2} wrap="wrap">
                                    {Array.from({ length: Math.max(1, Math.min(4, Number((e as { nombre?: number }).nombre) || 1)) })
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
                      {isThinking && etapesWeb.length === 0 ? (
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
                          {/* Le fondu court du PREMIER mot de la réponse, quand
                              il y a eu une réflexion : c'est le moment exact où
                              le bloc de réflexion se replie — la frontière
                              réflexion → écriture, enfin visible. */}
                          <Markdown
                            className={m.reasoning && bodyText.trim() ? "apparition-ecriture" : undefined}
                            isStreaming={streamingThis}>
                            {bodyText || " "}
                          </Markdown>
                          {/* Une modification dont l'ancre n'existe pas dans le
                              fichier ne s'applique PAS. On le dit, plutôt que de
                              laisser croire que la correction est faite. */}
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
                                    onClick={() => onDemanderFichierComplet(fragments[0])}
                                  />
                                ) : undefined
                              }
                            />
                          )}
                          {(modifs.echecs.length > 0 || editIllisible) && (
                            <Banner
                              status="warning"
                              title={t("Modification non appliquée")}
                              description={
                                editIllisible
                                  ? t("La modification proposée n'a pas pu être lue. Redemande la correction, ou demande le fichier complet.")
                                  : t("Le texte à remplacer n'a pas été retrouvé dans le fichier. Demande la correction en précisant l'endroit, ou demande le fichier complet.")
                              }
                            />
                          )}
                          {fichierCasse && (
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
                                    onClick={() => onRefaireFichier(fichierCasse.title)}
                                  />
                                ) : undefined
                              }
                            />
                          )}
                          {/* Coupé par le plafond de tokens : sans ce message, la
                              réponse s'arrête en plein mot et rien ne l'explique. */}
                          {(m.truncated || coupeReseau || (!streamingThis && messageIncomplet(messages, i, t))) && !streamingThis && (
                            <Banner
                              status="warning"
                              title={t("Réponse coupée")}
                              description={
                                m.truncated
                                  ? t("Le plafond de tokens a été atteint. Reprends la suite, ou augmente « Max tokens » dans les réglages.")
                                  : coupeReseau
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
                                    onClick={onContinuer}
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
