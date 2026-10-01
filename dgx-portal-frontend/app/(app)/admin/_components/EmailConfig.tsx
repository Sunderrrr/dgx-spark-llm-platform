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

/** Adresse d'expédition du portail (SMTP_FROM). Elle est écrite dans le DOM
 *  seulement APRÈS le montage, et jamais dans le HTML rendu par le serveur :
 *  Cloudflare (Scrape Shield) réécrit toute adresse email du HTML SERVEUR en
 *  <a class="__cf_email__">[email protected]</a>. React reçoit alors un DOM qui
 *  ne correspond plus à ce qu'il a rendu, et l'hydratation échoue — « Minified
 *  React error #418 (args[]=text) », mesuré le 2026-10-02 sur /admin, la seule
 *  page qui affichait une adresse. Cloudflare ne réécrit que la réponse HTTP,
 *  jamais les nœuds créés par le client : arrivée après le montage, l'adresse
 *  est intacte. (Supprimer l'obfuscation email dans Cloudflare lèverait la
 *  contrainte, mais le portail ne peut pas compter sur ce réglage.) */
const SENDER_ADDRESS = "no-reply@cronos.website";

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
  // Faux au rendu serveur comme au premier rendu client (donc aucun écart
  // d'hydratation), vrai juste après le montage : c'est ce qui autorise
  // SENDER_ADDRESS à entrer dans le DOM côté client seulement. Comme le
  // ThemeProvider pour ses préférences locales, c'est le cas « synchroniser
  // depuis un système externe » que vise la règle — pas une cascade de rendus.
  const [monte, setMonte] = useState(false);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- one-shot au montage
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
