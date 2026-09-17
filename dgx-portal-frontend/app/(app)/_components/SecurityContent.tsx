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

/** D'où vient le mot de passe, et ce qu'on peut en dire.
 *
 * Les sources du portail sont CUMULATIVES (`user_sources`) : un compte peut
 * être local ET avoir servi en SSO. On ne devine donc pas « SSO » à partir de
 * l'absence de ligne locale — on lit ce que le serveur répond, et quand il ne
 * sait pas, on le dit au lieu de proposer un formulaire qui échouera.
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
  // Sessions actives du compte : liste + révocation (unitaire ou « les autres »).
  const [sess, setSess] = useState<SessionsState | null>(null);
  const [sessBusy, setSessBusy] = useState<string | null>(null); // id de session ou "all"
  // Changement de mot de passe (comptes locaux uniquement).
  const [pwCurrent, setPwCurrent] = useState("");
  const [pwNew, setPwNew] = useState("");
  const [pwConfirm, setPwConfirm] = useState("");
  const [pwBusy, setPwBusy] = useState(false);
  const [pwMsg, setPwMsg] = useState<{ status: "success" | "error"; text: string } | null>(null);
  // Ré-vérification requise pour toute suppression / bascule : on mémorise
  // l'action en attente, puis on demande le mot de passe.
  const [pendingAction, setPendingAction] = useState<{
    // « register » passe par le MÊME dialogue que les deux autres : ajouter une
    // clé est l'action qui ACTIVE la double authentification, et le serveur exige
    // désormais le mot de passe (un cookie volé suffisait à poser SA clé et à
    // enfermer la victime hors de son compte).
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

  /** Demande le mot de passe, puis enregistre réellement la clé (cf. confirmPassword). */
  function addKey() {
    if (!supportsWebAuthn()) {
      showToast({ body: t("Ce navigateur ne supporte pas les clés de sécurité."), type: "error" });
      return;
    }
    setPw("");
    setPendingAction({ kind: "register" });
  }

  /** Crée la passkey — appelé une fois le mot de passe fourni. */
  async function enregistrerCle() {
    setBusy(true);
    try {
      const begin = await sendJSON<{
        publicKey?: Record<string, unknown>; nonce?: string; error?: string;
      }>("/api/security/register/begin", csrf, { password: pw });
      // `begin` peut ÉCHOUER (mot de passe faux, compte SSO, plafond d'essais) :
      // sans ce test, `createPasskey(undefined)` levait un TypeError et le
      // message du serveur était remplacé par un « Échec de l'enregistrement »
      // générique — constaté en test navigateur. `sendJSON` ne lève pas sur un
      // 4xx, donc c'est bien ici que le refus se lit.
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
        // L'ajout de clé n'appelle pas le serveur ici : `enregistrerCle` fait le
        // flux WebAuthn complet (begin → createPasskey → finish) avec ce mot de
        // passe, gère ses messages et recharge l'état. Le dialogue se ferme
        // avant, quel que soit le résultat.
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

  /** Révoque une session précise (les erreurs du serveur sont affichées
   * telles quelles : messages français pensés pour l'utilisateur). */
  /** Révoque une session précise. `t(res.error || …)` comme dans l'enregistrement
   * de clé : une phrase serveur libre retombe sur elle-même (fallback silencieux),
   * une clé connue est traduite — même motif aux trois handlers ci-dessous. */
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

  /** Révoque toutes les sessions sauf celle-ci. */
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

  /** Changement de mot de passe (comptes locaux). Le serveur répond 400 avec
   * {ok:false, error} : message français affiché tel quel. */
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
  // Source du mot de passe : dite par le serveur (`/api/whoami` puis
  // `/api/account/sessions` en repli). Tant qu'on ne sait pas, on n'affiche PAS
  // le formulaire : l'ancien `?? true` supposait « compte local » par défaut et
  // montrait donc un changeur de mot de passe à un compte SSO — le serveur le
  // refusait ensuite, mais après coup.
  const gestion = who?.password_managed_by ?? sess?.password_managed_by;
  const info = motDePasseInfo(gestion);
  const passkeyPossible = who?.passkey_possible ?? sess?.passkey_possible ?? info.passkey;
  // Suppression de compte : deux gestes distincts selon qui détient le compte.
  const [suppression, setSuppression] = useState(false);
  const [confirmSuppression, setConfirmSuppression] = useState("");
  const [mdpSuppression, setMdpSuppression] = useState("");
  const [suppressionBusy, setSuppressionBusy] = useState(false);
  const [suppressionMsg, setSuppressionMsg] = useState<string | null>(null);

  /** Supprime le compte (local) ou efface ses données (compte d'annuaire).
   *
   * Le serveur exécute `deprovisionner_compte` — le même chemin que la
   * suppression par un admin — puis coupe les sessions : on ne peut donc pas
   * rester connecté après. On l'annonce, puis on repart sur /login. */
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
      // Session déjà révoquée côté serveur : rester ici n'aurait aucun sens.
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
            /* Le portail ne peut pas re-vérifier un mot de passe SSO, or c'est
               ce que la 2FA exige pour être modifiée : proposer l'interrupteur
               ne mènerait qu'à « Mot de passe incorrect », ce qui est faux. */
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

      {/* Sessions actives — mêmes lignes que le détail côté admin */}
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

      {/* Mot de passe — comptes locaux ; LDAP/SSO le gèrent dans l'annuaire */}
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

      {/* Quitter la plateforme — le geste manquait complètement : il fallait
          passer par un administrateur pour supprimer son propre compte. */}
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
