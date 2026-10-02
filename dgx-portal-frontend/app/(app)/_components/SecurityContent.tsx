"use client";

import { useCallback, useEffect, useState } from "react";
import { Card } from "@astryxdesign/core/Card";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { Heading } from "@astryxdesign/core/Heading";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Switch } from "@astryxdesign/core/Switch";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { List, ListItem } from "@astryxdesign/core/List";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Banner } from "@astryxdesign/core/Banner";
import { useToast } from "@astryxdesign/core/Toast";
import {
  KeyIcon,
  PlusIcon,
  TrashIcon,
  ShieldCheckIcon,
} from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { getJSON, sendJSON } from "@/lib/api";
import { createPasskey } from "@/lib/webauthn";
import { useWhoami } from "@/lib/whoami";
import { useT, useLocale } from "@/lib/i18n";
import { SessionsList, type AccountSession } from "./SessionsList";

type Cred = { id: number; credential_id: string; label: string; created_at: number };
type SecurityState = {
  enabled: boolean;
  credentials: Cred[];
  password_managed_by?: string;
  passkey_possible?: boolean;
};
type SessionsState = {
  sessions: AccountSession[];
  local_account?: boolean;
  password_managed_by?: string;
  passkey_possible?: boolean;
};

/** Where the password lives, and what can be said about it.
 *
 * The portal's sources are CUMULATIVE (`user_sources`): an account can be
 * local AND have served in SSO. So we do not guess « SSO » from the absence
 * of a local row — we read what the server answers, and when it does not
 * know, we say so instead of offering a form that will fail.
 */
function motDePasseInfo(gestion: string | undefined) {
  switch (gestion) {
    case "portail":
      return { local: true, passkey: true, note: "" };
    case "annuaire-ldap":
      return {
        local: false,
        passkey: true,
        note: "Compte LDAP : ton mot de passe est celui de l'annuaire. Le portail ne le stocke pas, il le vérifie auprès de l'annuaire — change-le depuis ton administrateur.",
      };
    case "fournisseur-sso":
      return {
        local: false,
        passkey: false,
        note: "Compte SSO : ton mot de passe appartient au fournisseur d'identité, qui seul peut le changer. Le portail n'en connaît aucun.",
      };
    default:
      return {
        local: false,
        passkey: false,
        note: "Le portail n'a pas de mot de passe pour ce compte : il se connecte par l'annuaire (LDAP ou SSO), où le mot de passe se change.",
      };
  }
}

function supportsWebAuthn(): boolean {
  return typeof window !== "undefined" && !!window.PublicKeyCredential && !!navigator.credentials;
}

