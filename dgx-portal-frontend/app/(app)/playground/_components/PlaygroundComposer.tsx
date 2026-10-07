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
} from "@heroicons/react/24/outline";
import { BorderBeam } from "border-beam";
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
  rayonComposeur: number | undefined;
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
  baseHits: Skill[];
  customHits: Skill[];
  effectiveSel: number;
  selectSkill: (s: Skill) => void;
  openSkillCreator: (skill?: Skill) => void;
  deleteCustomSkill: (id: string) => void;
  imageVue: { srcs: string[]; index: number } | null;
  setImageVue: Dispatch<SetStateAction<{ srcs: string[]; index: number } | null>>;
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
  rayonComposeur,
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
  baseHits,
  customHits,
  effectiveSel,
  selectSkill,
  openSkillCreator,
  deleteCustomSkill,
  imageVue,
  setImageVue,
  isNarrow,
  setSnippetsOpen,
  setSkillCreatorOpen,
  setCtxOpen,
  used,
  max,
  hasMessages,
}: PlaygroundComposerProps) {
  const t = useT();
  return (
    <VStack gap={2} padding={4}>
      {/* Édition en place : la conversation reste affichée, on signale que le
          prochain envoi rebranchera depuis le message édité, avec annulation. */}
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
      {/* Sans clé API, le playground ne peut rien envoyer : il tourne sur
          la clé de l'utilisateur. On le dit AVANT la première question,
          avec le bouton qui mène pile au bon endroit — plutôt que de
          laisser découvrir le problème par un message d'erreur. */}
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
      {/* File d'attente, juste au-dessus du compositeur : les messages
          tapés pendant une génération attendent ici et partent seuls dès
          qu'elle se termine. Les actions ne servent qu'à ne pas attendre
          (Envoyer), reprendre le texte (Modifier) ou annuler (croix). */}
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
      {/* Deux effets, deux signaux, UN SEUL hôte : la carte du composeur.
          Le faisceau tourne pendant que le modèle travaille, le halo monte avec
          la voix pendant la dictée — au repos, le composeur reste sobre, c'est
          ce qui en fait des signaux et pas des ornements.

          Les deux enveloppent la carte, et non le champ de saisie : c'est le
          « chat input » de la librairie (sa lumière naît du bord bas de son
          hôte) et le seul élément qui ait une surface et un rayon. Le champ,
          lui, est un ruban transparent de 30 px sans rayon : la lumière y
          flottait au-dessus du texte, avec les coins de 16 px du repli de la
          librairie dans une carte qui en fait 28.

          `borderRadius` est fourni à la main (cf. `rayonComposeur`) : la
          détection automatique des deux librairies lit leur PREMIER ENFANT, et
          celui de `border-beam` est sa propre balise `<style>` — 0 px, donc le
          repli de 16 px, coins visiblement plus carrés que la carte. Le rognage
          des deux enveloppes, lui, est réglé dans globals.css (`overflow: clip`
          élargi de 8 px) : c'est lui, et non le `clip-path` des calques, qui
          contient les lumières. */}
      <BorderBeam active={streaming} theme={mode === "system" ? "auto" : mode}
                  borderRadius={rayonComposeur}
                  className="composeur-cadre">
      <VoiceBeam stream={dictation.stream}
                 active={dictation.isRecording || dictation.isTranscribing}
                 processing={dictation.isTranscribing}
                 theme={mode === "system" ? "auto" : mode}
                 borderRadius={rayonComposeur}
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
            <DictateButton dictation={dictation} isDisabled={false} size="md" />
          )
        }
      />
      </VoiceBeam>
      </BorderBeam>
      {/* Menu des compétences : affiché dès qu'on tape « / » (pour en
          sélectionner une) juste sous le champ. */}
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
      {imageVue && (
        <Lightbox
          isOpen
          onOpenChange={(o) => { if (!o) setImageVue(null); }}
          media={imageVue.srcs.map((src, k) => ({ src, alt: t("Image jointe {n}").replace("{n}", String(k + 1)) }))}
          index={imageVue.index}
          onIndexChange={(index) => setImageVue((v) => (v ? { ...v, index } : v))}
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
      {/* L'indice de bas de composeur est réservé aux écrans LARGES. Mesuré au
          gabarit 390 px : les boutons en occupent 236 des 326 px, il ne reste
          que ~82 px au texte, qui s'y étale sur 5 lignes — la rangée monte à
          100 px de haut, plus que la carte de saisie elle-même (94 px). Sur
          bureau le même texte tient sur une ligne (338 px, rangée de 28 px).
          Rien n'est perdu sur téléphone : le sélecteur de fichiers filtre déjà
          par `accept`, et le budget du compte est affiché sur l'accueil.
          `hAlign` suit, sinon la disparition du premier enfant ramènerait les
          boutons à gauche. */}
      <HStack hAlign={isNarrow ? "end" : "between"} gap={2}>
        {!isNarrow && (
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
            <Button
              label={t("Fenêtre de contexte")}
              variant="ghost"
              size="sm"
              isIconOnly
              icon={<ContextRing used={used} max={max} />}
              onClick={() => setCtxOpen(true)}
            />
          )}
        </HStack>
      </HStack>
    </VStack>
  );
}
