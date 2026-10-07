"use client";

// « Purger les données » dialog of the admin users section (directory
// accounts). Extracted VERBATIM from
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

export type PurgeAccountDialogProps = {
  purgeUser: LocalUser | null;
  setPurgeUser: (v: LocalUser | null) => void;
  purgeConfirm: string;
  setPurgeConfirm: Dispatch<SetStateAction<string>>;
  purgeError: string | null;
  submitPurge: () => void;
  purgeBusy: boolean;
};

export function PurgeAccountDialog({
  purgeUser,
  setPurgeUser,
  purgeConfirm,
  setPurgeConfirm,
  purgeError,
  submitPurge,
  purgeBusy,
}: PurgeAccountDialogProps) {
  const t = useT();
  const isNarrow = useIsNarrow();
  return (
    <>
      {/* Purge dialog — directory accounts (LDAP/SSO): erases the data,
          NOT the access; Bloquer is what does the offboarding. */}
      <Dialog isOpen={purgeUser != null} onOpenChange={(o) => { if (!o) setPurgeUser(null); }} purpose="form" width={isNarrow ? "94vw" : 480}>
        <Layout
          header={<DialogHeader title={t("Purger les données")} subtitle={purgeUser?.username} hasDivider onOpenChange={(o) => { if (!o) setPurgeUser(null); }} />}
          content={
            <LayoutContent padding={4}>
              <VStack gap={3}>
                <Text type="supporting" color="secondary">
                  {t("Cette action efface DÉFINITIVEMENT les données du compte : mémoires, conversations, liens de partage, préférences et clés API.")}
                </Text>
                <Text type="supporting" color="secondary">
                  {t("Elle ne retire pas l'accès : un compte LDAP/SSO peut se reconnecter et repartira d'un compte vide. Pour retirer l'accès, utilise Bloquer.")}
                </Text>
                <TextInput label={t("Tapez DELETE pour confirmer")} value={purgeConfirm} onChange={setPurgeConfirm} />
                {purgeError && <Banner status="error" title={purgeError} />}
              </VStack>
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" onClick={() => setPurgeUser(null)} />
                <Button label={t("Purger")} variant="destructive" onClick={submitPurge}
                  isLoading={purgeBusy} isDisabled={purgeConfirm !== "DELETE"} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>
    </>
  );
}
