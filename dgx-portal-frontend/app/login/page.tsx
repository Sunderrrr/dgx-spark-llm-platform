"use client";

import { useEffect, useState, type CSSProperties } from "react";
import { VStack } from "@astryxdesign/core/Stack";
import { Center } from "@astryxdesign/core/Center";
import { Card } from "@astryxdesign/core/Card";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Banner } from "@astryxdesign/core/Banner";
import { Divider } from "@astryxdesign/core/Divider";
import { ShieldCheckIcon, ArrowRightOnRectangleIcon, CpuChipIcon } from "@heroicons/react/24/outline";
import { fetchCsrfToken } from "@/lib/api";
import { useT } from "@/lib/i18n";

// Auto-hébergées (public/login-bg*.jpg) pour respecter la CSP img-src 'self' —
// pas de dépendance à un CDN externe. Photos : forêts enneigées, paysages nordiques (Unsplash).
const BACKGROUNDS = ["/login-bg.jpg", "/login-bg-2.jpg", "/login-bg-3.jpg"];

export default function LoginPage() {
  const t = useT();
  const [csrf, setCsrf] = useState("");
  const [oidcEnabled, setOidcEnabled] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [bg, setBg] = useState(BACKGROUNDS[0]);

  useEffect(() => {
    // Choisi côté client après le montage (pas dans le rendu SSR) pour éviter
    // un mismatch d'hydratation — un tirage aléatoire différerait entre serveur et client.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setBg(BACKGROUNDS[Math.floor(Math.random() * BACKGROUNDS.length)]);
    fetchCsrfToken().then(setCsrf).catch(() => {});
    fetch("/api/config")
      .then((r) => (r.ok ? r.json() : { oidc_enabled: false }))
      .then((d) => setOidcEnabled(d.oidc_enabled))
      .catch(() => {});
  }, []);

  const pageStyle: CSSProperties = {
    minHeight: "100vh",
    backgroundImage: `linear-gradient(rgba(10,15,20,.45), rgba(10,15,20,.25)), url(${bg})`,
    backgroundSize: "cover",
    backgroundPosition: "center",
  };

  async function submit() {
    if (!username || !password || !csrf) return;
    setIsSubmitting(true);
    setError("");
    try {
      const post = (token: string) =>
        fetch("/login", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": token },
          body: new URLSearchParams({ username, password }).toString(),
        });
      let res = await post(csrf);
      // Un 400 ici, c'est le jeton CSRF, pas le mot de passe : le cookie de
      // session a pu être remplacé entre le chargement de la page et l'envoi.
      // On récupère le jeton courant et on retente une fois, plutôt que
      // d'accuser l'utilisateur d'avoir mal tapé ses identifiants.
      if (res.status === 400) {
        const fresh = await fetchCsrfToken().catch(() => "");
        if (fresh && fresh !== csrf) {
          setCsrf(fresh);
          res = await post(fresh);
        }
      }
      // Le formulaire réussi redirige vers /, dont fetch suit — la présence d'un
      // cookie de session valide est ce qu'on vérifie, pas le corps de la réponse.
      const who = await fetch("/api/whoami", { credentials: "include" });
      if (who.ok) {
        window.location.href = "/";
      } else if (res.status === 400) {
        setError("Session expirée — recharge la page et réessaie.");
      } else {
        setError("Identifiants incorrects.");
      }
    } catch {
      setError("Erreur réseau — réessaie.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <Center axis="both" height="100vh" style={pageStyle}>
      <Card padding={8} width="100%" maxWidth={400}>
        <VStack gap={5} hAlign="stretch">
          <VStack gap={1} hAlign="center">
            <Icon icon={CpuChipIcon} size="lg" color="accent" />
            <Text type="display-1" as="h1">
              Cronos
            </Text>
            <Text type="body" color="secondary" size="sm">{t("Plateforme IA privée · NVIDIA DGX Spark")}</Text>
          </VStack>

          {error && <Banner status="error" title={error} />}

          <VStack gap={3}>
            <TextInput label={t("Identifiant LLDAP")} value={username} onChange={setUsername} size="lg" hasAutoFocus />
            <TextInput
              label={t("Mot de passe")}
              type="password"
              value={password}
              onChange={setPassword}
              size="lg"
              onKeyDown={(e: React.KeyboardEvent) => {
                if (e.key === "Enter") submit();
              }}
            />
            <Button
              label={t("Se connecter")}
              variant="primary"
              size="lg"
              icon={<Icon icon={ArrowRightOnRectangleIcon} size="sm" />}
              isDisabled={!username || !password || isSubmitting}
              isLoading={isSubmitting}
              onClick={submit}
            />
          </VStack>

          {oidcEnabled && (
            <>
              <Divider label={t("Ou")} />
              <Button
                label={t("Se connecter avec le SSO Cronos")}
                variant="secondary"
                size="lg"
                icon={<Icon icon={ShieldCheckIcon} size="sm" />}
                onClick={() => (window.location.href = "/login/sso")}
              />
            </>
          )}
        </VStack>
      </Card>
    </Center>
  );
}
