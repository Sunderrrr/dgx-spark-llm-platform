"use client";

// The playground composer: editing banner, no-key banner, queued-message panel,
// attachment drawer + dictation + model selector, skills menu, image lightbox
// and the hidden file input. Extracted VERBATIM from `composerNode` in
// app/(app)/playground/page.tsx (plain props, no behaviour change) — the page
// keeps the data layer (send/queue/attachment handlers, dictation hook).

import type { Dispatch, RefObject, SetStateAction } from "react";
import { Icon } from "@astryxdesign/core/Icon";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Card } from "@astryxdesign/core/Card";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Badge } from "@astryxdesign/core/Badge";
import { Banner } from "@astryxdesign/core/Banner";
import { Selector } from "@astryxdesign/core/Selector";
import { Token } from "@astryxdesign/core/Token";
import { Thumbnail } from "@astryxdesign/core/Thumbnail";
import { Lightbox } from "@astryxdesign/core/Lightbox";
import {
  ChatComposer,
  ChatComposerDrawer,
  ChatComposerInput,
} from "@astryxdesign/core/Chat";
import {
  PaperClipIcon,
  ClockIcon,
  PencilIcon,
  XMarkIcon,
  PaperAirplaneIcon,
  ArrowUpIcon,
  StopIcon,
  KeyIcon,
  BookmarkIcon,
  BoltIcon,
  SpeakerWaveIcon,
} from "@heroicons/react/24/outline";
import { BorderBeam } from "border-beam";
import { useResolvedMode } from "@/lib/use-system-dark";
import { VoiceBeam } from "voice-glow";
import { useT } from "@/lib/i18n";
import type { Attachment } from "@/lib/types";
import type { Dictation } from "@/lib/useDictation";
import type { Skill } from "@/lib/skills";
import type { SettingsSection } from "@/lib/settings-dialog";
import type { QueuedMsg } from "@/lib/playground-thread";
import { DictateButton } from "../../_components/DictateButton";
import { ContextRing } from "./ContextRing";
import { SkillsMenu } from "./SkillsMenu";

export type PlaygroundComposerProps = {
  input: string;
  handleInput: (v: string) => void;
  send: (value: string) => void;
  stop: () => void;
  streaming: boolean;
  editingIdx: number | null;
  setEditingIdx: Dispatch<SetStateAction<number | null>>;
  setInput: Dispatch<SetStateAction<string>>;
  hasKey: boolean | null;
  openSettings: (section?: SettingsSection) => void;
  queued: QueuedMsg[];
  editQueued: (idx: number) => void;
  sendQueuedNow: (idx: number) => void;
  discardQueued: (idx: number) => void;
  mode: "light" | "dark" | "system";
  dictation: Dictation;
  /** Opens the hands-free voice mode. */
  openVoice: () => void;
  composerRadius: number | undefined;
  placeholderText: string;
  attachments: Attachment[];
  setAttachments: Dispatch<SetStateAction<Attachment[]>>;
  handleFiles: (files: FileList | null) => void;
  fileInputRef: RefObject<HTMLInputElement | null>;
  fileAccept: string;
  modelVision: Record<string, boolean>;
  model: string;
  setModel: Dispatch<SetStateAction<string>>;
  runningModels: string[];
  slashQuery: string | null;
  /** Skill package attached to the next message (« / » menu). */
  invokedSkill: string | null;
  setInvokedSkill: (v: string | null) => void;
  baseHits: Skill[];
  customHits: Skill[];
  effectiveSel: number;
  selectSkill: (s: Skill) => void;
  openSkillCreator: (skill?: Skill) => void;
  deleteCustomSkill: (id: string) => void;
  viewedImage: { srcs: string[]; index: number } | null;
  setViewedImage: Dispatch<SetStateAction<{ srcs: string[]; index: number } | null>>;
  isNarrow: boolean;
  setSnippetsOpen: Dispatch<SetStateAction<boolean>>;
  setSkillCreatorOpen: Dispatch<SetStateAction<boolean>>;
  setCtxOpen: Dispatch<SetStateAction<boolean>>;
  used: number;
  max: number;
  hasMessages: boolean;
};

