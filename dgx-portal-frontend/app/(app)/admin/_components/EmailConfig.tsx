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

/** Sender address of the portal (SMTP_FROM). It is written into the DOM
 *  only AFTER mount, and never in the HTML rendered by the server:
 *  Cloudflare (Scrape Shield) rewrites every email address of the SERVER-side
 *  HTML into <a class="__cf_email__">[email protected]</a>. React then receives a DOM
 *  that no longer matches what it rendered, and hydration fails — « Minified
 *  React error #418 (args[]=text) », measured on 2026-10-02 on /admin, the only
 *  page that displayed an address. Cloudflare only rewrites the HTTP response,
 *  never client-created nodes: arriving after mount, the address is intact.
 *  (Removing the email obfuscation in Cloudflare would lift the constraint,
 *  but the portal cannot rely on that setting.) */
const SENDER_ADDRESS = "no-reply@cronos.website";

/**
 * « Emails de notification » block of the Admin page: states whether SMTP is
 * configured (host / user / password / admin) and allows sending a test
 * email to the admin — without ever exposing the password.
 */
export function EmailConfig() {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [cfg, setCfg] = useState<Config | null>(null);
  const [sending, setSending] = useState(false);
  // False on server render as on first client render (so no hydration
  // mismatch), true right after mount: that is what allows SENDER_ADDRESS
  // to enter the DOM client-side only. Like the ThemeProvider for its local
  // preferences, it is the « syncing from an external system » case
  // the rule targets — not a render cascade.
  const [monte, setMonte] = useState(false);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- one-shot on mount
    setMonte(true);
  }, []);

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
          {t("Envoi depuis {adresse} via Zoho ; les notifications admin partent vers l'adresse ci-dessous.")
            .replace("{adresse}", monte ? SENDER_ADDRESS : t("le portail"))}
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
