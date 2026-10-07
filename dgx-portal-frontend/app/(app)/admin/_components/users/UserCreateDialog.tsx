"use client";

// « Nouvel utilisateur » dialog of the admin users section. Extracted
// VERBATIM from app/(app)/admin/_components/UsersSection.tsx (plain props, no
// behaviour change).

import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent, LayoutFooter } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Button } from "@astryxdesign/core/Button";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Banner } from "@astryxdesign/core/Banner";
import { useT } from "@/lib/i18n";
import { useIsNarrow } from "@/lib/useIsNarrow";
import { Selector } from "@astryxdesign/core/Selector";
import { BOOL_OPTS, type NewUserForm, type UsersData } from "./shared";
import type { Dispatch, SetStateAction } from "react";

export type UserCreateDialogProps = {
  userDialog: boolean;
  setUserDialog: (v: boolean) => void;
  setFormWarning: (v: string | null) => void;
  formWarning: string | null;
  nu: NewUserForm;
  setNu: Dispatch<SetStateAction<NewUserForm>>;
  groupOptions: { label: string; value: string }[];
  data: UsersData | null;
  fmtBudget: (n: number) => string;
  createUser: () => void;
};

export function UserCreateDialog({
  userDialog,
  setUserDialog,
  setFormWarning,
  formWarning,
  nu,
  setNu,
  groupOptions,
  data,
  fmtBudget,
  createUser,
}: UserCreateDialogProps) {
  const t = useT();
  const isNarrow = useIsNarrow();
  return (
    <>
      {/* New user dialog */}
      <Dialog isOpen={userDialog} onOpenChange={(o) => { setUserDialog(o); if (!o) setFormWarning(null); }} purpose="form" width={isNarrow ? "94vw" : 520}>
        <Layout
          header={<DialogHeader title={t("Nouvel utilisateur")} hasDivider onOpenChange={(o) => { setUserDialog(o); if (!o) setFormWarning(null); }} />}
          content={
            <LayoutContent padding={4} isScrollable>
              <VStack gap={3}>
                {formWarning && <Banner status="warning" title={formWarning} />}
                <TextInput label={t("Identifiant")} value={nu.username} onChange={(v) => setNu({ ...nu, username: v })} placeholder="jdupont" />
                <TextInput label={t("Nom complet")} value={nu.fullname} onChange={(v) => setNu({ ...nu, fullname: v })} placeholder="Jean Dupont" />
                <TextInput label={t("Mot de passe")} type="password" value={nu.password} onChange={(v) => setNu({ ...nu, password: v })} />
                <Selector label={t("Groupe")} value={nu.group} onChange={(v) => setNu({ ...nu, group: v ?? "" })} options={groupOptions} />
                <TextInput label={t("Quota (vide = groupe/défaut)")} value={nu.max_budget} onChange={(v) => setNu({ ...nu, max_budget: v })} placeholder={data ? fmtBudget(data.default_budget) : ""} />
                <Selector label={t("Admin")} value={nu.is_admin} onChange={(v) => setNu({ ...nu, is_admin: v ?? "0" })} options={BOOL_OPTS(t)} />
              </VStack>
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" onClick={() => { setFormWarning(null); setUserDialog(false); }} />
                <Button label={t("Créer")} variant="primary" onClick={createUser} isDisabled={!nu.username || nu.password.length < 8} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>
    </>
  );
}
