"use client";

import { useEffect, useState } from "react";
import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Icon } from "@astryxdesign/core/Icon";
import { EnvelopeIcon, PaperAirplaneIcon } from "@heroicons/react/24/outline";
import { useToast } from "@astryxdesign/core/Toast";
import { useCsrf } from "@/lib/useCsrf";
import { getJSON, sendJSON, ForbiddenError } from "@/lib/api";
import { useT } from "@/lib/i18n";

type Config = { configured?: boolean; admin_email?: string };
type TestResp = { ok?: boolean; error?: { message?: string } };

/**
 * Bloc « Emails de notification » de la page Admin : indique si le SMTP est
 * configuré (hôte / user / mot de passe / admin) et permet d'envoyer un email
 * de test à l'admin — sans jamais exposer le mot de passe.
 */
export function EmailConfig() {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [cfg, setCfg] = useState<Config | null>(null);
  const [sending, setSending] = useState(false);

  useEffect(() => {
    getJSON<Config>("/admin/email/config")
      .then(setCfg)
      .catch((e: unknown) => {
        if (!(e instanceof ForbiddenError)) setCfg({ configured: false });
      });
  }, []);

  async function sendTest() {
    setSending(true);
    try {
      const res = await sendJSON<TestResp>("/admin/email/test", csrf, {});
      if (res.ok) showToast({ body: t("Email de test envoyé."), type: "info" });
      else showToast({ body: t(res.error?.message || "Échec de l'envoi."), type: "error" });
    } catch {
      showToast({ body: t("Échec de l'envoi."), type: "error" });
    } finally {
      setSending(false);
    }
  }

  const configured = cfg?.configured === true;

  return (
    <Card>
      <VStack gap={3}>
        <HStack hAlign="between" vAlign="center" gap={2}>
          <HStack gap={2} vAlign="center">
            <Icon icon={EnvelopeIcon} size="sm" />
            <Text weight="semibold">{t("Emails de notification")}</Text>
          </HStack>
          {cfg && (
            <StatusDot
              variant={configured ? "success" : "error"}
              label={configured ? t("SMTP configuré") : t("SMTP non configuré")}
            />
          )}
        </HStack>
        <Text type="supporting" color="secondary">
          {t("Envoi depuis no-reply@cronos.website via Zoho ; les notifications admin partent vers l'adresse ci-dessous.")}
        </Text>
        <HStack hAlign="between" vAlign="center" gap={2} wrap="wrap">
          <Text type="supporting" color="secondary" wordBreak="break-all">
            {cfg?.admin_email || "SMTP"}
          </Text>
          <Button
            label={t("Envoyer un test")}
            icon={<Icon icon={PaperAirplaneIcon} size="sm" />}
            variant="secondary"
            size="sm"
            isDisabled={!cfg || !configured || sending}
            clickAction={sendTest}
          />
        </HStack>
      </VStack>
    </Card>
  );
}
