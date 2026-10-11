"use client";
// Instance configuration: LDAP, SSO, SMTP — set HERE and nowhere else.
//
// « je veux que le SSO, le LDAP et le SMTP soient configurables que depuis
// l'interface web » (operator, 2026-10-11): .env only starts the stack. The
// panel says where each value comes from (interface / défaut). A SECRET is
// write-only: its field is never filled, typing one replaces it, an empty
// field leaves it unchanged, and « Effacer » removes it.

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

type Categorie = { titre: string; mots: string[] };

/** Sent to clear a secret (parametres.EFFACER): an empty field means « unchanged ». */
const EFFACER = "__effacer__";

// « la partie configuration dans l'admin est horrible, améliore ça, fais des
// catégories » — one card per concern, so the eye finds LDAP without reading
// fourteen labels. A new field goes to its category by its prefix.
const CATEGORIES: Categorie[] = [
  { titre: "LDAP", mots: ["ldap_"] },
  { titre: "SSO / Authentification", mots: ["oidc_"] },
  { titre: "SMTP / Courriel", mots: ["smtp_", "admin_email"] },
  { titre: "Discord", mots: ["discord_"] },
];

function categorieDe(nom: string): Categorie {
  return CATEGORIES.find((c) => c.mots.some((m) => nom.startsWith(m)))
    ?? { titre: "Autres", mots: [] };
}

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
      for (const [k, v] of Object.entries(brouillon)) {
        const ligne = lignes.find((l) => l.nom === k);
        // A secret goes only when typed (or cleared): empty = unchanged.
        if (ligne?.secret && !v) continue;
        corps.append(k, v);
      }
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
        // Typed secrets are sent: empty their fields again (write-only).
        setBrouillon((b) => {
          const v = { ...b };
          for (const l of relue.config as Ligne[]) if (l.secret) v[l.nom] = "";
          return v;
        });
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
              {t("LDAP, SSO et e-mail se règlent ici, et nulle part ailleurs. Un secret n'est jamais réaffiché : laisse son champ vide pour le garder.")}
            </Text>
          </VStack>
          {CATEGORIES.map((cat) => {
            const groupe = lignes.filter((l) => categorieDe(l.nom).titre === cat.titre);
            return groupe.length === 0 ? null : (
              <Card key={cat.titre}>
                <VStack gap={2}>
                  <Text weight="semibold">{t(cat.titre)}</Text>
                  {groupe.map((l) => (
                    <HStack key={l.nom} gap={3} vAlign="center">
                      {/* One column for the label, one for the control: with
                          `wrap` and a variable label the inputs zigzagged. */}
                      <VStack gap={0} style={{ width: "var(--spacing-64)", flexShrink: 0 }}>
                        <Text weight="semibold" size="sm">{t(l.libelle)}</Text>
                        <Text type="supporting" color="secondary">
                          {t("Source : {s}").replace("{s}", l.source)}
                        </Text>
                      </VStack>
                      {l.secret ? (
                        <HStack gap={2} vAlign="center">
                          <TextInput
                            label=""
                            type="password"
                            value={brouillon[l.nom] === EFFACER ? "" : (brouillon[l.nom] ?? "")}
                            placeholder={brouillon[l.nom] === EFFACER ? t("(sera effacé)")
                              : l.configure ? t("(inchangé)") : t("(non défini)")}
                            onChange={(v) => setBrouillon((b) => ({ ...b, [l.nom]: v }))}
                          />
                          <Badge
                            label={l.configure ? t("Configuré") : t("Absent")}
                            variant={l.configure ? "success" : "neutral"}
                          />
                          {l.configure ? (
                            <Button
                              label={t("Effacer")}
                              variant="ghost"
                              size="sm"
                              onClick={() => setBrouillon((b) => ({ ...b, [l.nom]: EFFACER }))}
                            />
                          ) : null}
                        </HStack>
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
                </VStack>
              </Card>
            );
          })}
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
