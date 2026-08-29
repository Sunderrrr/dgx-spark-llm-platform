"use client";

import { useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { Text } from "@astryxdesign/core/Text";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Icon } from "@astryxdesign/core/Icon";
import { BellAlertIcon, CheckIcon } from "@heroicons/react/24/outline";
import { useToast } from "@astryxdesign/core/Toast";
import { useCsrf } from "@/lib/useCsrf";
import { sendJSON } from "@/lib/api";
import { useT } from "@/lib/i18n";

type MediaCategory = "image" | "music" | "video" | "ocr" | "voice";
type Req = { ok?: boolean; error?: { message?: string } };

/**
 * Bouton « demander un modèle » affiché sur les pages média quand AUCUN
 * modèle de la catégorie n'est chargé. Il prévient l'admin (email) — le
 * bouton disparaît côté backend si un modèle de la catégorie tourne déjà.
 * Une fois la demande envoyée, il se verrouille (évite les doublons).
 */
export function ModelRequestButton({ category }: { category: MediaCategory }) {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [sent, setSent] = useState(false);

  async function request() {
    try {
      const res = await sendJSON<Req>("/api/model/request", csrf, { category });
      if (res.ok) {
        setSent(true);
        showToast({ body: t("Demande envoyée au responsable."), type: "info" });
      } else {
        showToast({ body: t(res.error?.message || "La demande a échoué."), type: "error" });
      }
    } catch {
      showToast({ body: t("La demande a échoué."), type: "error" });
    }
  }

  return (
    <VStack gap={2}>
      <Text type="supporting" color="secondary">
        {t("Envie de ce modèle ? Préviens le responsable, il pourra le lancer.")}
      </Text>
      <HStack gap={2}>
        <Button
          label={sent ? t("Demande envoyée") : t("Demander ce modèle")}
          icon={<Icon icon={sent ? CheckIcon : BellAlertIcon} size="sm" />}
          variant={sent ? "secondary" : "primary"}
          isDisabled={sent}
          clickAction={request}
        />
      </HStack>
    </VStack>
  );
}