export function PlaygroundComposer({
  input,
  handleInput,
  send,
  stop,
  streaming,
  editingIdx,
  setEditingIdx,
  setInput,
  hasKey,
  openSettings,
  queued,
  editQueued,
  sendQueuedNow,
  discardQueued,
  mode,
  dictation,
  openVoice,
  composerRadius,
  placeholderText,
  attachments,
  setAttachments,
  handleFiles,
  fileInputRef,
  fileAccept,
  modelVision,
  model,
  setModel,
  runningModels,
  slashQuery,
  invokedSkill,
  setInvokedSkill,
  baseHits,
  customHits,
  effectiveSel,
  selectSkill,
  openSkillCreator,
  deleteCustomSkill,
  viewedImage,
  setViewedImage,
  isNarrow,
  setSnippetsOpen,
  setSkillCreatorOpen,
  setCtxOpen,
  used,
  max,
  hasMessages,
}: PlaygroundComposerProps) {
  const t = useT();
  // « system » resolved here, hydration-safe: border-beam resolves « auto » during
  // render, dark on the server and light in the browser (React #418).
  const resolvedMode = useResolvedMode(mode);
  return (
    <VStack gap={2} padding={4}>
      {/* In-place editing: the conversation stays on screen, and we say that
          the next send will branch from the edited message, with a cancel. */}
      {editingIdx !== null && (
        <Banner
          status="info"
          title={t("Message en cours de modification")}
          description={t("Le prochain envoi remplacera ce message et la suite de la conversation.")}
          endContent={
            <Button
              label={t("Annuler")}
              variant="ghost"
              size="sm"
              onClick={() => { setEditingIdx(null); setInput(""); }}
            />
          }
        />
      )}
      {/* The skill package the next message will use, removable. */}
      {invokedSkill ? (
        <Banner
          status="info"
          title={t("Compétence « {name} » jointe au prochain message").replace("{name}", invokedSkill)}
          endContent={
            <Button label={t("Retirer")} variant="ghost" size="sm" onClick={() => setInvokedSkill(null)} />
          }
        />
      ) : null}
      {/* Without an API key the playground cannot send anything: it runs on the
          user's key. We say so BEFORE the first question, with the button that
          leads straight to the right place — rather than letting an error
          message reveal the problem. */}
      {hasKey === false && (
        <Banner
          status="warning"
          title={t("Aucune clé API")}
          description={t(
            "Le playground consomme le budget de ton compte via ta clé API. Crée-en une pour pouvoir discuter avec le modèle.",
          )}
          endContent={
            <Button
              label={t("Créer une clé API")}
              variant="primary"
              size="sm"
              icon={<Icon icon={KeyIcon} size="sm" />}
              onClick={() => openSettings("keys")}
            />
          }
        />
      )}
      {/* Queue, right above the composer: messages typed during a generation
          wait here and leave on their own as soon as it ends. The actions only
          exist to skip the wait (Send), take the text back (Edit) or cancel
          (cross). */}
      {queued.length > 0 && (
        <Card
          variant="muted"
          padding={3}
          style={{ border: "var(--border-width) solid var(--color-border-emphasized)" }}>
          <VStack gap={2}>
            <HStack hAlign="between" vAlign="center" gap={2}>
              <HStack gap={2} vAlign="center">
                <Text weight="semibold">{t("Messages en attente")}</Text>
                <Badge label={String(queued.length)} variant="warning" />
              </HStack>
              <Icon icon={ClockIcon} size="sm" color="secondary" />
            </HStack>
            {queued.map((q, i) => (
              <HStack key={`queued-${q.ts}-${i}`} gap={2} vAlign="center">
                <StackItem size="fill">
                  <Text maxLines={1} color="secondary">{q.text || q.content}</Text>
                </StackItem>
                <Button
                  label={t("Modifier")}
                  variant="ghost"
                  size="sm"
                  icon={<Icon icon={PencilIcon} size="sm" />}
                  onClick={() => editQueued(i)}
                />
                <Button
                  label={t("Envoyer")}
                  variant="secondary"
                  size="sm"
                  icon={<Icon icon={PaperAirplaneIcon} size="sm" />}
                  onClick={() => sendQueuedNow(i)}
                />
                <Button
                  label={t("Retirer")}
                  variant="ghost"
                  size="sm"
                  isIconOnly
                  icon={<Icon icon={XMarkIcon} size="sm" />}
                  onClick={() => discardQueued(i)}
                />
              </HStack>
            ))}
            <Text type="supporting" color="secondary">
              {streaming
                ? t("Envoi automatique dès la fin de la réponse. « Envoyer » interrompt et passe à ce message.")
                : t("Envoi imminent…")}
            </Text>
          </VStack>
        </Card>
      )}
      {/* Two effects, two signals, ONE host: the composer card. The beam spins
          while the model works, the halo rises with the voice during dictation
          — at rest the composer stays plain, which is what makes them signals
          and not ornaments.

          Both wrap the card, not the text field: it is the library's « chat
          input » (its light grows from the bottom edge of its host) and the
          only element with a surface and a radius. The field is a transparent
          30 px ribbon without a radius: the light floated above the text, with
          the library's 16 px fallback corners inside a card of 28.

          `borderRadius` is passed by hand (see `composerRadius`): both
          libraries detect it on their FIRST CHILD, and border-beam's is its own
          `<style>` tag — 0 px, hence the 16 px fallback, corners visibly
          squarer than the card. The clipping of both wrappers is set in
          globals.css (`overflow: clip`): that, not the layers' `clip-path`,
          contains the lights. */}
      <BorderBeam active={streaming} theme={resolvedMode}
                  borderRadius={composerRadius}
                  className="composeur-cadre">
      <VoiceBeam stream={dictation.stream}
                 active={dictation.isRecording || dictation.isTranscribing}
                 processing={dictation.isTranscribing}
                 theme={resolvedMode}
                 borderRadius={composerRadius}
                 className="composeur-halo">
      <ChatComposer
        value={input}
        onChange={handleInput}
        onSubmit={send}
        isStopShown={streaming}
        onStop={stop}
        placeholder={placeholderText}
        input={
          <ChatComposerInput value={input} onChange={handleInput} onSubmit={send}
                                  // Pasting or dropping a file in the input:
                                  // the component calls `onFiles` — the page did not
                                  // pass it, so the event was swallowed without
                                  // saying anything (unconditional `preventDefault()`).
                                  onFiles={(files) => handleFiles(files as unknown as FileList)} />
        }
        drawer={
          attachments.length ? (
            <ChatComposerDrawer count={attachments.length} label={t("Fichiers joints")}>
              <VStack gap={2}>
                {attachments.some((f) => f.image) && (
                  <HStack gap={1} wrap="wrap">
                    {attachments.map((f, i) => f.image ? (
                      <Thumbnail
                        key={f.name + i}
                        src={f.image}
                        alt={f.name}
                        label={f.name}
                        onRemove={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                      />
                    ) : null)}
                  </HStack>
                )}
                {attachments.some((f) => !f.image) && (
                  <HStack gap={1} wrap="wrap">
                    {attachments.map((f, i) => f.image ? null : (
                      <Token
                        key={f.name + i}
                        label={`${f.name} (${Math.ceil(f.content.length / 1024)} Ko)`}
                        onRemove={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}
                      />
                    ))}
                  </HStack>
                )}
              </VStack>
            </ChatComposerDrawer>
          ) : undefined
        }
        footerActions={
          <Button
            label={modelVision[model] ? t("Joindre un fichier ou une image") : t("Joindre un fichier")}
            variant="ghost"
            size="sm"
            isIconOnly
            icon={<Icon icon={PaperClipIcon} size="sm" />}
            onClick={() => fileInputRef.current?.click()}
          />
        }
        sendActions={
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
        sendButton={
          streaming ? (
            <Button
              label={t("Arrêter")}
              variant="primary"
              isIconOnly
              size="md"
              icon={<Icon icon={StopIcon} size="sm" />}
              onClick={stop}
            />
          ) : input.trim().length > 0 || attachments.length > 0 ? (
            <Button
              label={t("Envoyer")}
              variant="primary"
              isIconOnly
              size="md"
              icon={<Icon icon={ArrowUpIcon} size="sm" />}
              onClick={() => send(input)}
            />
          ) : (
            <HStack gap={1} vAlign="center">
              <DictateButton dictation={dictation} isDisabled={false} size="md" />
              {/* Hands-free conversation: you talk, it answers aloud. */}
              <Button
                label={t("Mode vocal")}
                variant="secondary"
                isIconOnly
                size="md"
                icon={<Icon icon={SpeakerWaveIcon} size="sm" />}
                onClick={openVoice}
              />
            </HStack>
          )
        }
      />
      </VoiceBeam>
      </BorderBeam>
      {/* Skills menu: shown as soon as « / » is typed (to pick one), right
          under the field. */}
      {slashQuery !== null && (
        <SkillsMenu
          baseSkills={baseHits}
          customSkills={customHits}
          query={slashQuery}
          selectedIndex={effectiveSel}
          onSelect={selectSkill}
          onCreate={() => openSkillCreator()}
          onEdit={(s) => openSkillCreator(s)}
          onDelete={deleteCustomSkill}
        />
      )}
      {viewedImage && (
        <Lightbox
          isOpen
          onOpenChange={(o) => { if (!o) setViewedImage(null); }}
          media={viewedImage.srcs.map((src, k) => ({ src, alt: t("Image jointe {n}").replace("{n}", String(k + 1)) }))}
          index={viewedImage.index}
          onIndexChange={(index) => setViewedImage((v) => (v ? { ...v, index } : v))}
          hasZoom
        />
      )}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={fileAccept}
        style={{ display: "none" }}
        onChange={(e) => handleFiles(e.target.files)}
      />
      {/* The composer's bottom hint is for WIDE screens only. Measured at 390
          px: the buttons take 236 of the 326 px, leaving ~82 px for the text,
          which spreads over 5 lines — the row grows to 100 px, taller than the
          input card itself (94 px). On desktop the same text fits on one line
          (338 px, 28 px row). Nothing is lost on a phone: the file picker
          already filters by `accept`, and the account budget is shown on the
          home page. `hAlign` follows, otherwise the first child's disappearance
          would bring the buttons back to the left. */}
      {/* Once the conversation has started the hint is known: it stays on the
          empty screen only, and the row then keeps its tools on the right. */}
      <HStack hAlign={isNarrow || hasMessages ? "end" : "between"} gap={2}>
        {!isNarrow && !hasMessages && (
          <Text type="supporting" color="secondary">{t("Fichiers texte uniquement. Les tokens comptent sur ton budget.")}</Text>
        )}
        <HStack gap={2}>
          <Button
            label={t("Snippets")}
            variant="ghost"
            size="sm"
            icon={<Icon icon={BookmarkIcon} size="sm" />}
            onClick={() => setSnippetsOpen(true)}
          />
          <Button
            label={t("Compétences")}
            variant="ghost"
            size="sm"
            icon={<Icon icon={BoltIcon} size="sm" />}
            onClick={() => setSkillCreatorOpen(true)}
          />
          {hasMessages && (
            // Ring + words: alone, an empty ring read as a loading spinner
            // that never finished.
            <Button
              label={`${t("Contexte")} ${Math.round(Math.min(1, used / (max || 1)) * 100)} %`}
              variant="ghost"
              size="sm"
              icon={<ContextRing used={used} max={max} />}
              onClick={() => setCtxOpen(true)}
            />
          )}
        </HStack>
      </HStack>
    </VStack>
  );
}
