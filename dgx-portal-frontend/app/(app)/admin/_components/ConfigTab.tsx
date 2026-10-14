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
import { useT, tServer } from "@/lib/i18n";
import { copyText } from "@/lib/clipboard";
import { BrandingConfig } from "./BrandingConfig";
import { StorageConfig } from "./StorageConfig";

type Row = {
  name: string;
  label: string;
  secret: boolean;
  value: string;
  configure: boolean;
  source: string;
  default?: string;
};

type Suggestions = Partial<Record<"oidc_redirect_uri" | "oidc_post_logout" | "discord_redirect_uri", string>>;

/** Sent to clear a secret (parametres.EFFACER): an empty field means « unchanged ». */
const CLEAR = "__effacer__";

/** How a field reads in its card: short label, what it is for, an example. */
type Field = { label: string; help?: string; example?: string };

const FIELDS: Record<string, Field> = {
  ldap_uri: { label: "Serveur", help: "ldaps:// pour une connexion chiffrée.", example: "ldap://annuaire.example.org:389" },
  ldap_base: { label: "Base DN", example: "dc=example,dc=org" },
  ldap_users_dn: { label: "Branche des comptes", help: "Relative à la base DN." },
  ldap_login_attr: { label: "Attribut d'identifiant", help: "cn (Authentik), uid (OpenLDAP, lldap), sAMAccountName (Active Directory)." },
  ldap_bind_dn: { label: "Compte de service (DN)", help: "Lit les comptes, leur état et leurs groupes.", example: "cn=service,ou=users,dc=example,dc=org" },
  ldap_bind_pw: { label: "Mot de passe du compte de service" },
  oidc_metadata_url: { label: "URL de métadonnées", example: "https://sso.example.org/application/o/dgx/.well-known/openid-configuration" },
  oidc_client_id: { label: "Client ID" },
  oidc_client_secret: { label: "Client secret" },
  oidc_admin_group: { label: "Groupe administrateur", help: "Ses membres (LDAP ou SSO) sont administrateurs du portail." },
  oidc_redirect_uri: { label: "URI de retour", help: "Vide : l'adresse suggérée ci-dessous." },
  oidc_logout_url: { label: "Déconnexion du fournisseur", help: "Optionnelle : ferme aussi la session SSO à la déconnexion.", example: "https://sso.example.org/application/o/dgx/end-session/" },
  smtp_host: { label: "Serveur", example: "smtp.example.org" },
  smtp_port: { label: "Port", help: "587 avec STARTTLS." },
  smtp_user: { label: "Utilisateur", example: "no-reply@example.org" },
  smtp_pass: { label: "Mot de passe" },
  smtp_from: { label: "Expéditeur", help: "Vide : l'utilisateur SMTP.", example: "Plateforme IA <no-reply@example.org>" },
  admin_email: { label: "Destinataire des alertes", help: "Demandes, maintenance, pannes — et le test d'envoi.", example: "admin@example.org" },
  discord_webhook_url: { label: "Webhook des alertes admin", help: "Demandes de modèle et de budget, publiées sur un salon." },
  discord_bot_token: { label: "Jeton du bot", help: "Envoie les annonces en message privé aux comptes liés." },
  discord_client_id: { label: "Client ID", help: "Application OAuth2 qui lie un compte Discord." },
  discord_client_secret: { label: "Client secret" },
  discord_redirect_uri: { label: "URI de retour", help: "Vide : l'adresse suggérée ci-dessous." },
  s3_endpoint: { label: "Endpoint", help: "Adresse du service S3 (Garage, MinIO, AWS…).", example: "https://s3.example.org" },
  s3_region: { label: "Région", help: "Garage : « garage » ; MinIO : souvent « us-east-1 »." },
  s3_bucket: { label: "Bucket", help: "Doit exister et rester privé : il reçoit les sauvegardes (avec .env).", example: "dgx" },
  s3_prefix: { label: "Préfixe", help: "Optionnel : dossier du bucket où tout est rangé.", example: "dgx" },
  s3_access_key: { label: "Clé d'accès" },
  s3_secret_key: { label: "Clé secrète" },
  security_contact: { label: "Contact sécurité", help: "Publié dans /.well-known/security.txt. Vide : le destinataire des alertes.", example: "mailto:security@example.org" },
};

