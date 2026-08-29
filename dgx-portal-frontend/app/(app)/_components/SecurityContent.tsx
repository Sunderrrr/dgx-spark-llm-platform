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
import { useT } from "@/lib/i18n";

type Cred = { id: number; credential_id: string; label: string; created_at: number };
type SecurityState = { enabled: boolean; credentials: Cred[] };

function supportsWebAuthn(): boolean {
  return typeof window !== "undefined" && !!window.PublicKeyCredential && !!navigator.credentials;
}

export function SecurityContent() {
  const t = useT();
  const csrf = useCsrf();
  const showToast = useToast();
  const [sec, setSec] = useState<SecurityState | null>(null);
  const [busy, setBusy] = useState(false);
  // Ré-vérification requise pour toute suppression / bascule : on mémorise
  // l'action en attente, puis on demande le mot de passe.
  const [pendingAction, setPendingAction] = useState<{
    kind: "toggle" | "remove";
    enabled?: boolean;
    credential_id?: string;
  } | null>(null);
  const [pw, setPw] = useState("");

  const load = useCallback(() => {
    getJSON<SecurityState>("/api/security").then(setSec).catch(() => {});
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function addKey() {
    if (!supportsWebAuthn()) {
      showToast({ body: t("Ce navigateur ne supporte pas les clés de sécurité."), type: "error" });
      return;
    }
    setBusy(true);
    try {
      const begin = await sendJSON<{ publicKey: Record<string, unknown>; nonce: string }>(
        "/api/security/register/begin", csrf);
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

  const enabled = sec?.enabled ?? false;
  const credentials = sec?.credentials ?? [];

  return (
    <VStack gap={3}>
      <VStack gap={1}>
        <Heading level={3}>{t("Sécurité")}</Heading>
        <Text type="supporting" color="secondary">
          {t("Double authentification par clé de sécurité (passkey, YubiKey, 1Password) — pas de TOTP.")}
        </Text>
      </VStack>

      <Card>
        <VStack gap={3}>
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
        </VStack>
      </Card>

      <Card>
        <VStack gap={3}>
          <HStack hAlign="between" vAlign="center">
            <Text weight="semibold">{t("Clés de sécurité")}</Text>
            <Button
              label={t("Ajouter une clé")}
              variant="primary"
              size="sm"
              icon={<Icon icon={PlusIcon} size="sm" />}
              isLoading={busy}
              onClick={addKey}
            />
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
                  description={new Date(c.created_at * 1000).toLocaleDateString()}
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

      {pendingAction && (
        <Card>
          <VStack gap={3}>
            <HStack gap={2} vAlign="center">
              <Icon icon={ShieldCheckIcon} size="sm" color="accent" />
              <Text weight="semibold">
                {pendingAction.kind === "remove"
                  ? t("Confirme avec ton mot de passe pour supprimer la clé.")
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