export function SecurityContent() {
  const t = useT();
  const numLocale = useLocale();
  const csrf = useCsrf();
  const showToast = useToast();
  const { who } = useWhoami();
  const [sec, setSec] = useState<SecurityState | null>(null);
  const [busy, setBusy] = useState(false);
  // Active sessions of the account: list + revocation (single or « les autres »).
  const [sess, setSess] = useState<SessionsState | null>(null);
  const [sessBusy, setSessBusy] = useState<string | null>(null); // session id or "all"
  // Password change (local accounts only).
  const [pwCurrent, setPwCurrent] = useState("");
  const [pwNew, setPwNew] = useState("");
  const [pwConfirm, setPwConfirm] = useState("");
  const [pwBusy, setPwBusy] = useState(false);
  const [pwMsg, setPwMsg] = useState<{ status: "success" | "error"; text: string } | null>(null);
  // Re-verification required for every deletion / toggle: we remember
  // the pending action, then ask for the password.
  const [pendingAction, setPendingAction] = useState<{
    // « register » goes through the SAME dialog as the other two: adding a
    // key is the action that ENABLES two-factor authentication, and the server
    // now requires the password (a stolen cookie was enough to set THEIR key
    // and lock the victim out of their account).
    kind: "toggle" | "remove" | "register";
    enabled?: boolean;
    credential_id?: string;
  } | null>(null);
  const [pw, setPw] = useState("");

  const load = useCallback(() => {
    getJSON<SecurityState>("/api/security").then(setSec).catch(() => {});
    getJSON<SessionsState>("/api/account/sessions").then(setSess).catch(() => {});
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  /** Asks for the password, then actually registers the key (cf. confirmPassword). */
  function addKey() {
    if (!supportsWebAuthn()) {
      showToast({ body: t("Ce navigateur ne supporte pas les clés de sécurité."), type: "error" });
      return;
    }
    setPw("");
    setPendingAction({ kind: "register" });
  }

  /** Creates the passkey — called once the password is provided. */
  async function enregistrerCle() {
    setBusy(true);
    try {
      const begin = await sendJSON<{
        publicKey?: Record<string, unknown>; nonce?: string; error?: string;
      }>("/api/security/register/begin", csrf, { password: pw });
      // `begin` can FAIL (wrong password, SSO account, attempt ceiling):
      // without this test, `createPasskey(undefined)` threw a TypeError and the
      // server message was replaced by a generic « Échec de l'enregistrement »
      // — seen in browser testing. `sendJSON` does not throw on a 4xx,
      // so this is where the refusal is read.
      if (!begin?.publicKey || !begin.nonce) {
        showToast({ body: t(begin?.error || "Échec de l'enregistrement de la clé."), type: "error" });
        return;
      }
      const credential = await createPasskey(begin.publicKey);
      const res = await sendJSON<{ ok: boolean; error?: string }>(
        "/api/security/register/finish", csrf,
        { nonce: begin.nonce, credential, label: "" });
      if (!res.ok) {
        showToast({ body: t(res.error || "Échec de l'enregistrement de la clé."), type: "error" });
        return;
      }
      showToast({ body: t("Clé de sécurité ajoutée."), type: "info" });
      load();
    } catch (e) {
      const msg = (e as Error)?.message;
      showToast({
        body: msg === "create-cancelled"
          ? t("Ajout de clé annulé.")
          : t("Échec de l'enregistrement de la clé."),
        type: "error",
      });
    } finally {
      setBusy(false);
    }
  }

  async function confirmPassword() {
    if (!pendingAction || !pw) return;
    setBusy(true);
    try {
      if (pendingAction.kind === "register") {
        // The key addition does not call the server here: `enregistrerCle` runs the
        // full WebAuthn flow (begin → createPasskey → finish) with this
        // password, handles its messages and reloads the state. The dialog closes
        // beforehand, whatever the result.
        setPendingAction(null);
        await enregistrerCle();
        return;
      }
      let res: { ok: boolean; error?: string };
      if (pendingAction.kind === "remove" && pendingAction.credential_id) {
        res = await sendJSON<{ ok: boolean; error?: string }>(
          "/api/security/remove", csrf,
          { credential_id: pendingAction.credential_id, password: pw });
      } else if (pendingAction.kind === "toggle") {
        res = await sendJSON<{ ok: boolean; error?: string }>(
          "/api/security/toggle", csrf,
          { enabled: !!pendingAction.enabled, password: pw });
      } else {
        return;
      }
      if (!res.ok) {
        showToast({ body: t(res.error || "Échec."), type: "error" });
        return;
      }
      showToast({
        body: pendingAction.kind === "remove" ? t("Clé supprimée.") : t("Double authentification mise à jour."),
        type: "info",
      });
      setPendingAction(null);
      setPw("");
      load();
    } catch {
      showToast({ body: t("Échec."), type: "error" });
    } finally {
      setBusy(false);
    }
  }

  /** Revokes a specific session (server errors are displayed
   * as-is: French messages written for the user). */
  /** Revokes a specific session. `t(res.error || …)` as in the key
   * registration: a free server sentence falls back to itself (silent
   * fallback), a known key is translated — same pattern in the three handlers below. */
  async function revokeSession(id: string) {
    if (!csrf || sessBusy) return;
    setSessBusy(id);
    try {
      const res = await sendJSON<{ ok: boolean; error?: string }>(
        "/api/account/sessions/revoke", csrf, { id });
      if (!res.ok) {
        showToast({ body: t(res.error || "Échec."), type: "error" });
        return;
      }
      showToast({ body: t("Session révoquée."), type: "info" });
      load();
    } catch {
      showToast({ body: t("Échec."), type: "error" });
    } finally {
      setSessBusy(null);
    }
  }

  /** Revokes every session except this one. */
  async function revokeOtherSessions() {
    if (!csrf || sessBusy) return;
    setSessBusy("all");
    try {
      const res = await sendJSON<{ ok: boolean; error?: string }>(
        "/api/account/sessions/revoke", csrf, { all: true });
      if (!res.ok) {
        showToast({ body: t(res.error || "Échec."), type: "error" });
        return;
      }
      showToast({ body: t("Sessions révoquées."), type: "info" });
      load();
    } catch {
      showToast({ body: t("Échec."), type: "error" });
    } finally {
      setSessBusy(null);
    }
  }

  /** Password change (local accounts). The server answers 400 with
   * {ok:false, error}: French message displayed as-is. */
  async function changePassword() {
    if (!csrf || pwBusy) return;
    if (pwNew.length < 8 || pwNew !== pwConfirm) return;
    setPwBusy(true);
    setPwMsg(null);
    try {
      const res = await sendJSON<{ ok: boolean; error?: string }>(
        "/api/account/password", csrf, { current: pwCurrent, new: pwNew });
      if (!res.ok) {
        setPwMsg({ status: "error", text: t(res.error || "Échec.") });
        return;
      }
      setPwCurrent("");
      setPwNew("");
      setPwConfirm("");
      setPwMsg({ status: "success", text: t("Mot de passe modifié.") });
    } catch {
      setPwMsg({ status: "error", text: t("Échec.") });
    } finally {
      setPwBusy(false);
    }
  }

  const enabled = sec?.enabled ?? false;
  const credentials = sec?.credentials ?? [];
  const sessions = sess?.sessions ?? [];
  const otherSessions = sessions.filter((s) => !s.current);
  // Password source: stated by the server (`/api/whoami` then
  // `/api/account/sessions` as fallback). As long as we do not know, we do NOT
  // display the form: the old `?? true` assumed « compte local » by default and
  // therefore showed a password changer to an SSO account — the server
  // refused it afterwards, but only after the fact.
  const gestion = who?.password_managed_by ?? sess?.password_managed_by;
  const info = motDePasseInfo(gestion);
  const passkeyPossible = who?.passkey_possible ?? sess?.passkey_possible ?? info.passkey;
  // Account deletion: two distinct gestures depending on who owns the account.
  const [suppression, setSuppression] = useState(false);
  const [confirmSuppression, setConfirmSuppression] = useState("");
  const [mdpSuppression, setMdpSuppression] = useState("");
  const [suppressionBusy, setSuppressionBusy] = useState(false);
  const [suppressionMsg, setSuppressionMsg] = useState<string | null>(null);

  /** Deletes the account (local) or erases its data (directory account).
   *
   * The server runs `deprovisionner_compte` — the same path as a deletion
   * by an admin — then cuts the sessions: one therefore cannot stay
   * logged in afterwards. We say so, then head back to /login. */
  async function supprimerCompte() {
    if (!csrf || suppressionBusy) return;
    setSuppressionBusy(true);
    setSuppressionMsg(null);
    try {
      const res = await sendJSON<{
        ok: boolean; deleted?: boolean; error?: string; warning?: string;
      }>("/api/account/delete", csrf, {
        confirm: confirmSuppression.trim(),
        password: mdpSuppression,
      });
      if (!res.ok) {
        setSuppressionMsg(t(res.error || "La suppression a échoué."));
        return;
      }
      setSuppressionMsg(
        res.deleted
          ? t("Ton compte est supprimé. Tu vas être déconnecté…")
          : t("Tes données du portail sont effacées. Ton compte, lui, existe toujours dans l'annuaire : demande à un administrateur de le bloquer si tu pars.")
          + (res.warning ? ` ${res.warning}` : ""),
      );
      // Session already revoked server-side: staying here would make no sense.
      window.setTimeout(() => window.location.assign("/login"), res.deleted ? 2500 : 6000);
    } catch {
      setSuppressionMsg(t("Le serveur n'a pas répondu — réessaie."));
    } finally {
      setSuppressionBusy(false);
    }
  }

  return (
    <VStack gap={3}>
      <VStack gap={1}>
        <Heading level={3}>{t("Sécurité")}</Heading>
        <Text type="supporting" color="secondary">
          {t("Double authentification, sessions actives et mot de passe de ton compte.")}
        </Text>
      </VStack>

      <Card>
        <VStack gap={3}>
          {passkeyPossible ? (
            <>
              <Switch
                label={t("Exiger une clé de sécurité au login")}
                value={enabled}
                isDisabled={!credentials.length && !enabled}
                onChange={(v) => setPendingAction({ kind: "toggle", enabled: v })}
              />
              <Text type="supporting" color="secondary">
                {enabled
                  ? t("Ta clé sera demandée après le mot de passe (local ou LDAP).")
                  : t("Une fois activée, la clé est exigée à chaque connexion.")}
              </Text>
            </>
          ) : (
            /* The portal cannot re-verify an SSO password, yet that is
               what 2FA requires to be changed: offering the toggle
               would only lead to « Mot de passe incorrect », which is false. */
            <Banner
              status="info"
              title={t("Compte SSO : la double authentification se règle chez ton fournisseur d'identité, pas ici.")}
            />
          )}
        </VStack>
      </Card>

      <Card>
        <VStack gap={3}>
          <HStack hAlign="between" vAlign="center">
            <Text weight="semibold">{t("Clés de sécurité")}</Text>
            {passkeyPossible && (
              <Button
                label={t("Ajouter une clé")}
                variant="primary"
                size="sm"
                icon={<Icon icon={PlusIcon} size="sm" />}
                isLoading={busy}
                onClick={addKey}
              />
            )}
          </HStack>

          {credentials.length === 0 ? (
            <EmptyState
              icon={<Icon icon={KeyIcon} size="lg" />}
              title={t("Aucune clé de sécurité")}
              description={t("Ajoute une passkey, une YubiKey ou une clé 1Password pour sécuriser ton compte.")}
            />
          ) : (
            <List>
              {credentials.map((c) => (
                <ListItem
                  key={c.id}
                  startContent={<Icon icon={KeyIcon} size="sm" color="secondary" />}
                  label={t(c.label)}
                  description={new Date(c.created_at * 1000).toLocaleDateString(numLocale)}
                  endContent={
                    <Button
                      label={t("Supprimer")}
                      variant="ghost"
                      size="sm"
                      isIconOnly
                      icon={<Icon icon={TrashIcon} size="sm" />}
                      onClick={() => {
                        setPendingAction({ kind: "remove", credential_id: c.credential_id });
                        setPw("");
                      }}
                    />
                  }
                />
              ))}
            </List>
          )}
        </VStack>
      </Card>

      {/* Active sessions — same rows as the admin-side detail */}
      <Card>
        <VStack gap={3}>
          <HStack hAlign="between" vAlign="center" wrap="wrap" gap={2}>
            <VStack gap={0}>
              <Text weight="semibold">{t("Sessions actives")}</Text>
              <Text type="supporting" color="secondary">
                {t("Les appareils connectés à ton compte — révoque ceux que tu ne reconnais pas.")}
              </Text>
            </VStack>
            <Button
              label={t("Révoquer les autres sessions")}
              variant="secondary"
              size="sm"
              isLoading={sessBusy === "all"}
              isDisabled={otherSessions.length === 0 || (!!sessBusy && sessBusy !== "all")}
              onClick={revokeOtherSessions}
            />
          </HStack>
          <SessionsList
            sessions={sessions}
            rowAction={(s) => (
              <Button
                label={t("Révoquer")}
                variant="ghost"
                size="sm"
                isLoading={sessBusy === s.id}
                isDisabled={!!sessBusy && sessBusy !== s.id}
                onClick={() => revokeSession(s.id)}
              />
            )}
          />
        </VStack>
      </Card>

      {/* Password — local accounts; LDAP/SSO manage it in the directory */}
      <Card>
        <VStack gap={3}>
          <VStack gap={0}>
            <Text weight="semibold">{t("Mot de passe")}</Text>
            <Text type="supporting" color="secondary">
              {t("Le mot de passe sert au login et à confirmer les actions sensibles.")}
            </Text>
          </VStack>
          {info.local ? (
            <VStack gap={3}>
              {pwMsg && <Banner status={pwMsg.status} title={pwMsg.text} />}
              <TextInput
                label={t("Mot de passe actuel")}
                type="password"
                value={pwCurrent}
                onChange={(v) => { setPwCurrent(v); setPwMsg(null); }}
              />
              <TextInput
                label={t("Nouveau mot de passe (8 caractères min.)")}
                type="password"
                value={pwNew}
                onChange={(v) => { setPwNew(v); setPwMsg(null); }}
              />
              <VStack gap={1}>
                <TextInput
                  label={t("Confirmer le nouveau mot de passe")}
                  type="password"
                  value={pwConfirm}
                  onChange={(v) => { setPwConfirm(v); setPwMsg(null); }}
                  onKeyDown={(e: React.KeyboardEvent) => {
                    if (e.key === "Enter") changePassword();
                  }}
                />
                {pwConfirm && pwNew !== pwConfirm ? (
                  <Text type="supporting" color="secondary">
                    {t("Les deux nouveaux mots de passe ne correspondent pas.")}
                  </Text>
                ) : null}
              </VStack>
              <HStack gap={2}>
                <Button
                  label={t("Changer le mot de passe")}
                  variant="primary"
                  size="sm"
                  isLoading={pwBusy}
                  isDisabled={!pwCurrent || pwNew.length < 8 || pwNew !== pwConfirm}
                  onClick={changePassword}
                />
              </HStack>
            </VStack>
          ) : (
            <Banner status="info" title={t(info.note)} />
          )}
        </VStack>
      </Card>

      {/* Leaving the platform — the gesture was completely missing: it
          took an administrator to delete one's own account. */}
      <Card>
        <VStack gap={3}>
          <VStack gap={0}>
            <Text weight="semibold">{t("Supprimer mon compte")}</Text>
            <Text type="supporting" color="secondary">
              {info.local
                ? t("Cela supprime ton compte, tes clés API, tes conversations et ta mémoire. C'est définitif.")
                : t("Cela efface tes données du portail (clés, conversations, mémoire, préférences). Ton compte, lui, appartient à l'annuaire : il continue d'exister.")}
            </Text>
          </VStack>
          {suppressionMsg ? <Banner status="info" title={suppressionMsg} /> : null}
          {suppression ? (
            <VStack gap={3}>
              <TextInput
                label={t("Tapez DELETE pour confirmer")}
                value={confirmSuppression}
                onChange={setConfirmSuppression}
              />
              {info.passkey && (
                <TextInput
                  label={t("Ton mot de passe")}
                  type="password"
                  value={mdpSuppression}
                  onChange={setMdpSuppression}
                  onKeyDown={(e: React.KeyboardEvent) => {
                    if (e.key === "Enter") supprimerCompte();
                  }}
                />
              )}
              <HStack gap={2}>
                <Button
                  label={info.local ? t("Supprimer définitivement mon compte") : t("Effacer mes données")}
                  variant="primary"
                  size="sm"
                  isLoading={suppressionBusy}
                  isDisabled={confirmSuppression.trim() !== "DELETE"
                    || (info.passkey && !mdpSuppression)}
                  onClick={supprimerCompte}
                />
                <Button
                  label={t("Annuler")}
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    setSuppression(false);
                    setConfirmSuppression("");
                    setMdpSuppression("");
                  }}
                />
              </HStack>
            </VStack>
          ) : (
            <HStack>
              <Button
                label={t("Supprimer mon compte")}
                variant="secondary"
                size="sm"
                icon={<Icon icon={TrashIcon} size="sm" />}
                onClick={() => setSuppression(true)}
              />
            </HStack>
          )}
        </VStack>
      </Card>

      {pendingAction && (
        <Card>
          <VStack gap={3}>
            <HStack gap={2} vAlign="center">
              <Icon icon={ShieldCheckIcon} size="sm" color="accent" />
              <Text weight="semibold">
                {pendingAction.kind === "remove"
                  ? t("Confirme avec ton mot de passe pour supprimer la clé.")
                  : pendingAction.kind === "register"
                    ? t("Confirme avec ton mot de passe pour ajouter une clé.")
                    : t("Confirme avec ton mot de passe pour changer la double authentification.")}
              </Text>
            </HStack>
            <TextInput
              label={t("Mot de passe")}
              type="password"
              value={pw}
              onChange={setPw}
              onKeyDown={(e: React.KeyboardEvent) => {
                if (e.key === "Enter") confirmPassword();
              }}
            />
            <HStack gap={2}>
              <Button
                label={t("Confirmer")}
                variant="primary"
                size="sm"
                isLoading={busy}
                isDisabled={!pw}
                onClick={confirmPassword}
              />
              <Button
                label={t("Annuler")}
                variant="secondary"
                size="sm"
                onClick={() => {
                  setPendingAction(null);
                  setPw("");
                }}
              />
            </HStack>
          </VStack>
        </Card>
      )}
    </VStack>
  );
}