type State = { variant: "success" | "warning" | "neutral"; label: string };

type Section = {
  id: string;
  title: string;
  description: string;
  prefixes: string[];
  /** POST /admin/config/test/<test> */
  test?: string;
  state: (v: (name: string) => boolean) => State;
};

const ACTIVE: State = { variant: "success", label: "Actif" };
const INCOMPLETE: State = { variant: "warning", label: "Incomplet" };
const DISABLED: State = { variant: "neutral", label: "Désactivé" };

/** Configured if all of `required`, off if none of them, incomplete otherwise. */
function accordingTo(v: (n: string) => boolean, required: string[]): State {
  const n = required.filter(v).length;
  return n === required.length ? ACTIVE : n === 0 ? DISABLED : INCOMPLETE;
}

const SECTIONS: Section[] = [
  {
    id: "ldap", title: "Annuaire LDAP", prefixes: ["ldap_"], test: "ldap",
    description: "Connexion avec les comptes de l'annuaire, état des comptes et groupes.",
    state: (v) => accordingTo(v, ["ldap_uri", "ldap_bind_dn", "ldap_bind_pw"]),
  },
  {
    id: "sso", title: "SSO (OpenID Connect)", prefixes: ["oidc_"], test: "sso",
    description: "Bouton « Se connecter avec le SSO » sur la page de connexion.",
    state: (v) => accordingTo(v, ["oidc_metadata_url", "oidc_client_id", "oidc_client_secret"]),
  },
  {
    id: "smtp", title: "E-mail", prefixes: ["smtp_", "admin_email"], test: "smtp",
    description: "Alertes aux administrateurs et messages aux utilisateurs.",
    state: (v) => {
      const e = accordingTo(v, ["smtp_host", "smtp_user", "smtp_pass"]);
      return e === ACTIVE && !v("admin_email") ? INCOMPLETE : e;
    },
  },
  {
    id: "discord", title: "Discord", prefixes: ["discord_"], test: "discord",
    description: "Optionnel : alertes sur un salon, annonces en message privé.",
    state: (v) => (v("discord_webhook_url") || v("discord_bot_token")
      || (v("discord_client_id") && v("discord_client_secret")) ? ACTIVE : DISABLED),
  },
  {
    id: "s3", title: "Stockage S3", prefixes: ["s3_"], test: "s3",
    description: "Optionnel : sauvegardes et fichiers générés sur un stockage S3 (réglés plus bas).",
    state: (v) => accordingTo(v, ["s3_endpoint", "s3_bucket", "s3_access_key", "s3_secret_key"]),
  },
  {
    id: "securite", title: "Sécurité", prefixes: ["security_"],
    description: "Contact publié pour signaler une faille.",
    state: (v) => (v("security_contact") ? ACTIVE : DISABLED),
  },
];

function sectionDe(name: string): Section | undefined {
  return SECTIONS.find((s) => s.prefixes.some((p) => name.startsWith(p)));
}

/** The value the form starts from for a line (a secret always starts empty). */
function initial(l: Row): string {
  return l.secret ? "" : l.value;
}

