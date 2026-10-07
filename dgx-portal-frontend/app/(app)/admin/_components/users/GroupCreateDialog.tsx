"use client";

// « Nouveau groupe » dialog of the admin users section. Extracted VERBATIM
// from app/(app)/admin/_components/UsersSection.tsx (plain props, no behaviour
// change).

import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent, LayoutFooter } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Button } from "@astryxdesign/core/Button";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Banner } from "@astryxdesign/core/Banner";
import { useT } from "@/lib/i18n";
import { useIsNarrow } from "@/lib/useIsNarrow";
import { Selector } from "@astryxdesign/core/Selector";
import { BOOL_OPTS, type NewGroupForm } from "./shared";
import type { Dispatch, SetStateAction } from "react";

export type GroupCreateDialogProps = {
  groupDialog: boolean;
  setGroupDialog: (v: boolean) => void;
  setFormWarning: (v: string | null) => void;
  formWarning: string | null;
  ng: NewGroupForm;
  setNg: Dispatch<SetStateAction<NewGroupForm>>;
  createGroup: () => void;
};

export function GroupCreateDialog({
  groupDialog,
  setGroupDialog,
  setFormWarning,
  formWarning,
  ng,
  setNg,
  createGroup,
}: GroupCreateDialogProps) {
  const t = useT();
  const isNarrow = useIsNarrow();
  return (
    <>
      {/* New group dialog */}
      <Dialog isOpen={groupDialog} onOpenChange={(o) => { setGroupDialog(o); if (!o) setFormWarning(null); }} purpose="form" width={isNarrow ? "94vw" : 480}>
        <Layout
          header={<DialogHeader title={t("Nouveau groupe")} hasDivider onOpenChange={(o) => { setGroupDialog(o); if (!o) setFormWarning(null); }} />}
          content={
            <LayoutContent padding={4} isScrollable>
              <VStack gap={3}>
                {formWarning && <Banner status="warning" title={formWarning} />}
                <TextInput label={t("Nom du groupe")} value={ng.name} onChange={(v) => setNg({ ...ng, name: v })} placeholder="équipe-data" />
                <TextInput label={t("Quota / j (optionnel)")} value={ng.max_budget} onChange={(v) => setNg({ ...ng, max_budget: v })} />
                <Selector label={t("Admin par défaut")} value={ng.is_admin} onChange={(v) => setNg({ ...ng, is_admin: v ?? "0" })} options={BOOL_OPTS(t)} />
              </VStack>
            </LayoutContent>
          }
          footer={
            <LayoutFooter>
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" onClick={() => { setFormWarning(null); setGroupDialog(false); }} />
                <Button label={t("Ajouter le groupe")} variant="primary" onClick={createGroup} isDisabled={!ng.name} />
              </HStack>
            </LayoutFooter>
          }
        />
      </Dialog>
    </>
  );
}
