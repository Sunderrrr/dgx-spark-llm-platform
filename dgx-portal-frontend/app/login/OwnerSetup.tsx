"use client";

// First start of an instance: nobody administers it yet, so the login page
// offers to create the OWNER account instead of signing in (proprietaire.py on
// the portal side). The setup token proves the person is the server's operator:
// it is printed in the portal's log, nowhere a visitor can read it.
import { useState } from "react";
import { VStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Button } from "@astryxdesign/core/Button";
import { Banner } from "@astryxdesign/core/Banner";
import { Icon } from "@astryxdesign/core/Icon";
import { Code } from "@astryxdesign/core/Code";
import { KeyIcon } from "@heroicons/react/24/outline";
import { useT, tServeur } from "@/lib/i18n";

export function OwnerSetup({ csrf }: { csrf: string }) {
  const t = useT();
  const [token, setToken] = useState("");
  const [username, setUsername] = useState("");
  const [fullname, setFullname] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const mismatch = confirm.length > 0 && confirm !== password;
  const ready = token && username && password && confirm === password && csrf && !busy;

  async function submit() {
    if (!ready) return;
    setBusy(true);
    setError("");
    try {
      const r = await fetch("/api/setup", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
        body: JSON.stringify({ token: token.trim(), username: username.trim(), fullname: fullname.trim(), password }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.ok) {
        setError(d?.error ? tServeur(d.error, t) : t("Création impossible."));
        return;
      }
      // Logged in as the owner: the rest is configured from Admin → Config.
      window.location.assign("/admin?tab=config");
    } finally {
      setBusy(false);
    }
  }

  return (
    <VStack gap={3}>
      <VStack gap={1}>
        <Text weight="semibold">{t("Première installation")}</Text>
        <Text type="supporting" color="secondary">
          {t("Crée le compte propriétaire de l'instance. LDAP, SSO et e-mail se configurent ensuite depuis Admin → Config.")}
        </Text>
        <Text type="supporting" color="secondary">
          {t("Le jeton de configuration est dans le journal du portail :")}{" "}
          <Code>{"docker logs dgx-portal 2>&1 | grep setup"}</Code>
        </Text>
      </VStack>
      {error && <Banner status="error" title={error} />}
      <TextInput label={t("Jeton de configuration")} value={token} onChange={setToken} size="lg" hasAutoFocus />
      <TextInput label={t("Identifiant")} value={username} onChange={setUsername} size="lg" />
      <TextInput label={t("Nom affiché (optionnel)")} value={fullname} onChange={setFullname} size="lg" />
      <TextInput label={t("Mot de passe")} type="password" value={password} onChange={setPassword} size="lg" />
      <TextInput
        label={t("Confirmer le mot de passe")}
        type="password"
        value={confirm}
        onChange={setConfirm}
        size="lg"
        status={mismatch ? { type: "error", message: t("Les mots de passe ne correspondent pas.") } : undefined}
        onKeyDown={(e: React.KeyboardEvent) => {
          if (e.key === "Enter") void submit();
        }}
      />
      <Button
        label={t("Créer le compte propriétaire")}
        variant="primary"
        size="lg"
        icon={<Icon icon={KeyIcon} size="sm" />}
        isDisabled={!ready}
        isLoading={busy}
        onClick={() => void submit()}
      />
    </VStack>
  );
}
