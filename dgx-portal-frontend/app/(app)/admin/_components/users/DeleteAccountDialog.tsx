"use client";

// « Supprimer ce compte » dialog of the admin users section (destructive,
// typed DELETE confirmation). Extracted VERBATIM from
// app/(app)/admin/_components/UsersSection.tsx (plain props, no behaviour
// change).

import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent, LayoutFooter } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Button } from "@astryxdesign/core/Button";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Banner } from "@astryxdesign/core/Banner";
import { useT } from "@/lib/i18n";
import { useIsNarrow } from "@/lib/useIsNarrow";
import type { LocalUser } from "./shared";
import type { Dispatch, SetStateAction } from "react";

export type DeleteAccountDialogProps = {
  delUser: LocalUser | null;
  setDelUser: (v: LocalUser | null) => void;
  delWarning: string | null;
  setDelWarning: (v: string | null) => void;
  delConfirm: string;
  setDelConfirm: Dispatch<SetStateAction<string>>;
  delError: string | null;
  submitDelete: () => void;
  delBusy: boolean;
};

export function DeleteAccountDialog({
  delUser,
  setDelUser,
  delWarning,
  setDelWarning,
  delConfirm,
  setDelConfirm,
  delError,
  submitDelete,
  delBusy,
}: DeleteAccountDialogProps) {
  const t = useT();
  const isNarrow = useIsNarrow();
  return (
    <>
      {/* Delete dialog — destructive: confirmation by typing DELETE */}
      <Dialog isOpen={delUser != null} onOpenChange={(o) => { if (!o) { setDelUser(null); setDelWarning(null); } }} purpose="form" width={isNarrow ? "94vw" : 480}>
        <Layout
          header={<DialogHeader title={t("Supprimer ce compte")} subtitle={delUser?.username} hasDivider onOpenChange={(o) => { if (!o) { setDelUser(null); setDelWarning(null); } }} />}
          content={
            <LayoutContent padding={4}>
              <VStack gap={3}>
                <Text type="supporting" color="secondary">
                  {t("Cette action est définitive : elle révoque les clés API et les sessions du compte, supprime son enveloppe LiteLLM ET ses données personnelles (mémoires, conversations, préférences, passkeys).")}
                </Text>
                {/* Success warning: some keys could not be revoked
                    on the LiteLLM side — to be read, never silent. */}
                {delWarning && <Banner status="warning" title={delWarning} />}
                <TextInput label={t("Tapez DELETE pour confirmer")} value={delConfirm} onChange={setDelConfirm} />
                {delError && <Banner status="error" title={delError} />}
              </VStack>
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" onClick={() => { setDelUser(null); setDelWarning(null); }} />
                <Button label={t("Supprimer définitivement")} variant="destructive" onClick={submitDelete}
                  isLoading={delBusy} isDisabled={delConfirm !== "DELETE"} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>
    </>
  );
}
