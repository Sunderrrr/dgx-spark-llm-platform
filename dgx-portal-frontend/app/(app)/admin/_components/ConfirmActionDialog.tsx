"use client";

import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { useT } from "@/lib/i18n";
import type { ConfirmAction } from "./adminTypes";

/** Confirmation dialog for the four consequential actions (stopping the
 * served model, launch, catalog deletion, maintenance toggle). A
 * consequence sentence, a confirmation button (destructive for what
 * cuts/destroys, primary for the launch), an Annuler button — the POST is
 * only sent on the click on Confirmer. */
export function ConfirmActionDialog({ action, maintenanceActive, isDisabled, onClose, onConfirm }: {
  action: ConfirmAction | null;
  maintenanceActive: boolean;
  isDisabled: boolean;
  onClose: () => void;
  onConfirm: (action: ConfirmAction) => void;
}) {
  const t = useT();
  if (!action) return null;
  let title: string;
  let body: string;
  let confirmLabel: string;
  let confirmVariant: "primary" | "secondary" | "destructive";
  if (action.kind === "stop-model") {
    title = t("Arrêter le modèle servi ?");
    body = t("Le modèle va être coupé : toutes les générations en cours, pour tous les utilisateurs, seront interrompues.");
    confirmLabel = t("Arrêter");
    confirmVariant = "destructive";
  } else if (action.kind === "launch") {
    title = t("Lancer {name} ?").replace("{name}", action.name);
    body = t("Le modèle sera chargé en mémoire unifiée — le lancement peut prendre plusieurs minutes.");
    confirmLabel = t("Lancer");
    confirmVariant = "primary";
  } else if (action.kind === "delete-model") {
    title = t("Supprimer {name} ?").replace("{name}", action.name);
    body = t("Le modèle sera retiré du catalogue et du routage LiteLLM, et ses fichiers seront effacés du disque (sauf s'ils servent à un autre modèle). Pour le retélécharger, il faudra le réinstaller. Un modèle en cours doit d'abord être arrêté.");
    confirmLabel = t("Supprimer");
    confirmVariant = "destructive";
  } else if (maintenanceActive) {
    title = t("Désactiver le mode maintenance ?");
    body = t("Le trafic non-admin vers les routes de génération sera de nouveau accepté.");
    confirmLabel = t("Désactiver");
    confirmVariant = "secondary";
  } else {
    title = t("Activer le mode maintenance ?");
    body = t("Le trafic non-admin vers les routes de génération sera refusé ; les admins gardent l'accès.");
    confirmLabel = t("Activer");
    confirmVariant = "secondary";
  }
  return (
    <Dialog isOpen onOpenChange={(o) => { if (!o) onClose(); }} purpose="form">
      <DialogHeader title={title} />
      <VStack gap={3}>
        <Text type="supporting" color="secondary">{body}</Text>
        <HStack gap={2} hAlign="end">
          <Button label={t("Annuler")} variant="ghost" size="sm" onClick={onClose} />
          <Button
            label={confirmLabel}
            variant={confirmVariant}
            size="sm"
            isDisabled={isDisabled}
            onClick={() => { onConfirm(action); onClose(); }}
          />
        </HStack>
      </VStack>
    </Dialog>
  );
}
