"use client";

/**
 * « Add a voice model » — the engines the voice sidecar can actually serve.
 *
 * Voice is not like the chat catalog: the sidecar starts ONE engine, and the
 * backend validates the repository against a closed list (`VOICE_REPO_IDS` in
 * the portal). Offering a Hugging Face search here would propose repositories
 * that the container cannot build, so the list IS the catalogue — searchable,
 * which is what the operator asked for, without promising what does not exist.
 */
import { useMemo, useState } from "react";
import { VStack, HStack } from "@astryxdesign/core/Stack";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Button } from "@astryxdesign/core/Button";
import { List, ListItem } from "@astryxdesign/core/List";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Icon } from "@astryxdesign/core/Icon";
import { useT } from "@/lib/i18n";

/// `<PlusIcon>` from the same icon set the admin already uses.
import { PlusIcon } from "@heroicons/react/24/outline";

/// The engines the voice sidecar supports, mirrors `VOICE_REPO_IDS`.
const ENGINES: { repo: string; label: string; note: string }[] = [
  { repo: "kyutai/tts-1.6b-en_fr", label: "Kyutai TTS 1.6B", note: "fr · en — voix fr-a à fr-d, parle pendant une conversation" },
  { repo: "custom-voice", label: "Voix personnalisée", note: "ta propre voix (Fish Audio S2 Pro + ta référence, voice-custom/), parle pendant une conversation" },
  { repo: "Qwen3-TTS-12Hz-1.7B-Base", label: "Qwen3-TTS 1.7B", note: "multilingue, synthèse hors conversation" },
  { repo: "Qwen3-TTS-12Hz-0.6B-Base", label: "Qwen3-TTS 0.6B", note: "version légère" },
  { repo: "chatterbox-multilingual", label: "Chatterbox multilingual", note: "multilingue, clonage de voix" },
  { repo: "chatterbox-turbo", label: "Chatterbox Turbo", note: "rapide" },
  { repo: "chatterbox", label: "Chatterbox", note: "voix anglaise de référence" },
];

export function AddVoiceDialog({
  isDisabled,
  onSubmit,
}: {
  isDisabled: boolean;
  onSubmit: (params: Record<string, string>) => Promise<{ ok: boolean; error?: string }>;
}) {
  const t = useT();
  const [isOpen, setIsOpen] = useState(false);
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const hits = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return ENGINES;
    return ENGINES.filter(
      (e) => e.label.toLowerCase().includes(needle) || e.repo.toLowerCase().includes(needle) || e.note.includes(needle),
    );
  }, [q]);

  const pick = (repo: string) => {
    setPicked(repo);
    // A readable default name from the repository, editable before adding.
    const base = repo.includes("/") ? repo.split("/").pop()! : repo;
    setName(base.toLowerCase().replace(/[^a-z0-9.]+/g, "-"));
  };

  return (
    <>
      <Button
        label={t("Ajouter un modèle voix")}
        variant="secondary"
        size="sm"
        icon={<Icon icon={PlusIcon} size="sm" />}
        isDisabled={isDisabled}
        onClick={() => {
          setQ("");
          setPicked(null);
          setName("");
          setIsOpen(true);
        }}
      />
      <Dialog isOpen={isOpen} onOpenChange={setIsOpen} purpose="form">
        <DialogHeader
          title={t("Ajouter un modèle voix")}
          subtitle={t("Le sidecar vocal sert un moteur à la fois : seuls ceux qu'il sait construire sont proposés.")}
        />
        <VStack gap={3}>
          <TextInput label={t("Rechercher un moteur")} value={q} onChange={setQ} size="sm" placeholder="kyutai, chatterbox…" />
          <List>
            {hits.map((e) => (
              <ListItem
                key={e.repo}
                label={e.label}
                description={`${e.repo} · ${e.note}`}
                onClick={() => pick(e.repo)}
                endContent={
                  <Button
                    label={picked === e.repo ? t("Sélectionné") : t("Utiliser")}
                    variant="secondary"
                    size="sm"
                    isDisabled={isDisabled || picked === e.repo}
                    onClick={() => pick(e.repo)}
                  />
                }
              />
            ))}
          </List>
          {hits.length === 0 ? <Text type="supporting" color="secondary">{t("Aucun moteur ne correspond.")}</Text> : null}
          <TextInput
            label={t("Nom")}
            description={t("Nom affiché, et nom du modèle dans l'API.")}
            value={name}
            onChange={setName}
            size="sm"
          />
          <HStack gap={2} hAlign="end">
            <Button label={t("Annuler")} variant="ghost" size="sm" onClick={() => setIsOpen(false)} />
            <Button
              label={t("Ajouter")}
              variant="primary"
              size="sm"
              icon={<Icon icon={PlusIcon} size="sm" />}
              isDisabled={isDisabled || busy || !picked || !name.trim()}
              onClick={async () => {
                setBusy(true);
                const res = await onSubmit({ name: name.trim(), repo_id: picked ?? "" });
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
