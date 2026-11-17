"use client";

/**
 * « Add a decision model » — a GGUF the System One engine can load.
 *
 * The decision engine is a SECOND llama.cpp on its own port; it answers typed
 * questions on `/v1/systemone` and has no chat endpoint. Until 2026-11-17 the
 * model came from `DECISIONS_MODEL`, so changing it meant editing `.env` and
 * restarting the runner — which kills the chat model. A catalog row now travels
 * with the start request and only affects that launch.
 */
import { useState } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Button } from "@astryxdesign/core/Button";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Icon } from "@astryxdesign/core/Icon";
import { PlusIcon } from "@heroicons/react/24/outline";
import { useT } from "@/lib/i18n";
import { HfModelSearch, type HfPreset } from "./ModelSearch";

const DEFAULT_ARGS = "--ctx-size 8192 --ubatch-size 2048 --n-gpu-layers 999 --flash-attn on";

export function DecisionsCatalogDialog({
  isDisabled,
  onSubmit,
}: {
  isDisabled: boolean;
  onSubmit: (params: Record<string, string>) => Promise<{ ok: boolean; error?: string }>;
}) {
  const t = useT();
  const empty = { name: "", hf_model_id: "", args: DEFAULT_ARGS };
  const [isOpen, setIsOpen] = useState(false);
  const [m, setM] = useState(empty);
  const [busy, setBusy] = useState(false);

  return (
    <>
      <Button
        label={t("Ajouter un modèle de décision")}
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
          title={t("Ajouter un modèle de décision")}
          subtitle={t("Un GGUF qui répond sur /v1/systemone : pas un modèle de conversation.")}
        />
        <VStack gap={3}>
          <HfModelSearch
            task="text-generation"
            isDisabled={isDisabled || busy}
            onPick={(p: HfPreset) => {
              const repo = p.hf_model_id ?? "";
              setM((s) => ({
                ...s,
                hf_model_id: repo,
                name: s.name || (p.name ?? repo.split("/").pop() ?? "decision"),
              }));
            }}
          />
          <TextInput label={t("Nom")} value={m.name} onChange={(v) => setM((s) => ({ ...s, name: v }))} size="sm" />
          <TextInput
            label={t("Identifiant (dépôt ou local:<dossier>)")}
            value={m.hf_model_id}
            onChange={(v) => setM((s) => ({ ...s, hf_model_id: v }))}
            size="sm"
            placeholder="local:clef-q4km"
          />
          <TextInput
            label={t("Arguments du moteur")}
            description={t("Le moteur de décision évalue le prompt en un lot : un grand contexte ne sert à rien.")}
            value={m.args}
            onChange={(v) => setM((s) => ({ ...s, args: v }))}
            size="sm"
          />
          <HStack gap={2} hAlign="end">
            <Button label={t("Annuler")} variant="ghost" size="sm" onClick={() => setIsOpen(false)} />
            <Button
              label={t("Ajouter")}
              variant="primary"
              size="sm"
              icon={<Icon icon={PlusIcon} size="sm" />}
              isDisabled={isDisabled || busy || !m.name.trim() || !m.hf_model_id.trim()}
              onClick={async () => {
                setBusy(true);
                const res = await onSubmit({
                  name: m.name.trim(),
                  hf_model_id: m.hf_model_id.trim(),
                  args: m.args.trim(),
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
