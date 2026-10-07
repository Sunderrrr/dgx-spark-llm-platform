"use client";

// « Réinitialiser le mot de passe » dialog of the admin users section.
// Extracted VERBATIM from app/(app)/admin/_components/UsersSection.tsx
// (plain props, no behaviour change).

import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent, LayoutFooter } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Button } from "@astryxdesign/core/Button";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Banner } from "@astryxdesign/core/Banner";
import { useT } from "@/lib/i18n";
import { useIsNarrow } from "@/lib/useIsNarrow";
import type { LocalUser } from "./shared";
import type { Dispatch, SetStateAction } from "react";

export type PasswordResetDialogProps = {
  pwUser: LocalUser | null;
  setPwUser: (v: LocalUser | null) => void;
  setFormWarning: (v: string | null) => void;
  formWarning: string | null;
  pw: string;
  setPw: Dispatch<SetStateAction<string>>;
  submitPassword: () => void;
};

export function PasswordResetDialog({
  pwUser,
  setPwUser,
  setFormWarning,
  formWarning,
  pw,
  setPw,
  submitPassword,
}: PasswordResetDialogProps) {
  const t = useT();
  const isNarrow = useIsNarrow();
  return (
    <>
      {/* Reset password dialog */}
      <Dialog isOpen={pwUser != null} onOpenChange={(o) => { if (!o) { setPwUser(null); setFormWarning(null); } }} purpose="form" width={isNarrow ? "94vw" : 440}>
        <Layout
          header={<DialogHeader title={t("Réinitialiser le mot de passe")} subtitle={pwUser?.username} hasDivider onOpenChange={(o) => { if (!o) { setPwUser(null); setFormWarning(null); } }} />}
          content={
            <LayoutContent padding={4}>
              <VStack gap={3}>
                {formWarning && <Banner status="warning" title={formWarning} />}
                <TextInput label={t("Nouveau mot de passe (8 caractères min.)")} type="password" value={pw} onChange={setPw} />
              </VStack>
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" onClick={() => { setFormWarning(null); setPwUser(null); }} />
                <Button label={t("Enregistrer")} variant="primary" onClick={submitPassword} isDisabled={pw.length < 8} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>
    </>
  );
}
