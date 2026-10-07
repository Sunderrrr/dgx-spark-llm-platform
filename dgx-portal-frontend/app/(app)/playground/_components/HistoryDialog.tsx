"use client";

// The conversation history dialog: one row per conversation, trash at the
// end. Extracted VERBATIM from app/(app)/playground/page.tsx (plain props, no
// behaviour change) — the page keeps the list computation and the mutations.

import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Timestamp } from "@astryxdesign/core/Timestamp";
import { ClickableCard } from "@astryxdesign/core/ClickableCard";
import {
  CheckIcon,
  ClipboardDocumentIcon,
  LinkIcon,
  PencilIcon,
  TrashIcon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
import StarSolidIcon from "@heroicons/react/24/solid/StarIcon";
import { StarIcon } from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import type { Conversation } from "@/lib/types";
import type { ExportConversation } from "@/lib/export";

export type HistoryDialogProps = {
  historyOpen: boolean;
  setHistoryOpen: (v: boolean) => void;
  count: number;
  histQuery: string;
  setHistQuery: (v: string) => void;
  q: string;
  visibleConvs: Conversation[];
  currentId: string | null;
  renamingId: string | null;
  setRenamingId: (v: string | null) => void;
  renameValue: string;
  setRenameValue: (v: string) => void;
  commitRename: (id: string) => void;
  pinnedIds: string[];
  togglePinned: (id: string) => void;
  startRename: (id: string, title: string) => void;
  selectConversation: (conv: Conversation) => void;
  exportConversation: (conv: ExportConversation, fmt: "md" | "json") => void;
  shareConversation: (id: string) => void;
  deleteConversation: (id: string) => void;
};

export function HistoryDialog({
  historyOpen,
  setHistoryOpen,
  count,
  histQuery,
  setHistQuery,
  q,
  visibleConvs,
  currentId,
  renamingId,
  setRenamingId,
  renameValue,
  setRenameValue,
  commitRename,
  pinnedIds,
  togglePinned,
  startRename,
  selectConversation,
  exportConversation,
  shareConversation,
  deleteConversation,
}: HistoryDialogProps) {
  const t = useT();
  return (
          <Dialog isOpen={historyOpen} onOpenChange={(o) => { if (!o) setHistoryOpen(false); }} width={560}>
            <DialogHeader
              title={t("Historique")}
              subtitle={`${count} ${t("conversations")}`}
              hasDivider
              onOpenChange={(o) => { if (!o) setHistoryOpen(false); }}
            />
            <VStack padding={3} gap={2}>
              <TextInput
                label={t("Rechercher")}
                isLabelHidden
                size="sm"
                value={histQuery}
                onChange={setHistQuery}
                placeholder={t("Rechercher une conversation")}
              />
              <VStack gap={1} height={470} isScrollable>
              {visibleConvs.length === 0 ? (
                <Text color="secondary">{q ? t("Aucun résultat") : t("Aucune conversation")}</Text>
              ) : (
                visibleConvs.map((conv) =>
                  renamingId === conv.id ? (
                    <HStack key={conv.id} gap={2} vAlign="center">
                      <StackItem size="fill">
                        <TextInput
                          label={t("Renommer")}
                          isLabelHidden
                          value={renameValue}
                          onChange={setRenameValue}
                          size="sm"
                          hasAutoFocus
                          onEnter={() => commitRename(conv.id)}
                        />
                      </StackItem>
                      <Button label={t("Valider")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={CheckIcon} size="sm" />} onClick={() => commitRename(conv.id)} />
                      <Button label={t("Annuler")} variant="ghost" size="sm" isIconOnly icon={<Icon icon={XMarkIcon} size="sm" />} onClick={() => setRenamingId(null)} />
                    </HStack>
                  ) : (
                    <HStack key={conv.id} gap={2} vAlign="center">
                      <StackItem size="fill">
                        <ClickableCard
                          label={conv.title || t("Conversation")}
                          variant={conv.id === currentId ? "default" : "muted"}
                          onClick={() => { selectConversation(conv); setHistoryOpen(false); }}>
                          <VStack gap={0}>
                            <Text maxLines={1}>{conv.title || t("Conversation")}</Text>
                            <Text type="supporting" color="secondary">
                              <Timestamp value={conv.ts} format="date_time" />
                            </Text>
                          </VStack>
                        </ClickableCard>
                      </StackItem>
                      <Button
                        label={pinnedIds.includes(conv.id) ? t("Désépingler") : t("Épingler")}
                        variant="ghost"
                        size="sm"
                        isIconOnly
                        icon={<Icon icon={pinnedIds.includes(conv.id) ? StarSolidIcon : StarIcon} size="sm" color={pinnedIds.includes(conv.id) ? "accent" : "inherit"} />}
                        onClick={() => togglePinned(conv.id)}
                      />
                      <Button
                        label={t("Renommer")}
                        variant="ghost"
                        size="sm"
                        isIconOnly
                        icon={<Icon icon={PencilIcon} size="sm" />}
                        onClick={() => startRename(conv.id, conv.title)}
                      />
                      <Button
                        label={t("Exporter JSON")}
                        variant="ghost"
                        size="sm"
                        isIconOnly
                        icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                        onClick={() => exportConversation({ title: conv.title || t("Conversation"), model: conv.model || "", messages: conv.messages }, "json")}
                      />
                      <Button
                        label={t("Partager")}
                        variant="ghost"
                        size="sm"
                        isIconOnly
                        icon={<Icon icon={LinkIcon} size="sm" />}
                        onClick={() => shareConversation(conv.id)}
                      />
                      <Button
                        label={t("Supprimer")}
                        variant="ghost"
                        size="sm"
                        isIconOnly
                        icon={<Icon icon={TrashIcon} size="sm" />}
                        onClick={() => deleteConversation(conv.id)}
                      />
                    </HStack>
                  ),
                )
              )}
              </VStack>
            </VStack>
          </Dialog>
  );
}
