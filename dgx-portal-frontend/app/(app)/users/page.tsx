"use client";

import { useEffect, useState } from "react";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { Center } from "@astryxdesign/core/Center";
import { VStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { ShieldExclamationIcon } from "@heroicons/react/24/outline";
import { getJSON, ForbiddenError } from "@/lib/api";
import { useCsrf } from "@/lib/useCsrf";
import { useT } from "@/lib/i18n";
import { UsersSection } from "../admin/_components/UsersSection";

export default function UsersPage() {
  const t = useT();
  const csrf = useCsrf();
  // Réservé aux admins, exactement comme /admin. La nav ne montre l'onglet
  // qu'aux admins, mais on vérifie aussi ici (accès direct par URL) : l'API
  // /api/admin/users renvoie 403 → on affiche l'état « accès refusé » plutôt
  // qu'une page vide.
  const [status, setStatus] = useState<"loading" | "ok" | "forbidden">("loading");

  useEffect(() => {
    getJSON("/api/admin/users")
      .then(() => setStatus("ok"))
      .catch((e) => setStatus(e instanceof ForbiddenError ? "forbidden" : "ok"));
  }, []);

  if (status === "forbidden") {
    return (
      <Layout
        height="fill"
        content={
          <LayoutContent padding={6} isScrollable>
            <Center axis="both" height="100%">
              <EmptyState
                icon={<Icon icon={ShieldExclamationIcon} size="lg" color="secondary" />}
                title={t("Accès réservé aux administrateurs")}
                description={t("Ton compte n'a pas les droits nécessaires pour voir cette page.")}
                actions={<Button label={t("Retour à l'accueil")} variant="primary" href="/" />}
              />
            </Center>
          </LayoutContent>
        }
      />
    );
  }

  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable>
          <VStack gap={6}>
            <VStack gap={1}>
              <Heading level={1}>{t("Utilisateurs")}</Heading>
              <Text type="supporting" color="secondary">
                {t("Crée et gère les comptes locaux, les groupes, les quotas et les droits.")}
              </Text>
            </VStack>
            {status === "ok" && <UsersSection csrf={csrf} />}
          </VStack>
        </LayoutContent>
      }
    />
  );
}
