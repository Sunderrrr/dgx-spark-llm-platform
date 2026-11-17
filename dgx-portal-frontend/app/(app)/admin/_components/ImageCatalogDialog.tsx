"use client";

/**
 * « Add an image model » — a diffusers folder the image sidecar can serve.
 *
 * The sidecar takes a FOLDER, a display name and two inference settings that are
 * NOT universal: a distilled few-step model wants ~4 steps and guidance 1.0,
 * a full one ~28-35 steps and guidance 4-6. They live in the catalog row
 * (image_configs), which is why this dialog asks for them instead of guessing.
 */
import { useState } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Button } from "@astryxdesign/core/Button";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Icon } from "@astryxdesign/core/Icon";
import { PlusIcon } from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { HfModelSearch, type HfPreset } from "./ModelSearch";

/// `black-forest-labs/FLUX.2-klein-4B` → `flux-2-klein-4b`, same rule as the
/// portal's `image_catalog::slug_of` (the server recomputes it anyway).
function slugOf(repo: string): string {
  const last = repo.includes("/") ? repo.split("/").pop()! : repo;
  return last
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
}

export function AddImageDialog({
  isDisabled,
  onSubmit,
}: {
  isDisabled: boolean;
  onSubmit: (params: Record<string, string>) => Promise<{ ok: boolean; error?: string }>;
}) {
  const t = useT();
  const empty = { name: "", hf_model_id: "", slug: "", steps: "28", guidance: "4" };
  const [isOpen, setIsOpen] = useState(false);
  const [m, setM] = useState(empty);
  const [busy, setBusy] = useState(false);

  return (
    <>
      <Button
        label={t("Ajouter un modèle image")}
        variant="secondary"
        size="sm"
        icon={<Icon icon={PlusIcon} size="sm" />}
        isDisabled={isDisabled}
        onClick={() => {
          setM(empty);
          setIsOpen(true);
        }}
      />
      <Dialog isOpen={isOpen} onOpenChange={setIsOpen} purpose="form">
        <DialogHeader
          title={t("Ajouter un modèle image")}
          subtitle={t("Le sidecar sert un dossier diffusers ; les réglages d'inférence dépendent du modèle.")}
        />
        <VStack gap={3}>
          <HfModelSearch
            task="text-to-image"
            isDisabled={isDisabled || busy}
            onPick={(p: HfPreset) => {
              const repo = p.hf_model_id ?? "";
              setM((s) => ({
                ...s,
                hf_model_id: repo,
                slug: slugOf(repo),
                name: s.name || (p.name ?? slugOf(repo)),
              }));
            }}
          />
          <TextInput
            label={t("Nom")}
            description={t("Nom affiché dans le catalogue et sur la page Image.")}
            value={m.name}
            onChange={(v) => setM((s) => ({ ...s, name: v }))}
            size="sm"
          />
          <TextInput
            label={t("Identifiant Hugging Face")}
            description={t("Dépôt diffusers déjà présent sur l'hôte, dans /root/models/<dossier>.")}
            value={m.hf_model_id}
            onChange={(v) => setM((s) => ({ ...s, hf_model_id: v, slug: slugOf(v) }))}
            size="sm"
            placeholder="stabilityai/sdxl-turbo"
          />
          <TextInput
            label={t("Dossier local")}
            description={t("/root/models/<dossier> — c'est ce que le conteneur monte.")}
            value={m.slug}
            onChange={(v) => setM((s) => ({ ...s, slug: v }))}
            size="sm"
          />
          <HStack gap={2}>
            <TextInput
              label={t("Étapes")}
              description={t("Distillé : 4 à 8. Complet : 28 à 50.")}
              value={m.steps}
              onChange={(v) => setM((s) => ({ ...s, steps: v }))}
              size="sm"
            />
            <TextInput
              label="Guidance"
              description={t("Distillé : 1.0. Complet : 4 à 6.")}
              value={m.guidance}
              onChange={(v) => setM((s) => ({ ...s, guidance: v }))}
              size="sm"
            />
          </HStack>
          <Text type="supporting" color="secondary">
            {t("Les poids (souvent 30 Go et plus) se téléchargent sur l'hôte : le catalogue ne fait que déclarer le dossier.")}
          </Text>
          <HStack gap={2} hAlign="end">
            <Button label={t("Annuler")} variant="ghost" size="sm" onClick={() => setIsOpen(false)} />
            <Button
              label={t("Ajouter")}
              variant="primary"
              size="sm"
              icon={<Icon icon={PlusIcon} size="sm" />}
              isDisabled={isDisabled || busy || !m.name.trim() || !m.hf_model_id.includes("/") || !m.slug.trim()}
              onClick={async () => {
                setBusy(true);
                const res = await onSubmit({
                  name: m.name.trim(),
                  hf_model_id: m.hf_model_id.trim(),
                  slug: m.slug.trim(),
                  steps: m.steps.trim(),
                  guidance: m.guidance.trim(),
                });
                setBusy(false);
                if (res.ok) setIsOpen(false);
              }}
            />
          </HStack>
        </VStack>
      </Dialog>
    </>
  );
}
