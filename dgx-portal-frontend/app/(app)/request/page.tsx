"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Heading } from "@astryxdesign/core/Heading";
import { Text } from "@astryxdesign/core/Text";
import { Card } from "@astryxdesign/core/Card";
import { TextInput } from "@astryxdesign/core/TextInput";
import { TextArea } from "@astryxdesign/core/TextArea";
import { Button } from "@astryxdesign/core/Button";
import { Icon } from "@astryxdesign/core/Icon";
import { Link } from "@astryxdesign/core/Link";
import { useToast } from "@astryxdesign/core/Toast";
import { PaperAirplaneIcon } from "@heroicons/react/24/outline";
import { useCsrf } from "@/lib/useCsrf";
import { sendJSON } from "@/lib/api";
import { useT } from "@/lib/i18n";

function RequestForm() {
  const t = useT();
  const csrf = useCsrf();
  const router = useRouter();
  const searchParams = useSearchParams();
  const showToast = useToast();
  const [modelId, setModelId] = useState(searchParams.get("model") || "");
  const [reason, setReason] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function submit() {
    if (!modelId.trim() || !csrf) return;
    setIsSubmitting(true);
    try {
      const r = await sendJSON<{
        ok: boolean; message?: string; error?: string; warning?: string; code?: string;
      }>("/request", csrf, { model_id: modelId.trim(), reason });
      if (!r.ok) {
        // Refus du serveur (demande déjà en attente, identifiant vide…). On
        // RESTE sur la page : renvoyer à l'accueil ferait croire à une demande
        // enregistrée — c'est exactement ce que faisait l'ancien 204 muet.
        showToast({ body: messageRefus(r), type: "error" });
        return;
      }
      // Le message de succès est construit ici (et non repris du serveur) pour
      // rester traduisible ; l'avertissement, lui, vient du serveur : il décrit
      // un échec qu'il est seul à connaître.
      showToast({ body: t("Demande envoyée !"), type: "info" });
      if (r.warning) showToast({ body: r.warning, type: "error" });
      router.push("/");
    } catch {
      showToast({ body: t("Erreur lors de l'envoi de la demande."), type: "error" });
    } finally {
      setIsSubmitting(false);
    }
  }

  /** Les refus stables portent un `code` : on les traduit. Le texte du serveur
   * (français) ne sert que de repli pour les cas non prévus côté client. */
  function messageRefus(r: { error?: string; code?: string }): string {
    switch (r.code) {
      case "deja_en_attente":
        return t("Tu as déjà une demande en attente pour ce modèle.");
      case "identifiant_requis":
        return t("L'identifiant du modèle est requis.");
      default:
        return r.error || t("Erreur lors de l'envoi de la demande.");
    }
  }

  return (
    <Layout
      height="fill"
      content={
        <LayoutContent padding={6} isScrollable>
          <VStack gap={5} maxWidth={560}>
            <VStack gap={1}>
              <Heading level={1}>{t("Demander un modèle")}</Heading>
              <Text type="supporting" color="secondary">{t("L'admin est notifié par Discord et email. Le statut apparaît sur ta page d'accueil.")}</Text>
            </VStack>
            <Card>
              <VStack gap={4}>
                <TextInput
                  label={t("Identifiant HuggingFace *")}
                  value={modelId}
                  onChange={setModelId}
                  placeholder="ex: Qwen/Qwen3-30B-A3B"
                  description={t("Format : organisation/nom-du-modèle")}
                />
                <TextArea
                  label={t("Pourquoi ce modèle ? (optionnel)")}
                  rows={3}
                  value={reason}
                  onChange={setReason}
                  placeholder={t("Ex : tester les capacités de raisonnement, comparer avec Ornith...")}
                />
                <HStack gap={2}>
                  <Button label={t("Annuler")} variant="secondary" onClick={() => router.push("/")} />
                  <Button
                    label={t("Envoyer la demande")}
                    variant="primary"
                    icon={<Icon icon={PaperAirplaneIcon} size="sm" />}
                    isDisabled={!modelId.trim() || isSubmitting}
                    isLoading={isSubmitting}
                    onClick={submit}
                  />
                </HStack>
              </VStack>
            </Card>
            <Text type="supporting" color="secondary">
              {t("Tu ne connais pas l'ID exact ? ")}<Link href="/search">{t("Cherche sur HuggingFace →")}</Link>
            </Text>
          </VStack>
        </LayoutContent>
      }
    />
  );
}

export default function RequestPage() {
  return (
    <Suspense>
      <RequestForm />
    </Suspense>
  );
}
