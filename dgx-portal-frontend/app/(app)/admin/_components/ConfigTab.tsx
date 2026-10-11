"use client";
// Instance configuration: LDAP, SSO, e-mail, Discord — set HERE and nowhere else.
//
// « je veux que le SSO, le LDAP et le SMTP soient configurables que depuis
// l'interface web » (operator, 2026-10-11): .env only starts the stack. Then
// « si tu peux améliorer en termes d'UX la config dans l'admin, fais-le »:
// one card per service, saying whether it works (state + « Tester »), what each
// field is for (short label, help, example as placeholder), what to declare at
// the provider (redirect URIs, copyable), and saved per card — a typo in the
// SMTP card no longer travels with an LDAP change.
//
// A SECRET is write-only: its field is never filled, typing one replaces it, an
// empty field leaves it unchanged, and « Effacer » removes it.

import { useCallback, useEffect, useMemo, useState } from "react";
import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Grid } from "@astryxdesign/core/Grid";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Banner } from "@astryxdesign/core/Banner";
import { Code } from "@astryxdesign/core/Code";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { useToast } from "@astryxdesign/core/Toast";
import { useCsrf } from "@/lib/useCsrf";
import { useT } from "@/lib/i18n";
import { copierTexte } from "@/lib/copier";

type Ligne = {
  nom: string;
  libelle: string;
  secret: boolean;
  valeur: string;
  configure: boolean;
  source: string;
  defaut?: string;
};

type Suggestions = Partial<Record<"oidc_redirect_uri" | "oidc_post_logout" | "discord_redirect_uri", string>>;

/** Sent to clear a secret (parametres.EFFACER): an empty field means « unchanged ». */
const EFFACER = "__effacer__";

/** How a field reads in its card: short label, what it is for, an example. */
type Champ = { label: string; aide?: string; exemple?: string };

const CHAMPS: Record<string, Champ> = {
  ldap_uri: { label: "Serveur", aide: "ldaps:// pour une connexion chiffrée.", exemple: "ldap://annuaire.example.org:389" },
  ldap_base: { label: "Base DN", exemple: "dc=example,dc=org" },
  ldap_users_dn: { label: "Branche des comptes", aide: "Relative à la base DN." },
  ldap_login_attr: { label: "Attribut d'identifiant", aide: "cn (Authentik), uid (OpenLDAP, lldap), sAMAccountName (Active Directory)." },
  ldap_bind_dn: { label: "Compte de service (DN)", aide: "Lit les comptes, leur état et leurs groupes.", exemple: "cn=service,ou=users,dc=example,dc=org" },
  ldap_bind_pw: { label: "Mot de passe du compte de service" },
  oidc_metadata_url: { label: "URL de métadonnées", exemple: "https://sso.example.org/application/o/dgx/.well-known/openid-configuration" },
  oidc_client_id: { label: "Client ID" },
  oidc_client_secret: { label: "Client secret" },
  oidc_admin_group: { label: "Groupe administrateur", aide: "Ses membres (LDAP ou SSO) sont administrateurs du portail." },
  oidc_redirect_uri: { label: "URI de retour", aide: "Vide : l'adresse suggérée ci-dessous." },
  oidc_logout_url: { label: "Déconnexion du fournisseur", aide: "Optionnelle : ferme aussi la session SSO à la déconnexion.", exemple: "https://sso.example.org/application/o/dgx/end-session/" },
  smtp_host: { label: "Serveur", exemple: "smtp.example.org" },
  smtp_port: { label: "Port", aide: "587 avec STARTTLS." },
  smtp_user: { label: "Utilisateur", exemple: "no-reply@example.org" },
  smtp_pass: { label: "Mot de passe" },
  smtp_from: { label: "Expéditeur", aide: "Vide : l'utilisateur SMTP.", exemple: "Plateforme IA <no-reply@example.org>" },
  admin_email: { label: "Destinataire des alertes", aide: "Demandes, maintenance, pannes — et le test d'envoi.", exemple: "admin@example.org" },
  discord_webhook_url: { label: "Webhook des alertes admin", aide: "Demandes de modèle et de budget, publiées sur un salon." },
  discord_bot_token: { label: "Jeton du bot", aide: "Envoie les annonces en message privé aux comptes liés." },
  discord_client_id: { label: "Client ID", aide: "Application OAuth2 qui lie un compte Discord." },
  discord_client_secret: { label: "Client secret" },
  discord_redirect_uri: { label: "URI de retour", aide: "Vide : l'adresse suggérée ci-dessous." },
  security_contact: { label: "Contact sécurité", aide: "Publié dans /.well-known/security.txt. Vide : le destinataire des alertes.", exemple: "mailto:security@example.org" },
};

