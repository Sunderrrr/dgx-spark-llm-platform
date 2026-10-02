"use client";

import { useEffect, useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { Text } from "@astryxdesign/core/Text";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Icon } from "@astryxdesign/core/Icon";
import { BellAlertIcon, CheckIcon, ClockIcon } from "@heroicons/react/24/outline";
import { useToast } from "@astryxdesign/core/Toast";
import { useCsrf } from "@/lib/useCsrf";
import { sendJSON } from "@/lib/api";
import { useT } from "@/lib/i18n";

type MediaCategory = "image" | "music" | "video" | "ocr" | "voice";
type Req = { ok?: boolean; error?: { message?: string }; retry_after?: number; email_sent?: boolean };

/**
 * « demander un modèle » button shown on the media pages when NO model of
 * the category is loaded. It alerts the admin (email) — the button
 * disappears backend-side if a model of the category is already running.
 * Once the request is sent, it locks itself (avoids duplicates); a
 * 429 (anti-spam cooldown) shows a countdown on the button.
 * With `showText={false}`, it renders only the button (for the EmptyState).
 */
export function ModelRequestButton({
  category,
  showText = true,
}: {
  category: MediaCategory;
  showText?: boolean;
}) {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [sent, setSent] = useState(false);
  const [cooldownUntil, setCooldownUntil] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  // Tick during an active cooldown: updates `now` (countdown) and disables
  // the auto-cleanup once the window has elapsed (setState in the interval
  // callback, never in the body of the effect).
  useEffect(() => {
    if (cooldownUntil === null) return;
    const id = setInterval(() => {
      const n = Date.now();
      setNow(n);
      if (n >= cooldownUntil) {
        clearInterval(id);
        setCooldownUntil(null);
      }
    }, 1000);
    return () => clearInterval(id);
  }, [cooldownUntil]);

  async function request() {
    try {
      const res = await sendJSON<Req>("/api/model/request", csrf, { category });
      if (res.ok) {
        setSent(true);
        showToast({
          body: res.email_sent
            ? t("Demande envoyée au responsable.")
            : t("Demande enregistrée mais l'email à l'admin n'a pas pu partir."),
          type: "info",
        });
      } else if (res.retry_after !== undefined) {
        setCooldownUntil(Date.now() + res.retry_after * 1000);
        showToast({
          body: t("Déjà signalé — la demande se débloquera automatiquement."),
          type: "info",
        });
      } else {
        showToast({ body: t(res.error?.message || "La demande a échoué."), type: "error" });
      }
    } catch {
      showToast({ body: t("La demande a échoué."), type: "error" });
    }
  }

  const locked = sent || (cooldownUntil !== null && now < cooldownUntil);
  const remainMin =
    cooldownUntil !== null ? Math.max(1, Math.ceil((cooldownUntil - now) / 60000)) : 0;
  const label = sent
    ? t("Demande envoyée")
    : locked
      ? `${t("Réessaie dans")} ${remainMin} ${t("min")}`
      : t("Demander ce modèle");
  const icon = sent ? CheckIcon : locked ? ClockIcon : BellAlertIcon;

  const button = (
    <Button
      label={label}
      icon={<Icon icon={icon} size="sm" />}
      variant={sent ? "secondary" : "primary"}
      isDisabled={locked}
      clickAction={request}
    />
  );

  if (!showText) return button;

  return (
    <VStack gap={2}>
      <Text type="supporting" color="secondary">
        {t("Envie de ce modèle ? Préviens le responsable, il pourra le lancer.")}
      </Text>
      <HStack gap={2}>{button}</HStack>
    </VStack>
  );
}
