"use client";
// Instance configuration: LDAP, SSO, SMTP — editable from the admin.
//
// « je veux que sur l'admin on puisse conf le ldap, le sso, le smtp, bref tout
// … c'est mieux que les vars importantes soient stockées sur l'interface web ».
// The panel shows where each value comes from (interface / .env / défaut) —
// the first question when a setting « does not take ». A SECRET is never shown:
// it reads « Configuré » or « Absent », and typing one replaces it.

import { useEffect, useState } from "react";
import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Badge } from "@astryxdesign/core/Badge";
import { useToast } from "@astryxdesign/core/Toast";
import { useCsrf } from "@/lib/useCsrf";
import { useT } from "@/lib/i18n";

type Ligne = {
  nom: string;
  libelle: string;
  secret: boolean;
  valeur: string;
  configure: boolean;
  source: string;
};

export function ConfigTab() {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [lignes, setLignes] = useState<Ligne[]>([]);
  const [brouillon, setBrouillon] = useState<Record<string, string>>({});
  const [envoi, setEnvoi] = useState(false);

  useEffect(() => {
    fetch("/admin/config")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d?.config) {
          setLignes(d.config);
          const v: Record<string, string> = {};
          for (const l of d.config) v[l.nom] = l.secret ? "" : l.valeur;
          setBrouillon(v);
        }
      })
      .catch(() => {});
  }, []);

  async function enregistrer() {
    setEnvoi(true);
    try {
      const corps = new URLSearchParams();
      for (const [k, v] of Object.entries(brouillon)) corps.append(k, v);
      const r = await fetch("/admin/config", {
        method: "POST",
        headers: { "X-CSRFToken": csrf, "Content-Type": "application/x-www-form-urlencoded" },
        body: corps.toString(),
      });
      const d = await r.json().catch(() => ({}));
      showToast({
        body: d?.ok ? t("Configuration enregistrée.") : t("Échec de l'enregistrement"),
        type: d?.ok ? "info" : "error",
      });
      if (d?.ok) {
        const relue = await fetch("/admin/config").then((r) => r.json());
        setLignes(relue.config);
      }
    } catch {
      showToast({ body: t("Échec de l'enregistrement"), type: "error" });
    } finally {
      setEnvoi(false);
    }
  }

  return (
    <VStack gap={6}>
      <Card>
        <VStack gap={3}>
          <VStack gap={0}>
            <Text weight="semibold">{t("Configuration de l'instance")}</Text>
            <Text type="supporting" color="secondary">
              {t("LDAP, SSO, SMTP — l'interface d'abord, .env ensuite, le défaut en dernier. Un secret n'est jamais affiché.")}
            </Text>
          </VStack>
          {lignes.map((l) => (
            <HStack key={l.nom} gap={3} vAlign="center" wrap="wrap">
              <VStack gap={0} style={{ minWidth: "var(--spacing-48)" }}>
                <Text weight="semibold" size="sm">{t(l.libelle)}</Text>
                <Text type="supporting" color="secondary">
                  {t("Source : {s}").replace("{s}", l.source)}
                </Text>
              </VStack>
              {l.secret ? (
                <Badge
                  label={l.configure ? t("Configuré") : t("Absent")}
                  variant={l.configure ? "success" : "neutral"}
                />
              ) : (
                <TextInput
                  label=""
                  value={brouillon[l.nom] ?? ""}
                  placeholder={l.configure ? t("(valeur présente)") : t("(non défini)")}
                  onChange={(v) => setBrouillon((b) => ({ ...b, [l.nom]: v }))}
                />
              )}
            </HStack>
          ))}
          <HStack>
            <Button
              label={envoi ? t("Enregistrement…") : t("Enregistrer")}
              variant="primary"
              isDisabled={envoi}
              onClick={enregistrer}
            />
          </HStack>
        </VStack>
      </Card>
    </VStack>
  );
}