export function ConfigTab() {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [lines, setLines] = useState<Row[]>([]);
  const [suggestions, setSuggestions] = useState<Suggestions>({});
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [sending, setSending] = useState<string | null>(null);
  const [test, setTest] = useState<Record<string, { running: boolean; ok?: boolean; message?: string }>>({});
  // Bumped at every (re)load: the storage card re-reads when S3 changes.
  const [version, setVersion] = useState(0);

  const load = useCallback(async () => {
    const d = await fetch("/admin/config").then((r) => (r.ok ? r.json() : null)).catch(() => null);
    if (!d?.config) return;
    setLines(d.config);
    setSuggestions(d.suggestions ?? {});
    const v: Record<string, string> = {};
    for (const l of d.config as Row[]) v[l.name] = initial(l);
    setDraft(v);
    setVersion((n) => n + 1);
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- loads the panel once
    void load();
  }, [load]);

  const bySection = useMemo(() => {
    const m: Record<string, Row[]> = {};
    for (const l of lines) {
      const s = sectionDe(l.name)?.id ?? "securite";
      (m[s] ??= []).push(l);
    }
    return m;
  }, [lines]);

  const modified = (l: Row) => (draft[l.name] ?? "") !== initial(l);
  const configure = (name: string) => !!lines.find((l) => l.name === name)?.configure;

  async function save(section: Section) {
    const fields = (bySection[section.id] ?? []).filter(modified);
    if (!fields.length) return;
    setSending(section.id);
    try {
      const body = new URLSearchParams();
      for (const l of fields) body.append(l.name, draft[l.name] ?? "");
      const r = await fetch("/admin/config", {
        method: "POST",
        headers: { "X-CSRFToken": csrf, "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
      });
      const d = await r.json().catch(() => ({}));
      showToast({
        body: d?.ok ? t("{s} : enregistré.").replace("{s}", t(section.title)) : t("Échec de l'enregistrement"),
        type: d?.ok ? "info" : "error",
      });
      if (d?.ok) {
        // The panel re-reads (states, sources); typed secrets empty again.
        setTest((x) => ({ ...x, [section.id]: { running: false } }));
        await load();
      }
    } catch {
      showToast({ body: t("Échec de l'enregistrement"), type: "error" });
    } finally {
      setSending(null);
    }
  }

  function cancel(section: Section) {
    setDraft((b) => {
      const v = { ...b };
      for (const l of bySection[section.id] ?? []) v[l.name] = initial(l);
      return v;
    });
  }

  async function tester(section: Section) {
    if (!section.test) return;
    setTest((x) => ({ ...x, [section.id]: { running: true } }));
    try {
      const r = await fetch(`/admin/config/test/${section.test}`, {
        method: "POST",
        headers: { "X-CSRFToken": csrf },
      });
      const d = await r.json().catch(() => ({}));
      // The server answers in French (as all its messages); tServeur shows the
      // English template when the interface is in English.
      const raw: string = d?.message || d?.error || "";
      setTest((x) => ({ ...x, [section.id]: { running: false, ok: !!d?.ok, message: raw ? tServer(raw, t) : "" } }));
    } catch {
      setTest((x) => ({ ...x, [section.id]: { running: false, ok: false, message: t("Le test n'a pas pu être lancé.") } }));
    }
  }

  function clipboardCopy(value: string) {
    void copyText(value, () => showToast({ body: t("Copie impossible depuis ce navigateur."), type: "error" }));
  }

  function field(l: Row) {
    const meta = FIELDS[l.name] ?? { label: l.label };
    const clear = draft[l.name] === CLEAR;
    // Placeholder: the example, else the code default the empty field falls back to.
    const example = meta.example ?? (l.default ? l.default : undefined);
    if (l.secret) {
      return (
        <VStack key={l.name} gap={1}>
          <TextInput
            label={t(meta.label)}
            description={meta.help ? t(meta.help) : undefined}
            type="password"
            value={clear ? "" : (draft[l.name] ?? "")}
            placeholder={clear ? t("(sera effacé à l'enregistrement)")
              : l.configure ? t("•••••• (laisse vide pour garder)") : t("(non défini)")}
            onChange={(v) => setDraft((b) => ({ ...b, [l.name]: v }))}
          />
          {l.configure && !clear ? (
            <HStack>
              <Button label={t("Effacer")} variant="ghost" size="sm"
                onClick={() => setDraft((b) => ({ ...b, [l.name]: CLEAR }))} />
            </HStack>
          ) : null}
        </VStack>
      );
    }
    return (
      <TextInput
        key={l.name}
        label={t(meta.label)}
        description={meta.help ? t(meta.help) : undefined}
        value={draft[l.name] ?? ""}
        placeholder={example ? t("ex. {v}").replace("{v}", example) : undefined}
        onChange={(v) => setDraft((b) => ({ ...b, [l.name]: v }))}
      />
    );
  }

  function toDeclare(section: Section) {
    const uriLines: [string, string | undefined][] =
      section.id === "sso"
        ? [["URI de retour", suggestions.oidc_redirect_uri], ["URI après déconnexion", suggestions.oidc_post_logout]]
        : section.id === "discord"
          ? [["URI de retour", suggestions.discord_redirect_uri]]
          : [];
    const visible = uriLines.filter((x): x is [string, string] => !!x[1]);
    if (!visible.length) return null;
    return (
      <VStack gap={1}>
        <Text type="supporting" color="secondary">{t("À déclarer chez le fournisseur :")}</Text>
        {visible.map(([label, uri]) => (
          <HStack key={label} gap={2} vAlign="center" wrap="wrap">
            <Text type="supporting">{t(label)}</Text>
            <Code>{uri}</Code>
            <Button label={t("Copier")} variant="ghost" size="sm" onClick={() => clipboardCopy(uri)} />
          </HStack>
        ))}
      </VStack>
    );
  }

  if (!lines.length) {
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
      {/* The instance's identity first: what the operator sets once, at deploy. */}
      <BrandingConfig />
      {SECTIONS.map((section) => {
        const fields = bySection[section.id] ?? [];
        if (!fields.length) return null;
        const state = section.state(configure);
        const dirty = fields.some(modified);
        const result = test[section.id];
        return (
          <Card key={section.id}>
            <VStack gap={4}>
              <HStack hAlign="between" vAlign="start" gap={3} wrap="wrap">
                <VStack gap={0}>
                  <HStack gap={2} vAlign="center">
                    <Text weight="semibold">{t(section.title)}</Text>
                    <StatusDot variant={state.variant} label={t(state.label)} />
                    <Text type="supporting" color="secondary">{t(state.label)}</Text>
                  </HStack>
                  <Text type="supporting" color="secondary">{t(section.description)}</Text>
                </VStack>
                {section.test ? (
                  <Button
                    label={t("Tester")}
                    variant="secondary"
                    size="sm"
                    isLoading={!!result?.running}
                    isDisabled={!!result?.running || dirty || state === DISABLED}
                    onClick={() => void tester(section)}
                  />
                ) : null}
              </HStack>
              {dirty && section.test ? (
                <Text type="supporting" color="secondary">{t("Enregistre d'abord : le test porte sur les valeurs enregistrées.")}</Text>
              ) : null}
              {result && !result.running && result.message ? (
                <Banner status={result.ok ? "success" : "error"} title={result.message} />
              ) : null}
              <Grid columns={{ minWidth: 300, max: 2 }} gap={4}>
                {fields.map(field)}
              </Grid>
              {toDeclare(section)}
              <HStack gap={2} hAlign="end">
                <Button label={t("Annuler")} variant="ghost" size="sm" isDisabled={!dirty || sending === section.id}
                  onClick={() => cancel(section)} />
                <Button
                  label={sending === section.id ? t("Enregistrement…") : t("Enregistrer")}
                  variant="primary"
                  size="sm"
                  isDisabled={!dirty || sending !== null}
                  onClick={() => void save(section)}
                />
              </HStack>
            </VStack>
          </Card>
        );
      })}
      <StorageConfig version={version} />
    </VStack>
  );
}
