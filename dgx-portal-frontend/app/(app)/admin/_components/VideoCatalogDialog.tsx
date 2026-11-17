"use client";

/**
 * « Add a video model » — the files a ComfyUI generation loads.
 *
 * The workflow templates used to carry the file names (`unet_name` in node 127,
 * the two VAEs in 119/120), so only the model baked into them could ever run.
 * The catalog stores those names now; ComfyUI resolves them inside its own model
 * folders, hence plain file names and no paths.
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

export function AddVideoDialog({
  isDisabled,
  onSubmit,
}: {
  isDisabled: boolean;
  onSubmit: (params: Record<string, string>) => Promise<{ ok: boolean; error?: string }>;
}) {
  const t = useT();
  const empty = { name: "", hf_model_id: "", unet_file: "", vae_file: "", audio_vae_file: "" };
  const [isOpen, setIsOpen] = useState(false);
  const [m, setM] = useState(empty);
  const [busy, setBusy] = useState(false);

  return (
    <>
      <Button
        label={t("Ajouter un modèle vidéo")}
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
          title={t("Ajouter un modèle vidéo")}
          subtitle={t("Les fichiers que ComfyUI charge pour une génération : noms de fichiers, sans chemin.")}
        />
        <VStack gap={3}>
          <HfModelSearch
            task="text-to-video"
            isDisabled={isDisabled || busy}
            onPick={(p: HfPreset) => {
              const repo = p.hf_model_id ?? "";
              const base = (repo.split("/").pop() ?? repo).toLowerCase().replace(/[^a-z0-9._-]+/g, "-");
              setM((s) => ({
                ...s,
                hf_model_id: repo,
                name: s.name || (p.name ?? base),
                unet_file: s.unet_file || `${base}.safetensors`,
              }));
            }}
          />
          <TextInput label={t("Nom")} value={m.name} onChange={(v) => setM((s) => ({ ...s, name: v }))} size="sm" />
          <TextInput
            label={t("Dépôt Hugging Face")}
            value={m.hf_model_id}
            onChange={(v) => setM((s) => ({ ...s, hf_model_id: v }))}
            size="sm"
            placeholder="MiniMaxAI/MiniMax-H3"
          />
          <TextInput
            label={t("Fichier UNET")}
            description={t("Dans ComfyUI/models/diffusion_models/")}
            value={m.unet_file}
            onChange={(v) => setM((s) => ({ ...s, unet_file: v }))}
            size="sm"
            placeholder="minimax_h3_fl2va_pruned_nvfp4.safetensors"
          />
          <TextInput
            label={t("Fichier VAE vidéo")}
            value={m.vae_file}
            onChange={(v) => setM((s) => ({ ...s, vae_file: v }))}
            size="sm"
            placeholder="minimax_h3_video_vae_fp16.safetensors"
          />
          <TextInput
            label={t("Fichier VAE audio (optionnel)")}
            value={m.audio_vae_file}
            onChange={(v) => setM((s) => ({ ...s, audio_vae_file: v }))}
            size="sm"
          />
          <Text type="supporting" color="secondary">
            {t("Les fichiers se déposent dans ComfyUI côté hôte : le catalogue ne fait que les nommer.")}
          </Text>
          <HStack gap={2} hAlign="end">
            <Button label={t("Annuler")} variant="ghost" size="sm" onClick={() => setIsOpen(false)} />
            <Button
              label={t("Ajouter")}
              variant="primary"
              size="sm"
              icon={<Icon icon={PlusIcon} size="sm" />}
              isDisabled={isDisabled || busy || !m.name.trim() || !m.unet_file.trim() || !m.vae_file.trim()}
              onClick={async () => {
                setBusy(true);
                const res = await onSubmit({
                  name: m.name.trim(),
                  hf_model_id: m.hf_model_id.trim(),
                  unet_file: m.unet_file.trim(),
                  vae_file: m.vae_file.trim(),
                  audio_vae_file: m.audio_vae_file.trim(),
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
