"use client";

// « Bloquer ce compte » dialog of the admin users section. Extracted VERBATIM
// from app/(app)/admin/_components/UsersSection.tsx (plain props, no behaviour
// change).

import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent, LayoutFooter } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Button } from "@astryxdesign/core/Button";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useT } from "@/lib/i18n";
import { useIsNarrow } from "@/lib/useIsNarrow";
import type { LocalUser } from "./shared";
import type { Dispatch, SetStateAction } from "react";

export type BlockAccountDialogProps = {
  blockUser: LocalUser | null;
  setBlockUser: (v: LocalUser | null) => void;
  blockReason: string;
  setBlockReason: Dispatch<SetStateAction<string>>;
  submitBlock: () => void;
  blockBusy: boolean;
};

export function BlockAccountDialog({
  blockUser,
  setBlockUser,
  blockReason,
  setBlockReason,
  submitBlock,
  blockBusy,
}: BlockAccountDialogProps) {
  const t = useT();
  const isNarrow = useIsNarrow();
  return (
    <>
      {/* Block dialog — the reason is optional but requested */}
      <Dialog isOpen={blockUser != null} onOpenChange={(o) => { if (!o) setBlockUser(null); }} purpose="form" width={isNarrow ? "94vw" : 440}>
        <Layout
          header={<DialogHeader title={t("Bloquer ce compte")} subtitle={blockUser?.username} hasDivider onOpenChange={(o) => { if (!o) setBlockUser(null); }} />}
          content={
            <LayoutContent padding={4}>
              <VStack gap={3}>
                <Text type="supporting" color="secondary">
                  {t("Le compte sera refusé au login, quelle que soit la source d'authentification, et ses sessions actives seront révoquées.")}
                </Text>
                <TextInput label={t("Raison (optionnel)")} value={blockReason} onChange={setBlockReason} />
              </VStack>
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" onClick={() => setBlockUser(null)} />
                <Button label={t("Bloquer")} variant="destructive" onClick={submitBlock} isLoading={blockBusy} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>
    </>
  );
}
