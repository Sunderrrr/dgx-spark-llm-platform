"use client";

// The playground header actions: new conversation, history, export, share,
// settings, auto-title, summary, context. Extracted VERBATIM from
// app/(app)/playground/page.tsx (plain props, no behaviour change).

import { HStack } from "@astryxdesign/core/Stack";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import {
  ArrowDownTrayIcon,
  ClockIcon,
  Cog6ToothIcon,
  DocumentMagnifyingGlassIcon,
  DocumentTextIcon,
  LinkIcon,
  PlusIcon,
  SparklesIcon,
} from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { convTitleFallback, type ExportConversation } from "@/lib/export";
import type { ChatMsg } from "@/lib/types";

export type PlaygroundActionsProps = {
  streaming: boolean;
  newConversation: () => void;
  setHistoryOpen: (v: boolean) => void;
  exportConversation: (conv: ExportConversation, fmt: "md" | "json") => void;
  messages: ChatMsg[];
  model: string;
  shared: boolean;
  currentId: string | null;
  shareConversation: (id: string) => void;
  toggleSettings: (el: HTMLElement | null) => void;
  busyTitle: boolean;
  genTitle: () => void;
  genSummary: () => void;
  setCtxOpen: (v: boolean) => void;
};

export function PlaygroundActions({
  streaming,
  newConversation,
  setHistoryOpen,
  exportConversation,
  messages,
  model,
  shared,
  currentId,
  shareConversation,
  toggleSettings,
  busyTitle,
  genTitle,
  genSummary,
  setCtxOpen,
}: PlaygroundActionsProps) {
  const t = useT();
  return (
            <HStack gap={2}>
              <Button
                label={t("Nouvelle conversation")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={PlusIcon} size="sm" />}
                isIconOnly
                // `newConversation` bails out while an answer is generating: a
                // active button with a silent click made it look like a failure. We say
                // the unavailability instead of ignoring it — like « Nouvel onglet ».
                isDisabled={streaming}
                onClick={newConversation}
              />
              <Button
                label={t("Historique")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={ClockIcon} size="sm" />}
                onClick={() => setHistoryOpen(true)}
              />
              <Button
                label={t("Exporter")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={ArrowDownTrayIcon} size="sm" />}
                isIconOnly
                onClick={() => exportConversation({ title: convTitleFallback(messages, t("Conversation")), model, messages }, "md")}
              />
              <Button
                label={shared ? t("Lien copié") : t("Partager")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={LinkIcon} size="sm" />}
                isIconOnly
                isDisabled={!currentId}
                onClick={() => currentId && shareConversation(currentId)}
              />
              {/* Réglages : panneau ancré SOUS la roue crantée, en overlay
                  fixe — la page ne bouge pas (voir toggleSettings). */}
              <Button
                label={t("Réglages")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={Cog6ToothIcon} size="sm" />}
                isIconOnly
                onClick={(e) => toggleSettings(e.currentTarget)}
              />
              <Button
                label={t("Titrer automatiquement")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={SparklesIcon} size="sm" />}
                isIconOnly
                isDisabled={busyTitle || !currentId || !messages.length}
                onClick={genTitle}
              />
              <Button
                label={t("Résumer")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={DocumentTextIcon} size="sm" />}
                isIconOnly
                isDisabled={!messages.length}
                onClick={genSummary}
              />
              <Button
                label={t("Contexte")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={DocumentMagnifyingGlassIcon} size="sm" />}
                isIconOnly
                onClick={() => setCtxOpen(true)}
              />
            </HStack>
  );
}