type Etat = { variant: "success" | "warning" | "neutral"; label: string };

type Section = {
  id: string;
  titre: string;
  description: string;
  prefixes: string[];
  /** POST /admin/config/test/<test> */
  test?: string;
  etat: (v: (nom: string) => boolean) => Etat;
};

const ACTIF: Etat = { variant: "success", label: "Actif" };
const INCOMPLET: Etat = { variant: "warning", label: "Incomplet" };
const DESACTIVE: Etat = { variant: "neutral", label: "Désactivé" };

/** Configured if all of `requis`, off if none of them, incomplete otherwise. */
function selon(v: (n: string) => boolean, requis: string[]): Etat {
  const n = requis.filter(v).length;
  return n === requis.length ? ACTIF : n === 0 ? DESACTIVE : INCOMPLET;
}

const SECTIONS: Section[] = [
  {
    id: "ldap", titre: "Annuaire LDAP", prefixes: ["ldap_"], test: "ldap",
    description: "Connexion avec les comptes de l'annuaire, état des comptes et groupes.",
    etat: (v) => selon(v, ["ldap_uri", "ldap_bind_dn", "ldap_bind_pw"]),
  },
  {
    id: "sso", titre: "SSO (OpenID Connect)", prefixes: ["oidc_"], test: "sso",
    description: "Bouton « Se connecter avec le SSO » sur la page de connexion.",
    etat: (v) => selon(v, ["oidc_metadata_url", "oidc_client_id", "oidc_client_secret"]),
  },
  {
    id: "smtp", titre: "E-mail", prefixes: ["smtp_", "admin_email"], test: "smtp",
    description: "Alertes aux administrateurs et messages aux utilisateurs.",
    etat: (v) => {
      const e = selon(v, ["smtp_host", "smtp_user", "smtp_pass"]);
      return e === ACTIF && !v("admin_email") ? INCOMPLET : e;
    },
  },
  {
    id: "discord", titre: "Discord", prefixes: ["discord_"], test: "discord",
    description: "Optionnel : alertes sur un salon, annonces en message privé.",
    etat: (v) => (v("discord_webhook_url") || v("discord_bot_token")
      || (v("discord_client_id") && v("discord_client_secret")) ? ACTIF : DESACTIVE),
  },
  {
    id: "securite", titre: "Sécurité", prefixes: ["security_"],
    description: "Contact publié pour signaler une faille.",
    etat: (v) => (v("security_contact") ? ACTIF : DESACTIVE),
  },
];

function sectionDe(nom: string): Section | undefined {
  return SECTIONS.find((s) => s.prefixes.some((p) => nom.startsWith(p)));
}

/** The value the form starts from for a line (a secret always starts empty). */
function initiale(l: Ligne): string {
  return l.secret ? "" : l.valeur;
}

