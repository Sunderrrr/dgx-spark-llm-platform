"use client";

// The playground header actions: new conversation, history and settings in
// sight; export, share, auto-title, summary and context in the « ⋯ » menu.

import { HStack } from "@astryxdesign/core/Stack";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { MoreMenu } from "@astryxdesign/core/MoreMenu";
import type { DropdownMenuOption } from "@astryxdesign/core/DropdownMenu";
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
  // Three actions stay in sight (new, history, settings); the occasional ones
  // go behind « ⋯ » with a LABEL each — five bare icons side by side (export,
  // share, auto-title, summary, context) could not be told apart.
  const more: DropdownMenuOption[] = [
    {
      label: t("Exporter en Markdown"),
      icon: ArrowDownTrayIcon,
      isDisabled: !messages.length,
      onClick: () => exportConversation({ title: convTitleFallback(messages, t("Conversation")), model, messages }, "md"),
    },
    {
      label: shared ? t("Lien copié") : t("Copier le lien de partage"),
      icon: LinkIcon,
      isDisabled: !currentId,
      onClick: () => currentId && shareConversation(currentId),
    },
    { type: "divider" },
    {
      label: t("Titrer automatiquement"),
      icon: SparklesIcon,
      isDisabled: busyTitle || !currentId || !messages.length,
      onClick: genTitle,
    },
    {
      label: t("Résumer"),
      icon: DocumentTextIcon,
      isDisabled: !messages.length,
      onClick: genSummary,
    },
    {
      label: t("Contexte"),
      icon: DocumentMagnifyingGlassIcon,
      onClick: () => setCtxOpen(true),
    },
  ];
  return (
    <HStack gap={2}>
      <Button
        label={t("Nouvelle conversation")}
        variant="secondary"
        size="sm"
        icon={<Icon icon={PlusIcon} size="sm" />}
        // `newConversation` bails out while an answer is generating: an
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
      {/* Settings: a panel anchored UNDER the gear, as a fixed overlay
          — the page does not move (see toggleSettings). */}
      <Button
        label={t("Réglages")}
        variant="secondary"
        size="sm"
        icon={<Icon icon={Cog6ToothIcon} size="sm" />}
        isIconOnly
        onClick={(e) => toggleSettings(e.currentTarget)}
      />
      <MoreMenu label={t("Plus d'actions")} variant="secondary" size="sm" items={more} />
    </HStack>
  );
}
