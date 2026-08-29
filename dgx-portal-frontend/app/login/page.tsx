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
import { getPasskeyAssertion } from "@/lib/webauthn";
import { useT } from "@/lib/i18n";

// Self-hosted (public/login-bg*.jpg) to respect the CSP img-src 'self' —
// no dependency on an external CDN. Photos: snowy forests, Nordic landscapes (Unsplash).
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
    // Chosen client-side after mount (not in the SSR render) to avoid
    // a hydration mismatch — a random draw would differ between server and client.
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
      // Server reads the token from the header; a copy in the closure is needed
      // after a CSRF retry, where the React state may not have flushed yet.
      let activeCsrf = csrf;
      let res = await post(activeCsrf);
      // A 400 here is the CSRF token, not the password: the session cookie
      // may have been replaced between the page load and the submission.
      // We fetch the current token and retry once, rather than
      // accusing the user of mistyping their credentials.
      if (res.status === 400) {
        const fresh = await fetchCsrfToken().catch(() => "");
        if (fresh && fresh !== activeCsrf) {
          activeCsrf = fresh;
          setCsrf(fresh);
          res = await post(fresh);
        }
      }
      // 2e facteur : mot de passe/LDAP valide mais la passkey est exigée. Le
      // backend renvoie un JSON {webauthn_required, publicKey, nonce} et ne pose
      // PAS encore la session. On déclenche `navigator.credentials.get` puis on
      // finalise via /api/security/verify-login (qui, lui, ouvre la session).
      const ct = res.headers.get("content-type") ?? "";
      if (res.ok && ct.includes("application/json")) {
        const body = await res.json().catch(() => null);
        if (body?.webauthn_required) {
          await complete2FA(body, activeCsrf);
          return;
        }
      }
      // A successful form redirects to /, which fetch follows — the presence of a
      // valid session cookie is what we check, not the response body.
      const who = await fetch("/api/whoami", { credentials: "include" });
      if (who.ok) {
        // Deliberate full reload: we just obtained a session cookie,
        // and the server render must start over with it.
        window.location.assign("/");
      } else if (res.status === 400) {
        setError(t("Session expirée — recharge la page et réessaie."));
      } else {
        setError(t("Identifiants incorrects."));
      }
    } catch (e) {
      const msg = (e as Error)?.message;
      setError(
        msg === "create-cancelled"
          ? t("Double authentification annulée.")
          : msg || t("Erreur réseau — réessaie."),
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  async function complete2FA(body: { publicKey: Record<string, unknown>; nonce: string }, token: string) {
    const assertion = await getPasskeyAssertion(body.publicKey);
    const res = await fetch("/api/security/verify-login", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", "X-CSRFToken": token },
      body: JSON.stringify({ nonce: body.nonce, credential: assertion }),
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      setError(j?.error || t("Vérification de la clé échouée."));
      return;
    }
    window.location.assign("/");
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
                // /login/sso is a Flask route that redirects to Authentik:
                // a next/link navigation couldn't get out of it.
                onClick={() => (window.location.href = "/login/sso")}
              />
            </>
          )}
        </VStack>
      </Card>
    </Center>
  );
}
