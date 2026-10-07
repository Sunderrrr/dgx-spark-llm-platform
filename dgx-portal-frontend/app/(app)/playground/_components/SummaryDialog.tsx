"use client";

// The generated conversation summary dialog. Extracted VERBATIM from
// app/(app)/playground/page.tsx (plain props, no behaviour change).

import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { useToast } from "@astryxdesign/core/Toast";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { ClipboardDocumentIcon } from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { copierTexte } from "@/lib/copier";

export type SummaryDialogProps = {
  summaryOpen: boolean;
  setSummaryOpen: (v: boolean) => void;
  summary: string;
};

export function SummaryDialog({ summaryOpen, setSummaryOpen, summary }: SummaryDialogProps) {
  const t = useT();
  const showToast = useToast();
  return (
          <Dialog isOpen={summaryOpen} onOpenChange={(o) => { if (!o) setSummaryOpen(false); }} width={560}>
            <DialogHeader
              title={t("Résumé de la conversation")}
              hasDivider
              onOpenChange={(o) => { if (!o) setSummaryOpen(false); }}
            />
            <VStack padding={3} gap={3}>
              <Text type="supporting" color="secondary">{summary}</Text>
              <HStack hAlign="end" gap={2}>
                <Button
                  label={t("Copier")}
                  variant="secondary"
                  size="sm"
                  icon={<Icon icon={ClipboardDocumentIcon} size="sm" />}
                  onClick={() => void copierTexte(summary, () =>
                    showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }))}
                />
              </HStack>
            </VStack>
          </Dialog>
  );
}