export function ConfigTab() {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [lignes, setLignes] = useState<Ligne[]>([]);
  const [suggestions, setSuggestions] = useState<Suggestions>({});
  const [brouillon, setBrouillon] = useState<Record<string, string>>({});
  const [envoi, setEnvoi] = useState<string | null>(null);
  const [test, setTest] = useState<Record<string, { enCours: boolean; ok?: boolean; message?: string }>>({});

  const charger = useCallback(async () => {
    const d = await fetch("/admin/config").then((r) => (r.ok ? r.json() : null)).catch(() => null);
    if (!d?.config) return;
    setLignes(d.config);
    setSuggestions(d.suggestions ?? {});
    const v: Record<string, string> = {};
    for (const l of d.config as Ligne[]) v[l.nom] = initiale(l);
    setBrouillon(v);
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- loads the panel once
    void charger();
  }, [charger]);

  const parSection = useMemo(() => {
    const m: Record<string, Ligne[]> = {};
    for (const l of lignes) {
      const s = sectionDe(l.nom)?.id ?? "securite";
      (m[s] ??= []).push(l);
    }
    return m;
  }, [lignes]);

  const modifie = (l: Ligne) => (brouillon[l.nom] ?? "") !== initiale(l);
  const configure = (nom: string) => !!lignes.find((l) => l.nom === nom)?.configure;

  async function enregistrer(section: Section) {
    const champs = (parSection[section.id] ?? []).filter(modifie);
    if (!champs.length) return;
    setEnvoi(section.id);
    try {
      const corps = new URLSearchParams();
      for (const l of champs) corps.append(l.nom, brouillon[l.nom] ?? "");
      const r = await fetch("/admin/config", {
        method: "POST",
        headers: { "X-CSRFToken": csrf, "Content-Type": "application/x-www-form-urlencoded" },
        body: corps.toString(),
      });
      const d = await r.json().catch(() => ({}));
      showToast({
        body: d?.ok ? t("{s} : enregistré.").replace("{s}", t(section.titre)) : t("Échec de l'enregistrement"),
        type: d?.ok ? "info" : "error",
      });
      if (d?.ok) {
        // The panel re-reads (states, sources); typed secrets empty again.
        setTest((x) => ({ ...x, [section.id]: { enCours: false } }));
        await charger();
      }
    } catch {
      showToast({ body: t("Échec de l'enregistrement"), type: "error" });
    } finally {
      setEnvoi(null);
    }
  }

  function annuler(section: Section) {
    setBrouillon((b) => {
      const v = { ...b };
      for (const l of parSection[section.id] ?? []) v[l.nom] = initiale(l);
      return v;
    });
  }

  async function tester(section: Section) {
    if (!section.test) return;
    setTest((x) => ({ ...x, [section.id]: { enCours: true } }));
    try {
      const r = await fetch(`/admin/config/test/${section.test}`, {
        method: "POST",
        headers: { "X-CSRFToken": csrf },
      });
      const d = await r.json().catch(() => ({}));
      setTest((x) => ({ ...x, [section.id]: { enCours: false, ok: !!d?.ok, message: d?.message || d?.error } }));
    } catch {
      setTest((x) => ({ ...x, [section.id]: { enCours: false, ok: false, message: t("Le test n'a pas pu être lancé.") } }));
    }
  }

  function copier(valeur: string) {
    void copierTexte(valeur, () => showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }));
  }

  function champ(l: Ligne) {
    const meta = CHAMPS[l.nom] ?? { label: l.libelle };
    const effacer = brouillon[l.nom] === EFFACER;
    // Placeholder: the example, else the code default the empty field falls back to.
    const exemple = meta.exemple ?? (l.defaut ? l.defaut : undefined);
    if (l.secret) {
      return (
        <VStack key={l.nom} gap={1}>
          <TextInput
            label={t(meta.label)}
            description={meta.aide ? t(meta.aide) : undefined}
            type="password"
            value={effacer ? "" : (brouillon[l.nom] ?? "")}
            placeholder={effacer ? t("(sera effacé à l'enregistrement)")
              : l.configure ? t("•••••• (laisse vide pour garder)") : t("(non défini)")}
            onChange={(v) => setBrouillon((b) => ({ ...b, [l.nom]: v }))}
          />
          {l.configure && !effacer ? (
            <HStack>
              <Button label={t("Effacer")} variant="ghost" size="sm"
                onClick={() => setBrouillon((b) => ({ ...b, [l.nom]: EFFACER }))} />
            </HStack>
          ) : null}
        </VStack>
      );
    }
    return (
      <TextInput
        key={l.nom}
        label={t(meta.label)}
        description={meta.aide ? t(meta.aide) : undefined}
        value={brouillon[l.nom] ?? ""}
        placeholder={exemple ? t("ex. {v}").replace("{v}", exemple) : undefined}
        onChange={(v) => setBrouillon((b) => ({ ...b, [l.nom]: v }))}
      />
    );
  }

  function aDeclarer(section: Section) {
    const lignesUri: [string, string | undefined][] =
      section.id === "sso"
        ? [["URI de retour", suggestions.oidc_redirect_uri], ["URI après déconnexion", suggestions.oidc_post_logout]]
        : section.id === "discord"
          ? [["URI de retour", suggestions.discord_redirect_uri]]
          : [];
    const visibles = lignesUri.filter((x): x is [string, string] => !!x[1]);
    if (!visibles.length) return null;
    return (
      <VStack gap={1}>
        <Text type="supporting" color="secondary">{t("À déclarer chez le fournisseur :")}</Text>
        {visibles.map(([libelle, uri]) => (
          <HStack key={libelle} gap={2} vAlign="center" wrap="wrap">
            <Text type="supporting">{t(libelle)}</Text>
            <Code>{uri}</Code>
            <Button label={t("Copier")} variant="ghost" size="sm" onClick={() => copier(uri)} />
          </HStack>
        ))}
      </VStack>
    );
  }

  if (!lignes.length) {
    return <Text type="supporting" color="secondary">{t("Chargement…")}</Text>;
  }

  return (
    <VStack gap={4}>
      <VStack gap={0}>
        <Text weight="semibold">{t("Configuration de l'instance")}</Text>
        <Text type="supporting" color="secondary">
          {t("Tout se règle ici, sans redémarrage. Un secret n'est jamais réaffiché : laisse son champ vide pour le garder.")}
        </Text>
      </VStack>
      {SECTIONS.map((section) => {
        const champs = parSection[section.id] ?? [];
        if (!champs.length) return null;
        const etat = section.etat(configure);
        const sale = champs.some(modifie);
        const resultat = test[section.id];
        return (
          <Card key={section.id}>
            <VStack gap={4}>
              <HStack hAlign="between" vAlign="start" gap={3} wrap="wrap">
                <VStack gap={0}>
                  <HStack gap={2} vAlign="center">
                    <Text weight="semibold">{t(section.titre)}</Text>
                    <StatusDot variant={etat.variant} label={t(etat.label)} />
                    <Text type="supporting" color="secondary">{t(etat.label)}</Text>
                  </HStack>
                  <Text type="supporting" color="secondary">{t(section.description)}</Text>
                </VStack>
                {section.test ? (
                  <Button
                    label={t("Tester")}
                    variant="secondary"
                    size="sm"
                    isLoading={!!resultat?.enCours}
                    isDisabled={!!resultat?.enCours || sale || etat === DESACTIVE}
                    onClick={() => void tester(section)}
                  />
                ) : null}
              </HStack>
              {sale && section.test ? (
                <Text type="supporting" color="secondary">{t("Enregistre d'abord : le test porte sur les valeurs enregistrées.")}</Text>
              ) : null}
              {resultat && !resultat.enCours && resultat.message ? (
                <Banner status={resultat.ok ? "success" : "error"} title={resultat.message} />
              ) : null}
              <Grid columns={{ minWidth: 300, max: 2 }} gap={4}>
                {champs.map(champ)}
              </Grid>
              {aDeclarer(section)}
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" size="sm" isDisabled={!sale || envoi === section.id}
                  onClick={() => annuler(section)} />
                <Button
                  label={envoi === section.id ? t("Enregistrement…") : t("Enregistrer")}
                  variant="primary"
                  size="sm"
                  isDisabled={!sale || envoi !== null}
                  onClick={() => void enregistrer(section)}
                />
              </HStack>
            </VStack>
          </Card>
        );
      })}
    </VStack>
  );
}
