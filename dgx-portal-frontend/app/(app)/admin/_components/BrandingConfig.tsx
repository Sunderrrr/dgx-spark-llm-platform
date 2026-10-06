"use client";
// The deployment's identity: the instance name and its logo.
//
// « personnaliser en fonction de celui qui le déploie » — Cronos is THIS
// operator's homelab name, not the product's. The name shows top left in the
// sidebar (and in the settings dialog); the logo replaces the built-in favicon
// next to it. Both live in the `settings` table so a redeployed instance keeps
// them, and both are served to the login page too (no session yet).

import { useRef, useState } from "react";
import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Icon } from "@astryxdesign/core/Icon";
import { PhotoIcon } from "@heroicons/react/24/outline";
import { useToast } from "@astryxdesign/core/Toast";
import { useCsrf } from "@/lib/useCsrf";
import { postFormJSON } from "@/lib/api";
import { useT } from "@/lib/i18n";

/** The logo is sent as the uploaded data URL: one row in `settings`, no
 *  filesystem to police, and the server bounds it (2 Mo) like every other
 *  upload on this portal. */
const LOGO_MAX_OCTETS = 2 * 1024 * 1024;

export function BrandingConfig({ initialName }: { initialName?: string }) {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [nom, setNom] = useState(initialName || "Cronos");
  const [logo, setLogo] = useState<string | null>(null);
  const [apercu, setApercu] = useState<string | null>(null);
  const [envoi, setEnvoi] = useState(false);
  const champFichier = useRef<HTMLInputElement>(null);

  function choisir(fichier?: File | null) {
    if (!fichier) return;
    if (fichier.size > LOGO_MAX_OCTETS) {
      showToast({ body: t("Logo trop lourd (2 Mo maximum)."), type: "error" });
      return;
    }
    const lecteur = new FileReader();
    lecteur.onload = () => {
      const url = String(lecteur.result || "");
      setLogo(url);
      setApercu(url);
    };
    lecteur.readAsDataURL(fichier);
  }

  async function enregistrer() {
    setEnvoi(true);
    try {
      const rep = await postFormJSON("/admin/branding", csrf, {
        platform_name: nom.trim(),
        platform_logo: logo || "",
      });
      if (rep?.ok) {
        showToast({ body: t("Identité enregistrée."), type: "info" });
      } else {
        showToast({ body: t("Échec de l'enregistrement"), type: "error" });
      }
    } catch {
      showToast({ body: t("Échec de l'enregistrement"), type: "error" });
    } finally {
      setEnvoi(false);
    }
  }

  return (
    <Card>
      <VStack gap={3}>
        <Text weight="semibold">{t("Identité de la plateforme")}</Text>
        <Text type="supporting" color="secondary">
          {t("Le nom et le logo affichés en haut à gauche — à l'image de celui qui déploie l'instance.")}
        </Text>
        <TextInput
          label={t("Nom de l'instance")}
          value={nom}
          onChange={setNom}
          placeholder={t("Ex : Cronos")}
        />
        <HStack gap={3} vAlign="center" wrap="wrap">
          <Button
            label={t("Choisir un logo")}
            variant="secondary"
            icon={<Icon icon={PhotoIcon} size="sm" />}
            onClick={() => champFichier.current?.click()}
          />
          {apercu ? (
            <>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={apercu} alt="" style={{ height: "var(--spacing-6)", width: "auto" }} />
              <Button label={t("Retirer le logo")} variant="ghost" size="sm" onClick={() => { setLogo("-"); setApercu(null); }} />
            </>
          ) : (
            <Text type="supporting" color="secondary">{t("Aucun logo : la favicon par défaut est utilisée.")}</Text>
          )}
          <input
            ref={champFichier}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/svg+xml,image/gif"
            style={{ display: "none" }}
            onChange={(e) => choisir(e.target.files?.[0])}
          />
        </HStack>
        <HStack>
          <Button label={envoi ? t("Enregistrement…") : t("Enregistrer")} variant="primary" onClick={enregistrer} />
        </HStack>
      </VStack>
    </Card>
  );
}
