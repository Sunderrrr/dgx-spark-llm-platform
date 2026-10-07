"use client";

// Reusable prompts (snippets) dialog. Extracted VERBATIM from
// app/(app)/playground/page.tsx (plain props, no behaviour change).

import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { ClickableCard } from "@astryxdesign/core/ClickableCard";
import { PlusIcon, TrashIcon } from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import type { Snippet } from "@/lib/playground-thread";

export type SnippetsDialogProps = {
  snippetsOpen: boolean;
  setSnippetsOpen: (v: boolean) => void;
  input: string;
  saveSnippet: () => void;
  snippets: Snippet[];
  insertSnippet: (content: string) => void;
  deleteSnippet: (id: string) => void;
};

export function SnippetsDialog({
  snippetsOpen,
  setSnippetsOpen,
  input,
  saveSnippet,
  snippets,
  insertSnippet,
  deleteSnippet,
}: SnippetsDialogProps) {
  const t = useT();
  return (
          <Dialog isOpen={snippetsOpen} onOpenChange={(o) => { if (!o) setSnippetsOpen(false); }} width={480}>
            <DialogHeader
              title={t("Snippets")}
              subtitle={t("Prompts réutilisables")}
              hasDivider
              onOpenChange={(o) => { if (!o) setSnippetsOpen(false); }}
            />
            <VStack padding={3} gap={3}>
              <Button
                label={t("Enregistrer le prompt courant en snippet")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={PlusIcon} size="sm" />}
                isDisabled={!input.trim()}
                onClick={saveSnippet}
              />
              {snippets.length === 0 ? (
                <Text color="secondary">{t("Aucun snippet")}</Text>
              ) : (
                <VStack gap={1} height={380} isScrollable>
                  {snippets.map((s) => (
                    <HStack key={s.id} gap={2} vAlign="center">
                      <StackItem size="fill">
                        <ClickableCard label={s.label} variant="muted" onClick={() => insertSnippet(s.content)}>
                          <Text maxLines={2} type="supporting" color="secondary">{s.content}</Text>
                        </ClickableCard>
                      </StackItem>
                      <Button
                        label={t("Supprimer")}
                        variant="ghost"
                        size="sm"
                        isIconOnly
                        icon={<Icon icon={TrashIcon} size="sm" />}
                        onClick={() => deleteSnippet(s.id)}
                      />
                    </HStack>
                  ))}
                </VStack>
              )}
            </VStack>
          </Dialog>
  );
}
